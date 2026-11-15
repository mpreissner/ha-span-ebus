"""The Homie parser against a real, redacted r202639 capture (MAIN 40, 29
circuits, no battery or PV). Regenerate with tools/probe_local.py.
"""

import json
from pathlib import Path

from span_client.local import homie
from span_client.local.homie import DeviceState

FIXTURES = Path(__file__).parent / "fixtures" / "r202639"
PANEL = "nt-0000-test"


def capture_tree() -> homie.DeviceTree:
    tree = homie.DeviceTree()
    for topic, payload in json.loads((FIXTURES / "capture.json").read_text()).items():
        tree.apply(topic, payload.encode())
    return tree


def test_every_device_hangs_off_the_panel():
    tree = capture_tree()
    assert tree.roots() == [PANEL]
    assert len(list(tree.walk(PANEL))) == len(tree.descriptions) == 32
    assert all(tree.effective_state(d) is DeviceState.READY for d in tree.descriptions)


def test_every_value_has_a_described_property():
    tree = capture_tree()
    for device_id, description in tree.descriptions.items():
        assert set(tree.values(device_id)) <= set(description.properties), device_id


def test_classes_match_the_schema():
    tree = capture_tree()
    schema = json.loads((FIXTURES / "schema.json").read_text())
    classes = {d.device_class for d in tree.descriptions.values()}
    assert classes == {"distribution-enclosure", "circuit", "lugs"}
    assert classes <= set(schema["deviceClasses"])


def test_circuit_values_decode_to_python_types():
    values = capture_tree().values("circuit-01")
    assert isinstance(values["meter/active-power"], float)
    assert isinstance(values["breaker/rating"], int)
    assert values["switch/relay"] in {"OPEN", "CLOSED"}
    assert values["switch/relay-controllable"] is True


def test_lugs_directions():
    tree = capture_tree()
    directions = {tree.value(d, "info/direction") for d in ("lugs-01", "lugs-02")}
    assert directions == {"UPSTREAM", "DOWNSTREAM"}
