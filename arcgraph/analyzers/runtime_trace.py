"""Optional runtime trace importer for runtime-only ArcGraph evidence."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcgraph.core.schemas import BuildWarning, Edge, Evidence, FactResolution, Node
from arcgraph.core.utils import int_or_none


@dataclass(frozen=True)
class RuntimeTraceConfig:
    trace_path: Path | None = None
    commit_sha: str | None = None
    index_version: str | None = None
    max_events: int = 50_000
    max_file_bytes: int = 10 * 1024 * 1024
    max_seconds: float = 120.0


@dataclass
class RuntimeTraceAnalysis:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    warnings: list[BuildWarning] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    status: str = "unavailable"


class RuntimeTraceImporter:
    """Import a ArcGraph runtime trace JSON file as runtime-only facts."""

    SENSITIVE_EVENT_FIELDS = {
        "args",
        "kwargs",
        "parameters",
        "return_value",
        "headers",
        "env",
        "environment",
        "payload",
        "body",
        "request",
        "response",
    }
    EDGE_KIND_BY_EVENT = {
        "call": "calls",
        "calls": "calls",
        "dynamic_call": "calls",
        "import": "imports",
        "imports": "imports",
        "dynamic_import": "imports",
        "register": "registers",
        "registers": "registers",
        "invoke": "invokes",
        "invokes": "invokes",
        "enqueue": "enqueues",
        "enqueues": "enqueues",
        "consume": "consumes",
        "consumes": "consumes",
        "read": "reads",
        "reads": "reads",
        "write": "writes",
        "writes": "writes",
    }

    def __init__(
        self,
        repo_root: Path,
        trace_path: str | Path | None = None,
        *,
        commit_sha: str | None = None,
        index_version: str | None = None,
        max_events: int = 50_000,
        max_file_bytes: int = 10 * 1024 * 1024,
        max_seconds: float = 120.0,
        config: RuntimeTraceConfig | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config or RuntimeTraceConfig(
            trace_path=Path(trace_path) if trace_path else None,
            commit_sha=commit_sha,
            index_version=index_version,
            max_events=max_events,
            max_file_bytes=max_file_bytes,
            max_seconds=max_seconds,
        )

    def analyze(self, nodes: list[Node]) -> RuntimeTraceAnalysis:
        started = time.monotonic()
        path = self.config.trace_path
        if path is None:
            return RuntimeTraceAnalysis(metrics=self._base_metrics("unavailable"))
        absolute = self._absolute_path(path)
        if not absolute.exists():
            return RuntimeTraceAnalysis(
                warnings=[
                    BuildWarning(
                        kind="runtime_trace_missing",
                        path=self._display_path(absolute),
                        message=f"Runtime trace file does not exist: {absolute}",
                    )
                ],
                metrics=self._base_metrics("partial", trace_path=absolute),
                status="partial",
            )
        if absolute.stat().st_size > self.config.max_file_bytes:
            return RuntimeTraceAnalysis(
                warnings=[
                    BuildWarning(
                        kind="runtime_trace_too_large",
                        path=self._display_path(absolute),
                        message=(
                            "Runtime trace file exceeds max_file_bytes="
                            f"{self.config.max_file_bytes}: {absolute}"
                        ),
                    )
                ],
                metrics=self._base_metrics("partial", trace_path=absolute),
                status="partial",
            )

        try:
            payload = json.loads(absolute.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return RuntimeTraceAnalysis(
                warnings=[
                    BuildWarning(
                        kind="runtime_trace_parse_error",
                        path=self._display_path(absolute),
                        message=str(exc),
                    )
                ],
                metrics=self._base_metrics("partial", trace_path=absolute),
                status="partial",
            )

        return self.analyze_payload(
            payload, nodes, trace_path=absolute, started=started
        )

    def analyze_payload(
        self,
        payload: Any,
        nodes: list[Node],
        *,
        trace_path: str | Path | None = None,
        started: float | None = None,
    ) -> RuntimeTraceAnalysis:
        """Import an already parsed ArcGraph runtime trace payload."""

        started = time.monotonic() if started is None else started
        absolute = (
            self._absolute_path(Path(trace_path))
            if trace_path is not None
            else self.config.trace_path or Path("runtime-trace.json")
        )
        trace_run = self._trace_run(payload)
        stale_warning = self._stale_warning(trace_run, absolute)
        if stale_warning is not None:
            return RuntimeTraceAnalysis(
                warnings=[stale_warning],
                metrics={
                    **self._base_metrics("stale", trace_path=absolute, payload=payload),
                    "stale_trace": True,
                    "events_total": len(self._events(payload)),
                    "events_processed": 0,
                    "edges_imported": 0,
                    "unresolved_events": 0,
                    "redacted_fields": 0,
                    "truncated_events": 0,
                },
                status="partial",
            )

        node_by_id = {node.id: node for node in nodes}
        node_by_qualname = {node.qualname: node for node in nodes if node.qualname}
        events = self._events(payload)
        warnings = self._payload_warnings(payload, absolute)
        max_events = max(1, self.config.max_events)
        truncated_events = max(0, len(events) - max_events)
        if truncated_events:
            warnings.append(
                BuildWarning(
                    kind="runtime_trace_event_limit_exceeded",
                    path=self._display_path(absolute),
                    message=(
                        f"Runtime trace has {len(events)} event(s); imported first "
                        f"{max_events}."
                    ),
                )
            )

        edges: list[Edge] = []
        unresolved_events = 0
        redacted_fields = 0
        events_processed = 0
        wall_time_limited = False
        for event in events[:max_events]:
            if self._wall_time_exceeded(started):
                wall_time_limited = True
                warnings.append(
                    BuildWarning(
                        kind="runtime_trace_wall_time_limit_exceeded",
                        path=self._display_path(absolute),
                        message=(
                            "Runtime trace import exceeded max_seconds="
                            f"{self.config.max_seconds}."
                        ),
                    )
                )
                break
            events_processed += 1
            redacted_fields += self._redacted_field_count(event)
            edge, event_warning = self._edge_from_event(
                event,
                trace_run=trace_run,
                trace_path=absolute,
                node_by_id=node_by_id,
                node_by_qualname=node_by_qualname,
            )
            if event_warning is not None:
                warnings.append(event_warning)
            if edge is None:
                unresolved_events += 1
                continue
            edges.append(edge)
        wall_time_truncated_events = (
            max(0, min(len(events), max_events) - events_processed)
            if wall_time_limited
            else 0
        )

        if redacted_fields:
            warnings.append(
                BuildWarning(
                    kind="runtime_trace_redacted_fields",
                    path=self._display_path(absolute),
                    message=(
                        f"Ignored {redacted_fields} sensitive runtime trace field(s)."
                    ),
                )
            )

        status = (
            "partial"
            if warnings or unresolved_events or truncated_events or wall_time_limited
            else "available"
        )
        metrics = {
            **self._base_metrics(status, trace_path=absolute, payload=payload),
            "stale_trace": False,
            "events_total": len(events),
            "events_processed": events_processed,
            "edges_imported": len(edges),
            "unresolved_events": unresolved_events,
            "redacted_fields": redacted_fields,
            "truncated_events": truncated_events,
            "wall_time_limited": wall_time_limited,
            "wall_time_truncated_events": wall_time_truncated_events,
            "duration_seconds": round(time.monotonic() - started, 6),
            "diagnostics": len(warnings),
        }
        return RuntimeTraceAnalysis(
            edges=sorted(
                edges,
                key=lambda edge: (
                    edge.source,
                    edge.target,
                    edge.kind,
                    edge.semantic_role or "",
                ),
            ),
            warnings=warnings,
            metrics=metrics,
            status=status,
        )

    def _edge_from_event(
        self,
        event: dict[str, Any],
        *,
        trace_run: dict[str, Any],
        trace_path: Path,
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
    ) -> tuple[Edge | None, BuildWarning | None]:
        source = self._resolve_node(event.get("source"), node_by_id, node_by_qualname)
        target = self._resolve_node(event.get("target"), node_by_id, node_by_qualname)
        if source is None or target is None or source.id == target.id:
            return None, None

        event_kind = self._event_kind(event)
        edge_kind = self.EDGE_KIND_BY_EVENT.get(event_kind, event_kind)
        safe_path, warning = self._safe_event_path(event, trace_path)
        return (
            Edge(
                source=source.id,
                target=target.id,
                kind=edge_kind,
                confidence="runtime-only",
                semantic_role=self._string_or_none(event.get("semantic_role")),
                resolution=FactResolution(
                    status="runtime-only",
                    strategy="runtime_trace",
                    detail=self._string_or_none(event.get("detail")),
                ),
                evidence=[
                    Evidence(
                        kind=f"runtime_trace_{event_kind}",
                        path=safe_path,
                        start_line=int_or_none(
                            event.get("start_line") or event.get("line")
                        ),
                        end_line=int_or_none(event.get("end_line")),
                        column=int_or_none(event.get("column")),
                        detail=self._string_or_none(event.get("test_id"))
                        or self._string_or_none(trace_run.get("test_id"))
                        or self._string_or_none(trace_run.get("trace_run_id")),
                    )
                ],
                properties={
                    "trace_run_id": self._trace_run_id(trace_run),
                    "test_id": self._string_or_none(event.get("test_id"))
                    or self._string_or_none(trace_run.get("test_id")),
                    "event_kind": event_kind,
                    "trace_source": self._string_or_none(trace_run.get("source")),
                },
            ),
            warning,
        )

    def _safe_event_path(
        self, event: dict[str, Any], trace_path: Path
    ) -> tuple[str, BuildWarning | None]:
        value = event.get("path") or event.get("filename")
        if not isinstance(value, str) or not value:
            return self._display_path(trace_path), None
        candidate = Path(value)
        try:
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (self.repo_root / candidate).resolve()
            )
            relative = resolved.relative_to(self.repo_root)
        except ValueError:
            return (
                self._display_path(trace_path),
                BuildWarning(
                    kind="runtime_trace_path_outside_repo",
                    path=self._display_path(trace_path),
                    message=f"Runtime trace event path is outside repo scope: {value}",
                ),
            )
        return relative.as_posix(), None

    def _stale_warning(
        self, trace_run: dict[str, Any], trace_path: Path
    ) -> BuildWarning | None:
        trace_commit = self._string_or_none(trace_run.get("commit_sha"))
        trace_index = self._string_or_none(trace_run.get("index_version"))
        stale_reasons: list[str] = []
        if (
            self.config.commit_sha
            and trace_commit
            and trace_commit != self.config.commit_sha
        ):
            stale_reasons.append("commit_sha")
        if (
            self.config.index_version
            and trace_index
            and trace_index != self.config.index_version
        ):
            stale_reasons.append("index_version")
        if not stale_reasons:
            return None
        return BuildWarning(
            kind="runtime_trace_stale",
            path=self._display_path(trace_path),
            message=(
                "Runtime trace is stale for current index context: "
                + ", ".join(stale_reasons)
            ),
        )

    def _payload_warnings(self, payload: Any, trace_path: Path) -> list[BuildWarning]:
        warnings: list[BuildWarning] = []
        if not isinstance(payload, dict):
            return warnings
        for item in payload.get("diagnostics", []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "runtime_trace_diagnostic")
            message = str(item.get("message") or "Runtime trace diagnostic")
            path = item.get("path") or self._display_path(trace_path)
            warnings.append(
                BuildWarning(
                    kind=(
                        kind
                        if kind.startswith("runtime_trace_")
                        else f"runtime_trace_{kind}"
                    ),
                    path=(
                        path
                        if isinstance(path, str)
                        else self._display_path(trace_path)
                    ),
                    message=message,
                )
            )
        return warnings

    @staticmethod
    def _trace_run(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("trace_run"), dict):
            return payload["trace_run"]
        if isinstance(payload, dict):
            return {
                key: payload.get(key)
                for key in (
                    "trace_run_id",
                    "commit_sha",
                    "index_version",
                    "test_id",
                    "started_at",
                    "frontend_version",
                    "source",
                )
                if payload.get(key) is not None
            }
        return {}

    @staticmethod
    def _events(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
            return [item for item in payload["events"] if isinstance(item, dict)]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    @staticmethod
    def _event_kind(event: dict[str, Any]) -> str:
        return str(event.get("kind") or event.get("event_kind") or "call").lower()

    @staticmethod
    def _trace_run_id(trace_run: dict[str, Any]) -> str | None:
        return RuntimeTraceImporter._string_or_none(
            trace_run.get("trace_run_id") or trace_run.get("id")
        )

    @staticmethod
    def _resolve_node(
        value: Any,
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
    ) -> Node | None:
        if not isinstance(value, str) or not value:
            return None
        if value in node_by_id:
            return node_by_id[value]
        if value in node_by_qualname:
            return node_by_qualname[value]
        stripped = value.rstrip("#().")
        return node_by_id.get(stripped) or node_by_qualname.get(stripped)

    def _base_metrics(
        self,
        status: str,
        *,
        trace_path: Path | None = None,
        payload: Any | None = None,
    ) -> dict[str, Any]:
        trace_run = self._trace_run(payload)
        return {
            "status": status,
            "trace_path": self._display_path(trace_path) if trace_path else None,
            "trace_run_id": self._trace_run_id(trace_run),
            "commit_sha": self._string_or_none(trace_run.get("commit_sha")),
            "index_version": self._string_or_none(trace_run.get("index_version")),
            "frontend_version": self._string_or_none(trace_run.get("frontend_version")),
            "source": self._string_or_none(trace_run.get("source")),
            "max_events": max(1, self.config.max_events),
            "max_file_bytes": self.config.max_file_bytes,
            "max_seconds": self.config.max_seconds,
        }

    def _wall_time_exceeded(self, started: float) -> bool:
        return (
            self.config.max_seconds >= 0
            and (time.monotonic() - started) >= self.config.max_seconds
        )

    def _redacted_field_count(self, event: dict[str, Any]) -> int:
        count = sum(1 for key in event if key in self.SENSITIVE_EVENT_FIELDS)
        properties = event.get("properties")
        if isinstance(properties, dict):
            count += sum(1 for key in properties if key in self.SENSITIVE_EVENT_FIELDS)
        return count

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        return str(value) if value else None

    def _absolute_path(self, path: Path) -> Path:
        return path if path.is_absolute() else self.repo_root / path

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)
