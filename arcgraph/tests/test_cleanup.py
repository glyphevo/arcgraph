from __future__ import annotations

import json
from pathlib import Path

from arcgraph.core.cleanup import (
    arcgraph_output_storage_status,
    plan_arcgraph_output_cleanup,
    prune_arcgraph_output,
    prune_arcgraph_output_after_publish,
)
from arcgraph.core.evidence_manifest import (
    build_evidence_manifest,
    evidence_manifest_path,
    load_live_evidence_manifest,
)
from arcgraph.core.operation_lock import arcgraph_operation_lock
from arcgraph.interfaces.cli import main


def test_plan_cleanup_keeps_current_and_recent_builds(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    _write_build(output_dir, "20260101T000000Z-old")
    _write_build(output_dir, "20260102T000000Z-old")
    previous = _write_build(output_dir, "20260103T000000Z-prev")
    current = _write_build(output_dir, "20260104T000000Z-current")
    _write_current(output_dir, current.name)

    plan = plan_arcgraph_output_cleanup(output_dir, keep_builds=2)

    assert plan.current_build == f"builds/{current.name}"
    assert plan.kept_builds == [f"builds/{current.name}", f"builds/{previous.name}"]
    assert {candidate.index_version for candidate in plan.candidates} == {
        "20260101T000000Z-old",
        "20260102T000000Z-old",
    }
    assert all(candidate.kind == "build" for candidate in plan.candidates)


def test_storage_status_is_read_only_and_reports_safe_prune_preview(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    old = _write_build(output_dir, "20260101T000000Z-old")
    current = _write_build(output_dir, "20260102T000000Z-current")
    _write_current(output_dir, current.name)

    status = arcgraph_output_storage_status(output_dir, keep_builds=1)

    assert status["status_is_read_only"] is True
    assert status["build_count"] == 2
    assert status["current_build"] == f"builds/{current.name}"
    assert status["cleanup_candidate_count"] == 1
    assert status["reclaimable_bytes"] > 0
    assert status["safe_prune"]["mode"] == "dry_run"
    assert status["safe_prune"]["apply_requires_explicit_flag"] == "--apply"
    assert old.exists()
    assert current.exists()


def test_plan_cleanup_prunes_incomplete_newer_builds(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    previous = _write_build(output_dir, "20260103T000000Z-prev")
    current = _write_build(output_dir, "20260104T000000Z-current")
    incomplete = _write_incomplete_build(output_dir, "20260105T000000Z-incomplete")
    _write_current(output_dir, current.name)

    plan = plan_arcgraph_output_cleanup(output_dir, keep_builds=2)

    assert plan.kept_builds == [f"builds/{current.name}", f"builds/{previous.name}"]
    assert [candidate.index_version for candidate in plan.candidates] == [
        incomplete.name
    ]
    assert plan.candidates[0].reason == "incomplete or unpublished build"


def test_prune_cleanup_deletes_only_planned_old_builds(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    old = _write_build(output_dir, "20260101T000000Z-old")
    current = _write_build(output_dir, "20260102T000000Z-current")
    _write_current(output_dir, current.name)

    result = prune_arcgraph_output(output_dir, keep_builds=1, dry_run=False)

    assert result["status"] == "pruned"
    assert result["deleted_count"] == 1
    assert not old.exists()
    assert current.exists()
    assert (output_dir / "current.json").exists()


def test_after_publish_cleanup_runs_inside_existing_operation_lock(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    old = _write_build(output_dir, "20260101T000000Z-old")
    current = _write_build(output_dir, "20260102T000000Z-current")
    _write_current(output_dir, current.name)

    with arcgraph_operation_lock(output_dir):
        result = prune_arcgraph_output_after_publish(output_dir, keep_builds=1)

    assert result["status"] == "pruned"
    assert result["deleted_count"] == 1
    assert not old.exists()
    assert current.exists()


def test_after_publish_cleanup_reports_planning_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def broken_plan(*args, **kwargs):
        raise OSError("planned failure")

    monkeypatch.setattr(
        "arcgraph.core.cleanup.plan_arcgraph_output_cleanup", broken_plan
    )

    result = prune_arcgraph_output_after_publish(tmp_path / "arcgraph")

    assert result["status"] == "partial"
    assert result["deleted_count"] == 0
    assert result["errors"][0]["error"] == "OSError"


def test_cleanup_refuses_to_prune_builds_without_current_marker(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    old = _write_build(output_dir, "20260101T000000Z-old")

    plan = plan_arcgraph_output_cleanup(output_dir, keep_builds=1)

    assert plan.candidates == []
    assert old.exists()
    assert any("current" in warning for warning in plan.warnings)


def test_cleanup_treats_empty_active_pin_lists_as_corrupt_and_retains_history(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    old = _write_build(output_dir, "20260101T000000Z-old")
    current = _write_build(output_dir, "20260102T000000Z-current")
    _write_current(output_dir, current.name)
    pins = output_dir / "pins"
    pins.mkdir()
    (pins / "active.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "change_contract_version": "1.1.0",
                "repo_id": "repo",
                "pins_by_index_version": {old.name: []},
            }
        ),
        encoding="utf-8",
    )

    plan = plan_arcgraph_output_cleanup(output_dir, keep_builds=1)

    assert plan.candidates == []
    assert old.exists()
    assert any("invalid pin ids" in warning for warning in plan.warnings)


def test_apply_noop_does_not_create_missing_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "missing-ArcGraph"

    result = prune_arcgraph_output(output_dir, dry_run=False)

    assert result["status"] == "pass"
    assert not output_dir.exists()
    assert result["candidate_count"] == 0


def test_cleanup_optionally_removes_generated_input_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    current = _write_build(output_dir, "20260102T000000Z-current")
    _write_current(output_dir, current.name)
    scip_json = output_dir / "scip-index.json"
    coverage = output_dir / "coverage.xml"
    scip_json.write_text("{}", encoding="utf-8")
    coverage.write_text("<coverage />", encoding="utf-8")
    build_evidence_manifest(
        repo_root=tmp_path,
        output_dir=output_dir,
        commit_sha=None,
        source_roots=["."],
        precision_metrics={"status": "available", "scip_status": "available"},
        coverage_metrics={"status": "available", "path": "ArcGraph/coverage.xml"},
        scip_index_path=scip_json,
        coverage_path=coverage,
    )

    dry_run = prune_arcgraph_output(
        output_dir,
        keep_builds=1,
        include_input_artifacts=True,
    )
    result = prune_arcgraph_output(
        output_dir,
        keep_builds=1,
        include_input_artifacts=True,
        dry_run=False,
    )

    assert dry_run["status"] == "dry_run"
    assert {candidate["path"] for candidate in dry_run["candidates"]} == {
        "coverage.xml",
        "scip-index.json",
    }
    assert result["status"] == "pruned"
    assert not scip_json.exists()
    assert not coverage.exists()
    assert evidence_manifest_path(output_dir).exists()
    health = load_live_evidence_manifest(
        repo_root=tmp_path,
        output_dir=output_dir,
        commit_sha=None,
        source_roots=["."],
    )
    coverage_health = next(
        item for item in health["artifacts"] if item["kind"] == "coverage"
    )
    assert coverage_health["status"] == "missing"
    assert current.exists()


def test_cli_ops_prune_applies_cleanup_plan(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    old = _write_build(output_dir, "20260101T000000Z-old")
    current = _write_build(output_dir, "20260102T000000Z-current")
    _write_current(output_dir, current.name)

    exit_code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            "arcgraph",
            "ops",
            "prune",
            "--keep-builds",
            "1",
            "--apply",
        ]
    )

    assert exit_code == 0
    assert not old.exists()
    assert current.exists()


def _write_build(output_dir: Path, name: str) -> Path:
    build_dir = output_dir / "builds" / name
    build_dir.mkdir(parents=True)
    (build_dir / "index.sqlite").write_bytes(b"sqlite")
    (build_dir / "summary.json").write_text("{}", encoding="utf-8")
    return build_dir


def _write_incomplete_build(output_dir: Path, name: str) -> Path:
    build_dir = output_dir / "builds" / name
    build_dir.mkdir(parents=True)
    (build_dir / "index.sqlite").write_bytes(b"partial sqlite")
    (build_dir / "index.sqlite-wal").write_bytes(b"wal")
    return build_dir


def _write_current(output_dir: Path, build_name: str) -> None:
    payload = {
        "schema_version": "1.0.0",
        "index_version": build_name,
        "build_dir": f"builds/{build_name}",
        "summary_path": f"builds/{build_name}/summary.json",
    }
    (output_dir / "current.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_storage_status_cache_is_keyed_by_retention(tmp_path: Path) -> None:
    """keep_builds changes the candidate set and the suggested command, so
    it is part of a cached answer's identity."""

    from arcgraph.core import cleanup as cleanup_module

    output_dir = tmp_path / "output"
    (output_dir / "builds").mkdir(parents=True)
    cleanup_module._STORAGE_STATUS_CACHE.clear()

    first = cleanup_module.arcgraph_output_storage_status(output_dir, keep_builds=1)
    second = cleanup_module.arcgraph_output_storage_status(output_dir, keep_builds=9)

    assert first["safe_prune"]["command"].endswith("--keep-builds 1")
    assert second["safe_prune"]["command"].endswith("--keep-builds 9")
