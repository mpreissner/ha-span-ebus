"""Config flow: sign in to SPAN cloud once, store tokens (never the password)."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import logging
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_DECLINED,
    CONF_DEVICE_UUID,
    CONF_EMAIL,
    CONF_PANELS,
    CONF_PRIMARY_PANEL,
    CONF_SERIAL,
    CONF_TOKENS,
    CONF_USER_ID,
    DOMAIN,
    TOKEN_DIR,
)
from .span_client import cloud_auth
from .span_client.backend import (
    CloudBackend,
    SiteInfo,
    device_keys,
    discover_sites,
    panel_labels,
    panel_plan,
)

_LOGGER = logging.getLogger(__name__)

# How long to wait for the first telemetry frame when validating the channel.
# Generous: the subscribe is deliberately delayed a couple of seconds and the
# first frames SPAN sends often carry no circuits.
PROBE_TIMEOUT = 45.0

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


def _user_id_from_access_token(access_token: str) -> str | None:
    """Pull the Cognito `username` claim (the SPAN userId) from the JWT."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
        return None
    value = claims.get("username")
    return value if isinstance(value, str) else None


def _new_device_uuid() -> str:
    """Mint this install's client id for the Ably channel.

    SPAN does not issue these — the AblyToken RPC echoes back whatever we send,
    and `SubscribeAndGetTraits` registers the resulting channel for us. Uppercase
    to match the mobile client's spelling; channel names are case-sensitive, so
    whatever we generate here must then be used verbatim forever.
    """
    return str(uuid.uuid4()).upper()


def _probe(tokens: cloud_auth.CloudTokens, device_uuid: str, user_id: str | None) -> str | None:
    """Verify the channel actually carries telemetry; return the panel serial.

    Runs on an executor thread. This exercises the whole live path — topology
    lookup, Ably token, subscribe, first frame — so setup fails loudly instead of
    finishing with zero entities and nothing in the log.
    """
    with tempfile.TemporaryDirectory() as tmp:
        token_path = Path(tmp) / "tokens.json"
        cloud_auth.save_tokens(token_path, tokens)
        backend = CloudBackend(token_path, device_uuid, user_id=user_id)
        schema = backend.probe(timeout=PROBE_TIMEOUT)
    return schema.serial if schema.serial != "span-cloud" else None


def _panels_schema(labels: dict[str, str], ticked: list[str]) -> vol.Schema:
    options = [SelectOptionDict(value=key, label=label) for key, label in labels.items()]
    selector = SelectSelector(
        SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
    )
    return vol.Schema({vol.Required(CONF_PANELS, default=ticked): selector})


def _panel_options(labels: dict[str, str], ticked: list[str]) -> dict[str, list[str]]:
    """The stored answer: what was left off has been seen, so it stays quiet."""
    return {CONF_PANELS: ticked, CONF_DECLINED: [key for key in labels if key not in ticked]}


def _sites_from_token_store(token_path: Path) -> list[SiteInfo]:
    return discover_sites(cloud_auth.access_token_from_store(token_path))


class SpanEbusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the SPAN eBus config and reauth flows."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry_data: Mapping[str, Any] | None = None
        self._entry_data: dict[str, Any] = {}
        self._labels: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SpanEbusOptionsFlow:
        return SpanEbusOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            # Reauth must keep the entry's existing channel id; setup mints one.
            device_uuid = self._existing_device_uuid() or _new_device_uuid()

            try:
                tokens = await self.hass.async_add_executor_job(
                    cloud_auth.authenticate, email, password
                )
            except cloud_auth.CloudAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("unexpected error authenticating to SPAN cloud")
                errors["base"] = "cannot_connect"
            else:
                # The password is discarded here — only tokens are persisted.
                user_id = _user_id_from_access_token(tokens.access_token)
                serial: str | None = None
                try:
                    serial = await self.hass.async_add_executor_job(
                        _probe, tokens, device_uuid, user_id
                    )
                except TimeoutError:
                    errors["base"] = "no_telemetry"
                except Exception:
                    _LOGGER.exception("SPAN cloud telemetry probe failed")
                    errors["base"] = "cannot_connect"

                if not errors:
                    await self.async_set_unique_id(user_id or email.lower())

                    data = {
                        CONF_EMAIL: email,
                        CONF_TOKENS: dataclasses.asdict(tokens),
                        CONF_USER_ID: user_id,
                        CONF_DEVICE_UUID: device_uuid,
                        CONF_SERIAL: serial,
                    }

                    if self._reauth_entry_data is not None:
                        entry = self._get_reauth_entry()
                        self._abort_if_unique_id_mismatch(reason="wrong_account")
                        # Reauth proves the login and changes nothing else. The
                        # probe's serial is whichever panel came first, which on
                        # a multi-panel account need not be the saved one.
                        data = {
                            **entry.data,
                            **data,
                            CONF_SERIAL: entry.data.get(CONF_SERIAL) or serial,
                        }
                        return self.async_update_reload_and_abort(entry, data=data)

                    self._abort_if_unique_id_configured()
                    self._entry_data = data
                    self._labels = await self._discover_labels(tokens.access_token)
                    if len(self._labels) > 1:
                        return await self.async_step_panels()
                    return self.async_create_entry(title=email, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, self._suggested_values(user_input)
            ),
            errors=errors,
        )

    async def _discover_labels(self, access_token: str) -> dict[str, str]:
        """The account's panels. Failing to learn them doesn't block setup: nothing
        is recorded, and every panel runs until a start can read the account."""
        try:
            sites = await self.hass.async_add_executor_job(discover_sites, access_token)
        except Exception as err:  # noqa: BLE001 — discovery is optional here
            _LOGGER.debug("could not list SPAN panels during setup: %s", err)
            return {}
        return panel_labels(sites)

    async def async_step_panels(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        ticked = list(self._labels)
        if user_input is not None:
            ticked = user_input[CONF_PANELS]
            if ticked:
                return self.async_create_entry(
                    title=self._entry_data[CONF_EMAIL],
                    data=self._entry_data,
                    options=_panel_options(self._labels, ticked),
                )
            errors["base"] = "no_panels"
        return self.async_show_form(
            step_id="panels", data_schema=_panels_schema(self._labels, ticked), errors=errors
        )

    def _existing_device_uuid(self) -> str | None:
        """The channel id already on the entry, on reauth.

        It must survive re-authentication: SPAN's realtime channel name embeds it
        verbatim, so a new one would silently orphan every existing entity.
        """
        if self._reauth_entry_data is None:
            return None
        value = self._reauth_entry_data.get(CONF_DEVICE_UUID)
        return value if isinstance(value, str) and value else None

    def _suggested_values(self, user_input: dict[str, Any] | None) -> dict[str, Any]:
        """Prefill the email so a retry or reauth doesn't retype it."""
        suggested: dict[str, Any] = {}
        if self._reauth_entry_data is not None:
            suggested[CONF_EMAIL] = self._reauth_entry_data.get(CONF_EMAIL)
        if user_input and user_input.get(CONF_EMAIL):
            suggested[CONF_EMAIL] = user_input[CONF_EMAIL]
        return {k: v for k, v in suggested.items() if v is not None}

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._reauth_entry_data = entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=self.add_suggested_values_to_schema(
                    STEP_USER_SCHEMA, self._suggested_values(None)
                ),
            )
        return await self.async_step_user(user_input)


class SpanEbusOptionsFlow(OptionsFlow):
    """Change which of the account's panels run here."""

    def __init__(self) -> None:
        self._sites: list[SiteInfo] = []
        self._chosen: list[str] = []
        self._labels: dict[str, str] = {}
        self._ticked: list[str] = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None:
            token_path = (
                Path(self.hass.config.path(TOKEN_DIR)) / f"{self.config_entry.entry_id}.json"
            )
            try:
                sites = await self.hass.async_add_executor_job(_sites_from_token_store, token_path)
            except Exception as err:  # noqa: BLE001 — the form can't be built without them
                _LOGGER.debug("could not list SPAN panels: %s", err)
                return self.async_abort(reason="cannot_connect")
            options = self.config_entry.options
            plan = panel_plan(sites, options.get(CONF_PANELS), options.get(CONF_DECLINED, ()))
            self._sites = plan.sites
            self._labels = panel_labels(plan.sites)
            self._chosen = [key for key in plan.chosen if key in self._labels]
            self._ticked = self._chosen
        else:
            self._ticked = user_input[CONF_PANELS]
            if not self._ticked:
                errors["base"] = "no_panels"
            elif set(self._chosen) - set(self._ticked):
                return await self.async_step_confirm_remove()
            else:
                return self._save()
        return self.async_show_form(
            step_id="init", data_schema=_panels_schema(self._labels, self._ticked), errors=errors
        )

    async def async_step_confirm_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            count = len(set(self._chosen) - set(self._ticked))
            return self.async_show_form(
                step_id="confirm_remove",
                data_schema=vol.Schema({}),
                description_placeholders={"count": str(count)},
            )
        self._remove_devices()
        return self._save()

    def _remove_devices(self) -> None:
        """Drop the unticked panels' devices. Their energy totals stay with the entry,
        so ticking one again resumes them."""
        primary_panel = self.config_entry.data.get(CONF_PRIMARY_PANEL)
        before = device_keys(self._sites, self._chosen, primary_panel)
        after = device_keys(self._sites, self._ticked, primary_panel)
        registry = dr.async_get(self.hass)
        dropped = {(DOMAIN, key) for key in before - after}
        entry_id = self.config_entry.entry_id
        for device in dr.async_entries_for_config_entry(registry, entry_id):
            if device.identifiers & dropped:
                registry.async_update_device(device.id, remove_config_entry_id=entry_id)

    def _save(self) -> ConfigFlowResult:
        # Stored here, not left to the flow's result: the reload must see the answer.
        entry = self.config_entry
        options = {**entry.options, **_panel_options(self._labels, self._ticked)}
        self.hass.config_entries.async_update_entry(entry, options=options)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data=options)
