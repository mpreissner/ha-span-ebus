"""Tests for the probe tool's redaction, which guards what users share."""

import importlib.util
from pathlib import Path

import pytest
from span_client.local import homie

_PATH = Path(__file__).resolve().parents[1] / "tools" / "probe_local.py"
_spec = importlib.util.spec_from_file_location("probe_local", _PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)  # type: ignore[union-attr]


def test_redact_ignores_case_and_prefers_longer_keys():
    text = "ebus/5/NT-1234-ABCDE-lugs-up and nt-1234-abcde"
    out = probe.redact(text, {"nt-1234-abcde-lugs-up": "lugs-01", "nt-1234-abcde": "panel"})
    assert out == "ebus/5/lugs-01 and panel"


def test_redact_does_not_rewrite_its_own_placeholders():
    assert probe.redact("aa bb", {"aa": "bb", "bb": "cc"}) == "bb cc"


def test_placeholders_cover_value_only_devices():
    tree = homie.DeviceTree()
    tree.apply(
        "ebus/5/root-id/$description",
        b'{"type": "energy.ebus.device.distribution-enclosure", "children": ["kid"]}',
    )
    tree.apply("ebus/5/kid/$description", b'{"type": "energy.ebus.device.circuit"}')
    tree.apply("ebus/5/orphan-id/meter/active-power", b"5")
    names = probe.placeholders(tree)
    assert names == {"root-id": "panel", "kid": "circuit-01", "orphan-id": "unknown-01"}


def test_redact_whole_tokens_only():
    out = probe.redact("ab12 ab123 x-ab12-y", {"ab12": "circuit-02"})
    assert out == "circuit-02 ab123 x-circuit-02-y"


def test_location_properties_are_identifying():
    for key in ("info/latitude", "info/longitude", "info/locality", "status/postal-code"):
        assert probe.is_identifying(key)
    assert not probe.is_identifying("meter/active-power")


@pytest.mark.parametrize(
    ("key", "root", "identifying"),
    [
        ("info/city", False, True),
        ("info/ip-address", False, True),
        ("info/nameplate-capacity", False, False),
        ("info/mac", False, True),
        ("info/machine-type", False, False),
        ("info/name", True, True),
        ("info/name", False, False),
        ("status/time-zone", True, True),
        ("info/serial-number", False, True),  # a battery's or charger's own serial
        ("shed-forecast/confidence", True, False),
    ],
)
def test_identifying_matches_whole_words(key, root, identifying):
    assert probe.is_identifying(key, root=root) is identifying


def test_referenced_ids_split_lists():
    messages = {
        "ebus/5/lugs-1/connection/fed-by-device-id": b"other-panel",
        "ebus/5/panel/bess/grid-forming-entity": b"bess-a, bess-b",
        "ebus/5/panel/connection/feeds-device-ids": b"x1,x2",
        "ebus/5/panel/info/name": b"Home",
    }
    assert probe.referenced_ids(messages) == {"other-panel", "bess-a", "bess-b", "x1", "x2"}


def test_survivors_catch_ids_glued_to_text():
    assert probe.survivors("prefixnt-1234-abcdsuffix", {"NT-1234-ABCD"}) == 1
    assert probe.survivors("clean", {"NT-1234-ABCD"}) == 0
