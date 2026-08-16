"""Config flow: sign in to SPAN cloud once, store tokens (never the password)."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import logging
import uuid
from collections.abc import Mapping
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
    CONF_TOKENS,
    CONF_USER_ID,
    DOMAIN,
)
from .span_client import cloud_auth

_LOGGER = logging.getLogger(__name__)

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
            try:
                tokens = await self.hass.async_add_executor_job(
                    cloud_auth.authenticate, email, password
                )
            except cloud_auth.CloudAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 — surface anything else as a connent error
                _LOGGER.exception("unexpected error authenticating to SPAN cloud")
                errors["base"] = "cannot_connect"
            else:
                # The password is discarded here — only tokens are persisted.
                user_id = _user_id_from_access_token(tokens.access_token)
                await self.async_set_unique_id(user_id or email.lower())

                data = {
                    CONF_EMAIL: email,
                    CONF_TOKENS: dataclasses.asdict(tokens),
                    CONF_USER_ID: user_id,
                    CONF_DEVICE_UUID: str(uuid.uuid4()),
                }

                if self._reauth_entry_data is not None:
                    entry = self._get_reauth_entry()
                    # Keep the original device UUID across a reauth.
                    data[CONF_DEVICE_UUID] = self._reauth_entry_data.get(
                        CONF_DEVICE_UUID, data[CONF_DEVICE_UUID]
                    )
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(entry, data=data)

                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=email, data=data)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

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
                step_id="reauth_confirm", data_schema=STEP_USER_SCHEMA
            )
        return await self.async_step_user(user_input)
