"""TLS handling for SPAN's per-device certificate authority.

Every panel mints its own CA at commissioning time and signs its broker
certificate with it, so there is no shared root to trust — each panel is its
own trust anchor. Both `auth` (recovering the CA) and `backends.ebus`
(connecting with it) need the same non-default handshake settings, so they
live here rather than being duplicated.
"""

from __future__ import annotations

import socket
import ssl
from pathlib import Path

CONNECT_TIMEOUT = 15


def build_ssl_context(ca_cert_path: Path | str) -> ssl.SSLContext:
    """A context that trusts one panel's CA and tolerates its cert quirks.

    Two deviations from `create_default_context()`, both required against real
    firmware (verified on spanos3/r202627/06):

    - `check_hostname = False`. The broker cert's CN is `span-<SERIAL>.local`,
      which never matches when connecting by IP. The chain is still verified.
    - `VERIFY_X509_STRICT` cleared. SPAN's per-device CA omits the Authority
      Key Identifier extension that OpenSSL 3 strict mode requires; leaving it
      on fails the handshake with "Missing Authority Key Identifier".
      `PARTIAL_CHAIN` then lets the panel CA act as a trust anchor in its own
      right, instead of OpenSSL demanding a path up to a public root.

    Neither weakens the guarantee that matters: the broker must still present a
    certificate carrying a valid signature from this specific panel's CA, so a
    stranger on the LAN cannot impersonate it.
    """
    context = ssl.create_default_context(cafile=str(ca_cert_path))
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return context


def peer_chain_pem(host: str, port: int, timeout: int = CONNECT_TIMEOUT) -> list[str]:
    """Return the certificate chain a TLS server presents, as PEM strings.

    Verification is disabled for this handshake, because obtaining the CA is
    the very thing that would make verification possible. Nothing here is
    trusted on the strength of this call alone — see `verifies_peer`.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return [ssl.DER_cert_to_PEM_cert(der) for der in tls.get_unverified_chain() or []]


def verifies_peer(ca_cert_path: Path | str, host: str, port: int,
                  timeout: int = CONNECT_TIMEOUT) -> bool:
    """Whether a full, verified handshake succeeds using this CA.

    This is the real test of a candidate CA: matching a subject string only
    shows the certificate claims the right name, whereas completing a verified
    handshake proves it actually signed what the server presented.
    """
    try:
        context = build_ssl_context(ca_cert_path)
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host):
                return True
    except (OSError, ssl.SSLError):
        return False
