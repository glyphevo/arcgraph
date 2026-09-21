"""CLI handlers for explicit incremental synchronization lifecycle commands."""

from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path
from typing import Any

from arcgraph.interfaces.ops import sync_index, watch_index


def handle_sync(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    return sync_index(
        repo_root=repo_root,
        output_dir=output_dir,
        if_stale=args.if_stale,
    )


def handle_watch(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    stop_state: dict[str, str | None] = {"reason": None}
    previous_handlers: dict[signal.Signals, Any] = {}

    def request_stop(signum: int, _frame: Any) -> None:
        stop_state["reason"] = f"signal:{signal.Signals(signum).name}"

    for watched_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[watched_signal] = signal.getsignal(watched_signal)
            signal.signal(watched_signal, request_stop)
        except ValueError:
            # Embedders can invoke the CLI outside the main thread. The library
            # callback contract still supports cooperative shutdown there.
            previous_handlers.clear()
            break

    def emit_event(frame: dict[str, Any]) -> None:
        if stop_state["reason"] is not None:
            return
        try:
            print(json.dumps(frame, ensure_ascii=False, sort_keys=True), flush=True)
        except BrokenPipeError:
            # A closed consumer ends the watch cooperatively: continuing would
            # keep publishing incremental builds (which delete retired builds)
            # with nobody able to read the disclosure.
            stop_state["reason"] = "stdout_closed"

    try:
        payload = watch_index(
            repo_root=repo_root,
            output_dir=output_dir,
            poll_interval_seconds=args.poll_interval,
            debounce_seconds=args.debounce,
            max_cycles=args.max_cycles,
            stop_requested=lambda: stop_state["reason"] is not None,
            stop_reason=lambda: stop_state["reason"] or "requested",
            on_event=emit_event,
        )
    finally:
        for watched_signal, previous in previous_handlers.items():
            signal.signal(watched_signal, previous)
    payload["_streaming_output"] = True
    return payload
