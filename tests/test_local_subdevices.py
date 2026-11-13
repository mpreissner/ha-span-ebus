"""Battery, solar, EV charger, MID and remote CT devices (r202639, PR 4b-2).

All data here is SYNTHETIC: the captured panel has no subdevices, so every id,
vendor, firmware and serial below is made up in the r202639 shape. Nothing here
comes from a real site.

Battery power keeps SPAN's raw sign: + = discharging, - = charging.
"""

import json

import pytest
from span_client.local import homie, model
from span_client.local.homie import DeviceState
from span_client.local.model import Scope

from tests.test_local_homie import description
from tests.test_local_model import CLOUD_UID
from tests.test_local_model_synthetic import (
    CIRCUIT,
    CIRCUIT_KEYS,
    CIRCUIT_SPECIAL,
    LINK_KEYS,
    LUGS,
    LUGS_KEYS,
    LUGS_SPECIAL,
    PANEL,
    ROOT_KEYS,
    ROOT_SPECIAL,
    _props_map,
    row,
    snap,
)

KEY = "nt-0000-test"
VENDOR = "Synthetic Vendor"
FIRMWARE = "0.0.0-synthetic"
SERIAL = "SYNTHETIC-NOT-A-SERIAL"
BESS, PV, EVSE, EVSE_2, MID = "bess-syn-01", "pv-syn-01", "evse-syn-01", "evse-syn-02", "mid-syn-01"
CIRCUIT_2 = "circuit-02"

_INFO = {
    "info/serial-number": {"datatype": "string"},
    "info/vendor-name": {"datatype": "string"},
    "info/model": {"datatype": "string"},
    "info/firmware-version": {"datatype": "string"},
    "info/hardware-version": {"datatype": "string"},
}
_TEXT = {"datatype": "string"}
SUBDEVICE_PROPS = {
    "bess": {
        **_INFO,
        "meter/active-power": {"datatype": "float"},
        "soc/soc": {"datatype": "float"},
        "soc/soe": {"datatype": "float"},
        "info/nameplate-capacity": {"datatype": "float"},
        "status/communication-state": _TEXT,
    },
    "pv": {**_INFO, "info/nominal-power": {"datatype": "float"}},
    "evse": {
        **_INFO,
        "status/status": _TEXT,
        "meter/advertised-current": {"datatype": "float"},
        "config/max-charge-current": {"datatype": "float"},
        "config/user-max-charge-current": {"datatype": "float"},
        "switch/lock-state": _TEXT,
    },
    "mid": {
        **_INFO,
        "grid/grid-state": _TEXT,
        "grid/islanding-state": _TEXT,
        "grid/grid-forming-entity": _TEXT,
    },
}
SUBDEVICE_VALUES = {
    "bess": {
        "meter/active-power": "1500.0",
        "soc/soc": "81.4",
        "soc/soe": "11.234",
        "info/nameplate-capacity": "13.5",
        "status/communication-state": "OK",
    },
    "pv": {"info/nominal-power": "7600"},
    "evse": {
        "status/status": "CHARGING",
        "meter/advertised-current": "31.96",
        "config/max-charge-current": "48",
        "config/user-max-charge-current": "40",
        "switch/lock-state": "UNLOCKED",
    },
    "mid": {
        "grid/grid-state": "UP",
        "grid/islanding-state": "ON_GRID",
        "grid/grid-forming-entity": "GRID",
    },
}
LINK_SPECIAL = {
    **CIRCUIT_SPECIAL,
    "connection/feeds-device-status": {"datatype": "enum", "format": "OK,LOST,DEGRADED"},
}
ROOT_ISLANDING = {
    **ROOT_SPECIAL,
    "shed/asserted-islanding-state": {
        "datatype": "enum",
        "format": "NONE,ON_GRID,OFF_GRID",
        "settable": True,
    },
}


def _sub(kind: str, *, drop: tuple[str, ...] = ()) -> bytes:
    props = {k: v for k, v in SUBDEVICE_PROPS[kind].items() if k not in drop}
    nodes: dict = {}
    for key, spec in props.items():
        capability, prop = key.split("/")
        nodes.setdefault(capability, {"properties": {}})["properties"][prop] = spec
    return description(kind, nodes, parent=PANEL, root=PANEL)


def site(
    kinds: dict[str, str],
    *,
    feeds: dict[str, tuple[str, str]] | None = None,
    values: dict[str, dict[str, str]] | None = None,
    drop: dict[str, tuple[str, ...]] | None = None,
    circuits: tuple[str, ...] = (CIRCUIT,),
) -> homie.DeviceTree:
    """A panel with lugs, `circuits` and the subdevices `{id: class}`.

    `feeds` maps a circuit to the (target id, status) its link publishes.
    Every subdevice gets the synthetic vendor, firmware and serial.
    """
    feeds = feeds or {}
    drop = drop or {}
    children = [LUGS, *circuits, *kinds]
    tree = homie.DeviceTree()
    root_keys = (*ROOT_KEYS, "shed/asserted-islanding-state")
    tree.apply(
        homie.description_topic(PANEL),
        description(
            "distribution-enclosure", _props_map(root_keys, ROOT_ISLANDING), children=children
        ),
    )
    tree.apply(
        homie.description_topic(LUGS),
        description("lugs", _props_map(LUGS_KEYS, LUGS_SPECIAL), parent=PANEL, root=PANEL),
    )
    for circuit_id in circuits:
        tree.apply(
            homie.description_topic(circuit_id),
            description("circuit", _props_map(LINK_KEYS, LINK_SPECIAL), parent=PANEL, root=PANEL),
        )
    for device_id, kind in kinds.items():
        tree.apply(homie.description_topic(device_id), _sub(kind, drop=drop.get(device_id, ())))
    for device_id in (PANEL, LUGS, *circuits, *kinds):
        tree.apply(f"ebus/5/{device_id}/$state", b"ready")
    tree.apply(homie.value_topic(LUGS, "info/direction"), b"UPSTREAM")
    tree.apply(homie.value_topic(PANEL, "shed/asserted-islanding-state"), b"ON_GRID")
    for device_id, kind in kinds.items():
        info = {
            "info/serial-number": SERIAL,
            "info/vendor-name": VENDOR,
            "info/model": f"Synthetic {kind}",
            "info/firmware-version": FIRMWARE,
            "info/hardware-version": "hw-synthetic",
        }
        for key, text in {**info, **SUBDEVICE_VALUES[kind]}.items():
            if key in SUBDEVICE_PROPS[kind] and key not in drop.get(device_id, ()):
                tree.apply(homie.value_topic(device_id, key), text.encode())
    for circuit_id, (target, status) in feeds.items():
        tree.apply(homie.value_topic(circuit_id, "connection/feeds-device-id"), target.encode())
        tree.apply(homie.value_topic(circuit_id, "connection/feeds-device-status"), status.encode())
    for device_id, props in (values or {}).items():
        for key, text in props.items():
            tree.apply(homie.value_topic(device_id, key), text.encode())
    return tree


FULL = {BESS: "bess", PV: "pv", EVSE: "evse", MID: "mid"}


def full_site(**kwargs) -> homie.DeviceTree:
    kwargs.setdefault("circuits", (CIRCUIT, CIRCUIT_2))
    kwargs.setdefault("feeds", {CIRCUIT: (EVSE, "OK"), CIRCUIT_2: (PV, "OK")})
    return site(FULL, **kwargs)


def entities(snapshot: model.Snapshot) -> list[tuple[model.Row, str]]:
    return [
        (r, device_id)
        for r in model.ROWS
        for device_id in model.row_devices(snapshot, r)
        if model.row_exists(snapshot, r, device_id)
    ]


@pytest.fixture
def fresh_unknown_log():
    """Unknown enum values log once per key per process; start each test clean."""
    model._unknown_values_logged.clear()
    yield
    model._unknown_values_logged.clear()


# --- devices and identity --------------------------------------------------------------


def test_every_subdevice_is_classified_and_nothing_is_unmapped():
    snapshot = snap(full_site())
    assert list(snapshot.bess) == [BESS]
    assert list(snapshot.pv) == [PV]
    assert list(snapshot.evse) == [EVSE]
    assert list(snapshot.mid) == [MID]
    assert snapshot.unmapped == frozenset()
    assert not set(FULL) & set(snapshot.circuits)


def test_subdevices_hang_off_the_panel_with_vendor_firmware_and_no_serial():
    snapshot = snap(full_site())
    refs = {
        model.row_device(snapshot, r, device_id)
        for r, device_id in entities(snapshot)
        if r.scope in model.SUBDEVICE_SCOPES
    }
    assert {ref.device_id for ref in refs} == set(FULL)
    for ref in refs:
        assert ref.manufacturer == VENDOR
        assert ref.sw_version == FIRMWARE
        assert ref.model == f"Synthetic {ref.kind}"
        assert ref.hw_version == ("hw-synthetic" if ref.kind == "mid" else None)
        assert ref.name_hint is None  # one EVSE: no hint
        assert model.device_identifier(KEY, ref) == f"{KEY}_{ref.kind}/{ref.device_id}"
        assert SERIAL not in repr(ref)


def test_panel_rows_stay_on_the_panel_device():
    snapshot = snap(full_site())
    for r, device_id in entities(snapshot):
        if r.scope in (Scope.PANEL, Scope.UPSTREAM, Scope.DOWNSTREAM):
            assert model.row_device(snapshot, r, device_id) is model.PANEL_REF
    assert model.device_identifier(KEY, model.PANEL_REF) is None


def test_serials_never_reach_facts_values_attributes_or_the_store():
    tree = full_site()
    snapshot = snap(tree)
    for facts in (*snapshot.bess.values(), *snapshot.pv.values(), *snapshot.evse.values()):
        assert "info/serial-number" not in facts.described
        assert SERIAL not in repr(facts)
    for r, device_id in entities(snapshot):
        facts = model.row_facts(snapshot, r, device_id)
        assert SERIAL not in repr(r.value(facts))
        assert SERIAL not in repr(r.attrs(facts) if r.attrs else {})
    stored = json.dumps(model.store_payload(tree, PANEL))
    assert SERIAL not in stored
    assert "Synthetic Vendor" in stored  # the rest of the device survives a restart


def test_the_store_keeps_only_devices_under_the_root():
    tree = full_site()
    tree.apply(homie.description_topic("bess-orphan"), _sub("bess"))  # no parent listing it
    tree.apply(homie.value_topic("bess-orphan", "soc/soc"), b"50")
    payload = model.store_payload(tree, PANEL)
    assert "bess-orphan" not in payload["descriptions"]
    assert "bess-orphan" not in payload["values"]
    assert BESS in payload["values"]


def test_restored_subdevices_come_back_without_serials():
    payload = model.store_payload(full_site(), PANEL)
    restored = homie.DeviceTree()
    model.load_payload(payload, restored)
    back = model.build_snapshot(restored, PANEL, restored=True)
    assert back.bess[BESS].vendor == VENDOR
    assert back.bess[BESS].get("soc/soc") == 81.4
    assert "info/serial-number" not in back.bess[BESS].values


def test_unique_ids_are_unique_scoped_and_cannot_meet_the_cloud_ones():
    snapshot = snap(full_site())
    uids = [model.row_uid(r, KEY, device_id) for r, device_id in entities(snapshot)]
    assert len(uids) == len(set(uids))
    assert not any(CLOUD_UID.match(uid) for uid in uids)
    assert f"{KEY}_bess/{BESS}/soc/soc" in uids
    assert f"{KEY}_evse/{EVSE}/derived/charging" in uids
    assert f"{KEY}_distribution-enclosure/derived/grid-islandable" in uids


def test_subdevice_rows_need_their_class():
    snapshot = snap(site({BESS: "bess"}))
    keys = {r.key for r, _ in entities(snapshot)}
    assert {k for k in keys if k.startswith(("pv:", "evse:", "mid:"))} == set()
    assert "bess:soc/soc" in keys


# --- battery -------------------------------------------------------------------------


def test_battery_values_keep_the_raw_sign_plus_is_discharging():
    snapshot = snap(full_site(values={BESS: {"meter/active-power": "-2200.5"}}))
    facts = snapshot.bess[BESS]
    assert row("bess:meter/active-power").value(facts) == -2200.5  # charging
    snapshot = snap(full_site())
    facts = snapshot.bess[BESS]
    assert row("bess:meter/active-power").value(facts) == 1500.0  # discharging
    assert row("bess:soc/soc").value(facts) == 81.4
    assert row("bess:soc/soe").value(facts) == 11.234
    assert row("bess:info/nameplate-capacity").value(facts) == 13.5
    assert row("bess:status/communication-state").value(facts) == "ok"


@pytest.mark.parametrize(
    ("comm", "feeds", "expected"),
    [
        ("OK", None, True),
        ("LOST", None, False),
        ("DEGRADED", (BESS, "OK"), False),  # communication wins over the link
        (None, (BESS, "OK"), True),
        (None, (BESS, "LOST"), False),
    ],
)
def test_battery_connected_from_communication_then_the_link(comm, feeds, expected):
    values = {BESS: {"status/communication-state": comm}} if comm else None
    drop = {} if comm else {BESS: ("status/communication-state",)}
    tree = site(
        {BESS: "bess"},
        feeds={CIRCUIT: feeds} if feeds else None,
        values=values,
        drop=drop,
    )
    snapshot = snap(tree)
    connected = row("bess:derived/connected")
    assert model.row_exists(snapshot, connected, BESS)
    assert connected.value(snapshot.bess[BESS]) is expected


@pytest.mark.parametrize(
    ("key", "device_id", "state"),
    [
        ("bess:derived/connected", BESS, b"lost"),
        ("bess:derived/connected", BESS, b"disconnected"),
        ("pv:derived/panel-link", PV, b"lost"),
        ("evse:derived/panel-link", EVSE, b"lost"),
    ],
)
def test_a_subdevice_that_is_not_up_reads_disconnected_not_unavailable(key, device_id, state):
    tree = full_site()
    tree.apply(f"ebus/5/{device_id}/$state", state)
    snapshot = snap(tree)
    connectivity = row(key)
    assert connectivity.ignores_state
    facts = model.row_facts(snapshot, connectivity, device_id)
    assert facts.state not in model.UP_STATES
    assert connectivity.value(facts) is False


def test_connectivity_rows_still_read_the_link_while_up():
    snapshot = snap(full_site(feeds={CIRCUIT: (EVSE, "LOST"), CIRCUIT_2: (PV, "OK")}))
    assert row("evse:derived/panel-link").value(snapshot.evse[EVSE]) is False
    assert row("pv:derived/panel-link").value(snapshot.pv[PV]) is True


def test_battery_connected_is_absent_without_communication_or_link():
    snapshot = snap(site({BESS: "bess"}, drop={BESS: ("status/communication-state",)}))
    assert not model.row_exists(snapshot, row("bess:derived/connected"), BESS)


def test_battery_power_names_its_circuit_instead_of_copying_its_readings():
    tree = site(
        {BESS: "bess"},
        feeds={CIRCUIT: (BESS, "OK")},
        values={CIRCUIT: {"info/name": "Battery feed", "meter/active-power": "900"}},
    )
    snapshot = snap(tree)
    attrs = row("bess:meter/active-power").attrs(snapshot.bess[BESS])
    assert attrs == {"circuit": "Battery feed"}


# --- islanding on the panel ---------------------------------------------------------


def test_grid_islandable_is_absent_without_a_mid_or_battery():
    snapshot = snap(site({PV: "pv"}))
    assert not model.row_exists(snapshot, row("grid_islandable"), PANEL)
    assert not model.row_exists(snapshot, row("mid:grid/grid-forming-entity"), PANEL)


def test_grid_islandable_is_off_with_only_a_battery():
    snapshot = snap(site({BESS: "bess"}))
    islandable = row("grid_islandable")
    assert model.row_exists(snapshot, islandable, PANEL)
    assert islandable.value(snapshot.panel) is False
    assert islandable.attrs(snapshot.panel) == {"asserted_islanding_state": "on_grid"}
    assert not model.row_exists(snapshot, row("mid:grid/grid-forming-entity"), PANEL)


def test_grid_islandable_is_on_with_a_mid():
    snapshot = snap(site({MID: "mid"}))
    assert row("grid_islandable").value(snapshot.panel) is True
    forming = row("mid:grid/grid-forming-entity")
    assert model.row_exists(snapshot, forming, PANEL)
    assert forming.value(snapshot.panel) == "grid"


@pytest.mark.parametrize("kind", ["bess", "mid"])
def test_grid_islandable_is_unknown_once_the_mid_or_battery_leaves(kind):
    device_id = MID if kind == "mid" else BESS
    islandable = row("grid_islandable")
    before = snap(site({device_id: kind}))
    assert model.row_exists(before, islandable, PANEL)
    after = snap(site({}))  # the entity outlives the device that made it
    assert islandable.value(after.panel) is None  # unknown, never "not islandable"


@pytest.mark.parametrize("raw", ["BATTERY", "GENERATOR", "GRID", "NONE", "PV", "UNKNOWN"])
def test_grid_forming_known_values(raw, fresh_unknown_log):
    snapshot = snap(site({MID: "mid"}, values={MID: {"grid/grid-forming-entity": raw}}))
    value = row("mid:grid/grid-forming-entity").value(snapshot.panel)
    assert value == raw.lower()
    assert value in model.GRID_FORMING_OPTIONS


def test_grid_forming_unknown_value_shows_unknown_and_logs_once_without_it(
    caplog, fresh_unknown_log
):
    caplog.set_level("DEBUG")
    tree = site({MID: "mid"}, values={MID: {"grid/grid-forming-entity": "WIND-SECRET"}})
    forming = row("mid:grid/grid-forming-entity")
    assert forming.value(snap(tree).panel) == "unknown"
    assert forming.value(snap(tree).panel) == "unknown"
    assert caplog.text.count("Unrecognised value") == 1
    assert "WIND-SECRET" not in caplog.text
    assert MID not in caplog.text


def test_mid_grid_and_islanding_state():
    facts = snap(site({MID: "mid"}, values={MID: {"grid/islanding-state": "OFF_GRID"}})).mid[MID]
    assert row("mid:grid/grid-state").value(facts) == "up"
    assert row("mid:grid/islanding-state").value(facts) == "off_grid"


# --- EV charger ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "status", "charging", "plugged"),
    [
        ("CHARGING", "charging", True, True),
        ("PREPARING", "preparing", False, True),
        ("AVAILABLE", "available", False, False),
        ("UNAVAILABLE", "unavailable", False, False),
        ("SUSPENDED_EV", None, False, True),  # outside the enum: not shown, still plugged
    ],
)
def test_evse_status_charging_and_plug(raw, status, charging, plugged, fresh_unknown_log):
    facts = snap(site({EVSE: "evse"}, values={EVSE: {"status/status": raw}})).evse[EVSE]
    assert row("evse:status/status").value(facts) == status
    assert row("evse:derived/charging").value(facts) is charging
    assert row("evse:derived/ev-connected").value(facts) is plugged


def test_evse_unknown_status_logs_once_without_the_value(caplog, fresh_unknown_log):
    caplog.set_level("DEBUG")
    tree = site({EVSE: "evse"}, values={EVSE: {"status/status": "FAULTED-SECRET"}})
    status = row("evse:status/status")
    assert status.value(snap(tree).evse[EVSE]) is None
    assert status.value(snap(tree).evse[EVSE]) is None
    assert caplog.text.count("Unrecognised value") == 1
    assert "FAULTED-SECRET" not in caplog.text
    assert EVSE not in caplog.text


def test_evse_without_a_status_has_no_charging_or_plug_entities():
    snapshot = snap(site({EVSE: "evse"}, drop={EVSE: ("status/status",)}))
    for key in ("evse:status/status", "evse:derived/charging", "evse:derived/ev-connected"):
        assert not model.row_exists(snapshot, row(key), EVSE)


def test_evse_readings_and_attributes():
    tree = full_site(values={CIRCUIT: {"info/name": "Garage"}})
    facts = snap(tree).evse[EVSE]
    assert row("evse:meter/advertised-current").value(facts) == 31.96
    assert row("evse:config/max-charge-current").value(facts) == 48.0
    assert row("evse:config/max-charge-current").attrs(facts) == {"user_max_charge_current": 40.0}
    assert row("evse:status/status").attrs(facts) == {"lock_state": "unlocked", "circuit": "Garage"}
    assert row("evse:derived/panel-link").value(facts) is True


def test_one_evse_has_no_name_hint_two_take_circuit_or_index():
    one = snap(full_site())
    assert model.row_device(one, row("evse:status/status"), EVSE).name_hint is None
    tree = site(
        {EVSE: "evse", EVSE_2: "evse"},
        feeds={CIRCUIT: (EVSE_2, "OK")},
        values={CIRCUIT: {"info/name": "Driveway"}},
    )
    two = snap(tree)
    hints = {d: model.row_device(two, row("evse:status/status"), d).name_hint for d in two.evse}
    assert hints == {EVSE: "1", EVSE_2: "Driveway"}


# --- solar and links ----------------------------------------------------------------


def test_pv_panel_link_names_its_circuit():
    tree = full_site(values={CIRCUIT_2: {"info/name": "Roof array"}})
    snapshot = snap(tree)
    link = row("pv:derived/panel-link")
    assert model.row_exists(snapshot, link, PV)
    assert link.value(snapshot.pv[PV]) is True
    assert link.attrs(snapshot.pv[PV]) == {"circuit": "Roof array"}
    assert row("pv:info/nominal-power").value(snapshot.pv[PV]) == 7600.0


def test_pv_panel_link_is_absent_until_a_status_is_published():
    snapshot = snap(site({PV: "pv"}))
    assert snapshot.pv[PV].link is None
    assert not model.row_exists(snapshot, row("pv:derived/panel-link"), PV)


def test_link_conflict_lowest_claimant_wins_and_is_noted():
    tree = site(
        {EVSE: "evse"},
        circuits=(CIRCUIT, CIRCUIT_2),
        feeds={CIRCUIT_2: (EVSE, "LOST"), CIRCUIT: (EVSE, "OK")},
    )
    snapshot = snap(tree)
    link = snapshot.evse[EVSE].link
    assert (link.via_kind, link.via_id, link.status) == ("circuit", CIRCUIT, "OK")
    assert model.Unmapped("evse", model.LINK_CONFLICT) in snapshot.unmapped


def test_a_link_to_an_unknown_target_stays_on_the_circuit():
    tree = site({EVSE: "evse"}, feeds={CIRCUIT: ("evse-syn-missing", "OK")})
    snapshot = snap(tree)
    assert snapshot.evse[EVSE].link is None
    assert snapshot.circuits[CIRCUIT].feeds.via_id == "evse-syn-missing"
    assert not any(u.prop == model.LINK_CONFLICT for u in snapshot.unmapped)


# --- remote CT ----------------------------------------------------------------------

REMOTE_KEYS = tuple(k for k in CIRCUIT_KEYS if not k.startswith(("switch/", "info/spaces")))


def _remote_tree(names: dict[str, str]) -> homie.DeviceTree:
    tree = site({}, circuits=(CIRCUIT, CIRCUIT_2))
    for circuit_id in (CIRCUIT, CIRCUIT_2):
        tree.apply(
            homie.description_topic(circuit_id),
            description(
                "circuit", _props_map(REMOTE_KEYS, CIRCUIT_SPECIAL), parent=PANEL, root=PANEL
            ),
        )
        tree.apply(f"ebus/5/{circuit_id}/$state", b"ready")
    for circuit_id, name in names.items():
        tree.apply(homie.value_topic(circuit_id, "info/name"), name.encode())
    return tree


def test_remote_ct_gets_its_own_device_named_after_it():
    snapshot = snap(_remote_tree({CIRCUIT: "Detached garage"}))
    ref = model.row_device(snapshot, row("power"), CIRCUIT)
    assert ref.kind == model.REMOTE_CT
    assert ref.name_hint == "Detached garage"
    assert model.device_identifier(KEY, ref) == f"{KEY}_circuit/{CIRCUIT}"
    unnamed = model.row_device(snapshot, row("power"), CIRCUIT_2)
    position = list(snapshot.circuits).index(CIRCUIT_2) + 1  # among remote CTs, panel order
    assert unnamed.name_hint == f"Remote CT {position}"


def test_ordinary_circuits_stay_on_the_panel_device():
    snapshot = snap(full_site())
    assert model.row_device(snapshot, row("power"), CIRCUIT) is model.PANEL_REF


def test_subdevices_go_unavailable_with_the_root():
    tree = full_site()
    tree.apply(f"ebus/5/{PANEL}/$state", b"lost")
    snapshot = snap(tree)
    assert snapshot.bess[BESS].state is DeviceState.LOST
    assert snapshot.evse[EVSE].state is DeviceState.LOST


# --- removing stale devices ---------------------------------------------------------


def _ids(snapshot: model.Snapshot, *refs: model.DeviceRef) -> frozenset[str]:
    return frozenset(model.device_identifier(KEY, ref) for ref in refs)


def test_the_panel_device_is_never_removable():
    snapshot = snap(full_site())
    assert not model.device_removable(snapshot, KEY, KEY, frozenset({KEY}))
    assert not model.device_removable(model.EMPTY, KEY, KEY, frozenset({KEY}))


@pytest.mark.parametrize(("kind", "device_id"), [(k, i) for i, k in FULL.items()])
def test_a_current_subdevice_is_not_removable(kind, device_id):
    snapshot = snap(full_site())
    ids = _ids(snapshot, model.DeviceRef(kind, device_id))
    assert not model.device_removable(snapshot, KEY, KEY, ids)


def test_a_subdevice_the_panel_no_longer_reports_is_removable():
    snapshot = snap(site({PV: "pv"}))
    stale = _ids(snapshot, model.DeviceRef("bess", BESS))
    assert model.device_removable(snapshot, KEY, KEY, stale)
    # A device carrying a current identifier too stays.
    current = _ids(snapshot, model.DeviceRef("pv", PV))
    assert not model.device_removable(snapshot, KEY, KEY, stale | current)


def test_a_current_remote_ct_is_not_removable_but_a_gone_one_is():
    snapshot = snap(_remote_tree({CIRCUIT: "Detached garage"}))
    remote = _ids(snapshot, model.DeviceRef(model.REMOTE_CT, CIRCUIT))
    assert remote == {f"{KEY}_circuit/{CIRCUIT}"}
    assert not model.device_removable(snapshot, KEY, KEY, remote)
    # The same circuit back in its breaker: no longer a remote CT device.
    assert model.device_removable(snap(full_site()), KEY, KEY, remote)


def test_nothing_is_removable_before_the_panel_is_known():
    stale = frozenset({f"{KEY}_bess/{BESS}"})
    assert model.EMPTY.panel is None
    assert not model.device_removable(model.EMPTY, KEY, KEY, stale)


# --- translations ---------------------------------------------------------------------

STRINGS = "custom_components/span_ebus/strings.json"
EN = "custom_components/span_ebus/translations/en.json"


def test_every_named_row_has_a_translation_and_no_description():
    with open(STRINGS) as f:
        strings = json.load(f)
    with open(EN) as f:
        assert json.load(f) == strings
    for r in model.ROWS:
        if r.scope is Scope.CIRCUIT:
            continue  # named "<circuit> <suffix>" in code
        entry = strings["entity"][r.platform][model.translation_key(r)]
        assert entry["name"]
        assert "description" not in entry
    sensor = strings["entity"]["sensor"]
    assert set(sensor["mid_grid_grid_forming_entity"]["state"]) == set(model.GRID_FORMING_OPTIONS)
    assert set(sensor["evse_status_status"]["state"]) == set(model.EVSE_STATUS_OPTIONS)
