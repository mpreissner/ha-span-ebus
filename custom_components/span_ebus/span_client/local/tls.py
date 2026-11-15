"""TLS for SPAN's per-panel certificate authority.

Every panel mints its own CA at commissioning and signs its HTTPS and MQTTS
certificates with it, so there is no shared root to trust: each panel is its
own trust anchor. The CA is fetched once, when the panel is added, then pinned
in the config entry as PEM text. Everything here works on that text and never
touches the filesystem, so it is safe to call from the event loop.
"""

from __future__ import annotations

import hashlib
import ssl


def pinned_context(ca_pem: str) -> ssl.SSLContext:
    """A client context that trusts exactly one panel's CA.

    It differs from a default context in two ways, both needed against real
    firmware:

    - `check_hostname` is off. The certificate names `span-<serial>.local` and
      the panel's hosted-network addresses, so a connection by LAN IP never
      matches. The chain is still verified against the pinned CA.
    - `VERIFY_X509_STRICT` is cleared. The panel CA omits the Authority Key
      Identifier that OpenSSL 3 strict mode requires. `PARTIAL_CHAIN` then lets
      the panel CA stand as a trust anchor without a public root above it.

    Neither weakens what matters: the peer must present a certificate signed
    by this panel's CA, so another device on the LAN cannot pose as the panel.

    No system trust store is loaded, which keeps this cheap enough for the
    event loop and means a public CA can never vouch for a panel.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_verify_locations(cadata=ca_pem)
    return context


def unverified_context() -> ssl.SSLContext:
    """A context for fetching the CA itself, before anything can be verified.

    It is used for exactly one request, `GET /api/v2/certificate/ca`. Its
    answer is trusted on first use: the user is shown the fingerprint, and
    every later connection is verified against it.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def ca_fingerprint(ca_pem: str) -> str:
    """SHA-256 of the CA's DER encoding, as colon-separated upper-case hex.

    This is the same form browsers and `openssl x509 -fingerprint -sha256`
    show, so a user can compare it against another tool.
    """
    der = ssl.PEM_cert_to_DER_cert(first_certificate(ca_pem))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def first_certificate(pem: str) -> str:
    """The first PEM certificate block in `pem`.

    Raises ValueError when there is none, which is how a non-certificate
    answer (an HTML error page, an empty body) is told apart from a CA.
    """
    begin = "-----BEGIN CERTIFICATE-----"
    end = "-----END CERTIFICATE-----"
    start = pem.find(begin)
    stop = pem.find(end, start + len(begin)) if start >= 0 else -1
    if start < 0 or stop < 0:
        raise ValueError("no PEM certificate found")
    return pem[start : stop + len(end)] + "\n"
