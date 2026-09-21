from __future__ import annotations

from pathlib import Path

from arcgraph.core.evidence_manifest import (
    build_evidence_manifest,
    evidence_sidecar_path,
    evidence_status,
    evidence_manifest_path,
    evidence_plan,
    refresh_evidence_manifest_health,
    write_evidence_sidecar,
)


def test_evidence_manifest_reports_missing_and_unknown_tool_version(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = repo_root / "output" / "arcgraph"
    repo_root.mkdir()
    pyright = output_dir / "pyright-export.json"
    pyright.parent.mkdir(parents=True)
    pyright.write_text(
        '{"status":"available","source_roots":["src"],"type_info":[]}',
        encoding="utf-8",
    )

    manifest = build_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
        precision_metrics={"status": "available", "pyright_status": "available"},
        pyright_export_path=pyright,
    )
    pyright_record = _artifact(manifest, "precision_pyright")
    coverage_record = _artifact(manifest, "coverage")

    assert evidence_manifest_path(output_dir).exists()
    assert pyright_record["status"] == "available"
    assert pyright_record["tool_version"] == "unknown"
    assert coverage_record["status"] == "missing"


def test_evidence_manifest_refresh_marks_hash_change_stale(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    output_dir = repo_root / "output" / "arcgraph"
    repo_root.mkdir()
    scip = output_dir / "scip-index.json"
    scip.parent.mkdir(parents=True)
    scip.write_text('{"documents":[]}', encoding="utf-8")
    manifest = build_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
        precision_metrics={"status": "available", "scip_status": "available"},
        scip_index_path=scip,
    )

    scip.write_text('{"documents":[{"path":"src/pkg.py"}]}', encoding="utf-8")
    refreshed = refresh_evidence_manifest_health(
        manifest,
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
    )

    assert _artifact(refreshed, "precision_scip")["status"] == "stale"
    assert _artifact(refreshed, "precision_scip")["reason"] == ("artifact_hash_changed")


def test_evidence_manifest_reports_partial_importer_metric(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    output_dir = repo_root / "output" / "arcgraph"
    repo_root.mkdir()
    scip = output_dir / "scip-index.json"
    scip.parent.mkdir(parents=True)
    scip.write_text('{"documents":[]}', encoding="utf-8")

    manifest = build_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
        precision_metrics={"status": "partial", "scip_status": "partial"},
        scip_index_path=scip,
        write_live=False,
    )
    scip_record = _artifact(manifest, "precision_scip")

    assert scip_record["status"] == "partial"
    assert scip_record["reason"] == "metric_partial"


def test_evidence_manifest_reports_invalid_and_out_of_scope(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = repo_root / "output" / "arcgraph"
    repo_root.mkdir()
    pyright = output_dir / "pyright-export.json"
    pyright.parent.mkdir(parents=True)
    pyright.write_text("{not-json", encoding="utf-8")

    invalid = build_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
        precision_metrics={"status": "available", "pyright_status": "available"},
        pyright_export_path=pyright,
        write_live=False,
    )
    assert _artifact(invalid, "precision_pyright")["status"] == "invalid"

    pyright.write_text(
        '{"status":"available","source_roots":["other"]}',
        encoding="utf-8",
    )
    out_of_scope = build_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
        precision_metrics={"status": "available", "pyright_status": "available"},
        pyright_export_path=pyright,
        write_live=False,
    )

    assert _artifact(out_of_scope, "precision_pyright")["status"] == "out_of_scope"
    assert _artifact(out_of_scope, "precision_pyright")["reason"] == (
        "source_roots_mismatch"
    )

    runtime_trace = output_dir / "runtime-trace.json"
    runtime_trace.write_text(
        '{"trace_run":{"commit_sha":"other","source":"pytest"},"events":[]}',
        encoding="utf-8",
    )
    commit_mismatch = build_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
        runtime_metrics={"status": "available"},
        runtime_trace_path=runtime_trace,
        write_live=False,
    )

    assert _artifact(commit_mismatch, "runtime_trace")["status"] == "out_of_scope"
    assert _artifact(commit_mismatch, "runtime_trace")["reason"] == "commit_mismatch"


def test_evidence_plan_reports_missing_actions(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    output_dir = repo_root / "output" / "arcgraph"
    repo_root.mkdir()

    plan = evidence_plan(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha=None,
        source_roots=["src"],
    )

    assert plan["status"] == "warn"
    assert plan["live_manifest_exists"] is False
    assert {action["kind"] for action in plan["actions"]} == {
        "precision_scip",
        "precision_pyright",
        "coverage",
        "runtime_trace",
    }
    assert all(action["expected_confidence_gain"] for action in plan["actions"])
    coverage_action = next(
        action for action in plan["actions"] if action["kind"] == "coverage"
    )
    assert "arcgraph evidence stamp" in coverage_action["suggested_command"]
    assert "--cov=src" in coverage_action["suggested_commands"][0]
    assert "--cov-report=xml:output/arcgraph/coverage.xml" in (
        coverage_action["suggested_commands"][0]
    )


def test_evidence_plan_uses_project_name_and_build_inputs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    output_dir = repo_root / "output" / "arcgraph"
    repo_root.mkdir()

    plan = evidence_plan(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha=None,
        source_roots=[".", "lib"],
        project_name="fixture-project",
        profile="python_full",
        python_full_requirements={
            "precision": {"blocks_python_full": True},
            "coverage": {"blocks_python_full": True},
            "runtime_trace": {"blocks_python_full": True},
        },
    )
    actions = {action["kind"]: action for action in plan["actions"]}

    scip_command = actions["precision_scip"]["suggested_commands"][0]
    coverage_command = actions["coverage"]["suggested_commands"][0]

    assert "--project-name fixture-project" in scip_command
    assert "NAME" not in scip_command
    assert "--cov=." in coverage_command
    assert "--cov=lib" in coverage_command
    assert actions["precision_scip"]["follow_up_build_inputs"] == [
        "--scip-index",
        "output/arcgraph/scip-index.json",
    ]
    assert actions["coverage"]["follow_up_build_inputs"] == [
        "--coverage",
        "output/arcgraph/coverage.xml",
    ]
    assert actions["coverage"]["blocks_python_full"] is True


def test_evidence_status_reports_live_snapshot_without_actions(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = repo_root / "output" / "arcgraph"
    repo_root.mkdir()
    manifest = build_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
    )

    status = evidence_status(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
        snapshot_manifest=manifest,
        capabilities={
            "precision": "ast_fallback_only",
            "coverage": "unavailable",
            "runtime_trace": "unavailable",
        },
    )

    assert status["status"] == "warn"
    assert "actions" not in status
    assert "suggested_commands" not in status
    assert status["live_manifest"]["summary"]["status"] == "missing"
    assert status["snapshot_manifest"]["exists"] is True
    assert status["live_vs_snapshot"]["status"] == "matched"
    assert status["capability_summary"]["coverage"] == "unavailable"


def test_evidence_manifest_uses_coverage_sidecar_scope_metadata(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = repo_root / "output" / "arcgraph"
    repo_root.mkdir()
    coverage = output_dir / "coverage.xml"
    coverage.parent.mkdir(parents=True)
    coverage.write_text("<coverage />", encoding="utf-8")

    sidecar = write_evidence_sidecar(
        repo_root=repo_root,
        artifact_path=coverage,
        kind="coverage",
        commit_sha="old",
        source_roots=["src"],
        tool_name="coverage",
        tool_version="7.0",
    )
    scoped = build_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
        coverage_metrics={"status": "available"},
        coverage_path=coverage,
        write_live=False,
    )

    assert evidence_sidecar_path(coverage).exists()
    assert sidecar["metadata"]["tool_version"] == "7.0"
    coverage_record = _artifact(scoped, "coverage")
    assert coverage_record["status"] == "out_of_scope"
    assert coverage_record["reason"] == "commit_mismatch"
    assert coverage_record["commit_sha"] == "old"
    assert coverage_record["source_roots"] == ["src"]
    assert coverage_record["tool_version"] == "7.0"


def test_evidence_manifest_marks_malformed_sidecar_invalid(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    output_dir = repo_root / "output" / "arcgraph"
    repo_root.mkdir()
    coverage = output_dir / "coverage.xml"
    coverage.parent.mkdir(parents=True)
    coverage.write_text("<coverage />", encoding="utf-8")
    evidence_sidecar_path(coverage).write_text("{not-json", encoding="utf-8")

    manifest = build_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc123",
        source_roots=["src"],
        coverage_metrics={"status": "available"},
        coverage_path=coverage,
        write_live=False,
    )

    coverage_record = _artifact(manifest, "coverage")
    assert coverage_record["status"] == "invalid"
    assert coverage_record["reason"].startswith("sidecar_parse_error:")


def _artifact(manifest: dict[str, object], kind: str) -> dict[str, object]:
    for artifact in manifest["artifacts"]:  # type: ignore[index]
        if isinstance(artifact, dict) and artifact.get("kind") == kind:
            return artifact
    raise AssertionError(f"Missing artifact {kind}")
