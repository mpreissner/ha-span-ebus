"""Tests for rotating a local panel's credentials, free of Home Assistant.

The flow runs with fakes for every side effect; the PUT's error mapping runs
against the real REST client and a fake panel over TLS.
"""

import asyncio
import contextlib
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from span_client.local import rest, rotation
from span_client.local.rest import RotatedPassphrase
from span_client.local.rotation import Candidate, Problem, RotationError

from tests.test_local_rest import CA, FIXTURES, TOKEN

ENTRY = "entry-0001"
OTHER = "entry-0002"
CLOUD = "entry-cloud"

SENTINEL = "SENTINEL-hop-7f3a91"
BROKER_SENTINEL = "SENTINEL-broker-2c6d08"
NEW_PASSWORD = "new-broker-pw"


# --- fakes -----------------------------------------------------------------------------


class FakeApi:
    def __init__(
        self,
        *,
        broker: str = NEW_PASSWORD,
        hop: str = SENTINEL,
        error: Exception | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.broker = broker
        self.hop = hop
        self.error = error
        self.calls = calls if calls is not None else []

    async def rotate_passphrase(self) -> RotatedPassphrase:
        self.calls.append("put")
        if self.error is not None:
            raise self.error
        return RotatedPassphrase(broker_password=self.broker, hop_passphrase=self.hop)


class Effects:
    """Records store, probe, reload and sleep; each can be told to fail."""

    def __init__(
        self,
        *,
        refusals: int = 0,
        store_error: Exception | None = None,
        reload_error: Exception | None = None,
        reload_result: bool = True,
        probe_error: type[Exception] = rest.AuthFailed,
    ) -> None:
        self.stored: list[Any] = []
        self.probed: list[str] = []
        self.reloads = 0
        self.slept: list[float] = []
        self.refusals = refusals
        self.store_error = store_error
        self.reload_error = reload_error
        self.reload_result = reload_result
        self.probe_error = probe_error

    async def store(self, password: str) -> None:
        if self.store_error is not None:
            raise self.store_error
        self.stored.append(password)

    async def probe(self, password: str) -> None:
        self.probed.append(password)
        if self.refusals:
            self.refusals -= 1
            raise self.probe_error("refused")

    async def reload(self) -> bool:
        self.reloads += 1
        if self.reload_error is not None:
            raise self.reload_error
        return self.reload_result

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def kwargs(self) -> dict[str, Any]:
        return {
            "entry_id": ENTRY,
            "has_token": True,
            "has_ca": True,
            "store": self.store,
            "probe": self.probe,
            "reload": self.reload,
            "sleep": self.sleep,
        }


def rotate(api: Any, effects: Effects, **overrides: Any) -> rotation.RotationResult:
    return asyncio.run(rotation.rotate(api, **{**effects.kwargs(), **overrides}))


def rotate_error(api: Any, effects: Effects, **overrides: Any) -> RotationError:
    with pytest.raises(RotationError) as caught:
        rotate(api, effects, **overrides)
    return caught.value


# --- targeting -------------------------------------------------------------------------

LOCAL = Candidate(local=True, loaded=True)
UNLOADED = Candidate(local=True, loaded=False)
CLOUD_ONLY = Candidate(local=False, loaded=True)


@pytest.mark.parametrize(
    ("entries", "entry_id", "device", "expected"),
    [
        # Neither id: exactly one local panel, none, or several.
        ({ENTRY: LOCAL, CLOUD: CLOUD_ONLY}, None, None, ENTRY),
        ({CLOUD: CLOUD_ONLY}, None, None, Problem.NO_LOCAL_PANEL),
        ({}, None, None, Problem.NO_LOCAL_PANEL),
        ({ENTRY: LOCAL, OTHER: LOCAL}, None, None, Problem.MULTIPLE_PANELS),
        ({ENTRY: UNLOADED}, None, None, Problem.NOT_LOADED),
        # The entry id alone.
        ({ENTRY: LOCAL, OTHER: LOCAL}, OTHER, None, OTHER),
        ({ENTRY: LOCAL, CLOUD: CLOUD_ONLY}, CLOUD, None, Problem.NOT_LOCAL),
        ({ENTRY: UNLOADED}, ENTRY, None, Problem.NOT_LOADED),
        ({ENTRY: LOCAL}, "entry-unknown", None, Problem.NO_LOCAL_PANEL),
        # The device alone.
        ({ENTRY: LOCAL, OTHER: LOCAL}, None, {OTHER}, OTHER),
        ({ENTRY: LOCAL, CLOUD: CLOUD_ONLY}, None, {CLOUD}, Problem.NOT_LOCAL),
        ({ENTRY: UNLOADED}, None, {ENTRY}, Problem.NOT_LOADED),
        ({ENTRY: LOCAL}, None, set(), Problem.NO_LOCAL_PANEL),  # unknown device
        ({ENTRY: LOCAL}, None, {"entry-gone"}, Problem.NO_LOCAL_PANEL),
        # A device in a local and a cloud entry (a linked panel) is the local one.
        ({ENTRY: LOCAL, CLOUD: CLOUD_ONLY}, None, {ENTRY, CLOUD}, ENTRY),
        # Both: agreeing, or not.
        ({ENTRY: LOCAL, OTHER: LOCAL}, ENTRY, {ENTRY}, ENTRY),
        ({ENTRY: LOCAL, OTHER: LOCAL}, ENTRY, {OTHER}, Problem.TARGET_MISMATCH),
        ({ENTRY: LOCAL}, ENTRY, set(), Problem.TARGET_MISMATCH),
    ],
)
def test_pick_target(entries, entry_id, device, expected):
    assert rotation.pick_target(entries, entry_id, device) == expected


def test_refusals_are_the_targeting_and_precondition_problems():
    refusals = {p for p in Problem if p.is_refusal}
    assert refusals == {
        Problem.NO_LOCAL_PANEL,
        Problem.MULTIPLE_PANELS,
        Problem.TARGET_MISMATCH,
        Problem.NOT_LOCAL,
        Problem.NOT_LOADED,
        Problem.NO_TOKEN,
        Problem.NO_CA,
    }


# --- admin only ------------------------------------------------------------------------


class User:
    def __init__(self, is_admin: bool) -> None:
        self.is_admin = is_admin


def admin(user_id: str | None, users: dict[str, User]) -> bool:
    async def get_user(uid: str) -> User | None:
        return users.get(uid)

    return asyncio.run(rotation.is_admin(user_id, get_user))


def test_admin_check():
    users = {"u-admin": User(True), "u-plain": User(False)}
    assert admin(None, users) is False  # automations, and calls without a user
    assert admin("u-plain", users) is False
    assert admin("u-missing", users) is False
    assert admin("u-admin", users) is True


# --- the flow --------------------------------------------------------------------------


def test_happy_path_stores_only_the_broker_password():
    effects = Effects()
    result = rotate(FakeApi(), effects)
    assert result.as_response() == {
        "hop_passphrase": SENTINEL,
        "reconnected": True,
        "stored": True,
    }
    assert effects.stored == [NEW_PASSWORD]
    assert effects.probed == [NEW_PASSWORD]
    assert effects.reloads == 1
    assert effects.slept == []


def test_preconditions_refuse_before_any_request():
    calls: list[str] = []
    assert rotate_error(FakeApi(calls=calls), Effects(), has_token=False).problem is (
        Problem.NO_TOKEN
    )
    assert rotate_error(FakeApi(calls=calls), Effects(), has_ca=False).problem is Problem.NO_CA
    assert calls == []


@pytest.mark.parametrize("error", [rest.AuthFailed, rest.CannotConnect])
def test_probe_retries_with_the_new_password_only(error):
    effects = Effects(refusals=3, probe_error=error)
    result = rotate(FakeApi(), effects)
    assert result.reconnected is True
    assert effects.slept == [1, 2, 4]
    assert effects.probed == [NEW_PASSWORD] * 4
    assert effects.reloads == 1


def test_probe_never_accepted_keeps_the_stored_password():
    effects = Effects(refusals=100)
    result = rotate(FakeApi(), effects)
    assert result.as_response() == {
        "hop_passphrase": SENTINEL,
        "reconnected": False,
        "stored": True,
    }
    assert effects.slept == list(rotation.RECONNECT_DELAYS)
    assert len(effects.probed) == len(rotation.RECONNECT_DELAYS) + 1
    assert effects.reloads == 0
    assert effects.stored == [NEW_PASSWORD]


def test_unexpected_probe_error_still_returns_the_passphrase():
    effects = Effects(refusals=1, probe_error=RuntimeError)
    result = rotate(FakeApi(), effects)
    assert (result.hop_passphrase, result.reconnected, result.stored) == (SENTINEL, False, True)
    assert effects.reloads == 0


def test_store_raises_returns_the_passphrase_unstored():
    effects = Effects(store_error=RuntimeError("entry gone"))
    result = rotate(FakeApi(), effects)
    assert result.as_response() == {
        "hop_passphrase": SENTINEL,
        "reconnected": False,
        "stored": False,
    }
    assert effects.probed == []
    assert effects.reloads == 0


@pytest.mark.parametrize(
    "effects",
    [Effects(reload_error=RuntimeError("busy")), Effects(reload_result=False)],
    ids=["raises", "not set up"],
)
def test_reload_failure_is_not_reconnected(effects):
    result = rotate(FakeApi(), effects)
    assert (result.reconnected, result.stored) == (False, True)
    assert effects.reloads == 1


@pytest.mark.parametrize(
    ("error", "problem"),
    [
        (rest.AuthFailed("PUT /auth/passphrase: 401"), Problem.AUTH_FAILED),
        (rest.RateLimited(42), Problem.RATE_LIMITED),
        (rest.OutcomeUnknown("lost"), Problem.OUTCOME_UNKNOWN),
        (rest.NotSent("refused"), Problem.CANNOT_CONNECT),
        (rest.CannotConnect("sent, then lost"), Problem.OUTCOME_UNKNOWN),
        (rest.UnexpectedResponse("HTTP 503"), Problem.FAILED),
        (rest.NotFound("HTTP 404"), Problem.FAILED),
        (rest.SpanLocalError("no pinned CA; call fetch_ca first"), Problem.NO_CA),
    ],
)
def test_put_errors_store_nothing(error, problem):
    effects = Effects()
    caught = rotate_error(FakeApi(error=error), effects)
    assert caught.problem is problem
    assert str(caught) == str(problem)
    assert effects.stored == effects.probed == []
    assert effects.reloads == 0
    if problem is Problem.RATE_LIMITED:
        assert caught.retry_after == 42


def test_an_unexpected_put_error_is_not_swallowed():
    with pytest.raises(RuntimeError):
        rotate(FakeApi(error=RuntimeError("bug")), Effects())


def test_result_repr_is_redacted():
    result = rotation.RotationResult(SENTINEL, reconnected=True, stored=True)
    assert SENTINEL not in repr(result)
    assert SENTINEL not in str(result)
    assert "reconnected=True" in repr(result)


# --- the in-progress flag --------------------------------------------------------------


class FakeMqtt:
    auth_failed = True  # the old session was refused mid-rotation


class FakeTransport:
    def __init__(self) -> None:
        self.rotating = False
        self.mqtt = FakeMqtt()
        self.reauths = 0

    def tick(self) -> None:
        """What `SpanTransportManager._evaluate` does about a refused broker."""
        if rotation.reauth_due(self.mqtt.auth_failed, self.rotating):
            self.reauths += 1


def test_flag_blocks_reauth_while_rotating():
    transport = FakeTransport()
    with rotation.rotating(transport):
        transport.tick()
        assert transport.rotating is True
    assert transport.reauths == 0
    assert transport.rotating is False
    transport.tick()
    assert transport.reauths == 1


def test_flag_cleared_on_exception():
    transport = FakeTransport()
    with pytest.raises(RotationError), rotation.rotating(transport):
        rotate(FakeApi(error=rest.AuthFailed("401")), Effects())
    assert transport.rotating is False


def test_flag_cleared_on_cancel():
    transport = FakeTransport()

    async def main() -> None:
        async def slow() -> None:
            with rotation.rotating(transport):
                await asyncio.sleep(10)

        task = asyncio.create_task(slow())
        await asyncio.sleep(0)
        assert transport.rotating is True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert transport.rotating is False


def test_reauth_due():
    assert rotation.reauth_due(True, False) is True
    assert rotation.reauth_due(True, True) is False
    assert rotation.reauth_due(False, False) is False


# --- one at a time ---------------------------------------------------------------------


def test_concurrent_rotations_run_in_sequence():
    events: list[str] = []
    data = {"password": "old"}
    passwords = iter(["first-pw", "second-pw"])

    class StepApi:
        async def rotate_passphrase(self) -> RotatedPassphrase:
            events.append(f"put from {data['password']}")
            await asyncio.sleep(0.01)
            return RotatedPassphrase(next(passwords), SENTINEL)

    async def store(password: str) -> None:
        await asyncio.sleep(0.01)
        data["password"] = password
        events.append(f"stored {password}")

    async def probe(_password: str) -> None:
        await asyncio.sleep(0)

    async def reload() -> bool:
        return True

    async def main() -> list[rotation.RotationResult]:
        locks: dict[str, asyncio.Lock] = {}

        async def one() -> rotation.RotationResult:
            async with rotation.lock_for(locks, ENTRY):
                return await rotation.rotate(
                    StepApi(),
                    entry_id=ENTRY,
                    has_token=True,
                    has_ca=True,
                    store=store,
                    probe=probe,
                    reload=reload,
                )

        return await asyncio.gather(one(), one())

    results = asyncio.run(main())
    assert [r.reconnected for r in results] == [True, True]
    assert events == [
        "put from old",
        "stored first-pw",
        "put from first-pw",
        "stored second-pw",
    ]


def test_lock_is_per_entry():
    locks: dict[str, asyncio.Lock] = {}
    assert rotation.lock_for(locks, ENTRY) is rotation.lock_for(locks, ENTRY)
    assert rotation.lock_for(locks, ENTRY) is not rotation.lock_for(locks, OTHER)


# --- the PUT over HTTPS ----------------------------------------------------------------

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def serve(handler: Handler, scenario: Callable[[Callable[..., rest.SpanLocalApi]], Awaitable[Any]]):
    """Serve PUT /api/v2/auth/passphrase over TLS and run `scenario(make_api)`."""

    async def main() -> Any:
        app = web.Application()
        app.router.add_put("/api/v2/auth/passphrase", handler)
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(FIXTURES / "server.pem", FIXTURES / "server.key")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            async with aiohttp.ClientSession() as session:

                def make_api(**kwargs: Any) -> rest.SpanLocalApi:
                    kwargs.setdefault("ca_pem", CA)
                    kwargs.setdefault("access_token", TOKEN)
                    return rest.SpanLocalApi(session, "127.0.0.1", port=port, **kwargs)

                return await scenario(make_api)
        finally:
            await runner.cleanup()

    return asyncio.run(main())


def body(payload: Any, status: int = 200) -> Handler:
    async def handler(_request: web.Request) -> web.StreamResponse:
        return web.json_response(payload, status=status)

    return handler


def text(content: str, status: int = 200) -> Handler:
    async def handler(_request: web.Request) -> web.StreamResponse:
        return web.Response(text=content, status=status)

    return handler


def put_error(handler: Handler, **api_kwargs: Any) -> rest.SpanLocalError:
    async def scenario(make_api):
        with pytest.raises(rest.SpanLocalError) as caught:
            await make_api(**api_kwargs).rotate_passphrase()
        return caught.value

    return serve(handler, scenario)


GOOD = {"ebusBrokerPassword": BROKER_SENTINEL, "hopPassphrase": SENTINEL}


def test_put_returns_both_secrets():
    async def scenario(make_api):
        return await make_api().rotate_passphrase()

    rotated = serve(body(GOOD), scenario)
    assert (rotated.broker_password, rotated.hop_passphrase) == (BROKER_SENTINEL, SENTINEL)


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (text("not json"), rest.OutcomeUnknown),  # 2xx, unreadable
        (body({"hopPassphrase": SENTINEL}), rest.OutcomeUnknown),  # 2xx, incomplete
        (body(["a list"]), rest.OutcomeUnknown),
        # A null or empty field is missing, never the string "None".
        (body({**GOOD, "ebusBrokerPassword": None}), rest.OutcomeUnknown),
        (body({**GOOD, "ebusBrokerPassword": ""}), rest.OutcomeUnknown),
        (body({**GOOD, "hopPassphrase": None}), rest.OutcomeUnknown),
        (body({**GOOD, "hopPassphrase": ""}), rest.OutcomeUnknown),
        (body({**GOOD, "hopPassphrase": 1234}), rest.OutcomeUnknown),
        (text(""), rest.OutcomeUnknown),  # 2xx, empty
        (text("", status=500), rest.OutcomeUnknown),
        (text("", status=502), rest.OutcomeUnknown),
        (text("", status=504), rest.OutcomeUnknown),
        (text("", status=503), rest.UnexpectedResponse),
        (text("", status=400), rest.UnexpectedResponse),
        (text("", status=401), rest.AuthFailed),
        (text("", status=403), rest.AuthFailed),
    ],
)
def test_put_status_mapping(handler, expected):
    error = put_error(handler)
    assert type(error) is expected


def test_put_rate_limited():
    async def handler(_request: web.Request) -> web.StreamResponse:
        return web.json_response({}, status=429, headers={"Retry-After": "42"})

    error = put_error(handler)
    assert isinstance(error, rest.RateLimited)
    assert error.retry_after == 42


def test_put_timeout_after_sending_is_outcome_unknown():
    async def handler(_request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(2)
        return web.json_response(GOOD)

    error = put_error(handler, timeout=0.3)
    assert type(error) is rest.OutcomeUnknown


def _stalled_handshake(scenario: Callable[[int], Awaitable[Any]]) -> Any:
    """Run `scenario(port)` against a server that accepts and never answers TLS."""

    async def main() -> Any:
        held: list[asyncio.StreamWriter] = []

        async def accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            held.append(writer)

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await scenario(port)
        finally:
            for writer in held:
                writer.close()
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


def test_put_connect_timeout_is_not_sent():
    async def scenario(port: int) -> rest.SpanLocalError:
        async with aiohttp.ClientSession() as session:
            api = rest.SpanLocalApi(
                session, "127.0.0.1", port=port, ca_pem=CA, access_token=TOKEN, timeout=0.6
            )
            with pytest.raises(rest.SpanLocalError) as caught:
                await api.rotate_passphrase()
            return caught.value

    error = _stalled_handshake(scenario)
    assert type(error) is rest.NotSent


def test_flow_maps_a_connect_timeout_to_cannot_connect():
    async def scenario(port: int) -> RotationError:
        async with aiohttp.ClientSession() as session:
            api = rest.SpanLocalApi(
                session, "127.0.0.1", port=port, ca_pem=CA, access_token=TOKEN, timeout=0.6
            )
            with pytest.raises(RotationError) as caught:
                await rotation.rotate(api, **Effects().kwargs())
            return caught.value

    assert _stalled_handshake(scenario).problem is Problem.CANNOT_CONNECT


def test_put_without_a_pinned_ca_is_no_ca():
    async def main() -> RotationError:
        async with aiohttp.ClientSession() as session:
            api = rest.SpanLocalApi(session, "127.0.0.1", access_token=TOKEN)
            with pytest.raises(RotationError) as caught:
                await rotation.rotate(api, **Effects().kwargs())
            return caught.value

    assert asyncio.run(main()).problem is Problem.NO_CA


def test_put_untrusted_certificate_is_not_sent():
    other_ca = (FIXTURES / "other-ca.pem").read_text()
    error = put_error(body(GOOD), ca_pem=other_ca)
    assert type(error) is rest.NotSent
    assert isinstance(error, rest.CannotConnect)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_put_connection_refused_is_cannot_connect():
    async def main() -> RotationError:
        async with aiohttp.ClientSession() as session:
            api = rest.SpanLocalApi(
                session, "127.0.0.1", port=free_port(), ca_pem=CA, access_token=TOKEN
            )
            with pytest.raises(RotationError) as caught:
                await rotation.rotate(api, **Effects().kwargs())
            return caught.value

    assert asyncio.run(main()).problem is Problem.CANNOT_CONNECT


class RaisingSession:
    """A session whose request fails before anything is written."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def request(self, *_args: Any, **_kwargs: Any) -> Any:
        error = self.error

        class _Context:
            async def __aenter__(self) -> Any:
                raise error

            async def __aexit__(self, *_exc: object) -> None:
                return None

        return _Context()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (aiohttp.ConnectionTimeoutError("connect"), rest.NotSent),
        (aiohttp.ServerDisconnectedError(), rest.OutcomeUnknown),
        (aiohttp.SocketTimeoutError("read"), rest.OutcomeUnknown),
        (TimeoutError(), rest.OutcomeUnknown),
    ],
)
def test_put_transport_errors(error, expected):
    async def main() -> None:
        api = rest.SpanLocalApi(RaisingSession(error), "127.0.0.1", ca_pem=CA, access_token=TOKEN)  # type: ignore[arg-type]
        await api.rotate_passphrase()

    with pytest.raises(rest.SpanLocalError) as caught:
        asyncio.run(main())
    assert type(caught.value) is expected


def test_flow_over_https_maps_a_bad_body_and_a_502():
    async def scenario(make_api):
        with pytest.raises(RotationError) as caught:
            await rotation.rotate(make_api(), **Effects().kwargs())
        return caught.value.problem

    assert serve(text("{broken"), scenario) is Problem.OUTCOME_UNKNOWN
    assert serve(text("", status=502), scenario) is Problem.OUTCOME_UNKNOWN
    assert serve(text("", status=503), scenario) is Problem.FAILED
    assert serve(body({**GOOD, "ebusBrokerPassword": None}), scenario) is (Problem.OUTCOME_UNKNOWN)


# --- redaction -------------------------------------------------------------------------

REDACTION_LOGGERS = ("span_client", "span_ebus", "aiohttp", "asyncio")


@pytest.fixture
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG)
    for name in REDACTION_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)
    return caplog


def _assert_absent(secret: str, caplog, things: list[Any]) -> None:
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in str(record.msg)
        assert all(secret not in repr(arg) for arg in record.args or ())
    for thing in things:
        assert secret not in str(thing)
        assert secret not in repr(thing)


def _every_path(make_api: Callable[..., Any]) -> tuple[list[Any], list[Any]]:
    """Run the flow down each path; return what was raised or returned, and stored."""
    outcomes: list[Any] = []
    stored: list[Any] = []
    cases = [
        Effects(),
        Effects(refusals=3),
        Effects(refusals=100),
        Effects(refusals=1, probe_error=RuntimeError),
        Effects(store_error=RuntimeError("entry gone")),
        Effects(reload_error=RuntimeError("busy")),
        Effects(reload_result=False),
    ]
    for effects in cases:
        outcomes.append(rotate(make_api(), effects))
        stored.extend(effects.stored)
    for error in (
        rest.AuthFailed("401"),
        rest.RateLimited(5),
        rest.OutcomeUnknown("lost"),
        rest.NotSent("refused"),
        rest.UnexpectedResponse("HTTP 503"),
    ):
        effects = Effects()
        outcomes.append(rotate_error(FakeApi(error=error), effects))
        stored.extend(effects.stored)
    return outcomes, stored


@pytest.mark.parametrize("broker", [NEW_PASSWORD, BROKER_SENTINEL])
def test_passphrase_never_leaks(debug_logs, broker):
    outcomes, stored = _every_path(lambda: FakeApi(broker=broker))
    # Only the broker password is ever stored.
    assert set(stored) == {broker}
    results = [o for o in outcomes if isinstance(o, rotation.RotationResult)]
    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert results and errors
    assert all(r.hop_passphrase == SENTINEL for r in results)
    _assert_absent(SENTINEL, debug_logs, outcomes)
    if broker == BROKER_SENTINEL:
        _assert_absent(BROKER_SENTINEL, debug_logs, outcomes)


def test_passphrase_never_leaks_over_https(debug_logs):
    outcomes: list[Any] = []

    async def collect(make_api):
        try:
            outcomes.append(await rotation.rotate(make_api(), **Effects().kwargs()))
        except Exception as exc:  # noqa: BLE001 — collected for the check
            outcomes.append(exc)

    serve(body(GOOD), collect)
    serve(body({"hopPassphrase": SENTINEL}), collect)  # incomplete: outcome unknown
    serve(text(f'{{"hopPassphrase": "{SENTINEL}"'), collect)  # unreadable
    serve(body(GOOD, status=502), collect)
    assert isinstance(outcomes[0], rotation.RotationResult)
    assert all(isinstance(o, RotationError) for o in outcomes[1:])
    _assert_absent(SENTINEL, debug_logs, outcomes)
    _assert_absent(BROKER_SENTINEL, debug_logs, outcomes)
