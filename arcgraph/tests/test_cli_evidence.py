from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.ci import evidence_requirement_summary, run_ci_checks
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def test_cli_evidence_status_reports_state_without_actions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "evidence",
            "status",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "warn"
    assert "actions" not in payload
    assert "suggested_commands" not in json.dumps(payload)
    assert payload["live_manifest"]["summary"]["status"] == "missing"
    assert payload["snapshot_manifest"]["exists"] is True
    assert payload["capability_summary"]["precision"] == "ast_fallback_only"


def test_cli_evidence_plan_python_full_matches_ci_required_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "evidence",
            "plan",
            "--profile",
            "python_full",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    requirements = evidence_requirement_summary(
        QueryEngine(output_dir).current(), require_python_full=True
    )["requirements"]
    ci_result = run_ci_checks(QueryEngine(output_dir), require_python_full=True)
    ci_checks = {check["name"]: check for check in ci_result["checks"]}
    actions = {action["kind"]: action for action in payload["actions"]}

    assert ci_checks["precision_inputs"]["status"] == (
        requirements["precision"]["check_status"]
    )
    assert ci_checks["coverage_inputs"]["status"] == (
        requirements["coverage"]["check_status"]
    )
    assert ci_checks["runtime_trace_inputs"]["status"] == (
        requirements["runtime_trace"]["check_status"]
    )
    assert actions["precision_scip"]["blocks_python_full"] is True
    assert actions["precision_pyright"]["blocks_python_full"] is True
    assert actions["coverage"]["blocks_python_full"] is True
    assert actions["runtime_trace"]["blocks_python_full"] is True


def test_cli_evidence_plan_rejects_invalid_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--repo-root",
                str(FIXTURE_ROOT),
                "--output-dir",
                str(output_dir),
                "evidence",
                "plan",
                "--profile",
                "full",
            ]
        )

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def _build_fixture_index(tmp_path: Path) -> Path:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    return output_dir
