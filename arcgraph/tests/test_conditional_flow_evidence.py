"""Known values retain guards after conditional writes without hiding unknowns."""

import pytest
from arcgraph.tests.test_flow_attribute_chains import _calls


@pytest.mark.parametrize(
    "body",
    [
        "for key in parts:\n        module = nodes.get(key)\n        if module is not None:\n            break\n    else:\n        return None\n    module.properties.get('bindings', [])",
        "for key in parts:\n        module = nodes.get(key)\n        if module is None:\n            continue\n        module.properties.get('bindings', [])",
    ],
)
def test_loop_optional_node_guard_keeps_property_dictionary(tmp_path, body):
    source = (
        "class Node:\n    properties: dict[str, list]\ndef use(nodes: dict[str, Node], parts: list[str]):\n    "
        + body
        + "\n"
    )
    calls = _calls(tmp_path, source)
    assert calls.get("module.properties.get('bindings', [])") == {
        "extsym:builtins.dict.get"
    }


@pytest.mark.parametrize(
    "body",
    [
        "for x in xs:\n        q=p / 'a'\n    q.exists()",
        "q=p / 'a'\n    if flag:\n        q=unknown\n    q.exists()",
        "q=p / 'a'\n    for x in xs:\n        q=unknown\n    q.exists()",
    ],
)
def test_conditional_or_loop_unknown_entry_is_not_discarded(tmp_path, body):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path, xs: list, flag, unknown):\n    "
        + body
        + "\n",
    )
    assert "q.exists()" not in calls


def test_unknown_write_before_loop_break_is_not_skipped(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path, xs: list, unknown):\n    for x in xs:\n        q = p / 'a'\n        q = unknown\n        if x:\n            break\n    else:\n        return\n    q.exists()\n",
    )
    assert "q.exists()" not in calls


def test_break_before_assignment_does_not_prove_a_loop_value(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path, xs: list, flag):\n    for x in xs:\n        if flag:\n            break\n        q = p / 'a'\n        break\n    else:\n        return\n    q.exists()\n",
    )
    assert "q.exists()" not in calls


def test_exhaustive_string_branches_keep_literal_ternary_type(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(prefix: str, pattern: str):\n    if not prefix:\n        result = f'/{pattern}' if pattern else '/'\n    elif not pattern:\n        result = f'/{prefix}'\n    else:\n        result = f'/{prefix}/{pattern}'\n    result.replace('//', '/')\n",
    )
    assert calls.get("result.replace('//', '/')") == {"extsym:builtins.str.replace"}


def test_literal_ternary_does_not_hide_an_unknown_arm(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(flag, other):\n    result = 'init'\n    result = 'text' if flag else other\n    result.replace('a', 'b')\n",
    )
    assert "result.replace('a', 'b')" not in calls


def test_literal_ternary_different_types_are_not_selected(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(flag):\n    result = 'init'\n    result = 'text' if flag else 1\n    result.replace('a', 'b')\n",
    )
    assert "result.replace('a', 'b')" not in calls
