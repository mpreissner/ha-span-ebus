"""SPAN cloud authentication — AWS Cognito SRP.

The panel's local REST tier is dead on this firmware (502 on every `/api/*`), so
the only way to reach live data today is SPAN's cloud, which authenticates
against an AWS Cognito user pool using SRP (Secure Remote Password). SRP never
sends the password over the wire: the client proves knowledge of it through a
zero-knowledge exchange, and Cognito returns JWTs.

The resulting **access token** is the `Bearer` credential for the gRPC data
plane at `mobilefrontend.prd.span.io` (see docs/CLOUD-FLOW.md).

We implement SRP directly against the Cognito JSON API rather than pulling in
boto3/warrant: the math is stdlib-only (`hashlib`, `hmac`, `secrets`) and the
two HTTP calls are plain `requests`. This keeps the bridge's dependency surface
small enough to vendor into a HACS integration later.

**Password handling.** This module never stores, logs, or transmits the
password. `authenticate()` takes it as a parameter; callers are expected to
prompt the user for it at runtime (`getpass`) and hold it only for the duration
of the call. Once authenticated, only the refresh token is cached — the password
is discarded.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# --- Cognito pool identity (from live-app capture, docs/CLOUD-FLOW.md) --------

REGION = "us-west-2"
USER_POOL_ID = "us-west-2_xqz9y67ID"
CLIENT_ID = "21vd907gimk5ctc0pop94l2lip"
# The FIPS endpoint is what the live app uses; the plain endpoint works too.
COGNITO_HOST = "cognito-idp-fips.us-west-2.amazonaws.com"

# Pool name for the SRP hash is the pool id *without* its region prefix.
POOL_NAME = USER_POOL_ID.split("_", 1)[1]

AUTH_TIMEOUT = 20

# --- SRP group (RFC 5054 3072-bit prime, the group AWS Cognito uses) ---------

_N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
    "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
    "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31"
    "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
_N = int(_N_HEX, 16)
_G = 2
# HKDF info string, fixed by the AWS SRP implementation.
_INFO_BITS = b"Caldera Derived Key"


class CloudAuthError(RuntimeError):
    """Cognito authentication failed."""


@dataclass(frozen=True)
class CloudTokens:
    """JWTs returned by Cognito. `access_token` is the data-plane credential."""

    access_token: str
    id_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds when the access token expires
    token_type: str = "Bearer"

    @property
    def expired(self) -> bool:
        # Refresh a minute early to avoid racing the expiry mid-request.
        return time.time() >= self.expires_at - 60


# --- SRP primitives ----------------------------------------------------------


def _hash_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hex_hash(hex_str: str) -> str:
    return _hash_hex(bytes.fromhex(hex_str))


def _pad_hex(value: int | str) -> str:
    """Hex-encode with an even length, prefixing 00 when the top bit is set.

    SRP treats these values as unsigned; a leading byte >= 0x80 would otherwise
    be read as a negative two's-complement number by the other side.
    """
    h = value if isinstance(value, str) else format(value, "x")
    if len(h) % 2:
        h = "0" + h
    elif h[0] in "89abcdefABCDEF":
        h = "00" + h
    return h


def _compute_hkdf(ikm: bytes, salt: bytes) -> bytes:
    """HKDF-SHA256 as AWS uses it: extract, then one expand block, 16 bytes."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = hmac.new(prk, _INFO_BITS + b"\x01", hashlib.sha256).digest()
    return okm[:16]


def _k() -> int:
    return int(_hex_hash(_pad_hex(_N) + _pad_hex(_G)), 16)


def _generate_a() -> tuple[int, int]:
    """Return (a, A) with A = g^a mod N, retrying if A ≡ 0 (mod N)."""
    while True:
        a = secrets.randbits(256) % _N
        A = pow(_G, a, _N)
        if A % _N != 0:
            return a, A


def _cognito_timestamp(now: datetime.datetime | None = None) -> str:
    """Format like ``Wed Aug 16 20:00:00 UTC 2026`` — C-locale, unpadded day.

    Built by hand so the process locale can't change the month/day names or
    zero-pad the day, either of which makes the signature fail.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    return (
        f"{days[now.weekday()]} {months[now.month - 1]} {now.day} "
        f"{now:%H:%M:%S} UTC {now.year}"
    )


def _process_challenge(
    password: str, params: dict, a: int, big_a: int, timestamp: str
) -> dict:
    """Compute the PASSWORD_VERIFIER response from the SRP challenge."""
    user_id = params["USER_ID_FOR_SRP"]
    salt_hex = params["SALT"]
    srp_b_hex = params["SRP_B"]
    secret_block_b64 = params["SECRET_BLOCK"]

    big_b = int(srp_b_hex, 16)
    if big_b % _N == 0:
        raise CloudAuthError("Cognito returned an invalid SRP_B (≡ 0 mod N)")

    u = int(_hex_hash(_pad_hex(big_a) + _pad_hex(big_b)), 16)
    if u == 0:
        raise CloudAuthError("SRP hash of A|B was zero; cannot proceed")

    id_hash = _hash_hex(f"{POOL_NAME}{user_id}:{password}".encode())
    x = int(_hex_hash(_pad_hex(salt_hex) + id_hash), 16)

    # S = (B - k * g^x) ^ (a + u*x) mod N
    g_x = pow(_G, x, _N)
    base = (big_b - _k() * g_x) % _N
    s = pow(base, a + u * x, _N)

    hkdf = _compute_hkdf(
        bytes.fromhex(_pad_hex(s)),
        bytes.fromhex(_pad_hex(u)),
    )

    secret_block = base64.b64decode(secret_block_b64)
    msg = (
        POOL_NAME.encode()
        + user_id.encode()
        + secret_block
        + timestamp.encode()
    )
    signature = base64.b64encode(
        hmac.new(hkdf, msg, hashlib.sha256).digest()
    ).decode()

    return {
        "USERNAME": user_id,
        "PASSWORD_CLAIM_SECRET_BLOCK": secret_block_b64,
        "PASSWORD_CLAIM_SIGNATURE": signature,
        "TIMESTAMP": timestamp,
    }


# --- HTTP ---------------------------------------------------------------------


def _cognito_call(target: str, body: dict) -> dict:
    resp = requests.post(
        f"https://{COGNITO_HOST}/",
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
        },
        json=body,
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code != 200:
        # Cognito reports failures as JSON with a __type; surface it plainly.
        detail = resp.text[:300]
        try:
            err = resp.json()
            detail = f"{err.get('__type', '?')}: {err.get('message', detail)}"
        except ValueError:
            pass
        raise CloudAuthError(f"{target} failed ({resp.status_code}): {detail}")
    return resp.json()


def _tokens_from_result(result: dict, refresh_token: str | None = None) -> CloudTokens:
    try:
        expires_in = int(result.get("ExpiresIn", 3600))
        return CloudTokens(
            access_token=result["AccessToken"],
            id_token=result["IdToken"],
            # Refresh responses omit RefreshToken; keep the one we already have.
            refresh_token=result.get("RefreshToken") or refresh_token,
            expires_at=time.time() + expires_in,
            token_type=result.get("TokenType", "Bearer"),
        )
    except KeyError as exc:
        raise CloudAuthError(f"auth result missing field {exc}") from exc


# --- public API ---------------------------------------------------------------


def authenticate(username: str, password: str) -> CloudTokens:
    """Log in via Cognito SRP and return the JWTs.

    The password is used only to compute the SRP proof and is never sent to
    Cognito or persisted here.
    """
    a, big_a = _generate_a()

    challenge = _cognito_call(
        "InitiateAuth",
        {
            "AuthFlow": "USER_SRP_AUTH",
            "ClientId": CLIENT_ID,
            "AuthParameters": {
                "USERNAME": username,
                "SRP_A": format(big_a, "x"),
            },
        },
    )

    name = challenge.get("ChallengeName")
    if name != "PASSWORD_VERIFIER":
        raise CloudAuthError(
            f"expected PASSWORD_VERIFIER challenge, got {name!r} — this account "
            "may require an unsupported step (MFA, new-password, device SRP)"
        )

    timestamp = _cognito_timestamp()
    responses = _process_challenge(
        password, challenge["ChallengeParameters"], a, big_a, timestamp
    )

    result = _cognito_call(
        "RespondToAuthChallenge",
        {
            "ChallengeName": "PASSWORD_VERIFIER",
            "ClientId": CLIENT_ID,
            "ChallengeResponses": responses,
        },
    )

    auth = result.get("AuthenticationResult")
    if not auth:
        # A follow-up challenge (e.g. MFA) rather than tokens.
        raise CloudAuthError(
            f"login did not complete; Cognito returned challenge "
            f"{result.get('ChallengeName')!r}"
        )
    log.info("Cognito login succeeded for %s", username)
    return _tokens_from_result(auth)


def refresh(refresh_token: str) -> CloudTokens:
    """Exchange a refresh token for a fresh access/id token."""
    result = _cognito_call(
        "InitiateAuth",
        {
            "AuthFlow": "REFRESH_TOKEN_AUTH",
            "ClientId": CLIENT_ID,
            "AuthParameters": {"REFRESH_TOKEN": refresh_token},
        },
    )
    auth = result.get("AuthenticationResult")
    if not auth:
        raise CloudAuthError("refresh did not return an AuthenticationResult")
    log.info("refreshed Cognito access token")
    return _tokens_from_result(auth, refresh_token=refresh_token)


# --- token store --------------------------------------------------------------
#
# Only the refresh token needs to persist between runs; the access token is
# short-lived and re-derived from it. We still cache the access token so a quick
# restart need not round-trip Cognito. The store is written 0600 — a refresh
# token is a long-lived credential, but note it is not the password (which is
# never stored, per the module contract).


def save_tokens(path: Path, tokens: CloudTokens) -> None:
    """Persist tokens to `path` as 0600 JSON."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": tokens.access_token,
        "id_token": tokens.id_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at,
        "token_type": tokens.token_type,
    }
    # Write via a private temp file so a reader never sees a half-written store.
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def load_tokens(path: Path) -> CloudTokens | None:
    """Load tokens from `path`, or None if the store does not exist."""
    path = Path(path).expanduser()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return CloudTokens(
            access_token=data["access_token"],
            id_token=data.get("id_token", ""),
            refresh_token=data.get("refresh_token"),
            expires_at=float(data["expires_at"]),
            token_type=data.get("token_type", "Bearer"),
        )
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise CloudAuthError(f"cloud token store at {path} is corrupt: {exc}") from exc


def access_token_from_store(path: Path) -> str:
    """Return a currently-valid access token, refreshing and re-saving if needed.

    Raises `CloudAuthError` if there is no store yet (run `cloud-login` first) or
    if it holds no refresh token to renew with.
    """
    tokens = load_tokens(path)
    if tokens is None:
        raise CloudAuthError(
            f"no cloud credentials at {path}; run 'span-bridge cloud-login' first"
        )
    if not tokens.expired:
        return tokens.access_token
    if not tokens.refresh_token:
        raise CloudAuthError("cached access token expired and no refresh token is stored")
    tokens = refresh(tokens.refresh_token)
    save_tokens(path, tokens)
    return tokens.access_token


def user_id_from_token(access_token: str) -> str | None:
    """SPAN's user id: the access token's `username` claim.

    This is a dash-stripped UUID and is *not* Cognito's `sub`. It is the same
    `<userId>` that appears in the Ably channel name `c:<userId>:<deviceUUID>`,
    and SPAN treats it as a resource id in its own right — trait commands name it
    as the requester (`cloud_commands`).

    The signature is not verified: we are reading back a claim about ourselves
    from a token the server just gave us, and the server checks it again anyway.
    """
    try:
        body = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError) as exc:
        log.debug("could not read claims from the access token: %s", exc)
        return None
    username = claims.get("username")
    return username if isinstance(username, str) and username else None
