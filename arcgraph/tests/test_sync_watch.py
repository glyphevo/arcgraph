from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces import ops
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import FullBuildRequired


def _indexed_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "src" / "pkg" / "service.py"
    source.parent.mkdir(parents=True)
    (source.parent / "__init__.py").write_text("", encoding="utf-8")
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    output = tmp_path / "output" / "arcgraph"
    ArcGraphIndexer(tmp_path, output, [SourceRoot("src")]).build()
    return tmp_path, output, source


def test_sync_if_stale_publishes_a_fresh_incremental_index_quickly(
    tmp_path: Path,
) -> None:
    repo, output, source = _indexed_project(tmp_path)
    before = json.loads((output / "current.json").read_text(encoding="utf-8"))
    source.write_text("def run():\n    return 2\n", encoding="utf-8")

    result = ops.sync_index(repo_root=repo, output_dir=output, if_stale=True)

    assert result["status"] == "available"
    assert result["published"] is True
    assert result["freshness"]["stale"] is False
    assert isinstance(result["elapsed_ms"], int)
    assert result["elapsed_ms"] >= 0
    assert result["cleanup"]["dry_run"] is True
    assert result["cleanup"]["deleted_count"] == 0
    after = json.loads((output / "current.json").read_text(encoding="utf-8"))
    assert after["index_version"] != before["index_version"]


def test_sync_failure_keeps_previous_current_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, output, source = _indexed_project(tmp_path)
    before = (output / "current.json").read_bytes()
    source.write_text("def run():\n    return 3\n", encoding="utf-8")

    def fail(_self: object, *, cleanup_after_publish: bool = True) -> dict[str, object]:
        del cleanup_after_publish
        raise RuntimeError("synthetic incremental failure")

    monkeypatch.setattr(ops.ArcGraphReindexer, "reindex_changed", fail)
    result = ops.sync_index(repo_root=repo, output_dir=output, if_stale=True)

    assert result["status"] == "partial"
    assert result["action"] == "kept_previous_index"
    assert result["recovery_action"]["command"] == "arcgraph sync --if-stale"
    assert (output / "current.json").read_bytes() == before


def test_sync_reports_full_build_when_incremental_guard_requires_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, output, source = _indexed_project(tmp_path)
    source.write_text("def run():\n    return 3\n", encoding="utf-8")

    def fail(_self: object, *, cleanup_after_publish: bool = True) -> dict[str, object]:
        del cleanup_after_publish
        raise FullBuildRequired("module identity changed")

    monkeypatch.setattr(ops.ArcGraphReindexer, "reindex_changed", fail)

    result = ops.sync_index(repo_root=repo, output_dir=output, if_stale=True)

    assert result["status"] == "partial"
    assert result["recovery_action"]["command"] == "arcgraph build"


def test_watch_debounces_and_syncs_stale_index(tmp_path: Path) -> None:
    repo, output, source = _indexed_project(tmp_path)
    source.write_text("def run():\n    return 4\n", encoding="utf-8")

    result = ops.watch_index(
        repo_root=repo,
        output_dir=output,
        poll_interval_seconds=0.05,
        debounce_seconds=0,
        max_cycles=1,
    )

    assert result["events"][0]["published"] is True
    assert QueryEngine(output).current()["freshness"]["stale"] is False
    assert result["cleanup_policy"]["automatic_deletion"] is False


def test_watch_default_lifecycle_uses_declared_debounce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify default timing deterministically instead of racing a CI runner."""

    repo, output, _source = _indexed_project(tmp_path)
    clock = {"now": 10.0}
    published = {"value": False}

    def current_status(_output: Path) -> tuple[dict[str, object], None]:
        return (
            {
                "freshness": {
                    "status": "fresh" if published["value"] else "stale",
                    "stale": not published["value"],
                }
            },
            None,
        )

    def sync_index(**_kwargs: object) -> dict[str, object]:
        published["value"] = True
        return {"status": "available", "published": True, "elapsed_ms": 120}

    monkeypatch.setattr(ops, "_current_status", current_status)
    monkeypatch.setattr(ops, "sync_index", sync_index)
    monkeypatch.setattr(ops.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        ops.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    result = ops.watch_index(repo_root=repo, output_dir=output, max_cycles=5)

    assert result["poll_interval_seconds"] == 0.25
    assert result["debounce_seconds"] == 0.5
    assert len(result["events"]) == 1
    assert result["events"][0]["published"] is True
    assert result["events"][0]["elapsed_ms"] == 120
    assert clock["now"] == 11.0


def test_watch_streams_events_and_truthfully_summarizes_omissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, output, _source = _indexed_project(tmp_path)
    current = QueryEngine(output).current()
    current["freshness"] = {"status": "stale", "stale": True}
    monkeypatch.setattr(ops, "_current_status", lambda _output: (current, None))
    monkeypatch.setattr(
        ops,
        "sync_index",
        lambda **_kwargs: {
            "status": "available",
            "published": True,
            "_exit_code": 7,
        },
    )
    frames: list[dict[str, object]] = []

    result = ops.watch_index(
        repo_root=repo,
        output_dir=output,
        debounce_seconds=0,
        max_cycles=25,
        on_event=frames.append,
    )

    assert len(frames) == 25
    assert all(frame["type"] == "watch_event" for frame in frames)
    assert all("_exit_code" not in frame["event"] for frame in frames)
    assert result["event_summary"] == {
        "total": 25,
        "returned": 20,
        "limit": 20,
        "truncated": True,
        "omitted": 5,
    }


def test_watch_failure_is_nonzero_without_leaking_internal_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, output, _source = _indexed_project(tmp_path)
    current = QueryEngine(output).current()
    current["freshness"] = {"status": "stale", "stale": True}
    monkeypatch.setattr(ops, "_current_status", lambda _output: (current, None))
    monkeypatch.setattr(
        ops,
        "sync_index",
        lambda **_kwargs: {"status": "partial", "_exit_code": 1},
    )

    result = ops.watch_index(
        repo_root=repo,
        output_dir=output,
        debounce_seconds=0,
        max_cycles=1,
    )

    assert result["status"] == "partial"
    assert result["stopped"] == "sync_failed"
    assert result["_exit_code"] == 1
    assert "_exit_code" not in result["events"][0]


def test_watch_supports_cooperative_shutdown(tmp_path: Path) -> None:
    repo, output, _source = _indexed_project(tmp_path)

    result = ops.watch_index(
        repo_root=repo,
        output_dir=output,
        stop_requested=lambda: True,
        stop_reason=lambda: "signal:SIGTERM",
    )

    assert result["status"] == "stopped"
    assert result["stopped"] == "signal:SIGTERM"
    assert result["cycles"] == 0


def test_watch_cli_streams_json_and_returns_nonzero_without_internal_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_watch_index(**kwargs: object) -> dict[str, object]:
        on_event = kwargs["on_event"]
        assert callable(on_event)
        on_event(
            {
                "schema_version": "1.0.0",
                "type": "watch_event",
                "event_index": 1,
                "event": {"status": "partial"},
            }
        )
        return {
            "schema_version": "1.0.0",
            "status": "partial",
            "events": [{"status": "partial"}],
            "_exit_code": 1,
        }

    monkeypatch.setattr("arcgraph.interfaces.cli_ops.watch_index", fake_watch_index)

    exit_code = main(
        [
            "--repo-root",
            str(tmp_path),
            "watch",
            "--max-cycles",
            "1",
        ]
    )
    frames = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert exit_code == 1
    assert [frame.get("type") for frame in frames] == ["watch_event", None]
    assert all("_exit_code" not in frame for frame in frames)


def test_stale_query_returns_machine_readable_recovery_action(tmp_path: Path) -> None:
    _repo, output, source = _indexed_project(tmp_path)
    source.write_text("def run():\n    return 5\n", encoding="utf-8")

    payload = QueryEngine(output).symbol("pkg.service.run")

    assert payload["freshness"]["stale"] is True
    assert payload["recovery_action"] == {
        "kind": "refresh_index",
        "command": "arcgraph sync --if-stale",
        "automatic": False,
        "reader_remains_read_only": True,
    }


def test_watch_stops_cooperatively_when_stdout_consumer_closes(
    tmp_path: Path, monkeypatch
) -> None:
    """A closed stdout pipe ends the watch instead of raising, so no further
    incremental publications happen with nobody able to read them."""

    import argparse

    from arcgraph.interfaces import cli_ops

    emitted: list[dict] = []

    def broken_print(*args, **kwargs):
        emitted.append({})
        raise BrokenPipeError(32, "Broken pipe")

    captured: dict[str, object] = {}

    def fake_watch_index(**kwargs):
        on_event = kwargs["on_event"]
        on_event({"type": "watch_event", "event_index": 1})
        captured["stop_requested"] = kwargs["stop_requested"]()
        captured["stop_reason"] = kwargs["stop_reason"]()
        # A second event must not raise again after the pipe closed.
        on_event({"type": "watch_event", "event_index": 2})
        return {"status": "available", "stopped": "stdout_closed", "events": []}

    monkeypatch.setattr(cli_ops, "print", broken_print, raising=False)
    monkeypatch.setattr(cli_ops, "watch_index", fake_watch_index)

    args = argparse.Namespace(
        repo_root=str(tmp_path),
        output_dir="output",
        poll_interval=0.25,
        debounce=0.5,
        max_cycles=1,
    )
    payload = cli_ops.handle_watch(args)

    assert captured["stop_requested"] is True
    assert captured["stop_reason"] == "stdout_closed"
    assert len(emitted) == 1
    assert payload["_streaming_output"] is True
