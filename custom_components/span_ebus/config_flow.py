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
PROBE_TIMEOUT = 15.0

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_DEVICE_UUID): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
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


def _normalize_device_uuid(raw: str) -> str | None:
    """Accept a device UUID in any common spelling, or None if it isn't one."""
    try:
        return str(uuid.UUID(raw.strip()))
    except (ValueError, AttributeError):
        return None


def _probe(
    tokens: cloud_auth.CloudTokens, device_uuid: str, user_id: str | None
) -> str | None:
    """Verify the channel actually carries telemetry; return the panel serial.

    Runs on an executor thread. The device UUID is the identifier SPAN's cloud
    binds the Ably channel to, so a wrong one attaches to a channel that simply
    stays silent — which used to leave the integration installed with zero
    entities and nothing in the log. Probing here turns that into a form error.
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
            device_uuid = _normalize_device_uuid(user_input[CONF_DEVICE_UUID])

            if device_uuid is None:
                errors[CONF_DEVICE_UUID] = "invalid_device_uuid"
            else:
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
                        errors[CONF_DEVICE_UUID] = "no_telemetry"
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

    def _suggested_values(self, user_input: dict[str, Any] | None) -> dict[str, Any]:
        """Prefill email and device UUID so a retry doesn't retype them.

        On reauth the device UUID is already known and must not change — the
        Ably channel is bound to it — so it comes from the existing entry.
        """
        suggested: dict[str, Any] = {}
        if self._reauth_entry_data is not None:
            suggested[CONF_EMAIL] = self._reauth_entry_data.get(CONF_EMAIL)
            suggested[CONF_DEVICE_UUID] = self._reauth_entry_data.get(CONF_DEVICE_UUID)
        if user_input:
            for key in (CONF_EMAIL, CONF_DEVICE_UUID):
                if user_input.get(key):
                    suggested[key] = user_input[key]
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
