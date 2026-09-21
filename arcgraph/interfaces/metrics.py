"""JSONL metrics helpers for ArcGraph CLI and automation."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arcgraph.interfaces.agent_capabilities import (
    ALL_MCP_CAPABILITY_NAMES,
    mcp_capability_names,
)
from arcgraph.interfaces.local_state import (
    append_private_bytes,
    read_private_bytes,
)

PERFORMANCE_BUDGETS_MS: dict[str, dict[str, float]] = {
    "current": {"p95": 500.0},
    "status": {"p95": 500.0},
    "symbol": {"p95": 1000.0},
    "callers": {"p95": 1000.0},
    "callees": {"p95": 1000.0},
    "impact": {"p95": 3000.0},
    "get_context": {"p95": 3000.0},
    "context": {"p95": 3000.0},
    "reindex": {"p50": 3000.0, "p95": 10000.0},
    "report": {"p95": 10000.0},
}

MCP_METRICS_WRITE_WARNING = (
    "ArcGraph MCP metrics: unable to write the local metrics log; "
    "use a new canonical private path (POSIX parent 0700, file 0600). "
    "Further write errors are suppressed.\n"
)
CLI_METRICS_WRITE_WARNING = (
    "ArcGraph CLI metrics: unable to write the local metrics log safely; "
    "use a new canonical private path (POSIX parent 0700, file 0600).\n"
)
METRICS_LOG_INVALID_ENCODING_ERROR_CODE = "METRICS_LOG_INVALID_ENCODING"
METRICS_LOG_UNREADABLE_ERROR_CODE = "METRICS_LOG_UNREADABLE"
_FRESHNESS_METRIC_STATUSES = frozenset({"fresh", "stale", "unknown"})
_MISSING_FRESHNESS_STATUS = "not_reported"
_TRIAL_METRIC_STATUSES = frozenset(
    {"success", "error", "failed", "blocked", "available", "unavailable"}
)


class MetricsLogEncodingError(RuntimeError):
    """A local metrics log is not valid UTF-8 JSONL.

    Deliberately not a ``PrivateLocalStateError``: the bytes are corrupt, not
    the path or its permissions, and the two need different remedies.  Read
    paths must catch this explicitly rather than inherit fail-closed handling
    that would tell the operator to pick a new canonical private path.
    """


class MetricsRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record_cli_command(
        self,
        *,
        command: str | None,
        status: str,
        duration_ms: float,
        exit_code: int,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        payload = payload or {}
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "arcgraph_cli_command",
            "command": command,
            "status": status,
            "duration_ms": round(duration_ms, 3),
            "exit_code": exit_code,
            "schema_version": payload.get("schema_version"),
            "index_version": payload.get("index_version"),
            "freshness_status": (
                payload.get("freshness", {}).get("status")
                if isinstance(payload.get("freshness"), dict)
                else None
            ),
            "error": error,
        }
        semantic_summary = _semantic_summary_from_payload(payload)
        if semantic_summary:
            event["semantic_summary"] = semantic_summary
        self.record(event)

    def record(self, event: dict[str, Any]) -> None:
        encoded = (json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        append_private_bytes(self.path, encoded)


class MCPMetricsRecorder:
    """Best-effort, privacy-bounded JSONL metrics for MCP tool handlers."""

    def __init__(
        self,
        path: Path,
        *,
        allowed_tool_names: frozenset[str] | None = None,
    ) -> None:
        self.path = path
        self._allowed_tool_names = (
            allowed_tool_names
            if allowed_tool_names is not None
            else frozenset(mcp_capability_names())
        )
        self._lock = threading.Lock()
        self._write_failed = False

    def record_tool_call(
        self,
        *,
        tool_name: str,
        status: str,
        duration_ms: float,
        payload: Any | None,
    ) -> None:
        try:
            payload_bytes, token_estimate = _serialized_payload_stats(payload)
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tool_name": (
                    tool_name
                    if isinstance(tool_name, str)
                    and tool_name in self._allowed_tool_names
                    else "unknown_tool"
                ),
                "status": status,
                "duration_ms": round(duration_ms, 3),
                "payload_bytes": payload_bytes,
                "estimated_tokens": token_estimate,
                "truncated": _payload_was_truncated(payload),
                "freshness_status": _payload_freshness_status(payload),
            }
            serialized_event = json.dumps(
                event,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except Exception:
            self.disable_after_write_failure()
            return

        with self._lock:
            if self._write_failed:
                return
        try:
            self._append_serialized_event(serialized_event)
        except Exception:
            self.disable_after_write_failure()

    def _append_serialized_event(self, serialized_event: str) -> None:
        append_private_bytes(
            self.path,
            f"{serialized_event}\n".encode("utf-8"),
        )

    def disable_after_write_failure(self) -> None:
        """Latch best-effort metrics off and warn once, from any failure path.

        Public so callers that never reach ``record_tool_call`` -- an offload
        or scheduling failure, for example -- can still make the loss visible
        instead of silently dropping every later event.
        """

        with self._lock:
            if self._write_failed:
                return
            self._write_failed = True
            _write_mcp_metrics_warning()


def summarize_metrics(path: Path, *, limit: int = 1000) -> dict[str, Any]:
    if not path.exists():
        return metrics_unavailable_payload(
            path,
            trial_summary=False,
            warning="Metrics log does not exist.",
        )

    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    text = _read_metrics_text(path)
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"Invalid JSONL at line {line_number}: {exc}")
            continue
        if isinstance(event, dict):
            events.append(event)
    events = events[-max(1, limit) :]

    commands: dict[str, int] = {}
    statuses: dict[str, int] = {}
    durations: list[float] = []
    durations_by_command: dict[str, list[float]] = {}
    durations_by_category: dict[str, list[float]] = {}
    latest_semantic_summary: dict[str, Any] = {}
    for event in events:
        command = str(event.get("command") or event.get("tool_name") or "unknown")
        status = str(event.get("status") or "unknown")
        commands[command] = commands.get(command, 0) + 1
        statuses[status] = statuses.get(status, 0) + 1
        duration = event.get("duration_ms")
        if isinstance(duration, int | float):
            value = float(duration)
            durations.append(value)
            durations_by_command.setdefault(command, []).append(value)
            durations_by_category.setdefault(_command_category(command), []).append(
                value
            )
        semantic_summary = event.get("semantic_summary")
        if isinstance(semantic_summary, dict):
            latest_semantic_summary = semantic_summary

    duration_summary = _duration_summary(durations)
    command_summaries = {
        command: _duration_summary(values)
        for command, values in sorted(durations_by_command.items())
    }
    category_summaries = {
        category: _duration_summary(values)
        for category, values in sorted(durations_by_category.items())
    }

    return {
        "status": "available" if events else "empty",
        "path": str(path),
        "event_count": len(events),
        "first_timestamp": events[0].get("timestamp") if events else None,
        "last_timestamp": events[-1].get("timestamp") if events else None,
        "commands": dict(sorted(commands.items())),
        "statuses": dict(sorted(statuses.items())),
        "duration_ms": duration_summary,
        "duration_by_command_ms": command_summaries,
        "duration_by_category_ms": category_summaries,
        "performance_budget": _performance_budget_summary(command_summaries),
        "semantic_summary": latest_semantic_summary,
        "warnings": warnings,
    }


def summarize_trial_metrics(path: Path, *, limit: int = 1000) -> dict[str, Any]:
    """Return a shareable aggregate without paths, timestamps, or raw events."""
    if not path.exists():
        return metrics_unavailable_payload(
            path,
            trial_summary=True,
            warning="Metrics log does not exist.",
        )

    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    text = _read_metrics_text(path)
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"Invalid JSONL at line {line_number}.")
            continue
        if isinstance(event, dict):
            events.append(event)
    events = events[-max(1, limit) :]

    events_by_tool: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        tool_name = _trial_metric_tool_name(event)
        events_by_tool.setdefault(tool_name, []).append(event)

    return {
        "schema_version": "1.0",
        "status": "available" if events else "empty",
        "event_count": len(events),
        "overall": _trial_metric_bucket(events),
        "by_tool": {
            tool_name: _trial_metric_bucket(tool_events)
            for tool_name, tool_events in sorted(events_by_tool.items())
        },
        "privacy": _trial_summary_privacy(),
        "warnings": warnings,
    }


def metrics_unavailable_payload(
    path: Path,
    *,
    trial_summary: bool,
    warning: str,
) -> dict[str, Any]:
    """Return the stable unavailable shape for one metrics summary mode."""

    if trial_summary:
        return {
            "schema_version": "1.0",
            "status": "unavailable",
            "event_count": 0,
            "overall": _trial_metric_bucket([]),
            "by_tool": {},
            "privacy": _trial_summary_privacy(),
            "warnings": [warning],
        }
    return {
        "status": "unavailable",
        "path": str(path),
        "event_count": 0,
        "commands": {},
        "statuses": {},
        "duration_ms": {},
        "warnings": [warning],
    }


def _read_metrics_text(path: Path) -> str:
    try:
        return read_private_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MetricsLogEncodingError("Metrics log is not valid UTF-8.") from exc


def _trial_metric_bucket(events: list[dict[str, Any]]) -> dict[str, Any]:
    durations = _numeric_metric_values(events, "duration_ms")
    payload_bytes = _numeric_metric_values(events, "payload_bytes")
    estimated_tokens = _numeric_metric_values(events, "estimated_tokens")
    statuses: dict[str, int] = {}
    freshness_statuses: dict[str, int] = {}
    observed_truncation = 0
    truncated_count = 0
    for event in events:
        raw_status = event.get("status")
        status = (
            raw_status
            if isinstance(raw_status, str) and raw_status in _TRIAL_METRIC_STATUSES
            else "unknown"
        )
        statuses[status] = statuses.get(status, 0) + 1
        freshness_status = _safe_freshness_metric_status(event.get("freshness_status"))
        freshness_statuses[freshness_status] = (
            freshness_statuses.get(freshness_status, 0) + 1
        )
        truncated = event.get("truncated")
        if isinstance(truncated, bool):
            observed_truncation += 1
            if truncated:
                truncated_count += 1

    return {
        "count": len(events),
        "statuses": dict(sorted(statuses.items())),
        "duration_ms": _duration_summary(durations),
        "payload_bytes": _duration_summary(payload_bytes),
        "estimated_tokens": _duration_summary(estimated_tokens),
        "truncation": {
            "observed_count": observed_truncation,
            "truncated_count": truncated_count,
            "truncated_rate": (
                round(truncated_count / observed_truncation, 4)
                if observed_truncation
                else 0.0
            ),
        },
        "freshness_statuses": dict(sorted(freshness_statuses.items())),
    }


def _numeric_metric_values(events: list[dict[str, Any]], field: str) -> list[float]:
    return [
        float(value)
        for event in events
        if isinstance((value := event.get(field)), int | float)
        and not isinstance(value, bool)
    ]


def _trial_metric_tool_name(event: dict[str, Any]) -> str:
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str):
        return "non_mcp_event"

    return tool_name if tool_name in ALL_MCP_CAPABILITY_NAMES else "unknown_tool"


def _trial_summary_privacy() -> dict[str, Any]:
    return {
        "shareable": True,
        "omitted": [
            "input_path",
            "timestamps",
            "tool_arguments",
            "repository_ids",
            "source_paths",
            "source_text",
            "returned_payloads",
            "raw_errors",
            "client_identity",
        ],
    }


def _serialized_payload_stats(payload: Any | None) -> tuple[int, int]:
    if payload is None:
        return 0, 0
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return len(serialized.encode("utf-8")), max(1, len(serialized) // 4)


def _payload_was_truncated(payload: Any) -> bool:
    if isinstance(payload, dict):
        truncation = payload.get("truncation")
        if isinstance(truncation, dict) and truncation.get("truncated") is True:
            return True
        return any(_payload_was_truncated(item) for item in payload.values())
    if isinstance(payload, list):
        return any(_payload_was_truncated(item) for item in payload)
    return False


def _payload_freshness_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        return _MISSING_FRESHNESS_STATUS
    freshness = payload.get("freshness")
    if not isinstance(freshness, dict):
        return _MISSING_FRESHNESS_STATUS
    return _safe_freshness_metric_status(freshness.get("status"))


def _safe_freshness_metric_status(value: Any) -> str:
    return (
        value
        if isinstance(value, str) and value in _FRESHNESS_METRIC_STATUSES
        else _MISSING_FRESHNESS_STATUS
    )


def _write_mcp_metrics_warning() -> None:
    try:
        sys.stderr.write(MCP_METRICS_WRITE_WARNING)
    except Exception:
        pass


def _semantic_summary_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("semantic_summary")
    if isinstance(summary, dict):
        return summary
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return {}
    binding_summary = payload.get("binding_summary") or metrics.get(
        "binding_summary", {}
    )
    type_summary = payload.get("type_summary") or metrics.get("type_summary", {})
    return {
        "callsite_total": metrics.get("callsite_total", 0),
        "resolved_callsite_total": metrics.get("resolved_callsite_total", 0),
        "unresolved_callsite_total": metrics.get("unresolved_callsite_total", 0),
        "resolution_rate": metrics.get("resolution_rate", 1.0),
        "binding_summary": binding_summary,
        "type_summary": type_summary,
    }


def _duration_summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "avg": round(sum(ordered) / len(ordered), 3),
        "p50": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.50))], 3),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
    }


def _performance_budget_summary(
    duration_by_command_ms: dict[str, dict[str, float | int]],
) -> dict[str, Any]:
    observed: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for command, thresholds in sorted(PERFORMANCE_BUDGETS_MS.items()):
        summary = duration_by_command_ms.get(command)
        if not summary:
            continue
        command_observation = {
            "command": command,
            "thresholds_ms": thresholds,
            "observed_ms": {
                metric: summary.get(metric)
                for metric in thresholds
                if metric in summary
            },
        }
        observed.append(command_observation)
        for metric, budget in thresholds.items():
            value = summary.get(metric)
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
        "budget_count": len(PERFORMANCE_BUDGETS_MS),
        "observed_budget_count": len(observed),
        "violation_count": len(violations),
        "observed": observed,
        "violations": violations,
    }


def _command_category(command: str) -> str:
    if command in {"build", "reindex"}:
        return "build"
    if command in {"ci"}:
        return "ci"
    if command in {"ops", "metrics"}:
        return "ops"
    if command in {"report"}:
        return "report"
    if command in {
        "imports",
        "symbol",
        "callers",
        "callees",
        "impact",
        "tests",
        "similar",
        "route",
        "worker",
        "architecture",
        "current",
        "status",
        "stats",
        "semantic-stats",
        "unresolved",
        "bindings",
        "types",
        "callsites",
    }:
        return "query"
    return "other"
