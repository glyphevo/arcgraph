"""Test-only stdio server that makes MCP runtime scheduling observable."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import threading
import time
from typing import Any

from mcp.server.mcpserver import Context

from arcgraph.interfaces import mcp_server
from arcgraph.interfaces.mcp_tools import (
    ArcGraphMCPConfig,
    ArcGraphMCPToolGroup,
    register_arcgraph_mcp_tools,
)

CONCURRENT_SAME = "__arcgraph_probe_concurrent_same__"
CONCURRENT_MIXED = "__arcgraph_probe_concurrent_mixed__"
CANCEL_STARTED = "__arcgraph_probe_cancel_started__"
TIMEOUT_STARTED = "__arcgraph_probe_timeout_started__"
_CONCURRENCY_PROBES = frozenset({CONCURRENT_SAME, CONCURRENT_MIXED})
_CANCELLATION_PROBES = frozenset({CANCEL_STARTED, TIMEOUT_STARTED})
_PROBE_WAIT_SECONDS = 5.0


def probe_release_path(state_path: Path, marker: str) -> Path:
    label = marker.strip("_")
    return state_path.with_name(f"{state_path.stem}-{label}.release")


class _ProbeCoordinator:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._lock = threading.Lock()
        self._rounds: dict[str, dict[str, Any]] = {}
        self._barriers: dict[str, threading.Barrier] = {}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_state()

    def wait_for_peer(self, marker: str) -> None:
        barrier = self._enter(marker, with_barrier=True)
        if barrier is None:  # pragma: no cover - guarded by with_barrier
            raise RuntimeError("concurrency probe barrier is unavailable")
        try:
            barrier.wait(timeout=_PROBE_WAIT_SECONDS)
        except threading.BrokenBarrierError as exc:
            self._mark(marker, "barrier_failed")
            self.complete(marker)
            raise RuntimeError("MCP handlers did not overlap at the server") from exc

    def wait_for_cancellation(self, marker: str, context: Context | None) -> None:
        self._enter(marker, with_barrier=False)
        if context is None:
            self._mark(marker, "context_missing")
            self.complete(marker)
            raise RuntimeError("MCP cancellation probe did not receive request context")

        # MCPServer's high-level Context does not currently expose the
        # dispatcher cancellation event.  The request-scoped outbound channel
        # is the SDK-owned object that receives notifications/cancelled, so the
        # test-only probe intentionally observes that boundary directly.
        cancel_requested = (
            context.request_context.session._request_outbound.cancel_requested
        )
        deadline = time.monotonic() + _PROBE_WAIT_SECONDS
        while not cancel_requested.is_set():
            if time.monotonic() >= deadline:
                self._mark(marker, "cancel_timeout")
                self.complete(marker)
                raise RuntimeError("MCP server did not observe request cancellation")
            time.sleep(0.01)
        self._increment(marker, "cancel_seen")

        release_path = probe_release_path(self.state_path, marker)
        deadline = time.monotonic() + _PROBE_WAIT_SECONDS
        while not release_path.exists():
            if time.monotonic() >= deadline:
                self._mark(marker, "release_timeout")
                self.complete(marker)
                raise RuntimeError("MCP cancellation probe was not released")
            time.sleep(0.01)

    def complete(self, marker: str) -> None:
        with self._lock:
            state = self._round(marker)
            state["active"] -= 1
            state["completed"] += 1
            self._write_state_locked()

    def _enter(
        self,
        marker: str,
        *,
        with_barrier: bool,
    ) -> threading.Barrier | None:
        with self._lock:
            state = self._round(marker)
            state["started"] += 1
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            thread_id = threading.get_ident()
            if thread_id not in state["thread_ids"]:
                state["thread_ids"].append(thread_id)
            barrier = (
                self._barriers.setdefault(marker, threading.Barrier(2))
                if with_barrier
                else None
            )
            self._write_state_locked()
            return barrier

    def _increment(self, marker: str, field: str) -> None:
        with self._lock:
            state = self._round(marker)
            state[field] = int(state.get(field, 0)) + 1
            self._write_state_locked()

    def _mark(self, marker: str, field: str) -> None:
        with self._lock:
            self._round(marker)[field] = True
            self._write_state_locked()

    def _round(self, marker: str) -> dict[str, Any]:
        return self._rounds.setdefault(
            marker,
            {
                "active": 0,
                "completed": 0,
                "max_active": 0,
                "started": 0,
                "thread_ids": [],
            },
        )

    def _write_state(self) -> None:
        with self._lock:
            self._write_state_locked()

    def _write_state_locked(self) -> None:
        temporary = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        temporary.write_text(
            json.dumps({"rounds": self._rounds}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)


class _ProbeToolGroup(ArcGraphMCPToolGroup):
    def __init__(
        self,
        config: ArcGraphMCPConfig,
        coordinator: _ProbeCoordinator,
    ) -> None:
        super().__init__(config)
        self._coordinator = coordinator

    def arcgraph_get_context(
        self,
        repo_id: str = "default",
        task: str | None = None,
        targets: list[str] | None = None,
        max_results: int = 30,
        detail_level: str = "summary",
        profile: str = "review_default",
        include_source: bool = False,
        mcp_context: Context | None = None,
    ) -> dict[str, Any]:
        self._before_probe(task, mcp_context)
        try:
            return super().arcgraph_get_context(
                repo_id=repo_id,
                task=task,
                targets=targets,
                max_results=max_results,
                detail_level=detail_level,
                profile=profile,
                include_source=include_source,
            )
        finally:
            self._complete_probe(task)

    def arcgraph_explain(
        self,
        repo_id: str = "default",
        task: str | None = None,
        targets: list[str] | None = None,
        max_results: int = 30,
        detail_level: str = "summary",
        profile: str = "review_default",
        include_source: bool = False,
        mcp_context: Context | None = None,
    ) -> dict[str, Any]:
        self._before_probe(task, mcp_context)
        try:
            return super().arcgraph_explain(
                repo_id=repo_id,
                task=task,
                targets=targets,
                max_results=max_results,
                detail_level=detail_level,
                profile=profile,
                include_source=include_source,
            )
        finally:
            self._complete_probe(task)

    def _before_probe(self, marker: str | None, context: Context | None) -> None:
        if marker in _CONCURRENCY_PROBES:
            self._coordinator.wait_for_peer(marker)
        elif marker in _CANCELLATION_PROBES:
            self._coordinator.wait_for_cancellation(marker, context)

    def _complete_probe(self, marker: str | None) -> None:
        if marker in _CONCURRENCY_PROBES or marker in _CANCELLATION_PROBES:
            self._coordinator.complete(marker)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--state-path", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_group = mcp_server.create_tool_group(
        repo_root=args.repo_root,
        output_dir=args.output_dir,
        repo_id=args.repo_id,
    )
    tool_group = _ProbeToolGroup(
        base_group.config,
        _ProbeCoordinator(Path(args.state_path)),
    )
    app = mcp_server._create_mcp_server("ArcGraph Runtime Probe")
    register_arcgraph_mcp_tools(app, tool_group)
    mcp_server._run_mcp_app(app, transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
