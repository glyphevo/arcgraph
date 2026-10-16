"""Review regressions: default compatibility and complete sync input recovery."""

import json
import shutil
from pathlib import Path

import pytest

from arcgraph.core.freshness import compute_freshness
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.project_environment import ProjectEnvironment
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.tests.test_batch30_build import configure, graph_records
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def test_bad_toml_default_build_and_freshness_match_parent(tmp_path):
    (tmp_path / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.arcgraph]\nsource_roots=["."]\n', encoding="utf-8")
    output = tmp_path / "out"
    ArcGraphIndexer(tmp_path, output).build()
    reader = GraphStoreReader.from_current(output)
    before = compute_freshness(reader)
    config.write_text('[tool.arcgraph\nsource_roots=["."]\n', encoding="utf-8")
    after = compute_freshness(reader)
    assert (
        (before.status, before.reason)
        == (after.status, after.reason)
        == (
            "fresh",
            None,
        )
    )
    ArcGraphIndexer(tmp_path, tmp_path / "bad-out").build()
    assert graph_records(output) == graph_records(tmp_path / "bad-out")


@pytest.mark.parametrize(
    "config,diagnostic",
    [
        ('[tool.arcgraph]\nsemantic_core="true"', "semantic_core must be a boolean"),
        ("[tool.arcgraph]\nsemantic_core=1", "semantic_core must be a boolean"),
        ("[tool.arcgraph]\nsemantic_core=[]", "semantic_core must be a boolean"),
        ('[tool]\narcgraph="no"', "[tool.arcgraph] must be a TOML table"),
    ],
)
def test_bad_semantic_config_uses_cli_user_error(tmp_path, capsys, config, diagnostic):
    (tmp_path / "pyproject.toml").write_text(config, encoding="utf-8")
    assert main(["--repo-root", str(tmp_path), "build"]) == 2
    captured = capsys.readouterr()
    assert diagnostic in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(captured.out)["error_code"] != "ARCGRAPH_INTERNAL_ERROR"


@pytest.mark.parametrize("populated", [False, True])
def test_active_environment_metadata_and_freshness_use_real_project(
    prepared, tmp_path, populated
):
    project, environment, _, _ = prepared
    site = next(
        environment.glob("lib/python*/site-packages"), environment / "Lib/site-packages"
    )
    for child in site.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    if populated:
        (site / "keep.py").write_text("value=1\n", encoding="utf-8")
    configure(project)
    (project / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    output = tmp_path / "out"
    metadata, _ = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    stored = metadata.extractor_metadata["python-v1-compat-shim"][
        "dependency_environment"
    ]
    assert stored == ProjectEnvironment(project).metadata
    freshness = compute_freshness(GraphStoreReader.from_current(output))
    assert freshness.status == "fresh", freshness


@pytest.mark.parametrize("initial_enabled", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "openapi_path",
        "scip_graph_index_path",
        "scip_index_path",
        "pyright_export_path",
        "coverage_path",
        "runtime_trace_path",
    ],
)
def test_sync_recovers_each_explicit_artifact_and_matches_full(
    prepared, tmp_path, field, initial_enabled
):
    project, _, _, _ = prepared
    (project / "demo.py").write_text(
        "def target(): return 1\ndef f(): return target()\n", encoding="utf-8"
    )
    artifact = project / (field + ".json")
    if field == "openapi_path":
        payload = {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/items": {
                    "get": {
                        "operationId": "items",
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    elif field == "scip_graph_index_path":
        fixture = (
            Path(__file__).parent / "fixtures/scip_protocol_baseline/scip-index.json"
        )
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        shutil.copytree(fixture.parent / "src", project / "src")
    elif field == "coverage_path":
        payload = {
            "meta": {"format": 3},
            "files": {"demo.py": {"executed_lines": [1], "missing_lines": []}},
        }
    elif field == "runtime_trace_path":
        payload = {
            "trace_run": {
                "trace_run_id": "review",
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
    else:
        payload = {"documents": []}
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    if initial_enabled:
        configure(project)
    output = tmp_path / "sync"
    inputs = {field: artifact}
    ArcGraphIndexer(project, output, **inputs).build()
    if not initial_enabled:
        configure(project)
    ArcGraphReindexer(project, output).reindex_changed()
    reader = GraphStoreReader.from_current(output)
    if field == "scip_graph_index_path":
        assert any(
            n.properties.get("frontend_name") == "scip-protocol"
            for n in reader.read_nodes()
        )
    if field == "runtime_trace_path":
        assert any(
            any(item.kind == "runtime_trace_dynamic_call" for item in e.evidence)
            for e in reader.read_edges()
        )
    saved = json.loads(
        (reader.sqlite_path.parent / "build-inputs.json").read_text(encoding="utf-8")
    )
    assert saved[field] == str(artifact)
    ArcGraphIndexer(project, tmp_path / "full", **inputs).build()
    assert graph_records(output) == graph_records(tmp_path / "full")
    full = GraphStoreReader.from_current(tmp_path / "full")
    assert stable_summary(reader.metadata, output) == stable_summary(
        full.metadata, tmp_path / "full"
    )
    assert stable_summary(
        json.loads(
            (reader.sqlite_path.parent / "summary.json").read_text(encoding="utf-8")
        ),
        output,
    ) == stable_summary(
        json.loads(
            (full.sqlite_path.parent / "summary.json").read_text(encoding="utf-8")
        ),
        tmp_path / "full",
    )


def stable_summary(value, output):
    if isinstance(value, dict):
        if "manifest_sha256" in value:
            from arcgraph.semantic.build import generation_directory
            from arcgraph.semantic.snapshot import sha

            reader = GraphStoreReader.from_current(output)
            manifest = generation_directory(reader.sqlite_path.parent) / "manifest.json"
            assert value["manifest_sha256"] == sha(manifest.read_bytes())
            # The manifest binds physical journal URIs and the publication revision.
            # Verify each binding before normalizing this per-generation identity.
        return {
            k: (
                "<semantic-generation-manifest>"
                if k == "manifest_sha256"
                else stable_summary(v, output)
            )
            for k, v in value.items()
            if k
            not in (
                "created_at",
                "index_version",
                "phase_timings",
                "duration_seconds",
                "diagnostic_lifecycle",
                "first_seen_index_version",
                "last_seen_index_version",
                "seen_count",
                "lifecycle_status",
                "generated_at",
                "diagnostic_id",
                "first_seen_index",
                "last_seen_index",
                "diagnostic_lifecycle_status",
            )
        }
    if isinstance(value, list):
        return [stable_summary(v, output) for v in value]
    if isinstance(value, str):
        return value.replace(str(output), "<output>")
    return value


def test_disabled_incremental_sync_preserves_constructor_inputs_for_switch(
    prepared, tmp_path
):
    project, _, _, _ = prepared
    (project / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    spec = project / "api.json"
    spec.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "t", "version": "1"},
                "paths": {"/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "sync"
    ArcGraphIndexer(
        project,
        output,
        openapi_path=spec,
        ignore_rules=["ignored.py"],
        enable_v2_call_resolution=False,
    ).build()
    (project / "demo.py").write_text("def f(): return 2\n", encoding="utf-8")
    (project / "ignored.py").write_text("def hidden(): return 0\n", encoding="utf-8")
    ArcGraphReindexer(project, output).reindex_changed()
    assert "fn:ignored.hidden" not in {
        n.id for n in GraphStoreReader.from_current(output).read_nodes()
    }
    configure(project)
    ArcGraphReindexer(project, output).reindex_changed()
    ArcGraphIndexer(
        project,
        tmp_path / "full",
        openapi_path=spec,
        ignore_rules=["ignored.py"],
        enable_v2_call_resolution=False,
    ).build()
    assert graph_records(output) == graph_records(tmp_path / "full")
    assert "fn:ignored.hidden" not in {
        n.id for n in GraphStoreReader.from_current(output).read_nodes()
    }


def test_sync_recovers_explicit_environment_and_root_specs(prepared, tmp_path):
    from arcgraph.core.scanner import SourceRoot, SourceRootDetection

    project, environment, _, _ = prepared
    selected = project / ".selected-venv"
    environment.rename(selected)
    (project / "demo.py").unlink()
    (project / "src").mkdir()
    (project / "src/demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    inputs = dict(
        project_environment=selected,
        source_roots=[SourceRoot("src", "pkg")],
        source_root_detection=SourceRootDetection("cli_explicit", ("src",)),
    )
    output = tmp_path / "sync"
    ArcGraphIndexer(project, output, **inputs).build()
    configure(project)
    ArcGraphReindexer(project, output).reindex_changed()
    ArcGraphIndexer(project, tmp_path / "full", **inputs).build()
    reader = GraphStoreReader.from_current(output)
    assert graph_records(output) == graph_records(tmp_path / "full")
    assert stable_summary(reader.metadata, output) == stable_summary(
        GraphStoreReader.from_current(tmp_path / "full").metadata, tmp_path / "full"
    )
    assert compute_freshness(reader).status == "fresh"


def test_active_sync_source_detection_equals_full(prepared, tmp_path):
    project, _, _, _ = prepared
    configure(project)
    output = tmp_path / "sync"
    ArcGraphIndexer(project, output).build()
    ArcGraphReindexer(project, output).reindex_changed()
    ArcGraphIndexer(project, tmp_path / "full").build()
    assert (
        GraphStoreReader.from_current(output).metadata["source_root_detection"]
        == GraphStoreReader.from_current(tmp_path / "full").metadata[
            "source_root_detection"
        ]
    )


@pytest.mark.parametrize(
    "field,fixture",
    [
        ("openapi_path", "openapi_protocol_baseline/openapi.json"),
        ("scip_graph_index_path", "scip_protocol_baseline/scip-index.json"),
    ],
)
def test_legacy_generation_recovers_protocol_paths_without_private_sidecar(
    prepared, tmp_path, field, fixture
):
    project, _, _, _ = prepared
    source = Path(__file__).parent / "fixtures" / fixture
    artifact = project / "protocol.json"
    artifact.write_bytes(source.read_bytes())
    if field == "scip_graph_index_path":
        shutil.copytree(source.parent / "src", project / "src")
    inputs = {field: artifact}
    output = tmp_path / "sync"
    ArcGraphIndexer(project, output, **inputs).build()
    reader = GraphStoreReader.from_current(output)
    assert any(
        n.properties.get("frontend_name") in ("openapi-protocol", "scip-protocol")
        for n in reader.read_nodes()
    )
    (reader.sqlite_path.parent / "build-inputs.json").unlink(missing_ok=True)
    configure(project)
    ArcGraphReindexer(project, output).reindex_changed()
    ArcGraphIndexer(project, tmp_path / "full", **inputs).build()
    assert graph_records(output) == graph_records(tmp_path / "full")
