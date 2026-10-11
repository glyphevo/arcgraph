"""Native coverage.py reports preserve aggregate and test-context evidence."""

import json

import pytest

from arcgraph.analyzers.coverage import CoverageCollector
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer
from arcgraph.core.scanner import FileScanner, SourceRoot


def graph(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/service.py").write_text(
        "def run():\n    return 7\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_service.py").write_text(
        "def test_run():\n    pass\n", encoding="utf-8"
    )
    return (
        PythonGraphAnalyzer()
        .analyze(FileScanner(tmp_path, [SourceRoot(".")]).scan())
        .nodes
    )


def report(tmp_path, payload):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_coverage_python_json_and_cobertura_share_aggregate_targets(tmp_path):
    nodes = graph(tmp_path)
    path = report(
        tmp_path,
        {
            "meta": {"format": 3},
            "files": {"pkg/service.py": {"executed_lines": [1, 2]}},
        },
    )
    xml = tmp_path / "coverage.xml"
    xml.write_text(
        '<coverage><packages><package><classes><class filename="pkg/service.py"><lines><line number="1" hits="1"/><line number="2" hits="1"/></lines></class></classes></package></packages></coverage>',
        encoding="utf-8",
    )
    native = CoverageCollector(tmp_path, path).analyze(nodes)
    old = CoverageCollector(tmp_path, xml).analyze(nodes)
    assert native.status == "available"
    assert native.metrics["format"] == "coverage.py"
    assert {(e.source, e.target, e.kind) for e in native.edges} == {
        (e.source, e.target, e.kind) for e in old.edges
    }
    assert {e.target for e in native.edges} == {"mod:pkg.service", "fn:pkg.service.run"}


def test_coverage_python_test_contexts_merge_parameter_runs_without_guessing(tmp_path):
    nodes = graph(tmp_path)
    path = report(
        tmp_path,
        {
            "meta": {"format": 3, "show_contexts": True},
            "files": {
                "pkg/service.py": {
                    "executed_lines": [1, 2],
                    "contexts": {
                        "1": [""],
                        "2": [
                            "tests/test_service.py::test_run[a]|run",
                            "tests/test_service.py::test_run[b]|run",
                            "unknown.py::test_run|run",
                            "production",
                            "tests/test_service.py::test_run[a]|run",
                        ],
                    },
                }
            },
        },
    )
    result = CoverageCollector(tmp_path, path).analyze(nodes)
    matches = [
        e
        for e in result.edges
        if e.source == "fn:tests.test_service.test_run"
        and e.target == "fn:pkg.service.run"
    ]
    assert matches
    context = matches[0]
    assert context.confidence == "runtime-only"
    assert context.properties["contexts"] == [
        "tests/test_service.py::test_run[a]|run",
        "tests/test_service.py::test_run[b]|run",
    ]
    assert context.properties["covered_lines"] == [2]
    assert not any(e.source.endswith("production") for e in result.edges)
    assert result.metrics["contexts_unmatched"] == 2


@pytest.mark.parametrize("raw_path", ["pkg/service.py", "pkg\\service.py", "absolute"])
def test_coverage_python_paths_match_only_the_indexed_file(tmp_path, raw_path):
    nodes = graph(tmp_path)
    if raw_path == "absolute":
        raw_path = str(tmp_path / "pkg/service.py")
    path = report(
        tmp_path, {"meta": {"format": 3}, "files": {raw_path: {"executed_lines": [2]}}}
    )
    result = CoverageCollector(tmp_path, path).analyze(nodes)
    assert {e.target for e in result.edges} == {"mod:pkg.service", "fn:pkg.service.run"}


def test_coverage_python_rejects_invalid_lines_and_unmatched_files(tmp_path):
    nodes = graph(tmp_path)
    path = report(
        tmp_path,
        {
            "meta": {"format": 3},
            "files": {
                "pkg/service.py": {"executed_lines": [True, 0, -1, "2", None]},
                "other.py": {"executed_lines": [2]},
                "broken.py": None,
            },
        },
    )
    result = CoverageCollector(tmp_path, path).analyze(nodes)
    assert not result.edges
    assert result.metrics.get("entries_unmatched") == 1
    assert result.metrics.get("entries_malformed") == 1


def test_coverage_python_does_not_claim_a_future_format(tmp_path):
    nodes = graph(tmp_path)
    path = report(
        tmp_path,
        {"meta": {"format": 999}, "files": {"pkg/service.py": {"executed_lines": [2]}}},
    )
    result = CoverageCollector(tmp_path, path).analyze(nodes)
    assert not result.edges
    assert any(w.kind == "coverage_python_unsupported_format" for w in result.warnings)


def test_real_pytest_coverage_json_matches_xml_and_preserves_test_context(tmp_path):
    import os
    import subprocess
    import sys

    import coverage

    nodes = graph(tmp_path)
    (tmp_path / "pkg/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests/test_service.py").write_text(
        "from pkg.service import run\ndef test_run():\n    assert run() == 7\n",
        encoding="utf-8",
    )
    # Reparse the actual test source before executing the same isolated fixture.
    nodes = (
        PythonGraphAnalyzer()
        .analyze(FileScanner(tmp_path, [SourceRoot(".")]).scan())
        .nodes
    )
    env = dict(
        os.environ,
        PYTHONPATH=str(tmp_path),
        COVERAGE_FILE=str(tmp_path / ".coverage-fixture"),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path / "tests/test_service.py"),
            "-q",
            "--cov=pkg",
            "--cov-context=test",
            "--cov-fail-under=0",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cov = coverage.Coverage(data_file=env["COVERAGE_FILE"])
    cov.load()
    json_path, xml_path = tmp_path / "actual.json", tmp_path / "actual.xml"
    cov.json_report(outfile=str(json_path), show_contexts=True)
    cov.xml_report(outfile=str(xml_path))
    native = CoverageCollector(tmp_path, json_path).analyze(nodes)
    old = CoverageCollector(tmp_path, xml_path).analyze(nodes)
    assert native.edges
    assert {
        (e.source, e.target, e.kind)
        for e in native.edges
        if e.source == "coverage:aggregate"
    } == {(e.source, e.target, e.kind) for e in old.edges}
    assert any(
        e.source == "fn:tests.test_service.test_run"
        and e.target == "fn:pkg.service.run"
        for e in native.edges
    )
