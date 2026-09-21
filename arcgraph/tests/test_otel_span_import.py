from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
_COPIED_FIXTURE_IGNORE = shutil.ignore_patterns(
    "output",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)


def test_trace_import_otel_imports_mappable_span_and_redacts_attributes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    otel_path = tmp_path / "otel-spans.json"
    _build_fixture_index(repo_root, output_dir)
    otel_path.write_text(
        json.dumps(
            {
                "resourceSpans": [
                    {
                        "scopeSpans": [
                            {
                                "spans": [
                                    _otel_span(
                                        source="fn:pkg.api.create_memory",
                                        target="fn:pkg.service.build_message",
                                        path="src/pkg/api.py",
                                        line=21,
                                        extra_attributes={
                                            "http.request.header.authorization": "secret",
                                            "request.body": "do-not-persist",
                                            "cookie": "session=secret",
                                        },
                                    )
                                ]
                            }
                        ]
                    }
                ]
            }
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
            "import-otel",
            str(otel_path),
            "--max-spans",
            "10",
            "--max-file-bytes",
            str(1024 * 1024),
            "--max-seconds",
            "120",
        ]
    )
    payload = _last_cli_json(capsys.readouterr().out)
    current = QueryEngine(output_dir).current()
    edges = GraphStoreReader.from_current(output_dir).read_edges()
    runtime_edge = next(
        edge
        for edge in edges
        if edge.source == "fn:pkg.api.create_memory"
        and edge.target == "fn:pkg.service.build_message"
        and edge.confidence == "runtime-only"
    )
    serialized_edge = json.dumps(runtime_edge.model_dump(mode="json"))

    assert exit_code == 0
    assert payload["action"] == "otel_span_imported"
    assert payload["runtime_metrics"]["source"] == "opentelemetry"
    assert payload["runtime_metrics"]["otel_spans_total"] == 1
    assert payload["runtime_metrics"]["otel_spans_converted"] == 1
    assert current["capabilities"]["runtime_trace"] in {"available", "partial"}
    assert current["runtime_metrics"]["source"] == "opentelemetry"
    runtime_artifact = _artifact(
        current["evidence_manifest"]["artifacts"], "runtime_trace"
    )
    assert runtime_artifact["tool_name"] == "opentelemetry"
    assert runtime_artifact["commit_sha"] == current["commit_sha"]
    assert runtime_edge.resolution.strategy == "runtime_trace"
    assert any(item.kind == "runtime_trace_call" for item in runtime_edge.evidence)
    assert "secret" not in serialized_edge
    assert "authorization" not in serialized_edge
    assert "request.body" not in serialized_edge
    assert "cookie" not in serialized_edge


def test_trace_import_otel_accepts_jsonl_and_enforces_span_limit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    otel_path = tmp_path / "otel-spans.jsonl"
    _build_fixture_index(repo_root, output_dir)
    otel_path.write_text(
        "\n".join(
            [
                json.dumps(
                    _otel_span(
                        source="fn:pkg.api.create_memory",
                        target="fn:pkg.service.build_message",
                        path="src/pkg/api.py",
                        line=21,
                    )
                ),
                json.dumps(
                    _otel_span(
                        source="fn:pkg.api.create_memory",
                        target="fn:pkg.service.build_message",
                        path="src/pkg/api.py",
                        line=22,
                    )
                ),
            ]
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
            "import-otel",
            str(otel_path),
            "--max-spans",
            "1",
        ]
    )
    payload = _last_cli_json(capsys.readouterr().out)
    warnings = GraphStoreReader.from_current(output_dir).read_warnings()

    assert exit_code == 0
    assert payload["status"] == "partial"
    assert payload["runtime_metrics"]["otel_spans_total"] == 2
    assert payload["runtime_metrics"]["otel_spans_processed"] == 1
    assert payload["runtime_metrics"]["otel_spans_limited"] == 1
    assert "runtime_trace_otel_span_limit_exceeded" in {
        warning.kind for warning in warnings
    }


def test_trace_import_otel_reports_unmapped_malformed_and_outside_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    _build_fixture_index(repo_root, output_dir)

    unmapped_path = tmp_path / "otel-unmapped.json"
    unmapped_path.write_text(
        json.dumps(
            {
                "spans": [
                    _otel_span(
                        source="fn:pkg.api.create_memory",
                        target="fn:pkg.service.build_message",
                        path=str((tmp_path / "outside.py").resolve()),
                        line=21,
                    ),
                    _otel_span(
                        source="fn:pkg.api.create_memory",
                        target="fn:pkg.missing.nope",
                        path="src/pkg/api.py",
                        line=22,
                    ),
                ]
            }
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
            "import-otel",
            str(unmapped_path),
        ]
    )
    payload = _last_cli_json(capsys.readouterr().out)
    warnings = GraphStoreReader.from_current(output_dir).read_warnings()

    assert exit_code == 0
    assert payload["status"] == "partial"
    assert payload["runtime_metrics"]["otel_spans_total"] == 2
    assert payload["runtime_metrics"]["otel_spans_converted"] == 2
    assert payload["runtime_metrics"]["unresolved_events"] == 1
    assert "runtime_trace_path_outside_repo" in {warning.kind for warning in warnings}
    assert str(tmp_path / "outside.py") not in json.dumps(
        [
            edge.model_dump(mode="json")
            for edge in GraphStoreReader.from_current(output_dir).read_edges()
        ]
    )

    malformed_path = tmp_path / "otel-malformed.json"
    malformed_path.write_text("{not json", encoding="utf-8")
    malformed_exit = main(
        [
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "trace",
            "import-otel",
            str(malformed_path),
        ]
    )
    malformed_payload = _last_cli_json(capsys.readouterr().out)
    malformed_warnings = GraphStoreReader.from_current(output_dir).read_warnings()

    assert malformed_exit == 0
    assert malformed_payload["status"] == "partial"
    assert malformed_payload["runtime_metrics"]["otel_parse_errors"] == 1
    assert "runtime_trace_otel_parse_error" in {
        warning.kind for warning in malformed_warnings
    }

    oversized_path = tmp_path / "otel-oversized.json"
    oversized_path.write_text(json.dumps({"spans": []}), encoding="utf-8")
    oversized_exit = main(
        [
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "trace",
            "import-otel",
            str(oversized_path),
            "--max-file-bytes",
            "2",
        ]
    )
    oversized_payload = _last_cli_json(capsys.readouterr().out)
    oversized_warnings = GraphStoreReader.from_current(output_dir).read_warnings()

    assert oversized_exit == 0
    assert oversized_payload["status"] == "partial"
    assert oversized_payload["runtime_metrics"]["otel_too_large"] == 1
    assert "runtime_trace_otel_too_large" in {
        warning.kind for warning in oversized_warnings
    }


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


def _otel_span(
    *,
    source: str,
    target: str,
    path: str,
    line: int,
    extra_attributes: dict[str, str] | None = None,
) -> dict[str, Any]:
    attributes = {
        "arcgraph.source": source,
        "arcgraph.target": target,
        "arcgraph.event_kind": "call",
        "code.filepath": path,
        "code.lineno": line,
    }
    attributes.update(extra_attributes or {})
    return {
        "traceId": "trace-1",
        "spanId": f"span-{line}",
        "name": "ArcGraph runtime evidence",
        "attributes": [
            {"key": key, "value": _otel_value(value)}
            for key, value in attributes.items()
        ],
    }


def _otel_value(value: object) -> dict[str, object]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _last_cli_json(output: str) -> dict[str, Any]:
    for match in reversed(list(re.finditer(r"(?m)^\{", output))):
        try:
            payload = json.loads(output[match.start() :].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError(f"No CLI JSON payload found in output:\n{output}")


def _artifact(artifacts: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return next(artifact for artifact in artifacts if artifact.get("kind") == kind)
