from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.ci import run_ci_checks
from arcgraph.interfaces.cli import main as cli_main
from arcgraph.pipeline.external_frontend import ExternalSemanticExtractorFrontend
from arcgraph.pipeline.external_protocol import (
    ExternalExtractorValidationError,
    ToolchainRequirement,
    external_payload_to_fragment,
)
from arcgraph.pipeline.frontends import FrontendRegistry
from arcgraph.pipeline.indexer import ArcGraphIndexer

SEMANTIC_EDGE_KINDS_UNDER_TEST = (
    "calls",
    "conforms",
    "declares",
    "defines",
    "extends",
    "has_annotation",
    "has_attribute",
    "has_field",
    "imports",
    "inherits",
    "implements",
    "invokes",
    "links_to",
    "overrides",
    "reads",
    "references",
    "renders",
    "type_ref",
    "uses",
    "writes",
)


def _sample_repo(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "app.mock").write_text("fn run {}\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'mockrepo'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )
    return tmp_path


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "extractor": {"name": "mock-external", "version": "0.1"},
        "language": "mocklang",
        "capabilities": {
            "tier": "L3",
            "supported_node_kinds": ["module", "function"],
            "supported_edge_kinds": ["defines"],
        },
        "toolchain": {
            "status": "available",
            "tools": [
                {
                    "name": "mock-extractor",
                    "status": "available",
                    "required": False,
                }
            ],
            "requirements": [],
        },
        "nodes": [
            {
                "id": "mod:src.app",
                "kind": "module",
                "name": "app",
                "qualname": "src.app",
                "path": "src/app.mock",
                "properties": {"language": "mocklang"},
            },
            {
                "id": "fn:src.app.run",
                "kind": "function",
                "name": "run",
                "qualname": "src.app.run",
                "path": "src/app.mock",
                "start_line": 1,
                "end_line": 1,
                "properties": {"language": "mocklang"},
            },
        ],
        "edges": [
            {
                "source": "mod:src.app",
                "target": "fn:src.app.run",
                "kind": "defines",
                "confidence": "confirmed",
                "resolution": {
                    "status": "resolved",
                    "strategy": "mock_symbol_table",
                    "fallbacks": [],
                },
                "evidence": [
                    {
                        "kind": "mock_definition",
                        "path": "src/app.mock",
                        "start_line": 1,
                        "end_line": 1,
                        "column": 1,
                    }
                ],
                "properties": {"language": "mocklang"},
            }
        ],
        "warnings": [
            {
                "kind": "mock_external_note",
                "message": "mock note",
                "path": "src/app.mock",
            }
        ],
        "metrics": {"definitions_total": 1},
    }
    payload.update(overrides)
    return payload


def _frontend(
    payload_path: Path | None = None,
    *,
    supported_node_kinds: tuple[str, ...] = ("module", "function"),
    supported_edge_kinds: tuple[str, ...] = ("defines",),
    **kwargs: Any,
):
    return ExternalSemanticExtractorFrontend(
        name="mock-external",
        version="0.1",
        language="mocklang",
        file_extensions=(".mock",),
        payload_path=payload_path,
        supported_node_kinds=supported_node_kinds,
        supported_edge_kinds=supported_edge_kinds,
        **kwargs,
    )


def _build_with_frontend(tmp_path: Path, frontend) -> GraphStoreReader:
    _sample_repo(tmp_path)
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
        frontend_registry=FrontendRegistry([frontend]),
    ).build()
    return GraphStoreReader.from_current(output_dir)


def _write_payload(tmp_path: Path, payload: dict[str, Any] | str) -> Path:
    payload_path = tmp_path / "payload.json"
    if isinstance(payload, str):
        payload_path.write_text(payload, encoding="utf-8")
    else:
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload_path


def _payload_with_edge_kind(edge_kind: str) -> dict[str, Any]:
    payload = _payload()
    payload["capabilities"]["supported_edge_kinds"] = [edge_kind]
    payload["edges"][0]["kind"] = edge_kind
    return payload


def test_external_payload_converts_to_frontend_fragment(tmp_path: Path) -> None:
    repo_root = _sample_repo(tmp_path)
    frontend = _frontend(_write_payload(tmp_path, _payload()))

    fragment = external_payload_to_fragment(
        _payload(),
        repo_root=repo_root,
        expected_frontend=frontend.capabilities(),
        supported_node_kinds={"module", "function"},
        supported_edge_kinds={"defines"},
    )

    assert fragment.frontend is not None
    assert fragment.frontend.name == "mock-external"
    assert [node.kind for node in fragment.nodes] == ["module", "function"]
    assert fragment.edges[0].resolution.strategy == "mock_symbol_table"
    assert fragment.toolchain_status["status"] == "available"
    assert fragment.extractor_metadata["tier"] == "L3"


def test_external_frontend_valid_payload_indexes_graph_and_reporting(
    tmp_path: Path, capsys
) -> None:
    payload_path = _write_payload(tmp_path, _payload())
    reader = _build_with_frontend(tmp_path, _frontend(payload_path))
    output_dir = tmp_path / "arcgraph"

    assert any(node.id == "fn:src.app.run" for node in reader.read_nodes())
    assert any(edge.kind == "defines" for edge in reader.read_edges())
    current = QueryEngine(output_dir).current()
    stats = QueryEngine(output_dir).stats()
    ci = run_ci_checks(QueryEngine(output_dir))
    checks = {check["name"]: check for check in ci["checks"]}

    assert current["language_tiers"]["mocklang"]["tier"] == "L3"
    assert current["toolchain_status"]["mock-external"]["status"] == "available"
    assert stats["toolchain_status"]["mock-external"]["status"] == "available"
    assert checks["external_extractor_toolchains"]["status"] == "pass"
    assert (
        checks["external_extractor_toolchains"]["details"]["frontends"][
            "mock-external"
        ]["status"]
        == "available"
    )

    exit_code = cli_main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            str(output_dir),
            "doctor",
        ]
    )
    assert exit_code == 0
    doctor = json.loads(capsys.readouterr().out)
    external_check = next(
        check for check in doctor["checks"] if check["name"] == "external_toolchains"
    )
    assert external_check["status"] == "pass"


def test_external_frontend_missing_toolchain_is_warning(tmp_path: Path) -> None:
    frontend = _frontend(
        None,
        command=["arcgraph-definitely-missing-extractor"],
        required_toolchains=(
            ToolchainRequirement(
                name="missing-extractor",
                command="arcgraph-definitely-missing-extractor",
                required=False,
            ),
        ),
    )

    reader = _build_with_frontend(tmp_path, frontend)

    warnings = {warning.kind for warning in reader.read_warnings()}
    assert "external_extractor_missing_toolchain" in warnings
    assert reader.metadata["toolchain_status"]["mock-external"]["status"] == "missing"


def test_external_frontend_required_missing_toolchain_is_ci_warning(
    tmp_path: Path, capsys
) -> None:
    frontend = _frontend(
        None,
        command=["arcgraph-definitely-missing-extractor"],
        required_toolchains=(
            ToolchainRequirement(
                name="missing-extractor",
                command="arcgraph-definitely-missing-extractor",
                required=True,
            ),
        ),
    )

    _build_with_frontend(tmp_path, frontend)
    output_dir = tmp_path / "arcgraph"
    ci = run_ci_checks(QueryEngine(output_dir))
    checks = {check["name"]: check for check in ci["checks"]}

    assert checks["external_extractor_toolchains"]["status"] == "warn"
    assert checks["external_extractor_toolchains"]["details"][
        "required_unavailable"
    ] == ["mock-external"]

    exit_code = cli_main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            str(output_dir),
            "doctor",
        ]
    )
    assert exit_code == 0
    doctor = json.loads(capsys.readouterr().out)
    external_check = next(
        check for check in doctor["checks"] if check["name"] == "external_toolchains"
    )
    assert external_check["status"] == "warn"


def test_external_frontend_timeout_is_warning(tmp_path: Path) -> None:
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")

    reader = _build_with_frontend(
        tmp_path,
        _frontend(
            None,
            command=[sys.executable, str(script)],
            timeout_seconds=0.1,
        ),
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    assert "external_extractor_timeout" in warnings
    assert reader.metadata["toolchain_status"]["mock-external"]["status"] == "timeout"


def test_external_frontend_invalid_json_is_warning(tmp_path: Path) -> None:
    payload_path = _write_payload(tmp_path, "{not-json")

    reader = _build_with_frontend(tmp_path, _frontend(payload_path))

    warnings = {warning.kind for warning in reader.read_warnings()}
    assert "external_extractor_invalid_json" in warnings
    assert (
        reader.metadata["toolchain_status"]["mock-external"]["status"]
        == "invalid_output"
    )


def test_external_frontend_invalid_confidence_is_warning(tmp_path: Path) -> None:
    payload = _payload()
    payload["edges"][0]["confidence"] = "certain"

    reader = _build_with_frontend(
        tmp_path, _frontend(_write_payload(tmp_path, payload))
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    assert "external_extractor_invalid_payload" in warnings


def test_external_frontend_unsupported_node_kind_is_warning(tmp_path: Path) -> None:
    payload = _payload()
    payload["nodes"][0]["kind"] = "macro"

    reader = _build_with_frontend(
        tmp_path, _frontend(_write_payload(tmp_path, payload))
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("Unsupported external extractor node kind" in item for item in messages)


def test_external_frontend_unsupported_edge_kind_is_warning(tmp_path: Path) -> None:
    payload = _payload()
    payload["edges"][0]["kind"] = "mutates"

    reader = _build_with_frontend(
        tmp_path, _frontend(_write_payload(tmp_path, payload))
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("Unsupported external extractor edge kind" in item for item in messages)


def test_external_frontend_missing_semantic_evidence_is_warning(
    tmp_path: Path,
) -> None:
    payload = _payload()
    payload["edges"][0]["evidence"] = []

    reader = _build_with_frontend(
        tmp_path, _frontend(_write_payload(tmp_path, payload))
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("missing evidence" in item for item in messages)


def test_external_frontend_missing_semantic_strategy_is_warning(
    tmp_path: Path,
) -> None:
    payload = _payload()
    payload["edges"][0]["resolution"] = {"status": "resolved"}

    reader = _build_with_frontend(
        tmp_path, _frontend(_write_payload(tmp_path, payload))
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("missing resolution.strategy" in item for item in messages)


@pytest.mark.parametrize("edge_kind", SEMANTIC_EDGE_KINDS_UNDER_TEST)
def test_external_payload_semantic_edges_require_evidence(
    tmp_path: Path, edge_kind: str
) -> None:
    repo_root = _sample_repo(tmp_path)
    frontend = _frontend(supported_edge_kinds=(edge_kind,))
    payload = _payload_with_edge_kind(edge_kind)
    payload["edges"][0]["evidence"] = []

    with pytest.raises(ExternalExtractorValidationError, match="missing evidence"):
        external_payload_to_fragment(
            payload,
            repo_root=repo_root,
            expected_frontend=frontend.capabilities(),
            supported_node_kinds={"module", "function"},
            supported_edge_kinds={edge_kind},
        )


@pytest.mark.parametrize("edge_kind", SEMANTIC_EDGE_KINDS_UNDER_TEST)
def test_external_payload_semantic_edges_require_resolution_strategy(
    tmp_path: Path, edge_kind: str
) -> None:
    repo_root = _sample_repo(tmp_path)
    frontend = _frontend(supported_edge_kinds=(edge_kind,))
    payload = _payload_with_edge_kind(edge_kind)
    payload["edges"][0]["resolution"] = {"status": "resolved"}

    with pytest.raises(
        ExternalExtractorValidationError, match="missing resolution.strategy"
    ):
        external_payload_to_fragment(
            payload,
            repo_root=repo_root,
            expected_frontend=frontend.capabilities(),
            supported_node_kinds={"module", "function"},
            supported_edge_kinds={edge_kind},
        )


def test_external_frontend_path_escape_is_warning(tmp_path: Path) -> None:
    payload = _payload()
    payload["nodes"][0]["path"] = "../outside.mock"

    reader = _build_with_frontend(
        tmp_path, _frontend(_write_payload(tmp_path, payload))
    )

    messages = [warning.message for warning in reader.read_warnings()]
    assert any("outside the repository" in item for item in messages)


def test_external_frontend_payload_file_size_guard_blocks_l3(
    tmp_path: Path,
) -> None:
    payload_path = _write_payload(tmp_path, _payload())

    reader = _build_with_frontend(
        tmp_path,
        _frontend(payload_path, max_payload_bytes=8),
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    assert "external_extractor_output_too_large" in warnings
    assert (
        reader.metadata["toolchain_status"]["mock-external"]["status"]
        == "invalid_output"
    )
    assert reader.metadata["language_tiers"].get("mocklang", {}).get("tier") != "L3"


def test_external_frontend_live_stdout_size_guard_blocks_l3(
    tmp_path: Path,
) -> None:
    script = tmp_path / "large_stdout.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({json.dumps(json.dumps(_payload()))!r} + ' ' * 2048)\n",
        encoding="utf-8",
    )

    reader = _build_with_frontend(
        tmp_path,
        _frontend(
            None,
            command=[sys.executable, str(script)],
            max_stdout_bytes=128,
        ),
    )

    warnings = {warning.kind for warning in reader.read_warnings()}
    assert "external_extractor_output_too_large" in warnings
    assert (
        reader.metadata["toolchain_status"]["mock-external"]["status"]
        == "invalid_output"
    )
    assert reader.metadata["language_tiers"].get("mocklang", {}).get("tier") != "L3"


def test_external_frontend_uses_explicit_repo_root_for_live_request(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    source_dir = repo_root / "packages" / "service" / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "app.mock").write_text("fn run {}\n", encoding="utf-8")
    script = tmp_path / "echo_payload.py"
    script.write_text(
        """
import json
import sys

request = json.loads(sys.stdin.read())
path = request["files"][0]["path"]
payload = {
    "schema_version": "1",
    "extractor": {"name": "mock-external", "version": "0.1"},
    "language": "mocklang",
    "capabilities": {
        "tier": "L3",
        "supported_node_kinds": ["module", "function"],
        "supported_edge_kinds": ["defines"],
    },
    "nodes": [
        {
            "id": "mod:deep.app",
            "kind": "module",
            "name": "app",
            "qualname": "deep.app",
            "path": path,
            "properties": {"language": "mocklang"},
        },
        {
            "id": "fn:deep.app.run",
            "kind": "function",
            "name": "run",
            "qualname": "deep.app.run",
            "path": path,
            "properties": {"language": "mocklang"},
        },
    ],
    "edges": [
        {
            "source": "mod:deep.app",
            "target": "fn:deep.app.run",
            "kind": "defines",
            "confidence": "confirmed",
            "resolution": {"status": "resolved", "strategy": "mock_symbol_table"},
            "evidence": [{"kind": "mock_definition", "path": path}],
        }
    ],
    "metrics": {"request_repo_root": request["repo_root"]},
}
print(json.dumps(payload))
""".strip(),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"

    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("packages/service/src")],
        frontend_registry=FrontendRegistry(
            [
                _frontend(
                    None,
                    command=[sys.executable, str(script)],
                    repo_root=repo_root,
                )
            ]
        ),
    ).build()

    reader = GraphStoreReader.from_current(output_dir)

    assert reader.metadata["adapter_metrics"]["mock-external"][
        "request_repo_root"
    ] == str(repo_root.resolve())
    assert reader.metadata["language_tiers"]["mocklang"]["tier"] == "L3"


def test_external_frontend_preserves_l2_l3_language_tier_distinction(
    tmp_path: Path,
) -> None:
    reader = _build_with_frontend(
        tmp_path,
        _frontend(_write_payload(tmp_path, _payload())),
    )

    tiers = reader.metadata["language_tiers"]
    assert tiers["mocklang"]["tier"] == "L3"
    assert tiers["go"]["tier"] == "L2"
    assert tiers["go"]["status"] == "requires_explicit_scip_graph_index"


def test_external_payload_direct_validation_rejects_bad_schema(tmp_path: Path) -> None:
    repo_root = _sample_repo(tmp_path)
    frontend = _frontend(_write_payload(tmp_path, _payload()))
    payload = _payload(schema_version="0")

    try:
        external_payload_to_fragment(
            payload,
            repo_root=repo_root,
            expected_frontend=frontend.capabilities(),
            supported_node_kinds={"module", "function"},
            supported_edge_kinds={"defines"},
        )
    except ExternalExtractorValidationError as exc:
        assert "schema_version" in str(exc)
    else:
        raise AssertionError("expected ExternalExtractorValidationError")
