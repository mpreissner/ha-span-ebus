# rotate_credentials service (Tier 2)

Status: draft rev 1, 2026-11-14. Local only: never commit or push.
Source: local-transport.md, Tier 2 ("admin only, returns the new passphrase in the response, retries the
broker for about 60 s").

## 1. Goal

Give an admin a way to change a local panel's passphrase and broker password from Home Assistant, for
example after the passphrase has been shared or written down somewhere. The new passphrase appears exactly
once, in the service response. Home Assistant keeps only the new broker password, which it needs to stay
connected.

Out of scope:
- cloud entries;
- rotating the access token (the panel has no endpoint for it);
- any UI button. The service is called from Developer Tools > Actions.

## 2. Upstream reference

Pinned commits:
- SpanPanel/span @ f1d754a60af88c8e2ffef89bfe4b9dc58cef925e (`custom_components/span_panel/services.py`,
  `services.yaml`)
- span-panel-api @ 3bae3ced45fc64d0058f2ca2a60513863588c617 (`src/span_panel_api/auth.py`)

| What | Where |
|---|---|
| Reconnect delays (1, 2, 4, 8, 15, 30 s) | services.py L57-60 |
| In-progress set; setup reports a refused broker as not-ready, and the options listener skips its reload | services.py L62-64, L71-78 |
| Per-entry lock | services.py L66-68 |
| Outcome-unknown error, with a passphrase-only variant | services.py L81-107 |
| "Did the request leave?": TLS verification, ConnectError, ConnectTimeout and PoolTimeout mean no | services.py L110-123 |
| Admin check: no user_id is refused, and so is a non-admin | services.py L940-966 |
| Entry choice: none → refuse, several and no id → refuse | services.py L972-1012 |
| Rotation: token required, pinned CA only, status mapping, store broker password only, reload loop, failure returned not raised | services.py L1014-1150 |
| Handler: lock, then look the entry up again inside it; `SupportsResponse.ONLY` | services.py L1152-1173 |
| Field `config_entry_id` with a config_entry selector | services.yaml L320-325 |
| `rotate_passphrase` / `regenerate_passphrase` / `_put_passphrase` | auth.py L603-658, L661-697, L699-739 |

Upstream behaviour we keep:
- `PUT /api/v2/auth/passphrase` with the bearer token returns `{ebusBrokerPassword, hopPassphrase}`. Today
  both carry the same value.
- Existing access tokens aren't revoked, so the stored token keeps working.
- Only the broker password is stored. The hop passphrase is never stored.
- After the rotation, reconnect with the new password only. Never fall back to the old one, which the panel
  honours for about 30 s at most.
- If reconnecting fails, return `reconnected: false` instead of raising. Raising would throw away the only
  copy of the passphrase.

## 3. What we already have (dev)

- `span_client/local/rest.py:275` `SpanLocalApi.rotate_passphrase()` already exists and is tested
  (`tests/test_local_rest.py:284`).
  - A timeout, a `CannotConnect` or a 504 raises `OutcomeUnknown`.
  - Missing fields raise `UnexpectedResponse`.
  - `RotatedPassphrase.__repr__` is redacted (`rest.py:131`).
- `entry.data` holds:
  - `CONF_ACCESS_TOKEN`, `CONF_CA_PEM` and `CONF_BROKER_USERNAME`;
  - `CONF_BROKER_PASSWORD` (`const.py:26-29`).

  The passphrase is never stored; strings.json already promises that.
- There is no update listener and no registered service. A data change doesn't reload the entry.
- When the broker refuses: `LocalMqtt._supervise` (`mqtt.py:150`) sets `auth_failed` and stops. The
  transport tick (`transport.py:251-262`) then starts a local reauth every tick
  (`data={..., LOCAL_REAUTH: True}`).
- The local reauth flow asks for the hop passphrase and registers again. So the returned passphrase is
  always enough to recover by hand.

## 4. Design

### 4.1 Service registration

- Registered once, in a new `async_setup(hass, config)` in `__init__.py`. Registering there rather than in
  setup_entry means calls to a target that isn't loaded get a clear error instead of "unknown action".
- Name: `span_ebus.rotate_credentials`, with `supports_response=SupportsResponse.ONLY`.
- Schema:
  ```
  vol.Schema({vol.Optional("config_entry_id"): str, vol.Optional("device_id"): str})
  ```
- Add a `services.yaml` entry:
  - `config_entry_id` uses a `config_entry` selector (`integration: span_ebus`);
  - `device_id` uses a `device` selector (`integration: span_ebus`).
- Add a `services.rotate_credentials` block in strings.json and translations/en.json. It has `name`,
  `description` and per-field `name`/`description`. These are service descriptions, not entity
  descriptions, so the "no entity description keys" rule doesn't apply (**D7**).

### 4.2 Admin only

This is a copy of upstream L940-966:
- If `call.context.user_id` is None, raise `Unauthorized`. Automations and anything else without a user
  are refused.
- Look the user up with `hass.auth.async_get_user`. If there's no user, or it isn't an admin, raise
  `Unauthorized(context=call.context, permission=POLICY_CONTROL)`.
- Don't use `verify_domain_control`: it only checks entity permissions, and a non-admin may hold those.
- Refusing calls without a user also keeps the passphrase out of automation traces.
- A script that an admin runs still carries their user_id, so its trace would hold the response. §6 covers
  this.

### 4.3 Targeting

A pure function in `span_client/local/rotation.py`:
```
pick_target(candidates, entry_id, device_entry_ids) -> str | Refusal
```

- Candidates are this domain's entries that have `CONF_LOCAL_SERIAL`, excluding ignored and disabled
  entries. Unlike upstream, they must also be loaded: we need the running transport to swap credentials.
- `device_id` resolves through the device registry to `device.config_entries ∩ candidates`. Any of the
  entry's devices is accepted: the panel, a circuit or a subdevice.
- The rules:

  | Input | Result |
  |---|---|
  | Neither id given, exactly one candidate | That candidate |
  | Neither id given, none or several candidates | Refuse: `no_local_panel` or `multiple_panels` |
  | Both ids given and they disagree | Refuse: `target_mismatch` |
  | `config_entry_id` names a cloud-only entry | Refuse: `not_local` |
  | `config_entry_id` names an entry that isn't loaded | Refuse: `not_loaded` |
  | Unknown id | Refuse: `no_local_panel` |

- Every refusal is a `ServiceValidationError` with `translation_domain=DOMAIN`. Placeholders never carry a
  host, serial or title. Mentioning the entry_id is fine.

### 4.4 Concurrency and the in-progress flag

- `hass.data[DOMAIN]["rotation_locks"][entry_id]` holds an `asyncio.Lock`. The handler takes it, then picks
  the target again inside it, as upstream does at L1166-1171.
- `SpanTransportManager.rotating: bool` is set for the whole rotation. While it's set, `_evaluate` doesn't
  call `async_start_reauth`.
  - The current broker session may be refused once the old password expires, and that refusal mustn't
    open a reauth that asks the user for a passphrase they haven't seen yet.
  - The flag is cleared in a `finally`.
  - If the swap succeeds, `auth_failed` is reset with it.

### 4.5 The flow

This lives in `rotation.py`, free of HA, with every side effect injected. It is
`async def rotate(api, store, probe, swap, sleep, delays=RECONNECT_DELAYS) -> RotationResult`:

1. **Preconditions**
   - No access token or no CA: refuse with `no_token` or `no_ca` before any request.
   - Pinned CA only. There's no fallback to an unpinned context, and `SpanLocalApi` already refuses one.
2. **The PUT:** `api.rotate_passphrase()`. Map the errors as follows; nothing is stored in any of these
   cases.

   | Error | Raised as | Meaning |
   |---|---|---|
   | `AuthFailed` (401/403) | `auth_failed` | The access token was refused. Run reauth; nothing changed. |
   | `RateLimited` | `rate_limited`, with retry-after seconds | |
   | `OutcomeUnknown` | `outcome_unknown` | The passphrase may have changed. If it did, the new one is in the SPAN app; enter it in the reauth that follows. |
   | `CannotConnect` where the request never left | `cannot_connect` | Nothing changed. |
   | `UnexpectedResponse` on a 2xx (body unreadable) | `outcome_unknown` | |
   | `UnexpectedResponse` on 5xx other than 503 | `outcome_unknown` | |
   | `UnexpectedResponse` on 503 | `failed` | |
   | Any other `UnexpectedResponse` | `failed` | |

   - The "never left" case is connection refused, TLS verification, or a connect or pool timeout. It needs
     the change in §5.1.
   - Treating a 2xx with an unreadable body, or a 5xx other than 503, as outcome unknown matches upstream
     L1014+. Today rest.py raises `UnexpectedResponse` for these, which would wrongly say "nothing changed"
     (**D4**).
3. **Store:** `store(broker_password)`, which is `hass.config_entries.async_update_entry(entry, data={...,
   CONF_BROKER_PASSWORD: new})`.
   - It's stored before reconnecting, so a restart in the middle uses the new password.
   - The hop passphrase is never passed to `store`.
4. **Probe:** a fresh `LocalMqtt` with the new password calls `connect_once()` and then `stop()`, using
   the same factory as the config flow's credential check.
   - It retries after each delay in (1, 2, 4, 8, 15, 30), so it gives up after about 60 s.
   - `AuthFailed` (the CONNACK refused credentials) and `CannotConnect` are both retryable. A broker that
     is still propagating the change refuses briefly.
   - The running session stays up throughout, so entities don't go unavailable.
5. **Swap**, only if the probe succeeded:
   - `transport.async_replace_broker(password)` stops the running `LocalMqtt`, builds a new one with the
     new password, and starts its supervisor.
   - This is the step that takes the entry over to the new password. Pending commands get the existing
     `OutcomeUnknown("connection closed...")`.
   - No entry reload: unlike upstream's reload loop, the cloud, backfill and entities are left alone
     (**D2**).
6. **Result:**

   | Outcome | Response | Log |
   |---|---|---|
   | Reconnected | `{"hop_passphrase": ..., "reconnected": true, "stored": true}` | INFO, naming the entry_id only |
   | Not reconnected | Same, with `"reconnected": false` | ERROR, naming the entry_id: the passphrase is in the response; rotate again, or run reauth with it |

   - On failure, don't swap, and keep the stored new password: it's the one the panel now holds.
   - Don't fall back to the old password.

### 4.6 Part-way failures

| Where it stops | Panel state | What we do |
|---|---|---|
| PUT outcome unknown | Maybe rotated | Raise `outcome_unknown` with no secret. If it did rotate, the running session dies within about 30 s; with the in-progress flag cleared, reauth opens and asks for the passphrase the SPAN app shows. |
| PUT ok, `store` raises (HA stopping, entry removed while waiting) | Rotated | Catch it. Return `{"hop_passphrase", "reconnected": false, "stored": false}` and log an ERROR saying to run reauth with the passphrase from the response. Don't raise: the passphrase would be lost. |
| Stored, probe never accepted | Rotated, password stored | `reconnected: false` (above). The broker keeps refusing, so once the flag clears reauth opens, and the returned passphrase completes it. |
| Probe ok, swap raises | Rotated, stored, broker accepts | Return `reconnected: false`. A reload or restart picks the stored password up. |
| Cancelled (HA stopping) after the PUT | Rotated | `finally` clears the flag. The passphrase is lost unless the response was delivered, and the SPAN app shows it. Documented, not handled. |

The flow catches broad `Exception` only around `store` and `swap`, never around the PUT.

## 5. Changes to existing code

1. **`rest.py`:** `_request` keeps knowing whether a `PUT /auth/passphrase` was sent.
   - aiohttp `ClientConnectorError` (including `ClientConnectorCertificateError`) and the connect-phase
     `ServerTimeoutError`/pool timeout raise `CannotConnect` with `sent=False`.
   - Everything else on that path raises `OutcomeUnknown`.
   - `rotate_passphrase` then raises `NotSent` (a `CannotConnect` subclass) only for `sent=False`.
   - The messages still name the error class only.
2. **`rest.py`:** for `PUT /auth/passphrase`, a 2xx with an unreadable or incomplete body, and a 500, 502
   or other 5xx except 503, raise `OutcomeUnknown`.
3. **`transport.py`:**
   - `rotating` flag, checked in `_evaluate` before `async_start_reauth`;
   - `async_replace_broker(password)`;
   - a `probe_factory` reused from config_flow's credential check (extract a small `local_mqtt_for(entry,
     password)` helper).
4. **`__init__.py`:** `async_setup` registers the service. The handler stays thin: admin, then target,
   then lock, then `rotation.rotate(...)`.
5. **New:** `services.yaml`, the strings.json `services` and `exceptions` blocks, and en.json in step.

## 6. Keeping the passphrase secret

Rules:
- It's never logged at any level, never in an exception message or placeholder, never in an event, the
  logbook or a repair, and never in `entry.data`, `entry.options` or a Store.
- `RotationResult.__repr__` is redacted, like `RotatedPassphrase`.
- The service's `call_service` event carries only the ids in `service_data`, which is safe.

Outside our control; documented in README and in the service description (**D6**):
- A script or automation that stores the response in a variable keeps it in its trace
  (`.storage/trace.saved_traces`). Admin-only plus refusing calls without a user stops automations, but
  not an admin-run script.
- With `homeassistant.components.websocket_api` set to debug logging, HA logs the outgoing result,
  passphrase included.

## 7. Tests (no HA)

New `tests/test_rotation.py`, driving `rotation.rotate` with fakes plus the existing aiohttp test server in
`test_local_rest.py`:

- **Targeting:** every row of the §4.3 table.
- **Happy path**
  - Store gets the broker password only. Assert the hop passphrase never reaches `store`.
  - Probe on the first try; swap called once; `reconnected: true`.
- **Retries**
  - Probe refuses (`AuthFailed`) three times and then accepts. Sleeps follow 1, 2, 4. Never retried with
    the old password.
  - The probe always refuses: all six delays are used (about 60 s), the result is `reconnected: false`,
    swap isn't called, and the password stays stored.
- **PUT errors:** each row of the §4.5 step 2 table, with nothing stored. Includes a 200 with a bad body
  and a 502 giving `outcome_unknown`, and connection refused giving `cannot_connect`.
- **Part-way failures**
  - `store` raises: the response holds the passphrase, `stored: false`, nothing raised.
  - `swap` raises: `reconnected: false`.
- **The in-progress flag**
  - A transport-level test with a fake `LocalMqtt` whose `auth_failed` is True while `rotating` is set:
    the tick doesn't call `async_start_reauth`.
  - Cleared afterwards, including on exception.
- **Lock:** two concurrent rotations run in sequence, and the second starts from the first's stored
  password.
- **Redaction:** with a unique sentinel passphrase, run every path above under `caplog` at DEBUG, including
  `span_ebus.*` and aiohttp loggers. Assert the sentinel is absent from:
  - every log record's message and args;
  - `str()` and `repr()` of every raised exception and of `RotationResult`;
  - anything `store` received, other than the broker-password field.

  Also run it with the broker password set to a different sentinel.
- **Admin check:** the helper takes `(user_id, get_user)`, so it's tested pure:
  - None is refused;
  - a non-admin is refused;
  - an admin passes.

A Home Assistant container import check, as the reviewer did for PR 6, covers registration.

## 8. Decisions

Recommended options are marked ★. Risky ones are flagged.

- **D1. Targeting:** ★ both `config_entry_id` and `device_id`, optional, refused when ambiguous. The
  alternative is `config_entry_id` only, which is upstream parity.
- **D2. Reconnect:** ★ probe, then swap in place, with no reload. The alternative is upstream's reload
  loop.
  - The probe keeps entities and the cloud running, and avoids a reload storm on our heavier setup (the
    cloud link and backfill).
  - The cost is a new `async_replace_broker`.
- **D3. Retry schedule:** ★ upstream's 1/2/4/8/15/30 s (about 60 s), with a refused CONNACK retryable.
- **D4. Changes to error mapping in rest.py (§5.1-5.2):** ★ yes. **Risky (low):**
  - It changes shared `_request` error handling, so it is limited to the `PUT /auth/passphrase` path.
  - The existing rest tests must stay green.
- **D5. Loaded entries only:** ★ yes, unlike upstream, which also rotates entries that aren't loaded. An
  entry that isn't loaded has no transport to swap, and nothing would show whether the password works.
- **D6. Trace and websocket-debug exposure:** ★ document it; don't block admin-run scripts. **Risky (by
  design):** the passphrase can persist in a script trace. The alternative is to refuse any call whose
  context has a `parent_id`, which would also stop a script that an admin runs from a dashboard button.
- **D7. Service strings:** ★ add a `services` block (name and description) and `exceptions` translations.
  These aren't entity descriptions.
- **D8. Response field `stored`:** ★ add it, as an extension of upstream's
  `{hop_passphrase, reconnected}`. It's needed to report the part-way case in §4.6 honestly.
- **D9. 403 vs 401:** ★ keep them merged as `auth_failed`. Upstream splits out "insufficient privilege".
  Our `_raise_for` merges them, and both mean "reauth".
- **D10. Testing on the real panel:** ★ none, per the no-live-tests rule. Ship it marked "untested live".
  **Risky:**
  - The swap path and the broker's acceptance delay are only modelled.
  - A rotation on the user's panel would change the passphrase in the SPAN app.
