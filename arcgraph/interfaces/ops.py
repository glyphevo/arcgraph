"""Operational helpers for ArcGraph rebuild and stale-index automation."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable

from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.cleanup import (
    DEFAULT_BUILD_RETENTION,
    prune_arcgraph_output,
)
from arcgraph.core.query_engine import QueryEngine, SchemaVersionError
from arcgraph.core.scanner import SourceRoot, SourceRootDetection
from arcgraph.core.schemas import SCHEMA_VERSION
from arcgraph.pipeline.reindexer import ArcGraphReindexer, FullBuildRequired


def rebuild_index(
    *,
    repo_root: Path,
    output_dir: Path,
    source_roots: list[SourceRoot],
    if_stale: bool = False,
    scip_index_path: str | Path | None = None,
    pyright_export_path: str | Path | None = None,
    runtime_trace_path: str | Path | None = None,
    coverage_path: str | Path | None = None,
    enable_v2_call_resolution: bool = True,
    source_root_detection: SourceRootDetection | None = None,
) -> dict[str, Any]:
    before, reason = _current_status(output_dir)
    freshness = before.get("freshness", {}) if before else {}
    should_rebuild = (
        not if_stale
        or before is None
        or freshness.get("stale")
        or before.get("schema_version") != SCHEMA_VERSION
    )

    if not should_rebuild:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "skipped",
            "action": "none",
            "reason": "Current index is fresh.",
            "freshness": freshness,
            "before": before,
        }

    metadata, build_dir = ArcGraphIndexer(
        repo_root,
        output_dir,
        source_roots,
        scip_index_path=scip_index_path,
        pyright_export_path=pyright_export_path,
        runtime_trace_path=runtime_trace_path,
        coverage_path=coverage_path,
        enable_v2_call_resolution=enable_v2_call_resolution,
        source_root_detection=source_root_detection,
    ).build()
    payload = metadata.model_dump(mode="json")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "available",
        "action": "rebuilt",
        "reason": reason or "Forced rebuild requested.",
        "freshness": {"status": "fresh", "stale": False},
        "before": before,
        "metadata": payload,
        "build_dir": str(build_dir),
    }


def stale_alert(
    query_engine: QueryEngine, *, fail_on_stale: bool = False
) -> dict[str, Any]:
    current = query_engine.current()
    freshness = current.get("freshness", {})
    stale = bool(freshness.get("stale"))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "index_version": current.get("index_version"),
        "status": "warn" if stale else "pass",
        "freshness": freshness,
        "capabilities": current.get("capabilities", {}),
        "warnings": ["ArcGraph index is stale."] if stale else [],
    }
    if stale and fail_on_stale:
        payload["_exit_code"] = 1
    return payload


def sync_index(
    *,
    repo_root: Path,
    output_dir: Path,
    if_stale: bool = False,
) -> dict[str, Any]:
    """Explicitly publish an incremental index and preserve the old pointer on failure."""

    started = time.monotonic()
    before, reason = _current_status(output_dir)
    if before is None:
        return _sync_failure(
            before=None,
            reason=reason or "No current index exists.",
            elapsed_ms=_elapsed_ms(started),
            command="arcgraph build",
        )
    freshness = before.get("freshness", {})
    if if_stale and not freshness.get("stale"):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "skipped",
            "action": "none",
            "published": False,
            "reason": "Current index is fresh.",
            "before_index_version": before.get("index_version"),
            "index_version": before.get("index_version"),
            "freshness": freshness,
            "elapsed_ms": _elapsed_ms(started),
        }
    try:
        result = ArcGraphReindexer(repo_root, output_dir).reindex_changed(
            cleanup_after_publish=False
        )
    except FullBuildRequired as exc:
        return _sync_failure(
            before=before,
            reason=f"{type(exc).__name__}: {exc}",
            elapsed_ms=_elapsed_ms(started),
            command="arcgraph build",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _sync_failure(
            before=before,
            reason=f"{type(exc).__name__}: {exc}",
            elapsed_ms=_elapsed_ms(started),
            command="arcgraph sync --if-stale",
        )

    after, after_reason = _current_status(output_dir)
    if after is None:
        return _sync_failure(
            before=before,
            reason=after_reason or "Published index could not be reopened.",
            elapsed_ms=_elapsed_ms(started),
            command="arcgraph build",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "available",
        "action": result.get("status", "reindexed"),
        "published": after.get("index_version") != before.get("index_version"),
        "before_index_version": before.get("index_version"),
        "index_version": after.get("index_version"),
        "freshness": after.get("freshness", {}),
        "changed": result.get("changed", {}),
        "cleanup": prune_arcgraph_output(
            output_dir,
            keep_builds=DEFAULT_BUILD_RETENTION,
            dry_run=True,
        ),
        "elapsed_ms": _elapsed_ms(started),
    }


def watch_index(
    *,
    repo_root: Path,
    output_dir: Path,
    poll_interval_seconds: float = 0.25,
    debounce_seconds: float = 0.5,
    max_cycles: int | None = None,
    stop_requested: Callable[[], bool] | None = None,
    stop_reason: Callable[[], str] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Poll freshness and debounce explicit incremental publications."""

    poll_interval_seconds = max(0.05, min(poll_interval_seconds, 5.0))
    debounce_seconds = max(0.0, min(debounce_seconds, 10.0))
    cycles = 0
    stale_since: float | None = None
    events: list[dict[str, Any]] = []
    event_count = 0
    stopped = "max_cycles"
    stop_requested_fn = stop_requested
    stop_reason_fn = stop_reason

    def record_event(event: dict[str, Any]) -> None:
        nonlocal event_count
        public_event = dict(event)
        public_event.pop("_exit_code", None)
        event_count += 1
        events.append(public_event)
        if len(events) > 20:
            del events[0]
        if on_event is not None:
            on_event(
                {
                    "schema_version": SCHEMA_VERSION,
                    "type": "watch_event",
                    "event_index": event_count,
                    "event": public_event,
                }
            )

    try:
        while max_cycles is None or cycles < max_cycles:
            if stop_requested_fn is not None and stop_requested_fn():
                stopped = (
                    stop_reason_fn() if stop_reason_fn is not None else "requested"
                )
                break
            cycles += 1
            current, reason = _current_status(output_dir)
            if current is None:
                record_event(
                    _sync_failure(
                        before=None,
                        reason=reason or "No current index exists.",
                        elapsed_ms=0,
                        command="arcgraph build",
                    )
                )
                stopped = "index_unavailable"
                break
            now = time.monotonic()
            if current.get("freshness", {}).get("stale"):
                stale_since = stale_since or now
                if now - stale_since >= debounce_seconds:
                    event = sync_index(
                        repo_root=repo_root,
                        output_dir=output_dir,
                        if_stale=True,
                    )
                    record_event(event)
                    if event.get("status") == "partial":
                        stopped = "sync_failed"
                        break
                    stale_since = None
            else:
                stale_since = None
            if max_cycles is None or cycles < max_cycles:
                time.sleep(poll_interval_seconds)
    except KeyboardInterrupt:
        stopped = "keyboard_interrupt"
    abnormal_stop = stopped in {"index_unavailable", "sync_failed"}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "partial" if abnormal_stop else "stopped",
        "action": "watched",
        "cycles": cycles,
        "poll_interval_seconds": poll_interval_seconds,
        "debounce_seconds": debounce_seconds,
        "events": events,
        "event_summary": {
            "total": event_count,
            "returned": len(events),
            "limit": 20,
            "truncated": event_count > len(events),
            "omitted": max(0, event_count - len(events)),
        },
        "cleanup_policy": {
            "automatic_deletion": False,
            "detail": "Watch publishes indexes but only reports dry-run cleanup candidates.",
        },
        "stopped": stopped,
    }
    if abnormal_stop:
        payload["_exit_code"] = 1
    return payload


def _sync_failure(
    *,
    before: dict[str, Any] | None,
    reason: str,
    elapsed_ms: int,
    command: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "partial",
        "action": "kept_previous_index",
        "published": False,
        "reason": reason,
        "before_index_version": (before or {}).get("index_version"),
        "index_version": (before or {}).get("index_version"),
        "freshness": (before or {}).get("freshness", {}),
        "recovery_action": {
            "kind": "refresh_index",
            "command": command,
            "automatic": False,
        },
        "elapsed_ms": elapsed_ms,
        "_exit_code": 1,
    }


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def prune_output(
    *,
    output_dir: Path,
    keep_builds: int = DEFAULT_BUILD_RETENTION,
    include_input_artifacts: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    return prune_arcgraph_output(
        output_dir,
        keep_builds=keep_builds,
        include_input_artifacts=include_input_artifacts,
        dry_run=not apply,
    )


def _current_status(output_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        current = QueryEngine(output_dir).current()
    except FileNotFoundError:
        return None, "No current index exists."
    except SchemaVersionError as exc:
        return None, str(exc)
    return current, None
