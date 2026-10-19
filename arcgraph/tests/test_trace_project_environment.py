"""Trace execution belongs to the analyzed environment, not the tool's venv."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.trace import run_runtime_trace
from arcgraph.tests.project_environment_support import (
    test_dependencies as add_test_deps,
)


def project_env(root):
    env = root / "env"
    result = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(env)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    add_test_deps(env)
    (root / "pyproject.toml").write_text(
        '[tool.arcgraph]\npython_environment="env"\n', encoding="utf-8"
    )
    return env, env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def sources(root, expected_prefix=None):
    (root / "service.py").write_text("def value(): return 4\n", encoding="utf-8")
    (root / "test_service.py").write_text(
        "import sys\nfrom service import value\ndef test_value():\n    assert value() == 4\n"
        + (
            "    assert sys.prefix == " + repr(str(expected_prefix)) + "\n"
            if expected_prefix
            else ""
        ),
        encoding="utf-8",
    )


def test_trace_automatically_uses_configured_project_environment(tmp_path):
    env, _ = project_env(tmp_path)
    sources(tmp_path, env)
    output = tmp_path / "trace.json"
    result = run_runtime_trace(
        repo_root=tmp_path,
        output_path=output,
        pytest_args=["test_service.py", "-q"],
        source_roots=[SourceRoot(".")],
    )
    assert result["status"] == "pass"
    assert result.get("interpreter_source") == "project_environment"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["trace_run"]["python_prefix"] == str(env)
    assert any(e["target"] == "fn:service.value" for e in payload["events"])


def test_trace_cli_can_select_an_interpreter_without_installing_arcgraph(tmp_path):
    env, python = project_env(tmp_path)
    sources(tmp_path, env)
    cli = Path(__file__).resolve().parents[2] / "scripts/arcgraph.py"
    output = tmp_path / "trace.json"
    result = subprocess.run(
        [
            sys.executable,
            str(cli),
            "--repo-root",
            str(tmp_path),
            "trace",
            "run",
            "--output",
            str(output),
            "--python",
            str(python),
            "--",
            "test_service.py",
            "-q",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["trace_run"]["python_prefix"] == str(env)
    assert payload["trace_run"]["interpreter_source"] == "explicit"


def test_trace_missing_environment_reports_its_host_fallback(tmp_path):
    sources(tmp_path)
    output = tmp_path / "trace.json"
    result = run_runtime_trace(
        repo_root=tmp_path,
        output_path=output,
        pytest_args=["test_service.py", "-q"],
        source_roots=[SourceRoot(".")],
    )
    assert result["status"] == "pass"
    assert result.get("interpreter_source") == "host_fallback"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert any(
        d["kind"] == "project_interpreter_missing"
        for d in payload["trace_run"]["environment_diagnostics"]
    )
    assert (
        result["environment_diagnostics"]
        == payload["trace_run"]["environment_diagnostics"]
    )


def test_project_interpreter_probe_retains_the_venv_path(tmp_path):
    from arcgraph.core import project_environment

    probe = getattr(project_environment, "project_interpreter_path", None)
    assert callable(probe)
    _, python = project_env(tmp_path)
    assert probe(tmp_path) == python
    assert probe(tmp_path, sys.executable) == Path(sys.executable).absolute()
    assert probe(tmp_path / "missing") is None
    with pytest.raises(RuntimeError, match="interpreter"):
        probe(tmp_path, tmp_path / "does-not-exist")


def test_doctor_reports_trace_environment_downgrade(tmp_path):
    from arcgraph.interfaces.cli_support import python_checks

    missing = next(
        (
            c
            for c in python_checks(tmp_path)
            if c["name"] == "project_trace_interpreter"
        ),
        None,
    )
    assert missing is not None
    assert missing["status"] == "warn"
    _, python = project_env(tmp_path)
    available = next(
        c for c in python_checks(tmp_path) if c["name"] == "project_trace_interpreter"
    )
    assert available["status"] == "pass"
    assert str(python) in available["message"]
