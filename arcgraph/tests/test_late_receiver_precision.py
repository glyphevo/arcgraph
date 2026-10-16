"""Late inference must keep exact tuple elements and guarded fallback types."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets

PREFIX = """class Provider:
    def lookup(self) -> tuple[str, list[dict[str, object]], int | None] | None:
        return ('ready', [{}], None)
    def text(self) -> str | None:
        return 'prefix-value'
class Holder:
    def __init__(self):
        self.provider = Provider()
"""


def uses(tmp_path, body):
    _targets(tmp_path, {"user": PREFIX + body}, v2=True)
    return [
        edge
        for edge in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if edge.source == "fn:pkg.user.use"
        and edge.resolution.status == "resolved"
        and edge.confidence != "heuristic"
    ]


def receiver_at_use(tmp_path, expression):
    from arcgraph.analyzers.calls.analyzer import CallAnalyzer
    from arcgraph.analyzers.calls.context import _CallResolutionContext

    reader = GraphStoreReader.from_current(tmp_path / "out")
    context = _CallResolutionContext(list(reader.read_nodes()))
    source = context.by_id["fn:pkg.user.use"]
    fact = next(c for c in source.properties["callsites"] if c["name"] == "rpartition")
    context.callsite_position = (source.id, fact["line"], fact["column"])
    return CallAnalyzer(enable_v2=True)._receiver_type_ref(source, expression, context)


def test_guarded_late_optional_tuple_keeps_the_selected_dictionary(tmp_path):
    edges = uses(
        tmp_path,
        "def use(holder: Holder):\n"
        "    result = holder.provider.lookup()\n"
        "    if not result: return\n"
        "    entry = result[1][0]\n"
        "    entry.get('key')\n",
    )
    assert "extsym:builtins.dict.get" in {edge.target for edge in edges}
    assert "extsym:collections.abc.Mapping.get" not in {edge.target for edge in edges}


def test_nullable_factory_default_and_same_block_local_keep_str(tmp_path):
    edges = uses(
        tmp_path,
        "def use(holder: Holder):\n"
        "    value = holder.provider.text() or ''\n"
        "    if value.startswith('prefix-'):\n"
        "        tail = value.removeprefix('prefix-')\n"
        "        tail.split('-')\n",
    )
    assert "extsym:builtins.str.split" in {edge.target for edge in edges}
    assert "extsym:builtins.bytes.split" not in {edge.target for edge in edges}


@pytest.mark.parametrize(
    "index,method,owner",
    [(0, "upper", "str"), (1, "clear", "list"), (-2, "clear", "list")],
)
def test_constant_tuple_index_selects_one_member(tmp_path, index, method, owner):
    edges = uses(
        tmp_path,
        "def use(values: tuple[str, list[int], dict[str, int]]):\n"
        f"    selected = values[{index}]\n"
        f"    selected.{method}()\n",
    )
    assert f"extsym:builtins.{owner}.{method}" in {edge.target for edge in edges}


@pytest.mark.parametrize(
    "body",
    [
        "    result = holder.provider.lookup()\n    entry = result[1][0]\n    entry.get('key')\n",
        "    result = holder.provider.lookup()\n    if result:\n        entry = result[1][0]\n    entry.get('key')\n",
        "    result = holder.provider.lookup()\n    if not result: return\n    entry = result[1][0]\n    entry = other\n    entry.get('key')\n",
        "    values = ('text', [])\n    selected = values[index]\n    selected.get('key')\n",
    ],
)
def test_optional_conditional_rebound_and_dynamic_index_stay_unproved(tmp_path, body):
    edges = uses(tmp_path, "def use(holder: Holder, other, index):\n" + body)
    assert "extsym:builtins.dict.get" not in {edge.target for edge in edges}


def test_optional_cached_initializer_is_narrowed_at_each_use(tmp_path):
    edges = uses(
        tmp_path,
        "def use(holder: Holder):\n"
        "    result = holder.provider.lookup()\n"
        "    if result:\n"
        "        result[1][0].get('inside')\n"
        "    result[1][0].get('outside')\n",
    )
    strong = [edge for edge in edges if edge.target == "extsym:builtins.dict.get"]
    assert strong
    assert {e.start_line for edge in strong for e in edge.evidence} == {
        len(PREFIX.splitlines()) + 4
    }


def test_unknown_argument_does_not_invalidate_a_declared_return(tmp_path):
    edges = uses(
        tmp_path,
        "def make(value) -> str: return str(value)\n"
        "def use(value):\n"
        "    text = make(value)\n"
        "    text.upper()\n",
    )
    assert "extsym:builtins.str.upper" in {edge.target for edge in edges}


def test_unknown_receiver_cannot_promote_a_name_affine_method_return(tmp_path):
    edges = uses(
        tmp_path,
        "class Worker:\n    def make(self) -> str: return ''\n"
        "def use(worker):\n"
        "    text = worker.make()\n"
        "    text.upper()\n",
    )
    assert "extsym:builtins.str.upper" not in {edge.target for edge in edges}


@pytest.mark.parametrize(
    "guard", ["pair[1]", "pair[1] is not None", "pair and pair[0] == 'tag' and pair[1]"]
)
def test_fixed_tuple_element_presence_guard_proves_its_own_member(tmp_path, guard):
    edges = uses(
        tmp_path,
        "def use(pair: tuple[str, str | None]):\n"
        f"    if {guard}:\n"
        "        pair[1].rpartition('.')\n",
    )
    assert "extsym:builtins.str.rpartition" in {edge.target for edge in edges}
    ref = receiver_at_use(tmp_path, "pair[1]")
    assert ref and "narrowed_receiver" in ref


@pytest.mark.parametrize(
    "body",
    [
        "    if pair[0]:\n        pair[1].rpartition('.')\n",
        "    if pair[1] or enabled:\n        pair[1].rpartition('.')\n",
        "    if pair[1]:\n        pair = other\n        pair[1].rpartition('.')\n",
        "    if pair[1]:\n        for item in other:\n            pair[1].rpartition('.')\n            pair = item\n",
        "    if pair[index]:\n        pair[1].rpartition('.')\n",
        "    if pair[1]:\n        def change():\n            nonlocal pair\n            pair = other\n        pair[1].rpartition('.')\n",
    ],
)
def test_tuple_element_guard_does_not_survive_other_branches_or_writes(tmp_path, body):
    uses(
        tmp_path,
        "def use(pair: tuple[str, str | None], other, enabled, index):\n" + body,
    )
    # Existing nullable-container summaries can retain a legacy target. The
    # new element guard must supply no narrowing proof in these cases.
    ref = receiver_at_use(tmp_path, "pair[1]")
    assert not ref or "narrowed_receiver" not in ref


def test_list_element_presence_is_not_an_immutable_tuple_proof(tmp_path):
    uses(
        tmp_path,
        "def use(values: list[str | None]):\n"
        "    if values[0]:\n"
        "        values[0].rpartition('.')\n",
    )
    ref = receiver_at_use(tmp_path, "values[0]")
    assert not ref or "narrowed_receiver" not in ref


@pytest.mark.parametrize(
    "slice_expression", ["values[1:]", "values[::-1]", "values[index:]"]
)
@pytest.mark.parametrize("assigned", [False, True])
def test_tuple_slice_does_not_keep_original_member_positions(
    tmp_path, slice_expression, assigned
):
    body = (
        f"    rest = {slice_expression}\n    selected = rest[0]\n    selected.bit_length()\n"
        if assigned
        else f"    {slice_expression}[0].bit_length()\n"
    )
    edges = uses(tmp_path, "def use(values: tuple[int, str], index):\n" + body)
    assert "extsym:builtins.int.bit_length" not in {edge.target for edge in edges}


def test_homogeneous_tuple_slice_keeps_its_element_type(tmp_path):
    edges = uses(
        tmp_path,
        "def use(values: tuple[int, ...]):\n"
        "    rest = values[1:]\n"
        "    rest[0].bit_length()\n",
    )
    assert "extsym:builtins.int.bit_length" in {edge.target for edge in edges}
