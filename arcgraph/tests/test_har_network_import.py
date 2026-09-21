from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import IndexMetadata, Node
from arcgraph.core.visual_slice_engine import VisualSliceEngine, VisualSliceLimits
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.providers.context_provider import ContextProvider

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
_COPIED_FIXTURE_IGNORE = shutil.ignore_patterns(
    "output",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)


def test_trace_import_har_imports_route_request_and_redacts_network_data(
    tmp_path: Path,
    capsys: Any,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    har_path = tmp_path / "network.har"
    _build_fixture_index(repo_root, output_dir)
    har_path.write_text(
        json.dumps(
            _har_payload(
                [
                    _har_entry(
                        "GET",
                        "https://app.example.test/hello?name=Ada&token=secret",
                        status=200,
                        headers=[{"name": "Authorization", "value": "Bearer secret"}],
                        cookies=[{"name": "session", "value": "secret"}],
                        post_data={"text": "do-not-persist"},
                    )
                ]
            )
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "trace",
            "import-har",
            str(har_path),
            "--max-entries",
            "10",
            "--max-file-bytes",
            str(1024 * 1024),
            "--max-seconds",
            "120",
        ]
    )
    payload = _last_cli_json(capsys.readouterr().out)
    current = QueryEngine(output_dir).current()
    nodes = GraphStoreReader.from_current(output_dir).read_nodes()
    edges = GraphStoreReader.from_current(output_dir).read_edges()
    session_node = next(node for node in nodes if node.kind == "runtime_session")
    runtime_edge = next(
        edge
        for edge in edges
        if edge.source == session_node.id
        and edge.target == "route:GET:/hello"
        and edge.confidence == "runtime-only"
    )
    serialized_edge = json.dumps(runtime_edge.model_dump(mode="json"))

    assert exit_code == 0
    assert payload["action"] == "har_network_imported"
    assert payload["runtime_metrics"]["source"] == "har_network"
    assert payload["runtime_metrics"]["har_entries_total"] == 1
    assert payload["runtime_metrics"]["har_entries_converted"] == 1
    assert current["capabilities"]["runtime_trace"] in {"available", "partial"}
    assert current["runtime_metrics"]["source"] == "har_network"
    assert current["runtime_metrics"]["har_entries_converted"] == 1
    assert runtime_edge.kind == "invokes"
    assert runtime_edge.resolution.status == "runtime-only"
    assert runtime_edge.resolution.strategy == "har_network_import"
    assert any(
        item.kind == "runtime_trace_har_request" for item in runtime_edge.evidence
    )
    assert "name=Ada" not in serialized_edge
    assert "token=secret" not in serialized_edge
    assert "Authorization" not in serialized_edge
    assert "Bearer secret" not in serialized_edge
    assert "session=secret" not in serialized_edge
    assert "do-not-persist" not in serialized_edge


def test_trace_import_har_read_side_surfaces_show_runtime_network_evidence(
    tmp_path: Path,
    capsys: Any,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    har_path = tmp_path / "network.har"
    _build_fixture_index(repo_root, output_dir)
    har_path.write_text(
        json.dumps(_har_payload([_har_entry("GET", "https://app.example.test/hello")])),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--repo-root",
                str(repo_root),
                "--output-dir",
                str(output_dir),
                "trace",
                "import-har",
                str(har_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    engine = QueryEngine(output_dir)
    provider = ContextProvider(engine)
    explain = provider.explain(
        _context_request(["route:GET:/hello"], detail_level="detailed")
    )
    callers = provider.compact_callers(
        "route:GET:/hello",
        _context_request(["route:GET:/hello"], detail_level="detailed"),
    )
    visual_with_runtime = VisualSliceEngine(engine).build_slice(
        "impact_radius",
        target="route:GET:/hello",
        include_runtime=True,
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )
    visual_without_runtime = VisualSliceEngine(engine).build_slice(
        "impact_radius",
        target="route:GET:/hello",
        include_runtime=False,
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )

    explain_edge = _runtime_edge(explain["explanations"][0]["incoming_edges"])
    caller_edge = _runtime_edge(callers["edges"])
    assert caller_edge == explain_edge
    assert explain_edge["confidence"] == "runtime-only"
    assert explain_edge["resolution"]["strategy"] == "har_network_import"
    assert explain_edge["evidence"][0]["kind"] == "runtime_trace_har_request"
    assert "properties" not in explain_edge
    assert not _contains_key(explain, "headers")
    assert not _contains_key(explain, "cookies")
    assert not _contains_key(explain, "postData")
    assert any(
        edge["confidence"] == "runtime-only" for edge in visual_with_runtime["edges"]
    )
    assert all(
        edge.get("confidence") != "runtime-only"
        for edge in visual_without_runtime["edges"]
    )


def test_trace_import_har_reports_ambiguous_unmatched_and_non_http_entries(
    tmp_path: Path,
    capsys: Any,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    output_dir = tmp_path / "arcgraph"
    _write_route_store(
        output_dir,
        repo_root,
        [
            Node(id="route:GET:/shared", kind="route", name="GET /shared"),
            Node(id="route:GET:/shared/", kind="route", name="GET /shared/"),
            Node(id="route:POST:/submit", kind="route", name="POST /submit"),
        ],
    )
    har_path = tmp_path / "network.har"
    har_path.write_text(
        json.dumps(
            _har_payload(
                [
                    _har_entry("GET", "https://app.example.test/shared?secret=1"),
                    _har_entry("GET", "https://app.example.test/missing"),
                    _har_entry("GET", "data:text/plain,hello"),
                    _har_entry("POST", "https://app.example.test/submit"),
                ]
            )
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--repo-root",
                str(repo_root),
                "--output-dir",
                str(output_dir),
                "trace",
                "import-har",
                str(har_path),
            ]
        )
        == 0
    )
    payload = _last_cli_json(capsys.readouterr().out)
    warnings = GraphStoreReader.from_current(output_dir).read_warnings()
    edges = GraphStoreReader.from_current(output_dir).read_edges()

    assert payload["status"] == "partial"
    assert payload["runtime_metrics"]["har_entries_total"] == 4
    assert payload["runtime_metrics"]["har_entries_converted"] == 1
    assert payload["runtime_metrics"]["har_entries_ambiguous"] == 1
    assert payload["runtime_metrics"]["har_entries_unmatched"] == 1
    assert payload["runtime_metrics"]["har_entries_non_http"] == 1
    assert {
        "runtime_trace_har_route_ambiguous",
        "runtime_trace_har_route_unmatched",
        "runtime_trace_har_non_http_url",
    }.issubset({warning.kind for warning in warnings})
    assert not any(
        edge.target in {"route:GET:/shared", "route:GET:/shared/"} for edge in edges
    )
    assert any(edge.target == "route:POST:/submit" for edge in edges)


def test_trace_import_har_limits_malformed_empty_and_oversized_inputs(
    tmp_path: Path,
    capsys: Any,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    _build_fixture_index(repo_root, output_dir)

    limited_path = tmp_path / "limited.har"
    limited_path.write_text(
        json.dumps(
            _har_payload(
                [
                    _har_entry("GET", "https://app.example.test/hello"),
                    _har_entry("POST", "https://app.example.test/memories"),
                ]
            )
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--repo-root",
                str(repo_root),
                "--output-dir",
                str(output_dir),
                "trace",
                "import-har",
                str(limited_path),
                "--max-entries",
                "1",
            ]
        )
        == 0
    )
    limited_payload = _last_cli_json(capsys.readouterr().out)
    limited_warnings = GraphStoreReader.from_current(output_dir).read_warnings()
    assert limited_payload["status"] == "partial"
    assert limited_payload["runtime_metrics"]["har_entries_total"] == 2
    assert limited_payload["runtime_metrics"]["har_entries_processed"] == 1
    assert limited_payload["runtime_metrics"]["har_entries_limited"] == 1
    assert "runtime_trace_har_entry_limit_exceeded" in {
        warning.kind for warning in limited_warnings
    }

    malformed_path = tmp_path / "malformed.har"
    malformed_path.write_text("{not json", encoding="utf-8")
    assert (
        main(
            [
                "--repo-root",
                str(repo_root),
                "--output-dir",
                str(output_dir),
                "trace",
                "import-har",
                str(malformed_path),
            ]
        )
        == 0
    )
    malformed_payload = _last_cli_json(capsys.readouterr().out)
    assert malformed_payload["runtime_metrics"]["har_parse_errors"] == 1

    empty_path = tmp_path / "empty.har"
    empty_path.write_text(json.dumps(_har_payload([])), encoding="utf-8")
    assert (
        main(
            [
                "--repo-root",
                str(repo_root),
                "--output-dir",
                str(output_dir),
                "trace",
                "import-har",
                str(empty_path),
            ]
        )
        == 0
    )
    empty_payload = _last_cli_json(capsys.readouterr().out)
    assert empty_payload["runtime_metrics"]["har_entries_total"] == 0
    assert empty_payload["status"] == "partial"

    oversized_path = tmp_path / "oversized.har"
    oversized_path.write_text(json.dumps(_har_payload([])), encoding="utf-8")
    assert (
        main(
            [
                "--repo-root",
                str(repo_root),
                "--output-dir",
                str(output_dir),
                "trace",
                "import-har",
                str(oversized_path),
                "--max-file-bytes",
                "2",
            ]
        )
        == 0
    )
    oversized_payload = _last_cli_json(capsys.readouterr().out)
    assert oversized_payload["runtime_metrics"]["har_too_large"] == 1


def _build_fixture_index(repo_root: Path, output_dir: Path) -> None:
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()


def _copy_fixture(tmp_path: Path) -> Path:
    repo_root = tmp_path / "sample_project"
    shutil.copytree(FIXTURE_ROOT, repo_root, ignore=_COPIED_FIXTURE_IGNORE)
    return repo_root


def _write_route_store(output_dir: Path, repo_root: Path, nodes: list[Node]) -> None:
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="har-test",
            repo_root=str(repo_root),
            source_roots=["."],
            commit_sha=None,
        ),
        files=[],
        nodes=nodes,
        edges=[],
        warnings=[],
    )


def _har_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"log": {"version": "1.2", "creator": {"name": "test"}, "entries": entries}}


def _har_entry(
    method: str,
    url: str,
    *,
    status: int = 200,
    headers: list[dict[str, str]] | None = None,
    cookies: list[dict[str, str]] | None = None,
    post_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "request": {
            "method": method,
            "url": url,
            "headers": headers or [],
            "cookies": cookies or [],
            "postData": post_data,
        },
        "response": {
            "status": status,
            "headers": [],
            "cookies": [],
            "content": {"text": "secret"},
        },
        "time": 12,
    }


def _context_request(targets: list[str], *, detail_level: str) -> Any:
    from arcgraph.core.schemas import ContextRequest

    return ContextRequest(targets=targets, detail_level=detail_level, max_results=30)


def _runtime_edge(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return next(edge for edge in edges if edge.get("confidence") == "runtime-only")


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _last_cli_json(output: str) -> dict[str, Any]:
    for match in reversed(list(re.finditer(r"(?m)^\{", output))):
        try:
            payload = json.loads(output[match.start() :].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError(f"No CLI JSON payload found in output:\n{output}")
