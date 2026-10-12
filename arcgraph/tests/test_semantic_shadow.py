"""Fully mapped shadow candidates retain the existing export contract."""

from arcgraph.semantic.graph_projection import project_graph
from arcgraph.tests.test_semantic_core import (
    make_view,
    make_records,
    add,
    project_records,
)


def test_mapped_open_candidate_exports_as_resolved_inferred_edge(tmp_path):
    snapshot, root, view = make_view(
        tmp_path,
        source="class C:\n    def run(self): return 1\ndef f(x): return x.run()\n",
    )
    main = make_records(snapshot, root, view)
    fallback = make_records(snapshot, root, view, "arcgraph")
    add(main, "x.run()", "C.run")
    decisions = project_records(main, fallback)
    site = next(s for s in view.callsites if s.expression == "x.run()")
    definitions = {d["id"]: d for d in view.data["definitions"]}
    nodes = [
        dict(
            id=d.id,
            kind=d.symbol_kind,
            name=d.name,
            qualname=d.qualname,
            path=d.path,
            start_line=definitions[d.id]["line"],
            properties={},
        )
        for d in view.definitions
    ]
    source = next(n for n in nodes if n["id"] == site.owner)
    source["properties"]["callsites"] = [
        dict(
            name="x.run",
            line=site.span.start[0] + 1,
            column=site.span.start[1],
            call_expression=site.expression,
            context="return",
        )
    ]
    graph, bridge = project_graph(
        view,
        {"nodes": nodes, "edges": []},
        fallback.finish(),
        decisions,
        primary_producer=main.backend.id,
    )
    assert not bridge["graph_losses"]
    assert len(graph) == 1
    assert graph[0]["resolution"]["status"] == "resolved"
    assert graph[0]["confidence"] == "inferred"
    assert graph[0]["properties"]["callsite"]["resolution_status"] == "resolved"
    assert main.finish().answers[-1].completeness in ("open", "unknown")


def test_collection_restarts_without_shrinking_scope_or_losing_journal_rows(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager
    import json
    from arcgraph.semantic import transport

    root = tmp_path / "root"
    root.mkdir()
    (root / "demo.py").write_text("def f(): pass\n", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    plan = output / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "snapshot_id": "frozen",
                "definitions": [
                    {
                        "path": "demo.py",
                        "positions": {"utf-16": {"line": 0, "character": 4}},
                    }
                ]
                * 3,
            }
        ),
        encoding="utf-8",
    )
    calls = []

    class Peer:
        stderr = []

        def notify(self, method, params):
            calls.append(method)

        def request(self, method, params):
            calls.append(method)
            if method == "initialize":
                return {"capabilities": {"callHierarchyProvider": True}}
            if method == "textDocument/prepareCallHierarchy":
                return [{"name": "f"}]
            return []

    @contextmanager
    def session(*args, **kwargs):
        yield Peer(), {
            "node": "fixed",
            "node_version": "v22",
            "pyright_version": "1.1.414",
        }

    monkeypatch.setattr(transport, "SESSION_DEFINITIONS", 1)
    monkeypatch.setattr(transport, "session", session)
    result = transport.collect(plan, root, {}, output)
    rows = [
        json.loads(line)
        for line in (output / "responses.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(result["sessions"]) == 3
    assert calls.count("initialize") == 3
    assert calls.count("textDocument/didOpen") == 3
    assert calls.count("callHierarchy/outgoingCalls") == 3
    assert rows[-1]["complete"] and rows[-1]["count"] == 9


def test_frozen_dependencies_live_scope_and_artifacts_reject_drift(tmp_path):
    import hashlib
    import json
    import sys
    import pytest
    from arcgraph.semantic.frozen import (
        environment_identity,
        verify_environment,
        source_identity,
        verify_live_sources,
    )
    from arcgraph.semantic.snapshot import capture
    from scripts.semantic_shadow import WRITE_INPUTS, verify_write_inputs

    environment = tmp_path / "environment"
    site = environment / "lib/python3.11/site-packages"
    site.mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("version = 3.11.15\n", encoding="utf-8")
    dependency = site / "dependency.pyi"
    dependency.write_text("def f() -> int: ...\n", encoding="utf-8")
    expected = environment_identity(environment)[0]
    verify_environment(environment, expected)
    dependency.write_text("def f() -> str: ...\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen dependency changed"):
        verify_environment(environment, expected)

    source = tmp_path / "source"
    source.mkdir()
    (source / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    (source / "demo.pyi").write_text("def f() -> int: ...\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        '[tool.arcgraph]\nsource_roots=["."]\n', encoding="utf-8"
    )
    identity = source_identity(source)[0]
    snapshot = capture(
        source,
        ["demo.py", "demo.pyi", "pyproject.toml"],
        tmp_path / "frozen",
        target_python=f"{sys.version_info.major}.{sys.version_info.minor}",
        platform=sys.platform,
        environment=(("dependencies", "none"),),
    )
    verify_live_sources(snapshot, source, identity)
    (source / "added.py").write_text("def added(): pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source scope changed"):
        verify_live_sources(snapshot, source, identity)
    (source / "added.py").unlink()
    (source / "pyproject.toml").write_text(
        '[tool.arcgraph]\nsource_roots=["missing"]\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="changed"):
        verify_live_sources(snapshot, source, identity)

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest = {}
    for name in WRITE_INPUTS:
        (evidence / name).write_text("{}", encoding="utf-8")
        manifest[name] = hashlib.sha256(b"{}").hexdigest()
    (evidence / "write-inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    verify_write_inputs(evidence)
    (evidence / "python-edges.json").write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="shadow artifact changed"):
        verify_write_inputs(evidence)


def test_mirrored_callsite_fields_share_one_mapping_and_cannot_escape_suppression(
    tmp_path,
):
    from arcgraph.semantic.backends import wrap_graph
    from arcgraph.semantic.projection import project

    snapshot, root, view = make_view(
        tmp_path,
        source="class C:\n    def run(self): return 1\ndef f(x: int): return x.run()\n",
    )
    primary = make_records(snapshot, root, view)
    fallback = make_records(snapshot, root, view, "arcgraph")
    definition = {d["id"]: d for d in view.data["definitions"]}
    nodes = [
        dict(
            id=d.id,
            kind=d.symbol_kind,
            name=d.name,
            qualname=d.qualname,
            path=d.path,
            start_line=definition[d.id]["line"],
            properties={},
        )
        for d in view.definitions
    ]
    site = next(s for s in view.callsites if s.expression == "x.run()")
    target = next(d for d in view.definitions if d.qualname == "demo.C.run")
    fact = dict(
        callsite_id="one",
        line=site.span.start[0] + 1,
        column=site.span.start[1],
        call_expression=site.expression,
        raw_expression="x.run",
        resolution_strategy="unique_method_fallback",
    )
    edge = dict(
        source=site.owner,
        target=target.id,
        kind="calls",
        confidence="heuristic",
        resolution={"status": "resolved"},
        properties={
            "callsite": fact,
            "callsites": [{**fact, "confidence": "heuristic"}],
        },
    )
    raw = {"nodes": nodes, "edges": [edge]}
    wrapped = wrap_graph(snapshot, root, fallback.backend, view, raw)
    assert len(wrapped.mapping) == 1
    decisions = project(
        view, primary.finish(), wrapped, (primary.backend, fallback.backend)
    )
    assert not decisions["projected_calls"]
    graph, bridge = project_graph(
        view, raw, wrapped, decisions, primary_producer=primary.backend.id
    )
    assert (
        graph == []
    ), "A differently decorated mirror must not escape policy suppression"
    assert len(bridge["removed_facts"]) == 1
