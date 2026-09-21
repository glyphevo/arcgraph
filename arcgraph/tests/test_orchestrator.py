from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from arcgraph.core.schemas import (
    ContextResponse,
    READ_SCHEMA_VERSION,
    RiskReport,
    SCHEMA_VERSION,
    WhyReport,
)
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.scanner import SourceRoot
from arcgraph.providers.orchestrator import AgentContextOrchestrator
from arcgraph.providers.memory_connector import StaticExternalMemoryConnector

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def test_agent_context_orchestrator_merges_native_context(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    _init_git_repo(repo_root)
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    external_memory = StaticExternalMemoryConnector(
        [
            {
                "title": "Memory creation architecture",
                "content": "pkg.service.MemoryService.create_memory is reached by the memory route.",
                "memory_type": "architecture_design",
                "tags": ["ArcGraph", "memory"],
            }
        ]
    )
    orchestrator = AgentContextOrchestrator(
        repo_root=repo_root,
        output_dir=output_dir,
        external_memory=external_memory,
    )

    context = asyncio.run(
        orchestrator.build_task_context(
            task="change memory creation",
            repo_id="sample",
            targets=["pkg.service.MemoryService.create_memory"],
            policy="coding_change",
            max_results=20,
        )
    )

    assert context["status"] == "available"
    assert context["schema_version"] == READ_SCHEMA_VERSION
    assert context["index_schema_version"] == SCHEMA_VERSION
    assert context["repo_id"] == "sample"
    assert "similarity" not in context["capabilities"]
    assert context["index_status"]["capabilities"]["similarity"] == "available"
    assert context["capabilities_summary"]["reported"] == len(context["capabilities"])
    assert context["capabilities_summary"]["total"] > len(context["capabilities"])
    assert context["profile"] == "review_default"
    assert context["estimated_tokens"] > 0
    assert context["truncation"]["max_results"] == 20
    assert context["structural_context"]["confidence_profile"] == "review_default"
    assert context["structural_context"]["grouped_effects"]["confirmed"]
    assert context["structural_context"]["unresolved_risks"] == []
    assert context["structural_context"]["estimated_tokens"] > 0
    assert "route:POST:/memories" in {
        node["id"] for node in context["risk"]["blast_radius"]["entrypoints"]
    }
    assert context["why"]["structural_why"]
    assert RiskReport.model_validate(context["risk"])
    assert WhyReport.model_validate(context["why"])
    for payload in context["similar_implementations"]:
        assert payload["schema_version"] == READ_SCHEMA_VERSION
        assert payload["index_schema_version"] == SCHEMA_VERSION
        assert all(value != "available" for value in payload["capabilities"].values())
    assert context["memories"][0]["title"] == "Memory creation architecture"
    assert context["test_recommendations"]
    assert context["git_diff"]["status"] == "available"


def test_agent_context_orchestrator_scopes_top_level_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composite read payload must not re-expand the whole index warning list."""

    repo_root = _copy_fixture(tmp_path)
    _init_git_repo(repo_root)
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    orchestrator = AgentContextOrchestrator(
        repo_root=repo_root,
        output_dir=output_dir,
    )
    current = orchestrator.query_engine.current()
    unrelated = [
        {
            "kind": "typescript_import_unresolved",
            "message": f"unrelated warning {index}",
            "path": f"web/src/unrelated-{index}.ts",
        }
        for index in range(40)
    ]
    current["warnings"] = unrelated
    monkeypatch.setattr(orchestrator.query_engine, "current", lambda: current)

    context = asyncio.run(
        orchestrator.build_task_context(
            task="review memory creation",
            targets=["pkg.service.MemoryService.create_memory"],
        )
    )
    top_level_warnings = json.dumps(context["warnings"], sort_keys=True)

    assert "web/src/unrelated-" not in top_level_warnings
    assert "index_warnings_omitted" in top_level_warnings
    # The explicit nested whole-index view remains available when a consumer
    # intentionally needs every warning.
    assert context["index_status"]["warnings"] == unrelated
    assert all(value != "available" for value in context["capabilities"].values())


def test_agent_context_orchestrator_architecture_policy(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    _init_git_repo(repo_root)
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    orchestrator = AgentContextOrchestrator(
        repo_root=repo_root,
        output_dir=output_dir,
    )

    context = asyncio.run(
        orchestrator.build_task_context(
            task="inspect architecture",
            targets=["pkg.api"],
            policy="architecture_investigation",
            detail_level="detailed",
        )
    )

    assert "architecture" in context["structural_context"]
    assert context["structural_context"]["architecture"]["status"] == "available"
    validated = ContextResponse.model_validate(context["structural_context"])
    assert validated.architecture is not None


def test_agent_context_orchestrator_marks_unresolved_targets_partial(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    _init_git_repo(repo_root)
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    orchestrator = AgentContextOrchestrator(
        repo_root=repo_root,
        output_dir=output_dir,
    )

    context = asyncio.run(
        orchestrator.build_task_context(
            task="inspect missing target",
            targets=["pkg.missing.Nope"],
        )
    )

    assert context["status"] == "partial"
    assert context["why"]["status"] == "partial"
    assert any(
        item["kind"] == "unresolved_target" for item in context["risk"]["risk_factors"]
    )


def _copy_fixture(tmp_path: Path) -> Path:
    repo_root = tmp_path / "sample_project"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    return repo_root


def _init_git_repo(repo_root: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "ArcGraph@example.test"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "ArcGraph Test"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
