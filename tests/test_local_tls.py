"""Tests for the pinned-CA TLS helpers.

The fixtures are throwaway certificates made for these tests. Like a real
panel's, the CA has no Authority Key Identifier and the server certificate
names only `span-test.local`, so a connection to 127.0.0.1 passes only with
hostname checking off and strict mode cleared.
"""

import asyncio
import ssl
from pathlib import Path

import pytest
from span_client.local import tls

FIXTURES = Path(__file__).parent / "fixtures" / "tls"
CA = (FIXTURES / "ca.pem").read_text()
OTHER_CA = (FIXTURES / "other-ca.pem").read_text()
CA_FINGERPRINT = (
    "E6:CD:1F:17:39:3A:DD:32:66:A1:E7:24:E5:05:05:48:"
    "C5:B4:A4:02:32:7F:7C:22:46:5F:52:50:23:63:A5:9C"
)


def server_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(FIXTURES / "server.pem", FIXTURES / "server.key")
    return context


async def handshake(client_context: ssl.SSLContext) -> None:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=server_context())
    port = server.sockets[0].getsockname()[1]
    try:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_context)
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


def test_fingerprint_matches_openssl():
    assert tls.ca_fingerprint(CA) == CA_FINGERPRINT


def test_fingerprint_ignores_surrounding_text():
    assert tls.ca_fingerprint(f"junk before\n{CA}\ntrailing") == CA_FINGERPRINT


def test_first_certificate_takes_the_first_block():
    pem = tls.first_certificate(CA + OTHER_CA)
    assert pem.count("BEGIN CERTIFICATE") == 1
    assert tls.ca_fingerprint(pem) == CA_FINGERPRINT


@pytest.mark.parametrize(
    "text", ["", "<html>not found</html>", "-----BEGIN CERTIFICATE-----\nAAAA"]
)
def test_first_certificate_rejects_non_certificates(text):
    with pytest.raises(ValueError):
        tls.first_certificate(text)


def test_pinned_context_settings():
    context = tls.pinned_context(CA)
    assert context.check_hostname is False
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert not context.verify_flags & ssl.VERIFY_X509_STRICT
    assert context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    # Only the pinned CA; no system roots.
    assert len(context.get_ca_certs()) == 1


def test_pinned_context_accepts_the_panel_by_ip():
    asyncio.run(handshake(tls.pinned_context(CA)))


def test_pinned_context_rejects_another_ca():
    with pytest.raises(ssl.SSLCertVerificationError):
        asyncio.run(handshake(tls.pinned_context(OTHER_CA)))


def test_unverified_context_accepts_anything():
    context = tls.unverified_context()
    assert context.verify_mode is ssl.CERT_NONE
    asyncio.run(handshake(context))
