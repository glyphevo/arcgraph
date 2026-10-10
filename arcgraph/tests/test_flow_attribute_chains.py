"""Attribute receivers retain the same flow evidence as direct receivers."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def _calls(tmp_path, source):
    _targets(tmp_path, {"user": source}, v2=True)
    result = {}
    for edge in GraphStoreReader.from_current(tmp_path / "out").read_edges():
        if edge.source != "fn:pkg.user.use":
            continue
        for site in edge.properties.get("callsites") or [
            edge.properties.get("callsite", {})
        ]:
            if edge.resolution.status == "resolved":
                result.setdefault(site.get("call_expression"), set()).add(edge.target)
    return result


@pytest.mark.parametrize(
    "annotation,body,expression,target",
    [
        (
            "object",
            "if isinstance(p, Path):\n        p.exists()\n        p.parent.exists()",
            "p.parent.exists()",
            "extsym:pathlib.Path.exists",
        ),
        (
            "Path | None",
            "if p is not None:\n        p.mkdir()\n        p.parent.mkdir()",
            "p.parent.mkdir()",
            "extsym:pathlib.Path.mkdir",
        ),
        (
            "Path | None",
            "if p is None:\n        return\n    p.mkdir()\n    p.parent.mkdir()",
            "p.parent.mkdir()",
            "extsym:pathlib.Path.mkdir",
        ),
        (
            "ast.stmt",
            "if isinstance(p, ast.ImportFrom):\n        p.module.startswith('d')",
            "p.module.startswith('d')",
            "extsym:builtins.str.startswith",
        ),
    ],
)
def test_narrowed_receiver_survives_attribute_chain(
    tmp_path, annotation, body, expression, target
):
    calls = _calls(
        tmp_path,
        f"import ast\nfrom pathlib import Path\ndef use(p: {annotation}):\n    {body}\n",
    )
    assert calls.get(expression) == {target}
    if "p.exists()" in body:
        assert calls.get("p.exists()") == {"extsym:pathlib.Path.exists"}
    if "p.mkdir()" in body:
        assert calls.get("p.mkdir()") == {"extsym:pathlib.Path.mkdir"}


@pytest.mark.parametrize(
    "body",
    [
        "p.parent.exists()",
        "if isinstance(p, str):\n        p.parent.exists()",
        "if isinstance(p, Path):\n        p = object()\n        p.parent.exists()",
        "if isinstance(p, Path):\n        pass\n    p.parent.exists()",
    ],
)
def test_unproved_or_invalidated_path_chain_is_not_guessed(tmp_path, body):
    calls = _calls(
        tmp_path, "from pathlib import Path\ndef use(p: object):\n    " + body + "\n"
    )
    assert "p.parent.exists()" not in calls


@pytest.mark.parametrize("assigned", [False, True])
def test_unchecked_optional_chain_follows_nullable_receiver_model(tmp_path, assigned):
    # Existing propagation permits the sole non-None type when assigned, but
    # an unchecked inline attribute chain still requires narrowing.
    body = "q=p.parent\n    q.mkdir()" if assigned else "p.parent.mkdir()"
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path | None):\n    " + body + "\n",
    )
    assert calls.get("q.mkdir()" if assigned else "p.parent.mkdir()") == (
        {"extsym:pathlib.Path.mkdir"} if assigned else None
    )


@pytest.mark.parametrize("assigned", [False, True])
@pytest.mark.parametrize("block", ["for x in xs:", "for x in xs:\n        try:"])
def test_loop_documented_value_is_available_inside_its_region(
    tmp_path, assigned, block
):
    indent = "            " if "try" in block else "        "
    tail = "\n        except ValueError:\n            pass" if "try" in block else ""
    body = (
        "q.name.endswith('x')"
        if not assigned
        else "n=q.name\n" + indent + "n.endswith('x')"
    )
    source = (
        "from pathlib import Path\ndef use(p: Path, xs: list[int]):\n    "
        + block
        + "\n"
        + indent
        + "q=p/'a'\n"
        + indent
        + "q.exists()\n"
        + indent
        + body
        + tail
        + "\n"
    )
    calls = _calls(tmp_path, source)
    assert calls.get("q.exists()") == {"extsym:pathlib.Path.exists"}
    assert calls.get("n.endswith('x')" if assigned else "q.name.endswith('x')") == {
        "extsym:builtins.str.endswith"
    }


def test_loop_try_text_result_is_not_a_missing_method(tmp_path):
    calls = _calls(
        tmp_path,
        "def replace_params() -> tuple[str, dict]: return '', {}\ndef use(xs: list[int], flag: bool):\n    if flag:\n        path_prefix, path_params = replace_params()\n        for path in xs:\n            try:\n                path_prefix.rstrip('/')\n            except ValueError:\n                pass\n",
    )
    assert calls.get("path_prefix.rstrip('/')") == {"extsym:builtins.str.rstrip"}


@pytest.mark.parametrize(
    "body",
    [
        "for x in xs:\n        q=p/'a'\n    q.name.endswith('x')",
        "for x in xs:\n        q=p/'a'\n        q=1\n        q.name.endswith('x')",
        "for x in xs:\n        q=p/'a'\n        del q\n        q.name.endswith('x')",
    ],
)
def test_loop_evidence_does_not_escape_or_survive_unknown_writes(tmp_path, body):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path, xs: list[int]):\n    "
        + body
        + "\n",
    )
    assert "q.name.endswith('x')" not in calls


def test_enum_value_is_not_promised_as_text(tmp_path):
    calls = _calls(
        tmp_path,
        "from enum import Enum\nclass Kind(Enum): A=1\ndef use(k: Kind):\n    k.name.replace('A','B')\n    k.value.replace('A','B')\n",
    )
    assert calls.get("k.name.replace('A', 'B')") == {"extsym:builtins.str.replace"}
    assert "k.value.replace('A', 'B')" not in calls


def test_own_async_property_shadows_constructor_field(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\nclass Dirs:\n    def __init__(self): self.temp=Path('.')\n    @property\n    async def temp(self): return Path('.')\ndef use(d: Dirs): d.temp.mkdir()\n",
    )
    assert "d.temp.mkdir()" not in calls


@pytest.mark.parametrize(
    "prefix",
    [
        "def isinstance(*args): return True\n",
        "from pkg.other import *\n",
        "Path = str\n",
    ],
)
def test_shadowed_guard_or_class_does_not_prove_path(tmp_path, prefix):
    _targets(
        tmp_path,
        {
            "other": "isinstance = lambda *args: True\n",
            "user": "from pathlib import Path\n"
            + prefix
            + "def use(p: object):\n    if isinstance(p, Path):\n        p.parent.exists()\n",
        },
        v2=True,
    )
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    assert not any(
        e.source == "fn:pkg.user.use" and e.target == "extsym:pathlib.Path.exists"
        for e in edges
    )


def test_direct_narrowed_receiver_cannot_fall_back_to_unrelated_method(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(p: object):\n    if isinstance(p, str):\n        p.exists()\n",
    )
    assert "p.exists()" not in calls


@pytest.mark.parametrize(
    "body",
    [
        "if isinstance(p, Path):\n        if flag: p = object()\n        p.parent.exists()",
        "if isinstance(p, Path):\n        del p\n        p.parent.exists()",
        "if isinstance(p, Path):\n        def change():\n            nonlocal p\n            p = object()\n        change()\n        p.parent.exists()",
    ],
)
def test_guard_does_not_survive_possible_write_delete_or_captured_mutation(
    tmp_path, body
):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: object, flag: bool):\n    " + body + "\n",
    )
    assert "p.parent.exists()" not in calls


def test_guard_on_mixed_union_does_not_narrow_other_branch(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path | str):\n    if isinstance(p, Path):\n        p.parent.exists()\n    else:\n        p.parent.mkdir()\n",
    )
    assert calls.get("p.parent.exists()") == {"extsym:pathlib.Path.exists"}
    assert "p.parent.mkdir()" not in calls


@pytest.mark.parametrize(
    "body",
    [
        "if flag:\n        q=p/'a'\n    else:\n        q.name.endswith('x')",
        "for x in xs:\n        n=q.name\n        n=1\n        n.endswith('x')",
    ],
)
def test_derived_value_does_not_cross_sibling_block_or_unknown_write(tmp_path, body):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path, xs: list[int], flag: bool):\n    q=p\n    "
        + body
        + "\n",
    )
    assert "q.name.endswith('x')" not in calls
    assert "n.endswith('x')" not in calls


def test_missing_attribute_and_distinct_conditional_types_stay_rejected(tmp_path):
    calls = _calls(
        tmp_path,
        "class Matcher:\n    def find(self, row): pass\nclass Other:\n    def find(self, row): pass\nclass Box:\n    def __init__(self): self.matcher=Matcher()\n    def method(self, flag):\n        if flag: self.matcher=Other()\n        self.matcher.find('x')\ndef use(b: Box): b.matcher.inner.find('x')\n",
    )
    assert "b.matcher.inner.find('x')" not in calls
    assert not any(
        e.source == "method:pkg.user.Box.method" and e.kind == "calls"
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
    )


@pytest.mark.parametrize(
    "body",
    [
        "p = other\n    if isinstance(p, Path):\n        p.exists()\n        p.parent.exists()",
        "if flag: p = other\n    if isinstance(p, Path):\n        p.exists()\n        p.parent.exists()",
    ],
)
def test_guard_proves_current_value_after_earlier_writes(tmp_path, body):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: object, other: object, flag: bool):\n    "
        + body
        + "\n",
    )
    assert calls.get("p.exists()") == {"extsym:pathlib.Path.exists"}
    assert calls.get("p.parent.exists()") == {"extsym:pathlib.Path.exists"}


@pytest.mark.parametrize(
    "body",
    [
        "[p.parent.exists() for p in xs]",
        "[p.parent.exists() for p in unknown]",
    ],
)
def test_comprehension_shadow_does_not_inherit_outer_guard(tmp_path, body):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: object, xs: list[str], unknown):\n    if isinstance(p, Path):\n        "
        + body
        + "\n",
    )
    assert "p.parent.exists()" not in calls


@pytest.mark.parametrize("guarded", [False, True])
def test_loop_optional_result_requires_nonempty_evidence(tmp_path, guarded):
    body = (
        "if q is not None:\n            q.name.endswith('x')"
        if guarded
        else "q.name.endswith('x')"
    )
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef pair(flag: bool) -> tuple[Path | None, int]: return (Path('.') if flag else None), 1\ndef use(xs: list[int], flag: bool):\n    for x in xs:\n        q, count=pair(flag)\n        "
        + body
        + "\n",
    )
    assert calls.get("q.name.endswith('x')") == (
        {"extsym:builtins.str.endswith"} if guarded else None
    )


@pytest.mark.parametrize(
    "guard", ["parent is None or name == 'skip'", "parent is None"]
)
def test_loop_mapping_value_chain_after_none_or_break(tmp_path, guard):
    calls = _calls(
        tmp_path,
        "class Node:\n    properties: dict[str, int]\ndef use(scopes: dict[str, Node] | None, names: list[str]):\n    name='x'\n    while names and scopes:\n        parent=scopes.get(name)\n        if "
        + guard
        + ":\n            break\n        parent.properties.get('x')\n",
    )
    assert calls.get("parent.properties.get('x')") == {"extsym:builtins.dict.get"}


def test_none_guard_keeps_container_element_types(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(paths: dict[str, Path] | None):\n    if paths is not None:\n        paths['x'].parent.mkdir()\n",
    )
    assert calls.get("paths['x'].parent.mkdir()") == {"extsym:pathlib.Path.mkdir"}


def test_outer_guard_is_invalid_inside_a_rebinding_loop(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: object, xs: list[int]):\n    if isinstance(p, Path):\n        for x in xs:\n            p.parent.exists()\n            p = object()\n",
    )
    assert "p.parent.exists()" not in calls


@pytest.mark.parametrize(
    "body",
    [
        "if not isinstance(value, str) or not value:\n        return\n    value.rsplit('.', 1)",
        "if value is None:\n        return\n    text=value.decode('utf-8') if isinstance(value, bytes) else str(value)",
    ],
)
def test_none_exclusion_of_any_does_not_hide_existing_type_evidence(tmp_path, body):
    calls = _calls(
        tmp_path,
        "from typing import Any\ndef use(payload: dict[str, Any]):\n    value=payload.get('value')\n    "
        + body
        + "\n",
    )
    expression = "value.rsplit('.', 1)" if "rsplit" in body else "value.decode('utf-8')"
    expected = (
        "extsym:builtins.str.rsplit"
        if "rsplit" in body
        else "extsym:builtins.bytes.decode"
    )
    assert calls.get(expression) == {expected}


def test_none_branch_summary_does_not_hide_a_nullable_text_result(tmp_path):
    calls = _calls(
        tmp_path,
        "def text() -> str | None: return 'a.b'\ndef use(flag: bool):\n    if flag: value=text()\n    else: value=None\n    if value:\n        value.rsplit('.', 1)\n",
    )
    assert calls.get("value.rsplit('.', 1)") == {"extsym:builtins.str.rsplit"}
