"""The integration's services: `rotate_credentials` and `analyze_leg_balance`.

The rotation flow lives in `span_client.local.rotation`, free of Home
Assistant; this module only finds the entry and wires the side effects in. The
new passphrase is in the response and nowhere else: never logged, stored or put
in an error. The leg-balance action is read-only (`leg_balance`).
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from functools import partial
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BROKER_PASSWORD,
    CONF_CA_PEM,
    CONF_LOCAL_SERIAL,
    DOMAIN,
)
from .leg_balance import async_analyze
from .span_client import leg_balance as lb
from .span_client.local import rotation
from .span_client.local.rest import SpanLocalApi, SpanLocalError
from .transport import local_mqtt_for

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

SERVICE_ROTATE_CREDENTIALS = "rotate_credentials"
SERVICE_ANALYZE_LEG_BALANCE = "analyze_leg_balance"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_DEVICE_ID = "device_id"
ROTATION_LOCKS = "rotation_locks"
LEG_BALANCE = "leg_balance"  # the prefix of its error keys

ATTR_DAYS = "days"
ATTR_MIN_GAIN_A = "min_gain_a"
ATTR_BALANCED_BELOW_A = "balanced_below_a"

ROTATE_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_CONFIG_ENTRY_ID): str, vol.Optional(ATTR_DEVICE_ID): str}
)
_AMPS = vol.All(vol.Coerce(float), vol.Range(min=0))
LEG_BALANCE_SCHEMA = ROTATE_SCHEMA.extend(
    {
        vol.Optional(ATTR_DAYS, default=lb.DEFAULT_DAYS): vol.All(
            vol.Coerce(int), vol.Range(min=7, max=90)
        ),
        vol.Optional(ATTR_MIN_GAIN_A, default=lb.MIN_GAIN_A): _AMPS,
        vol.Optional(ATTR_BALANCED_BELOW_A, default=lb.BALANCED_BELOW_A): _AMPS,
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    hass.data.setdefault(DOMAIN, {}).setdefault(ROTATION_LOCKS, {})
    hass.services.async_register(
        DOMAIN,
        SERVICE_ROTATE_CREDENTIALS,
        partial(_async_rotate_credentials, hass),
        schema=ROTATE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ANALYZE_LEG_BALANCE,
        partial(_async_analyze_leg_balance, hass),
        schema=LEG_BALANCE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def _error(
    problem: rotation.Problem, retry_after: int | None = None, prefix: str = "rotate"
) -> HomeAssistantError:
    """The translated error. Placeholders never carry a secret, host or serial."""
    kind = ServiceValidationError if problem.is_refusal else HomeAssistantError
    placeholders = None
    if problem is rotation.Problem.RATE_LIMITED:
        placeholders = {"retry_after": str(retry_after) if retry_after else "a few"}
    return kind(
        translation_domain=DOMAIN,
        translation_key=f"{prefix}_{problem}",
        translation_placeholders=placeholders,
    )


def _pick(hass: HomeAssistant, call: ServiceCall, prefix: str = "rotate") -> ConfigEntry:
    entries = {
        entry.entry_id: rotation.Candidate(
            local=CONF_LOCAL_SERIAL in entry.data,
            loaded=entry.state is ConfigEntryState.LOADED,
        )
        for entry in hass.config_entries.async_entries(
            DOMAIN, include_ignore=False, include_disabled=False
        )
    }
    device_entry_ids: set[str] | None = None
    if (device_id := call.data.get(ATTR_DEVICE_ID)) is not None:
        device = dr.async_get(hass).async_get(device_id)
        device_entry_ids = set(device.config_entries) if device is not None else set()
    target = rotation.pick_target(entries, call.data.get(ATTR_CONFIG_ENTRY_ID), device_entry_ids)
    if isinstance(target, rotation.Problem):
        raise _error(target, prefix=prefix)
    entry = hass.config_entries.async_get_entry(target)
    if entry is None:
        raise _error(rotation.Problem.NO_LOCAL_PANEL, prefix=prefix)
    return entry


async def _async_analyze_leg_balance(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Read-only, so open to every user: nothing is written or sent."""
    entry = _pick(hass, call, prefix=LEG_BALANCE)
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or runtime.transport is None:
        raise _error(rotation.Problem.NOT_LOADED, prefix=LEG_BALANCE)
    return await async_analyze(
        hass,
        entry,
        days=call.data[ATTR_DAYS],
        min_gain_a=call.data[ATTR_MIN_GAIN_A],
        balanced_below_a=call.data[ATTR_BALANCED_BELOW_A],
    )


async def _async_rotate_credentials(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    if not await rotation.is_admin(call.context.user_id, hass.auth.async_get_user):
        raise Unauthorized(context=call.context, permission=POLICY_CONTROL)

    locks: dict[str, asyncio.Lock] = hass.data[DOMAIN][ROTATION_LOCKS]
    locked_id = _pick(hass, call).entry_id
    lock = rotation.lock_for(locks, locked_id)
    async with lock:
        # Again: the entry may have changed while this call waited. If it is
        # now another entry, this lock does not cover it.
        entry = _pick(hass, call)
        if entry.entry_id != locked_id:
            raise _error(rotation.Problem.NOT_LOADED)
        runtime = getattr(entry, "runtime_data", None)
        transport = runtime.transport if runtime is not None else None
        if transport is None:
            raise _error(rotation.Problem.NOT_LOADED)
        data = entry.data
        try:
            api = SpanLocalApi(
                async_get_clientsession(hass),
                data[CONF_HOST],
                ca_pem=data.get(CONF_CA_PEM),
                access_token=data.get(CONF_ACCESS_TOKEN),
            )
        except (SpanLocalError, ssl.SSLError, ValueError):
            # A stored CA that will not load: nothing was sent. Unreachable on
            # a loaded entry, since the transport pins the same CA at setup.
            raise _error(rotation.Problem.NO_CA) from None

        async def store(password: str) -> None:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_BROKER_PASSWORD: password}
            )

        async def probe(password: str) -> None:
            # The entry's own client id, not a made-up one. Under MQTT 3.1.1
            # the broker takes over the old session only after a successful
            # CONNECT, so a failed probe leaves the running link alone, and a
            # new id risks the broker's ACL refusing it whatever the password.
            broker = local_mqtt_for(entry.data, password, on_message=_ignore)
            try:
                await broker.connect_once()
            finally:
                with contextlib.suppress(Exception):
                    await broker.stop()

        async def reload() -> bool:
            return await hass.config_entries.async_reload(entry.entry_id)

        # The reload runs inside rotating(): until it has replaced the old
        # transport, a broker refusing the old password must not open a
        # reauth. The flag is then cleared on the old transport, which the
        # reload has already stopped, so clearing it there is harmless.
        try:
            with rotation.rotating(transport):
                result = await rotation.rotate(
                    api,
                    entry_id=entry.entry_id,
                    has_token=bool(data.get(CONF_ACCESS_TOKEN)),
                    has_ca=bool(data.get(CONF_CA_PEM)),
                    store=store,
                    probe=probe,
                    reload=reload,
                )
        except rotation.RotationError as exc:
            raise _error(exc.problem, exc.retry_after) from None
    return result.as_response()


def _ignore(_topic: str, _payload: bytes, _retained: bool) -> None:
    """The probe only logs in; it reads nothing."""
