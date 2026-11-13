"""Config flow: add a panel over its local API, or a SPAN account over the cloud.

Neither the cloud password nor the panel passphrase is stored; only the
tokens and broker credentials they buy.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import json
import logging
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import zeroconf as ha_zeroconf
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from .const import (
    CLOUD_DATA_KEYS,
    CLOUD_OPTION_KEYS,
    CONF_ACCESS_TOKEN,
    CONF_BROKER_PASSWORD,
    CONF_BROKER_USERNAME,
    CONF_CA_PEM,
    CONF_CLIENT_NAME,
    CONF_DECLINED,
    CONF_DEVICE_UUID,
    CONF_EMAIL,
    CONF_FEEDS,
    CONF_LOCAL_SERIAL,
    CONF_MQTTS_PORT,
    CONF_PANELS,
    CONF_PRIMARY_PANEL,
    CONF_SERIAL,
    CONF_TOKENS,
    CONF_TRANSPORT_MODE,
    CONF_USER_ID,
    DOMAIN,
    LOCAL_REAUTH,
    TOKEN_DIR,
)
from .span_client import cloud_auth, feeds
from .span_client.backend import (
    CloudBackend,
    SiteInfo,
    device_keys,
    discover_sites,
    find_panel,
    panel_circuits,
    panel_labels,
    panel_plan,
    panel_traits,
    primary_panel_for,
    same_serial,
)
from .span_client.link import (
    LINKED_MINOR_VERSION,
    UNLINKED_MINOR_VERSION,
    PanelLink,
    feeds_from_kinds,
    link_to_options,
    match_circuits,
    unlinked_entry,
)
from .span_client.local import discovery, onboard, tls
from .span_client.local.health import Mode
from .span_client.local.rest import NotFound, SpanLocalError, UnexpectedResponse

if TYPE_CHECKING:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

_LOGGER = logging.getLogger(__name__)

CONF_CIRCUIT = "circuit"
CONF_PASSPHRASE = "passphrase"

# How long the add-panel picker listens for panels before giving up on mDNS.
BROWSE_SECONDS = 3.0
RESOLVE_MS = 1500
CONF_PICK = "panel"
PICK_MANUAL = "manual"

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


STEP_HOST_SCHEMA = vol.Schema({vol.Required(CONF_HOST): TextSelector()})
# Never stored or logged: it buys a client registration and is dropped.
STEP_PASSPHRASE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSPHRASE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


def _is_cloud_account(data: Mapping[str, Any]) -> bool:
    """A whole account added over the cloud, rather than one panel added locally."""
    return CONF_TOKENS in data and CONF_LOCAL_SERIAL not in data


def _is_linked(data: Mapping[str, Any]) -> bool:
    """A panel added locally with the cloud as its fallback."""
    return CONF_TOKENS in data and CONF_LOCAL_SERIAL in data


def _account_key(user_id: str | None, email: str) -> str:
    """What an account entry is keyed by: the user id, else the email."""
    return user_id or email.lower()


def _same_account(data: Mapping[str, Any], user_id: str | None, email: str) -> bool:
    """Whether an entry's stored account is this one, by user id or by email."""
    keys = {key for key in (user_id, email.lower()) if key}
    stored = {data.get(CONF_USER_ID), str(data.get(CONF_EMAIL, "")).lower()}
    return bool(keys & stored)


def _account_has_panel(entry: ConfigEntry, serial: str) -> bool:
    """Whether a cloud account entry already runs the panel with this serial."""
    keys = [entry.data.get(CONF_SERIAL), *entry.options.get(CONF_PANELS, [])]
    return any(isinstance(key, str) and same_serial(key, serial) for key in keys)


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


def _select(choices: dict[str, str]) -> SelectSelector:
    options = [SelectOptionDict(value=value, label=label) for value, label in choices.items()]
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))


def _panel_options(
    labels: dict[str, str], ticked: list[str], answers: dict[str, feeds.Feed]
) -> dict[str, Any]:
    """The stored answer: what was left off has been seen, so it stays quiet."""
    return {
        CONF_PANELS: ticked,
        CONF_DECLINED: [key for key in labels if key not in ticked],
        CONF_FEEDS: answers,
    }


def _new_link(cloud_key: str, serial: str, site: SiteInfo) -> PanelLink:
    """A fresh link record; its circuits and feeds are filled in from traits."""
    return PanelLink(
        cloud_key=cloud_key,
        local_serial=serial,
        site_panel_count=len(site.panels),
        linked_at=dt_util.utcnow().isoformat(),
    )


def _sites_from_token_store(token_path: Path) -> list[SiteInfo]:
    return discover_sites(cloud_auth.access_token_from_store(token_path))


class _FeedSteps:
    """The questions about what feeds each panel, shared by setup and options."""

    def __init__(self) -> None:
        super().__init__()
        self._sites: list[SiteInfo] = []
        self._labels: dict[str, str] = {}
        self._ticked: list[str] = []
        self._feeds: dict[str, feeds.Feed] = {}
        self._asking: list[str] = []
        self._circuits: dict[str, dict[str, str]] = {}

    def _access_token(self) -> str:
        raise NotImplementedError

    def _identity(self) -> Mapping[str, Any]:
        """Where the entry's user id, device UUID and primary panel are found."""
        raise NotImplementedError

    async def _finish_feeds(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def _ask_feeds(self) -> ConfigFlowResult:
        """Move on from the picker: one panel has nothing to ask about."""
        self._feeds = feeds.clean_feeds(self._feeds, self._ticked)
        if len(self._ticked) > 1:
            return await self.async_step_feed()
        return await self._finish_feeds()

    async def async_step_feed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        labels = {key: self._labels[key] for key in self._ticked}
        errors: dict[str, str] = {}
        if user_input is not None:
            answers = {
                key: feeds.from_form(user_input[label], self._feeds.get(key))
                for key, label in labels.items()
            }
            self._feeds = answers
            if feeds.has_cycle(answers):
                errors["base"] = "feed_cycle"
            else:
                self._asking = feeds.branch_fed(answers)
                return await self._next_feed_circuit()
        schema = {
            vol.Required(label, default=feeds.form_value(self._feeds[key])): _select(
                feeds.feed_choices(key, labels, self._feeds[key])
            )
            for key, label in labels.items()
        }
        return self.async_show_form(step_id="feed", data_schema=vol.Schema(schema), errors=errors)

    async def _next_feed_circuit(self) -> ConfigFlowResult:
        """Ask about the next branch feed whose upstream circuits can be read.

        A panel that can't be read keeps its answer without a circuit, and options
        shows it as unfinished, so one failure doesn't lose the rest.
        """
        while self._asking:
            if await self._upstream_circuits(self._asking[0]):
                return await self.async_step_feed_circuit()
            self._asking.pop(0)
        return await self._finish_feeds()

    async def _upstream_circuits(self, key: str) -> dict[str, str]:
        upstream = self._feeds[key]["upstream"]
        if upstream not in self._circuits:
            self._circuits[upstream] = await self.hass.async_add_executor_job(
                self._read_circuits, upstream
            )
        return self._circuits[upstream]

    def _read_circuits(self, upstream: str) -> dict[str, str]:
        found = find_panel(self._sites, upstream)
        if found is None:
            return {}
        identity = self._identity()
        try:
            circuits = panel_circuits(
                self._access_token(),
                *found,
                user_id=identity.get(CONF_USER_ID),
                device_uuid=identity[CONF_DEVICE_UUID],
                primary_panel=identity.get(CONF_PRIMARY_PANEL, primary_panel_for(self._sites)),
            )
        except Exception as err:  # noqa: BLE001 — the question can wait for options
            _LOGGER.debug("could not list a SPAN panel's circuits: %s", err)
            return {}
        return feeds.circuit_choices(circuits)

    async def async_step_feed_circuit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        key = self._asking[0]
        feed = self._feeds[key]
        choices = self._circuits[feed["upstream"]]
        if user_input is not None:
            feed["circuit"] = int(user_input[CONF_CIRCUIT])
            self._asking.pop(0)
            return await self._next_feed_circuit()
        field = vol.Required(CONF_CIRCUIT)
        if str(feed.get("circuit")) in choices:
            field = vol.Required(CONF_CIRCUIT, default=str(feed["circuit"]))
        return self.async_show_form(
            step_id="feed_circuit",
            data_schema=vol.Schema({field: _select(choices)}),
            description_placeholders={
                "panel": self._labels[key],
                "upstream": self._labels[feed["upstream"]],
            },
        )


class SpanEbusConfigFlow(_FeedSteps, ConfigFlow, domain=DOMAIN):
    """Handle the SPAN eBus config, discovery, reauth and reconfigure flows.

    A panel is added over its local API (host, then passphrase) with the cloud
    as an optional fallback, or the whole SPAN account is added over the cloud.
    """

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._reauth_entry_data: Mapping[str, Any] | None = None
        self._entry_data: dict[str, Any] = {}
        self._token = ""
        # The panel being added locally: where it is and its pinned CA, then,
        # once registered, the entry data for it.
        self._host = ""
        self._candidates: list[discovery.Candidate] = []
        self._ca_pem = ""
        self._serial = ""
        self._local: dict[str, Any] | None = None
        self._committed = False
        # Names registered whose outcome was never confirmed; removed with
        # the token of the registration that does succeed.
        self._unconfirmed: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SpanEbusOptionsFlow:
        return SpanEbusOptionsFlow()

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """Cloud accounts choose their panels; local panels add, tune or drop
        their cloud fallback."""
        return _is_cloud_account(config_entry.data) or CONF_LOCAL_SERIAL in config_entry.data

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["local", "cloud"])

    # --- local ------------------------------------------------------------------

    def _session(self) -> Any:
        return async_get_clientsession(self.hass)

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer the panels found on the network; ask for an address if there are none."""
        self._candidates = await self._discovered_panels()
        if not self._candidates:
            return await self.async_step_local_host()
        return await self.async_step_local_pick()

    async def async_step_local_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            choice = user_input[CONF_PICK]
            if choice == PICK_MANUAL:
                return await self.async_step_local_host()
            return await self.async_step_local_host({CONF_HOST: choice})
        options = [SelectOptionDict(value=c.host, label=c.label) for c in self._candidates]
        options.append(SelectOptionDict(value=PICK_MANUAL, label=PICK_MANUAL))
        schema = vol.Schema(
            {
                vol.Required(CONF_PICK, default=options[0]["value"]): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                        translation_key="local_pick",
                    )
                )
            }
        )
        return self.async_show_form(step_id="local_pick", data_schema=schema)

    async def async_step_local_host(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"retry_after": "60"}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                status, ca_pem = await onboard.probe_panel(self._session(), host)
            except NotFound:
                # Answers HTTPS but has no v2 API: older firmware.
                return await self.async_step_local_unsupported()
            except UnexpectedResponse:
                errors["base"] = "not_a_panel"
            except SpanLocalError as exc:
                errors["base"], extra = onboard.form_error(exc)
                placeholders.update(extra)
            else:
                if not onboard.firmware_supported(status.firmware):
                    return await self.async_step_local_unsupported()
                # A discovery flow for this panel may be waiting; this one, which
                # the user started, goes ahead, and finishing it ends the other.
                await self.async_set_unique_id(status.serial, raise_on_progress=False)
                # No host update here: only the pinned CA proves it is the same panel.
                self._abort_if_unique_id_configured()
                if self._cloud_runs_panel(status.serial):
                    return self.async_abort(reason="cloud_account_configured")
                self._host, self._ca_pem, self._serial = host, ca_pem, status.serial
                return await self.async_step_local_auth()
        return self.async_show_form(
            step_id="local_host",
            data_schema=self.add_suggested_values_to_schema(STEP_HOST_SCHEMA, user_input or {}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _discovered_panels(self) -> list[discovery.Candidate]:
        """Panels on the network that aren't set up yet, best effort.

        Two sources: discovery flows Home Assistant already started for
        `_ebus._tcp` (probed, so their serial is known), and a short browse
        for panels whose discovery was dismissed or never finished.
        """
        found: list[discovery.Candidate] = []
        for flow in self._async_in_progress(include_uninitialized=True):
            context = flow.get("context", {})
            host = context.get("title_placeholders", {}).get("host")
            if context.get("source") == "zeroconf" and host:
                found.append(discovery.Candidate(host, "", serial=context.get("unique_id")))
        try:
            found.extend(await self._browse())
        except Exception as err:  # noqa: BLE001 — the address form is the fallback
            _LOGGER.debug("mDNS browse for SPAN panels failed: %s", err)
        entries = self._async_current_entries(include_ignore=False)
        return discovery.offerable(
            found,
            configured_hosts=[e.data[CONF_HOST] for e in entries if CONF_HOST in e.data],
            configured_serials=[
                serial
                for e in entries
                for serial in (e.unique_id, e.data.get(CONF_LOCAL_SERIAL))
                if serial
            ],
        )

    async def _browse(self) -> list[discovery.Candidate]:
        """Ask Home Assistant's shared zeroconf for `_ebus._tcp` panels."""
        aiozc = await ha_zeroconf.async_get_async_instance(self.hass)
        names: set[str] = set()

        def on_change(
            zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
        ) -> None:
            if state_change is not ServiceStateChange.Removed:
                names.add(name)

        browser = AsyncServiceBrowser(
            aiozc.zeroconf, [discovery.SERVICE_TYPE], handlers=[on_change]
        )
        try:
            await asyncio.sleep(BROWSE_SECONDS)
        finally:
            await browser.async_cancel()

        async def resolve(name: str) -> list[discovery.Candidate]:
            info = AsyncServiceInfo(discovery.SERVICE_TYPE, name)
            if not await info.async_request(aiozc.zeroconf, RESOLVE_MS):
                return []
            label = discovery.instance_name(name)
            return [
                discovery.Candidate(address, label)
                for address in info.parsed_scoped_addresses(IPVersion.V4Only)
            ]

        # All at once, so several panels cost one resolve timeout, not several;
        # one that will not resolve is left out rather than spoiling the rest.
        results = await asyncio.gather(
            *(resolve(name) for name in sorted(names)), return_exceptions=True
        )
        found: list[discovery.Candidate] = []
        for result in results:
            if isinstance(result, BaseException):
                _LOGGER.debug("could not resolve a discovered panel: %s", result)
            else:
                found.extend(result)
        return found

    async def async_step_local_unsupported(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="local_unsupported", menu_options=["cloud", "local_host"]
        )

    def _cloud_runs_panel(self, serial: str) -> bool:
        """Whether a cloud account entry already runs this panel."""
        return any(
            _is_cloud_account(entry.data) and _account_has_panel(entry, serial)
            for entry in self._async_current_entries(include_ignore=False)
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        host = str(discovery_info.host)
        session = self._session()
        for entry in self._async_current_entries(include_ignore=False):
            if CONF_CA_PEM not in entry.data:
                continue
            if entry.data.get(CONF_HOST) == host:
                return self.async_abort(reason="already_configured")
            if await onboard.host_matches_ca(
                session, host, entry.data[CONF_CA_PEM], entry.data[CONF_LOCAL_SERIAL]
            ):
                # The panel moved, and its CA proves it: follow it.
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_HOST: host}
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="already_configured")
        try:
            status, ca_pem = await onboard.probe_panel(session, host)
        except NotFound:
            return self.async_abort(reason="not_supported")
        except SpanLocalError:
            return self.async_abort(reason="cannot_connect")
        if not onboard.firmware_supported(status.firmware):
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(status.serial)
        self._abort_if_unique_id_configured()
        if self._cloud_runs_panel(status.serial):
            return self.async_abort(reason="already_configured")
        self._host, self._ca_pem, self._serial = host, ca_pem, status.serial
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_local_auth()

    async def _register(self, passphrase: str) -> dict[str, Any]:
        """Register a client and prove its broker login; the local entry data.

        A registration that fails the broker test is removed again, and so is
        every earlier one whose outcome was never confirmed.
        """
        session = self._session()
        registration, name = await onboard.register_client(
            session,
            self._host,
            self._ca_pem,
            onboard.new_client_name(),
            passphrase,
            unconfirmed=self._unconfirmed,
        )
        token = registration.access_token
        stale, self._unconfirmed = self._unconfirmed, []
        for old in stale:
            await onboard.discard_client(session, self._host, self._ca_pem, token, old)
        try:
            if registration.serial != self._serial:
                raise UnexpectedResponse("registration is for another panel")
            await onboard.wait_until_ready(registration, self._host, self._ca_pem, name)
        except asyncio.CancelledError:
            # The flow is going away: don't await on a cancelled task.
            self.hass.async_create_background_task(
                onboard.discard_client(session, self._host, self._ca_pem, token, name),
                "span_ebus remove unused client",
            )
            raise
        except Exception:
            await onboard.discard_client(session, self._host, self._ca_pem, token, name)
            raise
        return {
            CONF_HOST: self._host,
            CONF_LOCAL_SERIAL: self._serial,
            CONF_CA_PEM: self._ca_pem,
            CONF_CLIENT_NAME: name,
            CONF_ACCESS_TOKEN: registration.access_token,
            CONF_BROKER_USERNAME: registration.broker_username,
            CONF_BROKER_PASSWORD: registration.broker_password,
            CONF_MQTTS_PORT: registration.mqtts_port,
        }

    async def _passphrase_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, ConfigFlowResult | None]:
        """Ask for the passphrase until a client registers.

        Returns the entry data once registered, else the form to show.
        """
        errors: dict[str, str] = {}
        placeholders = {"retry_after": "60", "host": self._host}
        if user_input is not None:
            try:
                return await self._register(user_input[CONF_PASSPHRASE]), None
            except SpanLocalError as exc:
                errors["base"], extra = onboard.form_error(exc)
                placeholders.update(extra)
            except Exception:
                _LOGGER.exception("unexpected error registering with a SPAN panel")
                errors["base"] = "unknown"
        placeholders["fingerprint"] = tls.ca_fingerprint(self._ca_pem)
        return None, self.async_show_form(
            step_id=step_id,
            data_schema=STEP_PASSPHRASE_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_local_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        data, form = await self._passphrase_step("local_auth", user_input)
        if data is None:
            assert form is not None  # noqa: S101 — one of the two is always set
            return form
        self._local = data
        return await self.async_step_cloud_fallback()

    async def async_step_cloud_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="cloud_fallback", menu_options=["cloud", "local_only"])

    async def async_step_local_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._local is None:
            return self.async_abort(reason="unknown")
        return self._create(self._local)

    def _create(
        self, data: dict[str, Any], options: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry. A local one is named for its host, never its serial."""
        self._committed = True
        title = f"SPAN Panel ({data[CONF_HOST]})" if CONF_HOST in data else data[CONF_EMAIL]
        if options is None:
            return self.async_create_entry(title=title, data=data)
        return self.async_create_entry(title=title, data=data, options=options)

    @callback
    def async_remove(self) -> None:
        """A flow left before its entry was made: unregister its client."""
        if self._local is None or self._committed:
            return
        local, self._local = self._local, None
        self.hass.async_create_background_task(
            onboard.discard_client(
                self._session(),
                local[CONF_HOST],
                local[CONF_CA_PEM],
                local[CONF_ACCESS_TOKEN],
                local[CONF_CLIENT_NAME],
            ),
            "span_ebus remove unused client",
        )

    async def _link_cloud(
        self, local: dict[str, Any], cloud: dict[str, Any], token: str
    ) -> ConfigFlowResult | str:
        """Make the local entry, with the cloud for this panel alone as its fallback.

        Returns a form error key when the account can't be read.
        """
        try:
            sites = await self.hass.async_add_executor_job(discover_sites, token)
        except Exception as err:  # noqa: BLE001 — shown as a form error
            _LOGGER.debug("could not list SPAN panels during setup: %s", err)
            return "cannot_connect"
        found = find_panel(sites, local[CONF_LOCAL_SERIAL])
        if found is None:
            return self.async_abort(reason="panel_not_on_account")
        site, panel = found
        serial = local[CONF_LOCAL_SERIAL]
        data = {**local, **cloud, CONF_SERIAL: serial}
        # Circuits are matched on the first cloud start, once the local tree
        # is in; until then the link carries only who is who.
        options = link_to_options(
            _panel_options(panel_labels(sites), [panel.key], {}),
            _new_link(panel.key, serial, site),
        )
        # Read by async_create_entry: a linked entry is made at 1.2.
        self.MINOR_VERSION = LINKED_MINOR_VERSION
        return self._create(data, options)

    # --- cloud ------------------------------------------------------------------

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
                # Before the slow probe: an account that can't be added here
                # should say so at once.
                reason = None if self._reauth_entry_data else self._account_conflict(user_id, email)
                if reason is not None:
                    return self.async_abort(reason=reason)
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
                    data = {
                        CONF_EMAIL: email,
                        CONF_TOKENS: dataclasses.asdict(tokens),
                        CONF_USER_ID: user_id,
                        CONF_DEVICE_UUID: device_uuid,
                        CONF_SERIAL: serial,
                    }

                    if self._reauth_entry_data is not None:
                        return await self._finish_cloud_reauth(data)

                    if self._local is not None:
                        linked = await self._link_cloud(self._local, data, tokens.access_token)
                        if not isinstance(linked, str):
                            return linked
                        errors["base"] = linked
                    else:
                        await self.async_set_unique_id(user_id or email.lower())
                        self._abort_if_unique_id_configured()
                        self._entry_data = data
                        self._token = tokens.access_token
                        self._labels = await self._discover_labels()
                        if len(self._labels) > 1:
                            return await self.async_step_panels()
                        return self._create(data)

        return self.async_show_form(
            step_id="cloud",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, self._suggested_values(user_input)
            ),
            errors=errors,
        )

    def _account_conflict(self, user_id: str | None, email: str) -> str | None:
        """Why this account can't be added in this flow, if it can't.

        Linking a local panel: the account must not already run as its own
        entry. Adding the account: none of its panels may run locally.
        """
        account = _account_key(user_id, email)
        for entry in self._async_current_entries(include_ignore=False):
            if _is_cloud_account(entry.data):
                if self._local is not None and entry.unique_id in {
                    account,
                    email.lower(),
                }:
                    return "cloud_account_configured"
            elif self._local is None and _same_account(entry.data, user_id, email):
                return "panel_configured_locally"
        return None

    async def _finish_cloud_reauth(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Reauth proves the login and changes nothing else."""
        entry = self._get_reauth_entry()
        account = _account_key(data[CONF_USER_ID], data[CONF_EMAIL])
        if _is_cloud_account(entry.data):
            await self.async_set_unique_id(account)
            self._abort_if_unique_id_mismatch(reason="wrong_account")
        else:
            # A local entry is keyed by its panel; the account is in its data.
            if not _same_account(entry.data, data[CONF_USER_ID], data[CONF_EMAIL]):
                return self.async_abort(reason="wrong_account")
        # The probe's serial is whichever panel came first, which on a
        # multi-panel account need not be the saved one.
        data = {**entry.data, **data, CONF_SERIAL: entry.data.get(CONF_SERIAL) or data[CONF_SERIAL]}
        return self.async_update_reload_and_abort(entry, data=data)

    def _access_token(self) -> str:
        return self._token

    def _identity(self) -> Mapping[str, Any]:
        return self._entry_data

    async def _discover_labels(self) -> dict[str, str]:
        """The account's panels. Failing to learn them doesn't block setup: nothing
        is recorded, and every panel runs until a start can read the account."""
        try:
            self._sites = await self.hass.async_add_executor_job(discover_sites, self._token)
        except Exception as err:  # noqa: BLE001 — discovery is optional here
            _LOGGER.debug("could not list SPAN panels during setup: %s", err)
            return {}
        return panel_labels(self._sites)

    async def async_step_panels(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        ticked = list(self._labels)
        if user_input is not None:
            ticked = user_input[CONF_PANELS]
            if ticked:
                self._ticked = ticked
                return await self._ask_feeds()
            errors["base"] = "no_panels"
        return self.async_show_form(
            step_id="panels", data_schema=_panels_schema(self._labels, ticked), errors=errors
        )

    async def _finish_feeds(self) -> ConfigFlowResult:
        return self._create(
            self._entry_data, _panel_options(self._labels, self._ticked, self._feeds)
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

    # --- reauth and reconfigure -------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._reauth_entry_data = entry_data
        # A local-only entry has only the panel to reauthenticate; a linked one
        # says which side failed.
        local_only = CONF_TOKENS not in entry_data and CONF_LOCAL_SERIAL in entry_data
        if local_only or entry_data.get(LOCAL_REAUTH):
            return await self.async_step_reauth_local()
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
        return await self.async_step_cloud(user_input)

    async def async_step_reauth_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Register a new client with the passphrase, then drop the old one."""
        entry = self._get_reauth_entry()
        self._host = entry.data[CONF_HOST]
        self._ca_pem = entry.data[CONF_CA_PEM]
        self._serial = entry.data[CONF_LOCAL_SERIAL]
        data, form = await self._passphrase_step("reauth_local", user_input)
        if data is None:
            assert form is not None  # noqa: S101 — one of the two is always set
            return form
        self._committed = True
        old_name = entry.data.get(CONF_CLIENT_NAME)
        if old_name and old_name != data[CONF_CLIENT_NAME]:
            await onboard.discard_client(
                self._session(),
                self._host,
                self._ca_pem,
                data[CONF_ACCESS_TOKEN],
                old_name,
            )
        return self.async_update_reload_and_abort(entry, data={**entry.data, **data})

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Point a local entry at a new address, if the panel there has its CA."""
        entry = self._get_reconfigure_entry()
        if CONF_CA_PEM not in entry.data:
            return self.async_abort(reason="not_local")
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            if await onboard.host_matches_ca(
                self._session(), host, entry.data[CONF_CA_PEM], entry.data[CONF_LOCAL_SERIAL]
            ):
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_HOST: host}
                )
            errors["base"] = "ca_mismatch"
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_HOST_SCHEMA, user_input or {CONF_HOST: entry.data[CONF_HOST]}
            ),
            errors=errors,
        )


class SpanEbusOptionsFlow(_FeedSteps, OptionsFlow):
    """Change which of the account's panels run here, and what feeds them."""

    def __init__(self) -> None:
        super().__init__()
        self._chosen: list[str] = []
        # "Add cloud fallback": what the sign-in found, kept for the confirm step.
        self._add: dict[str, Any] = {}

    def _token_path(self) -> Path:
        return Path(self.hass.config.path(TOKEN_DIR)) / f"{self.config_entry.entry_id}.json"

    def _access_token(self) -> str:
        return cloud_auth.access_token_from_store(self._token_path())

    def _identity(self) -> Mapping[str, Any]:
        return self.config_entry.data

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if _is_linked(self.config_entry.data):
            return await self.async_step_linked()
        if CONF_LOCAL_SERIAL in self.config_entry.data:
            return await self.async_step_cloud_add()
        errors: dict[str, str] = {}
        if user_input is None:
            try:
                sites = await self.hass.async_add_executor_job(
                    _sites_from_token_store, self._token_path()
                )
            except Exception as err:  # noqa: BLE001 — the form can't be built without them
                _LOGGER.debug("could not list SPAN panels: %s", err)
                return self.async_abort(reason="cannot_connect")
            options = self.config_entry.options
            plan = panel_plan(sites, options.get(CONF_PANELS), options.get(CONF_DECLINED, ()))
            self._sites = plan.sites
            self._labels = panel_labels(plan.sites)
            self._chosen = [key for key in plan.chosen if key in self._labels]
            self._ticked = self._chosen
            self._feeds = dict(options.get(CONF_FEEDS, {}))
        else:
            self._ticked = user_input[CONF_PANELS]
            if not self._ticked:
                errors["base"] = "no_panels"
            elif set(self._chosen) - set(self._ticked):
                return await self.async_step_confirm_remove()
            else:
                return await self._ask_feeds()
        return self.async_show_form(
            step_id="init", data_schema=_panels_schema(self._labels, self._ticked), errors=errors
        )

    async def async_step_transport(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Whether a linked panel may fall back to the cloud."""
        entry = self.config_entry
        current = entry.options.get(CONF_TRANSPORT_MODE, Mode.LOCAL_FIRST)
        if user_input is not None:
            options = {**entry.options, CONF_TRANSPORT_MODE: user_input[CONF_TRANSPORT_MODE]}
            # Reloading drops the broker link; only a real change is worth that.
            if user_input[CONF_TRANSPORT_MODE] != str(current):
                self.hass.config_entries.async_update_entry(entry, options=options)
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(data=options)
        schema = vol.Schema(
            {
                vol.Required(CONF_TRANSPORT_MODE, default=str(current)): SelectSelector(
                    SelectSelectorConfig(
                        options=[mode.value for mode in Mode],
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_TRANSPORT_MODE,
                    )
                )
            }
        )
        return self.async_show_form(step_id="transport", data_schema=schema)

    async def async_step_linked(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="linked", menu_options=["transport", "cloud_remove"])

    # --- a local panel's cloud fallback -------------------------------------

    def _local_circuits(self) -> dict[str, Any]:
        runtime = getattr(self.config_entry, "runtime_data", None)
        transport = runtime.transport if runtime is not None else None
        return dict(transport.snapshot.circuits) if transport is not None else {}

    async def async_step_cloud_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in to the SPAN account this panel is on, to use it as a fallback."""
        errors: dict[str, str] = {}
        if user_input is not None:
            serial = self.config_entry.data[CONF_LOCAL_SERIAL]
            try:
                tokens = await self.hass.async_add_executor_job(
                    cloud_auth.authenticate, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except cloud_auth.CloudAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("unexpected error authenticating to SPAN cloud")
                errors["base"] = "cannot_connect"
            else:
                # The password is discarded here; only the tokens are kept.
                result = await self._cloud_add_found(user_input[CONF_EMAIL], tokens, serial)
                if isinstance(result, str):
                    errors["base"] = result
                else:
                    return result
        return self.async_show_form(
            step_id="cloud_add",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {CONF_EMAIL: user_input[CONF_EMAIL]} if user_input else {},
            ),
            errors=errors,
        )

    async def _cloud_add_found(
        self, email: str, tokens: cloud_auth.CloudTokens, serial: str
    ) -> ConfigFlowResult | str:
        """Find the panel on the account and match its circuits; a form error key
        if the account can't be read.
        """
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if _is_cloud_account(entry.data) and _account_has_panel(entry, serial):
                return self.async_abort(reason="panel_on_cloud_entry")
        try:
            sites = await self.hass.async_add_executor_job(discover_sites, tokens.access_token)
        except Exception as err:  # noqa: BLE001 — shown as a form error
            _LOGGER.debug("could not list SPAN panels: %s", type(err).__name__)
            return "cannot_connect"
        found = find_panel(sites, serial)
        if found is None:
            return self.async_abort(reason="panel_not_on_account")
        site, panel = found
        user_id = _user_id_from_access_token(tokens.access_token)
        device_uuid = _new_device_uuid()
        link = _new_link(panel.key, serial, site)
        try:
            circuits, feeders = await self.hass.async_add_executor_job(
                lambda: panel_traits(
                    tokens.access_token,
                    site,
                    panel,
                    user_id=user_id,
                    device_uuid=device_uuid,
                    primary_panel=None,
                )
            )
        except Exception as err:  # noqa: BLE001 — matched on the first cloud start instead
            _LOGGER.debug("could not read the panel's circuits: %s", type(err).__name__)
        else:
            matched, unmatched_local, unmatched_cloud = match_circuits(
                self._local_circuits(), circuits.values()
            )
            link = dataclasses.replace(
                link,
                circuits=matched,
                feeds=feeds_from_kinds(feeders),
                unmatched_local=unmatched_local,
                unmatched_cloud=unmatched_cloud,
            )
        self._add = {
            "data": {
                CONF_EMAIL: email,
                CONF_TOKENS: dataclasses.asdict(tokens),
                CONF_USER_ID: user_id,
                CONF_DEVICE_UUID: device_uuid,
                CONF_SERIAL: serial,
            },
            "link": link,
            "labels": panel_labels(sites),
        }
        return await self.async_step_cloud_add_confirm()

    async def async_step_cloud_add_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        link: PanelLink = self._add["link"]
        if user_input is None:
            total = len(self._local_circuits())
            return self.async_show_form(
                step_id="cloud_add_confirm",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "matched": str(len(link.circuits)),
                    "total": str(total),
                    "unmatched": str(max(total - len(link.circuits), 0)),
                },
            )
        entry = self.config_entry
        data = {**entry.data, **self._add["data"]}
        options = {
            **link_to_options(entry.options, link),
            **_panel_options(self._add["labels"], [link.cloud_key], {}),
            CONF_TRANSPORT_MODE: Mode.LOCAL_FIRST.value,
        }
        self.hass.config_entries.async_update_entry(
            entry, data=data, options=options, minor_version=LINKED_MINOR_VERSION
        )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data=options)

    async def async_step_cloud_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Drop the account: the panel runs over its local API alone."""
        entry = self.config_entry
        if not _is_linked(entry.data):
            # TODO(5c): a cloud entry linked to a local one keeps its link and
            # stays at 1.2; removing that link is not this step's to do.
            return self.async_abort(reason="cloud_remove_unsupported")
        if user_input is None:
            return self.async_show_form(step_id="cloud_remove", data_schema=vol.Schema({}))
        data, options = unlinked_entry(
            entry.data,
            entry.options,
            data_keys=CLOUD_DATA_KEYS,
            option_keys=CLOUD_OPTION_KEYS,
        )
        options[CONF_TRANSPORT_MODE] = Mode.LOCAL_ONLY.value
        self.hass.config_entries.async_update_entry(
            entry, data=data, options=options, minor_version=UNLINKED_MINOR_VERSION
        )
        # Stop the cloud before its token file goes, so no refresh writes it
        # back; the unload after the reload removes it again for good measure.
        runtime = getattr(entry, "runtime_data", None)
        transport = runtime.transport if runtime is not None else None
        if transport is not None:
            await transport.async_detach_cloud()
        await self.hass.async_add_executor_job(cloud_auth.discard_tokens, self._token_path())
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data=options)

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
        return await self._ask_feeds()

    async def _finish_feeds(self) -> ConfigFlowResult:
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
        options = {**entry.options, **_panel_options(self._labels, self._ticked, self._feeds)}
        self.hass.config_entries.async_update_entry(entry, options=options)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data=options)
