from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.unresolved_classification import classify_unresolved_records
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "dynamic_boundary_triage"
EXPECTATIONS_PATH = (
    Path(__file__).parent / "golden" / "dynamic_boundary_triage" / "expectations.json"
)


def test_dynamic_boundary_triage_matrix_contract(tmp_path: Path) -> None:
    output_dir = _build_triage_index(tmp_path)
    expectations = _read_expectations()
    unresolved = QueryEngine(output_dir).unresolved(limit=1000)

    _assert_expected_unresolved(unresolved, expectations["expected_unresolved"])
    _assert_unresolved_summary(unresolved, expectations["unresolved_summary"])


def test_dynamic_boundary_cli_filters_after_classification(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = _build_triage_index(tmp_path)

    external_exit = main(
        [
            "--output-dir",
            str(output_dir),
            "unresolved",
            "--category",
            "external_service_boundary",
            "--limit",
            "2",
        ]
    )
    assert external_exit == 0
    external_payload = json.loads(capsys.readouterr().out)
    assert external_payload["summary"]["total"] == 5
    assert external_payload["summary"]["returned"] == 2
    assert all(
        item["category"] == "external_service_boundary"
        for item in external_payload["unresolved"]
    )

    blocking_exit = main(
        [
            "--output-dir",
            str(output_dir),
            "unresolved",
            "--release-blocking-only",
        ]
    )
    assert blocking_exit == 0
    blocking_payload = json.loads(capsys.readouterr().out)
    assert blocking_payload["summary"]["total"] == 0
    assert blocking_payload["classification"]["release_blocking_count"] == 0


def test_dynamic_boundary_classifier_keeps_negative_calls_dynamic() -> None:
    classified = classify_unresolved_records(
        {
            "unresolved": [
                _record("src/service.py", "cache.get"),
                _record("src/service.py", "bag.add"),
                _record("src/service.py", "plugin.run"),
                _record("src/service.py", "broker.publish"),
                _record("src/service.py", "router.add_api_route"),
                _record("src/generated/client.py", "client.call"),
                _record("src/openapi_client/client.py", "getattr(client, method_name)"),
            ]
        }
    )

    by_expression = {item["raw_expression"]: item for item in classified["records"]}
    for raw_expression in ("cache.get", "bag.add", "plugin.run"):
        record = by_expression[raw_expression]
        assert record["category"] == "true_dynamic_call"
        assert record["release_blocking"] is False

    assert by_expression["broker.publish"]["category"] == "external_service_boundary"
    assert by_expression["router.add_api_route"]["category"] == "framework_magic"
    assert by_expression["client.call"]["category"] == "generated_or_reflection"
    assert (
        by_expression["getattr(client, method_name)"]["category"]
        == "generated_or_reflection"
    )
    assert classified["release_blocking_count"] == 0


def _build_triage_index(tmp_path: Path) -> Path:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    return output_dir


def _read_expectations() -> dict[str, Any]:
    return json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))


def _assert_expected_unresolved(
    unresolved: dict[str, Any],
    expected_records: list[dict[str, Any]],
) -> None:
    records = unresolved["unresolved"]
    counts = Counter(
        (item.get("raw_expression"), item.get("category")) for item in records
    )
    by_key: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for item in records:
        by_key.setdefault(
            (item.get("raw_expression"), item.get("category")), []
        ).append(item)

    for expected in expected_records:
        key = (expected["raw_expression"], expected["category"])
        assert counts[key] == expected["count"]
        for record in by_key[key]:
            assert record["release_blocking"] is expected["release_blocking"]
            assert record["risk_level"] == expected["risk_level"]
            assert record["classification_reason"]
            assert record["suggested_next_step"]


def _assert_unresolved_summary(
    unresolved: dict[str, Any],
    expected_summary: dict[str, Any],
) -> None:
    classification = unresolved["classification"]
    assert classification["total_unresolved"] == expected_summary["total"]
    assert (
        classification["release_blocking_count"]
        == expected_summary["release_blocking_count"]
    )
    for category, count in expected_summary["category_counts"].items():
        assert classification["category_counts"][category] == count
    for risk_level, count in expected_summary["risk_counts"].items():
        assert classification["risk_counts"][risk_level] == count


def _record(path: str, raw_expression: str) -> dict[str, object]:
    return {
        "path": path,
        "start_line": 1,
        "properties": {
            "path": path,
            "line": 1,
            "raw_expression": raw_expression,
            "failed_strategy": "dynamic_dispatch",
        },
    }
