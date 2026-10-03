"""Multi-site accounts: one panel per SPAN "site", split and streamed separately.

Shapes mirror a real two-site GetSitesForUser response (a MAIN 16 and a MAIN
40 on one account), with every id and serial replaced by a placeholder.
"""

from __future__ import annotations

from span_client.backend import parse_sites
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
