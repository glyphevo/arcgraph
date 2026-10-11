"""A weaker presence test does not cancel a surrounding concrete guard."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def targets(tmp_path, body, nullable=False):
    _targets(
        tmp_path,
        {
            "user": "from collections.abc import Sequence\ndef use(raw: str | Sequence[str]"
            + (" | None" if nullable else "")
            + "):\n    "
            + body
            + "\n"
        },
        v2=True,
    )
    return {
        e.target
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use" and e.confidence != "heuristic"
    }


@pytest.mark.parametrize("nullable", [False, True])
def test_presence_expression_keeps_the_outer_isinstance_element_type(
    tmp_path, nullable
):
    assert "extsym:builtins.str.strip" in targets(
        tmp_path,
        'if isinstance(raw, str):\n        raw = [pattern.strip() for pattern in raw.split(",")] if raw else ()',
        nullable,
    )


@pytest.mark.parametrize(
    "body",
    [
        'return [pattern.strip() for pattern in raw.split(",")] if raw else ()',
        'if isinstance(raw, str):\n        raw = unknown\n        return [pattern.strip() for pattern in raw.split(",")] if raw else ()',
    ],
)
def test_presence_alone_and_an_invalidated_outer_guard_do_not_select_str(
    tmp_path, body
):
    assert "extsym:builtins.str.strip" not in targets(tmp_path, body)
