"""Rotating a local panel's passphrase and broker password.

Free of Home Assistant: the service handler in `services` injects every side
effect (the REST call, storing the password, the broker probe, the reload, and
sleeping), so the whole flow is tested with fakes.

The new hop passphrase is a secret. It is returned to the caller once, in
`RotationResult`, and never logged, stored, or put in an exception message.
Log lines name the config entry id only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .rest import (
    AuthFailed,
    CannotConnect,
    NotSent,
    OutcomeUnknown,
    RateLimited,
    RotatedPassphrase,
    SpanLocalError,
    UnexpectedResponse,
)

log = logging.getLogger(__name__)

# Seconds to wait before each further probe: about a minute in all.
RECONNECT_DELAYS: tuple[float, ...] = (1, 2, 4, 8, 15, 30)


class Problem(StrEnum):
    """Why a rotation was refused or failed. Each is an exception translation key."""

    # Which entry: nothing was sent.
    NO_LOCAL_PANEL = "no_local_panel"
    MULTIPLE_PANELS = "multiple_panels"
    TARGET_MISMATCH = "target_mismatch"
    NOT_LOCAL = "not_local"
    NOT_LOADED = "not_loaded"
    # Preconditions: nothing was sent.
    NO_TOKEN = "no_token"
    NO_CA = "no_ca"
    # The PUT.
    AUTH_FAILED = "auth_failed"  # nothing changed
    RATE_LIMITED = "rate_limited"  # nothing changed
    CANNOT_CONNECT = "cannot_connect"  # never sent: nothing changed
    FAILED = "failed"  # refused: nothing changed
    OUTCOME_UNKNOWN = "outcome_unknown"  # may have rotated

    @property
    def is_refusal(self) -> bool:
        """True when the call itself was wrong, not the panel's answer."""
        return self in _REFUSALS


_REFUSALS = frozenset(
    {
        Problem.NO_LOCAL_PANEL,
        Problem.MULTIPLE_PANELS,
        Problem.TARGET_MISMATCH,
        Problem.NOT_LOCAL,
        Problem.NOT_LOADED,
        Problem.NO_TOKEN,
        Problem.NO_CA,
    }
)


class RotationError(Exception):
    """A rotation that did not return a passphrase. The message is the key only."""

    def __init__(self, problem: Problem, retry_after: int | None = None) -> None:
        super().__init__(str(problem))
        self.problem = problem
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class RotationResult:
    hop_passphrase: str
    reconnected: bool
    stored: bool

    def as_response(self) -> dict[str, Any]:
        return {
            "hop_passphrase": self.hop_passphrase,
            "reconnected": self.reconnected,
            "stored": self.stored,
        }

    def __repr__(self) -> str:
        return (
            f"RotationResult(hop_passphrase=<redacted>, reconnected={self.reconnected}, "
            f"stored={self.stored})"
        )

    __str__ = __repr__


# --- who may call it -------------------------------------------------------------------


class _User(Protocol):
    is_admin: bool


async def is_admin(user_id: str | None, get_user: Callable[[str], Awaitable[_User | None]]) -> bool:
    """Only a known admin user may rotate: never an automation or a call without a user."""
    if user_id is None:
        return False
    user = await get_user(user_id)
    return user is not None and user.is_admin


# --- which entry -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One of this domain's entries (not ignored, not disabled)."""

    local: bool  # added over the local API
    loaded: bool


def pick_target(
    entries: Mapping[str, Candidate],
    entry_id: str | None,
    device_entry_ids: set[str] | frozenset[str] | None,
) -> str | Problem:
    """The entry to rotate, or why there is none.

    `device_entry_ids` is None when no device was given, else the config
    entries the device belongs to (empty for an unknown device).
    """
    if device_entry_ids is not None:
        if entry_id is not None:
            if entry_id not in device_entry_ids:
                return Problem.TARGET_MISMATCH
        else:
            known = sorted(e for e in device_entry_ids if e in entries)
            if not known:
                return Problem.NO_LOCAL_PANEL
            local = [e for e in known if entries[e].local]
            if not local:
                return Problem.NOT_LOCAL
            if len(local) > 1:
                return Problem.MULTIPLE_PANELS
            entry_id = local[0]
    if entry_id is None:
        local = [e for e, c in entries.items() if c.local]
        if not local:
            return Problem.NO_LOCAL_PANEL
        if len(local) > 1:
            return Problem.MULTIPLE_PANELS
        entry_id = local[0]
    candidate = entries.get(entry_id)
    if candidate is None:
        return Problem.NO_LOCAL_PANEL
    if not candidate.local:
        return Problem.NOT_LOCAL
    if not candidate.loaded:
        return Problem.NOT_LOADED
    return entry_id


# --- one at a time, and no reauth meanwhile --------------------------------------------


def lock_for(locks: dict[str, asyncio.Lock], entry_id: str) -> asyncio.Lock:
    """The entry's rotation lock: a second call waits for the first to finish."""
    return locks.setdefault(entry_id, asyncio.Lock())


class _Flagged(Protocol):
    rotating: bool


@contextlib.contextmanager
def rotating(transport: _Flagged) -> Iterator[None]:
    """Mark a rotation in progress for its whole length, cleared on any exit.

    While it is set, a refused broker session does not open a reauth that asks
    for a passphrase the user has not seen yet.
    """
    transport.rotating = True
    try:
        yield
    finally:
        transport.rotating = False


def reauth_due(auth_failed: bool, rotating: bool) -> bool:
    """Whether the transport should ask for a local reauth now."""
    return auth_failed and not rotating


# --- the flow --------------------------------------------------------------------------


class RotationApi(Protocol):
    async def rotate_passphrase(self) -> RotatedPassphrase: ...


async def rotate(
    api: RotationApi,
    *,
    entry_id: str,
    has_token: bool,
    has_ca: bool,
    store: Callable[[str], Awaitable[None]],
    probe: Callable[[str], Awaitable[None]],
    reload: Callable[[], Awaitable[bool | None]],
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    delays: tuple[float, ...] = RECONNECT_DELAYS,
) -> RotationResult:
    """Rotate, store the new broker password, check the broker takes it, reload.

    Raises RotationError when no passphrase came back. Once one has, it is
    always returned: a failure after that is reported in the result.
    """
    if not has_token:
        raise RotationError(Problem.NO_TOKEN)
    if not has_ca:
        raise RotationError(Problem.NO_CA)

    rotated = await _put(api)

    try:
        await store(rotated.broker_password)
    except Exception as exc:  # noqa: BLE001 — the passphrase must still be returned
        log.error(
            "rotated the passphrase of entry %s but could not store the new broker "
            "password (%s); the passphrase is in the response: run reauth with it",
            entry_id,
            type(exc).__name__,
        )
        return RotationResult(rotated.hop_passphrase, reconnected=False, stored=False)

    reconnected = await _probe(probe, rotated.broker_password, sleep, delays, entry_id)
    if reconnected:
        try:
            if await reload() is False:
                reconnected = False
                log.debug("reload of entry %s did not set it up", entry_id)
        except Exception as exc:  # noqa: BLE001 — stored, so a later reload picks it up
            reconnected = False
            log.debug("reload of entry %s failed: %s", entry_id, type(exc).__name__)

    if reconnected:
        log.info("rotated the passphrase of entry %s and reconnected", entry_id)
    else:
        log.error(
            "rotated the passphrase of entry %s but could not reconnect with the new "
            "broker password; the passphrase is in the response: rotate again, or run "
            "reauth with it",
            entry_id,
        )
    return RotationResult(rotated.hop_passphrase, reconnected=reconnected, stored=True)


async def _put(api: RotationApi) -> RotatedPassphrase:
    """The PUT, its errors mapped. Nothing broad is caught here."""
    try:
        return await api.rotate_passphrase()
    except AuthFailed:
        raise RotationError(Problem.AUTH_FAILED) from None
    except RateLimited as exc:
        raise RotationError(Problem.RATE_LIMITED, exc.retry_after) from None
    except OutcomeUnknown:
        raise RotationError(Problem.OUTCOME_UNKNOWN) from None
    except NotSent:
        raise RotationError(Problem.CANNOT_CONNECT) from None
    except CannotConnect:
        # rotate_passphrase raises NotSent for anything that never left.
        raise RotationError(Problem.OUTCOME_UNKNOWN) from None
    except UnexpectedResponse:
        raise RotationError(Problem.FAILED) from None
    except SpanLocalError:
        # The only bare one is raised before sending, without a pinned CA.
        raise RotationError(Problem.NO_CA) from None


async def _probe(
    probe: Callable[[str], Awaitable[None]],
    password: str,
    sleep: Callable[[float], Awaitable[Any]],
    delays: tuple[float, ...],
    entry_id: str,
) -> bool:
    """Log in with the new password until the broker accepts it, or the delays run out."""
    pending = list(delays)
    while True:
        try:
            await probe(password)
        except (AuthFailed, CannotConnect) as exc:
            # The broker can refuse briefly while the change propagates.
            log.debug("probe for entry %s: %s", entry_id, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 — the passphrase must still be returned
            log.debug("probe for entry %s failed: %s", entry_id, type(exc).__name__)
            return False
        else:
            return True
        if not pending:
            return False
        await sleep(pending.pop(0))
