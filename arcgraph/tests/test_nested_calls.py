from __future__ import annotations

import pytest

from arcgraph.analyzers.calls import CallAnalyzer
from arcgraph.tests.test_calls import _analyzed_nodes

PREFIX = "fn:pkg.calls."
TARGET = "method:pkg.calls.Reporter.report"
HEADER = "class Reporter:\n    def report(self):\n        return 'ok'\n\n"


def _analyze(body):
    nodes = _analyzed_nodes(HEADER + body)
    result = CallAnalyzer(enable_v2=True).analyze(nodes)
    calls = {(e.source, e.target) for e in result.edges if e.kind == "calls"}
    return nodes, result, calls


def test_nested_async_callback_captures_constructor_and_owns_its_call():
    nodes, _, calls = _analyze(
        "def create_server():\n"
        "    eyes = Reporter()\n"
        "    async def call_tool(name):\n"
        "        return eyes.report()\n"
        "    return call_tool\n"
    )
    assert (PREFIX + "create_server.call_tool", TARGET) in calls
    assert (PREFIX + "create_server", TARGET) not in calls
    inner = next(n for n in nodes if n.id == PREFIX + "create_server.call_tool")
    assert inner.properties["lexical_parent"] == PREFIX + "create_server"
    assert inner.properties["async"] is True


def test_nested_scope_chain_and_calls_to_local_functions():
    _, _, calls = _analyze(
        "def outer():\n"
        "    eyes = Reporter()\n"
        "    def middle():\n"
        "        def inner():\n            return eyes.report()\n"
        "        return inner()\n"
        "    return middle()\n"
        "def unrelated():\n    return inner()\n"
    )
    assert (PREFIX + "outer.middle.inner", TARGET) in calls
    assert (PREFIX + "outer.middle", PREFIX + "outer.middle.inner") in calls
    assert (PREFIX + "outer", PREFIX + "outer.middle") in calls
    assert not any(
        a == PREFIX + "unrelated" and b == PREFIX + "outer.middle.inner"
        for a, b in calls
    )


@pytest.mark.parametrize(
    "body",
    [
        # Parameter masks the captured receiver, including the name-based fallback.
        "    reporter = Reporter()\n    def inner(reporter):\n        return reporter.report()\n",
        "    eyes = Reporter()\n    def inner():\n        eyes = unknown()\n        return eyes.report()\n",
        "    eyes = Reporter()\n    def inner():\n        return eyes.report()\n    eyes = unknown()\n",
        "    eyes = Reporter()\n    def inner():\n        return eyes.report()\n    del eyes\n",
        "    eyes = Reporter()\n    def mutate():\n        nonlocal eyes\n        eyes = unknown()\n    def inner():\n        return eyes.report()\n",
        "    eyes = Reporter()\n    def inner():\n        nonlocal eyes\n        eyes = unknown()\n        return eyes.report()\n",
        "    eyes = Reporter()\n    def inner():\n        global eyes\n        return eyes.report()\n",
        "    if flag:\n        eyes = Reporter()\n    def inner():\n        return eyes.report()\n",
        "    if flag: eyes = Reporter()\n    def inner():\n        return eyes.report()\n",
        "    def inner():\n        return eyes.report()\n    eyes = Reporter()\n",
        "    eyes = Reporter()\n    def inner():\n        eyes.report()\n        eyes = unknown()\n",
        "    def inner():\n        eyes.report(); eyes = Reporter()\n",
        "    eyes = Reporter()\n    def inner():\n        eyes = Reporter()\n        eyes = unknown()\n        return eyes.report()\n",
        "    eyes = Reporter()\n    def inner():\n        (eyes := unknown())\n        return eyes.report()\n",
        "    eyes = Reporter()\n    def inner(value):\n        match value:\n            case {'key': eyes}:\n                return eyes.report()\n",
        "    def inner():\n        return reporter.report()\n",
        "    def inner():\n        class Reporter:\n            pass\n        return Reporter.report()\n",
    ],
)
def test_unsafe_capture_stays_unresolved(body):
    _, result, calls = _analyze("def outer(flag=False):\n" + body)
    assert (PREFIX + "outer.inner", TARGET) not in calls
    assert any(
        e.source == PREFIX + "outer.inner" and e.confidence == "unresolved"
        for e in result.edges
    )


def test_typed_shadow_and_method_self_capture_resolve():
    nodes = _analyzed_nodes(
        HEADER + "def outer():\n    eyes = unknown()\n"
        "    def inner(eyes: Reporter):\n        return eyes.report()\n"
        "    return inner\n\n"
        "class Wrapper:\n"
        '    def report(self):\n        return "ok"\n'
        "    def make(self):\n"
        "        def inner():\n            return self.report()\n"
        "        return inner\n"
    )
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    pairs = {(e.source, e.target) for e in edges if e.kind == "calls"}
    assert (PREFIX + "outer.inner", TARGET) in pairs
    assert (PREFIX + "Wrapper.make.inner", "method:pkg.calls.Wrapper.report") in pairs


def test_redefined_nested_functions_do_not_guess_a_body():
    _, _, calls = _analyze(
        "def outer(flag):\n    eyes = Reporter()\n"
        "    if flag:\n        def inner():\n            return eyes.report()\n"
        "    else:\n        def inner():\n            return unknown()\n"
        "    return inner()\n"
    )
    assert not any(
        a == PREFIX + "outer.inner" or b == PREFIX + "outer.inner" for a, b in calls
    )


@pytest.mark.parametrize(
    "other_body", ["pass", "def report(self):\n        return 'other'"]
)
def test_typed_shadow_overrides_class_name_and_unique_method(other_body):
    _, _, calls = _analyze(
        f"class Other:\n    {other_body}\n"
        "def outer():\n"
        "    def inner(Reporter: Other):\n        return Reporter.report()\n"
        "    return inner\n"
    )
    assert (PREFIX + "outer.inner", TARGET) not in calls
    assert ((PREFIX + "outer.inner", "method:pkg.calls.Other.report") in calls) == (
        other_body != "pass"
    )


def test_import_shadow_masks_module_function_and_is_captured():
    nodes = _analyzed_nodes(
        "def loads(value):\n    return value\n"
        "def outer():\n    from json import loads\n"
        "    def inner():\n        return loads('{}')\n"
        "    return inner\n"
    )
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    inner_edges = [e for e in edges if e.source == PREFIX + "outer.inner"]
    assert any(e.target == "extsym:json.loads" for e in inner_edges)
    assert not any(e.target == PREFIX + "loads" for e in inner_edges)


def test_identical_nested_names_are_separate_and_control_blocks_are_indexed():
    nodes, _, calls = _analyze(
        "def first():\n    def inner():\n        return 1\n    return inner()\n"
        "def second():\n    def inner():\n        return 2\n    return inner()\n"
        "def outer(flag):\n    eyes = Reporter()\n"
        "    try:\n        def in_try():\n            return eyes.report()\n"
        "    except Exception:\n        async def in_except():\n            return eyes.report()\n"
        "    match flag:\n        case 1:\n            def in_match():\n                return eyes.report()\n"
    )
    ids = {n.id for n in nodes}
    for name in ("in_try", "in_except", "in_match"):
        assert PREFIX + "outer." + name in ids
        assert (PREFIX + "outer." + name, TARGET) in calls
    for outer in ("first", "second"):
        assert (PREFIX + outer, PREFIX + outer + ".inner") in calls
    assert (PREFIX + "first", PREFIX + "second.inner") not in calls


def test_nested_return_annotation_does_not_leak_to_unrelated_scope():
    _, _, calls = _analyze(
        "def outer():\n    def build() -> Reporter:\n        return Reporter()\n"
        "    return build\n"
        "def unrelated():\n    eyes = build()\n    return eyes.report()\n"
    )
    assert (PREFIX + "unrelated", TARGET) not in calls


def test_later_module_definitions_remain_visible_to_nested_and_parent_bodies():
    _, _, calls = _analyze(
        "def outer():\n"
        "    def inner():\n        return later()\n"
        "    later()\n    return inner\n"
        "def later():\n    return 'ok'\n"
    )
    assert (PREFIX + "outer", PREFIX + "later") in calls
    assert (PREFIX + "outer.inner", PREFIX + "later") in calls


def test_shadowed_constructor_and_factory_do_not_supply_capture_types():
    _, _, calls = _analyze(
        "def build() -> Reporter:\n    return Reporter()\n"
        "def outer(Reporter, build):\n    eyes = Reporter()\n    other = build()\n"
        "    def inner():\n        eyes.report()\n        other.report()\n"
        "    return inner\n"
    )
    assert (PREFIX + "outer.inner", TARGET) not in calls


def test_nested_factory_annotation_is_visible_only_through_its_binding():
    _, _, calls = _analyze(
        "def outer():\n"
        "    def build() -> Reporter:\n        return Reporter()\n"
        "    eyes = build()\n"
        "    def inner():\n        return eyes.report()\n"
        "    return inner\n"
    )
    assert (PREFIX + "outer.inner", TARGET) in calls


def test_reindex_updates_capture_type_and_removes_deleted_nested_symbol(tmp_path):
    from arcgraph.core.query_engine import QueryEngine
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer

    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "sample.py"
    output = tmp_path / "index"
    roots = [SourceRoot(".")]
    source.write_text(
        HEADER
        + "def outer():\n    eyes = Reporter()\n    def inner():\n        return eyes.report()\n    return inner\n"
    )
    ArcGraphIndexer(repo, output, roots, enable_v2_call_resolution=True).build()

    def callers():
        return {
            n["id"]
            for n in QueryEngine(output).callers("sample.Reporter.report")["callers"]
        }

    assert "fn:sample.outer.inner" in callers()
    source.write_text(
        source.read_text().replace("eyes = Reporter()", "eyes = unknown()")
    )
    ArcGraphReindexer(repo, output, roots).reindex_changed()
    assert "fn:sample.outer.inner" not in callers()
    source.write_text(HEADER + "def outer():\n    return None\n")
    ArcGraphReindexer(repo, output, roots).reindex_changed()
    assert not QueryEngine(output).symbol("sample.outer.inner")["matches"]


def test_unresolved_callback_diagnostics_keep_enclosing_binding_evidence():
    from arcgraph.core.schemas import IndexMetadata
    from arcgraph.core.semantic_metrics import unresolved_callsite_diagnostics
    from arcgraph.core.unresolved_classification import classify_unresolved_records

    nodes, result, calls = _analyze(
        "def outer(callback):\n"
        "    def inner():\n        return callback()\n"
        "    def shadow():\n        callback()\n        def callback():\n            pass\n"
        "    return inner\n"
    )
    diagnostics = unresolved_callsite_diagnostics(
        IndexMetadata(index_version="closure", repo_root="/repo", source_roots=["src"]),
        nodes + result.nodes,
        result.edges,
        repo_id="test",
        frontend_name="python",
    )
    rows = [d.model_dump(mode="json") for d in diagnostics]
    inner = next(
        r for r in rows if r["properties"]["source_scope"] == PREFIX + "outer.inner"
    )
    shadow = next(
        r for r in rows if r["properties"]["source_scope"] == PREFIX + "outer.shadow"
    )
    assert inner["properties"]["callee_binding_scope"] == "enclosing"
    assert inner["properties"]["callee_binding_kind"] == "parameter"
    assert "callee_binding_kind" not in shadow["properties"]
    assert (
        classify_unresolved_records({"unresolved": [inner]})["release_blocking_count"]
        == 0
    )
    assert not any(source == PREFIX + "outer.inner" for source, _ in calls)
