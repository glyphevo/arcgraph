"""D2 regression contracts, also runnable against the parent implementation."""

import ast
import json
import os
import subprocess
import sys

from arcgraph.tests.project_environment_support import modern_python

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.interfaces.cli_support import python_checks
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer
from arcgraph.pipeline.reindexer import ArcGraphReindexer

FALLBACK = (
    "No project interpreter found; Python structure uses the ArcGraph host parser. "
    "Use the project .venv or set tool.arcgraph.python_environment to an environment "
    "with a Python version satisfying requires-python."
)
MODERN = "def identity[T](value: T) -> T:\n    return value\n"


def project_env(root, *, modern=False):
    executable = sys.executable
    if modern:
        executable = modern_python()
    subprocess.run(
        [executable, "-I", "-m", "venv", "--without-pip", str(root / ".venv")],
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return root / ".venv"


def analyze(root):
    files = FileScanner(root, [SourceRoot(".")]).scan()
    return PythonGraphAnalyzer(repo_root=root).analyze(files)


def test_project_structure_does_not_parse_files_on_the_host(tmp_path, monkeypatch):
    project_env(tmp_path)
    (tmp_path / "sample.py").write_text(
        "def identity(value):\n    return value\n", encoding="utf-8"
    )
    original = ast.parse

    def reject(source, filename="<unknown>", *args, **kwargs):
        assert filename != "sample.py", "Project files were parsed on the host"
        return original(source, filename, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", reject)
    graph = analyze(tmp_path)
    assert any(n.id == "fn:sample.identity" for n in graph.nodes)


def test_project_structure_reads_new_syntax(tmp_path):
    project_env(tmp_path, modern=True)
    (tmp_path / "sample.py").write_text(MODERN, encoding="utf-8")
    graph = analyze(tmp_path)
    assert not [w for w in graph.warnings if w.kind == "parse_error"]
    function = next(n for n in graph.nodes if n.id == "fn:sample.identity")
    assert function.start_line == 1
    assert function.properties["returns"] == "T"


def test_missing_interpreter_fallback_is_explicit_in_summary_and_doctor(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def identity(value):\n    return value\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    ArcGraphIndexer(tmp_path, output, [SourceRoot(".")]).build()
    reader = GraphStoreReader.from_current(output)
    state = reader.metadata.get("toolchain_status", {}).get("python_structure")
    assert state is not None
    assert state["status"] == "host_fallback"
    assert state["reason"] == FALLBACK
    assert not [w for w in reader.read_warnings() if w.kind == "project_python"]
    assert not [
        d for d in reader.read_diagnostics() if d.diagnostic_kind == "project_python"
    ]
    assert (
        "No project interpreter found; trace run falls back to the ArcGraph host. "
        + FALLBACK
        == next(
            c["message"]
            for c in python_checks(tmp_path)
            if c["name"] == "project_trace_interpreter"
        )
    )
    assert any(n.id == "fn:sample.identity" for n in reader.read_nodes())


def test_unstartable_project_interpreter_falls_back(tmp_path):
    environment = project_env(tmp_path)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.unlink()
    python.write_bytes(b"invalid executable\n")
    python.chmod(0o755)
    (tmp_path / "sample.py").write_text(
        "def identity(value):\n    return value\n", encoding="utf-8"
    )
    graph = analyze(tmp_path)
    state = graph.toolchain_status.get("python_structure")
    assert state is not None
    assert "Project interpreter failed" in state["reason"]
    assert any(n.id == "fn:sample.identity" for n in graph.nodes)
    check = next(
        c for c in python_checks(tmp_path) if c["name"] == "project_trace_interpreter"
    )
    assert check["status"] == "warn"
    assert "host parser" in check["message"]


def test_wrong_project_version_falls_back(tmp_path):
    project_env(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=99.0"\n', encoding="utf-8"
    )
    (tmp_path / "sample.py").write_text(
        "def identity(value):\n    return value\n", encoding="utf-8"
    )
    graph = analyze(tmp_path)
    state = graph.toolchain_status.get("python_structure")
    assert state is not None
    assert "does not satisfy requires-python >=99.0" in state["reason"]
    assert any(n.id == "fn:sample.identity" for n in graph.nodes)
    check = next(c for c in python_checks(tmp_path) if c["name"] == "project_python")
    assert check["status"] == "warn"
    assert "does not satisfy requires-python >=99.0" in check["message"]


def test_isolated_structure_build_never_executes_project_or_site_code(
    tmp_path, monkeypatch
):
    environment = project_env(tmp_path, modern=True)
    site = (
        environment / "Lib/site-packages"
        if os.name == "nt"
        else next(environment.glob("lib/python*/site-packages"))
    )
    markers = [
        tmp_path / name for name in ("pth-marker", "site-marker", "module-marker")
    ]
    (site / "evil.pth").write_text(
        f"import pathlib; pathlib.Path({str(markers[0])!r}).touch()\n", encoding="utf-8"
    )
    (site / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(markers[1])!r}).touch()\n",
        encoding="utf-8",
    )
    (tmp_path / "danger.py").write_text(
        f"from pathlib import Path\nPath({str(markers[2])!r}).touch()\n",
        encoding="utf-8",
    )
    (tmp_path / "sample.py").write_text("import danger\n" + MODERN, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("VIRTUAL_ENV", str(environment))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    output = tmp_path / "out"
    ArcGraphIndexer(tmp_path, output, [SourceRoot(".")]).build()
    reader = GraphStoreReader.from_current(output)
    assert any(n.id == "fn:sample.identity" for n in reader.read_nodes())
    assert not any(p.exists() for p in markers)


def graph_snapshot(output):
    reader = GraphStoreReader.from_current(output)
    return (
        sorted(
            (n.model_dump(mode="json") for n in reader.read_nodes()),
            key=lambda n: n["id"],
        ),
        sorted(
            (e.model_dump(mode="json") for e in reader.read_edges()),
            key=lambda e: json.dumps(e, sort_keys=True),
        ),
    )


def test_modern_syntax_sync_matches_full_after_edit(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    project_env(root, modern=True)
    path = root / "sample.py"
    path.write_text(MODERN, encoding="utf-8")
    output = tmp_path / "sync"
    roots = [SourceRoot(".")]
    ArcGraphIndexer(root, output, roots).build()
    assert any(n["id"] == "fn:sample.identity" for n in graph_snapshot(output)[0])
    for step, source in enumerate(
        [
            MODERN + "\ndef _private(value):\n    return str(value)\n",
            MODERN.replace("return value", "return str(value)"),
        ]
    ):
        path.write_text(source, encoding="utf-8")
        ArcGraphReindexer(root, output, roots).reindex_changed()
        full = tmp_path / f"full-{step}"
        ArcGraphIndexer(root, full, roots).build()
        assert graph_snapshot(output) == graph_snapshot(full)


def test_missing_interpreter_retains_host_parse_errors(tmp_path):
    (tmp_path / "sample.py").write_text(MODERN, encoding="utf-8")
    graph = analyze(tmp_path)
    state = graph.toolchain_status.get("python_structure")
    assert state is not None
    assert state["reason"] == FALLBACK
    if sys.version_info < (3, 12):
        errors = [w for w in graph.warnings if w.kind == "parse_error"]
        assert len(errors) == 1
        assert "parsed with Python 3.11" in errors[0].message


def test_structure_preserves_unknown_syntax_fields_locations_and_constants(tmp_path):
    project_env(tmp_path, modern=True)
    (tmp_path / "sample.py").write_text(
        "type Alias[T: int = int] = list[T]\n"
        "def identity[T = str](value: T) -> T:\n"
        "    return t'{value}'\n"
        "VALUES = (..., b'abc', 2j, -1.5)\n",
        encoding="utf-8",
    )
    graph = analyze(tmp_path)
    assert not [w for w in graph.warnings if w.kind == "parse_error"]
    tree = graph.parsed_files["sample.py"]
    alias = tree.body[0]
    assert type(alias).__name__ == "TypeAlias"
    assert isinstance(alias, ast.stmt)
    parameter = alias.type_params[0]
    assert parameter.name == "T"
    assert parameter.bound.id == "int"
    assert parameter.default_value.id == "int"
    assert parameter.lineno == 1
    template = tree.body[1].body[0].value
    assert type(template).__name__ == "TemplateStr"
    assert isinstance(template, ast.expr)
    assert template.values[0].value.id == "value"
    constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    assert Ellipsis in constants and b"abc" in constants and 2j in constants


def test_sync_refreshes_when_project_interpreter_becomes_unavailable(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    environment = project_env(root, modern=True)
    (root / "sample.py").write_text(MODERN, encoding="utf-8")
    output = tmp_path / "sync"
    roots = [SourceRoot(".")]
    ArcGraphIndexer(root, output, roots).build()
    assert any(n["id"] == "fn:sample.identity" for n in graph_snapshot(output)[0])
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.unlink()
    python.write_bytes(b"invalid executable\n")
    python.chmod(0o755)
    ArcGraphReindexer(root, output, roots).reindex_changed()
    full = tmp_path / "full"
    ArcGraphIndexer(root, full, roots).build()
    assert graph_snapshot(output) == graph_snapshot(full)
    state = (
        GraphStoreReader.from_current(output)
        .metadata.get("toolchain_status", {})
        .get("python_structure")
    )
    assert state is not None
    assert "Project interpreter failed" in state["reason"]
