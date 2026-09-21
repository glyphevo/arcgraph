import ast

from arcgraph.analyzers.imports import ImportAnalyzer
from arcgraph.core.schemas import Edge, Evidence, FileRecord


def test_import_analyzer_evidence_identity_includes_kind_and_snippet() -> None:
    edge = Edge(
        source="mod:pkg.a",
        target="mod:pkg.b",
        kind="imports",
        evidence=[
            Evidence(
                kind="ast_import",
                path="pkg/a.py",
                start_line=1,
                end_line=1,
                column=0,
                detail="from pkg import b",
            )
        ],
    )

    assert ImportAnalyzer._has_evidence(
        edge,
        Evidence(
            kind="ast_import",
            path="pkg/a.py",
            start_line=1,
            end_line=1,
            column=0,
            detail="from pkg import b",
        ),
    )
    assert not ImportAnalyzer._has_evidence(
        edge,
        Evidence(
            kind="scip_import",
            path="pkg/a.py",
            start_line=1,
            end_line=1,
            column=0,
            detail="from pkg import b",
        ),
    )
    assert not ImportAnalyzer._has_evidence(
        edge,
        Evidence(
            kind="ast_import",
            path="pkg/a.py",
            start_line=1,
            end_line=1,
            column=0,
            detail="from pkg import b",
            snippet="from pkg import b",
        ),
    )


def test_import_analyzer_skips_type_checking_only_import_edges() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "from typing import TYPE_CHECKING",
                "import runtime_dep",
                "if TYPE_CHECKING:",
                "    from pkg import service",
                "else:",
                "    import fallback_dep",
            ]
        )
    )
    file_record = FileRecord(
        path="pkg/api.py",
        abs_path="/repo/pkg/api.py",
        source_root="pkg",
        module="pkg.api",
        file_hash="hash",
        line_count=6,
    )

    analysis = ImportAnalyzer().analyze(file_record, tree, {"pkg.api", "pkg.service"})
    targets = {edge.target for edge in analysis.edges}

    assert "mod:pkg.service" not in targets
    assert targets == {"ext:typing", "ext:runtime_dep", "ext:fallback_dep"}


def test_import_analyzer_marks_function_local_import_edges() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import module_dep",
                "",
                "def run():",
                "    from pkg import service",
            ]
        )
    )
    file_record = FileRecord(
        path="pkg/api.py",
        abs_path="/repo/pkg/api.py",
        source_root="pkg",
        module="pkg.api",
        file_hash="hash",
        line_count=4,
    )

    analysis = ImportAnalyzer().analyze(file_record, tree, {"pkg.api", "pkg.service"})
    evidence_by_target = {
        edge.target: {item.kind for item in edge.evidence} for edge in analysis.edges
    }

    assert evidence_by_target["ext:module_dep"] == {"ast_import"}
    assert evidence_by_target["mod:pkg.service"] == {"ast_local_import"}
    assert not any(
        hasattr(node, "_arcgraph_import_scope")
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    )
