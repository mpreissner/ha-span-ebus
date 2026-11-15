"""Which discovered panels the add-panel picker offers."""

from span_client.local.discovery import Candidate, instance_name, offerable


def test_instance_name():
    assert instance_name("span-abc._ebus._tcp.local.") == "span-abc"
    assert instance_name("odd") == "odd"


def test_label():
    assert Candidate("10.0.0.5", "span-abc").label == "span-abc (10.0.0.5)"
    assert Candidate("10.0.0.5", "").label == "10.0.0.5"


def test_drops_configured_and_duplicates():
    found = [
        Candidate("10.0.0.9", "zeta"),
        Candidate("10.0.0.5", "alpha"),  # browse sighting, no serial
        Candidate("10.0.0.5", "alpha", serial="s-1"),  # flow sighting, probed
        Candidate("10.0.0.6", "beta", serial="s-2"),  # configured by serial
        Candidate("10.0.0.7", "gamma"),  # configured by host
        Candidate("10.0.0.8", "alpha-2", serial="s-1"),  # same panel, second address
    ]
    kept = offerable(found, configured_hosts=["10.0.0.7"], configured_serials=["s-2"])
    assert kept == [Candidate("10.0.0.5", "alpha", serial="s-1"), Candidate("10.0.0.9", "zeta")]


def test_nothing_found():
    assert offerable([], configured_hosts=[], configured_serials=[]) == []
