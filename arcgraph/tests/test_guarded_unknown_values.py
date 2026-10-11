"""A checked builtin class proves its class, not unknown element arguments."""

import pytest
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_flow_attribute_chains import _calls


@pytest.mark.parametrize(
    "guard",
    [
        "if isinstance(value, dict):\n        value.get('x')",
        "if not isinstance(value, dict):\n        return\n    value.get('x')",
    ],
)
def test_an_unknown_mapping_result_is_proved_by_its_guard(tmp_path, guard):
    calls = _calls(
        tmp_path,
        "from typing import Any\ndef use(record: Any):\n    if not isinstance(record, dict):\n        return\n    value=record.get('value')\n    "
        + guard
        + "\n",
    )
    assert calls.get("value.get('x')") == {"extsym:builtins.dict.get"}
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    assert all(
        e.confidence != "heuristic"
        for e in edges
        if e.source == "fn:pkg.user.use"
        and e.properties.get("callsite", {}).get("call_expression") == "value.get('x')"
        and e.resolution.status == "resolved"
    )


def test_a_checked_unknown_result_can_be_propagated_to_an_alias(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(value: object):\n    if isinstance(value, Path):\n        q=value\n        q.exists()\n",
    )
    assert calls.get("q.exists()") == {"extsym:pathlib.Path.exists"}


def test_checking_a_container_does_not_prove_its_elements(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\nfrom typing import Any\ndef use(record: Any):\n    if not isinstance(record, dict):\n        return\n    value=record.get('value')\n    if isinstance(value, list):\n        value[0].exists()\n",
    )
    assert not calls.get("value[0].exists()")


def test_a_write_after_the_guard_is_not_erased(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(record: dict[str, object], other):\n    value=record.get('value')\n    if isinstance(value, dict):\n        value=other\n        value.get('x')\n",
    )
    assert not calls.get("value.get('x')")


def test_a_guarded_mapping_result_propagates_its_class_to_an_alias(tmp_path):
    calls = _calls(
        tmp_path,
        "from typing import Any\nfrom pathlib import Path\ndef use(record: Any):\n    if not isinstance(record, dict):\n        return\n    value=record.get('value')\n    if isinstance(value, Path):\n        q=value\n        q.exists()\n",
    )
    assert calls.get("q.exists()") == {"extsym:pathlib.Path.exists"}


def test_an_unannotated_guarded_parameter_alias_has_type_evidence(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(value):\n    if isinstance(value, Path):\n        q=value\n        q.exists()\n",
    )
    assert calls.get("q.exists()") == {"extsym:pathlib.Path.exists"}
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    assert all(
        e.confidence != "heuristic"
        for e in edges
        if e.source == "fn:pkg.user.use"
        and e.properties.get("callsite", {}).get("call_expression") == "q.exists()"
        and e.resolution.status == "resolved"
    )


def test_identical_container_classes_with_different_arguments_keep_only_the_class(
    tmp_path,
):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(value: dict[str, Path] | dict[str, str]):\n    if isinstance(value, dict):\n        value.get('x')\n        value['x'].exists()\n    value.get('x').exists()\n    item=value['x']\n    item.exists()\n    alias=value\n    alias['x'].exists()\n",
    )
    assert calls.get("value.get('x')") == {"extsym:builtins.dict.get"}
    assert not calls.get("value['x'].exists()")
    assert not calls.get("value.get('x').exists()")
    assert not calls.get("item.exists()")
    assert not calls.get("alias['x'].exists()")


def test_none_exclusion_keeps_only_the_common_container_class(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(value: dict[str, Path] | dict[str, str] | None):\n    if value is None:\n        return\n    value.get('x')\n    value['x'].exists()\n    value.get('x').exists()\n    item=value['x']\n    item.exists()\n    alias=value\n    alias['x'].exists()\n",
    )
    assert calls.get("value.get('x')") == {"extsym:builtins.dict.get"}
    assert not calls.get("value['x'].exists()")
    assert not calls.get("value.get('x').exists()")
    assert not calls.get("item.exists()")
    assert not calls.get("alias['x'].exists()")
