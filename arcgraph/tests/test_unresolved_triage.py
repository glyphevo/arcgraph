from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.graph_store import GraphStoreWriter
from arcgraph.core.schemas import (
    ContextRequest,
    IndexMetadata,
    Node,
    SemanticDiagnostic,
)
from arcgraph.core.unresolved_classification import classify_unresolved_records
from arcgraph.interfaces.cli import main
from arcgraph.providers.context_provider import ContextProvider


def test_unresolved_classification_covers_agent_triage_fields() -> None:
    classified = classify_unresolved_records(
        {
            "unresolved": [
                _record("src/app.py", "service.compute", "ast_attribute_resolution"),
                _record("src/app.py", "fallback_fn", "dynamic_dispatch"),
                _record(
                    "tests/test_app.py", "assert_called_once", "ast_name_resolution"
                ),
                _record("src/db.py", "db.execute", "ast_attribute_resolution"),
                _record("src/types.py", "receiver.call", "missing_type_context"),
                _record("src/api.py", "APIRouter", "ast_name_resolution"),
                _record(
                    "src/generated/client.py", "client.call", "ast_attribute_resolution"
                ),
            ]
        }
    )

    assert classified["category_counts"] == {
        "external_service_boundary": 1,
        "framework_magic": 1,
        "generated_or_reflection": 1,
        "missing_type_context": 1,
        "mock_or_assertion": 1,
        "static_candidate": 1,
        "true_dynamic_call": 1,
    }
    assert classified["release_blocking_count"] == 2
    assert classified["risk_counts"] == {"high": 2, "low": 2, "medium": 3}
    assert classified["failed_strategy_counts"] == {}
    assert len(classified["recommended_actions"]) == 7
    for item in classified["records"]:
        assert item["category"]
        assert item["risk_level"] in {"high", "medium", "low"}
        assert isinstance(item["release_blocking"], bool)
        assert item["classification_reason"]
        assert item["suggested_next_step"]


def test_unresolved_classification_keeps_named_callback_parameters_dynamic() -> None:
    classified = classify_unresolved_records(
        {
            "unresolved": [
                _record("src/app.py", "key_fn", "dynamic_dispatch"),
                _record("src/app.py", "plan_exists_fn", "dynamic_dispatch"),
            ]
        }
    )

    assert classified["category_counts"]["true_dynamic_call"] == 2
    assert classified["release_blocking_count"] == 0


def test_unresolved_classification_does_not_downgrade_by_test_path_or_name() -> None:
    classified = classify_unresolved_records(
        {
            "unresolved": [
                _record(
                    "scripts/tests/test_bundle.py",
                    "mutation",
                    "ast_name_resolution",
                ),
                _record(
                    "scripts/tests/test_bundle.py",
                    "mutation",
                    "dynamic_dispatch",
                ),
            ]
        }
    )

    assert classified["category_counts"]["static_candidate"] == 1
    assert classified["category_counts"]["true_dynamic_call"] == 1
    assert classified["release_blocking_count"] == 1


@pytest.mark.parametrize("raw_expression", ["result_callback", "factory"])
def test_unresolved_direct_names_need_binding_evidence_to_be_dynamic(
    raw_expression: str,
) -> None:
    classified = classify_unresolved_records(
        {"unresolved": [_record("src/app.py", raw_expression, "ast_name_resolution")]}
    )

    assert classified["records"][0]["category"] == "static_candidate"
    assert classified["records"][0]["release_blocking"] is True


def test_mock_classification_uses_the_call_expression_not_scope_name() -> None:
    classified = classify_unresolved_records(
        {
            "unresolved": [
                {
                    **_record("src/app.py", "ordinary_call", "ast_name_resolution"),
                    "properties": {
                        **_record("src/app.py", "ordinary_call", "ast_name_resolution")[
                            "properties"
                        ],
                        "source_qualname": "pkg.test_assert_called_regression",
                    },
                },
                _record(
                    "src/app.py",
                    "worker.assert_called_once",
                    "ast_attribute_resolution",
                ),
            ]
        }
    )

    by_expression = {
        item["raw_expression"]: item["category"] for item in classified["records"]
    }
    assert by_expression == {
        "ordinary_call": "static_candidate",
        "worker.assert_called_once": "mock_or_assertion",
    }


@pytest.mark.parametrize(
    "source_qualname",
    ["pkg.make_fixture", "pkg.register_dependency", "pkg.build_viewset"],
)
def test_framework_classification_does_not_use_the_enclosing_scope_name(
    source_qualname: str,
) -> None:
    record = _record("src/app.py", "missing_symbol", "ast_name_resolution")
    record["properties"]["source_qualname"] = source_qualname

    classified = classify_unresolved_records({"unresolved": [record]})

    assert classified["records"][0]["category"] == "static_candidate"
    assert classified["records"][0]["release_blocking"] is True


def test_unresolved_query_filters_after_classification(tmp_path: Path) -> None:
    engine = QueryEngine(_build_unresolved_fixture_index(tmp_path))

    filtered = engine.unresolved(
        category="static_candidate",
        limit=1,
    )
    blocking = engine.unresolved(
        release_blocking_only=True,
        limit=2,
    )

    assert filtered["summary"]["total"] > filtered["summary"]["returned"]
    assert filtered["summary"]["returned"] == 1
    assert all(
        item["category"] == "static_candidate" for item in filtered["unresolved"]
    )
    assert all(item["release_blocking"] is True for item in filtered["unresolved"])
    assert blocking["summary"]["returned"] == 2
    assert all(item["release_blocking"] is True for item in blocking["unresolved"])
    with pytest.raises(ValueError):
        engine.unresolved(category="not_a_category")


def test_cli_unresolved_filters_and_rejects_invalid_category(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_unresolved_fixture_index(tmp_path)

    exit_code = main(
        [
            "--output-dir",
            str(output_dir),
            "unresolved",
            "--category",
            "static_candidate",
            "--limit",
            "1",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["returned"] == 1
    assert payload["summary"]["category_filter"] == "static_candidate"
    assert payload["unresolved"][0]["category"] == "static_candidate"
    assert payload["unresolved"][0]["release_blocking"] is True

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--output-dir",
                str(output_dir),
                "unresolved",
                "--category",
                "not_a_category",
            ]
        )
    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_context_and_explain_compact_unresolved_include_triage(
    tmp_path: Path,
) -> None:
    provider = ContextProvider(QueryEngine(_build_unresolved_fixture_index(tmp_path)))

    request = ContextRequest(
        targets=["pkg.service.run"],
        detail_level="detailed",
    )
    context = provider.get_context(request)
    explain = provider.explain(request)

    context_items = context["impact"][0]["unresolved_risks"]["items"]
    explain_items = explain["explanations"][0]["unresolved"]["items"]
    assert context_items
    assert explain_items
    for item in [*context_items, *explain_items]:
        assert item["category"]
        assert item["risk_level"]
        assert "release_blocking" in item
        assert item["classification_reason"]
        assert item["suggested_next_step"]
        assert "properties" not in item

    assert context["estimated_tokens"] < 8000
    assert explain["estimated_tokens"] < 8000


def _build_unresolved_fixture_index(tmp_path: Path) -> Path:
    output_dir = tmp_path / "arcgraph-unresolved"
    node = Node(
        id="fn:pkg.service.run",
        kind="function",
        name="run",
        qualname="pkg.service.run",
        path="src/pkg/service.py",
        start_line=1,
    )
    diagnostics = [
        _diagnostic("static", "service.compute", "ast_attribute_resolution", 10),
        _diagnostic("static-2", "service.transform", "ast_attribute_resolution", 11),
        _diagnostic("type", "receiver.call", "missing_type_context", 12),
        _diagnostic("dynamic", "fallback_fn", "ast_name_resolution", 13),
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="unresolved-triage-test",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[node],
        edges=[],
        warnings=[],
        diagnostics=diagnostics,
    )
    return output_dir


def _diagnostic(
    suffix: str, raw_expression: str, failed_strategy: str, line: int
) -> SemanticDiagnostic:
    return SemanticDiagnostic(
        diagnostic_id=f"diagnostic:{suffix}",
        index_version="unresolved-triage-test",
        diagnostic_kind="unresolved_callsite",
        message=f"Unresolved Python callsite {raw_expression!r}",
        severity="info",
        path="src/pkg/service.py",
        start_line=line,
        frontend_name="python-v1-compat-shim",
        properties={
            "path": "src/pkg/service.py",
            "line": line,
            "raw_expression": raw_expression,
            "failed_strategy": failed_strategy,
            "source_scope": "fn:pkg.service.run",
        },
    )


def _record(path: str, raw_expression: str, failed_strategy: str) -> dict[str, object]:
    return {
        "path": path,
        "start_line": 1,
        "properties": {
            "path": path,
            "line": 1,
            "raw_expression": raw_expression,
            "failed_strategy": failed_strategy,
        },
    }
