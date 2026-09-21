from pathlib import Path

import pytest

from arcgraph.change.identities import stable_diagnostic_identity
from arcgraph.pipeline.frontends import python_compat_pass_dispatcher
from arcgraph.core.ids import callsite_id
from arcgraph.core.merge import EvidenceMergeEngine, GraphIntegrityError
from arcgraph.core.passes import AnalysisPassContext, PassDispatcher
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    Evidence,
    FactResolution,
    IndexMetadata,
    Node,
)


def test_merge_engine_emits_artifacts_and_conflict_diagnostics(
    tmp_path: Path,
) -> None:
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
        commit_sha="abc123",
    )
    nodes = [
        Node(id="fn:pkg.a", kind="function", name="a", path="src/pkg.py"),
        Node(id="fn:pkg.b", kind="function", name="b", path="src/pkg.py"),
        Node(id="fn:pkg.c", kind="function", name="c", path="src/pkg.py"),
    ]
    edges = [
        Edge(
            source="fn:pkg.a",
            target="fn:pkg.b",
            kind="calls",
            confidence="confirmed",
            resolution=FactResolution(status="resolved", callsite_id="call:1"),
            evidence=[Evidence(kind="scip_call", path="src/pkg.py", start_line=10)],
        ),
        Edge(
            source="fn:pkg.a",
            target="fn:pkg.c",
            kind="calls",
            confidence="confirmed",
            resolution=FactResolution(status="resolved", callsite_id="call:1"),
            evidence=[Evidence(kind="scip_call", path="src/pkg.py", start_line=10)],
        ),
    ]
    warnings = [
        BuildWarning(kind="parse_error", message="bad syntax", path="src/pkg.py")
    ]

    artifacts = EvidenceMergeEngine(
        tmp_path,
        dispatcher=python_compat_pass_dispatcher(),
    ).merge_graph(metadata, nodes, edges, warnings)

    assert all(node.canonical_identity for node in artifacts.nodes)
    assert len(artifacts.semantic_facts) == len(artifacts.nodes) + len(artifacts.edges)
    assert {diagnostic.diagnostic_kind for diagnostic in artifacts.diagnostics} == {
        "merge_conflict",
        "parse_error",
    }
    assert artifacts.merge_metrics.conflicts_count == 1
    assert artifacts.merge_metrics.diagnostics_count == 2
    assert artifacts.pass_metrics["p1-python-v1-compat"]["edge_facts"] == 2


def test_merge_engine_rejects_dangling_edge_endpoints(tmp_path: Path) -> None:
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    source = Node(id="fn:pkg.a", kind="function", name="a")
    dangling = Edge(source=source.id, target="fn:pkg.missing", kind="calls")

    with pytest.raises(GraphIntegrityError, match="dangling edge endpoint"):
        EvidenceMergeEngine(tmp_path).merge_graph(
            metadata,
            [source],
            [dangling],
            [],
        )


def test_merge_engine_canonicalizes_similarity_pairs_and_keeps_highest_score() -> None:
    edges = [
        Edge(
            source="fn:pkg.z",
            target="fn:pkg.a",
            kind="similar_to",
            confidence="inferred",
            evidence=[Evidence(kind="ast_similarity", path="src/z.py")],
            properties={
                "score": 0.84,
                "algorithm_detail": "preserve",
                "source_structure_hash": "z-hash",
                "target_structure_hash": "a-hash",
            },
        ),
        Edge(
            source="fn:pkg.a",
            target="fn:pkg.z",
            kind="similar_to",
            confidence="inferred",
            evidence=[Evidence(kind="ast_similarity", path="src/a.py")],
            properties={
                "score": 0.95,
                "source_structure_hash": "a-hash",
                "target_structure_hash": "z-hash",
            },
        ),
    ]

    deduped = EvidenceMergeEngine.dedupe_edges(edges)

    assert len(deduped) == 1
    assert deduped[0].source == "fn:pkg.a"
    assert deduped[0].target == "fn:pkg.z"
    assert deduped[0].properties["score"] == 0.95
    assert deduped[0].properties["algorithm_detail"] == "preserve"
    assert {evidence.path for evidence in deduped[0].evidence} == {
        "src/a.py",
        "src/z.py",
    }


def test_merge_engine_omits_missing_similarity_hash_keys() -> None:
    edge = Edge(
        source="fn:pkg.z",
        target="fn:pkg.a",
        kind="similar_to",
        evidence=[Evidence(kind="ast_similarity", path="src/z.py")],
        properties={"source_structure_hash": "z-hash"},
    )

    deduped = EvidenceMergeEngine.dedupe_edges([edge])

    assert deduped[0].source == "fn:pkg.a"
    assert deduped[0].target == "fn:pkg.z"
    assert "source_structure_hash" not in deduped[0].properties
    assert deduped[0].properties["target_structure_hash"] == "z-hash"


def test_edge_dedupe_copies_only_when_normalizing_or_merging() -> None:
    first = Edge(source="fn:pkg.a", target="fn:pkg.b", kind="calls")
    second = Edge(source="fn:pkg.c", target="fn:pkg.d", kind="calls")

    deduped = EvidenceMergeEngine.dedupe_edges([first, second])

    assert deduped[0] is first
    assert deduped[1] is second

    duplicate = first.model_copy(
        deep=True,
        update={"evidence": [Evidence(kind="static_call", path="src/pkg.py")]},
    )
    merged = EvidenceMergeEngine.dedupe_edges([first, duplicate])[0]

    assert merged is not first
    assert first.evidence == []
    assert merged.evidence == duplicate.evidence


def test_node_dedupe_prefers_concrete_definition_over_unresolved_placeholder() -> None:
    placeholder = Node(
        id="fn:pkg.task",
        kind="function",
        name="task",
        qualname="pkg.task",
        properties={
            "external_reference": True,
            "resolution_status": "unresolved",
        },
    )
    definition = Node(
        id="fn:pkg.task",
        kind="function",
        name="task",
        qualname="pkg.task",
        path="src/pkg.py",
        start_line=1,
        end_line=2,
    )

    assert EvidenceMergeEngine.dedupe_nodes([placeholder, definition]) == [definition]
    assert EvidenceMergeEngine.dedupe_nodes([definition, placeholder]) == [definition]


def test_edge_dedupe_adopts_resolution_from_higher_confidence_fact() -> None:
    low = Edge(
        source="fn:pkg.a",
        target="fn:pkg.b",
        kind="calls",
        confidence="heuristic",
        resolution=FactResolution(
            status="partial",
            strategy="heuristic",
            callsite_id="call:low",
        ),
    )
    high = low.model_copy(
        deep=True,
        update={
            "confidence": "confirmed",
            "resolution": FactResolution(
                status="resolved",
                strategy="semantic",
                callsite_id="call:high",
            ),
        },
    )

    forward = EvidenceMergeEngine.dedupe_edges([low, high])[0]
    reverse = EvidenceMergeEngine.dedupe_edges([high, low])[0]

    assert forward.confidence == reverse.confidence == "confirmed"
    assert forward.resolution == reverse.resolution == high.resolution


@pytest.mark.parametrize("reverse", [False, True])
def test_import_edge_dedupe_keeps_runtime_dependency(reverse: bool) -> None:
    type_only = Edge(
        source="mod:server",
        target="ext:express",
        kind="imports",
        properties={"import_kind": "type", "specifier": "@types/express"},
    )
    runtime = Edge(
        source="mod:server",
        target="ext:express",
        kind="imports",
        properties={"import_kind": "commonjs", "specifier": "express"},
    )
    edges = [runtime, type_only] if reverse else [type_only, runtime]

    deduped = EvidenceMergeEngine.dedupe_edges(edges)

    assert len(deduped) == 1
    assert deduped[0].properties["import_kind"] == "commonjs"
    assert deduped[0].properties["specifier"] == "express"


def test_merge_engine_detects_confirmed_location_conflict_without_callsite(
    tmp_path: Path,
) -> None:
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    edges = [
        Edge(
            source="fn:pkg.a",
            target="fn:pkg.b",
            kind="calls",
            confidence="confirmed",
            evidence=[
                Evidence(
                    kind="scip_call",
                    path="src/pkg.py",
                    start_line=10,
                    column=4,
                )
            ],
        ),
        Edge(
            source="fn:pkg.a",
            target="fn:pkg.c",
            kind="calls",
            confidence="confirmed",
            evidence=[
                Evidence(
                    kind="ast_call",
                    path="src/pkg.py",
                    start_line=10,
                    column=4,
                )
            ],
        ),
    ]

    nodes = [
        Node(id=node_id, kind="function", name=node_id.rsplit(".", 1)[-1])
        for node_id in ("fn:pkg.a", "fn:pkg.b", "fn:pkg.c")
    ]
    artifacts = EvidenceMergeEngine(tmp_path).merge_graph(metadata, nodes, edges, [])
    conflicts = [
        diagnostic
        for diagnostic in artifacts.diagnostics
        if diagnostic.diagnostic_kind == "merge_conflict"
    ]

    assert len(conflicts) == 1
    assert conflicts[0].properties["conflict_scope"] == "location"
    assert conflicts[0].properties["targets"] == ["fn:pkg.b", "fn:pkg.c"]


def test_merge_conflict_identity_follows_semantic_witness_when_locations_swap(
    tmp_path: Path,
) -> None:
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    engine = EvidenceMergeEngine(tmp_path, repo_id="repo")

    def diagnostics_for(locations: list[tuple[int, str]]):
        edges = [
            Edge(
                source="fn:pkg.a",
                target=target,
                kind="calls",
                confidence="confirmed",
                evidence=[
                    Evidence(
                        kind="scip_call",
                        path="src/pkg.py",
                        start_line=line,
                        column=4,
                        detail=witness,
                    )
                ],
            )
            for line, witness in locations
            for target in ("fn:pkg.b", "fn:pkg.c")
        ]
        return engine._conflict_diagnostics(metadata, edges)

    baseline = diagnostics_for([(10, "left branch"), (20, "right branch")])
    current = diagnostics_for([(10, "right branch"), (20, "left branch")])

    baseline_ids = {
        diagnostic.properties["start_line"]: stable_diagnostic_identity(
            "repo", diagnostic
        )["stable_diagnostic_id"]
        for diagnostic in baseline
    }
    current_ids = {
        diagnostic.properties["start_line"]: stable_diagnostic_identity(
            "repo", diagnostic
        )["stable_diagnostic_id"]
        for diagnostic in current
    }

    assert baseline_ids[10] == current_ids[20]
    assert baseline_ids[20] == current_ids[10]
    assert baseline_ids[10] != current_ids[10]


def test_merge_engine_dedupes_unresolved_callsite_diagnostics(
    tmp_path: Path,
) -> None:
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    nodes = [
        Node(
            id="fn:pkg.a",
            kind="function",
            name="a",
            qualname="pkg.a",
            path="src/pkg.py",
            properties={
                "callsites": [
                    {
                        "name": "missing",
                        "line": 3,
                        "column": 11,
                        "context": "function_body",
                    },
                    {
                        "name": "missing",
                        "line": 3,
                        "column": 11,
                        "context": "function_body",
                    },
                ]
            },
        )
    ]

    artifacts = EvidenceMergeEngine(tmp_path).merge_graph(metadata, nodes, [], [])
    unresolved = [
        diagnostic
        for diagnostic in artifacts.diagnostics
        if diagnostic.diagnostic_kind == "unresolved_callsite"
    ]

    assert len(unresolved) == 1
    assert artifacts.merge_metrics.unresolved_count == 1


def test_merge_engine_treats_constructs_callsite_as_resolved(
    tmp_path: Path,
) -> None:
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    source = Node(
        id="fn:pkg.build",
        kind="function",
        name="build",
        qualname="pkg.build",
        path="src/pkg.py",
        properties={
            "callsites": [
                {
                    "name": "Widget",
                    "line": 4,
                    "column": 11,
                    "context": "function_body",
                }
            ]
        },
    )
    target = Node(
        id="class:pkg.Widget",
        kind="class",
        name="Widget",
        qualname="pkg.Widget",
        path="src/pkg.py",
    )
    edge = Edge(
        source=source.id,
        target=target.id,
        kind="constructs",
        resolution=FactResolution(
            status="resolved",
            callsite_id=callsite_id(source.id, source.path, 4, 11, "Widget"),
        ),
    )

    artifacts = EvidenceMergeEngine(tmp_path).merge_graph(
        metadata,
        [source, target],
        [edge],
        [],
    )

    assert [
        diagnostic
        for diagnostic in artifacts.diagnostics
        if diagnostic.diagnostic_kind == "unresolved_callsite"
    ] == []
    assert artifacts.merge_metrics.unresolved_count == 0


def test_merge_engine_counts_all_resolved_callsites_on_merged_edge(
    tmp_path: Path,
) -> None:
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    source = Node(
        id="fn:pkg.lookup",
        kind="function",
        name="lookup",
        qualname="pkg.lookup",
        path="src/pkg.py",
        properties={
            "callsites": [
                {
                    "name": "cache.get",
                    "line": 4,
                    "column": 11,
                    "context": "function_body",
                },
                {
                    "name": "cache.get",
                    "line": 5,
                    "column": 11,
                    "context": "function_body",
                },
            ]
        },
    )
    edge = Edge(
        source=source.id,
        target="extsym:builtins.dict.get",
        kind="uses",
        resolution=FactResolution(
            status="resolved",
            callsite_id=callsite_id(source.id, source.path, 4, 11, "cache.get"),
        ),
        evidence=[
            Evidence(
                kind="ast_attribute_call",
                path=source.path,
                start_line=4,
                column=11,
                detail="cache.get",
            ),
            Evidence(
                kind="ast_attribute_call",
                path=source.path,
                start_line=5,
                column=11,
                detail="cache.get",
            ),
        ],
    )
    target = Node(
        id="extsym:builtins.dict.get",
        kind="external_symbol",
        name="get",
        qualname="builtins.dict.get",
    )

    artifacts = EvidenceMergeEngine(tmp_path).merge_graph(
        metadata,
        [source, target],
        [edge],
        [],
    )

    assert [
        diagnostic
        for diagnostic in artifacts.diagnostics
        if diagnostic.diagnostic_kind == "unresolved_callsite"
    ] == []
    assert artifacts.merge_metrics.unresolved_count == 0


def test_pass_dispatcher_converts_pass_failure_to_partial_result(
    tmp_path: Path,
) -> None:
    class FailingPass:
        name = "failing-pass"

        def run(self, context: AnalysisPassContext):  # noqa: ANN001, ANN201
            raise RuntimeError("boom")

    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    context = AnalysisPassContext(
        metadata=metadata,
        repo_root=tmp_path,
        repo_id="default",
        nodes=[],
        edges=[],
        warnings=[],
    )

    result = PassDispatcher([FailingPass()]).run(context)

    assert result.results[0].result.status == "partial"
    assert result.diagnostics[0].diagnostic_kind == "analysis_pass_failed"
    assert result.diagnostics[0].properties["exception_type"] == "RuntimeError"
    assert any("boom" in line for line in result.diagnostics[0].properties["traceback"])


def test_merge_engine_uses_project_name_for_canonical_identity(
    tmp_path: Path,
) -> None:
    """Canonical identity must use project_name/version from metadata, not commit_sha."""
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
        commit_sha="abc123deadbeef",
        project_name="my-ArcGraph-project",
        project_version="2.0.0",
    )
    nodes = [
        Node(id="fn:pkg.hello", kind="function", name="hello", path="src/pkg.py"),
    ]

    artifacts = EvidenceMergeEngine(
        tmp_path,
        dispatcher=python_compat_pass_dispatcher(),
    ).merge_graph(metadata, nodes, [], [])

    assert len(artifacts.nodes) == 1
    identity = artifacts.nodes[0].canonical_identity
    assert identity is not None
    assert "my-ArcGraph-project" in identity
    assert "2.0.0" in identity
    # commit_sha must NEVER appear in canonical identity
    assert "abc123deadbeef" not in identity


def test_merge_engine_falls_back_to_working_tree_without_project_version(
    tmp_path: Path,
) -> None:
    """When project_version is missing, canonical identity uses 'working-tree'."""
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
        commit_sha="abc123deadbeef",
        project_name="my-project",
    )
    nodes = [
        Node(id="fn:pkg.hello", kind="function", name="hello", path="src/pkg.py"),
    ]

    artifacts = EvidenceMergeEngine(
        tmp_path,
        dispatcher=python_compat_pass_dispatcher(),
    ).merge_graph(metadata, nodes, [], [])

    identity = artifacts.nodes[0].canonical_identity
    assert identity is not None
    assert "my-project" in identity
    assert "working-tree" in identity
    assert "abc123deadbeef" not in identity
