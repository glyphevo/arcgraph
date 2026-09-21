from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces import cli as cli_module
from arcgraph.interfaces import cli_workspace
from arcgraph.interfaces.benchmark import (
    run_agent_startup_benchmark,
    run_benchmark_suite,
)
from arcgraph.pipeline.indexer import ArcGraphIndexer


def test_agent_startup_benchmark_records_agent_command_timings(
    tmp_path: Path,
) -> None:
    output_dir = _build_minimal_index(tmp_path)
    calls: list[list[str]] = []

    def fake_runner(
        argv: list[str], repo_root: Path, timeout_seconds: float
    ) -> dict[str, Any]:
        calls.append(argv)
        command = _subcommand(argv)
        duration = 750.0 if command == "current" else 100.0
        return {"exit_code": 0, "duration_ms": duration}

    payload = run_agent_startup_benchmark(
        repo_root=tmp_path,
        output_dir=output_dir,
        target="pkg.main",
        iterations=2,
        warmups=1,
        command_set="agent",
        timeout_seconds=5,
        runner=fake_runner,
    )

    assert payload["status"] == "warn"
    assert payload["failures"] == []
    assert {command["name"] for command in payload["commands"]} == {
        "current",
        "context",
        "explain",
    }
    assert all(command["duration_ms"]["count"] == 2 for command in payload["commands"])
    assert all(len(command["attempts"]) == 3 for command in payload["commands"])
    assert any("current p95" in warning for warning in payload["warnings"])
    assert len(calls) == 9


def test_agent_startup_benchmark_writes_output_and_fails_on_subprocess_error(
    tmp_path: Path,
) -> None:
    output_dir = _build_minimal_index(tmp_path)
    output_path = tmp_path / "reports" / "agent-startup.json"

    def failing_runner(
        argv: list[str], repo_root: Path, timeout_seconds: float
    ) -> dict[str, Any]:
        command = _subcommand(argv)
        return {
            "exit_code": 2 if command == "current" else 0,
            "duration_ms": 50.0,
            "error": "boom" if command == "current" else None,
        }

    payload = run_agent_startup_benchmark(
        repo_root=tmp_path,
        output_dir=output_dir,
        target="pkg.main",
        iterations=1,
        warmups=0,
        command_set="minimal",
        timeout_seconds=5,
        output_path=output_path,
        runner=failing_runner,
    )

    assert payload["status"] == "fail"
    assert payload["_exit_code"] == 2
    assert payload["failures"][0]["command"] == "current"
    assert payload["output_path"] == str(output_path.resolve())
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["status"] == "fail"
    assert written["failures"][0]["error"] == "boom"


def test_agent_startup_benchmark_cli_forwards_options(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    captured: dict[str, Any] = {}

    def fake_benchmark(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"schema_version": "1.0", "status": "pass", "commands": []}

    monkeypatch.setattr(cli_workspace, "run_agent_startup_benchmark", fake_benchmark)

    exit_code = cli_module.main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            "out",
            "benchmark",
            "agent-startup",
            "--target",
            "pkg.main",
            "--iterations",
            "3",
            "--warmups",
            "0",
            "--command-set",
            "minimal",
            "--timeout-seconds",
            "7",
            "--output",
            "reports/agent.json",
        ]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "pass"
    assert captured["repo_root"] == tmp_path.resolve()
    assert captured["output_dir"] == (tmp_path / "out").resolve()
    assert captured["target"] == "pkg.main"
    assert captured["iterations"] == 3
    assert captured["warmups"] == 0
    assert captured["command_set"] == "minimal"
    assert captured["timeout_seconds"] == 7
    assert captured["output_path"] == (tmp_path / "reports" / "agent.json")


def test_benchmark_suite_reports_quality_snapshot_and_query_probes(
    tmp_path: Path,
) -> None:
    output_dir = _build_minimal_index(tmp_path)
    calls: list[list[str]] = []

    def fake_runner(
        argv: list[str], repo_root: Path, timeout_seconds: float
    ) -> dict[str, Any]:
        calls.append(argv)
        return {"exit_code": 0, "duration_ms": 100.0}

    payload = run_benchmark_suite(
        repo_root=tmp_path,
        output_dir=output_dir,
        target="pkg.main",
        iterations=2,
        warmups=1,
        timeout_seconds=5,
        runner=fake_runner,
    )

    assert payload["status"] == "pass"
    assert set(payload) >= {
        "agent_startup",
        "quality_snapshot",
        "query_probes",
        "mcp_serve_recommendation",
    }
    assert payload["agent_startup"]["commands"][0]["duration_ms"]["count"] == 2
    assert len(payload["agent_startup"]["commands"][0]["attempts"]) == 3
    assert {command["name"] for command in payload["query_probes"]["commands"]} == {
        "current",
        "symbol",
        "context",
        "explain",
        "semantic-stats",
    }
    assert payload["quality_snapshot"]["counts"]["files"] == 1
    assert (
        payload["quality_snapshot"]["semantic_resolution_rates"]["overall"] is not None
    )
    assert payload["mcp_serve_recommendation"]["status"] == "not_needed_now"
    assert len(calls) == 24


def test_benchmark_suite_budget_warning_recommends_investigate(
    tmp_path: Path,
) -> None:
    output_dir = _build_minimal_index(tmp_path)

    def slow_context_runner(
        argv: list[str], repo_root: Path, timeout_seconds: float
    ) -> dict[str, Any]:
        command = _subcommand(argv)
        return {
            "exit_code": 0,
            "duration_ms": 4000.0 if command == "context" else 100.0,
        }

    payload = run_benchmark_suite(
        repo_root=tmp_path,
        output_dir=output_dir,
        target="pkg.main",
        iterations=1,
        warmups=0,
        timeout_seconds=5,
        runner=slow_context_runner,
    )

    assert payload["status"] == "warn"
    assert "_exit_code" not in payload
    assert payload["mcp_serve_recommendation"]["status"] == "investigate"
    assert payload["mcp_serve_recommendation"]["violating_commands"] == ["context"]
    assert any("context p95" in warning for warning in payload["warnings"])


def test_benchmark_suite_fails_on_subprocess_error(tmp_path: Path) -> None:
    output_dir = _build_minimal_index(tmp_path)

    def failing_runner(
        argv: list[str], repo_root: Path, timeout_seconds: float
    ) -> dict[str, Any]:
        command = _subcommand(argv)
        return {
            "exit_code": 2 if command == "symbol" else 0,
            "duration_ms": 50.0,
            "error": "boom" if command == "symbol" else None,
        }

    payload = run_benchmark_suite(
        repo_root=tmp_path,
        output_dir=output_dir,
        target="pkg.main",
        iterations=1,
        warmups=0,
        timeout_seconds=5,
        runner=failing_runner,
    )

    assert payload["status"] == "fail"
    assert payload["_exit_code"] == 2
    assert payload["failures"][0]["command"] == "symbol"
    assert payload["mcp_serve_recommendation"]["status"] == "needed_candidate"


def test_benchmark_suite_writes_output_and_isolates_build(
    tmp_path: Path,
) -> None:
    output_dir = _build_minimal_index(tmp_path)
    output_path = tmp_path / "reports" / "suite.json"
    build_argvs: list[list[str]] = []

    def fake_runner(
        argv: list[str], repo_root: Path, timeout_seconds: float
    ) -> dict[str, Any]:
        if _subcommand(argv) == "build":
            build_argvs.append(argv)
        return {"exit_code": 0, "duration_ms": 100.0}

    payload = run_benchmark_suite(
        repo_root=tmp_path,
        output_dir=output_dir,
        target="pkg.main",
        iterations=1,
        warmups=0,
        timeout_seconds=5,
        output_path=output_path,
        include_build=True,
        runner=fake_runner,
    )

    assert payload["build_probe"]["isolated"] is True
    assert payload["build_probe"]["output_dir"] != str(output_dir)
    assert build_argvs
    assert str(output_dir) not in build_argvs[0]
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["build_probe"]["isolated"] is True
    assert written["output_path"] == str(output_path.resolve())


def test_benchmark_suite_cli_forwards_options(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    captured: dict[str, Any] = {}

    def fake_suite(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"schema_version": "1.0", "status": "pass"}

    monkeypatch.setattr(cli_workspace, "run_benchmark_suite", fake_suite)

    exit_code = cli_module.main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            "out",
            "benchmark",
            "suite",
            "--target",
            "pkg.main",
            "--iterations",
            "3",
            "--warmups",
            "0",
            "--timeout-seconds",
            "7",
            "--output",
            "reports/suite.json",
            "--include-build",
        ]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "pass"
    assert captured["repo_root"] == tmp_path.resolve()
    assert captured["output_dir"] == (tmp_path / "out").resolve()
    assert captured["target"] == "pkg.main"
    assert captured["iterations"] == 3
    assert captured["warmups"] == 0
    assert captured["timeout_seconds"] == 7
    assert captured["output_path"] == (tmp_path / "reports" / "suite.json")
    assert captured["include_build"] is True


def _build_minimal_index(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "pkg.py").write_text(
        "def main():\n    return 'ok'\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output" / "arcgraph"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    return output_dir


def _subcommand(argv: list[str]) -> str:
    for candidate in (
        "current",
        "context",
        "explain",
        "symbol",
        "semantic-stats",
        "build",
    ):
        if candidate in argv:
            return candidate
    raise AssertionError(f"Unable to find benchmark command in argv: {argv}")
