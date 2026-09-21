from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.ci import run_ci_checks
from arcgraph.pipeline.external_l3_frontends import (
    CSHARP_EXTERNAL_FRONTEND_NAME,
    GO_EXTERNAL_FRONTEND_NAME,
    CSharpExternalSemanticFrontend,
    GoExternalSemanticFrontend,
)
from arcgraph.pipeline.external_protocol import external_payload_to_fragment
from arcgraph.pipeline.frontends import (
    FrontendRegistry,
    LanguageFrontend,
    default_frontend_registry,
)
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "external_l3_go_csharp"
GO_PAYLOAD = FIXTURE_ROOT / "go-payload.json"
CSHARP_PAYLOAD = FIXTURE_ROOT / "csharp-payload.json"
SCIP_PAYLOAD = FIXTURE_ROOT / "scip-index.json"


def _load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_payload(tmp_path: Path, name: str, payload: dict[str, Any] | str) -> Path:
    payload_path = tmp_path / name
    if isinstance(payload, str):
        payload_path.write_text(payload, encoding="utf-8")
    else:
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload_path


def _build(
    tmp_path: Path,
    frontends: list[LanguageFrontend],
    *,
    scip: bool = False,
) -> GraphStoreReader:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("go"), SourceRoot("csharp")],
        frontend_registry=FrontendRegistry(frontends),
        scip_graph_index_path=SCIP_PAYLOAD if scip else None,
    ).build()
    return GraphStoreReader.from_current(output_dir)


def test_go_external_payload_converts_to_l3_fragment() -> None:
    frontend = GoExternalSemanticFrontend(payload_path=GO_PAYLOAD)

    fragment = external_payload_to_fragment(
        _load_payload(GO_PAYLOAD),
        repo_root=FIXTURE_ROOT,
        expected_frontend=frontend.capabilities(),
        supported_node_kinds=set(frontend.supported_node_kinds),
        supported_edge_kinds=set(frontend.supported_edge_kinds),
    )

    assert fragment.frontend is not None
    assert fragment.frontend.name == GO_EXTERNAL_FRONTEND_NAME
    assert fragment.extractor_metadata == {
        "name": GO_EXTERNAL_FRONTEND_NAME,
        "version": "0.1.0",
        "language": "go",
        "tier": "L3",
    }
    assert any(node.kind == "struct" for node in fragment.nodes)
    assert any(edge.kind == "calls" for edge in fragment.edges)


def test_csharp_external_payload_converts_to_l3_fragment() -> None:
    frontend = CSharpExternalSemanticFrontend(payload_path=CSHARP_PAYLOAD)

    fragment = external_payload_to_fragment(
        _load_payload(CSHARP_PAYLOAD),
        repo_root=FIXTURE_ROOT,
        expected_frontend=frontend.capabilities(),
        supported_node_kinds=set(frontend.supported_node_kinds),
        supported_edge_kinds=set(frontend.supported_edge_kinds),
    )

    assert fragment.frontend is not None
    assert fragment.frontend.name == CSHARP_EXTERNAL_FRONTEND_NAME
    assert fragment.extractor_metadata["language"] == "csharp"
    assert fragment.extractor_metadata["tier"] == "L3"
    assert any(node.kind == "record" for node in fragment.nodes)
    assert any(edge.kind == "implements" for edge in fragment.edges)


def test_go_and_csharp_external_payloads_report_l3_language_tiers(
    tmp_path: Path,
) -> None:
    reader = _build(
        tmp_path,
        [
            GoExternalSemanticFrontend(payload_path=GO_PAYLOAD),
            CSharpExternalSemanticFrontend(payload_path=CSHARP_PAYLOAD),
        ],
    )
    output_dir = tmp_path / "arcgraph"
    tiers = reader.metadata["language_tiers"]
    node_ids = {node.id for node in reader.read_nodes()}
    edge_keys = {(edge.source, edge.kind, edge.target) for edge in reader.read_edges()}

    assert tiers["go"]["tier"] == "L3"
    assert tiers["go"]["frontend"] == GO_EXTERNAL_FRONTEND_NAME
    assert tiers["csharp"]["tier"] == "L3"
    assert tiers["csharp"]["frontend"] == CSHARP_EXTERNAL_FRONTEND_NAME
    assert "struct:go:example.com/arcgraphdemo/service.Repository" in node_ids
    assert "class:csharp:ArcGraphDemo.RepositoryService" in node_ids
    assert (
        "function:go:example.com/arcgraphdemo/service.UseSaver",
        "calls",
        "method:go:example.com/arcgraphdemo/service.Repository.Save",
    ) in edge_keys
    assert (
        "class:csharp:ArcGraphDemo.RepositoryService",
        "implements",
        "interface:csharp:ArcGraphDemo.IRepository",
    ) in edge_keys

    current = QueryEngine(output_dir).current()
    stats = QueryEngine(output_dir).stats()
    ci = run_ci_checks(QueryEngine(output_dir))
    checks = {check["name"]: check for check in ci["checks"]}

    assert current["language_tiers"] == tiers
    assert stats["language_tiers"] == tiers
    assert current["toolchain_status"][GO_EXTERNAL_FRONTEND_NAME]["status"] == (
        "available"
    )
    assert checks["language_capability_tiers"]["status"] == "pass"
    assert checks["external_extractor_toolchains"]["status"] == "pass"


def test_go_missing_toolchain_reports_unavailable_without_l3(
    tmp_path: Path,
) -> None:
    reader = _build(
        tmp_path,
        [
            GoExternalSemanticFrontend(
                command=["arcgraph-definitely-missing-go-extractor"]
            )
        ],
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    tiers = reader.metadata["language_tiers"]

    assert "external_extractor_missing_toolchain" in warnings
    assert (
        reader.metadata["toolchain_status"][GO_EXTERNAL_FRONTEND_NAME]["status"]
        == "missing"
    )
    assert tiers["go"]["tier"] == "L2"
    assert tiers["go"]["status"] == "requires_explicit_scip_graph_index"


def test_csharp_missing_toolchain_reports_unavailable_without_l3(
    tmp_path: Path,
) -> None:
    reader = _build(
        tmp_path,
        [
            CSharpExternalSemanticFrontend(
                command=["arcgraph-definitely-missing-csharp-extractor"]
            )
        ],
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    tiers = reader.metadata["language_tiers"]

    assert "external_extractor_missing_toolchain" in warnings
    assert (
        reader.metadata["toolchain_status"][CSHARP_EXTERNAL_FRONTEND_NAME]["status"]
        == "missing"
    )
    assert tiers["csharp"]["tier"] == "L2"
    assert tiers["csharp"]["status"] == "requires_explicit_scip_graph_index"


def test_go_invalid_payload_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(GO_PAYLOAD)
    payload["capabilities"]["supported_node_kinds"].append("runtime_symbol")
    payload_path = _write_payload(tmp_path, "bad-go-payload.json", payload)

    reader = _build(
        tmp_path,
        [GoExternalSemanticFrontend(payload_path=payload_path)],
    )

    assert "external_extractor_invalid_payload" in {
        warning.kind for warning in reader.read_warnings()
    }
    assert reader.metadata["language_tiers"]["go"]["tier"] == "L2"


def test_csharp_unsupported_confidence_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(CSHARP_PAYLOAD)
    payload["edges"][0]["confidence"] = "certain"
    payload_path = _write_payload(tmp_path, "bad-csharp-payload.json", payload)

    reader = _build(
        tmp_path,
        [CSharpExternalSemanticFrontend(payload_path=payload_path)],
    )

    assert "external_extractor_invalid_payload" in {
        warning.kind for warning in reader.read_warnings()
    }
    assert reader.metadata["language_tiers"]["csharp"]["tier"] == "L2"


def test_go_payload_path_escape_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(GO_PAYLOAD)
    payload["nodes"][0]["path"] = "../outside.go"
    payload_path = _write_payload(tmp_path, "escape-go-payload.json", payload)

    reader = _build(
        tmp_path,
        [GoExternalSemanticFrontend(payload_path=payload_path)],
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("outside the repository" in message for message in messages)
    assert reader.metadata["language_tiers"]["go"]["tier"] == "L2"


def test_scip_l2_and_external_l3_language_tiers_are_distinguished(
    tmp_path: Path,
) -> None:
    scip_only = _build(tmp_path / "scip-only", [], scip=True)
    assert scip_only.metadata["language_tiers"]["go"]["tier"] == "L2"
    assert scip_only.metadata["language_tiers"]["csharp"]["tier"] == "L2"
    assert scip_only.metadata["language_tiers"]["go"]["frontend"] == "scip-protocol"
    assert scip_only.metadata["language_tiers"]["csharp"]["frontend"] == (
        "scip-protocol"
    )

    combined = _build(
        tmp_path / "combined",
        [
            GoExternalSemanticFrontend(payload_path=GO_PAYLOAD),
            CSharpExternalSemanticFrontend(payload_path=CSHARP_PAYLOAD),
        ],
        scip=True,
    )
    tiers = combined.metadata["language_tiers"]

    assert tiers["go"]["tier"] == "L3"
    assert tiers["go"]["frontend"] == GO_EXTERNAL_FRONTEND_NAME
    assert tiers["csharp"]["tier"] == "L3"
    assert tiers["csharp"]["frontend"] == CSHARP_EXTERNAL_FRONTEND_NAME


def test_go_and_csharp_external_frontends_are_not_registered_by_default() -> None:
    inventory = default_frontend_registry(repo_root=FIXTURE_ROOT).inventory()
    names = {entry["name"] for entry in inventory}

    assert GO_EXTERNAL_FRONTEND_NAME not in names
    assert CSHARP_EXTERNAL_FRONTEND_NAME not in names
