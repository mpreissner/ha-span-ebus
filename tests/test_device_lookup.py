"""Device lookups pick the newest registry call the running Home Assistant has."""

import ast
from pathlib import Path

from device_lookup import device_by_identifier

COMPONENT = Path(__file__).parent.parent / "custom_components" / "span_ebus"
IDENT = ("span_ebus", "nt-0000-test")


class Old:
    """A registry before 2026.x: only `async_get_device`."""

    def __init__(self, device):
        self.device, self.calls = device, []

    def async_get_device(self, identifiers=None, connections=None):
        self.calls.append(("get_device", identifiers))
        return self.device


class New(Old):
    """A 2026.x registry."""

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        self.calls.append(("by_identifier", identifier, config_entry_id))
        return self.device

    def async_get_devices(self, *, identifiers=None, connections=None, config_entry_id=None):
        self.calls.append(("get_devices", identifiers))
        return [self.device] if self.device is not None else []


def test_new_registry_with_entry():
    reg = New("dev")
    assert device_by_identifier(reg, IDENT, "e1") == "dev"
    assert reg.calls == [("by_identifier", IDENT, "e1")]


def test_new_registry_across_entries():
    reg = New("dev")
    assert device_by_identifier(reg, IDENT) == "dev"
    assert reg.calls == [("get_devices", {IDENT})]
    assert device_by_identifier(New(None), IDENT) is None


def test_old_registry_falls_back():
    reg = Old("dev")
    assert device_by_identifier(reg, IDENT, "e1") == "dev"
    assert device_by_identifier(reg, IDENT) == "dev"
    assert reg.calls == [("get_device", {IDENT}), ("get_device", {IDENT})]


def _calls(source: str) -> list[tuple[str, list[str]]]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            found.append((node.func.attr, [kw.arg for kw in node.keywords if kw.arg]))
    return found


def test_no_deprecated_registry_calls_outside_the_helper():
    """`async_get_device` breaks in HA 2027.8, `merge_identifiers` with it."""
    for path in COMPONENT.rglob("*.py"):
        if path.name == "device_lookup.py":
            continue
        for attr, kwargs in _calls(path.read_text()):
            assert attr != "async_get_device", path.name
            assert "merge_identifiers" not in kwargs, path.name


def _function(name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    tree = ast.parse((COMPONENT / "config_flow.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(name)


def _abort_reasons(node: ast.AST) -> list[str]:
    return [
        kw.value.value
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "async_abort"
        for kw in call.keywords
        if kw.arg == "reason" and isinstance(kw.value, ast.Constant)
    ]


def test_discovery_defers_to_a_cloud_account_entry_before_probing():
    """Discovery stays quiet while a cloud account entry could link the panel."""
    source = ast.unparse(_function("async_step_zeroconf"))
    assert "link_from_cloud_entry" in _abort_reasons(_function("async_step_zeroconf"))
    assert source.index("link_from_cloud_entry") < source.index("probe_panel")
    assert source.index("host_matches_ca") < source.index("link_from_cloud_entry")


def test_link_drops_discovery_cards_after_saving():
    source = ast.unparse(_function("_link_commit"))
    assert source.index("self._committed = True") < source.index("_abort_stale_discoveries")
