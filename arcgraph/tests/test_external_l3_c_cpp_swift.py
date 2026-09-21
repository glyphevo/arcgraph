from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.ci import run_ci_checks
from arcgraph.pipeline.external_l3_frontends import (
    C_EXTERNAL_FRONTEND_NAME,
    CPP_EXTERNAL_FRONTEND_NAME,
    SWIFT_EXTERNAL_FRONTEND_NAME,
    CExternalSemanticFrontend,
    CppExternalSemanticFrontend,
    SwiftExternalSemanticFrontend,
)
from arcgraph.pipeline.external_protocol import external_payload_to_fragment
from arcgraph.pipeline.frontends import (
    FrontendRegistry,
    LanguageFrontend,
    default_frontend_registry,
)
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "external_l3_c_cpp_swift"
C_PAYLOAD = FIXTURE_ROOT / "c-payload.json"
CPP_PAYLOAD = FIXTURE_ROOT / "cpp-payload.json"
SWIFT_PAYLOAD = FIXTURE_ROOT / "swift-payload.json"
SCIP_PAYLOAD = FIXTURE_ROOT / "scip-index.json"


def _load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_payload(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    payload_path = tmp_path / name
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
        source_roots=[SourceRoot("c"), SourceRoot("cpp"), SourceRoot("swift")],
        frontend_registry=FrontendRegistry(frontends),
        scip_graph_index_path=SCIP_PAYLOAD if scip else None,
    ).build()
    return GraphStoreReader.from_current(output_dir)


def test_c_external_payload_converts_to_l3_fragment() -> None:
    frontend = CExternalSemanticFrontend(payload_path=C_PAYLOAD)

    fragment = external_payload_to_fragment(
        _load_payload(C_PAYLOAD),
        repo_root=FIXTURE_ROOT,
        expected_frontend=frontend.capabilities(),
        supported_node_kinds=set(frontend.supported_node_kinds),
        supported_edge_kinds=set(frontend.supported_edge_kinds),
    )

    assert fragment.frontend is not None
    assert fragment.frontend.name == C_EXTERNAL_FRONTEND_NAME
    assert fragment.extractor_metadata == {
        "name": C_EXTERNAL_FRONTEND_NAME,
        "version": "0.1.0",
        "language": "c",
        "tier": "L3",
    }
    assert any(node.kind == "translation_unit" for node in fragment.nodes)
    assert any(node.kind == "typedef" for node in fragment.nodes)
    assert any(edge.kind == "calls" for edge in fragment.edges)
    assert any(edge.kind == "reads" for edge in fragment.edges)


def test_cpp_external_payload_converts_to_l3_fragment() -> None:
    frontend = CppExternalSemanticFrontend(payload_path=CPP_PAYLOAD)

    fragment = external_payload_to_fragment(
        _load_payload(CPP_PAYLOAD),
        repo_root=FIXTURE_ROOT,
        expected_frontend=frontend.capabilities(),
        supported_node_kinds=set(frontend.supported_node_kinds),
        supported_edge_kinds=set(frontend.supported_edge_kinds),
    )

    assert fragment.frontend is not None
    assert fragment.frontend.name == CPP_EXTERNAL_FRONTEND_NAME
    assert fragment.extractor_metadata["language"] == "cpp"
    assert fragment.extractor_metadata["tier"] == "L3"
    assert any(node.kind == "template" for node in fragment.nodes)
    assert any(node.kind == "destructor" for node in fragment.nodes)
    assert any(edge.kind == "inherits" for edge in fragment.edges)
    assert any(edge.kind == "overrides" for edge in fragment.edges)


def test_swift_external_payload_converts_to_l3_fragment() -> None:
    frontend = SwiftExternalSemanticFrontend(payload_path=SWIFT_PAYLOAD)

    fragment = external_payload_to_fragment(
        _load_payload(SWIFT_PAYLOAD),
        repo_root=FIXTURE_ROOT,
        expected_frontend=frontend.capabilities(),
        supported_node_kinds=set(frontend.supported_node_kinds),
        supported_edge_kinds=set(frontend.supported_edge_kinds),
    )

    assert fragment.frontend is not None
    assert fragment.frontend.name == SWIFT_EXTERNAL_FRONTEND_NAME
    assert fragment.extractor_metadata["language"] == "swift"
    assert fragment.extractor_metadata["tier"] == "L3"
    assert any(node.kind == "protocol" for node in fragment.nodes)
    assert any(node.kind == "extension" for node in fragment.nodes)
    assert any(edge.kind == "conforms" for edge in fragment.edges)
    assert any(edge.kind == "calls" for edge in fragment.edges)


def test_c_cpp_and_swift_external_payloads_report_l3_language_tiers(
    tmp_path: Path,
) -> None:
    reader = _build(
        tmp_path,
        [
            CExternalSemanticFrontend(payload_path=C_PAYLOAD),
            CppExternalSemanticFrontend(payload_path=CPP_PAYLOAD),
            SwiftExternalSemanticFrontend(payload_path=SWIFT_PAYLOAD),
        ],
    )
    output_dir = tmp_path / "arcgraph"
    tiers = reader.metadata["language_tiers"]
    node_ids = {node.id for node in reader.read_nodes()}
    edge_keys = {(edge.source, edge.kind, edge.target) for edge in reader.read_edges()}
    warnings = {warning.kind for warning in reader.read_warnings()}

    assert tiers["c"]["tier"] == "L3"
    assert tiers["c"]["frontend"] == C_EXTERNAL_FRONTEND_NAME
    assert tiers["cpp"]["tier"] == "L3"
    assert tiers["cpp"]["frontend"] == CPP_EXTERNAL_FRONTEND_NAME
    assert tiers["swift"]["tier"] == "L3"
    assert tiers["swift"]["frontend"] == SWIFT_EXTERNAL_FRONTEND_NAME
    assert "function:c:use_repository" in node_ids
    assert "class:cpp:arcgraph::RepositoryService" in node_ids
    assert "struct:swift:ArcGraphDemo.Repository" in node_ids
    assert (
        "function:c:use_repository",
        "calls",
        "function:c:repository_save",
    ) in edge_keys
    assert (
        "class:cpp:arcgraph::RepositoryService",
        "inherits",
        "class:cpp:arcgraph::BaseSaver",
    ) in edge_keys
    assert (
        "function:swift:ArcGraphDemo.useSaver",
        "calls",
        "method:swift:ArcGraphDemo.Repository.save",
    ) in edge_keys
    assert "c_external_function_pointer_deferred" in warnings
    assert "cpp_external_template_instantiation_deferred" in warnings
    assert "swift_external_result_builder_deferred" in warnings

    current = QueryEngine(output_dir).current()
    stats = QueryEngine(output_dir).stats()
    ci = run_ci_checks(QueryEngine(output_dir))
    checks = {check["name"]: check for check in ci["checks"]}

    assert current["language_tiers"] == tiers
    assert stats["language_tiers"] == tiers
    assert current["toolchain_status"][C_EXTERNAL_FRONTEND_NAME]["status"] == (
        "available"
    )
    assert current["toolchain_status"][CPP_EXTERNAL_FRONTEND_NAME]["status"] == (
        "available"
    )
    assert current["toolchain_status"][SWIFT_EXTERNAL_FRONTEND_NAME]["status"] == (
        "available"
    )
    assert checks["language_capability_tiers"]["status"] == "pass"
    assert checks["external_extractor_toolchains"]["status"] == "pass"


def test_c_missing_toolchain_reports_unavailable_without_l3(tmp_path: Path) -> None:
    reader = _build(
        tmp_path,
        [
            CExternalSemanticFrontend(
                command=["arcgraph-definitely-missing-c-extractor"]
            )
        ],
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    tiers = reader.metadata["language_tiers"]

    assert "external_extractor_missing_toolchain" in warnings
    assert (
        reader.metadata["toolchain_status"][C_EXTERNAL_FRONTEND_NAME]["status"]
        == "missing"
    )
    assert tiers["c"]["tier"] == "L2"
    assert tiers["c"]["status"] == "requires_explicit_scip_graph_index"


def test_cpp_missing_toolchain_reports_unavailable_without_l3(
    tmp_path: Path,
) -> None:
    reader = _build(
        tmp_path,
        [
            CppExternalSemanticFrontend(
                command=["arcgraph-definitely-missing-cpp-extractor"]
            )
        ],
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    tiers = reader.metadata["language_tiers"]

    assert "external_extractor_missing_toolchain" in warnings
    assert (
        reader.metadata["toolchain_status"][CPP_EXTERNAL_FRONTEND_NAME]["status"]
        == "missing"
    )
    assert tiers["cpp"]["tier"] == "L2"
    assert tiers["cpp"]["status"] == "requires_explicit_scip_graph_index"


def test_swift_missing_toolchain_reports_unavailable_without_l3(
    tmp_path: Path,
) -> None:
    reader = _build(
        tmp_path,
        [
            SwiftExternalSemanticFrontend(
                command=["arcgraph-definitely-missing-swift-extractor"]
            )
        ],
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    tiers = reader.metadata["language_tiers"]

    assert "external_extractor_missing_toolchain" in warnings
    assert (
        reader.metadata["toolchain_status"][SWIFT_EXTERNAL_FRONTEND_NAME]["status"]
        == "missing"
    )
    assert tiers["swift"]["tier"] == "L2"
    assert tiers["swift"]["status"] == "requires_explicit_scip_graph_index"


def test_c_invalid_payload_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(C_PAYLOAD)
    payload["capabilities"]["supported_node_kinds"].append("runtime_symbol")
    payload_path = _write_payload(tmp_path, "bad-c-payload.json", payload)

    reader = _build(tmp_path, [CExternalSemanticFrontend(payload_path=payload_path)])

    assert "external_extractor_invalid_payload" in {
        warning.kind for warning in reader.read_warnings()
    }
    assert reader.metadata["language_tiers"]["c"]["tier"] == "L2"


def test_cpp_invalid_confidence_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(CPP_PAYLOAD)
    payload["edges"][0]["confidence"] = "certain"
    payload_path = _write_payload(tmp_path, "bad-cpp-payload.json", payload)

    reader = _build(tmp_path, [CppExternalSemanticFrontend(payload_path=payload_path)])

    assert "external_extractor_invalid_payload" in {
        warning.kind for warning in reader.read_warnings()
    }
    assert reader.metadata["language_tiers"]["cpp"]["tier"] == "L2"


def test_swift_unsupported_edge_kind_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(SWIFT_PAYLOAD)
    payload["edges"][0]["kind"] = "decorates"
    payload_path = _write_payload(tmp_path, "bad-swift-edge-payload.json", payload)

    reader = _build(
        tmp_path,
        [SwiftExternalSemanticFrontend(payload_path=payload_path)],
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("Unsupported external extractor edge kind" in item for item in messages)
    assert reader.metadata["language_tiers"]["swift"]["tier"] == "L2"


def test_swift_unsupported_node_kind_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(SWIFT_PAYLOAD)
    payload["nodes"][0]["kind"] = "macro"
    payload_path = _write_payload(tmp_path, "bad-swift-node-payload.json", payload)

    reader = _build(
        tmp_path,
        [SwiftExternalSemanticFrontend(payload_path=payload_path)],
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("Unsupported external extractor node kind" in item for item in messages)
    assert reader.metadata["language_tiers"]["swift"]["tier"] == "L2"


def test_c_payload_path_escape_is_warning_and_does_not_report_l3(
    tmp_path: Path,
) -> None:
    payload = _load_payload(C_PAYLOAD)
    payload["nodes"][0]["path"] = "../outside.c"
    payload_path = _write_payload(tmp_path, "escape-c-payload.json", payload)

    reader = _build(tmp_path, [CExternalSemanticFrontend(payload_path=payload_path)])

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("outside the repository" in message for message in messages)
    assert reader.metadata["language_tiers"]["c"]["tier"] == "L2"


def test_scip_l2_and_external_l3_c_cpp_swift_tiers_are_distinguished(
    tmp_path: Path,
) -> None:
    scip_only = _build(tmp_path / "scip-only", [], scip=True)
    assert scip_only.metadata["language_tiers"]["c"]["tier"] == "L2"
    assert scip_only.metadata["language_tiers"]["cpp"]["tier"] == "L2"
    assert scip_only.metadata["language_tiers"]["swift"]["tier"] == "L2"
    assert scip_only.metadata["language_tiers"]["c"]["frontend"] == "scip-protocol"
    assert scip_only.metadata["language_tiers"]["cpp"]["frontend"] == "scip-protocol"
    assert scip_only.metadata["language_tiers"]["swift"]["frontend"] == "scip-protocol"

    combined = _build(
        tmp_path / "combined",
        [
            CExternalSemanticFrontend(payload_path=C_PAYLOAD),
            CppExternalSemanticFrontend(payload_path=CPP_PAYLOAD),
            SwiftExternalSemanticFrontend(payload_path=SWIFT_PAYLOAD),
        ],
        scip=True,
    )
    tiers = combined.metadata["language_tiers"]

    assert tiers["c"]["tier"] == "L3"
    assert tiers["c"]["frontend"] == C_EXTERNAL_FRONTEND_NAME
    assert tiers["cpp"]["tier"] == "L3"
    assert tiers["cpp"]["frontend"] == CPP_EXTERNAL_FRONTEND_NAME
    assert tiers["swift"]["tier"] == "L3"
    assert tiers["swift"]["frontend"] == SWIFT_EXTERNAL_FRONTEND_NAME


def test_c_cpp_and_swift_external_frontends_are_not_registered_by_default() -> None:
    inventory = default_frontend_registry(repo_root=FIXTURE_ROOT).inventory()
    names = {entry["name"] for entry in inventory}

    assert C_EXTERNAL_FRONTEND_NAME not in names
    assert CPP_EXTERNAL_FRONTEND_NAME not in names
    assert SWIFT_EXTERNAL_FRONTEND_NAME not in names
