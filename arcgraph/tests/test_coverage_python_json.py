"""Native coverage.py reports preserve aggregate and test-context evidence."""

import json

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.schemas import Node
from arcgraph.interfaces.cli import main

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


def test_coverage_python_context_prefers_the_pytest_test_case_node(tmp_path):
    """The pytest adapter's test case wins over the same-named test function.

    Both nodes carry the same path and qualname, so a context that matches
    both must resolve to the ``test_case`` node: that is the node the pytest
    adapter owns and the one the per-test evidence belongs to.
    """
    nodes = graph(tmp_path)
    function = next(
        node for node in nodes if node.id == "fn:tests.test_service.test_run"
    )
    test_case = Node(
        id="test:tests.test_service.test_run",
        kind="test_case",
        name="test_run",
        qualname=function.qualname,
        path=function.path,
        start_line=function.start_line,
        end_line=function.end_line,
    )
    path = report(
        tmp_path,
        {
            "meta": {"format": 3, "show_contexts": True},
            "files": {
                "pkg/service.py": {
                    "executed_lines": [1, 2],
                    "contexts": {"2": ["tests/test_service.py::test_run|run"]},
                }
            },
        },
    )
    result = CoverageCollector(tmp_path, path).analyze([*nodes, test_case])
    per_test = {
        (edge.source, edge.target)
        for edge in result.edges
        if edge.source != "coverage:aggregate"
    }
    assert per_test == {
        (test_case.id, "mod:pkg.service"),
        (test_case.id, "fn:pkg.service.run"),
    }
    assert not any(edge.source == function.id for edge in result.edges)
    assert result.metrics["contexts_unmatched"] == 0
    assert result.status == "available"


def test_coverage_python_warns_about_unmatched_test_contexts(tmp_path):
    """An unmatched test context is reported with its count and examples."""
    nodes = graph(tmp_path)
    path = report(
        tmp_path,
        {
            "meta": {"format": 3, "show_contexts": True},
            "files": {
                "pkg/service.py": {
                    "executed_lines": [1, 2],
                    "contexts": {
                        "2": [
                            "tests/test_service.py::test_run|run",
                            "missing.py::test_other|run",
                        ]
                    },
                }
            },
        },
    )
    result = CoverageCollector(tmp_path, path).analyze(nodes)
    warnings = [
        warning
        for warning in result.warnings
        if warning.kind == "coverage_python_unmatched_contexts"
    ]
    assert len(warnings) == 1
    assert "1" in warnings[0].message
    assert "missing.py::test_other|run" in warnings[0].message
    assert result.metrics["contexts_unmatched"] == 1
    # One context still matched, so the file keeps its per-test evidence.
    assert result.status == "available"


def test_coverage_python_status_is_partial_when_every_test_context_is_unmatched(
    tmp_path,
):
    """A report whose test contexts all fail to match has partial coverage."""
    nodes = graph(tmp_path)
    path = report(
        tmp_path,
        {
            "meta": {"format": 3, "show_contexts": True},
            "files": {
                "pkg/service.py": {
                    "executed_lines": [1, 2],
                    "contexts": {
                        "2": ["missing.py::test_other|run", "production"],
                    },
                }
            },
        },
    )
    result = CoverageCollector(tmp_path, path).analyze(nodes)
    assert result.status == "partial"
    assert result.metrics["status"] == "partial"
    # The static context is not a test context: it is counted as unmatched but
    # it does not warn and it does not decide the status.
    assert result.metrics["contexts_unmatched"] == 2
    assert [warning.kind for warning in result.warnings] == [
        "coverage_python_unmatched_contexts"
    ]


def test_coverage_python_status_counts_test_contexts_across_the_report(tmp_path):
    """One file's unmatched contexts do not make the whole report partial."""
    graph(tmp_path)
    (tmp_path / "pkg/other.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    nodes = (
        PythonGraphAnalyzer()
        .analyze(FileScanner(tmp_path, [SourceRoot(".")]).scan())
        .nodes
    )
    path = report(
        tmp_path,
        {
            "meta": {"format": 3, "show_contexts": True},
            "files": {
                "pkg/service.py": {
                    "executed_lines": [1, 2],
                    "contexts": {"2": ["tests/test_service.py::test_run|run"]},
                },
                "pkg/other.py": {
                    "executed_lines": [1, 2],
                    "contexts": {"2": ["missing.py::test_other|run"]},
                },
            },
        },
    )
    result = CoverageCollector(tmp_path, path).analyze(nodes)
    assert result.status == "available"
    assert result.metrics["contexts_unmatched"] == 1
    assert [warning.kind for warning in result.warnings] == [
        "coverage_python_unmatched_contexts"
    ]


def test_coverage_python_contexts_cover_per_test_in_a_real_build(tmp_path, capsys):
    """A real build must link each pytest test to the code its context covered.

    The pytest adapter adds a ``test_case`` node with the same path and
    qualname as the test function, so a context that matches both must resolve
    to the ``test_case`` node and produce a per-test ``covers`` edge.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "service.py").write_text(
        "def run():\n    return 7\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_service.py").write_text(
        "from pkg.service import run\n\n\ndef test_run():\n    assert run() == 7\n",
        encoding="utf-8",
    )
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "meta": {"format": 3, "show_contexts": True},
                "files": {
                    "pkg/service.py": {
                        "executed_lines": [1, 2],
                        "contexts": {
                            "2": ["tests/test_service.py::test_run|run"],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"
    exit_code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            str(output),
            "build",
            "--coverage",
            str(coverage),
        ]
    )
    capsys.readouterr()
    assert exit_code == 0
    reader = GraphStoreReader.from_current(output)
    nodes = {node.id: node for node in reader.iter_nodes()}
    metrics = reader.metadata["coverage_metrics"]
    test_node = next(
        node
        for node in nodes.values()
        if node.kind == "test_case" and node.name == "test_run"
    )
    service = next(
        node
        for node in nodes.values()
        if node.kind == "function" and node.name == "run"
    )
    covers = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "covers"
    }
    assert (test_node.id, service.id) in covers, (
        "the pytest context did not produce a per-test covers edge; "
        f"contexts_unmatched={metrics.get('contexts_unmatched')}, "
        f"covers={sorted(covers)}"
    )
    assert metrics["contexts_unmatched"] == 0
    assert metrics["status"] == "available"


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


def test_real_pytest_coverage_json_matches_xml_and_preserves_test_context(
    tmp_path, monkeypatch
):
    import os
    import subprocess

    from arcgraph.tests.project_environment_support import isolated_test_python

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
            isolated_test_python(tmp_path),
            "-m",
            "pytest",
            str(tmp_path / "tests/test_service.py"),
            f"--rootdir={tmp_path}",
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
    # coverage makes report filenames relative to the reporting directory.
    # Bind it to the fixture even when pytest's private temp root is below cwd.
    monkeypatch.chdir(tmp_path)
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


@pytest.mark.parametrize("test_name", ["test_run", "TestSuite::test_run"])
@pytest.mark.parametrize(
    "parameter", ["::1", "2606:4700:4700::1111-False", "a|::1", "[::1]"]
)
def test_coverage_python_parameter_contexts_keep_ipv6_and_pipe_ids(
    tmp_path, test_name, parameter
):
    nodes = graph(tmp_path)
    if test_name.startswith("TestSuite"):
        (tmp_path / "tests/test_service.py").write_text(
            "class TestSuite:\n    def test_run(self):\n        pass\n",
            encoding="utf-8",
        )
        nodes = (
            PythonGraphAnalyzer()
            .analyze(FileScanner(tmp_path, [SourceRoot(".")]).scan())
            .nodes
        )
    qualname = "tests.test_service." + test_name.replace("::", ".")
    case = Node(
        id="test:" + qualname,
        kind="test_case",
        name="test_run",
        qualname=qualname,
        path="tests/test_service.py",
    )
    context = f"tests/test_service.py::{test_name}[{parameter}]|run"
    path = report(
        tmp_path,
        {
            "meta": {"format": 3},
            "files": {
                "pkg/service.py": {"executed_lines": [2], "contexts": {"2": [context]}}
            },
        },
    )
    result = CoverageCollector(tmp_path, path).analyze([*nodes, case])
    edges = [edge for edge in result.edges if edge.source == case.id]
    assert {edge.target for edge in edges} == {"mod:pkg.service", "fn:pkg.service.run"}
    assert all(edge.properties["contexts"] == [context] for edge in edges)
    assert result.metrics["contexts_unmatched"] == 0


def test_coverage_python_overlapping_symbols_keep_the_entire_context_line_set(tmp_path):
    nodes = graph(tmp_path)
    outer = Node(
        id="fn:pkg.service.outer",
        kind="function",
        name="outer",
        qualname="pkg.service.outer",
        path="pkg/service.py",
        start_line=1,
        end_line=8,
    )
    inner = Node(
        id="fn:pkg.service.outer.inner",
        kind="function",
        name="inner",
        qualname="pkg.service.outer.inner",
        path="pkg/service.py",
        start_line=2,
        end_line=4,
    )
    sibling = Node(
        id="fn:pkg.service.sibling",
        kind="function",
        name="sibling",
        qualname="pkg.service.sibling",
        path="pkg/service.py",
        start_line=6,
        end_line=7,
    )
    context = "tests/test_service.py::test_run|run"
    path = report(
        tmp_path,
        {
            "meta": {"format": 3},
            "files": {
                "pkg/service.py": {
                    "executed_lines": [3, 6, 99],
                    "contexts": {"3": [context], "99": [context]},
                }
            },
        },
    )
    result = CoverageCollector(tmp_path, path).analyze([*nodes, outer, inner, sibling])
    matched = [
        edge for edge in result.edges if edge.source == "fn:tests.test_service.test_run"
    ]
    assert {edge.target for edge in matched} == {"mod:pkg.service", outer.id, inner.id}
    assert all(edge.properties["covered_lines"] == [3, 99] for edge in matched)
    aggregate = [
        edge
        for edge in result.edges
        if edge.source == CoverageCollector.AGGREGATE_NODE_ID
    ]
    assert sibling.id in {edge.target for edge in aggregate}
    assert all(edge.properties["covered_lines"] == [3, 6, 99] for edge in aggregate)


def test_coverage_python_reader_uses_frozen_bytes_and_discards_symbol_summaries(
    tmp_path,
):
    from types import SimpleNamespace

    nodes = graph(tmp_path)
    path = report(
        tmp_path,
        {
            "meta": {"format": 3},
            "files": {
                "pkg/service.py": {
                    "executed_lines": [2],
                    "functions": {"run": {"contexts": {"2": ["redundant"]}}},
                    "classes": {"": {"executed_lines": [2]}},
                }
            },
        },
    )
    frozen = path.read_bytes()
    info = path.stat()
    path.write_text("this live file is no longer JSON", encoding="utf-8")

    class InspectingCollector(CoverageCollector):
        def _analyze_coverage_python(self, path, payload, nodes, modules):
            item = payload["files"]["pkg/service.py"]
            assert "functions" not in item and "classes" not in item
            return super()._analyze_coverage_python(path, payload, nodes, modules)

    collector = InspectingCollector(tmp_path, path)
    collector._frozen_inputs = SimpleNamespace(
        exists=lambda _: True, read_bytes=lambda _: frozen, stat=lambda _: info
    )
    result = collector.analyze(nodes)
    assert result.status == "available"
    assert {edge.target for edge in result.edges} == {
        "mod:pkg.service",
        "fn:pkg.service.run",
    }


@pytest.mark.parametrize(
    "context", ["one.py::test|two.py::test|run", "one.py::test[a]|two.py::test[b]|run"]
)
def test_coverage_python_context_rejects_multiple_node_ids(context):
    assert CoverageCollector._coverage_context_parts(context) is None


def test_coverage_python_context_keeps_static_brackets_and_parameter_pipes():
    assert CoverageCollector._coverage_context_parts(
        "production[dev]|tests/a.py::Test::test[[::1]|b]|run"
    ) == ("tests/a.py", ["Test", "test"])
