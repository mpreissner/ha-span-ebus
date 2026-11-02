"""The capture tool: what it records, and what it must never let into a report.

Every id here is invented.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from span_client import backend as cloud
from span_client.cloud_pb import field_message, field_string, field_varint

SPEC = importlib.util.spec_from_file_location(
    "capture_panels", Path(__file__).parents[1] / "tools" / "capture_panels.py"
)
capture = importlib.util.module_from_spec(SPEC)
sys.modules["capture_panels"] = capture
SPEC.loader.exec_module(capture)

GATEWAY = "a1b2c3d4e5f60718"
SITE = "11111111-2222-3333-4444-555555555555"
SERIAL = "XC-1234-56789"
LABEL = "Beach House, 1 Example Road"


def zigzag(value: int) -> int:
    return value * 2 if value >= 0 else -value * 2 - 1


def power(milliwatts: int) -> bytes:
    return field_varint(3, zigzag(milliwatts))


def meter(milliwatts: int) -> bytes:
    return field_message(3, field_message(3, power(milliwatts)))


def frame(*, feeder=5000, feedthrough=-1200, branches=3000, feed=4900, circuit=1500) -> bytes:
    panel = (
        field_message(1, meter(feeder))
        + field_message(2, meter(feedthrough))
        + field_message(3, field_message(2, power(branches)))
        + field_message(4, field_message(2, power(7)))
    )
    block = (
        field_message(1, field_string(1, GATEWAY))
        + field_message(2, field_varint(2, 1) + field_message(14, panel))
        + field_message(
            2, field_varint(2, 30) + field_message(11, field_message(3, power(circuit)))
        )
        + field_message(
            2, field_varint(2, 31) + field_message(11, field_message(3, power(circuit)))
        )
        + field_message(2, field_varint(2, 56) + field_message(11, field_message(3, power(feed))))
    )
    flows = field_message(2, field_message(2, field_message(1, power(feeder))))
    body = flows + field_message(3, block)
    envelope = field_message(3, body)
    return field_message(2, field_message(1, field_string(1, SITE))) + field_message(
        16, field_message(3, envelope)
    )


def recorder() -> capture.Recorder:
    rec = capture.Recorder(capture.Redactor())
    rec.circuits[GATEWAY] = {30: object(), 31: object()}
    return rec


def test_named_panel_readings():
    assert capture.panel_readings(next(capture.panel_bodies(frame()))[2]) == {
        "feeder": 5.0,
        "feedthrough": -1.2,
        "total_branch": 3.0,
        "balance": 0.007,
    }


def test_panel_bodies_names_the_block():
    assert [body[:2] for body in capture.panel_bodies(frame())] == [(GATEWAY, 1)]


def test_raw_leaves_stay_undecoded():
    body = next(capture.panel_bodies(frame()))[2]
    assert dict(capture.leaves(body))["2.3.3.3"] == zigzag(-1200)


def test_recorder_sorts_samples_into_phases():
    rec = recorder()
    rec.phase = "load off"
    rec.add("site 1", frame())
    rec.add("site 1", frame(feedthrough=-3000))
    rec.phase = "load on"
    rec.add("site 1", frame(feedthrough=-9000, circuit=2000))
    rec.phase = None
    rec.add("site 1", frame(feedthrough=-1))

    report = "\n".join(rec.tables(("load off", "load on")))
    placeholder = rec.redact.text(GATEWAY)
    assert f"-- block {placeholder} --" in report
    assert "panel feedthrough | -2.1 [-3.0..-1.2] | -9.0" in report
    assert "sum of circuits | 3.0 | 4.0" in report
    assert "circuits reporting | 2.0 | 2.0" in report
    assert "instance 56 (two_wire), not a circuit | 4.9 | 4.9" in report
    assert "grid | 5.0 | 5.0" in report
    assert GATEWAY not in report
    assert rec.frames == {("site 1", "load off"): 2, ("site 1", "load on"): 1}


def test_a_phase_with_no_frames_shows_as_a_gap():
    rec = recorder()
    rec.phase = "load off"
    rec.add("site 1", frame())
    assert "panel feeder | 5.0 | -" in "\n".join(rec.tables(("load off", "load on")))


def test_a_frame_that_will_not_parse_is_counted_and_survived():
    rec = recorder()
    rec.phase = "load off"
    rec.add("site 1", b"\xff\xff\xff")
    rec.add("site 1", frame())
    assert rec.frames == {("site 1", "load off"): 2, ("site 1", "undecodable"): 1}
    assert "panel feeder | 5.0" in "\n".join(rec.tables(("load off",)))


def test_a_frame_without_blocks_is_harmless():
    rec = recorder()
    rec.phase = "load off"
    rec.add("site 1", field_message(2, field_message(1, field_string(1, SITE))))
    assert rec.frames["site 1", "load off"] == 1
    assert rec.tables(("load off",)) == []


def sites_response() -> bytes:
    member = field_varint(1, 5) + field_string(2, GATEWAY)
    identity = field_string(3, SERIAL) + field_string(5, "1-02100-04")
    board = field_message(4, identity) + field_string(5, "Panelboard")
    group = field_message(1, member) + field_message(2, board)
    return field_message(
        1,
        field_message(1, field_string(1, SITE) + field_string(2, LABEL))
        + field_message(3, group)
        + field_varint(7, 1786904077118),
    )


def test_dump_redacts_every_value_but_keeps_the_shape():
    redact = capture.Redactor()
    report = "\n".join(capture.dump(sites_response(), redact))
    for secret in (GATEWAY, SITE, SERIAL, "Beach", "Example", "1786904077118"):
        assert secret not in report
    assert "1-02100-04" in report
    assert "Panelboard" in report
    assert report.count(redact.text(SITE)) == 1


def test_the_same_id_keeps_its_placeholder_so_links_show():
    redact = capture.Redactor()
    assert redact.text(SITE) == redact.text(SITE)
    assert redact.text(SITE) != redact.text(GATEWAY)


def test_account_lines_hide_the_site_label_and_ids():
    panel = cloud.PanelInfo(SITE, SERIAL, "1-02100-04", GATEWAY)
    site = cloud.SiteInfo(SITE, (SITE, GATEWAY), (panel,), (GATEWAY,), name=LABEL)
    redact = capture.Redactor()
    report = "\n".join(capture.account_lines([site], redact))
    redact.assert_clean(report)
    assert "1 panel(s), 1 gateway(s)" in report
    assert LABEL in redact.seen


def test_a_report_holding_a_private_value_is_refused():
    redact = capture.Redactor()
    redact.text(LABEL)
    with pytest.raises(ValueError, match="text-1"):
        redact.assert_clean(f"all fine until {LABEL}")


def test_a_snapshot_shows_non_circuit_resources_with_labels_redacted():
    key = field_varint(1, 1) + field_varint(3, 99)
    entry = field_message(1, key) + field_string(4, LABEL)
    block = field_message(1, field_string(1, SITE)) + field_message(2, entry)
    redact = capture.Redactor()
    report = "\n".join(capture.snapshot_lines(field_message(1, block), redact))
    assert "0 circuit(s), traits [('1/99', 1)]" in report
    assert LABEL not in report
    assert SITE not in report
    assert capture.resource_circuits(field_message(1, block)) == {SITE: {}}
