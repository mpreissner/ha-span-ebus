"""Tests for the MQTT adapter's own logic: CONNACK handling, publish
acknowledgement and disconnect cleanup. paho's callbacks are invoked directly,
so no broker is needed.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from paho.mqtt.enums import MQTTErrorCode
from span_client.local import mqtt, rest

CA = (Path(__file__).parent / "fixtures" / "tls" / "ca.pem").read_text()


def make(**kwargs):
    messages: list = []
    states: list = []
    client = mqtt.LocalMqtt(
        host="127.0.0.1",
        port=8883,
        username="u",
        password="p",
        ca_pem=CA,
        client_id="test",
        on_message=lambda *m: messages.append(m),
        on_connection=states.append,
        **kwargs,
    )
    client._loop = asyncio.get_running_loop()
    return client, messages, states


class FakeInfo:
    def __init__(self, mid: int, published: bool = False) -> None:
        self.mid = mid
        self.rc = MQTTErrorCode.MQTT_ERR_SUCCESS
        self._published = published

    def is_published(self) -> bool:
        return self._published


@pytest.mark.parametrize(
    ("code", "error"), [(5, rest.AuthFailed), (134, rest.AuthFailed), (3, rest.CannotConnect)]
)
def test_refused_connack(code, error):
    async def main():
        client, _, states = make()
        client._connack = asyncio.get_running_loop().create_future()
        client._handle_connect(client._client, None, None, code, None)
        with pytest.raises(error):
            await client._connack
        assert states == []

    asyncio.run(main())


def test_accepted_connack_subscribes_and_reports():
    async def main():
        client, _, states = make(subscriptions=["ebus/5/#"])
        subscribed = []
        client._client.subscribe = lambda topics: (
            subscribed.append(topics),
            (MQTTErrorCode.MQTT_ERR_SUCCESS, 1),
        )[1]
        client._connack = asyncio.get_running_loop().create_future()
        client._handle_connect(client._client, None, None, 0, None)
        await client._connack
        assert subscribed == [[("ebus/5/#", 1)]]
        assert states == [True]
        assert client.connected

    asyncio.run(main())


def test_publish_waits_for_ack():
    async def main():
        client, _, _ = make()
        client._connected = True
        client._client.publish = lambda *a, **k: FakeInfo(7)
        task = asyncio.create_task(client.publish("t/set", "OPEN"))
        await asyncio.sleep(0)
        assert not task.done()
        client._handle_publish(client._client, None, 7, 0, None)
        await task
        assert client._pending == {}

    asyncio.run(main())


def test_publish_already_acknowledged():
    async def main():
        client, _, _ = make()
        client._connected = True
        client._client.publish = lambda *a, **k: FakeInfo(1, published=True)
        await client.publish("t/set", "OPEN")

    asyncio.run(main())


def test_publish_timeout_is_outcome_unknown():
    async def main():
        client, _, _ = make()
        client._connected = True
        client._client.publish = lambda *a, **k: FakeInfo(3)
        with pytest.raises(rest.OutcomeUnknown):
            await client.publish("t/set", "OPEN", timeout=0.01)

    asyncio.run(main())


def test_publish_when_disconnected():
    async def main():
        client, _, _ = make()
        with pytest.raises(rest.CannotConnect):
            await client.publish("t/set", "OPEN")

    asyncio.run(main())


def test_disconnect_fails_pending_publishes():
    async def main():
        client, _, states = make()
        client._set_connected(True)
        client._client.publish = lambda *a, **k: FakeInfo(9)
        task = asyncio.create_task(client.publish("t/set", "OPEN"))
        await asyncio.sleep(0)
        client._handle_disconnect(client._client, None, None, 7, None)
        with pytest.raises(rest.CannotConnect):
            await task
        assert states == [True, False]
        assert client._disconnected.is_set()

    asyncio.run(main())


def test_message_handler_errors_are_contained():
    async def main():
        def boom(*_):
            raise RuntimeError("handler bug")

        client = mqtt.LocalMqtt(
            host="h", port=1, username="u", password=None, ca_pem=CA, client_id="c", on_message=boom
        )
        msg = SimpleNamespace(topic="ebus/5/x/$state", payload=b"ready", retain=True)
        client._handle_message(client._client, None, msg)  # must not raise

    asyncio.run(main())


def test_messages_are_forwarded():
    async def main():
        client, messages, _ = make()
        msg = SimpleNamespace(topic="ebus/5/x/$state", payload=b"ready", retain=True)
        client._handle_message(client._client, None, msg)
        assert messages == [("ebus/5/x/$state", b"ready", True)]

    asyncio.run(main())
