"""Review regressions, runnable unchanged against e2d85fe."""

import json
import marshal
import os
import shutil
import subprocess
import sys

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.core.structure import StructureView
from arcgraph.interfaces.cli_support import python_checks
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.tests.test_summary_history import normalized

MODERN = "def identity[T](value: T) -> T:\n    return value\n"


def modern_python():
    python = os.environ.get("ARCGRAPH_TEST_PROJECT_PYTHON") or shutil.which(
        "python3.14"
    )
    if not python:
        pytest.skip("A provisioned Python 3.14 is required")
    return python


def fake_environment(root, mode="good", requirement=">=3.14"):
    """An executable that succeeds on empty requests but can fail on real work."""
    python = modern_python()
    env = root / ".venv"
    (env / "bin").mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    flag = root / "parser-mode"
    flag.write_text(mode, encoding="utf-8")
    executable = env / "bin/python"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import marshal, pathlib, subprocess, sys, time\n"
        "request = marshal.load(sys.stdin.buffer)\n"
        f"mode = pathlib.Path({str(flag)!r}).read_text()\n"
        "if mode == 'crash' or (request and mode == 'exit'):\n"
        "    raise SystemExit(3)\n"
        "if request and mode == 'timeout':\n"
        "    time.sleep(10)\n"
        "if request and mode == 'protocol':\n"
        "    sys.stdout.buffer.write(b'not-marshal')\n"
        "    raise SystemExit(0)\n"
        "if request and mode == 'bad_record':\n"
        "    marshal.dump((1, '3.14.6', [], {p: ((0, (), ()), None) for p in request}), sys.stdout.buffer, 4)\n"
        "    raise SystemExit(0)\n"
        "if request and mode == 'incomplete':\n"
        "    marshal.dump((1, '3.14.6', [], {}), sys.stdout.buffer, 4)\n"
        "    raise SystemExit(0)\n"
        "if mode == 'chunk' and 'sample_300.py' in request:\n"
        "    raise SystemExit(3)\n"
        f"result = subprocess.run([{python!r}, '-I', '-S', '-c', sys.argv[-1]], "
        "input=marshal.dumps(request, 4), capture_output=True)\n"
        "sys.stdout.buffer.write(result.stdout)\n"
        "sys.stderr.buffer.write(result.stderr)\n"
        "raise SystemExit(result.returncode)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    (root / "pyproject.toml").write_text(
        f'[project]\nrequires-python = "{requirement}"\n', encoding="utf-8"
    )
    return executable, flag


def analyze(root):
    files = FileScanner(root, [SourceRoot(".")]).scan()
    return PythonGraphAnalyzer(repo_root=root).analyze(files)


def check(root, name):
    return next(c for c in python_checks(root) if c["name"] == name)


def snapshot(output):
    store = GraphStoreReader.from_current(output)
    return {
        "summary": normalized(store.metadata),
        "nodes": sorted(
            (n.model_dump(mode="json") for n in store.read_nodes()),
            key=lambda n: n["id"],
        ),
        "edges": sorted(
            (e.model_dump(mode="json") for e in store.read_edges()),
            key=lambda e: json.dumps(e, sort_keys=True),
        ),
        "parser": store.metadata["toolchain_status"]["python_structure"],
    }


def test_type_parameter_calls_have_definition_callsites_and_edges(tmp_path):
    fake_environment(tmp_path)
    functions = [
        "bound",
        "constraint",
        "default",
        "class_bound",
        "class_constraint",
        "class_default",
        "alias_bound",
        "alias_constraint",
        "alias_default",
        "nested_bound",
        "nested_alias",
        "nested_class",
        "class_alias",
        "deep_bound",
    ]
    source = "".join(f"def {name}():\n    return int\n" for name in functions)
    source += (
        "def generic[T: bound(), U: (constraint(), int), V = default()]():\n    pass\n"
        "class Box[T: class_bound(), U: (class_constraint(), int), V = class_default()]:\n"
        "    class Nested[T: nested_class()]:\n"
        "        class Deeper[T: deep_bound()]:\n            pass\n"
        "    type ClassAlias[T = class_alias()] = list[T]\n"
        "type Alias[T: alias_bound(), U: (alias_constraint(), int), V = alias_default()] = tuple[T, U, V]\n"
        "def outer():\n"
        "    async def inner[T: nested_bound()]():\n        pass\n"
        "    type Local[T = nested_alias()] = list[T]\n"
    )
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    graph = analyze(tmp_path)
    assert not [w for w in graph.warnings if w.kind == "parse_error"]
    calls = [c for n in graph.nodes for c in n.properties.get("callsites", [])]
    for name in functions:
        assert any(
            c["name"] == name for c in calls
        ), f"Missing type parameter call: {name}"
        assert any(
            e.target == f"fn:sample.{name}" and e.kind == "initializes"
            for e in graph.edges
        ), name


@pytest.mark.parametrize(
    "mode, reason",
    [
        ("exit", "exit code 3"),
        ("timeout", "timed out"),
        ("protocol", "protocol error"),
        ("bad_record", "protocol error"),
        ("incomplete", "protocol error"),
    ],
)
def test_doctor_real_request_agrees_with_build_fallback(
    tmp_path, monkeypatch, mode, reason
):
    executable, _ = fake_environment(tmp_path, mode)
    (tmp_path / "good.py").write_text(MODERN, encoding="utf-8")
    run = subprocess.run

    def quick_timeout(args, **kwargs):
        if (
            str(args[0]) == str(executable)
            and mode == "timeout"
            and marshal.loads(kwargs["input"])
        ):
            kwargs["timeout"] = 0.3
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", quick_timeout)
    try:
        graph = analyze(tmp_path)
    except (IndexError, TypeError, ValueError, AttributeError) as exc:
        assert False, f"Protocol failure escaped host fallback: {type(exc).__name__}"
    state = graph.toolchain_status["python_structure"]
    doctor = check(tmp_path, "project_python")
    assert state["status"] == "host_fallback"
    assert doctor["status"] == "warn", "Doctor only probed an empty request"
    assert doctor["message"] == state["reason"]
    assert reason in state["reason"]


def test_failed_batch_no_change_sync_is_stable_and_recovers(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _, flag = fake_environment(root, "exit")
    (root / "good.py").write_text(MODERN, encoding="utf-8")
    roots = [SourceRoot(".")]
    output = tmp_path / "sync"
    ArcGraphIndexer(root, output, roots).build()
    original = snapshot(output)
    for _ in range(2):
        result = ArcGraphReindexer(root, output, roots).reindex_changed()
        assert result["status"] == "unchanged", result
        assert snapshot(output) == original
    # The interpreter's bytes/mtime stay identical: its external condition recovers.
    full = tmp_path / "full"
    shutil.copytree(output, full)
    flag.write_text("good", encoding="utf-8")
    result = ArcGraphReindexer(root, output, roots).reindex_changed()
    assert result["status"] != "unchanged"
    ArcGraphIndexer(root, full, roots).build()
    assert snapshot(output) == snapshot(full)
    assert any(n["id"] == "fn:good.identity" for n in snapshot(output)["nodes"])


def test_startup_failure_warns_even_when_host_meets_requirement(tmp_path):
    fake_environment(tmp_path, "crash", ">=3.11")
    (tmp_path / "good.py").write_text(MODERN, encoding="utf-8")
    state = analyze(tmp_path).toolchain_status["python_structure"]
    doctor = check(tmp_path, "project_python")
    assert doctor["status"] == "warn"
    assert doctor["message"] == state["reason"]
    assert "exit code 3" in doctor["message"]


def test_project_syntax_error_does_not_suggest_newer_interpreter(tmp_path):
    fake_environment(tmp_path)
    (tmp_path / "bad.py").write_text("def (", encoding="utf-8")
    (tmp_path / "good.py").write_text(MODERN, encoding="utf-8")
    graph = analyze(tmp_path)
    error = next(w for w in graph.warnings if w.kind == "parse_error")
    assert graph.toolchain_status["python_structure"]["status"] == "project"
    assert error.message == "invalid syntax (bad.py, line 1)"
    assert any(n.id == "fn:good.identity" for n in graph.nodes)


def test_trace_interpreter_stays_pass_when_only_version_is_wrong(tmp_path):
    fake_environment(tmp_path, requirement=">=99.0")
    (tmp_path / "good.py").write_text("def f():\n    pass\n", encoding="utf-8")
    assert check(tmp_path, "project_trace_interpreter")["status"] == "pass"
    assert check(tmp_path, "project_python")["status"] == "warn"
    assert (
        analyze(tmp_path).toolchain_status["python_structure"]["status"]
        == "host_fallback"
    )


def test_large_project_uses_bounded_requests_and_only_failed_chunk_falls_back(
    tmp_path, monkeypatch
):
    executable, flag = fake_environment(tmp_path)
    paths = {}
    for i in range(600):
        path = tmp_path / f"sample_{i:03d}.py"
        path.write_text(MODERN, encoding="utf-8")
        paths[path.name] = path
    run = subprocess.run
    requests = []

    def limited_budget(args, **kwargs):
        if str(args[0]) == str(executable):
            count = len(marshal.loads(kwargs["input"]))
            requests.append(count)
            # A slow interpreter fits individual blocks, not an unbounded batch.
            if count > 256:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", limited_budget)
    view = StructureView(tmp_path)
    trees, warnings = view.read(paths)
    assert not warnings, "Total project size caused host fallback"
    assert len(trees) == 600
    assert max(requests) <= 256
    flag.write_text("chunk", encoding="utf-8")
    view = StructureView(tmp_path)
    trees, warnings = view.read(paths)
    assert 0 < len(warnings) <= 256
    assert len(trees) + len(warnings) == 600
    assert "sample_000.py" in trees and "sample_599.py" in trees
    assert "exit code 3" in view.status()["reason"]


def test_partial_fallback_sync_is_stable_recovers_and_matches_full(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _, flag = fake_environment(root, "chunk")
    for i in range(300, 558):
        (root / f"sample_{i:03d}.py").write_text(MODERN, encoding="utf-8")
    roots = [SourceRoot(".")]
    output = tmp_path / "sync"
    ArcGraphIndexer(root, output, roots).build()
    # A failure of the first block must not discard the healthy second block.
    assert any(n["id"] == "fn:sample_557.identity" for n in snapshot(output)["nodes"])
    assert (
        check(root, "project_python")["message"] == snapshot(output)["parser"]["reason"]
    )
    assert (
        ArcGraphReindexer(root, output, roots).reindex_changed()["status"]
        == "unchanged"
    )
    full = tmp_path / "full"
    shutil.copytree(output, full)
    (root / "sample_557.py").write_text(MODERN + "\nx = 1\n", encoding="utf-8")
    ArcGraphReindexer(root, output, roots).reindex_changed()
    ArcGraphIndexer(root, full, roots).build()
    assert snapshot(output) == snapshot(full)
    fresh = tmp_path / "recovered"
    shutil.copytree(output, fresh)
    flag.write_text("good", encoding="utf-8")
    assert (
        ArcGraphReindexer(root, output, roots).reindex_changed()["status"]
        != "unchanged"
    )
    ArcGraphIndexer(root, fresh, roots).build()
    assert snapshot(output) == snapshot(fresh)
