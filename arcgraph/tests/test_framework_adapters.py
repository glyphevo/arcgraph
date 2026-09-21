from __future__ import annotations

import ast
from pathlib import Path

from arcgraph.adapters.common import AdapterAnalysis, AdapterRegistry, SemanticAdapter
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import BuildWarning, FileRecord, Node
from arcgraph.interfaces.ci import run_ci_checks

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
V2_4_DSL_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "v2_4_dsl_project"


class _FailingAdapter(SemanticAdapter):
    name = "failing_test_adapter"

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        del files, parsed_files, nodes, module_names
        raise RuntimeError("adapter boom")


class _MarkerAdapter(SemanticAdapter):
    name = "marker_test_adapter"

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        del files, parsed_files, nodes, module_names
        return AdapterAnalysis(
            nodes=[
                Node(
                    id="adapter:marker",
                    kind="adapter_marker",
                    name="marker",
                    qualname="marker",
                )
            ],
            metrics={
                "discovered_registrations": 1,
                "resolved_handlers": 1,
                "unresolved_registrations": 0,
            },
        )


class _WarningOnlyAdapter(SemanticAdapter):
    name = "warning_only_test_adapter"

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        del files, parsed_files, nodes, module_names
        return AdapterAnalysis(
            warnings=[
                BuildWarning(
                    kind="adapter_quality_warning",
                    message="quality warning, not unresolved registration",
                )
            ],
            metrics={
                "discovered_registrations": 0,
                "resolved_handlers": 0,
            },
        )


def test_phase4_framework_adapters_emit_expected_facts(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    store = GraphStoreReader.from_current(output_dir)
    nodes = {node.id: node for node in store.read_nodes()}
    edges = {(edge.source, edge.target, edge.kind): edge for edge in store.read_edges()}

    assert nodes["route:POST:/memories"].kind == "route"
    assert nodes["mcp_tool:store_memory"].kind == "mcp_tool"
    assert nodes["worker:arq:embedding:generate_embedding_task"].kind == "worker_task"
    assert nodes["queue:arq:embedding"].kind == "queue"
    assert nodes["table:memories"].kind == "table"

    expected_edges = {
        ("route:POST:/memories", "fn:pkg.api.create_memory", "invokes"),
        ("route:POST:/memories", "fn:pkg.api.get_memory_service", "injects"),
        ("mcp_tool:store_memory", "fn:pkg.mcp.store_memory_tool", "invokes"),
        (
            "worker:arq:embedding:generate_embedding_task",
            "fn:pkg.worker.generate_embedding_task",
            "invokes",
        ),
        (
            "fn:pkg.worker.schedule_embedding",
            "worker:arq:embedding:generate_embedding_task",
            "enqueues",
        ),
        (
            "method:pkg.container.ServiceContainer.get_memory_service",
            "class:pkg.service.MemoryService",
            "provides",
        ),
        (
            "fn:pkg.api.create_memory",
            "method:pkg.service.MemoryService.create_memory",
            "calls",
        ),
        ("class:pkg.model.Memory", "table:memories", "maps_to"),
        ("method:pkg.repository.MemoryRepository.create", "table:memories", "writes"),
        ("method:pkg.repository.MemoryRepository.get", "table:memories", "reads"),
    }
    assert expected_edges <= set(edges)
    assert (
        edges[
            (
                "fn:pkg.api.create_memory",
                "method:pkg.service.MemoryService.create_memory",
                "calls",
            )
        ].confidence
        == "confirmed"
    )


def test_adapter_registry_isolates_failures_and_keeps_later_adapters() -> None:
    registry = AdapterRegistry([_FailingAdapter(), _MarkerAdapter()])

    analysis = registry.analyze([], {}, [], set())

    assert [node.id for node in analysis.nodes] == ["adapter:marker"]
    assert analysis.warnings[0].kind == "adapter_error"
    assert "failing_test_adapter" in analysis.warnings[0].message
    assert analysis.metrics["adapters"]["failing_test_adapter"]["status"] == "error"
    assert analysis.metrics["adapters"]["marker_test_adapter"]["status"] == "available"
    assert (
        analysis.metrics["adapters"]["marker_test_adapter"]["discovered_registrations"]
        == 1
    )


def test_adapter_registry_warning_fallback_does_not_count_unresolved() -> None:
    registry = AdapterRegistry([_WarningOnlyAdapter()])

    analysis = registry.analyze([], {}, [], set())
    metrics = analysis.metrics["adapters"]["warning_only_test_adapter"]

    assert metrics["warnings"] == 1
    assert metrics["unresolved_registrations"] == 0


def test_indexer_uses_registry_and_ci_reports_adapter_diagnostics(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "src" / "pkg"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    output_dir = tmp_path / "arcgraph"

    ArcGraphIndexer(
        repo_root=project,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
        adapter_registry=AdapterRegistry([_FailingAdapter(), _MarkerAdapter()]),
    ).build()

    store = GraphStoreReader.from_current(output_dir)
    nodes = {node.id: node for node in store.read_nodes()}
    warnings = store.read_warnings()
    ci_result = run_ci_checks(QueryEngine(output_dir))
    adapter_check = next(
        check for check in ci_result["checks"] if check["name"] == "adapter_diagnostics"
    )

    assert nodes["adapter:marker"].kind == "adapter_marker"
    assert any(warning.kind == "adapter_error" for warning in warnings)
    assert store.metadata["capabilities"]["framework_adapters"] == "partial"
    assert adapter_check["status"] == "fail"
    assert adapter_check["details"]["warnings_count"] == 1
    assert adapter_check["details"]["errors_count"] == 1


def test_v2_4_dsl_adapters_emit_cli_test_logging_and_config_facts(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"

    ArcGraphIndexer(
        repo_root=V2_4_DSL_FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    store = GraphStoreReader.from_current(output_dir)
    engine = QueryEngine(output_dir)
    nodes = {node.id: node for node in store.read_nodes()}
    edges = {(edge.source, edge.target, edge.kind): edge for edge in store.read_edges()}
    architecture = engine.architecture()
    cli_flow = engine.entrypoint_flow("cli:sync")
    adapter_metrics = engine.current()["adapter_metrics"]["adapters"]

    assert nodes["log:pkg.logging_demo"].kind == "log_sink"
    assert nodes["log:pkg.audit"].kind == "log_sink"
    assert nodes["log:pkg.local"].kind == "log_sink"
    assert nodes["schema:pkg.config.MemoryPayload"].kind == "pydantic_model"
    assert nodes["field:pkg.config.MemoryPayload.content"].kind == "model_field"
    assert nodes["config:env:APP_LOG_LEVEL"].kind == "config"
    assert nodes["cli:sync"].kind == "cli_command"
    assert nodes["cli:sync:option:limit"].kind == "cli_option"
    assert nodes["fixture:tests.service_cases.settings"].kind == "pytest_fixture"
    assert nodes["test:tests.service_cases.test_settings"].kind == "test_case"
    assert nodes["test:tests.service_cases.test_missing_fixture"].kind == "test_case"

    assert (
        "fn:pkg.logging_demo.record_event",
        "log:pkg.logging_demo",
        "logs",
    ) in edges
    assert ("class:pkg.logging_demo.AuditService", "log:pkg.audit", "logs") in edges
    assert (
        "method:pkg.logging_demo.AuditService.emit",
        "log:pkg.audit",
        "logs",
    ) in edges
    assert ("fn:pkg.logging_demo.record_local_event", "log:pkg.local", "logs") in edges
    assert (
        "class:pkg.config.MemoryPayload",
        "schema:pkg.config.MemoryPayload",
        "declares",
    ) in edges
    assert (
        "class:pkg.config.Settings",
        "config:env:APP_LOG_LEVEL",
        "configures",
    ) in edges
    assert ("cli:sync", "fn:pkg.cli.sync", "invokes") in edges
    assert ("cli:sync", "cli:sync:option:limit", "declares") in edges
    assert (
        "test:tests.service_cases.test_settings",
        "fixture:tests.service_cases.settings",
        "injects",
    ) in edges
    assert architecture["entrypoints"]["cli_command"] == 1
    assert "test_case" not in architecture["entrypoints"]
    assert architecture["resources"]["config"] == 1
    assert architecture["resources"]["log_sink"] == 3
    assert architecture["resources"]["pydantic_model"] == 2
    assert adapter_metrics["pydantic"]["pydantic_models"] == 2
    assert adapter_metrics["pytest"]["test_cases"] == 2
    assert adapter_metrics["pytest"]["unresolved_registrations"] == 0
    assert adapter_metrics["pytest"]["unresolved_fixture_injections"] == 1
    assert adapter_metrics["typer_click"]["cli_commands"] == 1
    assert adapter_metrics["python_logging"]["logger_bindings"] == 3
    assert adapter_metrics["python_logging"]["log_calls"] == 3
    assert cli_flow["status"] == "available"
    assert any(edge["target"] == "fn:pkg.cli.sync" for edge in cli_flow["edges"])
