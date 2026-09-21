from __future__ import annotations

from pathlib import Path

from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import WhyMemoryContext
from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup
from arcgraph.providers.memory_connector import (
    StaticExternalMemoryConnector,
    ExternalMemoryConnector,
    MemoryWhyQuery,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


class _FailingExternalMemoryConnector(ExternalMemoryConnector):
    def get_why_context(self, query: MemoryWhyQuery) -> WhyMemoryContext:
        raise RuntimeError("External memory backend timeout")


class _CountingExternalMemoryConnector(ExternalMemoryConnector):
    def __init__(self) -> None:
        self.calls = 0

    def get_why_context(self, query: MemoryWhyQuery) -> WhyMemoryContext:
        self.calls += 1
        return WhyMemoryContext(status="available", query=query.text())


def test_get_why_merges_structural_and_external_memory_context(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    connector = StaticExternalMemoryConnector(
        [
            {
                "id": "memory-decision-1",
                "title": "Memory creation goes through service layer",
                "content": "pkg.service.MemoryService.create_memory owns memory creation decisions and route integration.",
                "memory_type": "architecture_design",
                "metadata": {"repo_id": "sample"},
            },
            {
                "id": "memory-lesson-1",
                "title": "Creation changes need worker checks",
                "content": "When changing create_memory, inspect embedding worker and repository write effects.",
                "memory_type": "lesson_learned",
                "metadata": {"repo_id": "sample"},
            },
            {
                "id": "memory-best-1",
                "title": "Keep route handlers thin",
                "content": "Best practice for create_memory route changes is to keep orchestration in MemoryService.",
                "memory_type": "best_practice",
                "metadata": {"repo_id": "sample"},
            },
        ]
    )
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
            external_memory_connector=connector,
        )
    )

    why = tools.arcgraph_get_why(
        repo_id="sample",
        target="pkg.service.MemoryService.create_memory",
        task="change memory creation",
    )

    assert why["external_memory"]["status"] == "available"
    assert why["external_memory"]["memory_count"] == 3
    assert why["historical_decisions"][0]["memory_type"] == "architecture_design"
    assert why["lessons_learned"][0]["memory_type"] == "lesson_learned"
    assert why["best_practices"][0]["memory_type"] == "best_practice"
    assert why["structural_why"]


def test_get_why_keeps_structural_context_when_external_memory_fails(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
            external_memory_connector=_FailingExternalMemoryConnector(),
        )
    )

    why = tools.arcgraph_get_why(
        repo_id="sample",
        target="pkg.service.MemoryService.create_memory",
        task="change memory creation",
    )

    assert why["status"] == "available"
    assert why["structural_why"]
    assert why["external_memory"]["status"] == "unavailable"
    assert "External memory connector failed" in why["external_memory"]["reason"]
    assert why["external_memory"]["memory_count"] == 0


def test_get_why_discloses_stale_index_recovery(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    source = repo_root / "src" / "pkg" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    source.write_text("def run():\n    return 2\n", encoding="utf-8")
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            repo_root,
            repo_id="sample",
            output_dir=output_dir,
        )
    )

    why = tools.arcgraph_get_why(repo_id="sample", target="pkg.service.run")

    assert why["freshness"]["stale"] is True
    assert why["recovery_action"]["command"] == "arcgraph sync --if-stale"


def test_record_learning_does_not_query_external_memory(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    connector = _CountingExternalMemoryConnector()
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
            external_memory_connector=connector,
        )
    )

    learning = tools.arcgraph_record_learning(
        repo_id="sample",
        target="pkg.service.MemoryService.create_memory",
        observation="",
    )

    assert learning["mode"] == "proposal-only"
    assert connector.calls == 0


def test_record_learning_returns_traceable_memory_candidate(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
        )
    )

    learning = tools.arcgraph_record_learning(
        repo_id="sample",
        target="pkg.service.MemoryService.create_memory",
        observation="",
    )
    candidate = learning["memory_candidate"]

    assert learning["mode"] == "proposal-only"
    assert learning["external_memory"]["status"] == "candidate_only"
    assert candidate["memory_type"] == "lesson_learned"
    assert candidate["source"] == "arcgraph_record_learning"
    assert candidate["content"]
    assert candidate["metadata"]["repo_id"] == "sample"
    assert (
        "method:pkg.service.MemoryService.create_memory"
        in candidate["metadata"]["resolved_targets"]
    )
    assert (
        "method:pkg.service.MemoryService.create_memory"
        in candidate["metadata"]["symbol_ids"]
    )
    assert candidate["metadata"]["arcgraph_index_version"]
    assert "src/pkg/service.py" in candidate["metadata"]["paths"]
    assert "ArcGraph" in candidate["tags"]


def test_record_learning_redacts_sensitive_observation_text(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
        )
    )

    learning = tools.arcgraph_record_learning(
        repo_id="sample",
        target="pkg.service.MemoryService.create_memory",
        observation="Observed password=s3cr3t and token:abcd1234 in debugging notes.",
        title="Bearer abc.def",
    )
    candidate = learning["memory_candidate"]

    assert "s3cr3t" not in candidate["content"]
    assert "abcd1234" not in candidate["content"]
    assert "abc.def" not in candidate["title"]
    assert "[REDACTED]" in candidate["content"]
    assert "[REDACTED]" in candidate["title"]
    assert candidate["title"] == "Bearer [REDACTED]"
