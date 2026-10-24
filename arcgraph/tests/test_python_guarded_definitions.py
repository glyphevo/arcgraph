"""Record C87's guarded-definition probe without changing Python analysis."""

from pathlib import Path

import pytest

from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer

FIXTURE = Path(__file__).parent / "fixtures" / "guarded_definitions"


@pytest.mark.parametrize("repo_aware", [False, True], ids=["host", "repo-aware"])
def test_main_guard_function_records_current_missing_node(repo_aware):
    """Pin current behavior for leftover L33: guarded_fn is absent in both modes.

    This records the existing limitation; the positive controls ensure a missing
    guarded definition cannot pass because the entire module was left out.
    """
    analyzer = PythonGraphAnalyzer(**({"repo_root": FIXTURE} if repo_aware else {}))
    result = analyzer.analyze(FileScanner(FIXTURE, [SourceRoot(".")]).scan())
    nodes = {node.id: node for node in result.nodes}
    assert "mod:pkg.guarded" in nodes
    for suffix, start, end in [
        ("top_fn", 1, 2),
        ("make", 5, 11),
        ("make.nested_fn", 8, 9),
    ]:
        node = nodes[f"fn:pkg.guarded.{suffix}"]
        assert node.qualname == f"pkg.guarded.{suffix}"
        assert node.path == "pkg/guarded.py"
        assert (node.start_line, node.end_line) == (start, end)
    assert not any(node.qualname == "pkg.guarded.guarded_fn" for node in result.nodes)
    assert "fn:pkg.guarded.guarded_fn" not in nodes
