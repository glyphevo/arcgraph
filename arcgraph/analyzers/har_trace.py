"""Import offline HAR browser network dumps as runtime-only ArcGraph evidence."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from arcgraph.analyzers.runtime_trace import RuntimeTraceAnalysis
from arcgraph.core.schemas import BuildWarning, Edge, Evidence, FactResolution, Node


@dataclass
class HarNetworkImporter:
    repo_root: Path
    har_path: str | Path
    commit_sha: str | None = None
    index_version: str | None = None
    max_entries: int = 50_000
    max_file_bytes: int = 10 * 1024 * 1024
    max_seconds: float = 120.0
    _warnings: list[BuildWarning] = field(default_factory=list, init=False)

    def analyze(self, nodes: list[Node]) -> RuntimeTraceAnalysis:
        started = time.monotonic()
        path = self._absolute_path(Path(self.har_path))
        entries: list[dict[str, Any]] = []
        parse_errors = 0
        missing = 0
        too_large = 0
        if not path.exists():
            missing = 1
            self._warning(
                "runtime_trace_har_missing",
                f"HAR file does not exist: {path}",
            )
        elif path.stat().st_size > self.max_file_bytes:
            too_large = 1
            self._warning(
                "runtime_trace_har_too_large",
                f"HAR file exceeds max_file_bytes={self.max_file_bytes}: {path}",
            )
        else:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                entries = self._entries(payload)
            except (OSError, json.JSONDecodeError) as exc:
                parse_errors = 1
                self._warning("runtime_trace_har_parse_error", str(exc))

        if not entries and not (missing or too_large or parse_errors):
            self._warning("runtime_trace_har_empty", "HAR file contains no entries.")

        route_candidates = self._route_candidates(nodes)
        max_entries = max(1, self.max_entries)
        limited = max(0, len(entries) - max_entries)
        if limited:
            self._warning(
                "runtime_trace_har_entry_limit_exceeded",
                f"HAR file has {len(entries)} entrie(s); imported first {max_entries}.",
            )

        session_id = f"runtime_session:har:{self._artifact_hash(path)}"
        session_node: Node | None = None
        edges: list[Edge] = []
        processed = 0
        converted = 0
        unmatched = 0
        ambiguous = 0
        non_http = 0
        malformed = 0
        wall_time_limited = False
        for entry_index, entry in enumerate(entries[:max_entries]):
            if self._wall_time_exceeded(started):
                wall_time_limited = True
                self._warning(
                    "runtime_trace_har_wall_time_limit_exceeded",
                    f"HAR import exceeded max_seconds={self.max_seconds}.",
                )
                break
            processed += 1
            request = entry.get("request") if isinstance(entry, dict) else None
            if not isinstance(request, dict):
                malformed += 1
                self._warning(
                    "runtime_trace_har_entry_malformed",
                    f"HAR entry {entry_index} has no request object.",
                )
                continue
            method = self._request_method(request)
            path_value = self._request_path(request)
            if method is None or path_value is None:
                non_http += 1
                self._warning(
                    "runtime_trace_har_non_http_url",
                    f"HAR entry {entry_index} is not an HTTP(S) request.",
                )
                continue
            key = (method, self._canonical_path(path_value))
            candidates = route_candidates.get(key, [])
            if not candidates:
                unmatched += 1
                self._warning(
                    "runtime_trace_har_route_unmatched",
                    f"HAR entry {entry_index} did not match a known route: {method} {key[1]}",
                )
                continue
            if len(candidates) > 1:
                ambiguous += 1
                self._warning(
                    "runtime_trace_har_route_ambiguous",
                    (
                        f"HAR entry {entry_index} matched multiple routes for "
                        f"{method} {key[1]}: {', '.join(sorted(node.id for node in candidates))}"
                    ),
                )
                continue
            if session_node is None:
                session_node = Node(
                    id=session_id,
                    kind="runtime_session",
                    name=f"HAR session {self._artifact_hash(path)}",
                    qualname=session_id,
                    path=self._display_path(path),
                    properties={"source": "har_network"},
                )
            target = candidates[0]
            status = self._response_status(entry)
            edges.append(
                Edge(
                    source=session_node.id,
                    target=target.id,
                    kind="invokes",
                    confidence="runtime-only",
                    resolution=FactResolution(
                        status="runtime-only",
                        strategy="har_network_import",
                        detail=f"{method} {key[1]} status={status}",
                    ),
                    evidence=[
                        Evidence(
                            kind="runtime_trace_har_request",
                            path=self._display_path(path),
                            detail=(
                                f"entry={entry_index} method={method} "
                                f"path={key[1]} status={status}"
                            ),
                        )
                    ],
                    properties={
                        "trace_run_id": session_id,
                        "event_kind": "har_request",
                        "trace_source": "har_network",
                        "method": method,
                        "path": key[1],
                        "status": status,
                        "entry_index": entry_index,
                    },
                )
            )
            converted += 1

        status = (
            "partial"
            if (
                self._warnings
                or not entries
                or limited
                or unmatched
                or ambiguous
                or non_http
                or malformed
                or missing
                or too_large
                or parse_errors
                or wall_time_limited
            )
            else "available"
        )
        metrics = {
            "status": status,
            "trace_path": self._display_path(path),
            "trace_run_id": session_id,
            "commit_sha": self.commit_sha,
            "index_version": self.index_version,
            "frontend_version": "ArcGraph-har-network-import-v1",
            "source": "har_network",
            "max_events": max_entries,
            "max_file_bytes": self.max_file_bytes,
            "max_seconds": self.max_seconds,
            "stale_trace": False,
            "events_total": len(entries),
            "events_processed": processed,
            "edges_imported": len(edges),
            "unresolved_events": unmatched + ambiguous + non_http + malformed,
            "redacted_fields": 0,
            "truncated_events": limited,
            "wall_time_limited": wall_time_limited,
            "diagnostics": len(self._warnings),
            "har_entries_total": len(entries),
            "har_entries_processed": processed,
            "har_entries_converted": converted,
            "har_entries_unmatched": unmatched,
            "har_entries_ambiguous": ambiguous,
            "har_entries_non_http": non_http,
            "har_entries_malformed": malformed,
            "har_entries_limited": limited,
            "har_parse_errors": parse_errors,
            "har_missing": missing,
            "har_too_large": too_large,
            "duration_seconds": round(time.monotonic() - started, 6),
        }
        return RuntimeTraceAnalysis(
            nodes=[session_node] if session_node is not None else [],
            edges=sorted(
                edges,
                key=lambda edge: (edge.source, edge.target, edge.kind),
            ),
            warnings=self._warnings,
            metrics=metrics,
            status=status,
        )

    def _route_candidates(self, nodes: list[Node]) -> dict[tuple[str, str], list[Node]]:
        routes: dict[tuple[str, str], list[Node]] = {}
        for node in nodes:
            parsed = self._route_key(node)
            if parsed is None:
                continue
            routes.setdefault(parsed, []).append(node)
        return routes

    def _route_key(self, node: Node) -> tuple[str, str] | None:
        if node.kind != "route" or not node.id.startswith("route:"):
            return None
        parts = node.id.split(":", 2)
        if len(parts) != 3:
            return None
        method = parts[1].upper()
        path = self._canonical_path(parts[2])
        return (method, path)

    @staticmethod
    def _entries(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        log = payload.get("log")
        if not isinstance(log, dict):
            return []
        entries = log.get("entries")
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _request_method(request: dict[str, Any]) -> str | None:
        method = request.get("method")
        return method.upper() if isinstance(method, str) and method else None

    def _request_path(self, request: dict[str, Any]) -> str | None:
        url = request.get("url")
        if not isinstance(url, str) or not url:
            return None
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        return unquote(parsed.path or "/")

    @staticmethod
    def _canonical_path(path: str) -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        if len(normalized) > 1:
            normalized = normalized.rstrip("/")
        return normalized or "/"

    @staticmethod
    def _response_status(entry: dict[str, Any]) -> int | None:
        response = entry.get("response")
        if not isinstance(response, dict):
            return None
        status = response.get("status")
        if isinstance(status, bool):
            return None
        try:
            return int(status)
        except (TypeError, ValueError):
            return None

    def _warning(self, kind: str, message: str) -> None:
        self._warnings.append(
            BuildWarning(
                kind=kind,
                path=self._display_path(self._absolute_path(Path(self.har_path))),
                message=message,
            )
        )

    def _wall_time_exceeded(self, started: float) -> bool:
        return (
            self.max_seconds >= 0 and (time.monotonic() - started) >= self.max_seconds
        )

    def _artifact_hash(self, path: Path) -> str:
        try:
            payload = path.read_bytes()
        except OSError:
            payload = str(path).encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()[:12]

    def _absolute_path(self, path: Path) -> Path:
        return path if path.is_absolute() else self.repo_root / path

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)
