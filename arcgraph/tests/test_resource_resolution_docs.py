"""The shipped resolver descriptions include the resource exception and bound."""

from pathlib import Path

import pytest

from arcgraph.interfaces.docs import render_docs


@pytest.mark.parametrize("topic", ["cli-reference", "change-preflight"])
def test_builtin_resource_resolution_rule(topic: str) -> None:
    text = render_docs(topic)
    assert "Resource display names" in text
    assert "pinned before truncation" in text
    assert "candidate limit remains 8" in text
    assert "Qualified resource names" in text


def test_readme_and_preflight_explain_resource_pinning() -> None:
    root = Path(__file__).resolve().parents[2]
    for path in [root / "README.md", root / "docs/change-preflight.md"]:
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "Resource display names" in text
        assert "pinned before truncation" in text
        assert "candidate limit remains 8" in text
        assert "Qualified resource names" in text
