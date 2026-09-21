"""Agent-facing benchmark helpers for ArcGraph."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcgraph.core.metadata import current_commit
from arcgraph.core.query_engine import QueryEngine

DEFAULT_AGENT_STARTUP_TARGET = "arcgraph.interfaces.cli.main"
AGENT_STARTUP_SCHEMA_VERSION = "1.0"
BENCHMARK_SUITE_SCHEMA_VERSION = "1.0"
AGENT_STARTUP_BUDGETS_MS: dict[str, dict[str, float]] = {
    "current": {"p95": 500.0},
    "context": {"p95": 3000.0},
    "explain": {"p95": 3000.0},
}
QUERY_PROBE_BUDGETS_MS: dict[str, dict[str, float]] = {
    "current": {"p95": 500.0},
    "symbol": {"p95": 1000.0},
    "context": {"p95": 3000.0},
    "explain": {"p95": 3000.0},
    "semantic-stats": {"p95": 1500.0},
}


@dataclass(frozen=True)
class BenchmarkCommand:
    name: str
    args: tuple[str, ...]


def run_agent_startup_benchmark(
    *,
    repo_root: Path,
    output_dir: Path,
    target: str = DEFAULT_AGENT_STARTUP_TARGET,
    iterations: int = 5,
    warmups: int = 1,
    command_set: str = "agent",
    timeout_seconds: float = 30.0,
    output_path: Path | None = None,
    runner: Any | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise RuntimeError("--iterations must be at least 1.")
    if warmups < 0:
        raise RuntimeError("--warmups must be 0 or greater.")
    if timeout_seconds <= 0:
        raise RuntimeError("--timeout-seconds must be greater than 0.")
    commands = _benchmark_commands(command_set, target)
    cli_base = _cli_invocation(repo_root, output_dir)
    query_engine = QueryEngine(output_dir)
    current = query_engine.current()
    measurement = _measure_benchmark_commands(
        commands=commands,
        cli_base=cli_base,
        repo_root=repo_root,
        iterations=iterations,
        warmups=warmups,
        timeout_seconds=timeout_seconds,
        budgets=AGENT_STARTUP_BUDGETS_MS,
        runner=runner,
    )
    warnings = _budget_warnings(
        "Agent startup budget exceeded", measurement["budget_violations"]
    )

    payload = {
        "schema_version": AGENT_STARTUP_SCHEMA_VERSION,
        "status": "fail" if measurement["failures"] else "warn" if warnings else "pass",
        "repo_root": str(repo_root),
        "output_dir": str(output_dir),
        "commit_sha": current.get("commit_sha") or current_commit(repo_root),
        "index_version": current.get("index_version"),
        "command_set": command_set,
        "target": target,
        "commands": measurement["commands"],
        "failures": measurement["failures"],
        "warnings": warnings,
        "environment": {
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "cwd": str(repo_root),
        },
    }
    _write_payload_if_requested(payload, output_path)
    if measurement["failures"]:
        payload["_exit_code"] = 2
    return payload


def run_benchmark_suite(
    *,
    repo_root: Path,
    output_dir: Path,
    target: str = DEFAULT_AGENT_STARTUP_TARGET,
    iterations: int = 5,
    warmups: int = 1,
    timeout_seconds: float = 30.0,
    output_path: Path | None = None,
    include_build: bool = False,
    runner: Any | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise RuntimeError("--iterations must be at least 1.")
    if warmups < 0:
        raise RuntimeError("--warmups must be 0 or greater.")
    if timeout_seconds <= 0:
        raise RuntimeError("--timeout-seconds must be greater than 0.")

    query_engine = QueryEngine(output_dir)
    current = query_engine.current()
    semantic_stats = query_engine.semantic_stats()
    cli_base = _cli_invocation(repo_root, output_dir)

    agent_startup = run_agent_startup_benchmark(
        repo_root=repo_root,
        output_dir=output_dir,
        target=target,
        iterations=iterations,
        warmups=warmups,
        command_set="agent",
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    agent_exit_code = agent_startup.pop("_exit_code", 0)

    query_measurement = _measure_benchmark_commands(
        commands=_query_probe_commands(target),
        cli_base=cli_base,
        repo_root=repo_root,
        iterations=iterations,
        warmups=warmups,
        timeout_seconds=timeout_seconds,
        budgets=QUERY_PROBE_BUDGETS_MS,
        runner=runner,
    )
    warnings = [
        *agent_startup.get("warnings", []),
        *_budget_warnings(
            "Query probe budget exceeded", query_measurement["budget_violations"]
        ),
    ]
    failures = [
        *agent_startup.get("failures", []),
        *query_measurement["failures"],
    ]

    build_probe: dict[str, Any] | None = None
    if include_build:
        build_probe = _run_isolated_build_probe(
            repo_root=repo_root,
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        if build_probe["exit_code"] != 0:
            failures.append(
                {
                    "command": "build",
                    "run": 1,
                    "warmup": False,
                    "exit_code": build_probe["exit_code"],
                    "error": build_probe.get("error"),
                }
            )

    recommendation = _mcp_serve_recommendation(
        agent_startup=agent_startup,
        query_probes=query_measurement["commands"],
        failures=failures,
    )
    status = "fail" if failures else "warn" if warnings else "pass"
    payload = {
        "schema_version": BENCHMARK_SUITE_SCHEMA_VERSION,
        "status": status,
        "repo_root": str(repo_root),
        "output_dir": str(output_dir),
        "commit_sha": current.get("commit_sha") or current_commit(repo_root),
        "index_version": current.get("index_version"),
        "target": target,
        "iterations": iterations,
        "warmups": warmups,
        "agent_startup": agent_startup,
        "quality_snapshot": _quality_snapshot(current, semantic_stats),
        "query_probes": {
            "commands": query_measurement["commands"],
            "failures": query_measurement["failures"],
            "warnings": _budget_warnings(
                "Query probe budget exceeded",
                query_measurement["budget_violations"],
            ),
        },
        "build_probe": build_probe,
        "mcp_serve_recommendation": recommendation,
        "failures": failures,
        "warnings": warnings,
        "environment": {
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "cwd": str(repo_root),
        },
    }
    _write_payload_if_requested(payload, output_path)
    if failures or agent_exit_code:
        payload["_exit_code"] = 2
    return payload


def _benchmark_commands(command_set: str, target: str) -> list[BenchmarkCommand]:
    if command_set == "minimal":
        return [BenchmarkCommand("current", ("current",))]
    if command_set == "agent":
        return [
            BenchmarkCommand("current", ("current",)),
            BenchmarkCommand(
                "context",
                ("context", target, "--detail-level", "summary"),
            ),
            BenchmarkCommand(
                "explain",
                ("explain", target, "--detail-level", "summary"),
            ),
        ]
    raise RuntimeError(f"Unknown command set: {command_set}")


def _query_probe_commands(target: str) -> list[BenchmarkCommand]:
    return [
        BenchmarkCommand("current", ("current",)),
        BenchmarkCommand("symbol", ("symbol", target)),
        BenchmarkCommand("context", ("context", target, "--detail-level", "summary")),
        BenchmarkCommand("explain", ("explain", target, "--detail-level", "summary")),
        BenchmarkCommand("semantic-stats", ("semantic-stats",)),
    ]


def _cli_invocation(repo_root: Path, output_dir: Path) -> list[str]:
    script = repo_root / "scripts" / "arcgraph.py"
    if script.exists():
        return [
            sys.executable,
            str(script),
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
        ]
    return [
        "arcgraph",
        "--repo-root",
        str(repo_root),
        "--output-dir",
        str(output_dir),
    ]


def _run_subprocess_command(
    argv: list[str], repo_root: Path, timeout_seconds: float
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_subprocess_env(repo_root),
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        return {
            "exit_code": 124,
            "duration_ms": duration_ms,
            "error": f"Timed out after {timeout_seconds}s: {' '.join(argv)}",
            "stderr": exc.stderr,
        }
    duration_ms = (time.perf_counter() - started) * 1000
    error = completed.stderr.strip() if completed.returncode else None
    return {
        "exit_code": completed.returncode,
        "duration_ms": duration_ms,
        "error": error,
        "stderr": completed.stderr,
    }


def _subprocess_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    repo_path = str(repo_root)
    env["PYTHONPATH"] = (
        repo_path if not existing else f"{repo_path}{os.pathsep}{existing}"
    )
    return env


def _measure_benchmark_commands(
    *,
    commands: list[BenchmarkCommand],
    cli_base: list[str],
    repo_root: Path,
    iterations: int,
    warmups: int,
    timeout_seconds: float,
    budgets: dict[str, dict[str, float]],
    runner: Any | None,
) -> dict[str, Any]:
    runner_fn = runner or _run_subprocess_command
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    budget_violations: list[dict[str, Any]] = []
    for command in commands:
        durations: list[float] = []
        attempts: list[dict[str, Any]] = []
        total_runs = warmups + iterations
        for run_index in range(total_runs):
            is_warmup = run_index < warmups
            argv = [*cli_base, *command.args]
            outcome = runner_fn(argv, repo_root, timeout_seconds)
            attempt = {
                "run": run_index + 1,
                "warmup": is_warmup,
                "exit_code": outcome["exit_code"],
                "duration_ms": round(float(outcome["duration_ms"]), 3),
            }
            if outcome.get("error"):
                attempt["error"] = outcome["error"]
            attempts.append(attempt)
            if outcome["exit_code"] != 0:
                failures.append(
                    {
                        "command": command.name,
                        "run": run_index + 1,
                        "warmup": is_warmup,
                        "exit_code": outcome["exit_code"],
                        "error": outcome.get("error"),
                    }
                )
                continue
            if not is_warmup:
                durations.append(float(outcome["duration_ms"]))
        summary = _duration_summary(durations)
        budget = _budget_summary(command.name, summary, budgets)
        budget_violations.extend(budget["violations"])
        results.append(
            {
                "name": command.name,
                "argv": list(command.args),
                "warmups": warmups,
                "iterations": iterations,
                "duration_ms": summary,
                "budget": budget,
                "attempts": attempts,
            }
        )
    return {
        "commands": results,
        "failures": failures,
        "budget_violations": budget_violations,
    }


def _budget_warnings(prefix: str, violations: list[dict[str, Any]]) -> list[str]:
    return [
        f"{prefix}: {violation['command']} {violation['metric']}="
        f"{violation['observed_ms']}ms > {violation['budget_ms']}ms."
        for violation in violations
    ]


def _duration_summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
    }


def _percentile(ordered: list[float], fraction: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


def _budget_summary(
    command: str,
    summary: dict[str, float | int],
    budgets: dict[str, dict[str, float]],
) -> dict[str, Any]:
    thresholds = budgets.get(command, {})
    violations: list[dict[str, Any]] = []
    observed: dict[str, float | int | None] = {}
    for metric, budget in thresholds.items():
        value = summary.get(metric)
        observed[metric] = value
        if isinstance(value, int | float) and float(value) > budget:
            violations.append(
                {
                    "command": command,
                    "metric": metric,
                    "observed_ms": float(value),
                    "budget_ms": budget,
                }
            )
    return {
        "status": "warn" if violations else "pass",
        "thresholds_ms": thresholds,
        "observed_ms": observed,
        "violations": violations,
    }


def _quality_snapshot(
    current: dict[str, Any], semantic_stats: dict[str, Any]
) -> dict[str, Any]:
    metrics = semantic_stats.get("metrics") or {}
    merge_metrics = (
        current.get("merge_metrics") or semantic_stats.get("merge_metrics") or {}
    )
    return {
        "index_version": current.get("index_version"),
        "commit_sha": current.get("commit_sha"),
        "created_at": current.get("created_at"),
        "freshness": current.get("freshness"),
        "capabilities": current.get("capabilities") or {},
        "counts": {
            "files": current.get("file_count"),
            "nodes": current.get("node_count") or merge_metrics.get("nodes_out"),
            "edges": current.get("edge_count") or merge_metrics.get("edges_out"),
            "diagnostics": merge_metrics.get("diagnostics_count"),
            "unresolved": merge_metrics.get("unresolved_count")
            or metrics.get("unresolved_callsite_total"),
        },
        "semantic_resolution_rates": {
            "overall": metrics.get("resolution_rate"),
            "by_expression_kind": metrics.get("resolution_rate_by_expression_kind")
            or {},
            "resolved_callsite_total": metrics.get("resolved_callsite_total"),
            "unresolved_callsite_total": metrics.get("unresolved_callsite_total"),
        },
        "release_gate_relevant_status": {
            "freshness": (current.get("freshness") or {}).get("status"),
            "precision": (current.get("capabilities") or {}).get("precision"),
            "coverage": (current.get("capabilities") or {}).get("coverage"),
            "runtime_trace": (current.get("capabilities") or {}).get("runtime_trace"),
        },
    }


def _mcp_serve_recommendation(
    *,
    agent_startup: dict[str, Any],
    query_probes: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    failed_commands = sorted({failure["command"] for failure in failures})
    if failed_commands:
        return {
            "status": "needed_candidate",
            "reason": "Subprocess benchmark failures were observed.",
            "failed_commands": failed_commands,
            "violating_commands": [],
        }

    core_names = {"current", "context", "explain"}
    all_commands = [
        *(agent_startup.get("commands") or []),
        *query_probes,
    ]
    violating_commands = sorted(
        {
            command["name"]
            for command in all_commands
            if command.get("name") in core_names
            and (command.get("budget") or {}).get("violations")
        }
    )
    if len(violating_commands) >= 2:
        status = "needed_candidate"
        reason = "Multiple core agent commands exceeded local p95 budgets."
    elif violating_commands:
        status = "investigate"
        reason = "A core agent command exceeded its local p95 budget."
    else:
        status = "not_needed_now"
        reason = "Core agent command p95 timings are within local budgets."
    return {
        "status": status,
        "reason": reason,
        "failed_commands": [],
        "violating_commands": violating_commands,
    }


def _run_isolated_build_probe(
    *,
    repo_root: Path,
    timeout_seconds: float,
    runner: Any | None,
) -> dict[str, Any]:
    runner_fn = runner or _run_subprocess_command
    with tempfile.TemporaryDirectory(prefix="arcgraph-benchmark-build-") as tmp:
        temp_output_dir = Path(tmp) / "arcgraph"
        argv = [*_cli_invocation(repo_root, temp_output_dir), "build"]
        outcome = runner_fn(argv, repo_root, timeout_seconds)
        return {
            "isolated": True,
            "output_dir": str(temp_output_dir),
            "argv": argv,
            "exit_code": outcome["exit_code"],
            "duration_ms": round(float(outcome["duration_ms"]), 3),
            "error": outcome.get("error"),
        }


def _write_payload_if_requested(
    payload: dict[str, Any], output_path: Path | None
) -> None:
    if output_path is None:
        return
    payload["output_path"] = str(output_path.resolve())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
