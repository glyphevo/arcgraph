"""A reached inner statement and a builtin guard provide local type evidence."""

import pytest
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_flow_attribute_chains import _calls


def test_nested_loop_exit_proves_a_value_only_inside_the_outer_body(tmp_path):
    calls = _calls(
        tmp_path,
        "class Node:\n    properties: dict[str, list]\ndef use(nodes: dict[str, Node], parts: list[str], rounds: list[int]):\n    for turn in rounds:\n        for key in parts:\n            module=nodes.get(key)\n            if module is not None:\n                break\n        else:\n            return\n        module.properties.get('bindings', [])\n",
    )
    assert calls.get("module.properties.get('bindings', [])") == {
        "extsym:builtins.dict.get"
    }


@pytest.mark.parametrize(
    "type_name,method", [("dict", "get('k')"), ("str", "replace('a', 'b')")]
)
def test_builtin_guard_does_not_treat_an_unknown_branch_as_disjoint(
    tmp_path, type_name, method
):
    literal = "{}" if type_name == "dict" else "'a'"
    calls = _calls(
        tmp_path,
        f"def use(value, flag):\n    if flag:\n        value={literal}\n    if isinstance(value, {type_name}):\n        value.{method}\n",
    )
    assert calls.get(f"value.{method}") == {
        "extsym:builtins." + type_name + "." + method.split("(")[0]
    }


def test_zero_outer_iterations_do_not_inherit_an_inner_settlement(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\nclass Other:\n    def exists(self):pass\ndef use(p: Path, rounds: list[int], values: list[int]):\n    q=Other()\n    for turn in rounds:\n        for item in values:\n            q=p / 'a'\n            break\n        else:\n            return\n    q.exists()\n",
    )
    assert calls.get("q.exists()") == {
        "extsym:pathlib.Path.exists",
        "method:pkg.user.Other.exists",
    }


def test_unknown_branch_without_a_guard_is_not_selected(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(value, flag):\n    if flag:\n        value={}\n    value.get('k')\n",
    )
    assert not calls.get("value.get('k')")


def test_builtin_guard_does_not_reuse_unproved_generic_arguments(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(items: dict[str, Path], other, flag):\n    value=items\n    if flag:\n        value=other\n    if isinstance(value, dict):\n        value['x'].exists()\n",
    )
    assert not calls.get("value['x'].exists()")


def test_replacing_a_non_dict_with_a_dict_proves_the_post_branch_type(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(record: dict[str, object]):\n    properties=record.get('properties', {})\n    if not isinstance(properties, dict):\n        properties={}\n    properties.get('x')\n",
    )
    assert calls.get("properties.get('x')") == {"extsym:builtins.dict.get"}


@pytest.mark.parametrize("replacement", ["other", "[]"])
def test_unknown_or_different_replacement_does_not_prove_the_guard_type(
    tmp_path, replacement
):
    calls = _calls(
        tmp_path,
        f"def use(record: dict[str, object], other):\n    properties=record.get('properties', {{}})\n    if not isinstance(properties, dict):\n        properties={replacement}\n    properties.get('x')\n",
    )
    assert "extsym:builtins.dict.get" not in calls.get("properties.get('x')", set())
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    assert all(
        e.confidence == "heuristic"
        for e in edges
        if e.source == "fn:pkg.user.use"
        and e.properties.get("callsite", {}).get("call_expression")
        == "properties.get('x')"
        and e.resolution.status == "resolved"
    )


def test_post_branch_proof_is_invalidated_by_a_later_loop_write(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(record: dict[str, object], others):\n    properties=record.get('properties', {})\n    if not isinstance(properties, dict):\n        properties={}\n    for item in others:\n        properties=item\n    properties.get('x')\n",
    )
    assert not calls.get("properties.get('x')")


def test_shadowed_dict_does_not_create_a_builtin_post_branch_proof(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(record: dict[str, object], dict):\n    properties=record.get('properties', {})\n    if not isinstance(properties, dict):\n        properties={}\n    properties.get('x')\n",
    )
    assert not calls.get("properties.get('x')")


def test_a_loop_guard_survives_a_method_result_and_subscript(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(values: list):\n    for value in values:\n        if not isinstance(value, str):\n            continue\n        value.split('(', 1)[0].strip().rsplit('.', 1)\n",
    )
    assert calls.get("value.split('(', 1)[0].strip()") == {"extsym:builtins.str.strip"}
    assert calls.get("value.split('(', 1)[0].strip().rsplit('.', 1)") == {
        "extsym:builtins.str.rsplit"
    }


@pytest.mark.parametrize("manager", ["sqlite", "project"])
def test_non_suppressing_manager_preserves_a_completed_assignment(tmp_path, manager):
    if manager == "sqlite":
        source = "import sqlite3\nfrom pathlib import Path\ndef use(p: Path):\n    with sqlite3.connect(':memory:') as connection:\n        q=p / 'a'\n    q.exists()\n"
    else:
        source = "from pathlib import Path\nclass Manager:\n    def __enter__(self):pass\n    def __exit__(self, *args):\n        if args:\n            print('close')\ndef use(p: Path):\n    with Manager():\n        q=p / 'a'\n    q.exists()\n"
    calls = _calls(tmp_path, source)
    assert calls.get("q.exists()") == {"extsym:pathlib.Path.exists"}


@pytest.mark.parametrize("exit_body", ["return True", "return result()"])
def test_a_manager_that_may_suppress_does_not_prove_body_completion(
    tmp_path, exit_body
):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\nclass Manager:\n    def __enter__(self):pass\n    def __exit__(self, *args):"
        + exit_body
        + "\ndef use(p: Path):\n    with Manager():\n        q=p / 'a'\n    q.exists()\n",
    )
    assert not calls.get("q.exists()")


def test_a_later_manager_assignment_does_not_prove_an_earlier_with(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\nclass Safe:\n    def __enter__(self):pass\n    def __exit__(self, *args):pass\nclass Suppress:\n    def __enter__(self):pass\n    def __exit__(self, *args):return True\ndef use(p: Path):\n    manager=Suppress()\n    with manager:\n        q=p / 'a'\n    manager=Safe()\n    q.exists()\n",
    )
    assert not calls.get("q.exists()")


def test_an_unbound_manager_is_not_typed_from_a_future_assignment(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\nclass Safe:\n    def __enter__(self):pass\n    def __exit__(self, *args):pass\ndef use(p: Path):\n    with manager:\n        q=p / 'a'\n    manager=Safe()\n    q.exists()\n",
    )
    assert not calls.get("q.exists()")


def test_normalization_proves_a_class_but_drops_old_element_arguments(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(items: dict[str, Path], other, flag):\n    value=items\n    if flag:\n        value=other\n    if not isinstance(value, dict):\n        value={}\n    value.get('k')\n    value['k'].exists()\n",
    )
    assert calls.get("value.get('k')") == {"extsym:builtins.dict.get"}
    assert not calls.get("value['k'].exists()")


def test_non_suppressing_manager_from_an_owned_typed_field(tmp_path):
    calls = _calls(
        tmp_path,
        "import sqlite3\nfrom pathlib import Path\nclass Store:\n    def connect(self) -> sqlite3.Connection:\n        return sqlite3.connect(':memory:')\nclass Service:\n    def __init__(self, store: Store):self.store=store\n    def run(self, p: Path):\n        with self.store.connect():\n            q=p / 'a'\n        q.exists()\ndef use(p: Path):\n    with Store().connect():\n        q=p / 'a'\n    q.exists()\n",
    )
    assert calls.get("q.exists()") == {"extsym:pathlib.Path.exists"}
    from arcgraph.core.graph_store import GraphStoreReader

    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    assert any(
        e.source == "method:pkg.user.Service.run"
        and e.target == "extsym:pathlib.Path.exists"
        for e in edges
    )
