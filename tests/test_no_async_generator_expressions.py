"""An `await` inside a generator expression makes it an async generator,
which str.join, list(), any() and the like can't consume: the options
Controls step failed this way with "can only join an iterable".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "span_ebus"
SOURCES = sorted(PACKAGE.rglob("*.py"))


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_no_await_in_generator_expression(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.GeneratorExp)
        and any(isinstance(inner, ast.Await) for inner in ast.walk(node))
    ]
    assert not found, f"await inside a generator expression at lines {found}"
