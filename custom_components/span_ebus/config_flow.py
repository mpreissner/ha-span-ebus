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
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_DEVICE_UUID,
    CONF_EMAIL,
    CONF_SERIAL,
    CONF_TOKENS,
    CONF_USER_ID,
    DOMAIN,
)
from .span_client import cloud_auth
from .span_client.backend import CloudBackend

_LOGGER = logging.getLogger(__name__)

# How long to wait for the first telemetry frame when validating the channel.
# Generous: the subscribe is deliberately delayed a couple of seconds and the
# first frames SPAN sends often carry no circuits.
PROBE_TIMEOUT = 45.0

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL)
        ),
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


def _probe(
    tokens: cloud_auth.CloudTokens, device_uuid: str, user_id: str | None
) -> str | None:
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


class SpanEbusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the SPAN eBus config and reauth flows."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry_data: Mapping[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
                        return self.async_update_reload_and_abort(entry, data=data)

                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=email, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, self._suggested_values(user_input)
            ),
            errors=errors,
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

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
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
