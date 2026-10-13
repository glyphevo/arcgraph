"""Semantic build configuration, captured content, admission and publication."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def generation_directory(build_dir):
    from arcgraph.semantic.build import generation_directory as directory

    return directory(build_dir)


def configure(root, value="true"):
    path = root / "pyproject.toml"
    if path.exists():
        source = path.read_text(encoding="utf-8")
        source += "semantic_core = " + value + "\n"
    else:
        source = "[tool.arcgraph]\nsemantic_core = " + value + "\n"
    path.write_text(source, encoding="utf-8")


@pytest.mark.parametrize("value", [None, "false"])
def test_default_build_reports_disabled_without_semantic_work(tmp_path, value):
    (tmp_path / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    if value is not None:
        configure(tmp_path, value)
    metadata, _ = ArcGraphIndexer(tmp_path, tmp_path / "out").build()
    assert metadata.toolchain_status.get("semantic_core") == {
        "status": "disabled",
        "pyright_version": None,
        "node": None,
        "reason": None,
        "fix": None,
    }


@pytest.mark.parametrize("value", ['"true"', "1", '"auto"', "[]"])
def test_invalid_semantic_core_configuration_is_a_clear_error(tmp_path, value):
    configure(tmp_path, value)
    with pytest.raises(RuntimeError, match="semantic_core.*boolean.*true or false"):
        ArcGraphIndexer(tmp_path, tmp_path / "out").build()


def test_host_only_structure_falls_back_without_claiming_empty_success(tmp_path):
    configure(tmp_path)
    (tmp_path / "demo.py").write_text(
        "def target(): pass\ndef f(): return target()\n", encoding="utf-8"
    )
    metadata, directory = ArcGraphIndexer(tmp_path, tmp_path / "out").build()
    state = metadata.toolchain_status.get("semantic_core", {})
    assert state.get("status") == "unavailable", state
    assert state["reason"] == "host_fallback_not_semantic_evidence"
    reader = GraphStoreReader.from_current(tmp_path / "out")
    assert any(
        e.source == "fn:demo.f" and e.target == "fn:demo.target"
        for e in reader.read_edges()
    )
    assert not generation_directory(directory).exists()


def test_semantic_and_fallback_structure_use_the_bytes_captured_by_scanning(
    tmp_path, monkeypatch
):
    configure(tmp_path)
    source = "def target(): pass\ndef f(): return target()\n"
    path = tmp_path / "demo.py"
    path.write_text(source, encoding="utf-8")
    indexer = ArcGraphIndexer(tmp_path, tmp_path / "out")
    frontend = indexer.frontend_registry.frontends[0]
    original = frontend.analyze_to_graph

    def changed(files, **kwargs):
        path.write_text(
            "def different(): pass\ndef f(): return different()\n", encoding="utf-8"
        )
        return original(files, **kwargs)

    monkeypatch.setattr(frontend, "analyze_to_graph", changed)
    indexer.build()
    reader = GraphStoreReader.from_current(tmp_path / "out")
    assert reader.python_source("demo.py") == source
    assert "fn:demo.target" in {n.id for n in reader.read_nodes()}
    assert "fn:demo.different" not in {n.id for n in reader.read_nodes()}


def test_active_build_uses_pinned_pyright_and_publishes_matching_generation(
    prepared, tmp_path, monkeypatch
):
    from arcgraph.core.structure import StructureView

    reads = []
    original_read = StructureView.read

    def counted(self, paths, **kwargs):
        if "demo.py" in paths:
            reads.append(tuple(paths))
        return original_read(self, paths, **kwargs)

    monkeypatch.setattr(StructureView, "read", counted)
    project, _, markers, _ = prepared
    configure(project)
    (project / "demo.py").write_text(
        "def target(): return 7\ndef g(): return target()\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert len(reads) == 1, "Semantic conversion must reuse the build ASTs"
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    import json

    generation = generation_directory(directory)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is True
    assert manifest["revision"] == metadata.index_version
    from arcgraph.semantic.contract import Snapshot

    snapshot = Snapshot.model_validate(
        json.loads((generation / "snapshot.json").read_text(encoding="utf-8"))
    )
    assert manifest["snapshot"] == snapshot.id
    reader = GraphStoreReader.from_current(output)
    assert any(
        e.source == "fn:demo.g"
        and e.target == "fn:demo.target"
        and any(item.kind == "type_inference" for item in e.evidence)
        for e in reader.read_edges()
    )
    assert not [name for name, path in markers.items() if path.exists()]
    previous = (output / "current.json").read_bytes()

    def refused(self, *args):
        raise RuntimeError("injected publish failure")

    monkeypatch.setattr(GraphStoreWriter, "_publish_current", refused)
    with pytest.raises(RuntimeError, match="injected publish failure"):
        ArcGraphIndexer(project, output).build()
    assert (output / "current.json").read_bytes() == previous
    assert (
        GraphStoreReader.from_current(output).metadata["index_version"]
        == metadata.index_version
    )
    assert (generation / "pyright.json").is_file()


def graph_records(output):
    reader = GraphStoreReader.from_current(output)

    def stable(value):
        if isinstance(value, dict):
            return {
                k: stable(v)
                for k, v in value.items()
                if k not in ("created_at", "index_version")
            }
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value

    return [
        stable([r.model_dump(mode="json") for r in rows])
        for rows in (
            reader.read_nodes(),
            reader.read_edges(),
            reader.iter_semantic_facts(),
        )
    ]


@pytest.mark.parametrize("failure", ["node", "pyright", "timeout", "admission"])
def test_semantic_failures_are_visible_and_retain_the_same_fallback_graph(
    prepared, tmp_path, monkeypatch, failure
):
    from arcgraph.semantic import build, runtime

    project, _, _, _ = prepared
    configure(project)
    (project / "demo.py").write_text(
        "def target(): return 7\ndef g(): return target()\n", encoding="utf-8"
    )
    with monkeypatch.context() as control:
        control.setattr(runtime, "semantic_core_enabled", lambda root: False)
        ArcGraphIndexer(project, tmp_path / "control").build()
    if failure == "node":
        monkeypatch.setenv("PATH", str(tmp_path / "no-node"))
    elif failure == "pyright":
        monkeypatch.setenv("HOME", str(tmp_path / "missing-home"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "missing-cache"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "missing-local"))
    elif failure == "admission":
        monkeypatch.setattr(
            build,
            "admit_structure",
            lambda bundle, expected: (False, "injected admission rejection"),
        )
    else:

        def timed_out(*args):
            raise TimeoutError("injected pyright timeout")

        monkeypatch.setattr(build, "collect", timed_out)
    metadata, directory = ArcGraphIndexer(project, tmp_path / "failure").build()
    state = metadata.toolchain_status["semantic_core"]
    assert state["status"] == "unavailable", state
    assert state["reason"] and state["fix"]
    assert not generation_directory(directory).exists()
    assert graph_records(tmp_path / "failure") == graph_records(tmp_path / "control")


def test_enabled_sync_replaces_generation_and_equals_full_build(prepared, tmp_path):
    project, _, _, _ = prepared
    configure(project)
    source = project / "demo.py"
    source.write_text(
        "def first(): return 7\ndef g(): return first()\n", encoding="utf-8"
    )
    output = tmp_path / "sync"
    _, initial = ArcGraphIndexer(project, output).build()
    source.write_text(
        "def second(): return 8\ndef g(): return second()\n", encoding="utf-8"
    )
    synced = ArcGraphReindexer(project, output).reindex_changed()
    assert synced["status"] == "reindexed"
    ArcGraphIndexer(project, tmp_path / "full").build()
    assert graph_records(output) == graph_records(tmp_path / "full")
    assert not any(
        e.target == "fn:demo.first"
        for e in GraphStoreReader.from_current(output).read_edges()
    )
    assert (generation_directory(initial) / "pyright.json").is_file()
    source.write_text("def second(): return 8\ndef g(): return 0\n", encoding="utf-8")
    ArcGraphReindexer(project, output).reindex_changed()
    generations = list((output / "semantic/generations").glob("*/manifest.json"))
    assert len(generations) == 2
    assert not generation_directory(initial).exists()


def test_enabled_sync_preserves_explicit_coverage_and_runtime_inputs(
    prepared, tmp_path
):
    import json

    project, _, _, _ = prepared
    configure(project)
    source = project / "demo.py"
    source.write_text(
        "def target(): return 7\ndef g(): return target()\n", encoding="utf-8"
    )
    coverage = tmp_path / "explicit-coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "meta": {"format": 3},
                "files": {str(source): {"executed_lines": [1, 2], "missing_lines": []}},
            }
        ),
        encoding="utf-8",
    )
    trace = tmp_path / "explicit-runtime.json"
    trace.write_text(
        json.dumps(
            {
                "trace_run": {
                    "trace_run_id": "sync-inputs",
                    "source": "pytest",
                    "frontend_version": "ArcGraph-runtime-trace-v1",
                },
                "events": [
                    {
                        "kind": "dynamic_call",
                        "source": "fn:demo.g",
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
    inputs = {"coverage_path": coverage, "runtime_trace_path": trace}
    output = tmp_path / "sync"
    ArcGraphIndexer(project, output, **inputs).build()
    source.write_text(
        "def target(): return 7\ndef g(): return target() + 1\n", encoding="utf-8"
    )
    ArcGraphReindexer(project, output).reindex_changed()
    ArcGraphIndexer(project, tmp_path / "full", **inputs).build()
    assert graph_records(output) == graph_records(tmp_path / "full")
    synced = GraphStoreReader.from_current(output)
    full = GraphStoreReader.from_current(tmp_path / "full")
    assert synced.metadata["coverage_metrics"] == full.metadata["coverage_metrics"]
    assert {
        k: v
        for k, v in synced.metadata["runtime_metrics"].items()
        if k != "duration_seconds"
    } == {
        k: v
        for k, v in full.metadata["runtime_metrics"].items()
        if k != "duration_seconds"
    }
    # The explicit report predates the edit, so both paths may mark it stale.
    assert synced.metadata["coverage_metrics"]["status"] in ("available", "partial")
    assert synced.metadata["coverage_metrics"]["path"] == str(coverage)
    assert synced.metadata["runtime_metrics"]["trace_path"] == str(trace)
    assert any(e.kind == "covers" for e in synced.read_edges())


def test_semantic_definition_time_calls_keep_initializes_without_duplicate_calls(
    prepared, tmp_path
):
    project, _, _, _ = prepared
    configure(project)
    (project / "demo.py").write_text(
        "def target(): return 7\n"
        "def decorate(value): return lambda f: f\n"
        "@decorate(target())\n"
        "def g(value=target()): return target()\n"
        "class C:\n    value = target()\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    ArcGraphIndexer(project, output).build()
    edges = GraphStoreReader.from_current(output).read_edges()
    sites = [
        (e.kind, e.source, f["line"], f["column"])
        for e in edges
        if e.target == "fn:demo.target"
        for f in e.properties.get("callsites", [e.properties.get("callsite", {})])
        if f
    ]
    assert sites
    assert len(sites) == len(set((s, line, col) for _, s, line, col in sites)), sites
    assert all(
        kind == "initializes"
        for kind, _, line, col in sites
        if line in (3, 6) or (line == 4 and col == 12)
    ), sites
    assert ("calls", "fn:demo.g", 4, 30) in sites, sites


def test_partly_host_parsed_files_are_explicitly_uncovered(
    prepared, tmp_path, monkeypatch
):
    import subprocess
    from arcgraph.core.structure import StructureView

    project, _, _, _ = prepared
    configure(project)
    (project / "demo.py").unlink()
    for index in range(256):
        (project / f"a{index:03}.py").write_text(
            "def f(): return 1\n", encoding="utf-8"
        )
    (project / "zzz.py").write_text(
        "def target(): return 7\ndef g(): return target()\n", encoding="utf-8"
    )
    original = StructureView._read_project

    def partial(self, paths):
        if len(paths) == 256:
            raise subprocess.CalledProcessError(7, [str(self.interpreter)])
        return original(self, paths)

    monkeypatch.setattr(StructureView, "_read_project", partial)
    metadata, directory = ArcGraphIndexer(project, tmp_path / "out").build()
    state = metadata.toolchain_status["semantic_core"]
    assert state["status"] == "partial", state
    assert "256" in state["reason"] and "host_fallback" in state["reason"]
    from arcgraph.semantic.journal import load
    from arcgraph.semantic.disk_structure import read

    generation = generation_directory(directory)
    assert load(generation / "manifest.json")["complete"] is True
    structure = read(generation / "structure.jsonl")
    assert sum(f.status == "host_fallback" for f in structure.files) == 256
    assert {r.path for r in structure.records} == {"zzz.py"}


def test_semantic_config_and_dependency_changes_cannot_be_fresh(prepared, tmp_path):
    from arcgraph.core.freshness import compute_freshness

    project, environment, _, _ = prepared
    configure(project)
    (project / "demo.py").write_text(
        "def target(): return 7\ndef g(): return target()\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    ArcGraphIndexer(project, output).build()
    reader = GraphStoreReader.from_current(output)
    assert compute_freshness(reader).status == "fresh"
    project_config = project / "pyproject.toml"
    source = project_config.read_text(encoding="utf-8")
    project_config.write_text(
        source.replace("semantic_core = true", "semantic_core = false"),
        encoding="utf-8",
    )
    assert compute_freshness(reader).status == "stale"
    project_config.write_text(source, encoding="utf-8")
    sites = list(environment.glob("lib/python*/site-packages"))
    site = sites[0] if sites else environment / "Lib/site-packages"
    (site / "new-input.pyi").write_text("def target() -> str: ...\n", encoding="utf-8")
    assert compute_freshness(reader).status == "stale"


def test_publish_rejects_changed_generation_artifact_and_pointer(
    prepared, tmp_path, monkeypatch
):
    from arcgraph.semantic.build import SemanticBuild

    project, _, _, _ = prepared
    configure(project)
    (project / "demo.py").write_text(
        "def target(): return 7\ndef g(): return target()\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    ArcGraphIndexer(project, output).build()
    previous = (output / "current.json").read_bytes()
    original = SemanticBuild.publish

    def corrupt(self, directory):
        (self.generation / "pyright.json").write_text("{}", encoding="utf-8")
        return original(self, directory)

    with monkeypatch.context() as mutation:
        mutation.setattr(SemanticBuild, "publish", corrupt)
        with pytest.raises(ValueError, match="generation artifact changed"):
            ArcGraphIndexer(project, output).build()
    assert (output / "current.json").read_bytes() == previous

    def competing(self, directory):
        self.pointer.write_bytes(previous + b"\n")
        return original(self, directory)

    monkeypatch.setattr(SemanticBuild, "publish", competing)
    with pytest.raises(RuntimeError, match="publish revision changed"):
        ArcGraphIndexer(project, output).build()
    assert (output / "current.json").read_bytes() == previous + b"\n"


@pytest.mark.parametrize("state", ["active", "unavailable"])
def test_semantic_read_rejects_linked_manifest_or_failed_request(
    prepared, tmp_path, monkeypatch, state
):
    from arcgraph.core.freshness import compute_freshness
    from arcgraph.semantic import build

    project, _, _, _ = prepared
    configure(project)
    (project / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    if state == "unavailable":
        monkeypatch.setattr(build, "admit_structure", lambda *args: (False, "rejected"))
    output = tmp_path / "out"
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == state
    reader = GraphStoreReader.from_current(output)
    assert compute_freshness(reader).status == "fresh"
    path = (
        generation_directory(directory) / "manifest.json"
        if state == "active"
        else directory / "semantic-request.json"
    )
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    freshness = compute_freshness(reader)
    assert freshness.status == "stale"
    assert freshness.reason == "semantic_generation_unsafe"
