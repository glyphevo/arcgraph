from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.ci import run_ci_checks
from arcgraph.pipeline.external_l3_frontends import (
    JAVA_EXTERNAL_FRONTEND_NAME,
    RUST_EXTERNAL_FRONTEND_NAME,
    JavaExternalSemanticFrontend,
    RustExternalSemanticFrontend,
)
from arcgraph.pipeline.external_protocol import external_payload_to_fragment
from arcgraph.pipeline.frontends import (
    FrontendRegistry,
    LanguageFrontend,
    default_frontend_registry,
)
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "external_l3_java_rust"
JAVA_PAYLOAD = FIXTURE_ROOT / "java-payload.json"
RUST_PAYLOAD = FIXTURE_ROOT / "rust-payload.json"
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
        source_roots=[SourceRoot("java"), SourceRoot("rust")],
        frontend_registry=FrontendRegistry(frontends),
        scip_graph_index_path=SCIP_PAYLOAD if scip else None,
    ).build()
    return GraphStoreReader.from_current(output_dir)


def test_java_external_payload_converts_to_l3_fragment() -> None:
    frontend = JavaExternalSemanticFrontend(payload_path=JAVA_PAYLOAD)

    fragment = external_payload_to_fragment(
        _load_payload(JAVA_PAYLOAD),
        repo_root=FIXTURE_ROOT,
        expected_frontend=frontend.capabilities(),
        supported_node_kinds=set(frontend.supported_node_kinds),
        supported_edge_kinds=set(frontend.supported_edge_kinds),
    )

    assert fragment.frontend is not None
    assert fragment.frontend.name == JAVA_EXTERNAL_FRONTEND_NAME
    assert fragment.extractor_metadata == {
        "name": JAVA_EXTERNAL_FRONTEND_NAME,
        "version": "0.1.0",
        "language": "java",
        "tier": "L3",
    }
    assert any(node.kind == "record" for node in fragment.nodes)
    assert any(node.kind == "annotation" for node in fragment.nodes)
    assert any(edge.kind == "has_annotation" for edge in fragment.edges)
    assert any(edge.kind == "calls" for edge in fragment.edges)


def test_rust_external_payload_converts_to_l3_fragment() -> None:
    frontend = RustExternalSemanticFrontend(payload_path=RUST_PAYLOAD)

    fragment = external_payload_to_fragment(
        _load_payload(RUST_PAYLOAD),
        repo_root=FIXTURE_ROOT,
        expected_frontend=frontend.capabilities(),
        supported_node_kinds=set(frontend.supported_node_kinds),
        supported_edge_kinds=set(frontend.supported_edge_kinds),
    )

    assert fragment.frontend is not None
    assert fragment.frontend.name == RUST_EXTERNAL_FRONTEND_NAME
    assert fragment.extractor_metadata["language"] == "rust"
    assert fragment.extractor_metadata["tier"] == "L3"
    assert any(node.kind == "trait" for node in fragment.nodes)
    assert any(node.kind == "impl" for node in fragment.nodes)
    assert any(edge.kind == "implements" for edge in fragment.edges)
    assert any(edge.kind == "type_ref" for edge in fragment.edges)


def test_java_and_rust_external_payloads_report_l3_language_tiers(
    tmp_path: Path,
) -> None:
    reader = _build(
        tmp_path,
        [
            JavaExternalSemanticFrontend(payload_path=JAVA_PAYLOAD),
            RustExternalSemanticFrontend(payload_path=RUST_PAYLOAD),
        ],
    )
    output_dir = tmp_path / "arcgraph"
    tiers = reader.metadata["language_tiers"]
    node_ids = {node.id for node in reader.read_nodes()}
    edge_keys = {(edge.source, edge.kind, edge.target) for edge in reader.read_edges()}

    assert tiers["java"]["tier"] == "L3"
    assert tiers["java"]["frontend"] == JAVA_EXTERNAL_FRONTEND_NAME
    assert tiers["rust"]["tier"] == "L3"
    assert tiers["rust"]["frontend"] == RUST_EXTERNAL_FRONTEND_NAME
    assert "class:java:com.example.arcgraph.RepositoryService" in node_ids
    assert "trait:rust:arcgraph_demo::service::Saver" in node_ids
    assert (
        "method:java:com.example.arcgraph.RepositoryService.run",
        "calls",
        "method:java:com.example.arcgraph.RepositoryService.save",
    ) in edge_keys
    assert (
        "impl:rust:arcgraph_demo::service::Repository::Saver",
        "implements",
        "trait:rust:arcgraph_demo::service::Saver",
    ) in edge_keys

    current = QueryEngine(output_dir).current()
    stats = QueryEngine(output_dir).stats()
    ci = run_ci_checks(QueryEngine(output_dir))
    checks = {check["name"]: check for check in ci["checks"]}

    assert current["language_tiers"] == tiers
    assert stats["language_tiers"] == tiers
    assert current["toolchain_status"][JAVA_EXTERNAL_FRONTEND_NAME]["status"] == (
        "available"
    )
    assert current["toolchain_status"][RUST_EXTERNAL_FRONTEND_NAME]["status"] == (
        "available"
    )
    assert checks["language_capability_tiers"]["status"] == "pass"
    assert checks["external_extractor_toolchains"]["status"] == "pass"


def test_java_missing_toolchain_reports_unavailable_without_l3(
    tmp_path: Path,
) -> None:
    reader = _build(
        tmp_path,
        [
            JavaExternalSemanticFrontend(
                command=["arcgraph-definitely-missing-java-extractor"]
            )
        ],
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    tiers = reader.metadata["language_tiers"]

    assert "external_extractor_missing_toolchain" in warnings
    assert (
        reader.metadata["toolchain_status"][JAVA_EXTERNAL_FRONTEND_NAME]["status"]
        == "missing"
    )
    assert tiers["java"]["tier"] == "L2"
    assert tiers["java"]["status"] == "requires_explicit_scip_graph_index"


def test_rust_missing_toolchain_reports_unavailable_without_l3(
    tmp_path: Path,
) -> None:
    reader = _build(
        tmp_path,
        [
            RustExternalSemanticFrontend(
                command=["arcgraph-definitely-missing-rust-extractor"]
            )
        ],
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    tiers = reader.metadata["language_tiers"]

    assert "external_extractor_missing_toolchain" in warnings
    assert (
        reader.metadata["toolchain_status"][RUST_EXTERNAL_FRONTEND_NAME]["status"]
        == "missing"
    )
    assert tiers["rust"]["tier"] == "L2"
    assert tiers["rust"]["status"] == "requires_explicit_scip_graph_index"


def test_java_invalid_payload_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(JAVA_PAYLOAD)
    payload["capabilities"]["supported_node_kinds"].append("macro")
    payload_path = _write_payload(tmp_path, "bad-java-payload.json", payload)

    reader = _build(
        tmp_path,
        [JavaExternalSemanticFrontend(payload_path=payload_path)],
    )

    assert "external_extractor_invalid_payload" in {
        warning.kind for warning in reader.read_warnings()
    }
    assert reader.metadata["language_tiers"]["java"]["tier"] == "L2"


def test_rust_invalid_confidence_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(RUST_PAYLOAD)
    payload["edges"][0]["confidence"] = "certain"
    payload_path = _write_payload(tmp_path, "bad-rust-payload.json", payload)

    reader = _build(
        tmp_path,
        [RustExternalSemanticFrontend(payload_path=payload_path)],
    )

    assert "external_extractor_invalid_payload" in {
        warning.kind for warning in reader.read_warnings()
    }
    assert reader.metadata["language_tiers"]["rust"]["tier"] == "L2"


def test_java_unsupported_edge_kind_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(JAVA_PAYLOAD)
    payload["edges"][0]["kind"] = "mutates"
    payload_path = _write_payload(tmp_path, "bad-java-edge-payload.json", payload)

    reader = _build(
        tmp_path,
        [JavaExternalSemanticFrontend(payload_path=payload_path)],
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("Unsupported external extractor edge kind" in item for item in messages)
    assert reader.metadata["language_tiers"]["java"]["tier"] == "L2"


def test_rust_unsupported_node_kind_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(RUST_PAYLOAD)
    payload["nodes"][0]["kind"] = "macro"
    payload_path = _write_payload(tmp_path, "bad-rust-node-payload.json", payload)

    reader = _build(
        tmp_path,
        [RustExternalSemanticFrontend(payload_path=payload_path)],
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("Unsupported external extractor node kind" in item for item in messages)
    assert reader.metadata["language_tiers"]["rust"]["tier"] == "L2"


def test_java_payload_path_escape_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(JAVA_PAYLOAD)
    payload["nodes"][0]["path"] = "../outside.java"
    payload_path = _write_payload(tmp_path, "escape-java-payload.json", payload)

    reader = _build(
        tmp_path,
        [JavaExternalSemanticFrontend(payload_path=payload_path)],
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("outside the repository" in message for message in messages)
    assert reader.metadata["language_tiers"]["java"]["tier"] == "L2"


def test_scip_l2_and_external_l3_java_rust_tiers_are_distinguished(
    tmp_path: Path,
) -> None:
    scip_only = _build(tmp_path / "scip-only", [], scip=True)
    assert scip_only.metadata["language_tiers"]["java"]["tier"] == "L2"
    assert scip_only.metadata["language_tiers"]["rust"]["tier"] == "L2"
    assert scip_only.metadata["language_tiers"]["java"]["frontend"] == "scip-protocol"
    assert scip_only.metadata["language_tiers"]["rust"]["frontend"] == "scip-protocol"

    combined = _build(
        tmp_path / "combined",
        [
            JavaExternalSemanticFrontend(payload_path=JAVA_PAYLOAD),
            RustExternalSemanticFrontend(payload_path=RUST_PAYLOAD),
        ],
        scip=True,
    )
    tiers = combined.metadata["language_tiers"]

    assert tiers["java"]["tier"] == "L3"
    assert tiers["java"]["frontend"] == JAVA_EXTERNAL_FRONTEND_NAME
    assert tiers["rust"]["tier"] == "L3"
    assert tiers["rust"]["frontend"] == RUST_EXTERNAL_FRONTEND_NAME


def test_java_and_rust_external_frontends_are_not_registered_by_default() -> None:
    inventory = default_frontend_registry(repo_root=FIXTURE_ROOT).inventory()
    names = {entry["name"] for entry in inventory}

    assert JAVA_EXTERNAL_FRONTEND_NAME not in names
    assert RUST_EXTERNAL_FRONTEND_NAME not in names
