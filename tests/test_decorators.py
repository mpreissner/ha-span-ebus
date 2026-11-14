"""Static checks over the integration's decorators."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "custom_components"


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def stacked_on_property(source: str) -> list[int]:
    """Line numbers where ``@callback`` sits directly above ``@property``."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        names = [_name(d) for d in node.decorator_list]
        for upper, lower, dec in zip(names, names[1:], node.decorator_list, strict=False):
            if upper == "callback" and lower == "property":
                found.append(dec.lineno)
    return found


def test_the_check_catches_callback_on_property():
    source = "class A:\n    @callback\n    @property\n    def x(self):\n        return 1\n"
    assert stacked_on_property(source) == [2]
    assert stacked_on_property(source.replace("@callback\n    ", "")) == []


def test_no_callback_stacked_on_property():
    bad = []
    for path in sorted(ROOT.rglob("*.py")):
        bad += [f"{path.relative_to(ROOT)}:{n}" for n in stacked_on_property(path.read_text())]
    assert bad == []
