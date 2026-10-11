"""Expression guards apply only after the guarding operand has evaluated."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def typed_calls(tmp_path, body):
    _targets(
        tmp_path,
        {
            "user": "from pathlib import Path\ndef use(value: object):\n    "
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


@pytest.mark.parametrize(
    "body,target",
    [
        (
            'return not isinstance(value, str) or value.startswith("a")',
            "extsym:builtins.str.startswith",
        ),
        (
            'return None if not isinstance(value, dict) else value.get("a")',
            "extsym:builtins.dict.get",
        ),
        (
            'value = value if isinstance(value, dict) else {}\n    return value.get("a")',
            "extsym:builtins.dict.get",
        ),
        (
            'return isinstance(value, str) and value.startswith("a")',
            "extsym:builtins.str.startswith",
        ),
        (
            'return value.get("a") if isinstance(value, dict) else None',
            "extsym:builtins.dict.get",
        ),
        (
            "return isinstance(value, Path) and value.parent.exists()",
            "extsym:pathlib.Path.exists",
        ),
        (
            'return isinstance(value, dict) and value and value.get("a")',
            "extsym:builtins.dict.get",
        ),
    ],
)
def test_short_circuit_and_conditional_expression_use_the_guard(tmp_path, body, target):
    assert target in typed_calls(tmp_path, body)


@pytest.mark.parametrize(
    "body",
    [
        "return value.parent.exists() and isinstance(value, Path)",
        "return isinstance(value, str) and value.parent.exists()",
        "return isinstance(value, Path) or value.parent.exists()",
        "return value.parent.exists() if isinstance(value, str) else None",
        "return None if isinstance(value, Path) else value.parent.exists()",
        "return isinstance(value, Path) and (value := object()) and value.parent.exists()",
    ],
)
def test_the_other_operand_or_an_invalidated_guard_cannot_prove_path(tmp_path, body):
    assert "extsym:pathlib.Path.exists" not in typed_calls(tmp_path, body)


@pytest.mark.parametrize(
    "body",
    [
        'value = value if isinstance(value, dict) else []\n    return value.get("a")',
        'dict = str\n    value = value if isinstance(value, dict) else {}\n    return value.get("a")',
        'isinstance = lambda *args: True\n    value = value if isinstance(value, dict) else {}\n    return value.get("a")',
    ],
)
def test_normalization_requires_the_real_guard_and_matching_fallback(tmp_path, body):
    assert "extsym:builtins.dict.get" not in typed_calls(tmp_path, body)
