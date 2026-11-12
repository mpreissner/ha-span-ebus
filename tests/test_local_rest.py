"""Tests for the v2 REST client, against a fake panel on a local HTTPS server.

The fake serves the throwaway certificates in fixtures/tls, so every request
other than the CA fetch goes through the real pinned-CA verification.
"""

import asyncio
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from span_client.local import rest

FIXTURES = Path(__file__).parent / "fixtures" / "tls"
CA = (FIXTURES / "ca.pem").read_text()
OTHER_CA = (FIXTURES / "other-ca.pem").read_text()

TOKEN = "token-123"
PASSPHRASE = "correct horse"
REGISTER_BODY = {
    "accessToken": TOKEN,
    "tokenType": "Bearer",
    "iatMs": 1,
    "hostname": "span-test.local",
    "serialNumber": "nt-0000-test",
    "hopPassphrase": PASSPHRASE,
    "ebusBrokerUsername": "client-user",
    "ebusBrokerPassword": "broker-secret",
    "ebusBrokerHost": "span-test.local",
    "ebusBrokerMqttsPort": 8883,
    "ebusBrokerWsPort": 80,
    "ebusBrokerWssPort": 443,
}
SCHEMA_BODY = {
    "firmwareVersion": "spanos2/r202639/04",
    "homieDomain": "ebus",
    "homieVersion": 5,
    "dataModelVersion": "1.0",
    "deviceClasses": {"circuit": {"switch": {"relay": {"datatype": "enum"}}}},
    "deviceClassesSchemaHash": "sha256:0123456789abcdef",
}


class FakePanel:
    def __init__(self) -> None:
        self.register_status = 200
        self.rotate_status = 200
        self.rate_limited = False
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.auth_headers: list[str | None] = []
        self.app = web.Application(middlewares=[self.record])
        r = self.app.router
        r.add_get("/api/v2/certificate/ca", self.ca)
        r.add_get("/api/v2/status", self.status)
        r.add_get("/api/v2/homie/schema", self.schema)
        r.add_post("/api/v2/auth/register", self.register)
        r.add_get("/api/v2/auth/clients", self.clients)
        r.add_delete("/api/v2/auth/clients/{name}", self.delete_client)
        r.add_put("/api/v2/auth/passphrase", self.rotate)
        r.add_get("/api/v2/dns/fqdn", self.fqdn)

    @web.middleware
    async def record(self, request: web.Request, handler: Callable[..., Awaitable[Any]]) -> Any:
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.method, request.path, body))
        self.auth_headers.append(request.headers.get("Authorization"))
        if self.rate_limited:
            return web.json_response(
                {"detail": "slow down"}, status=429, headers={"Retry-After": "42"}
            )
        return await handler(request)

    @staticmethod
    def authorised(request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    async def ca(self, _request: web.Request) -> web.Response:
        return web.Response(text=CA, content_type="application/x-pem-file")

    async def status(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "serialNumber": "nt-0000-test",
                "firmwareVersion": "spanos2/r202639/04",
                "proximityProven": False,
                "hardwareVersion": "MAIN_40",
            }
        )

    async def schema(self, _request: web.Request) -> web.Response:
        return web.json_response(SCHEMA_BODY)

    async def register(self, request: web.Request) -> web.Response:
        if self.register_status != 200:
            return web.json_response({"detail": "nope"}, status=self.register_status)
        body = await request.json()
        if body.get("hopPassphrase") != PASSPHRASE:
            return web.json_response({"detail": "credential not accepted"}, status=422)
        return web.json_response(REGISTER_BODY)

    async def clients(self, request: web.Request) -> web.Response:
        if not self.authorised(request):
            return web.json_response({"detail": "unauthorised"}, status=401)
        return web.json_response({"clients": {"ha": {"description": "x", "issued_at": 1}}})

    async def delete_client(self, request: web.Request) -> web.Response:
        return web.Response(status=204) if self.authorised(request) else web.Response(status=403)

    async def rotate(self, _request: web.Request) -> web.Response:
        if self.rotate_status != 200:
            return web.Response(status=self.rotate_status)
        return web.json_response({"ebusBrokerPassword": "new-pw", "hopPassphrase": "new-phrase"})

    async def fqdn(self, _request: web.Request) -> web.Response:
        return web.json_response({"ebusTlsFqdn": ""})


def run(scenario: Callable[[FakePanel, Callable[..., rest.SpanLocalApi]], Awaitable[Any]]) -> Any:
    """Start a fake panel over TLS and run `scenario(panel, make_api)`."""

    async def main() -> Any:
        panel = FakePanel()
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(FIXTURES / "server.pem", FIXTURES / "server.key")
        runner = web.AppRunner(panel.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            async with aiohttp.ClientSession() as session:

                def make_api(**kwargs: Any) -> rest.SpanLocalApi:
                    return rest.SpanLocalApi(session, "127.0.0.1", port=port, **kwargs)

                return await scenario(panel, make_api)
        finally:
            await runner.cleanup()

    return asyncio.run(main())


def test_fetch_ca_then_pinned_calls():
    async def scenario(panel, make_api):
        ca = await make_api().fetch_ca()
        api = make_api(ca_pem=ca)
        return ca, await api.status(), await api.homie_schema()

    ca, status, schema = run(scenario)
    assert "BEGIN CERTIFICATE" in ca
    assert status == rest.PanelStatus("nt-0000-test", "spanos2/r202639/04", "MAIN_40", False)
    assert schema.schema_hash == "sha256:0123456789abcdef"
    assert schema.homie_version == 5
    assert "circuit" in schema.device_classes


def test_ca_fetch_never_carries_the_token():
    async def scenario(panel, make_api):
        await make_api(access_token=TOKEN).fetch_ca()
        return panel.auth_headers

    assert run(scenario) == [None]


def test_delete_client_rejects_a_slash():
    async def scenario(panel, make_api):
        with pytest.raises(ValueError):
            await make_api(ca_pem=CA, access_token=TOKEN).delete_client("../passphrase")
        return panel.requests

    assert run(scenario) == []


def test_nothing_but_the_ca_goes_out_unpinned():
    async def scenario(panel, make_api):
        with pytest.raises(rest.SpanLocalError):
            await make_api().status()
        return panel.requests

    assert run(scenario) == []


def test_wrong_ca_is_cannot_connect():
    async def scenario(panel, make_api):
        await make_api(ca_pem=OTHER_CA).status()

    with pytest.raises(rest.CannotConnect):
        run(scenario)


def test_register_sends_passphrase_and_keeps_token():
    async def scenario(panel, make_api):
        api = make_api(ca_pem=CA)
        registration = await api.register("home-assistant", passphrase=PASSPHRASE)
        clients = await api.list_clients()
        await api.delete_client("old")
        return panel.requests, registration, clients

    requests, registration, clients = run(scenario)
    assert requests[0] == (
        "POST",
        "/api/v2/auth/register",
        {"name": "home-assistant", "hopPassphrase": PASSPHRASE},
    )
    assert registration.access_token == TOKEN
    assert registration.broker_password == "broker-secret"
    assert registration.mqtts_port == 8883
    assert "ha" in clients
    assert requests[-1][:2] == ("DELETE", "/api/v2/auth/clients/old")


def test_registration_repr_hides_secrets():
    text = repr(rest.Registration.from_body(REGISTER_BODY))
    for secret in (TOKEN, PASSPHRASE, "broker-secret", "nt-0000-test"):
        assert secret not in text


def test_register_without_passphrase_omits_the_field():
    async def scenario(panel, make_api):
        with pytest.raises(rest.RegistrationRejected):
            await make_api(ca_pem=CA).register("ha")
        return panel.requests[0][2]

    assert run(scenario) == {"name": "ha"}


def test_wrong_passphrase_is_rejected():
    async def scenario(panel, make_api):
        await make_api(ca_pem=CA).register("ha", passphrase="wrong")

    with pytest.raises(rest.RegistrationRejected, match="credential not accepted"):
        run(scenario)


def test_register_not_ready():
    async def scenario(panel, make_api):
        panel.register_status = 503
        await make_api(ca_pem=CA).register("ha", passphrase=PASSPHRASE)

    with pytest.raises(rest.PanelNotReady):
        run(scenario)


def test_rate_limited_carries_retry_after():
    async def scenario(panel, make_api):
        panel.rate_limited = True
        await make_api().fetch_ca()

    with pytest.raises(rest.RateLimited) as caught:
        run(scenario)
    assert caught.value.retry_after == 42


def test_bad_token_is_auth_failed():
    async def scenario(panel, make_api):
        await make_api(ca_pem=CA, access_token="stale").list_clients()

    with pytest.raises(rest.AuthFailed):
        run(scenario)


def test_authenticated_call_needs_a_token():
    async def scenario(panel, make_api):
        with pytest.raises(rest.AuthFailed):
            await make_api(ca_pem=CA).list_clients()
        return panel.requests

    assert run(scenario) == []


def test_rotate_passphrase():
    async def scenario(panel, make_api):
        return await make_api(ca_pem=CA, access_token=TOKEN).rotate_passphrase()

    rotated = run(scenario)
    assert rotated.hop_passphrase == "new-phrase"
    assert "new-phrase" not in repr(rotated)


def test_rotate_gateway_timeout_is_outcome_unknown():
    async def scenario(panel, make_api):
        panel.rotate_status = 504
        await make_api(ca_pem=CA, access_token=TOKEN).rotate_passphrase()

    with pytest.raises(rest.OutcomeUnknown):
        run(scenario)


def test_unset_fqdn_is_none():
    async def scenario(panel, make_api):
        return await make_api(ca_pem=CA, access_token=TOKEN).get_fqdn()

    assert run(scenario) is None


def test_ipv6_host_is_bracketed():
    api = rest.SpanLocalApi(None, "fe80::1")  # type: ignore[arg-type]
    assert str(api._url("/status")) == "https://[fe80::1]/api/v2/status"
