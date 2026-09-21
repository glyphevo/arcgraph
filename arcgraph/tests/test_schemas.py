from arcgraph.core.schemas import (
    Edge,
    Evidence,
    FactEvidence,
    FactResolution,
    FrontendAnalyzeRequest,
    FrontendAnalyzeResult,
    FrontendCapabilities,
    IndexMetadata,
    MemoryCandidate,
    MergeMetrics,
    Node,
    READ_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SCHEMA_STABLE_TARGET_VERSION,
    SemanticDiagnostic,
    SemanticFact,
    MemoryRef,
    WhyMemoryContext,
    V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION,
    evidence_key,
    schema_direct_compatibility_step,
    schema_compatibility_steps,
    schema_version_reached,
)
from arcgraph.core.ids import diagnostic_id, semantic_fact_id, type_ref_id
from arcgraph.core.semantic import semantic_facts_from_graph


def test_schema_round_trip() -> None:
    node = Node(
        id="fn:pkg.service.build_message",
        kind="function",
        name="build_message",
        qualname="pkg.service.build_message",
        path="src/pkg/service.py",
        start_line=1,
        end_line=3,
        canonical_identity="ArcGraph local sample test fn:pkg.service.build_message",
    )
    edge = Edge(
        source="mod:pkg.service",
        target=node.id,
        kind="defines",
        evidence=[
            Evidence(kind="ast_definition", path=node.path, start_line=1, end_line=3)
        ],
    )
    metadata = IndexMetadata(
        index_version="test", repo_root="/repo", source_roots=["src"]
    )

    assert Node.model_validate(node.model_dump()).id == node.id
    assert Edge.model_validate(edge.model_dump()).evidence[0].kind == "ast_definition"
    assert "confidence" not in edge.evidence[0].model_dump()
    assert node.canonical_identity is not None
    assert evidence_key(edge.evidence[0]) == (
        "ast_definition",
        node.path,
        1,
        3,
        None,
        None,
        None,
    )
    assert metadata.schema_version == SCHEMA_VERSION
    assert SCHEMA_VERSION == "1.0.0"


def test_read_and_index_contracts_are_versioned_independently() -> None:
    assert SCHEMA_VERSION == "1.0.0"
    assert READ_SCHEMA_VERSION == "1.4.0"
    assert READ_SCHEMA_VERSION != SCHEMA_VERSION


def test_agent_facing_schema_defaults() -> None:
    metadata = IndexMetadata(
        index_version="test", repo_root="/repo", source_roots=["src"]
    )

    assert metadata.capabilities["imports"] == "available"
    assert metadata.capabilities["framework_adapters"] == "available"
    assert metadata.capabilities["entrypoints"] == "available"
    assert metadata.capabilities["resources"] == "available"
    assert metadata.capabilities["architecture"] == "basic"
    assert metadata.capabilities["precise_references"] == "ast-fallback"
    assert metadata.capabilities["test_gaps"] == "available"
    assert metadata.capabilities["coverage"] == "unavailable"
    assert metadata.capabilities["similarity"] == "available"
    assert metadata.capabilities["semantic_facts"] == "available"
    assert metadata.capabilities["diagnostics"] == "available"
    assert metadata.capabilities["merge_metrics"] == "available"
    assert metadata.capabilities["semantic_stats"] == "available"
    assert metadata.capabilities["unresolved"] == "available"
    assert metadata.capabilities["types"] == "available"
    assert metadata.capabilities["receiver_resolution"] == "available"


def test_content_hash_ids_are_deterministic_and_prefixed() -> None:
    assert semantic_fact_id("a", None, 1) == semantic_fact_id("a", None, 1)
    assert diagnostic_id("a", None, 1) == diagnostic_id("a", None, 1)
    assert type_ref_id("a", None, 1) == type_ref_id("a", None, 1)
    assert semantic_fact_id("a", None, 1).startswith("fact:")
    assert diagnostic_id("a", None, 1).startswith("diagnostic:")
    assert type_ref_id("a", None, 1).startswith("type_ref:")
    assert semantic_fact_id("a", None, 1) != diagnostic_id("a", None, 1)
    assert type_ref_id("a", None, 1) != semantic_fact_id("a", None, 1)


def test_platform_semantic_contract_defaults() -> None:
    frontend = FrontendCapabilities(
        name="python-semantic", version="0.2.0", language="python"
    )
    request = FrontendAnalyzeRequest(repo_root="/repo", source_roots=["src"])
    fact = SemanticFact(
        fact_id="fact:1",
        index_version="test",
        language="python",
        frontend_name=frontend.name,
        frontend_version=frontend.version,
        fact_kind="edge",
        source="fn:pkg.a",
        target="fn:pkg.b",
        edge_kind="calls",
        confidence="heuristic",
        semantic_role="runtime-call",
        resolution=FactResolution(
            status="resolved",
            strategy="unique_short_name",
            candidate_count=1,
        ),
        evidence=[FactEvidence(kind="ast_call", path="src/pkg/a.py", start_line=7)],
    )
    diagnostic = SemanticDiagnostic(
        diagnostic_id="diagnostic:1",
        index_version="test",
        diagnostic_kind="unresolved_callsite",
        message="Could not resolve x.y()",
        path="src/pkg/a.py",
    )
    result = FrontendAnalyzeResult(
        frontend=frontend,
        facts=[fact],
        diagnostics=[diagnostic],
    )
    metrics = MergeMetrics(index_version="test", facts_in=1, facts_merged=1)

    assert request.full_rebuild is True
    assert fact.schema_version == SCHEMA_VERSION
    assert fact.confidence == "heuristic"
    assert FactResolution().status == "resolved"
    assert fact.resolution.status == "resolved"
    assert result.status == "available"
    assert metrics.facts_in == 1


def test_schema_compatibility_route_to_stable_contract() -> None:
    steps = schema_compatibility_steps("0.6.0", SCHEMA_STABLE_TARGET_VERSION)
    direct_step = schema_direct_compatibility_step(
        "0.6.0", SCHEMA_STABLE_TARGET_VERSION
    )

    assert [step.from_schema_version for step in steps] == [
        "0.6.0",
        "0.7.0",
        "0.8.0",
        "0.9.0",
    ]
    assert steps[-1].to_schema_version == SCHEMA_STABLE_TARGET_VERSION
    assert [step.strategy for step in steps] == [
        "full-rebuild",
        "full-rebuild",
        "incremental-import",
        "compatibility-check",
    ]
    assert [step.required_rebuild for step in steps] == [
        True,
        True,
        False,
        False,
    ]
    assert direct_step is not None
    assert direct_step.strategy == "full-rebuild"
    assert direct_step.required_rebuild is True
    assert "not separately published stable runtime schemas" in direct_step.notes


def test_ci_schema_readiness_summary_records_stable_rebuild_policy() -> None:
    from arcgraph.interfaces.ci import _schema_readiness_summary

    summary = _schema_readiness_summary()

    assert summary["current_schema_version"] == SCHEMA_VERSION
    assert summary["stable_target_schema_version"] == SCHEMA_STABLE_TARGET_VERSION
    assert summary["ready_for_stable_contract"] is True
    assert summary["required_rebuilds"] == 0
    assert (
        summary["receiver_resolution_default_schema_version"]
        == V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION
    )

    legacy_direct = summary["legacy_0_6_to_stable_direct_strategy"]
    assert legacy_direct["from_schema_version"] == "0.6.0"
    assert legacy_direct["to_schema_version"] == SCHEMA_STABLE_TARGET_VERSION
    assert legacy_direct["strategy"] == "full-rebuild"
    assert legacy_direct["required_rebuild"] is True
    assert "not separately published stable runtime schemas" in legacy_direct["notes"]

    legacy_steps = summary["legacy_0_6_to_stable_steps"]
    assert [step["strategy"] for step in legacy_steps] == [
        "full-rebuild",
        "full-rebuild",
        "incremental-import",
        "compatibility-check",
    ]


def test_receiver_resolution_default_schema_gate() -> None:
    assert V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION == "1.0.0"
    assert schema_version_reached("1.0.0", "1.0.0") is True
    assert schema_version_reached("0.9.0", "1.0.0") is False
    assert schema_version_reached("0.6.0", "1.0.0") is False


def test_edge_semantic_fact_id_includes_evidence_location() -> None:
    metadata = IndexMetadata(
        index_version="test", repo_root="/repo", source_roots=["src"]
    )
    first = Edge(
        source="fn:pkg.a",
        target="fn:pkg.b",
        kind="calls",
        evidence=[Evidence(kind="ast_call", path="src/pkg/a.py", start_line=10)],
    )
    second = Edge(
        source="fn:pkg.a",
        target="fn:pkg.b",
        kind="calls",
        evidence=[Evidence(kind="ast_call", path="src/pkg/a.py", start_line=20)],
    )

    facts = semantic_facts_from_graph(metadata, [], [first, second])

    assert facts[0].fact_id != facts[1].fact_id
    assert {fact.start_line for fact in facts} == {10, 20}


def test_external_memory_schema_defaults() -> None:
    memory = MemoryRef(
        id="memory-1",
        title="Use service layer",
        content="Memory writes should go through MemoryService.",
        memory_type="architecture_design",
    )
    context = WhyMemoryContext(
        status="available",
        memories=[memory],
        historical_decisions=[memory],
    )
    candidate = MemoryCandidate(
        title="Run worker tests",
        content="When changing embedding enqueue behavior, run worker tests.",
        metadata={"repo_id": "sample"},
    )

    assert context.best_practices == []
    assert context.historical_decisions[0].id == "memory-1"
    assert candidate.memory_type == "lesson_learned"
    assert candidate.source == "arcgraph_record_learning"


def test_canonical_identity_uses_project_name_and_version() -> None:
    """Canonical identity must use project_name/version, never commit_sha."""
    from pathlib import Path
    from arcgraph.core.semantic import ensure_node_canonical_identities

    nodes = [
        Node(id="fn:pkg.hello", kind="function", name="hello", path="src/pkg.py"),
    ]

    ensure_node_canonical_identities(
        nodes,
        repo_root=Path("/repo"),
        project_name="my-project",
        project_version="1.2.3",
    )

    identity = nodes[0].canonical_identity
    assert identity is not None
    assert "my-project" in identity
    assert "1.2.3" in identity
    # commit_sha must never appear in canonical identity
    assert "abc123" not in identity


def test_canonical_identity_falls_back_to_working_tree() -> None:
    """When project_version is None, canonical identity uses 'working-tree'."""
    from pathlib import Path
    from arcgraph.core.semantic import ensure_node_canonical_identities

    nodes = [
        Node(id="fn:pkg.hello", kind="function", name="hello", path="src/pkg.py"),
    ]

    ensure_node_canonical_identities(
        nodes,
        repo_root=Path("/repo"),
        project_name="my-project",
        project_version=None,
    )

    identity = nodes[0].canonical_identity
    assert identity is not None
    assert "my-project" in identity
    assert "working-tree" in identity
