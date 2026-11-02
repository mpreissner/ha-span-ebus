"""What feeds each panel: reading, checking and offering the stored answers.

Shapes with several panels on a site are guesses until a real capture exists.
Every id here is invented.
"""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

import pytest
from span_client import backend as cloud
from span_client import feeds
from span_client.cloud_traits import CircuitInfo

A = cloud.PanelInfo("site-1", "XC-AAAA", "1-04100-01", "gw-a")
B = cloud.PanelInfo("site-1", "XC-BBBB", "1-04100-01", "gw-b")
C = cloud.PanelInfo("site-2", "XC-CCCC", "1-04100-01", "gw-c")
SHARED = cloud.SiteInfo("site-1", ("site-1", "gw-a", "gw-b"), (A, B), ("gw-a", "gw-b"))
ALONE = cloud.SiteInfo("site-2", ("site-2", "gw-c"), (C,), ("gw-c",))

UUID = "00000000-0000-4000-8000-000000000001"
OWN = {"type": feeds.OWN_SUPPLY}


def lugs(upstream):
    return {"type": feeds.LUGS, "upstream": upstream}


def test_missing_answer_is_own_supply():
    assert feeds.feed_of({}, "a", ["a", "b"]) == OWN


def test_departed_upstream_is_own_supply():
    assert feeds.feed_of({"a": lugs("gone")}, "a", ["a"]) == OWN


def test_a_panel_cannot_feed_itself():
    assert feeds.feed_of({"a": lugs("a")}, "a", ["a"]) == OWN
    assert "lugs:a" not in feeds.feed_choices("a", {"a": "A", "b": "B"}, OWN)


def test_clean_feeds_drops_unticked_panels_and_what_hung_off_them():
    answers = {"a": OWN, "b": lugs("a"), "c": lugs("b")}
    assert feeds.clean_feeds(answers, ["a", "c"]) == {"a": OWN, "c": OWN}


@pytest.mark.parametrize(
    "answers",
    [
        {"a": lugs("b"), "b": lugs("a")},
        {"a": lugs("b"), "b": lugs("c"), "c": lugs("a")},
        {"x": lugs("a"), "a": lugs("b"), "b": lugs("a")},
    ],
)
def test_cycles_are_found(answers):
    assert feeds.has_cycle(answers)


@pytest.mark.parametrize(
    "answers",
    [
        {"a": OWN, "b": OWN},
        {"a": OWN, "b": lugs("a"), "c": lugs("b")},
        {"a": OWN, "b": lugs("a"), "c": lugs("a")},
    ],
)
def test_chains_and_shared_upstreams_are_fine(answers):
    assert not feeds.has_cycle(answers)


def test_form_values_round_trip():
    branch = {"type": feeds.BRANCH, "upstream": "a:b"}
    assert feeds.from_form(feeds.form_value(branch)) == branch
    assert feeds.from_form(feeds.form_value(OWN)) == OWN


def test_circuit_survives_only_on_the_same_upstream():
    prior = {"type": feeds.BRANCH, "upstream": "a", "circuit": 7}
    assert feeds.from_form("branch:a", prior) == prior
    assert "circuit" not in feeds.from_form("branch:b", prior)
    assert "circuit" not in feeds.from_form("lugs:a", prior)


def test_unfinished_branch_is_shown():
    current = {"type": feeds.BRANCH, "upstream": "a"}
    choices = feeds.feed_choices("b", {"a": "A", "b": "B"}, current)
    assert "not chosen" in choices["branch:a"]
    done = feeds.feed_choices("b", {"a": "A", "b": "B"}, {**current, "circuit": 7})
    assert "not chosen" not in done["branch:a"]


def test_circuits_are_offered_in_panel_order_and_told_apart():
    circuits = {
        3: CircuitInfo(3, "Spare", (9, 11)),
        1: CircuitInfo(1, "Spare", (2,)),
        2: CircuitInfo(2, None),
    }
    assert feeds.circuit_choices(circuits) == {
        "2": "Circuit 2",
        "1": "Spare (2)",
        "3": "Spare (9, 11)",
    }


def test_labels_never_collide():
    other = replace(SHARED, site_id="site-9")
    a, b = [
        replace(s, panels=tuple(replace(p, serial=None) for p in s.panels)) for s in (SHARED, other)
    ]
    labels = cloud.panel_labels([a, b])
    assert len(set(labels.values())) == 2


def test_find_panel_answers_to_any_of_its_ids():
    assert cloud.find_panel([SHARED, ALONE], "gw-c") == (ALONE, C)
    assert cloud.find_panel([SHARED, ALONE], "XC-GONE") is None


class FakeGrpc:
    requests: ClassVar[list[bytes]] = []

    def __init__(self, token):
        assert token == "token"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def ably_token(self, request):
        return b""

    def subscribe_and_get_traits(self, request):
        self.requests.append(request)
        return b"snapshot"


def circuits_of(monkeypatch, site, panel, primary):
    monkeypatch.setattr(cloud.cloud_grpc, "CloudGrpcClient", FakeGrpc)
    FakeGrpc.requests = []
    monkeypatch.setattr(cloud, "parse_trait_snapshot", lambda raw: {"parsed": raw})
    return cloud.panel_circuits(
        "token", site, panel, user_id="user", device_uuid=UUID, primary_panel=primary
    )


def test_circuits_of_a_shared_site_keep_only_that_panel(monkeypatch):
    monkeypatch.setattr(
        cloud, "split_snapshot", lambda raw, routes, unrouted: {"XC-AAAA": b"a", "XC-BBBB": b"b"}
    )
    assert circuits_of(monkeypatch, SHARED, B, "XC-AAAA") == {"parsed": b"b"}
    assert f"c:user:{UUID}".encode() in FakeGrpc.requests[0]


def test_circuits_of_a_lone_panel_are_the_whole_snapshot(monkeypatch):
    assert circuits_of(monkeypatch, ALONE, C, None) == {"parsed": b"snapshot"}
    assert f"c:user:{UUID}".encode() not in FakeGrpc.requests[0]


def test_circuits_need_a_channel(monkeypatch):
    monkeypatch.setattr(cloud.cloud_grpc, "CloudGrpcClient", FakeGrpc)
    with pytest.raises(cloud.cloud_grpc.GrpcError):
        cloud.panel_circuits("token", ALONE, C, user_id=None, device_uuid=UUID, primary_panel=None)
