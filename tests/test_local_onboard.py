"""Tests for the local onboarding steps the config flow runs.

REST steps run against the fake HTTPS panel from test_local_rest; the broker
step runs against a fake MQTT client that replays retained topics.
"""

import asyncio
from typing import Any, ClassVar

import pytest
from span_client.local import onboard, rest

from tests.test_local_homie import PANEL, PANEL_DESC
from tests.test_local_rest import CA, PASSPHRASE, REGISTER_BODY, TOKEN, run

HOST = "127.0.0.1"


def port_of(make_api) -> int:
    return make_api()._port


# --- firmware and names -----------------------------------------------------


@pytest.mark.parametrize(
    ("firmware", "supported"),
    [
        ("spanos2/r202639/04", True),
        ("spanos2/r202701/01", True),
        ("spanos2/r202616/02", False),
        ("spanos3/2027.1", True),
        ("", True),
    ],
)
def test_firmware_supported(firmware, supported):
    assert onboard.firmware_supported(firmware) is supported


def test_client_names_are_fresh():
    names = {onboard.new_client_name() for _ in range(20)}
    assert len(names) == 20
    assert all(name.startswith(onboard.CLIENT_PREFIX) for name in names)


# --- REST steps -------------------------------------------------------------


def test_probe_pins_the_ca():
    async def scenario(panel, make_api):
        status, ca = await onboard.probe_panel(make_api()._session, HOST, port=port_of(make_api))
        return status, ca, [path for _, path, _ in panel.requests]

    status, ca, paths = run(scenario)
    assert ca == CA
    assert status.serial == "nt-0000-test"
    assert paths == ["/api/v2/certificate/ca", "/api/v2/status"]


def test_host_matches_ca():
    async def scenario(panel, make_api):
        session, port = make_api()._session, port_of(make_api)
        return (
            await onboard.host_matches_ca(session, HOST, CA, "nt-0000-test", port=port),
            await onboard.host_matches_ca(session, HOST, CA, "nt-9999-else", port=port),
        )

    assert run(scenario) == (True, False)


def test_host_with_another_ca_does_not_match():
    from tests.test_local_rest import OTHER_CA

    async def scenario(panel, make_api):
        return await onboard.host_matches_ca(
            make_api()._session, HOST, OTHER_CA, "nt-0000-test", port=port_of(make_api)
        )

    assert run(scenario) is False


def test_register_client():
    async def scenario(panel, make_api):
        registration = await onboard.register_client(
            make_api()._session, HOST, CA, "ha-span-ebus-test", PASSPHRASE, port=port_of(make_api)
        )
        return registration, panel.requests

    registration, requests = run(scenario)
    assert registration.access_token == TOKEN
    assert requests[0][2]["name"] == "ha-span-ebus-test"


def test_register_retries_a_dropped_connection_once(monkeypatch):
    calls: list[str] = []

    async def flaky(self, name, *, passphrase=None, description=None):
        calls.append(name)
        if len(calls) == 1:
            raise rest.CannotConnect("dropped")
        return rest.Registration.from_body(REGISTER_BODY)

    monkeypatch.setattr(rest.SpanLocalApi, "register", flaky)
    registration = asyncio.run(
        onboard.register_client(None, HOST, CA, "n", PASSPHRASE)  # type: ignore[arg-type]
    )
    assert registration.access_token == TOKEN
    assert calls == ["n", "n"]


def test_register_gives_up_after_the_retry(monkeypatch):
    async def down(self, name, *, passphrase=None, description=None):
        raise rest.CannotConnect("down")

    monkeypatch.setattr(rest.SpanLocalApi, "register", down)
    with pytest.raises(rest.CannotConnect):
        asyncio.run(onboard.register_client(None, HOST, CA, "n", PASSPHRASE))  # type: ignore[arg-type]


def test_wrong_passphrase_is_not_retried():
    async def scenario(panel, make_api):
        with pytest.raises(rest.RegistrationRejected):
            await onboard.register_client(
                make_api()._session, HOST, CA, "n", "wrong", port=port_of(make_api)
            )
        return len(panel.requests)

    assert run(scenario) == 1


def test_discard_client_uses_the_token():
    async def scenario(panel, make_api):
        await onboard.discard_client(
            make_api()._session, HOST, CA, TOKEN, "old-name", port=port_of(make_api)
        )
        return panel.requests, panel.auth_headers

    requests, headers = run(scenario)
    assert requests == [("DELETE", "/api/v2/auth/clients/old-name", None)]
    assert headers == [f"Bearer {TOKEN}"]


def test_discard_client_swallows_failures():
    async def scenario(panel, make_api):
        session, port = make_api()._session, port_of(make_api)
        await onboard.discard_client(session, HOST, CA, "stale", "x", port=port)
        await onboard.discard_client(session, HOST, CA, TOKEN, "..", port=port)

    run(scenario)  # neither raises


# --- broker step ------------------------------------------------------------


class FakeBroker:
    """Replays retained topics into on_message when connected."""

    instances: ClassVar[list["FakeBroker"]] = []

    def __init__(self, *, messages, connect_error=None, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.messages = messages
        self.connect_error = connect_error
        self.stopped = False
        FakeBroker.instances.append(self)

    async def connect_once(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        for topic, payload in self.messages:
            self.kwargs["on_message"](topic, payload, True)

    async def stop(self) -> None:
        self.stopped = True


def factory(messages, connect_error=None):
    FakeBroker.instances.clear()

    def make(**kwargs):
        return FakeBroker(messages=messages, connect_error=connect_error, **kwargs)

    return make


REGISTRATION = rest.Registration.from_body(REGISTER_BODY)


def wait(messages, connect_error=None, timeout=0.05):
    return asyncio.run(
        onboard.wait_until_ready(
            REGISTRATION,
            HOST,
            CA,
            "client-id",
            timeout=timeout,
            mqtt_factory=factory(messages, connect_error),
        )
    )


def test_ready_panel():
    root = wait(
        [
            (f"ebus/5/{PANEL}/$description", PANEL_DESC),
            (f"ebus/5/{PANEL}/$state", b"ready"),
        ]
    )
    assert root == PANEL
    broker = FakeBroker.instances[0]
    assert broker.stopped
    assert broker.kwargs["username"] == "client-user"
    assert broker.kwargs["password"] == "broker-secret"
    assert broker.kwargs["port"] == 8883
    assert broker.kwargs["client_id"] == "client-id"


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [(f"ebus/5/{PANEL}/$state", b"ready")],  # never described
        [(f"ebus/5/{PANEL}/$description", PANEL_DESC), (f"ebus/5/{PANEL}/$state", b"init")],
    ],
)
def test_not_ready_in_time(messages):
    with pytest.raises(onboard.NotReadyInTime):
        wait(messages)
    assert FakeBroker.instances[0].stopped


@pytest.mark.parametrize("error", [rest.AuthFailed("no"), rest.CannotConnect("no")])
def test_broker_login_failure_still_stops(error):
    with pytest.raises(type(error)):
        wait([], connect_error=error)
    assert FakeBroker.instances[0].stopped


# --- form errors ------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "key", "placeholders"),
    [
        (rest.RateLimited(42), "rate_limited", {"retry_after": "42"}),
        (rest.RateLimited(None), "rate_limited", {"retry_after": "60"}),
        (rest.RegistrationRejected("no"), "invalid_passphrase", {}),
        (rest.PanelNotReady("no"), "panel_not_ready", {}),
        (rest.AuthFailed("no"), "broker_auth", {}),
        (rest.CannotConnect("no"), "panel_unreachable", {}),
        (onboard.NotReadyInTime("no"), "no_ready_state", {}),
        (rest.UnexpectedResponse("no"), "unknown", {}),
    ],
)
def test_form_error(exc, key, placeholders):
    assert onboard.form_error(exc) == (key, placeholders)
