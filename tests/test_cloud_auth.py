"""Tests for the Cognito SRP math.

These never touch the network. The core check derives the SRP shared secret
from both the client and a synthetic server and asserts they match — the
standard way to prove an SRP client's `x`/`u`/`S` computation is correct
without a real server.
"""

import base64
import json
import time

import pytest
from span_client import cloud_auth as ca


def test_pad_hex_prefixes_when_high_bit_set():
    # 0x80 has the top bit set → must be padded to stay unsigned.
    assert ca._pad_hex(0x80) == "0080"
    # 0x7f does not.
    assert ca._pad_hex(0x7F) == "7f"
    # Odd-length hex is left-padded to an even length.
    assert ca._pad_hex(0x123) == "0123"


def test_cognito_timestamp_is_c_locale_unpadded_day():
    import datetime

    ts = ca._cognito_timestamp(datetime.datetime(2026, 8, 6, 9, 4, 5, tzinfo=datetime.UTC))
    # Day-of-month must NOT be zero-padded; names are English regardless of locale.
    assert ts == "Thu Aug 6 09:04:05 UTC 2026"


def test_srp_client_and_server_agree_on_shared_secret():
    """Full SRP round-trip against a locally-computed verifier."""
    N, g = ca._N, ca._G
    pool = ca.POOL_NAME
    user_id = "user-id-for-srp"
    password = "correct horse battery staple"
    salt_hex = ca._pad_hex(0xBEEF1234ABCD)

    # Server stores the verifier v = g^x mod N, where x derives from salt+creds.
    id_hash = ca._hash_hex(f"{pool}{user_id}:{password}".encode())
    x = int(ca._hex_hash(ca._pad_hex(salt_hex) + id_hash), 16)
    v = pow(g, x, N)

    # Client picks a, A.
    a, big_a = ca._generate_a()

    # Server picks b, B = k*v + g^b.
    k = ca._k()
    b = 0x1234567890ABCDEF
    big_b = (k * v + pow(g, b, N)) % N

    u = int(ca._hex_hash(ca._pad_hex(big_a) + ca._pad_hex(big_b)), 16)

    # Client-side S (mirrors _process_challenge).
    client_s = pow((big_b - k * pow(g, x, N)) % N, a + u * x, N)
    # Server-side S = (A * v^u) ^ b.
    server_s = pow((big_a * pow(v, u, N)) % N, b, N)

    assert client_s == server_s


def test_process_challenge_produces_valid_signature():
    """The signature must verify against the key the server would derive."""
    import hashlib
    import hmac

    N, g = ca._N, ca._G
    pool = ca.POOL_NAME
    user_id = "abc-123"
    password = "hunter2"
    salt_hex = ca._pad_hex(0xA1B2C3D4)
    secret_block = b"opaque-server-secret-block"
    secret_block_b64 = base64.b64encode(secret_block).decode()

    id_hash = ca._hash_hex(f"{pool}{user_id}:{password}".encode())
    x = int(ca._hex_hash(ca._pad_hex(salt_hex) + id_hash), 16)
    v = pow(g, x, N)

    a, big_a = ca._generate_a()
    k = ca._k()
    b = 0xDEADBEEFCAFE
    big_b = (k * v + pow(g, b, N)) % N

    params = {
        "USER_ID_FOR_SRP": user_id,
        "SALT": salt_hex,
        "SRP_B": format(big_b, "x"),
        "SECRET_BLOCK": secret_block_b64,
    }
    timestamp = "Thu Aug 6 09:04:05 UTC 2026"
    resp = ca._process_challenge(password, params, a, big_a, timestamp)

    # Recompute the key server-side and verify the client's signature.
    u = int(ca._hex_hash(ca._pad_hex(big_a) + ca._pad_hex(big_b)), 16)
    server_s = pow((big_a * pow(v, u, N)) % N, b, N)
    server_key = ca._compute_hkdf(
        bytes.fromhex(ca._pad_hex(server_s)),
        bytes.fromhex(ca._pad_hex(u)),
    )
    expected_msg = pool.encode() + user_id.encode() + secret_block + timestamp.encode()
    expected_sig = base64.b64encode(
        hmac.new(server_key, expected_msg, hashlib.sha256).digest()
    ).decode()

    assert resp["PASSWORD_CLAIM_SIGNATURE"] == expected_sig
    assert resp["USERNAME"] == user_id
    assert resp["PASSWORD_CLAIM_SECRET_BLOCK"] == secret_block_b64


def _access_token(claims: dict | None) -> str:
    if claims is None:
        return "not.a.jwt"
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


def test_user_id_comes_from_the_username_claim_not_the_subject():
    # SPAN's user id is `username`, a dash-stripped UUID. Cognito's `sub` is a
    # different UUID entirely, and sending it as a command's requester is
    # rejected — so reading the wrong claim would be a silent, live-only failure.
    token = _access_token(
        {
            "sub": "f8015330-8011-7055-244b-9b7fd7f18135",
            "username": "00112233445566778899aabbccddeeff",
        }
    )
    assert ca.user_id_from_token(token) == "00112233445566778899aabbccddeeff"


def test_a_token_without_a_usable_username_yields_none():
    assert ca.user_id_from_token(_access_token({"sub": "s"})) is None
    assert ca.user_id_from_token(_access_token({"username": ""})) is None
    assert ca.user_id_from_token(_access_token({"username": 7})) is None
    # Malformed tokens must not raise on a path that runs during setup.
    assert ca.user_id_from_token(_access_token(None)) is None
    assert ca.user_id_from_token("") is None


def _response(status: int, payload: dict):
    class Fake:
        status_code = status
        text = json.dumps(payload)

        def json(self):
            return payload

    return Fake()


def test_a_revoked_credential_is_distinguished_from_a_sulking_cognito(monkeypatch):
    # The whole point of the split: one of these needs the user, the other needs
    # patience, and treating them alike means either a login prompt nobody can
    # satisfy or an integration that silently never works again.
    monkeypatch.setattr(
        ca.requests,
        "post",
        lambda *a, **kw: _response(
            400, {"__type": "NotAuthorizedException", "message": "Refresh Token has expired"}
        ),
    )
    with pytest.raises(ca.CloudCredentialsRejected):
        ca._cognito_call("InitiateAuth", {})

    monkeypatch.setattr(
        ca.requests,
        "post",
        lambda *a, **kw: _response(500, {"__type": "InternalErrorException", "message": "oops"}),
    )
    with pytest.raises(ca.CloudAuthError) as caught:
        ca._cognito_call("InitiateAuth", {})
    assert not isinstance(caught.value, ca.CloudCredentialsRejected)


def test_a_service_qualified_error_type_still_matches():
    # Cognito spells it both ways depending on the operation.
    assert ca._error_type({"__type": "com.amazon.coral.service#NotAuthorizedException"}) in (
        ca.TERMINAL_COGNITO_ERRORS
    )
    assert ca._error_type({"__type": "NotAuthorizedException"}) in ca.TERMINAL_COGNITO_ERRORS
    assert ca._error_type({}) is None


def test_a_store_that_cannot_produce_a_token_asks_for_the_user(tmp_path):
    # No store at all, and a store whose refresh token is gone: both are dead
    # ends that only a fresh sign-in clears.
    with pytest.raises(ca.CloudCredentialsRejected):
        ca.access_token_from_store(tmp_path / "absent.json")

    path = tmp_path / "tokens.json"
    ca.save_tokens(
        path,
        ca.CloudTokens(
            access_token="at", id_token="it", refresh_token=None, expires_at=time.time() - 10
        ),
    )
    with pytest.raises(ca.CloudCredentialsRejected):
        ca.access_token_from_store(path)
