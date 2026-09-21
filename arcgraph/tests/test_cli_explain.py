from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph.core.schemas import ExplainResponse
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.providers.context_provider import _compact_explain_edges

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
TARGET = "pkg.service.build_message"
MISSING_TARGET = "pkg.service.DoesNotExist"


def test_cli_explain_returns_compact_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "explain",
            TARGET,
            "--task",
            "review message building",
            "--detail-level",
            "detailed",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    validated = ExplainResponse.model_validate(payload)
    explanation = validated.explanations[0]
    incoming_edge = next(
        edge
        for edge in explanation["incoming_edges"]
        if edge["source"] == "fn:pkg.api.hello"
        and edge["target"] == "fn:pkg.service.build_message"
    )

    assert validated.status == "available"
    assert validated.task == "review message building"
    assert validated.targets == [TARGET]
    assert validated.detail_level == "detailed"
    assert validated.source_snippets == {"requested": False, "enabled": False}
    assert explanation["resolved_targets"] == ["fn:pkg.service.build_message"]
    assert explanation["symbols"]
    assert explanation["callers"]
    assert incoming_edge["evidence"]
    assert incoming_edge["resolution"]["strategy"]
    assert "fallbacks" in incoming_edge["resolution"]
    assert "confidence_sources" in incoming_edge
    assert "properties" not in incoming_edge
    assert validated.estimated_tokens > 0
    assert validated.estimated_tokens < 8000
    assert not _contains_key(payload, "properties")
    assert not _contains_key(payload, "snippet")
    assert not _contains_key(payload, "source_snippet")


def test_cli_explain_include_source_sets_snippet_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "explain",
            TARGET,
            "--include-source",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    validated = ExplainResponse.model_validate(payload)

    assert validated.source_snippets == {"requested": True, "enabled": True}


def test_cli_explain_rejects_full_detail_level(
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
                "explain",
                TARGET,
                "--detail-level",
                "full",
            ]
        )

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_explain_marks_mixed_missing_target_partial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "explain",
            TARGET,
            MISSING_TARGET,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    validated = ExplainResponse.model_validate(payload)
    missing = next(
        explanation
        for explanation in validated.explanations
        if explanation["target"] == MISSING_TARGET
    )

    assert validated.status == "partial"
    assert missing["status"] == "partial"
    assert any(MISSING_TARGET in warning for warning in validated.warnings)
    assert not _contains_key(missing["unresolved"], "properties")


def test_compact_explain_edge_preserves_provenance_and_caps_evidence() -> None:
    edges, evidence_truncated = _compact_explain_edges(
        [
            {
                "source": "fn:source",
                "target": "fn:target",
                "kind": "calls",
                "confidence": "confirmed",
                "semantic_role": "call",
                "resolution": {
                    "status": "resolved",
                    "strategy": "scip",
                    "fallbacks": ["ast"],
                },
                "confidence_sources": {"scip": ["call", "definition"]},
                "evidence": [
                    {"kind": "one", "path": "src/a.py"},
                    {"kind": "two", "path": "src/a.py"},
                    {"kind": "three", "path": "src/a.py"},
                    {"kind": "four", "path": "src/a.py"},
                ],
                "properties": {"large": "payload"},
            }
        ],
        max_results=5,
        evidence_limit=2,
    )

    assert evidence_truncated == 2
    assert edges[0]["confidence_sources"] == {"scip": ["call", "definition"]}
    assert edges[0]["resolution"]["strategy"] == "scip"
    assert len(edges[0]["evidence"]) == 2
    assert edges[0]["evidence_total"] == 4
    assert edges[0]["evidence_truncated"] == 2
    assert "properties" not in edges[0]


def _build_fixture_index(tmp_path: Path) -> Path:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    return output_dir


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False
