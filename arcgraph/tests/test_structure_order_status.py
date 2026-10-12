"""Block-dependent failures must survive private cache serialization."""

import shutil

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.tests.test_structure_rework import (
    MODERN,
    check,
    fake_environment,
    snapshot,
)


def ordered_failure_project(root):
    executable, flag = fake_environment(root, "order")
    script = executable.read_text(encoding="utf-8")
    script = script.replace(
        "result = subprocess.run(",
        "if mode == 'order' and any(p.endswith('poison.py') for p in request):\n"
        "    raise SystemExit(3 if len(request) == 1 else 4)\n"
        "result = subprocess.run(",
    )
    executable.write_text(script, encoding="utf-8")
    pkg = root / "pkg"
    (pkg / "b").mkdir(parents=True)
    for i in range(256):
        (pkg / f"f{i:03d}.py").write_text(MODERN, encoding="utf-8")
    (pkg / "b/poison.py").write_text(MODERN, encoding="utf-8")
    paths = [file.path for file in FileScanner(root, [SourceRoot(".")]).scan()]
    assert paths[-1] == "pkg/b/poison.py"
    assert sorted(paths)[0] == "pkg/b/poison.py"
    return executable, flag


def test_block_membership_stays_identical_on_no_change_sync_and_recovery(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    executable, flag = ordered_failure_project(root)
    roots = [SourceRoot(".")]
    output = tmp_path / "sync"
    ArcGraphIndexer(root, output, roots).build()
    original = snapshot(output)
    assert "exit code 3" in original["parser"]["reason"]
    for _ in range(2):
        result = ArcGraphReindexer(root, output, roots).reindex_changed()
        assert result["status"] == "unchanged", result
        assert snapshot(output) == original
        assert len(list((output / "builds").iterdir())) == 1
    # Source changes in a failing generation must preserve the same block plan.
    full = tmp_path / "edited-full"
    shutil.copytree(output, full)
    (root / "pkg/f255.py").write_text(MODERN + "\nx = 1\n", encoding="utf-8")
    ArcGraphReindexer(root, output, roots).reindex_changed()
    ArcGraphIndexer(root, full, roots).build()
    assert snapshot(output) == snapshot(full)
    assert (
        ArcGraphReindexer(root, output, roots).reindex_changed()["status"]
        == "unchanged"
    )
    # Recover without changing executable bytes, mtime, or project Python files.
    before = (executable.read_bytes(), executable.stat().st_mtime_ns)
    recovered = tmp_path / "recovered-full"
    shutil.copytree(output, recovered)
    flag.write_text("good", encoding="utf-8")
    assert (executable.read_bytes(), executable.stat().st_mtime_ns) == before
    assert (
        ArcGraphReindexer(root, output, roots).reindex_changed()["status"]
        != "unchanged"
    )
    ArcGraphIndexer(root, recovered, roots).build()
    assert snapshot(output) == snapshot(recovered)
    assert snapshot(output)["parser"]["status"] == "project"
    assert (
        ArcGraphReindexer(root, output, roots).reindex_changed()["status"]
        == "unchanged"
    )


def test_partial_fallback_reports_project_interpreter_and_version(tmp_path):
    executable, _ = ordered_failure_project(tmp_path)
    output = tmp_path / "out"
    ArcGraphIndexer(tmp_path, output, [SourceRoot(".")]).build()
    reader = GraphStoreReader.from_current(output)
    state = reader.metadata["toolchain_status"]["python_structure"]
    assert state["status"] == "partial_fallback"
    assert state["interpreter"] == str(executable)
    assert state["version"].startswith("3.14.")
    assert set(state) == {"status", "interpreter", "version", "reason", "fix"}
    assert "exit code 3" in state["reason"] and "other blocks" in state["reason"]
    assert len([n for n in reader.read_nodes() if n.name == "identity"]) == 256
    assert [w.path for w in reader.read_warnings() if w.kind == "parse_error"] == [
        "pkg/b/poison.py"
    ]
    doctor = check(tmp_path, "project_python")
    assert doctor["status"] == "warn" and doctor["message"] == state["reason"]
    assert check(tmp_path, "project_trace_interpreter")["status"] == "pass"
