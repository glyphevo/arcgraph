"""Artifact mutations cannot mix graph facts with a later manifest revision."""

import hashlib
import json

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.semantic.build import SemanticBuild
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def inputs(project):
    source = project / "demo.py"
    source.write_text(
        "def target(): return 1\ndef f(): return target()\n", encoding="utf-8"
    )
    (project / "pyproject.toml").write_text(
        "[tool.arcgraph]\nsemantic_core = true\n", encoding="utf-8"
    )
    coverage = project / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "meta": {"format": 3},
                "files": {str(source): {"executed_lines": [1, 2], "missing_lines": []}},
            }
        ),
        encoding="utf-8",
    )
    runtime = project / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "trace_run": {
                    "trace_run_id": "inputs",
                    "source": "pytest",
                    "frontend_version": "ArcGraph-runtime-trace-v1",
                },
                "events": [
                    {
                        "kind": "dynamic_call",
                        "source": "fn:demo.f",
                        "target": "fn:demo.target",
                        "path": "demo.py",
                        "line": 2,
                        "column": 16,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    openapi = project / "api.json"
    openapi.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "test", "version": "1"},
                "paths": {
                    "/v1": {
                        "get": {
                            "operationId": "f",
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    scip = project / "protocol.json"
    scip.write_text(json.dumps({"documents": []}), encoding="utf-8")
    sidecar = coverage.with_name(coverage.name + ".arcgraph.json")
    sidecar.write_text(
        json.dumps({"tool_name": "coverage", "source_roots": ["."]}), encoding="utf-8"
    )
    return (
        dict(
            coverage_path=coverage,
            runtime_trace_path=runtime,
            openapi_path=openapi,
            scip_graph_index_path=scip,
        ),
        sidecar,
    )


@pytest.mark.parametrize(
    "field",
    [
        "coverage_path",
        "runtime_trace_path",
        "openapi_path",
        "scip_graph_index_path",
        "sidecar",
    ],
)
def test_persistent_artifact_changes_abort_before_publish(
    prepared, tmp_path, monkeypatch, field
):
    project, _, _, _ = prepared
    arguments, sidecar = inputs(project)
    output = tmp_path / "out"
    ArcGraphIndexer(project, output, **arguments).build()
    pointer = (output / "current.json").read_bytes()
    live_manifest = (output / "evidence/manifest.json").read_bytes()
    previous = GraphStoreReader.from_current(output).metadata["index_version"]
    path = sidecar if field == "sidecar" else arguments[field]
    original = SemanticBuild.apply

    def changed(self, content):
        result = original(self, content)
        path.write_text("{}\n", encoding="utf-8")
        return result

    monkeypatch.setattr(SemanticBuild, "apply", changed)
    with pytest.raises(
        ValueError, match="input_changed_during_run: artifact input changed"
    ):
        ArcGraphIndexer(project, output, **arguments).build()
    assert (output / "current.json").read_bytes() == pointer
    assert (output / "evidence/manifest.json").read_bytes() == live_manifest
    assert GraphStoreReader.from_current(output).metadata["index_version"] == previous


def test_transient_coverage_replacement_is_read_only_from_capture(
    prepared, tmp_path, monkeypatch
):
    from arcgraph.analyzers.coverage import CoverageCollector

    project, _, _, _ = prepared
    arguments, _ = inputs(project)
    path = arguments["coverage_path"]
    original = CoverageCollector._analyze_json
    raw = path.read_bytes()

    def replaced(self, value, nodes):
        path.write_text('{"files": {}}', encoding="utf-8")
        try:
            return original(self, value, nodes)
        finally:
            path.write_bytes(raw)

    monkeypatch.setattr(CoverageCollector, "_analyze_json", replaced)
    metadata, directory = ArcGraphIndexer(
        project, tmp_path / "out", **arguments
    ).build()
    reader = GraphStoreReader.from_current(tmp_path / "out")
    assert any(e.kind == "covers" for e in reader.read_edges())
    artifact = next(
        a for a in metadata.evidence_manifest["artifacts"] if a["kind"] == "coverage"
    )
    assert artifact["hash"] == hashlib.sha256(raw).hexdigest()
    snapshot = json.loads(
        (directory / "artifact-inputs.json").read_text(encoding="utf-8")
    )
    assert snapshot["inputs"]["coverage.json"] == artifact["hash"]
    assert (directory / ("input-" + artifact["hash"] + ".bin")).read_bytes() == raw


def test_transient_runtime_and_protocol_replacements_do_not_change_graph(
    prepared, tmp_path, monkeypatch
):
    from arcgraph.analyzers.runtime_trace import RuntimeTraceImporter
    from arcgraph.pipeline.openapi_frontend import OpenApiProtocolFrontend

    project, _, _, _ = prepared
    arguments, _ = inputs(project)
    runtime = arguments["runtime_trace_path"]
    spec = arguments["openapi_path"]
    original_runtime = RuntimeTraceImporter.analyze
    original_spec = OpenApiProtocolFrontend._load_payload

    def trace(self, nodes):
        raw = runtime.read_bytes()
        runtime.write_text('{"events": []}', encoding="utf-8")
        try:
            return original_runtime(self, nodes)
        finally:
            runtime.write_bytes(raw)

    def protocol(self, *args):
        raw = spec.read_bytes()
        spec.write_text('{"openapi": "3.0.0", "paths": {}}', encoding="utf-8")
        try:
            return original_spec(self, *args)
        finally:
            spec.write_bytes(raw)

    monkeypatch.setattr(RuntimeTraceImporter, "analyze", trace)
    monkeypatch.setattr(OpenApiProtocolFrontend, "_load_payload", protocol)
    metadata, _ = ArcGraphIndexer(project, tmp_path / "out", **arguments).build()
    reader = GraphStoreReader.from_current(tmp_path / "out")
    assert reader.metadata["runtime_metrics"]["events_total"] == 1
    assert any(
        e.kind == "calls"
        and any(x.kind.startswith("runtime_trace_") for x in e.evidence)
        for e in reader.read_edges()
    )
    assert any(
        n.kind == "route" and n.properties.get("path") == "/v1"
        for n in reader.read_nodes()
    )


def test_published_capture_and_live_artifact_changes_are_never_fresh(
    prepared, tmp_path
):
    from arcgraph.core.query_engine import QueryEngine

    project, _, _, _ = prepared
    arguments, _ = inputs(project)
    output = tmp_path / "out"
    _, directory = ArcGraphIndexer(project, output, **arguments).build()
    assert QueryEngine(output).current()["freshness"]["stale"] is False
    raw = arguments["coverage_path"].read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    capture = directory / ("input-" + checksum + ".bin")
    capture.write_bytes(b"changed")
    freshness = QueryEngine(output).current()["freshness"]
    assert freshness["stale"] is True
    assert freshness["reason"] == "semantic_artifact_input_capture_changed"
    capture.write_bytes(raw)
    arguments["coverage_path"].write_bytes(b"{}")
    freshness = QueryEngine(output).current()["freshness"]
    assert freshness["stale"] is True
    assert freshness["reason"] == "semantic_artifact_input_changed"
