from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import ContextRequest
from arcgraph.core.visual_slice_engine import VisualSliceEngine, VisualSliceLimits
from arcgraph.interfaces.docs import render_docs
from arcgraph.interfaces.trace import (
    import_har_network,
    import_otel_spans,
    import_runtime_trace,
)
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


def test_runtime_otel_har_evidence_import_matrix_contracts(tmp_path: Path) -> None:
    cases = [
        ("runtime_trace", _runtime_trace_case(tmp_path / "runtime_trace")),
        ("otel", _otel_case(tmp_path / "otel")),
        ("har", _har_case(tmp_path / "har")),
    ]

    for name, case in cases:
        repo_root = _copy_fixture(tmp_path / name)
        output_dir = tmp_path / name / "arcgraph"
        _build_fixture_index(repo_root, output_dir)

        result = case["importer"](
            repo_root=repo_root,
            output_dir=output_dir,
            **case["kwargs"],
        )
        reader = GraphStoreReader.from_current(output_dir)
        current = QueryEngine(output_dir).current()
        runtime_edges = [
            edge
            for edge in reader.read_edges()
            if edge.confidence == "runtime-only"
            and edge.resolution.strategy == case["strategy"]
        ]
        serialized = json.dumps(
            [edge.model_dump(mode="json") for edge in runtime_edges], sort_keys=True
        )

        assert result["status"] in {"available", "partial"}
        assert current["capabilities"]["runtime_trace"] in {"available", "partial"}
        assert current["runtime_metrics"]["source"] == case["source"]
        assert runtime_edges
        assert any(
            item.kind == case["evidence_kind"]
            for edge in runtime_edges
            for item in edge.evidence
        )
        assert all(edge.resolution.status == "runtime-only" for edge in runtime_edges)
        assert all(
            edge.resolution.strategy == case["strategy"] for edge in runtime_edges
        )
        assert all(edge.properties.get("headers") is None for edge in runtime_edges)
        assert "secret" not in serialized
        assert "authorization" not in serialized
        assert "body" not in serialized


def test_coverage_evidence_import_matrix_contracts(tmp_path: Path) -> None:
    cases = _coverage_cases(tmp_path)

    for name, repo_root, coverage_path, format_name, evidence_kind in cases:
        ArcGraphIndexer(
            repo_root=repo_root,
            output_dir=tmp_path / name / "arcgraph",
            source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
            coverage_path=coverage_path,
        ).build()
        reader = GraphStoreReader.from_current(tmp_path / name / "arcgraph")
        current = QueryEngine(tmp_path / name / "arcgraph").current()
        coverage_edges = [
            edge
            for edge in reader.read_edges()
            if edge.kind == "covers" and edge.confidence == "runtime-only"
        ]
        serialized = json.dumps(
            [edge.model_dump(mode="json") for edge in coverage_edges], sort_keys=True
        )

        assert current["capabilities"]["coverage"] == "available"
        assert current["coverage_metrics"]["format"] == format_name
        assert current["coverage_metrics"]["edges_imported"] > 0
        assert coverage_edges
        assert any(
            item.kind == evidence_kind
            for edge in coverage_edges
            for item in edge.evidence
        )
        assert "token=secret" not in serialized
        assert "source text should not persist" not in serialized
        assert "headers" not in serialized
        assert "cookies" not in serialized


def test_bad_evidence_inputs_stay_partial_without_fabricated_edges(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    _build_fixture_index(repo_root, output_dir)

    outside_path = (tmp_path / "outside.py").resolve()
    runtime_path = tmp_path / "runtime-outside.json"
    runtime_path.write_text(
        json.dumps(
            {
                "trace_run": {
                    "trace_run_id": "bad-runtime",
                    "source": "pytest",
                    "frontend_version": "ArcGraph-runtime-trace-v1",
                },
                "events": [
                    {
                        "kind": "call",
                        "source": "fn:pkg.api.create_memory",
                        "target": "fn:pkg.service.build_message",
                        "path": str(outside_path),
                        "line": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime_result = import_runtime_trace(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=runtime_path,
    )

    har_path = tmp_path / "bad.har"
    har_path.write_text(
        json.dumps(
            {
                "log": {
                    "version": "1.2",
                    "entries": [
                        {
                            "request": {
                                "method": "GET",
                                "url": "https://app.example.test/missing?secret=1",
                            },
                            "response": {"status": 404},
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    har_result = import_har_network(
        repo_root=repo_root,
        output_dir=output_dir,
        har_path=har_path,
    )

    coverage_path = tmp_path / "bad-browser-coverage.json"
    coverage_path.write_text(
        json.dumps(
            [
                {"url": "file:///C:/outside/project/app.ts", "ranges": []},
                {"url": "http://localhost:5173/src/missing.ts", "ranges": []},
            ]
        ),
        encoding="utf-8",
    )
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=tmp_path / "coverage-arcgraph",
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        coverage_path=coverage_path,
    ).build()
    coverage_reader = GraphStoreReader.from_current(tmp_path / "coverage-arcgraph")
    warning_kinds = {
        *(
            warning.kind
            for warning in GraphStoreReader.from_current(output_dir).read_warnings()
        ),
        *(warning.kind for warning in coverage_reader.read_warnings()),
    }
    runtime_edges = [
        edge
        for edge in GraphStoreReader.from_current(output_dir).read_edges()
        if edge.confidence == "runtime-only"
    ]
    coverage_edges = [
        edge
        for edge in coverage_reader.read_edges()
        if edge.kind == "covers"
        and edge.properties.get("coverage_source") == "browser_coverage"
    ]
    serialized_runtime = json.dumps(
        [edge.model_dump(mode="json") for edge in runtime_edges], sort_keys=True
    )

    assert runtime_result["status"] == "partial"
    assert har_result["status"] == "partial"
    assert "runtime_trace_path_outside_repo" in warning_kinds
    assert "runtime_trace_har_route_unmatched" in warning_kinds
    assert "coverage_browser_unsupported_url" in warning_kinds
    assert "coverage_browser_unmatched_url" in warning_kinds
    assert not coverage_edges
    assert str(outside_path) not in serialized_runtime
    assert "secret=1" not in serialized_runtime


def test_evidence_import_matrix_read_side_surfaces_are_compact_and_safe(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    _build_fixture_index(repo_root, output_dir)
    runtime_case = _runtime_trace_case(tmp_path)
    runtime_case["importer"](
        repo_root=repo_root,
        output_dir=output_dir,
        **runtime_case["kwargs"],
    )
    provider = ContextProvider(QueryEngine(output_dir))
    request = ContextRequest(
        targets=["pkg.service.build_message"],
        detail_level="detailed",
        max_results=30,
    )

    explain = provider.explain(request)
    context = provider.get_context(request)
    callers = provider.compact_callers("pkg.service.build_message", request)
    callees = provider.compact_callees("pkg.api.create_memory", request)
    impact = provider.compact_impact("pkg.service.build_message", request)
    visual_with_runtime = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "impact_radius",
        target="fn:pkg.service.build_message",
        include_runtime=True,
        limits=VisualSliceLimits(max_nodes=20, max_edges=20),
    )
    visual_without_runtime = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "impact_radius",
        target="fn:pkg.service.build_message",
        include_runtime=False,
        limits=VisualSliceLimits(max_nodes=20, max_edges=20),
    )

    runtime_edge = _runtime_edge(explain["explanations"][0]["incoming_edges"])
    assert runtime_edge["resolution"]["strategy"] == "runtime_trace"
    assert runtime_edge["evidence"][0]["kind"] == "runtime_trace_call"
    assert _runtime_edge(callers["edges"]) == runtime_edge
    assert _runtime_edge(callees["edges"]) == runtime_edge
    assert _runtime_edge(impact["call_impact"]["edges"]) == runtime_edge
    assert context["grouped_effects"]["runtime_only"]
    assert any(
        edge["confidence"] == "runtime-only" for edge in visual_with_runtime["edges"]
    )
    assert all(
        edge.get("confidence") != "runtime-only"
        for edge in visual_without_runtime["edges"]
    )
    for payload in (explain, context, callers, callees, impact):
        assert not _contains_key(payload, "properties")
        assert not _contains_key(payload, "snippet")
        assert not _contains_key(payload, "source_snippet")
        assert "secret" not in json.dumps(payload, sort_keys=True)
    assert "secret" not in json.dumps(visual_with_runtime, sort_keys=True)


def test_evidence_import_matrix_docs_are_exposed() -> None:
    cookbook = render_docs("evidence-cookbook")
    limitations = render_docs("limitations")

    assert "evidence import matrix" in cookbook.lower()
    assert "runtime trace" in cookbook
    assert "OpenTelemetry" in cookbook
    assert "HAR" in cookbook
    assert "Istanbul" in cookbook
    assert "Playwright/Chrome" in cookbook
    assert "explicit offline import" in limitations.lower()
    assert "source-map" in limitations
    assert "browser call-chain proof" in limitations


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


def _runtime_trace_case(tmp_path: Path) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    trace_path = tmp_path / "runtime-trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "trace_run": {
                    "trace_run_id": "matrix-runtime",
                    "source": "pytest",
                    "frontend_version": "ArcGraph-runtime-trace-v1",
                },
                "events": [
                    {
                        "kind": "call",
                        "source": "fn:pkg.api.create_memory",
                        "target": "fn:pkg.service.build_message",
                        "path": "src/pkg/api.py",
                        "line": 21,
                        "headers": {"authorization": "secret"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "importer": import_runtime_trace,
        "kwargs": {"trace_path": trace_path},
        "source": "pytest",
        "strategy": "runtime_trace",
        "evidence_kind": "runtime_trace_call",
    }


def _otel_case(tmp_path: Path) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    span_path = tmp_path / "otel-spans.jsonl"
    span_path.write_text(
        json.dumps(
            {
                "traceId": "trace-1",
                "spanId": "span-1",
                "name": "matrix span",
                "attributes": [
                    {
                        "key": "arcgraph.source",
                        "value": {"stringValue": "fn:pkg.api.create_memory"},
                    },
                    {
                        "key": "arcgraph.target",
                        "value": {"stringValue": "fn:pkg.service.build_message"},
                    },
                    {"key": "arcgraph.event_kind", "value": {"stringValue": "call"}},
                    {
                        "key": "code.filepath",
                        "value": {"stringValue": "src/pkg/api.py"},
                    },
                    {"key": "code.lineno", "value": {"intValue": 21}},
                    {
                        "key": "http.request.header.authorization",
                        "value": {"stringValue": "secret"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "importer": import_otel_spans,
        "kwargs": {"span_path": span_path},
        "source": "opentelemetry",
        "strategy": "runtime_trace",
        "evidence_kind": "runtime_trace_call",
    }


def _har_case(tmp_path: Path) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    har_path = tmp_path / "network.har"
    har_path.write_text(
        json.dumps(
            {
                "log": {
                    "version": "1.2",
                    "entries": [
                        {
                            "request": {
                                "method": "GET",
                                "url": "https://app.example.test/hello?token=secret",
                                "headers": [
                                    {"name": "Authorization", "value": "secret"}
                                ],
                                "cookies": [{"name": "session", "value": "secret"}],
                            },
                            "response": {"status": 200, "content": {"text": "secret"}},
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    return {
        "importer": import_har_network,
        "kwargs": {"har_path": har_path},
        "source": "har_network",
        "strategy": "har_network_import",
        "evidence_kind": "runtime_trace_har_request",
    }


def _custom_coverage_payload(repo_root: Path, coverage_path: Path) -> None:
    coverage_path.write_text(
        json.dumps(
            {
                "tests": [
                    {
                        "test": "fn:tests.service_cases.test_build_message",
                        "covers": [
                            {
                                "path": "src/pkg/service.py",
                                "symbols": ["fn:pkg.service.build_message"],
                                "lines": [9, 10],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _coverage_cases(tmp_path: Path) -> list[tuple[str, Path, Path, str, str]]:
    json_root = _copy_fixture(tmp_path / "json")
    json_path = tmp_path / "json" / "coverage.json"
    _custom_coverage_payload(json_root, json_path)

    cobertura_root = _copy_fixture(tmp_path / "cobertura")
    cobertura_path = tmp_path / "cobertura" / "coverage.xml"
    _cobertura_payload(cobertura_root, cobertura_path)

    istanbul_root = _copy_fixture(tmp_path / "istanbul")
    istanbul_path = tmp_path / "istanbul" / "coverage.json"
    _istanbul_payload(istanbul_root, istanbul_path)

    browser_text_root = _copy_fixture(tmp_path / "browser_with_text")
    browser_text_path = tmp_path / "browser_with_text" / "coverage.json"
    _browser_text_payload(browser_text_root, browser_text_path)

    browser_file_root = _copy_fixture(tmp_path / "browser_file_only")
    browser_file_path = tmp_path / "browser_file_only" / "coverage.json"
    _browser_file_payload(browser_file_root, browser_file_path)

    return [
        ("json", json_root, json_path, "json", "coverage_json"),
        ("cobertura", cobertura_root, cobertura_path, "cobertura", "coverage_xml"),
        ("istanbul", istanbul_root, istanbul_path, "istanbul", "coverage_istanbul"),
        (
            "browser_with_text",
            browser_text_root,
            browser_text_path,
            "browser_coverage",
            "coverage_browser",
        ),
        (
            "browser_file_only",
            browser_file_root,
            browser_file_path,
            "browser_coverage",
            "coverage_browser",
        ),
    ]


def _cobertura_payload(repo_root: Path, coverage_path: Path) -> None:
    coverage_path.write_text(
        """
<coverage>
  <packages>
    <package name="pkg">
      <classes>
        <class name="pkg.service" filename="src/pkg/service.py">
          <lines>
            <line number="9" hits="1" />
            <line number="10" hits="1" />
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
""".strip(),
        encoding="utf-8",
    )


def _istanbul_payload(repo_root: Path, coverage_path: Path) -> None:
    coverage_path.write_text(
        json.dumps(
            {
                "src/pkg/service.py": {
                    "path": "src/pkg/service.py",
                    "statementMap": {
                        "0": {
                            "start": {"line": 9, "column": 0},
                            "end": {"line": 10, "column": 24},
                        }
                    },
                    "s": {"0": 1},
                    "fnMap": {
                        "0": {
                            "name": "build_message",
                            "loc": {
                                "start": {"line": 9, "column": 0},
                                "end": {"line": 10, "column": 24},
                            },
                        }
                    },
                    "f": {"0": 1},
                }
            }
        ),
        encoding="utf-8",
    )


def _browser_text_payload(repo_root: Path, coverage_path: Path) -> None:
    source_text = (repo_root / "src" / "pkg" / "service.py").read_text(encoding="utf-8")
    coverage_path.write_text(
        json.dumps(
            [
                {
                    "url": "http://localhost:5173/src/pkg/service.py?token=secret#debug",
                    "text": source_text,
                    "ranges": [{"start": 0, "end": len(source_text)}],
                }
            ]
        ),
        encoding="utf-8",
    )


def _browser_file_payload(repo_root: Path, coverage_path: Path) -> None:
    coverage_path.write_text(
        json.dumps(
            [
                {
                    "url": "http://localhost:5173/src/pkg/service.py",
                    "ranges": [{"start": 0, "end": 10}],
                }
            ]
        ),
        encoding="utf-8",
    )


def _runtime_edge(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return next(edge for edge in edges if edge.get("confidence") == "runtime-only")


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False
