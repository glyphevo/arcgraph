from pathlib import Path

from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup
from arcgraph.pipeline.indexer import ArcGraphIndexer


def test_explicit_failed_target_never_returns_global_diagnostics(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text(
        "import pytest\n@pytest.fixture\ndef value():\n    return 1\n"
        "class Worker:\n    def run(self, unknown):\n        unknown.work()\n",
        encoding="utf-8",
    )
    (repo / "other.py").write_text(
        "def run(unknown):\n    unknown.other()\n", encoding="utf-8"
    )
    output = tmp_path / "index"
    ArcGraphIndexer(repo, output, [SourceRoot(".")]).build()
    engine = QueryEngine(output)
    assert engine.unresolved()["summary"]["total"] == 2
    for target in ("sample.value", "missing_symbol", "", "   "):
        result = engine.unresolved(target)
        assert result["summary"]["total"] == 0
        assert result["unresolved"] == []
        assert result["status"] == "partial"
        assert result["warnings"]
    ambiguous = engine.unresolved("sample.value")
    assert ambiguous["target_resolution"]["status"] == "ambiguous"
    assert len(ambiguous["target_resolution"]["candidates"]) == 2
    for target in (
        "method:sample.Worker.run",
        "class:sample.Worker",
        "mod:sample",
        "sample.py",
    ):
        result = engine.unresolved(target)
        assert result["summary"]["total"] == 1
        assert result["unresolved"][0]["path"] == "sample.py"
    for target in ("fixture:sample.value", "fn:sample.value", "missing.py"):
        assert engine.unresolved(target)["summary"]["total"] == 0
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            repo, repo_id="sample", output_dir=output, allowed_roots=[tmp_path]
        )
    )
    result = tools.arcgraph_explain(
        repo_id="sample", targets=["sample.value"], detail_level="detailed"
    )
    # Check the public payload, not merely the private scope helper.
    import json

    assert "unknown.work" not in json.dumps(result)
    assert "unknown.other" not in json.dumps(result)
    assert result["status"] == "partial"
    assert result["explanations"][0]["unresolved"]["summary"]["total"] == 0
