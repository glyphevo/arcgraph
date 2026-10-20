"""Failed semantic attempts retain exclusive phase accounting."""

from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.semantic import build, projection
from arcgraph.tests.test_semantic_isolation import prepared as prepared


@pytest.mark.parametrize("entry", ["before_parse", "structure_from_trees"])
def test_aborted_entry_updates_phase_and_total(monkeypatch, entry):
    semantic = build.SemanticBuild.__new__(build.SemanticBuild)
    semantic.timings = {"total": 0.0}
    semantic.indexer = SimpleNamespace(repo_root=Path("/unused"))
    semantic.interpreter = {}
    semantic.environment = None
    semantic.snapshot = object()
    semantic.root = Path("/unused")
    semantic.parser = None

    def fail(*args, **kwargs):
        time.sleep(0.2)
        raise RuntimeError("aborted entry")

    monkeypatch.setattr(build, "verify_interpreter", fail)
    monkeypatch.setattr(build, "analyze", fail)
    with pytest.raises(RuntimeError, match="aborted entry"):
        if entry == "before_parse":
            semantic.before_parse()
        else:
            semantic.structure_from_trees({}, [], [])
    phase = "interpreter_guard" if entry == "before_parse" else "structure_records"
    assert semantic.timings.get(phase, 0) >= 0.2
    # Unaccounted time is microseconds, but one preemption on a loaded CI runner
    # can add milliseconds; a phase missing from the total would differ by 100%.
    assert abs(semantic.timings["total"] - semantic.timings[phase]) <= (
        0.1 * semantic.timings["total"]
    )


@pytest.mark.parametrize("failed_phase", ["admission", "projection"])
def test_projection_internal_error_accounts_for_each_phase(monkeypatch, failed_phase):
    view = SimpleNamespace(bundle=SimpleNamespace(snapshot=object()), sites={"s": None})
    run = SimpleNamespace(answers=[SimpleNamespace(subject="s", candidates=[object()])])
    expected = SimpleNamespace(name="probe")
    timings = {}

    def fail(*args, **kwargs):
        time.sleep(0.02)
        raise ValueError("failed " + failed_phase)

    monkeypatch.setattr(
        projection,
        "admit",
        fail if failed_phase == "admission" else lambda *a: (True, None),
    )
    monkeypatch.setattr(projection, "relation", fail)
    with pytest.raises(ValueError, match="failed " + failed_phase):
        projection.project(view, run, run, (expected, expected), timings=timings)
    assert timings.get(failed_phase, 0) >= 0.02
    assert "admission" in timings and "projection" in timings


def test_project_value_error_publishes_complete_fallback_accounting(
    prepared, tmp_path, monkeypatch
):
    project, _, _, _ = prepared
    with (project / "pyproject.toml").open("a", encoding="utf-8") as stream:
        stream.write("semantic_core = true\n")

    def fail(*args, **kwargs):
        time.sleep(0.4)
        raise ValueError("probe-project-failed")

    monkeypatch.setattr(build, "project", fail)
    output = tmp_path / "out"
    metadata, _ = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "unavailable"
    timings = GraphStoreReader.from_current(output).metadata["phase_timings"][
        "semantic_core"
    ]
    accounted = sum(value for key, value in timings.items() if key != "total")
    assert abs(accounted - timings["total"]) <= 0.02 * timings["total"], timings
    assert "admission" in timings and timings.get("projection", 0) >= 0.4
