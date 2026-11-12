"""Async client for the panel's v2 REST API.

Everything goes over HTTPS on the panel's own port 443. The CA is fetched
first, without verification, and pinned; every later request is verified
against it (see `tls`). The caller owns the aiohttp session, which in Home
Assistant is the shared one from `async_get_clientsession`.

Errors come back as the typed exceptions below, so the config flow can show a
specific message without reading status codes. No secret (passphrase, token,
broker password) is ever put in an exception message or a log line.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from yarl import URL

from . import tls

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
API = "/api/v2"


class SpanLocalError(Exception):
    """Base for every local REST failure."""


class CannotConnect(SpanLocalError):
    """The panel did not answer: wrong host, offline, or a TLS failure."""


class RateLimited(SpanLocalError):
    """HTTP 429. `retry_after` is in seconds when the panel said."""

    def __init__(self, retry_after: int | None) -> None:
        super().__init__(f"rate limited; retry after {retry_after or 'a while'} s")
        self.retry_after = retry_after


class RegistrationRejected(SpanLocalError):
    """HTTP 422 on register: the passphrase was wrong or the request invalid."""


class PanelNotReady(SpanLocalError):
    """HTTP 503: the panel is not ready to register clients yet."""


class AuthFailed(SpanLocalError):
    """HTTP 401 or 403: the access token was refused."""


class OutcomeUnknown(SpanLocalError):
    """The request may or may not have taken effect (504, or a timeout).

    Used for passphrase rotation, where a lost answer means the user has to
    check the SPAN app for the new passphrase.
    """


class UnexpectedResponse(SpanLocalError):
    """Any other status, or a body that does not match the API."""


@dataclass(frozen=True)
class PanelStatus:
    """`GET /api/v2/status`."""

    serial: str
    firmware: str
    hardware: str
    proximity_proven: bool


@dataclass(frozen=True)
class Registration:
    """What `POST /api/v2/auth/register` returns.

    `hop_passphrase` is the panel's current passphrase. It is returned here so
    a caller can show it once, and must never be stored or logged.
    `broker_password` can be None; the API documents it as nullable.
    """

    access_token: str
    serial: str
    hostname: str
    broker_username: str
    broker_password: str | None
    broker_host: str
    mqtts_port: int
    hop_passphrase: str | None

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> Registration:
        try:
            return cls(
                access_token=str(body["accessToken"]),
                serial=str(body["serialNumber"]),
                hostname=str(body["hostname"]),
                broker_username=str(body["ebusBrokerUsername"]),
                broker_password=_opt_str(body.get("ebusBrokerPassword")),
                broker_host=str(body["ebusBrokerHost"]),
                mqtts_port=int(body["ebusBrokerMqttsPort"]),
                hop_passphrase=_opt_str(body.get("hopPassphrase")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UnexpectedResponse(f"register response missing {exc}") from None

    def __repr__(self) -> str:
        # The dataclass repr would print every secret.
        return f"Registration(broker_host={self.broker_host!r}, mqtts_port={self.mqtts_port})"


@dataclass(frozen=True)
class RotatedPassphrase:
    """`PUT /api/v2/auth/passphrase`. Never store or log either field."""

    broker_password: str
    hop_passphrase: str

    def __repr__(self) -> str:
        return "RotatedPassphrase(<redacted>)"


@dataclass(frozen=True)
class HomieSchema:
    """`GET /api/v2/homie/schema`: every device class the firmware can publish.

    `schema_hash` changes whenever `device_classes` does, so it is the cache
    key: a stored schema with the same hash needs no refetch.
    """

    firmware: str
    homie_domain: str
    homie_version: int
    data_model_version: str
    device_classes: dict[str, dict[str, dict[str, Any]]]
    schema_hash: str

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> HomieSchema:
        try:
            classes = body["deviceClasses"]
            if not isinstance(classes, dict):
                raise TypeError("deviceClasses")
            return cls(
                firmware=str(body["firmwareVersion"]),
                homie_domain=str(body["homieDomain"]),
                homie_version=int(body["homieVersion"]),
                data_model_version=str(body["dataModelVersion"]),
                device_classes=classes,
                schema_hash=str(body["deviceClassesSchemaHash"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UnexpectedResponse(f"homie schema missing {exc}") from None


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


class SpanLocalApi:
    """One panel's v2 REST API.

    `ca_pem` is the pinned CA. Without it, only `fetch_ca` may be called;
    every other method raises, so nothing is ever sent unverified apart from
    the CA request itself.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        *,
        ca_pem: str | None = None,
        access_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        port: int = 443,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._token = access_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._ssl = tls.pinned_context(ca_pem) if ca_pem else None

    @property
    def host(self) -> str:
        return self._host

    def set_access_token(self, token: str | None) -> None:
        self._token = token

    def _url(self, path: str) -> URL:
        # URL.build brackets an IPv6 literal, which an f-string would not.
        port = None if self._port == 443 else self._port
        return URL.build(scheme="https", host=self._host, port=port, path=f"{API}{path}")

    # --- unauthenticated --------------------------------------------------

    async def fetch_ca(self) -> str:
        """Download the panel CA as PEM, trusted on first use.

        Raises CannotConnect, RateLimited (10 per minute per IP), or
        UnexpectedResponse when the body is not a certificate.
        """
        text = await self._request(
            "GET", "/certificate/ca", ssl=tls.unverified_context(), auth=False, as_text=True
        )
        try:
            return tls.first_certificate(text)
        except ValueError:
            raise UnexpectedResponse("CA endpoint did not return a certificate") from None

    async def status(self) -> PanelStatus:
        body = await self._request("GET", "/status", auth=False)
        try:
            return PanelStatus(
                serial=str(body["serialNumber"]),
                firmware=str(body["firmwareVersion"]),
                hardware=str(body["hardwareVersion"]),
                proximity_proven=bool(body["proximityProven"]),
            )
        except (KeyError, TypeError) as exc:
            raise UnexpectedResponse(f"status missing {exc}") from None

    async def homie_schema(self) -> HomieSchema:
        return HomieSchema.from_body(await self._request("GET", "/homie/schema", auth=False))

    async def register(
        self, name: str, *, passphrase: str | None = None, description: str | None = None
    ) -> Registration:
        """Register this client and get MQTT credentials.

        `passphrase` is the panel's "hop" passphrase. Without one, the panel
        only accepts the request if proximity has been proven (door opened
        and closed recently). The new access token is kept for later calls.
        """
        payload: dict[str, str] = {"name": name}
        if description:
            payload["description"] = description
        if passphrase:
            payload["hopPassphrase"] = passphrase
        body = await self._request("POST", "/auth/register", json=payload, auth=False)
        registration = Registration.from_body(body)
        self._token = registration.access_token
        return registration

    # --- authenticated ----------------------------------------------------

    async def list_clients(self) -> dict[str, dict[str, Any]]:
        body = await self._request("GET", "/auth/clients")
        clients = body.get("clients") if isinstance(body, dict) else None
        if not isinstance(clients, dict):
            raise UnexpectedResponse("clients list missing")
        return clients

    async def delete_client(self, name: str) -> None:
        if "/" in name or name in {"", ".", ".."}:
            # These would address a different path. Anything else, `?`, `#`
            # and `%` included, is escaped by yarl as literal text.
            raise ValueError("not a deletable client name")
        await self._request("DELETE", f"/auth/clients/{name}")

    async def rotate_passphrase(self) -> RotatedPassphrase:
        """Regenerate the passphrase and broker password.

        A timeout or 504 raises OutcomeUnknown: the rotation may have happened,
        so the caller must not retry blindly. The old broker password stays
        valid for about 30 s.
        """
        try:
            body = await self._request("PUT", "/auth/passphrase")
        except (TimeoutError, CannotConnect):
            raise OutcomeUnknown("no answer to passphrase rotation") from None
        try:
            return RotatedPassphrase(
                broker_password=str(body["ebusBrokerPassword"]),
                hop_passphrase=str(body["hopPassphrase"]),
            )
        except (KeyError, TypeError):
            raise UnexpectedResponse("rotation response missing fields") from None

    async def get_fqdn(self) -> str | None:
        body = await self._request("GET", "/dns/fqdn")
        return _opt_str(body.get("ebusTlsFqdn")) or None

    async def set_fqdn(self, fqdn: str) -> None:
        await self._request("POST", "/dns/fqdn", json={"ebusTlsFqdn": fqdn})

    async def delete_fqdn(self) -> None:
        await self._request("DELETE", "/dns/fqdn")

    # --- transport --------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        auth: bool = True,
        ssl: Any = None,
        as_text: bool = False,
    ) -> Any:
        context = ssl or self._ssl
        if context is None:
            raise SpanLocalError("no pinned CA; call fetch_ca first")
        headers = {}
        if auth:
            # Only on authenticated calls: never on the unpinned CA fetch.
            if not self._token:
                raise AuthFailed("not registered")
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            async with self._session.request(
                method,
                self._url(path),
                json=json,
                headers=headers,
                ssl=context,
                timeout=self._timeout,
            ) as resp:
                if resp.status >= 400:
                    await _raise_for(resp, method, path)
                if as_text:
                    try:
                        return await resp.text()
                    except UnicodeDecodeError:
                        raise UnexpectedResponse(f"{method} {path}: body is not text") from None
                if resp.content_length == 0:
                    return {}
                try:
                    return await resp.json(content_type=None)
                except ValueError:
                    raise UnexpectedResponse(f"{method} {path}: body is not JSON") from None
        except TimeoutError:
            if method == "PUT" and path == "/auth/passphrase":
                raise
            raise CannotConnect(f"{method} {path}: timed out") from None
        except aiohttp.ClientError as exc:
            # Includes TLS verification failures. The message names the error
            # class only: some aiohttp messages embed the host.
            # No chained cause either, since HA logs tracebacks.
            raise CannotConnect(f"{method} {path}: {type(exc).__name__}") from None


async def _raise_for(resp: aiohttp.ClientResponse, method: str, path: str) -> None:
    status = resp.status
    detail = await _detail(resp)
    log.debug("%s %s -> %s %s", method, path, status, detail)
    if status == 429:
        raise RateLimited(_retry_after(resp))
    if status in (401, 403):
        raise AuthFailed(f"{method} {path}: {status}")
    if status == 422 and path == "/auth/register":
        raise RegistrationRejected(detail or "rejected")
    if status == 503 and path == "/auth/register":
        raise PanelNotReady(detail or "not ready")
    if status == 504:
        raise OutcomeUnknown(f"{method} {path}: gateway timeout")
    raise UnexpectedResponse(f"{method} {path}: HTTP {status}")


async def _detail(resp: aiohttp.ClientResponse) -> str:
    """The API's `{"detail": ...}` message, if the body has one."""
    try:
        body = await asyncio.wait_for(resp.json(content_type=None), timeout=2)
    except (ValueError, TimeoutError, aiohttp.ClientError):
        return ""
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"][:200]
    return ""


def _retry_after(resp: aiohttp.ClientResponse) -> int | None:
    value = resp.headers.get("Retry-After")
    if value and value.strip().isdigit():
        return int(value.strip())
    return None
