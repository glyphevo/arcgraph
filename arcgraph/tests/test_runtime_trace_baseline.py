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
from arcgraph.core.schemas import ContextRequest
from arcgraph.interfaces.ci import evidence_requirement_summary
from arcgraph.interfaces.cli import main
from arcgraph.interfaces.trace import import_runtime_trace
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.providers.context_provider import ContextProvider

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
EXPECTATIONS_PATH = (
    Path(__file__).parent / "golden" / "runtime_trace_baseline" / "expectations.json"
)
_COPIED_FIXTURE_IGNORE = shutil.ignore_patterns(
    "output",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)


def test_runtime_trace_cli_e2e_imports_pytest_trace_and_updates_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    trace_path = tmp_path / "pytest-runtime-trace.json"
    _build_fixture_index(repo_root, output_dir)

    run_exit = main(
        [
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "trace",
            "run",
            "--output",
            str(trace_path),
            "--legacy-roots",
            "--max-events",
            "200",
            "--max-seconds",
            "30",
            "--",
            "tests/service_cases.py::test_build_message",
            "-q",
        ]
    )
    run_payload = _last_cli_json(capsys.readouterr().out)
    trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))

    assert run_exit == 0
    assert run_payload["status"] == "pass"
    assert run_payload["events"] > 0
    assert trace_payload["trace_run"]["source"] == "pytest"
    assert "commit_sha" in trace_payload["trace_run"]
    assert "started_at" in trace_payload["trace_run"]
    assert trace_payload["trace_run"]["frontend_version"] == "ArcGraph-runtime-trace-v1"
    assert all(
        "args" not in event and "kwargs" not in event
        for event in trace_payload["events"]
    )
    assert any(
        event["source"] == "fn:tests.service_cases.test_build_message"
        and event["target"] == "fn:pkg.service.build_message"
        for event in trace_payload["events"]
    )

    import_exit = main(
        [
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "trace",
            "import",
            str(trace_path),
            "--max-events",
            "200",
            "--max-file-bytes",
            str(1024 * 1024),
            "--max-seconds",
            "120",
        ]
    )
    import_payload = _last_cli_json(capsys.readouterr().out)
    current = QueryEngine(output_dir).current()

    assert import_exit == 0
    assert import_payload["action"] == "runtime_trace_imported"
    assert current["capabilities"]["runtime_trace"] in {"available", "partial"}
    assert current["runtime_metrics"]["source"] == "pytest"
    assert current["runtime_metrics"]["events_total"] == len(trace_payload["events"])
    assert current["runtime_metrics"]["edges_imported"] > 0
    assert current["evidence_manifest"]["summary"]["total"] == 4

    ci_exit = main(
        [
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "ci",
            "--runtime-trace",
            str(trace_path),
            "--runtime-trace-max-seconds",
            "120",
        ]
    )
    ci_payload = _last_cli_json(capsys.readouterr().out)
    ci_checks = {check["name"]: check for check in ci_payload["checks"]}
    current_after_ci = QueryEngine(output_dir).current()
    runtime_requirement = evidence_requirement_summary(
        current_after_ci,
        require_python_full=True,
    )["requirements"]["runtime_trace"]

    assert ci_exit == 0
    assert ci_checks["runtime_trace_inputs"]["status"] == (
        runtime_requirement["check_status"]
    )

    status_exit = main(
        [
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "evidence",
            "status",
        ]
    )
    status_payload = _last_cli_json(capsys.readouterr().out)
    plan_exit = main(
        [
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "evidence",
            "plan",
            "--profile",
            "python_full",
        ]
    )
    plan_payload = _last_cli_json(capsys.readouterr().out)

    assert status_exit == 0
    assert plan_exit == 0
    assert status_payload["capability_summary"]["runtime_trace"] == (
        current_after_ci["capabilities"]["runtime_trace"]
    )
    runtime_artifact_status = _artifact_status(
        status_payload["snapshot_manifest"]["artifacts"],
        "runtime_trace",
    )
    assert runtime_artifact_status == current_after_ci["runtime_metrics"]["status"]
    assert (
        "runtime_trace" not in {action["kind"] for action in plan_payload["actions"]}
    ) == (runtime_requirement["check_status"] == "pass")


def test_runtime_trace_cli_e2e_records_fastapi_testclient_route(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    repo_root = _copy_fixture(tmp_path)
    _write_fastapi_smoke_case(repo_root)
    output_dir = tmp_path / "arcgraph"
    trace_path = tmp_path / "fastapi-runtime-trace.json"
    _build_fixture_index(repo_root, output_dir)

    run_exit = main(
        [
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "trace",
            "run",
            "--output",
            str(trace_path),
            "--legacy-roots",
            "--max-events",
            "1000",
            "--max-seconds",
            "30",
            "--",
            "tests/fastapi_smoke_cases.py::test_fastapi_hello_route",
            "-q",
        ]
    )
    run_payload = _last_cli_json(capsys.readouterr().out)
    trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
    events = trace_payload["events"]

    assert run_exit == 0
    assert run_payload["status"] == "pass"
    assert trace_payload["trace_run"]["source"] == "pytest"
    assert trace_payload["trace_run"]["frontend_version"] == "ArcGraph-runtime-trace-v1"
    assert events
    assert all(
        all(key not in event for key in ("args", "kwargs", "body", "headers"))
        for event in events
    )
    # TestClient may run the synchronous route body in an existing worker thread.
    # threading.setprofile only covers threads created after the hook is set, so
    # all worker-thread route edges are best-effort rather than a stable test oracle.
    assert all(not Path(event["path"]).is_absolute() for event in events)

    import_exit = main(
        [
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "trace",
            "import",
            str(trace_path),
            "--max-events",
            "1000",
            "--max-file-bytes",
            str(1024 * 1024),
            "--max-seconds",
            "120",
        ]
    )
    import_payload = _last_cli_json(capsys.readouterr().out)
    current = QueryEngine(output_dir).current()
    edges = GraphStoreReader.from_current(output_dir).read_edges()
    runtime_edges = [edge for edge in edges if edge.confidence == "runtime-only"]

    assert import_exit == 0
    assert import_payload["action"] == "runtime_trace_imported"
    assert current["capabilities"]["runtime_trace"] in {"available", "partial"}
    assert current["runtime_metrics"]["source"] == "pytest"
    assert current["runtime_metrics"]["edges_imported"] > 0
    assert runtime_edges
    assert all(edge.resolution.strategy == "runtime_trace" for edge in runtime_edges)
    assert any(
        item.kind == "runtime_trace_call"
        for edge in runtime_edges
        for item in edge.evidence
    )


def test_runtime_trace_golden_import_redacts_and_keeps_runtime_only_semantics(
    tmp_path: Path,
) -> None:
    expectations = _read_expectations()
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    outside_path = (tmp_path / "outside.py").resolve()
    trace_path = tmp_path / "runtime-trace-golden.json"
    trace_payload = {
        "trace_run": {
            "trace_run_id": "golden-run",
            "source": "pytest",
            "frontend_version": "ArcGraph-runtime-trace-v1",
        },
        "events": [
            {
                "kind": "dynamic_call",
                "source": "fn:pkg.api.create_memory",
                "target": "fn:pkg.service.build_message",
                "path": str(outside_path),
                "line": 21,
                "column": 17,
                "test_id": "tests/service_cases.py::test_build_message",
                "args": ["do-not-persist"],
                "headers": {"authorization": "secret"},
            },
            {
                "kind": "call",
                "source": "fn:pkg.api.create_memory",
                "target": "fn:pkg.missing.nope",
                "path": "src/pkg/api.py",
                "line": 22,
            },
        ],
    }
    trace_path.write_text(json.dumps(trace_payload), encoding="utf-8")

    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        runtime_trace_path=trace_path,
    ).build()
    current = QueryEngine(output_dir).current()
    warnings = GraphStoreReader.from_current(output_dir).read_warnings()
    edges = GraphStoreReader.from_current(output_dir).read_edges()
    expected_edge = expectations["expected_runtime_edge"]
    runtime_edge = next(
        edge
        for edge in edges
        if edge.source == expected_edge["source"]
        and edge.target == expected_edge["target"]
        and edge.kind == expected_edge["kind"]
        and any(item.kind == expected_edge["evidence_kind"] for item in edge.evidence)
    )

    assert (
        current["runtime_metrics"] | expectations["expected_metrics"]
        == current["runtime_metrics"]
    )
    assert runtime_edge.confidence == expected_edge["confidence"]
    assert runtime_edge.resolution.status == expected_edge["resolution_status"]
    assert runtime_edge.resolution.strategy == expected_edge["resolution_strategy"]
    assert {
        key
        for key in expectations["forbidden_edge_properties"]
        if key in runtime_edge.properties
    } == set()
    assert set(expectations["expected_warning_kinds"]).issubset(
        {warning.kind for warning in warnings}
    )


def test_runtime_trace_read_side_surfaces_expose_compact_runtime_evidence(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    trace_path = tmp_path / "runtime-trace-read-side.json"
    trace_path.write_text(
        json.dumps(
            {
                "trace_run": {
                    "trace_run_id": "read-side-run",
                    "source": "pytest",
                    "frontend_version": "ArcGraph-runtime-trace-v1",
                },
                "events": [
                    {
                        "kind": "dynamic_call",
                        "source": "fn:pkg.api.create_memory",
                        "target": "fn:pkg.service.build_message",
                        "path": "src/pkg/api.py",
                        "line": 21,
                        "column": 17,
                        "test_id": "tests/service_cases.py::test_build_message",
                        "args": ["do-not-persist"],
                        "headers": {"authorization": "secret"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        runtime_trace_path=trace_path,
    ).build()
    provider = ContextProvider(QueryEngine(output_dir))
    request = ContextRequest(
        targets=["pkg.service.build_message"],
        max_results=30,
        detail_level="detailed",
    )

    explain = provider.explain(request)
    callers = provider.compact_callers("pkg.service.build_message", request)
    callees = provider.compact_callees("pkg.api.create_memory", request)
    impact = provider.compact_impact("pkg.service.build_message", request)
    context = provider.get_context(request)

    explain_edge = _runtime_edge(
        explain["explanations"][0]["incoming_edges"],
        source="fn:pkg.api.create_memory",
        target="fn:pkg.service.build_message",
    )
    caller_edge = _runtime_edge(
        callers["edges"],
        source="fn:pkg.api.create_memory",
        target="fn:pkg.service.build_message",
    )
    callee_edge = _runtime_edge(
        callees["edges"],
        source="fn:pkg.api.create_memory",
        target="fn:pkg.service.build_message",
    )

    assert caller_edge == explain_edge
    assert callee_edge == explain_edge
    assert explain_edge["resolution"]["strategy"] == "runtime_trace"
    assert explain_edge["resolution"]["status"] == "runtime-only"
    assert isinstance(explain_edge["confidence_sources"], dict)
    assert explain_edge["evidence"][0]["kind"] == "runtime_trace_dynamic_call"
    assert explain_edge["evidence"][0]["path"] == "src/pkg/api.py"
    assert "properties" not in explain_edge
    assert not _contains_key(explain, "properties")
    assert not _contains_key(explain, "snippet")
    assert not _contains_key(explain, "source_snippet")
    assert not _contains_key(callers, "properties")
    assert not _contains_key(callees, "properties")

    runtime_impact_edge = _runtime_edge(
        impact["call_impact"]["edges"],
        source="fn:pkg.api.create_memory",
        target="fn:pkg.service.build_message",
    )
    assert runtime_impact_edge["resolution"]["strategy"] == "runtime_trace"
    assert impact["confidence_summary"]["runtime_only_edges"] >= 1
    assert (
        impact["provenance_summary"]["confidence"]["by_confidence"]["runtime-only"] >= 1
    )
    assert context["grouped_effects"]["runtime_only"]
    assert context["impact"][0]["grouped_effect_counts"]["runtime_only"] >= 1
    assert context["estimated_tokens"] < 8000
    assert "src/pkg/api.py" in {
        item["path"] for item in explain["recommended_next_reads"]
    }


def test_runtime_trace_import_limits_malformed_and_oversized_inputs(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    _build_fixture_index(repo_root, output_dir)
    limited_trace = tmp_path / "limited-runtime-trace.json"
    limited_trace.write_text(
        json.dumps(
            {
                "trace_run": {"trace_run_id": "limited-run", "source": "pytest"},
                "events": [
                    {
                        "kind": "call",
                        "source": "fn:pkg.api.create_memory",
                        "target": "fn:pkg.service.build_message",
                        "path": "src/pkg/api.py",
                        "line": 21,
                    },
                    {
                        "kind": "call",
                        "source": "fn:pkg.api.create_memory",
                        "target": "fn:pkg.service.build_message",
                        "path": "src/pkg/api.py",
                        "line": 22,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    limited_result = import_runtime_trace(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=limited_trace,
        max_events=1,
    )
    limited_current = QueryEngine(output_dir).current()
    limited_warnings = GraphStoreReader.from_current(output_dir).read_warnings()

    assert limited_result["status"] == "partial"
    assert limited_current["runtime_metrics"]["events_total"] == 2
    assert limited_current["runtime_metrics"]["events_processed"] == 1
    assert limited_current["runtime_metrics"]["truncated_events"] == 1
    assert "runtime_trace_event_limit_exceeded" in {
        warning.kind for warning in limited_warnings
    }

    malformed_trace = tmp_path / "malformed-runtime-trace.json"
    malformed_trace.write_text("{not json", encoding="utf-8")
    malformed_result = import_runtime_trace(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=malformed_trace,
    )
    malformed_warnings = GraphStoreReader.from_current(output_dir).read_warnings()

    assert malformed_result["status"] == "partial"
    assert QueryEngine(output_dir).current()["runtime_metrics"]["status"] == "partial"
    assert "runtime_trace_parse_error" in {
        warning.kind for warning in malformed_warnings
    }

    oversized_trace = tmp_path / "oversized-runtime-trace.json"
    oversized_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
    oversized_result = import_runtime_trace(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=oversized_trace,
        max_file_bytes=2,
    )
    oversized_warnings = GraphStoreReader.from_current(output_dir).read_warnings()

    assert oversized_result["status"] == "partial"
    assert "runtime_trace_too_large" in {warning.kind for warning in oversized_warnings}


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


def _write_fastapi_smoke_case(repo_root: Path) -> None:
    (repo_root / "tests" / "fastapi_smoke_cases.py").write_text(
        """
import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_fastapi_hello_route(monkeypatch):
    for module_name in [
        name for name in sys.modules if name == "pkg" or name.startswith("pkg.")
    ]:
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    fake_sqlalchemy = types.ModuleType("sqlalchemy")
    fake_sqlalchemy.select = lambda model: object()
    monkeypatch.setitem(sys.modules, "sqlalchemy", fake_sqlalchemy)
    from pkg.api import router

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/hello", params={"name": "Ada"})

    assert response.status_code == 200
    assert response.json() == "HELLO, ADA"
""".lstrip(),
        encoding="utf-8",
    )


def _read_expectations() -> dict[str, Any]:
    return json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))


def _runtime_edge(
    edges: list[dict[str, Any]], *, source: str, target: str
) -> dict[str, Any]:
    return next(
        edge
        for edge in edges
        if edge["source"] == source
        and edge["target"] == target
        and edge["confidence"] == "runtime-only"
    )


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


def _artifact_status(artifacts: list[dict[str, Any]], kind: str) -> str | None:
    for artifact in artifacts:
        if artifact.get("kind") == kind:
            status = artifact.get("status")
            return str(status) if status else None
    return None
