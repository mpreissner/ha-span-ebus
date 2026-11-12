"""Tests for the probe tool's redaction, which guards what users share."""

import importlib.util
from pathlib import Path

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
