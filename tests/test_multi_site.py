"""Multi-site accounts: one panel per SPAN "site", split and streamed separately.

Shapes mirror a real two-site GetSitesForUser response (a MAIN 16 and a MAIN
40 on one account), with every id and serial replaced by a placeholder.
"""

from __future__ import annotations

import stat
import threading
import uuid

from span_client import cloud_auth
from span_client.backend import (
    PanelInfo,
    SiteInfo,
    pair_by_lookup,
    parse_sites,
    primary_panel_for,
    site_device_uuid,
    stream_identity,
)
from span_client.cloud_pb import field_message, field_string, field_varint


def _member(kind: int, hardware_id: str) -> bytes:
    return field_message(1, field_varint(1, kind) + field_string(2, hardware_id))


def _site(site_id: str, serial: str, model: str, members: list[tuple[int, str]]) -> bytes:
    head = field_message(1, field_string(1, site_id) + field_string(2, "<address>"))
    board = field_message(
        2,
        field_message(4, field_string(3, serial) + field_string(5, model))
        + field_string(5, "Panelboard"),
    )
    body = b"".join(_member(k, h) for k, h in members) + board
    return field_message(1, head + field_message(3, body))


SITES = _site("site-a", "XC-AAAA", "1-04100-01", [(4, "site-a"), (5, "gw-a")]) + _site(
    "site-b", "XC-BBBB", "1-02100-04", [(6, "bess-b"), (4, "site-b"), (5, "gw-b")]
)


def test_two_sites_are_split_with_their_own_hardware_ids():
    sites = parse_sites(SITES)
    assert [s.site_id for s in sites] == ["site-a", "site-b"]
    assert sites[0].hardware_ids == ("site-a", "gw-a")
    assert sites[1].hardware_ids == ("bess-b", "site-b", "gw-b")


def test_each_site_carries_its_own_panel_serial_and_model():
    a, b = parse_sites(SITES)
    assert (a.serial, a.model) == ("XC-AAAA", "MAIN 16")
    assert (b.serial, b.model) == ("XC-BBBB", "MAIN 40")


def test_unknown_model_code_is_named_not_dropped():
    (site,) = parse_sites(_site("s", "XC-1", "9-99999-99", [(4, "s")]))
    assert site.model == "Panel 9-99999-99"


def test_unfamiliar_or_empty_shapes_give_no_sites():
    assert parse_sites(b"") == []
    assert parse_sites(b"\xff\xff\xff") == []
    # A site with no subscribable hardware has nothing to stream.
    assert parse_sites(_site("s", "XC-1", "1-02100-04", [])) == []


ENTRY_UUID = str(uuid.uuid4())


def _info(site_id: str = "site-a", serial: str | None = "XC-AAAA") -> SiteInfo:
    return SiteInfo(site_id, (site_id,), (PanelInfo(site_id, serial, "1-02100-04", None),))


def test_the_primary_panel_keeps_the_entrys_identity():
    # Entities, energy totals and the Ably channel must all survive an upgrade.
    identity = stream_identity(
        _info(), device_uuid=ENTRY_UUID, serial="XC-SAVED", primary_panel="XC-AAAA"
    )
    assert identity.device_uuid == ENTRY_UUID
    assert identity.serial == "XC-SAVED"
    assert identity.store_panel_key is None


def test_the_primary_panel_fills_in_a_serial_the_entry_never_saved():
    identity = stream_identity(
        _info(), device_uuid=ENTRY_UUID, serial=None, primary_panel="XC-AAAA"
    )
    assert identity.serial == "XC-AAAA"


def test_no_site_at_all_is_the_legacy_single_stream():
    identity = stream_identity(None, device_uuid=ENTRY_UUID, serial="XC-SAVED", primary_panel=None)
    assert (identity.device_uuid, identity.serial) == (ENTRY_UUID, "XC-SAVED")
    assert identity.store_panel_key is None


def test_the_primary_is_unmoved_when_the_account_gains_a_panel():
    primary, newcomer = (
        stream_identity(s, device_uuid=ENTRY_UUID, serial="XC-AAAA", primary_panel="XC-AAAA")
        for s in (_info("site-a", "XC-AAAA"), _info("site-b", "XC-BBBB"))
    )
    assert (primary.device_uuid, primary.store_panel_key) == (ENTRY_UUID, None)
    assert newcomer.device_uuid == site_device_uuid(ENTRY_UUID, "site-b")
    assert (newcomer.serial, newcomer.store_panel_key) == ("XC-BBBB", "XC-BBBB")


def test_without_a_primary_every_panel_is_scoped_by_its_own_key():
    a = stream_identity(
        _info("site-a", "XC-AAAA"), device_uuid=ENTRY_UUID, serial="XC-AAAA", primary_panel=None
    )
    b = stream_identity(
        _info("site-b", "XC-BBBB"), device_uuid=ENTRY_UUID, serial="XC-AAAA", primary_panel=None
    )
    assert a.device_uuid == site_device_uuid(ENTRY_UUID, "site-a")
    assert a.device_uuid != b.device_uuid
    assert (a.serial, b.serial) == ("XC-AAAA", "XC-BBBB")
    assert (a.store_panel_key, b.store_panel_key) == ("XC-AAAA", "XC-BBBB")


def test_panels_without_serials_still_get_separate_devices_and_stores():
    a = stream_identity(
        _info("site-a", None), device_uuid=ENTRY_UUID, serial=None, primary_panel=None
    )
    b = stream_identity(
        _info("site-b", None), device_uuid=ENTRY_UUID, serial=None, primary_panel=None
    )
    assert a.serial != b.serial
    assert a.store_panel_key != b.store_panel_key


def test_only_an_accounts_single_panel_is_primary():
    assert primary_panel_for([_info("site-a", "XC-AAAA")]) == "XC-AAAA"
    assert primary_panel_for([_info("site-a", None)]) == "site-a"
    assert primary_panel_for([_info("site-a"), _info("site-b", "XC-BBBB")]) is None


def test_a_panel_left_alone_on_the_account_stays_scoped():
    # "No primary" is a recorded answer, not a gap to fill in: this panel's totals
    # are in its own store, and the entry's is empty.
    identity = stream_identity(
        _info("site-b", "XC-BBBB"), device_uuid=ENTRY_UUID, serial="XC-AAAA", primary_panel=None
    )
    assert identity.device_uuid == site_device_uuid(ENTRY_UUID, "site-b")
    assert identity.store_panel_key == "XC-BBBB"


def test_the_others_stay_scoped_when_the_primary_leaves_the_account():
    identity = stream_identity(
        _info("site-b", "XC-BBBB"),
        device_uuid=ENTRY_UUID,
        serial="XC-AAAA",
        primary_panel="XC-AAAA",
    )
    assert identity.device_uuid == site_device_uuid(ENTRY_UUID, "site-b")
    assert identity.store_panel_key == "XC-BBBB"


def test_a_primary_recorded_by_hardware_id_survives_a_serial_appearing():
    site = SiteInfo("site-a", ("gw-a",), (PanelInfo("site-a", "XC-AAAA", None, "gw-a"),))
    identity = stream_identity(site, device_uuid=ENTRY_UUID, serial=None, primary_panel="gw-a")
    assert (identity.device_uuid, identity.store_panel_key) == (ENTRY_UUID, None)


def test_the_primary_keeps_its_identity_when_a_board_joins_its_site_ahead_of_it():
    panels = (
        PanelInfo("site-a", "XC-NEW", None, "gw-new"),
        PanelInfo("site-a", "XC-AAAA", None, "gw-a"),
    )
    site = SiteInfo("site-a", ("gw-new", "gw-a"), panels)
    identity = stream_identity(site, device_uuid=ENTRY_UUID, serial=None, primary_panel="XC-AAAA")
    assert (identity.device_uuid, identity.store_panel_key) == (ENTRY_UUID, None)


def test_the_primary_keeps_its_identity_when_a_panel_appears_on_another_site():
    primary = _info("site-a", "XC-AAAA")
    other = _info("site-b", "XC-BBBB")
    kept = stream_identity(primary, device_uuid=ENTRY_UUID, serial=None, primary_panel="XC-AAAA")
    moved = stream_identity(other, device_uuid=ENTRY_UUID, serial=None, primary_panel="XC-AAAA")
    assert kept.device_uuid == ENTRY_UUID
    assert (moved.device_uuid, moved.store_panel_key) == (
        site_device_uuid(ENTRY_UUID, "site-b"),
        "XC-BBBB",
    )


def test_a_serial_less_panel_is_keyed_by_its_hardware_id():
    panel = PanelInfo("site-a", None, None, "gw-a")
    assert panel.key == "gw-a"
    assert not panel.answers_to(None)


def test_a_site_uuid_is_stable_across_restarts():
    assert site_device_uuid(ENTRY_UUID, "site-a") == site_device_uuid(ENTRY_UUID, "site-a")


def test_panels_refreshing_together_do_not_break_the_shared_token_store(tmp_path):
    path = tmp_path / "entry.json"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def refresh(n: int) -> None:
        start.wait()
        tokens = cloud_auth.CloudTokens(f"a{n}", f"i{n}", f"r{n}", float(n))
        try:
            for _ in range(100):
                cloud_auth.save_tokens(path, tokens)
                assert cloud_auth.load_tokens(path) is not None
        except BaseException as err:  # noqa: BLE001 — reported by the main thread
            errors.append(err)

    threads = [threading.Thread(target=refresh, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["entry.json"]


# Two panels on one site. No such response has been seen: shapes A, B and C are
# the guesses from the spec, not captures, and every id below is invented.


def _board(serial: str | None, model: str = "1-02100-04", links: tuple[str, ...] = ()) -> bytes:
    ident = field_message(4, field_string(3, serial) + field_string(5, model)) if serial else b""
    components = b"".join(field_message(8, field_message(7, field_string(1, h))) for h in links)
    return field_message(2, ident + field_string(5, "<board name>") + components)


def _shared_site(*groups: bytes) -> bytes:
    head = field_message(1, field_string(1, "site-1") + field_string(2, "<address>"))
    return field_message(1, head + b"".join(field_message(3, g) for g in groups))


def _pairs(raw: bytes) -> dict[str | None, str | None]:
    (site,) = parse_sites(raw)
    return {p.serial: p.hardware_id for p in site.panels}


def test_a_one_panel_site_pairs_without_any_component_link():
    assert _pairs(_site("s", "XC-1", "1-02100-04", [(4, "s"), (5, "gw")])) == {"XC-1": "gw"}


def test_a_board_with_no_serial_stays_paired_on_a_one_panel_site():
    (site,) = parse_sites(_shared_site(_member(4, "site-1") + _member(5, "gw") + _board(None)))
    assert site.lead.hardware_id == "gw"
    assert site.lead.key == "gw"


def test_a_site_with_no_board_still_has_a_panel():
    (site,) = parse_sites(_shared_site(_member(4, "site-1") + _member(5, "gw")))
    assert [p.key for p in site.panels] == ["gw"]


def test_guessed_shape_a_pairs_within_each_group():
    raw = _shared_site(
        _member(4, "site-1") + _member(5, "gw-1") + _board("XC-A"),
        _member(5, "gw-2") + _board("XC-B"),
    )
    assert _pairs(raw) == {"XC-A": "gw-1", "XC-B": "gw-2"}


def test_guessed_shape_b_pairs_by_component_ids_not_by_order():
    raw = _shared_site(
        _member(4, "site-1")
        + _member(5, "gw-2")
        + _member(5, "gw-1")
        + _board("XC-A", links=("site-1", "gw-1"))
        + _board("XC-B", links=("gw-2",))
    )
    assert _pairs(raw) == {"XC-A": "gw-1", "XC-B": "gw-2"}


def test_matching_counts_never_pair_by_order():
    raw = _shared_site(
        _member(4, "site-1")
        + _member(5, "gw-1")
        + _member(5, "gw-2")
        + _board("XC-A")
        + _board("XC-B")
    )
    assert _pairs(raw) == {"XC-A": None, "XC-B": None}


def test_guessed_shape_c_leaves_two_boards_on_one_gateway_unpaired():
    raw = _shared_site(_member(4, "site-1") + _member(5, "gw") + _board("XC-A") + _board("XC-B"))
    assert _pairs(raw) == {"XC-A": None, "XC-B": None}


def test_two_boards_claiming_one_gateway_get_neither():
    raw = _shared_site(
        _member(5, "gw-1")
        + _member(5, "gw-2")
        + _board("XC-A", links=("gw-1",))
        + _board("XC-B", links=("gw-1",))
    )
    assert _pairs(raw) == {"XC-A": None, "XC-B": None}


def test_a_second_site_member_does_not_make_a_second_site():
    (site,) = parse_sites(
        _shared_site(
            _member(4, "site-1")
            + _member(4, "site-2")
            + _member(5, "gw-1")
            + _member(5, "gw-2")
            + _board("XC-A", links=("gw-1",))
            + _board("XC-B", links=("gw-2",))
        )
    )
    assert site.site_id == "site-1"
    assert site.hardware_ids == ("site-1", "site-2", "gw-1", "gw-2")
    assert len(site.panels) == 2


def test_two_panels_on_one_site_have_no_primary():
    raw = _shared_site(
        _member(5, "gw-1")
        + _member(5, "gw-2")
        + _board("XC-A", links=("gw-1",))
        + _board("XC-B", links=("gw-2",))
    )
    assert primary_panel_for(parse_sites(raw)) is None


def test_the_site_label_stays_out_of_its_repr():
    (site,) = parse_sites(_site("s", "XC-1", "1-02100-04", [(4, "s")]))
    assert site.name == "<address>"
    assert "<address>" not in repr(site)


UNPAIRED = _shared_site(
    _member(5, "gw-1") + _member(5, "gw-2") + _board("XC-A") + _board("XC-B"),
)


def _answer(serial: str, *gateways: str) -> bytes:
    return b"".join(_member(5, g) for g in gateways) + _board(serial)


def _lookup(answers: dict[str, bytes], calls: list[bytes] | None = None):
    def call(request: bytes) -> bytes:
        if calls is not None:
            calls.append(request)
        return next(raw for serial, raw in answers.items() if serial.encode() in request)

    return call


def test_a_lookup_naming_one_gateway_pairs_the_board():
    lookup = _lookup({"XC-A": _answer("XC-A", "gw-2"), "XC-B": _answer("XC-B", "gw-1")})
    (site,) = pair_by_lookup(parse_sites(UNPAIRED), lookup)
    assert {p.serial: p.hardware_id for p in site.panels} == {"XC-A": "gw-2", "XC-B": "gw-1"}


def test_a_lookup_returning_the_whole_site_settles_nothing():
    whole = _answer("XC-A", "gw-1", "gw-2") + _board("XC-B")
    (site,) = pair_by_lookup(parse_sites(UNPAIRED), _lookup({"XC": whole}))
    assert all(p.hardware_id is None for p in site.panels)


def test_a_lookup_giving_both_boards_one_gateway_pairs_neither():
    lookup = _lookup({"XC-A": _answer("XC-A", "gw-1"), "XC-B": _answer("XC-B", "gw-1")})
    (site,) = pair_by_lookup(parse_sites(UNPAIRED), lookup)
    assert all(p.hardware_id is None for p in site.panels)


def test_a_lookup_naming_another_panels_board_is_ignored():
    (site,) = pair_by_lookup(parse_sites(UNPAIRED), _lookup({"XC": _answer("XC-Z", "gw-1")}))
    assert all(p.hardware_id is None for p in site.panels)


def test_a_failed_lookup_counts_as_no_answer():
    def fail(request: bytes) -> bytes:
        raise OSError("cloud hiccup")

    (site,) = pair_by_lookup(parse_sites(UNPAIRED), fail)
    assert all(p.hardware_id is None for p in site.panels)


def test_no_lookup_when_the_site_response_already_pairs():
    calls: list[bytes] = []
    pair_by_lookup(parse_sites(SITES), _lookup({}, calls))
    raw = _shared_site(
        _member(5, "gw-1")
        + _member(5, "gw-2")
        + _board("XC-A", links=("gw-1",))
        + _board("XC-B", links=("gw-2",))
    )
    pair_by_lookup(parse_sites(raw), _lookup({}, calls))
    assert calls == []
