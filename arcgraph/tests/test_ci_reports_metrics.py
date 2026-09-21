from __future__ import annotations


import json
import os
import shutil
from pathlib import Path

from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces import ci as ci_module
from arcgraph.interfaces.ci import run_ci_checks
from arcgraph.interfaces.cli import main
from arcgraph.interfaces.docs import render_docs
from arcgraph.interfaces.metrics import summarize_metrics
from arcgraph.interfaces.reports import (
    classify_unresolved_records,
    render_ci_markdown,
    render_metrics_dashboard_html,
    render_pr_markdown,
    render_static_html_report,
)
from arcgraph.interfaces.visual_slice import VisualSliceLimits, make_visual_slice

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def test_unresolved_classification_keeps_dynamic_and_test_boundaries_visible() -> None:
    unresolved = {
        "unresolved": [
            {
                "path": "backend/tests/unit/workers/test_keyword_scan_worker.py",
                "start_line": 40,
                "properties": {
                    "raw_expression": "worker.assert_called_once",
                    "failed_strategy": "ast_name_resolution",
                },
            },
            {
                "path": "backend/tests/unit/services/test_memory_service.py",
                "start_line": 654,
                "properties": {
                    "raw_expression": "monkeypatch.setattr",
                    "failed_strategy": "ast_name_resolution",
                },
            },
            {
                "path": "backend/src/services/llm/fallback.py",
                "start_line": 139,
                "properties": {
                    "raw_expression": "fallback_fn",
                    "failed_strategy": "dynamic_dispatch",
                },
            },
            {
                "path": "backend/src/connections/database.py",
                "start_line": 189,
                "properties": {
                    "raw_expression": "session_maker",
                    "failed_strategy": "ast_name_resolution",
                },
            },
            {
                "path": "backend/src/services/logical/types_v3.py",
                "start_line": 410,
                "properties": {
                    "raw_expression": "get_entailed_types",
                    "failed_strategy": "ast_name_resolution",
                },
            },
            {
                "path": "arcgraph/arcgraph/core/scanner.py",
                "start_line": 112,
                "properties": {
                    "raw_expression": "detector",
                    "failed_strategy": "dynamic_dispatch",
                },
            },
        ]
    }

    classified = classify_unresolved_records(unresolved)

    assert classified["category_counts"] == {
        "external_service_boundary": 1,
        "framework_magic": 0,
        "generated_or_reflection": 0,
        "missing_type_context": 0,
        "mock_or_assertion": 2,
        "static_candidate": 1,
        "true_dynamic_call": 2,
    }
    assert classified["release_blocking_count"] == 1
    assert classified["risk_counts"]["high"] == 1
    blocking = [
        item for item in classified["records"] if item["release_blocking"] is True
    ]
    assert blocking[0]["category"] == "static_candidate"
    assert "suggested_next_step" in blocking[0]


def test_ci_checks_report_similarity_warning(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    result = run_ci_checks(
        QueryEngine(output_dir),
        targets=["pkg.service.normalize_title"],
    )

    assert result["status"] == "warn"
    assert result["summary"]["fail"] == 0
    assert any(
        check["name"] == "similarity_duplicates" and check["status"] == "warn"
        for check in result["checks"]
    )
    assert result["pr_summary"]["similarity"][0]["similar"] == (
        "fn:pkg.service.normalize_label"
    )

    changed_file_result = run_ci_checks(
        QueryEngine(output_dir),
        changed_files=["src/pkg/service.py"],
    )

    assert any(
        check["name"] == "similarity_duplicates" and check["status"] == "warn"
        for check in changed_file_result["checks"]
    )


def test_static_html_and_ci_markdown_reports(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    engine = QueryEngine(output_dir)
    ci_result = run_ci_checks(engine, targets=["pkg.service.normalize_title"])
    html = render_static_html_report(
        target="pkg.service.normalize_title",
        impact=engine.impact("pkg.service.normalize_title"),
        similar=engine.similar("pkg.service.normalize_title"),
        architecture=engine.architecture(),
        ci_result=ci_result,
    )
    markdown = render_ci_markdown(ci_result)

    assert "Similarity Map" in html
    assert "fn:pkg.service.normalize_label" in html
    assert "Architecture Map" in html
    assert "Graph Slice" in html
    assert "Export Mermaid" in html
    assert "Semantic Summary" in html
    assert "Semantic Impact" in html
    assert 'data-view="semantic"' in html
    assert "VisualSlice" in html
    slice_json = html.split(
        '<script id="ArcGraph-slice-data" type="application/json">', 1
    )[1].split("</script>", 1)[0]
    graph_data = json.loads(slice_json)
    assert graph_data["views"]["semantic"]["schema"] == "VisualSlice"
    assert isinstance(graph_data["views"]["semantic"]["diagnostics"], list)
    assert isinstance(graph_data["views"]["semantic"]["capabilities"], dict)
    assert graph_data["views"]["semantic"]["truncation"]["truncated"] is False
    assert "Bindings" in html
    assert "TypeRefs" in html
    assert "# ArcGraph CI Summary" in markdown
    assert "similarity_duplicates" in markdown
    assert "Semantic Summary" in markdown
    assert "Diagnostics Lifecycle" in markdown
    assert "Bindings" in markdown
    assert "TypeRefs" in markdown
    assert "Precision Evidence" in markdown
    assert "Type precision complete: false" in markdown
    assert "Pyright status: unavailable" in markdown
    assert ci_result["semantic_summary"]["binding_summary"]["binding_total"] > 0
    assert ci_result["semantic_summary"]["type_summary"]["type_ref_total"] > 0
    assert any(
        check["name"] == "adapter_metrics_baseline" and check["status"] == "pass"
        for check in ci_result["checks"]
    )
    assert any(
        check["name"] == "semantic_resolution_trend" and check["status"] == "pass"
        for check in ci_result["checks"]
    )
    assert any(
        check["name"] == "performance_budget" and check["status"] == "pass"
        for check in ci_result["checks"]
    )
    assert any(
        check["name"] == "coverage_inputs" and check["status"] == "pass"
        for check in ci_result["checks"]
    )
    lifecycle = next(
        check
        for check in ci_result["checks"]
        if check["name"] == "diagnostics_lifecycle"
    )
    assert lifecycle["status"] == "pass"
    assert lifecycle["details"]["new_diagnostics_count"] >= 0
    release_gate = next(
        check
        for check in ci_result["checks"]
        if check["name"] == "python_v2_release_gate"
    )
    assert release_gate["status"] == "pass"
    assert release_gate["details"]["selected_v2_queries"]["bindings"] == "available"
    assert release_gate["details"]["fallback_contract"]["build_flag"] == (
        "--semantic-call-resolution"
    )
    assert (
        release_gate["details"]["fallback_contract"][
            "fallback_expires_at_schema_version"
        ]
        == "1.0.0"
    )
    assert (
        release_gate["details"]["fallback_contract"]["fallback_window_expired"] is True
    )
    assert (
        release_gate["details"]["fallback_contract"]["legacy_escape_hatch"]
        == "--legacy-call-resolution"
    )
    quality = next(
        check
        for check in ci_result["checks"]
        if check["name"] == "semantic_quality_targets"
    )
    assert quality["status"] == "warn"
    assert quality["details"]["exceptions_present"] is True
    assert "exception_budget_declared" not in quality["details"]
    assert quality["details"]["targets"]["overall_callsite_resolution_rate"] == 0.96
    assert quality["details"]["targets"]["attribute_call_resolution_rate"] == 0.93
    assert quality["details"]["targets"]["chain_call_resolution_rate"] == 0.93
    assert "overall_callsite_resolution_rate" in quality["details"]["targets"]
    assert (
        quality["details"]["current"]["known_framework_registration_metric_source"]
        == "adapter_metrics"
    )
    assert (
        quality["details"]["current"]["known_framework_registration_discovered_total"]
        > 0
    )
    assert quality["details"]["current"]["known_framework_registration_by_adapter"]
    unresolved_classification = next(
        check
        for check in ci_result["checks"]
        if check["name"] == "semantic_unresolved_classification"
    )
    assert "static_candidate" in unresolved_classification["details"]["category_counts"]
    schema_check = next(
        check
        for check in ci_result["checks"]
        if check["name"] == "schema_compatibility"
    )
    assert schema_check["details"]["stable_target_schema_version"] == "1.0.0"
    assert (
        schema_check["details"]["receiver_resolution_default_schema_version"] == "1.0.0"
    )
    assert schema_check["details"]["ready_for_stable_contract"] is True
    assert schema_check["details"]["legacy_0_6_to_stable_steps"]
    assert (
        schema_check["details"]["legacy_0_6_to_stable_direct_strategy"]["strategy"]
        == "full-rebuild"
    )

    pr_markdown = render_pr_markdown(ci_result)
    assert "# ArcGraph PR Impact Summary" in pr_markdown
    assert "Similarity" in pr_markdown
    assert "Semantic Summary" in pr_markdown
    assert "Precision Evidence" in pr_markdown
    assert "Binding diagnostics" in pr_markdown
    assert "Type diagnostics" in pr_markdown


def test_semantic_quality_scope_excludes_adversarial_fixtures(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    package = project / "pkg"
    fixture_dir = project / "fixtures"
    package.mkdir(parents=True)
    fixture_dir.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "service.py").write_text(
        """
class Worker:
    def step(self):
        return 1

    def run(self):
        self.step()
        return self.step()


def make_worker() -> Worker:
    return Worker()


def entry():
    worker = make_worker()
    worker.run()
    return worker.step()
""".lstrip(),
        encoding="utf-8",
    )
    (fixture_dir / "dynamic_case.py").write_text(
        """
def noisy(cache, bag):
    cache.get("key")
    bag.add("item")
    cache.unknown()
    return cache.missing().again()
""".lstrip(),
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        """
[tool.arcgraph]
source_roots = ["."]

[tool.arcgraph.ci.semantic_quality_targets]
overall_callsite_resolution_rate = 0.95
attribute_call_resolution_rate = 0.8
chain_call_resolution_rate = 0.5
known_framework_registration_resolution_rate = 0.95

[tool.arcgraph.ci.semantic_quality_scope]
exclude = ["fixtures/**"]
""".lstrip(),
        encoding="utf-8",
    )

    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=project,
        output_dir=output_dir,
        source_roots=[SourceRoot(".")],
    ).build()

    ci_result = run_ci_checks(QueryEngine(output_dir))
    quality = next(
        check
        for check in ci_result["checks"]
        if check["name"] == "semantic_quality_targets"
    )
    scope = quality["details"]["scope"]

    assert scope["mode"] == "configured"
    assert scope["exclude"] == ["fixtures/**"]
    assert scope["filtered_callsite_total"] < scope["source_callsite_total"]
    assert (
        ci_result["semantic_summary"]["callsite_total"]
        == scope["source_callsite_total"]
    )
    assert quality["details"]["current"]["overall_callsite_resolution_rate"] > (
        ci_result["semantic_summary"]["resolution_rate"]
    )
    assert not any(
        exception["metric"] == "overall_callsite_resolution_rate"
        for exception in quality["details"]["exceptions"]
    )


def test_precision_summary_marks_type_precision_incomplete() -> None:
    summary = ci_module._precision_summary(
        {
            "capabilities": {
                "precision": "precision_available",
                "precise_references": "available",
            },
            "precision_metrics": {
                "status": "available",
                "pyright_status": "unavailable",
                "scip_type_occurrences": 0,
                "type_occurrences": 0,
            },
        }
    )

    assert summary["type_precision_complete"] is False
    assert summary["pyright_status"] == "unavailable"
    assert summary["scip_type_occurrences"] == 0
    assert "type precision is incomplete" in ci_module._precision_message(summary)


def test_precision_summary_marks_pyright_type_precision_complete() -> None:
    summary = ci_module._precision_summary(
        {
            "capabilities": {
                "precision": "precision_available",
                "precise_references": "available",
            },
            "precision_metrics": {
                "status": "available",
                "pyright_status": "available",
                "pyright_type_info_total": 3,
                "pyright_lsp_type_info_total": 3,
                "type_occurrences": 3,
            },
        }
    )

    assert summary["type_precision_complete"] is True
    assert "type precision is complete" in ci_module._precision_message(summary)


def test_ci_reports_language_capability_tiers(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    current = QueryEngine(output_dir).current()
    ci_result = run_ci_checks(QueryEngine(output_dir))
    checks = {check["name"]: check for check in ci_result["checks"]}
    details = checks["language_capability_tiers"]["details"]

    assert checks["language_capability_tiers"]["status"] == "pass"
    assert current["capabilities"]["language_tiers"] == "available"
    assert current["language_tiers"]["python"]["tier"] == "L3"
    assert current["language_tiers"]["typescript"]["tier"] == "L3"
    assert current["language_tiers"]["javascript"]["tier"] == "L3"
    assert current["language_tiers"]["go"]["status"] == (
        "requires_explicit_scip_graph_index"
    )
    assert current["language_tiers"]["c"]["status"] == (
        "requires_explicit_scip_graph_index"
    )
    assert current["language_tiers"]["cpp"]["status"] == (
        "requires_explicit_scip_graph_index"
    )
    assert current["language_tiers"]["swift"]["status"] == (
        "requires_explicit_scip_graph_index"
    )
    assert details["by_tier"]["L2"] == 7
    assert details["by_tier"]["L3"] == 3
    assert set(details["requires_artifact"]) == {
        "c",
        "cpp",
        "csharp",
        "go",
        "java",
        "rust",
        "swift",
    }


def test_legacy_receiver_resolution_fails_stable_schema_gate(
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci_module, "SCHEMA_VERSION", "1.0.0")
    monkeypatch.setattr(
        ci_module, "V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION", "1.0.0"
    )

    release_gate = ci_module._release_gate_summary(
        {
            "capabilities": {
                "semantic_stats": "available",
                "unresolved": "available",
                "bindings": "available",
                "types": "available",
                "receiver_resolution": "legacy",
            }
        }
    )

    assert release_gate["fallback_contract"]["fallback_window_expired"] is True
    assert release_gate["exception_count"] == 1
    assert release_gate["exceptions"][0]["kind"] == "legacy_receiver_resolution_enabled"


def test_ci_python_full_requires_optional_inputs(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    ci_result = run_ci_checks(QueryEngine(output_dir), require_python_full=True)
    checks = {check["name"]: check for check in ci_result["checks"]}

    assert ci_result["status"] == "fail"
    assert ci_result["required_inputs"] == {
        "python_full": True,
        "precision": True,
        "coverage": True,
        "runtime_trace": True,
    }
    assert checks["precision_inputs"]["status"] == "fail"
    assert checks["coverage_inputs"]["status"] == "fail"
    assert checks["runtime_trace_inputs"]["status"] == "fail"


def test_ci_python_full_fails_legacy_receiver_mode(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        enable_v2_call_resolution=False,
    ).build()

    ci_result = run_ci_checks(QueryEngine(output_dir), require_python_full=True)
    checks = {check["name"]: check for check in ci_result["checks"]}

    assert ci_result["status"] == "fail"
    assert checks["python_v2_release_gate"]["status"] == "fail"
    assert checks["python_v2_release_gate"]["details"]["receiver_resolution"] == (
        "legacy"
    )


def test_ci_python_full_fails_stale_runtime_trace(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    trace_path = tmp_path / "stale-runtime-trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "trace_run": {
                    "trace_run_id": "old-run",
                    "index_version": "different-index",
                },
                "events": [
                    {
                        "kind": "call",
                        "source": "fn:pkg.api.create_memory",
                        "target": "fn:pkg.service.build_message",
                        "path": "src/pkg/api.py",
                        "line": 21,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        runtime_trace_path=trace_path,
    ).build()

    ci_result = run_ci_checks(QueryEngine(output_dir), require_python_full=True)
    checks = {check["name"]: check for check in ci_result["checks"]}

    assert ci_result["status"] == "fail"
    assert checks["runtime_trace_inputs"]["status"] == "fail"
    assert checks["runtime_trace_inputs"]["details"]["status"] == "stale"
    assert checks["runtime_trace_inputs"]["details"]["stale_trace"] is True


def test_ci_evidence_commit_consistency_allows_missing_runtime_trace(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    ci_result = run_ci_checks(QueryEngine(output_dir))
    checks = {check["name"]: check for check in ci_result["checks"]}
    details = checks["evidence_commit_consistency"]["details"]

    assert checks["evidence_commit_consistency"]["status"] == "pass"
    assert details["index_matches_head"] is True
    assert details["runtime_trace_commit_sha"] is None
    assert details["runtime_trace_matches_head"] is True
    assert details["consistent"] is True


def test_ci_python_full_fails_evidence_commit_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    monkeypatch.setattr(
        ci_module,
        "current_commit",
        lambda repo_root: "ffffffffffffffffffffffffffffffffffffffff",
    )

    normal_result = run_ci_checks(QueryEngine(output_dir))
    normal_checks = {check["name"]: check for check in normal_result["checks"]}
    ci_result = run_ci_checks(QueryEngine(output_dir), require_python_full=True)
    checks = {check["name"]: check for check in ci_result["checks"]}

    assert normal_checks["evidence_commit_consistency"]["status"] == "pass"
    assert (
        normal_checks["evidence_commit_consistency"]["details"]["index_matches_head"]
        is False
    )
    assert (
        normal_checks["evidence_commit_consistency"]["details"][
            "runtime_trace_matches_head"
        ]
        is True
    )
    assert ci_result["status"] == "fail"
    assert checks["evidence_commit_consistency"]["status"] == "fail"
    assert checks["evidence_commit_consistency"]["details"]["checked"] is True
    assert (
        checks["evidence_commit_consistency"]["details"]["index_matches_head"] is False
    )


def test_ci_checks_pass_when_target_unresolved_callsites_are_resolved(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    engine = QueryEngine(output_dir)

    ci_result = run_ci_checks(
        engine, targets=["pkg.service.MemoryService.create_memory"]
    )
    gate = next(
        check
        for check in ci_result["checks"]
        if check["name"] == "high_risk_unresolved_callsites"
    )

    assert gate["status"] == "pass"
    assert gate["details"]["unresolved_callsite_total"] == 0
    assert gate["details"]["targets"] == []
    assert ci_result["pr_summary"]["high_risk_unresolved"] == gate["details"]
    assert "high_risk_unresolved_callsites" in render_ci_markdown(ci_result)
    assert "- None" in render_pr_markdown(ci_result)


def test_ci_checks_warn_on_semantic_resolution_regression(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    ci_result = run_ci_checks(
        QueryEngine(output_dir),
        semantic_baseline={"resolution_rate": 0.99, "callsite_total": 1},
        semantic_regression_tolerance=0.0,
    )
    trend = next(
        check
        for check in ci_result["checks"]
        if check["name"] == "semantic_resolution_trend"
    )

    assert trend["status"] == "warn"
    assert trend["details"]["baseline_available"] is True
    assert trend["details"]["regressed"] is True


def test_ci_checks_warn_on_performance_budget_override(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    ci_result = run_ci_checks(
        QueryEngine(output_dir),
        performance_budgets_ms={"current": 0.0},
    )
    budget = next(
        check for check in ci_result["checks"] if check["name"] == "performance_budget"
    )

    assert budget["status"] == "warn"
    assert budget["details"]["violation_count"] >= 1
    assert budget["details"]["violations"][0]["operation"] == "current"


def test_performance_budget_scales_current_freshness_by_file_count() -> None:
    summary = ci_module._performance_budget_summary(
        {"current": 1500.0},
        budgets_ms=None,
        file_count=500,
    )

    assert summary["violation_count"] == 0
    assert summary["budgets_ms"]["current"] == 9500.0
    assert summary["budget_model"]["current"]["file_count"] == 500


def test_static_html_report_renders_entrypoint_flow(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    engine = QueryEngine(output_dir)
    html = render_static_html_report(
        target="route:POST:/memories",
        impact=engine.impact("route:POST:/memories"),
        similar=engine.similar("route:POST:/memories"),
        architecture=engine.architecture(),
        entrypoint_flow=engine.entrypoint_flow("route:POST:/memories"),
    )

    assert "Entrypoint Flow" in html
    assert "fn:pkg.api.create_memory" in html
    assert "Affected Entrypoints" not in html

    mcp_html = render_static_html_report(
        target="mcp:store_memory",
        impact=engine.impact("mcp_tool:store_memory"),
        similar=engine.similar("mcp_tool:store_memory"),
        architecture=engine.architecture(),
        entrypoint_flow=engine.entrypoint_flow("mcp:store_memory"),
    )
    worker_html = render_static_html_report(
        target="worker:arq:embedding:generate_embedding_task",
        impact=engine.impact("worker:arq:embedding:generate_embedding_task"),
        similar=engine.similar("worker:arq:embedding:generate_embedding_task"),
        architecture=engine.architecture(),
        entrypoint_flow=engine.entrypoint_flow(
            "worker:arq:embedding:generate_embedding_task"
        ),
    )

    assert "fn:pkg.mcp.store_memory_tool" in mcp_html
    assert "fn:pkg.worker.generate_embedding_task" in worker_html


def test_static_html_report_escapes_script_payloads() -> None:
    malicious = '</script><script>alert("x")</script><!--hidden-->'
    edge = {
        "source": f"fn:source{malicious}",
        "target": "fn:target",
        "kind": "calls",
        "confidence": "confirmed",
    }
    impact = {
        "query": malicious,
        "status": "available",
        "freshness": {"status": "fresh"},
        "resolved_targets": [edge["target"]],
        "call_impact": {"affected_symbols": [], "edges": [edge]},
        "import_impact": {"affected_modules": [], "edges": []},
        "entrypoint_impact": {"entrypoints": [], "edges": []},
        "resource_impact": {"resources": [], "edges": []},
        "test_candidates": [],
        "test_gaps": [],
        "coverage": {"status": "unavailable", "covered_targets": []},
        "confidence_summary": {"confirmed_edges": 1},
        "confirmed_impact": [edge],
        "inferred_impact": [],
        "runtime_impact": [],
        "heuristic_impact": [],
        "unresolved_impact": [],
        "unresolved_risks": {"items": []},
    }

    html = render_static_html_report(
        target=malicious,
        impact=impact,
        similar={"similar": []},
        architecture={"import_cycles": [], "node_kinds": {}, "edge_kinds": {}},
    )

    assert "</script><script>" not in html
    assert "<!--hidden-->" not in html
    assert "\\u003C/script>" in html
    assert "\\u003C!--hidden-->" in html
    assert "&lt;/script&gt;" in html


def test_visual_slice_contract_truncates_with_stable_metadata() -> None:
    nodes = [{"id": f"node:{index}", "kind": "symbol"} for index in range(3)]
    edges = [
        {
            "source": "node:0",
            "target": f"node:{index}",
            "kind": "calls",
            "semantic_role": str(index),
        }
        for index in range(3)
    ]
    diagnostics = [{"diagnostic_id": f"d:{index}"} for index in range(3)]

    payload = make_visual_slice(
        nodes,
        edges,
        diagnostics=diagnostics,
        summary={"confirmed_edges": 3},
        capabilities={"impact": "available"},
        limits=VisualSliceLimits(max_nodes=2, max_edges=1, max_diagnostics=1),
    )

    assert payload["schema"] == "VisualSlice"
    assert payload["version"] == 1
    assert payload["summary"] == {"confirmed_edges": 3}
    assert payload["capabilities"] == {"impact": "available"}
    assert len(payload["nodes"]) == 2
    assert len(payload["edges"]) == 1
    assert len(payload["diagnostics"]) == 1
    assert payload["truncation"] == {
        "truncated": True,
        "nodes_dropped": 1,
        "edges_dropped": 2,
        "diagnostics_dropped": 2,
        "limits": {
            "max_nodes": 2,
            "max_edges": 1,
            "max_diagnostics": 1,
        },
    }


def test_cli_metrics_and_html_report(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    metrics_log = tmp_path / "metrics" / "arcgraph.jsonl"
    html_path = tmp_path / "reports" / "arcgraph.html"
    mcp_html_path = tmp_path / "reports" / "mcp.html"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "--metrics-log",
            str(metrics_log),
            "report",
            "html",
            "pkg.service.normalize_title",
            "--output",
            str(html_path),
        ]
    )

    assert exit_code == 0
    assert "Similarity Map" in html_path.read_text(encoding="utf-8")
    mcp_exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "report",
            "html",
            "mcp:store_memory",
            "--output",
            str(mcp_html_path),
        ]
    )
    assert mcp_exit_code == 0
    mcp_html = mcp_html_path.read_text(encoding="utf-8")
    assert "fn:pkg.mcp.store_memory_tool" in mcp_html
    assert "No graph node resolved" not in mcp_html
    events = [
        json.loads(line)
        for line in metrics_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[0]["event"] == "arcgraph_cli_command"
    assert events[0]["command"] == "report"
    assert events[0]["status"] == "success"

    summary_exit_code = main(["metrics", str(metrics_log)])
    assert summary_exit_code == 0
    metrics = summarize_metrics(metrics_log)
    assert metrics["duration_by_command_ms"]["report"]["count"] == 1
    assert metrics["duration_by_category_ms"]["report"]["count"] == 1

    dashboard = render_metrics_dashboard_html(metrics)
    assert "ArcGraph Metrics Dashboard" in dashboard
    assert "Query Latency" in dashboard
    assert "Build Latency" in dashboard
    assert "Performance Budget" in dashboard
    assert str(metrics_log) not in dashboard
    assert str(metrics["first_timestamp"]) not in dashboard
    assert str(metrics["last_timestamp"]) not in dashboard
    assert "Input path and event timestamps omitted" in dashboard
    bounded_dashboard = render_metrics_dashboard_html(
        {
            **metrics,
            "future_summary_field": "SECRET_UNREVIEWED_SUMMARY_FIELD",
            "warnings": ["/private/SECRET_WARNING_PATH/metrics.jsonl"],
        }
    )
    assert "SECRET_UNREVIEWED_SUMMARY_FIELD" not in bounded_dashboard
    assert "SECRET_WARNING_PATH" not in bounded_dashboard
    assert "warning_count" in bounded_dashboard


def test_metrics_summary_reports_performance_budget_violations(
    tmp_path: Path,
) -> None:
    metrics_log = tmp_path / "metrics" / "arcgraph.jsonl"
    metrics_log.parent.mkdir(parents=True, mode=0o700)
    if os.name == "posix":
        metrics_log.parent.chmod(0o700)
    events = [
        {
            "timestamp": "2026-05-14T00:00:00+00:00",
            "event": "arcgraph_cli_command",
            "command": "current",
            "status": "success",
            "duration_ms": 750.0,
            "exit_code": 0,
        },
        {
            "timestamp": "2026-05-14T00:00:01+00:00",
            "event": "arcgraph_cli_command",
            "command": "symbol",
            "status": "success",
            "duration_ms": 250.0,
            "exit_code": 0,
        },
    ]
    metrics_log.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events),
        encoding="utf-8",
    )
    metrics_log.chmod(0o600)

    metrics = summarize_metrics(metrics_log)
    budget = metrics["performance_budget"]
    dashboard = render_metrics_dashboard_html(metrics)

    assert metrics["duration_by_command_ms"]["current"]["p95"] == 750.0
    assert budget["status"] == "warn"
    assert budget["violation_count"] == 1
    assert budget["violations"][0]["command"] == "current"
    assert "Performance Budget" in dashboard
    assert "current" in dashboard


def test_cli_docs_reference_topics() -> None:
    assert main(["docs", "cli-reference"]) == 0
    capabilities = main(["docs", "capabilities", "--json"])
    troubleshooting = main(["docs", "troubleshooting"])
    migration = main(["docs", "migration-notes"])
    visualization = main(["docs", "visualization"])
    limitations = main(["docs", "limitations"])
    evidence_cookbook = main(["docs", "evidence-cookbook"])
    agent_cli_contract = main(["docs", "agent-cli-contract"])
    mcp_server = main(["docs", "mcp-server"])
    private_alpha_smoke = main(["docs", "private-alpha-smoke"])
    package_readiness = main(["docs", "package-readiness"])

    assert capabilities == 0
    assert troubleshooting == 0
    assert migration == 0
    assert visualization == 0
    assert limitations == 0
    assert evidence_cookbook == 0
    assert agent_cli_contract == 0
    assert mcp_server == 0
    assert private_alpha_smoke == 0
    assert package_readiness == 0

    cli_reference = render_docs("cli-reference")
    capability_docs = render_docs("capabilities")
    quickstart_docs = render_docs("quickstart")
    troubleshooting_docs = render_docs("troubleshooting")
    viz_docs = render_docs("visualization")
    limitations_docs = render_docs("limitations")
    evidence_docs = render_docs("evidence-cookbook")
    agent_cli_docs = render_docs("agent-cli-contract")
    mcp_server_docs = render_docs("mcp-server")
    smoke_docs = render_docs("private-alpha-smoke")
    package_docs = render_docs("package-readiness")

    assert "source checkout" in cli_reference
    assert "Public PyPI, npm, Docker/GHCR" in quickstart_docs
    assert "arcgraph doctor" in quickstart_docs
    assert "arcgraph init --dry-run" in quickstart_docs
    assert "arcgraph status" in quickstart_docs
    assert "arcgraph.pipeline.indexer.ArcGraphIndexer" in quickstart_docs
    assert "output/arcgraph" in quickstart_docs
    assert "Python 3.11 or 3.12" in troubleshooting_docs
    assert "npm package publishing" in troubleshooting_docs
    assert "arcgraph context TARGET..." in cli_reference
    assert "arcgraph explain TARGET..." in cli_reference
    assert "arcgraph callers TARGET" in cli_reference
    assert "arcgraph callees TARGET" in cli_reference
    assert "arcgraph impact TARGET" in cli_reference
    # The unbounded escape hatch must stay documented next to the commands it
    # applies to, so nobody rediscovers the 10 MB payload by accident.
    assert "`--raw` returns the unbounded debug payload" in cli_reference
    assert "arcgraph_explain" in cli_reference
    assert "arcgraph_get_why" in cli_reference
    assert "arcgraph evidence status" in cli_reference
    assert "arcgraph evidence plan" in cli_reference
    assert "arcgraph benchmark agent-startup" in cli_reference
    assert "arcgraph benchmark suite" in cli_reference
    assert "--profile default|python_full" in cli_reference
    assert "evidence/manifest.json" in cli_reference
    assert "context_limit" in cli_reference
    stale_python_baseline = "392" + "698b"
    assert stale_python_baseline not in cli_reference
    assert "Python V2 Release Baseline" not in cli_reference
    assert "second-phase closeout baseline" not in cli_reference
    assert "docs/language-support.md" in cli_reference
    assert "Python is the guaranteed native L3 frontend" in cli_reference
    assert (
        "TypeScript/JavaScript is L3 when a TypeScript compiler API is resolvable"
        in cli_reference
    )
    assert "outside that trial's acceptance scope" in cli_reference
    assert "validated external semantic extractor payloads" in cli_reference
    assert "--openapi-spec" in cli_reference
    assert "--scip-graph-index" in cli_reference
    assert "No language is claimed as L4" in cli_reference
    assert "do not prove full runtime coverage" in cli_reference
    assert "arcgraph mcp serve --repo-root . --output-dir output/arcgraph" in (
        cli_reference
    )
    assert "platform P2 roadmap" in capability_docs
    assert "Frontend Plugin Contract" in capability_docs
    assert "FrontendGraphFragment" in capability_docs
    assert "arcgraph visual force" in viz_docs
    assert "arcgraph visual workbench" in viz_docs
    assert "ArcGraph Explorer" in viz_docs
    assert "speculative" not in viz_docs
    assert "Architecture Overview" in viz_docs
    assert "Entry→Resource" in viz_docs or "Entry" in viz_docs
    assert "not a compiler" in limitations_docs
    assert "TypeScript" in limitations_docs
    assert "SCIP" in limitations_docs
    assert "persisted module identities" in limitations_docs
    assert "declaration context" in limitations_docs
    assert "deterministically mapped direct `calls`" in limitations_docs
    assert "complete import topology" in limitations_docs
    assert "without a warning" not in limitations_docs
    assert "Cross-file Express mount relationships" in limitations_docs
    assert "module identity" in limitations_docs
    assert "similarity_bucket_approximated" in limitations_docs
    assert "similarity_edges_capped" in limitations_docs
    assert "runtime trace" in limitations_docs
    assert "OpenTelemetry" in limitations_docs
    assert "browser coverage" in limitations_docs
    assert "Workspace" in limitations_docs
    assert "workbench" in limitations_docs
    assert "local-machine performance baselines" in limitations_docs
    assert "does not overwrite static confidence" in limitations_docs
    assert "worker threads are best-effort" in limitations_docs
    assert "scip-python" in evidence_docs
    assert "coverage.xml" in evidence_docs
    assert "browser coverage" in evidence_docs
    assert "trace run" in evidence_docs
    assert "trace import-otel" in evidence_docs
    assert "trace import-har" in evidence_docs
    assert "resolution.strategy=runtime_trace" in evidence_docs
    assert "--scip-graph-index" in evidence_docs
    assert "full program correctness" not in evidence_docs
    assert "local subprocess" in agent_cli_docs
    assert "JSON to stdout by default" in agent_cli_docs
    assert "Global `--human`" in agent_cli_docs
    assert "schema_version" in agent_cli_docs
    assert "freshness" in agent_cli_docs
    assert "warnings" in agent_cli_docs
    assert "truncation" in agent_cli_docs
    assert "Avoid `--include-source` by default" in agent_cli_docs
    assert "commonly return exit code 2" in agent_cli_docs
    assert "`arcgraph ci` can return nonzero" in agent_cli_docs
    assert "docs/examples/mcp_readonly_host.py" in agent_cli_docs
    assert "installed-wheel local stdio server" in agent_cli_docs
    assert "public package publication remains deferred" in agent_cli_docs
    assert "arcgraph mcp serve --repo-root . --output-dir output/arcgraph" in (
        mcp_server_docs
    )
    assert "stdio is the only supported transport" in mcp_server_docs
    assert "`arcgraph_record_learning` is proposal-only" in mcp_server_docs
    assert "does not auto-modify Claude" in mcp_server_docs
    assert "source checkout" in smoke_docs
    assert "arcgraph init --dry-run" in smoke_docs
    assert "arcgraph build" in smoke_docs
    assert "arcgraph current" in smoke_docs
    assert "arcgraph context arcgraph.pipeline.indexer.ArcGraphIndexer" in smoke_docs
    assert "arcgraph ci" in smoke_docs
    assert "python scripts/arcgraph_private_alpha_smoke.py" in smoke_docs
    assert "arcgraph mcp serve --help" in smoke_docs
    assert "PyPI publishing remains unapproved" in smoke_docs
    assert "npm package publishing remains private/dev-only" in smoke_docs
    assert "Docker/GHCR publishing remains unapproved" in smoke_docs
    assert "arcgraph_package_readiness_smoke.py" in package_docs
    assert "This does not publish PyPI" in package_docs
    assert "This does not approve package publishing" in package_docs
    assert "npm package publishing" in package_docs


def test_cli_pr_report_and_metrics_dashboard(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    pr_path = tmp_path / "reports" / "pr.md"
    unresolved_report_path = tmp_path / "reports" / "unresolved.md"
    unresolved_json_path = tmp_path / "reports" / "unresolved.json"
    metrics_log = tmp_path / "metrics" / "arcgraph.jsonl"
    metrics_html = tmp_path / "reports" / "metrics.html"
    semantic_metrics_path = tmp_path / "metrics" / "semantic-stats.json"
    metrics_log.parent.mkdir(parents=True, mode=0o700)
    if os.name == "posix":
        metrics_log.parent.chmod(0o700)
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    pr_exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "report",
            "pr",
            "--changed-file",
            "src/pkg/service.py",
            "--output",
            str(pr_path),
        ]
    )
    metrics_exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "--metrics-log",
            str(metrics_log),
            "semantic-stats",
            "--output",
            str(semantic_metrics_path),
        ]
    )
    unresolved_exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "unresolved",
            "src/pkg/service.py",
        ]
    )
    unresolved_report_exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "report",
            "unresolved",
            "src/pkg/service.py",
            "--output",
            str(unresolved_report_path),
            "--json-output",
            str(unresolved_json_path),
        ]
    )
    dashboard_exit_code = main(
        [
            "report",
            "metrics-html",
            str(metrics_log),
            "--output",
            str(metrics_html),
        ]
    )

    assert pr_exit_code == 0
    assert "Changed files" in pr_path.read_text(encoding="utf-8")
    assert "src/pkg/service.py" in pr_path.read_text(encoding="utf-8")
    assert "Semantic Summary" in pr_path.read_text(encoding="utf-8")
    assert metrics_exit_code == 0
    assert unresolved_exit_code == 0
    assert unresolved_report_exit_code == 0
    unresolved_report = unresolved_report_path.read_text(encoding="utf-8")
    assert "ArcGraph Unresolved Classification" in unresolved_report
    assert "static_candidate" in unresolved_report
    assert "Release-blocking records" in unresolved_report
    unresolved_json = json.loads(unresolved_json_path.read_text(encoding="utf-8"))
    assert "category_counts" in unresolved_json
    assert "static_candidate" in unresolved_json["category_counts"]
    assert "release_blocking_count" in unresolved_json
    assert semantic_metrics_path.exists()
    semantic_metrics = json.loads(semantic_metrics_path.read_text(encoding="utf-8"))
    assert semantic_metrics["metrics"]["unresolved_callsite_total"] > 0
    assert semantic_metrics["binding_summary"]["binding_total"] > 0
    assert semantic_metrics["type_summary"]["type_ref_total"] > 0
    metrics = summarize_metrics(metrics_log)
    assert metrics["semantic_summary"]["callsite_total"] > 0
    assert dashboard_exit_code == 0
    metrics_dashboard = metrics_html.read_text(encoding="utf-8")
    assert "ArcGraph Metrics Dashboard" in metrics_dashboard
    assert "Semantic Metrics" in metrics_dashboard
    assert str(metrics_log) not in metrics_dashboard
    assert str(metrics["first_timestamp"]) not in metrics_dashboard
    assert str(metrics["last_timestamp"]) not in metrics_dashboard


def test_cli_ci_fail_on_warnings(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "ci",
            "--target",
            "pkg.service.normalize_title",
            "--fail-on-warnings",
        ]
    )

    assert exit_code == 1


def test_cli_evidence_plan_doctor_current_and_ci_snapshot(
    tmp_path: Path, capsys
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    current_exit = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "current",
        ]
    )
    current_payload = json.loads(capsys.readouterr().out)
    plan_exit = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "evidence",
            "plan",
        ]
    )
    plan_payload = json.loads(capsys.readouterr().out)
    doctor_exit = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "doctor",
        ]
    )
    doctor_payload = json.loads(capsys.readouterr().out)
    ci = run_ci_checks(QueryEngine(output_dir))

    assert current_exit == 0
    assert current_payload["evidence_manifest"]["summary"]["total"] == 4
    assert plan_exit == 0
    assert plan_payload["live_manifest_exists"] is True
    assert {action["kind"] for action in plan_payload["actions"]} == {
        "precision_scip",
        "precision_pyright",
        "coverage",
        "runtime_trace",
    }
    assert doctor_exit == 0
    assert any(check["name"] == "evidence_health" for check in doctor_payload["checks"])
    evidence_check = next(
        check for check in ci["checks"] if check["name"] == "evidence_manifest"
    )
    assert evidence_check["status"] == "pass"


def test_cli_ci_can_explicitly_import_runtime_trace(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    trace_path = tmp_path / "runtime-trace.json"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    trace_path.write_text(
        json.dumps(
            {
                "trace_run": {
                    "trace_run_id": "ci-run-1",
                    "source": "pytest",
                    "frontend_version": "test",
                },
                "events": [
                    {
                        "kind": "dynamic_call",
                        "source": "fn:pkg.api.create_memory",
                        "target": "fn:pkg.service.build_message",
                        "path": "src/pkg/api.py",
                        "line": 21,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "ci",
            "--runtime-trace",
            str(trace_path),
            "--runtime-trace-max-seconds",
            "120",
        ]
    )
    current = QueryEngine(output_dir).current()

    assert exit_code == 0
    assert current["capabilities"]["runtime_trace"] == "available"
    assert current["runtime_metrics"]["trace_run_id"] == "ci-run-1"
    assert current["evidence_manifest"]["summary"]["total"] == 4


def test_cli_ops_rebuild_if_stale_and_stale_alert(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"

    first_exit = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "ops",
            "rebuild",
            "--if-stale",
            "--root",
            "src",
            "--root",
            "tests",
        ]
    )
    second_exit = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "ops",
            "rebuild",
            "--if-stale",
            "--root",
            "src",
            "--root",
            "tests",
        ]
    )
    stale_exit = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "ops",
            "stale-alert",
            "--fail-on-stale",
        ]
    )

    assert first_exit == 0
    assert second_exit == 0
    assert stale_exit == 0


def test_ci_checks_read_the_resolution_baseline_from_pyproject(
    tmp_path: Path,
) -> None:
    """The trend check ratchets without needing an explicit baseline argument.

    Without this fallback the check has no baseline in any ordinary run: it
    reports the current numbers, reports `baseline_available: false`, and can
    never find a regression. Keeping the value beside the quality targets makes
    the gate apply locally and in CI with no extra wiring.
    """

    repo_root = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    (repo_root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.arcgraph.ci.semantic_resolution_baseline]",
                "resolution_rate = 0.99",
                "callsite_total = 1",
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    ci_result = run_ci_checks(
        QueryEngine(output_dir),
        semantic_regression_tolerance=0.0,
    )
    trend = next(
        check
        for check in ci_result["checks"]
        if check["name"] == "semantic_resolution_trend"
    )

    assert trend["details"]["baseline_available"] is True
    assert trend["details"]["baseline_resolution_rate"] == 0.99
    assert trend["details"]["regressed"] is True
    assert trend["status"] == "warn"


def test_ci_checks_keep_an_explicit_baseline_over_the_configured_one(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    (repo_root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.arcgraph.ci.semantic_resolution_baseline]",
                "resolution_rate = 0.99",
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    ci_result = run_ci_checks(
        QueryEngine(output_dir),
        semantic_baseline={"resolution_rate": 0.1},
        semantic_regression_tolerance=0.0,
    )
    trend = next(
        check
        for check in ci_result["checks"]
        if check["name"] == "semantic_resolution_trend"
    )

    assert trend["details"]["baseline_resolution_rate"] == 0.1
    assert trend["details"]["regressed"] is False


def _layers_toml(entries: list[tuple[str, list[str]]]) -> str:
    lines: list[str] = []
    for name, paths in entries:
        rendered = ", ".join(f'"{path}"' for path in paths)
        lines.append("[[tool.arcgraph.ci.layers]]")
        lines.append(f'name = "{name}"')
        lines.append(f"paths = [{rendered}]")
    return "\n".join(lines)


def test_layer_check_uses_the_configured_layer_model(tmp_path: Path) -> None:
    """A repository states its own layers instead of inheriting a web layout."""

    repo_root = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    # Innermost first, deliberately the reverse of the built-in model, so any
    # violation reported here can only come from the configured order.
    (repo_root / "pyproject.toml").write_text(
        _layers_toml(
            [
                ("api", ["src/pkg/api.py"]),
                ("service", ["src/pkg/service.py"]),
                ("repository", ["src/pkg/repository.py"]),
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    check = next(
        item
        for item in run_ci_checks(QueryEngine(output_dir))["checks"]
        if item["name"] == "layer_violations"
    )

    assert check["details"]["model_source"] == "configured"
    assert check["details"]["applicable"] is True
    assert check["status"] == "warn"
    assert any(
        violation["source_layer"] == "api" and violation["target_layer"] == "service"
        for violation in check["details"]["violations"]
    )


def test_layer_check_reports_that_no_layer_model_applied(tmp_path: Path) -> None:
    """Nothing inspected must not be reported as nothing wrong.

    The built-in model names a conventional web layering. A repository laid out
    differently places no file in any layer, and the check used to report that
    no violations were detected -- absence of evidence rendered as evidence of
    absence, inside a release gate that requires zero warnings.
    """

    repo_root = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    (repo_root / "pyproject.toml").write_text(
        _layers_toml([("core", ["nowhere/core/"]), ("edge", ["nowhere/edge/"])]),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    check = next(
        item
        for item in run_ci_checks(QueryEngine(output_dir))["checks"]
        if item["name"] == "layer_violations"
    )

    assert check["details"]["applicable"] is False
    assert check["details"]["classified_edges"] == 0
    assert check["details"]["violations"] == []
    assert "no layer model applies" in check["message"].lower()
    assert "inspected nothing" in check["message"]
    # Still not a failure: a repository is free to have no layer model.
    assert check["status"] == "pass"


def test_layer_check_reports_exemptions_instead_of_hiding_them(
    tmp_path: Path,
) -> None:
    """An accepted inward import stays visible and counted.

    Deleting the edge or relocating the symbol until the import graph stops
    showing it would remove the report without removing the coupling, so an
    exemption is a named pair carrying the reason it is accepted.
    """

    repo_root = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    (repo_root / "pyproject.toml").write_text(
        "\n".join(
            [
                _layers_toml(
                    [
                        ("api", ["src/pkg/api.py"]),
                        ("service", ["src/pkg/service.py"]),
                    ]
                ),
                "[[tool.arcgraph.ci.layer_exemptions]]",
                'from = "api"',
                'to = "service"',
                'reason = "the fixture wires its router to the service directly"',
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    check = next(
        item
        for item in run_ci_checks(QueryEngine(output_dir))["checks"]
        if item["name"] == "layer_violations"
    )

    assert check["status"] == "pass"
    assert check["details"]["violations"] == []
    assert check["details"]["exempted_count"] >= 1
    assert all(
        entry["reason"] == "the fixture wires its router to the service directly"
        for entry in check["details"]["exempted"]
    )


def test_layer_check_requires_a_reason_for_an_exemption(tmp_path: Path) -> None:
    """An exemption without a stated reason does not exempt anything."""

    repo_root = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    (repo_root / "pyproject.toml").write_text(
        "\n".join(
            [
                _layers_toml(
                    [
                        ("api", ["src/pkg/api.py"]),
                        ("service", ["src/pkg/service.py"]),
                    ]
                ),
                "[[tool.arcgraph.ci.layer_exemptions]]",
                'from = "api"',
                'to = "service"',
                'reason = "   "',
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    check = next(
        item
        for item in run_ci_checks(QueryEngine(output_dir))["checks"]
        if item["name"] == "layer_violations"
    )

    assert check["status"] == "warn"
    assert check["details"]["exempted_count"] == 0
    assert check["details"]["violations"]


def test_change_scoped_checks_say_when_no_change_scope_was_given(
    tmp_path: Path,
) -> None:
    """A pass without a change scope must not read as a change reviewed.

    Both checks answer questions about a change: does it have test evidence,
    does it touch unresolved callsites. Run without `--target` or
    `--changed-file` they examined nothing, yet reported that high-risk changed
    targets have test guidance and that no high-risk unresolved callsites were
    detected.
    """

    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    unscoped = {
        check["name"]: check
        for check in run_ci_checks(QueryEngine(output_dir))["checks"]
    }
    scoped = {
        check["name"]: check
        for check in run_ci_checks(
            QueryEngine(output_dir), targets=["pkg.service.build_message"]
        )["checks"]
    }

    changed = {
        check["name"]: check
        for check in run_ci_checks(
            QueryEngine(output_dir), changed_files=["src/pkg/service.py"]
        )["checks"]
    }

    for name in ("high_risk_changes_without_tests", "high_risk_unresolved_callsites"):
        assert unscoped[name]["details"]["scoped"] is False
        assert unscoped[name]["details"]["target_count"] == 0
        assert "no change scope was given" in unscoped[name]["message"].lower()
        # Not a failure: running without a change scope is legitimate.
        assert unscoped[name]["status"] == "pass"

        assert scoped[name]["details"]["scoped"] is True
        assert scoped[name]["details"]["target_count"] == 1
        assert "no change scope was given" not in scoped[name]["message"].lower()

        # A changed file names the change just as a target does.
        assert changed[name]["details"]["scoped"] is True
        assert "no change scope was given" not in changed[name]["message"].lower()
