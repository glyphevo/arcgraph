from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.providers.risk_provider import RiskProvider


def _typescript_index(tmp_path: Path) -> tuple[Path, Path]:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for TypeScript reference tests.")
    source = tmp_path / "src" / "service.ts"
    source.parent.mkdir()
    source.write_text(
        """
export function target(value: number): number { return value + 1; }
export function first(): number { return target(1); }
export function second(): number { return target(2) + target(3); }
""".strip() + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    ArcGraphIndexer(tmp_path, output, [SourceRoot("src")]).build()
    if any(
        warning.kind == "typescript_frontend_unavailable"
        for warning in GraphStoreReader.from_current(output).read_warnings()
    ):
        pytest.skip("TypeScript compiler API is unavailable.")
    return output, source


def test_typescript_language_service_verifies_one_target_on_demand(
    tmp_path: Path,
) -> None:
    output, _source = _typescript_index(tmp_path)

    payload = QueryEngine(output).references("service.target")

    assert payload["status"] == "available"
    assert payload["backend"] == "typescript_language_service"
    assert payload["summary"] == {
        "total": 4,
        "definitions": 1,
        "writes": 1,
        "returned": 4,
        "truncated": False,
    }
    assert sum(item["is_definition"] for item in payload["references"]) == 1
    assert {item["line"] for item in payload["references"]} == {1, 2, 3}


def test_typescript_reference_lookup_handles_dollar_identifier_names(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for TypeScript reference tests.")
    source = tmp_path / "src" / "service.ts"
    source.parent.mkdir()
    source.write_text(
        """
export function data$(): number { return 1; }
export function first(): number { return data$(); }
export function second(): number { return data$(); }
""".strip() + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    ArcGraphIndexer(tmp_path, output, [SourceRoot("src")]).build()
    if any(
        warning.kind == "typescript_frontend_unavailable"
        for warning in GraphStoreReader.from_current(output).read_warnings()
    ):
        pytest.skip("TypeScript compiler API is unavailable.")

    payload = QueryEngine(output).references("service.data$")

    assert payload["status"] == "available"
    assert payload["summary"]["total"] == 3
    assert {item["line"] for item in payload["references"]} == {1, 2, 3}


def test_precise_reference_lookup_refuses_stale_target_coordinates(
    tmp_path: Path,
) -> None:
    output, source = _typescript_index(tmp_path)
    source.write_text(
        "// shifted\n" + source.read_text(encoding="utf-8"), encoding="utf-8"
    )

    payload = QueryEngine(output).references("service.target")

    assert payload["status"] == "partial"
    assert payload["references"] == []
    assert payload["recovery_action"]["command"] == "arcgraph sync --if-stale"


def test_change_preflight_can_request_precise_references_without_new_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, _source = _typescript_index(tmp_path)
    engine = QueryEngine(output)
    real_references = engine.references
    requested_limits: list[int] = []

    def references(target: str, *, max_results: int = 50):
        requested_limits.append(max_results)
        return real_references(target, max_results=max_results)

    monkeypatch.setattr(engine, "references", references)

    payload = RiskProvider(engine).get_risk(
        ["service.target"], max_results=7, verify_references=True
    )

    assert requested_limits == [7]
    assert payload["precise_references"][0]["backend"] == "typescript_language_service"
    assert payload["precise_references"][0]["summary"]["total"] == 4
    assert (
        payload["assurance"]["evidence"]["target_reference_verification"] == "available"
    )
    assert "exact_references" not in {item["kind"] for item in payload["unknowns"]}


def test_change_preflight_discloses_precise_reference_response_truncation(
    tmp_path: Path,
) -> None:
    output, _source = _typescript_index(tmp_path)

    payload = RiskProvider(QueryEngine(output)).get_risk(
        ["service.target"], max_results=1, verify_references=True
    )

    precise = payload["precise_references"][0]
    assert precise["summary"]["total"] == 4
    assert precise["summary"]["returned"] == 1
    assert precise["summary"]["truncated"] is True
    assert payload["truncation"]["truncated"] is True
    assert (
        payload["truncation"]["truncated_counts"]["precise_references.references"] == 3
    )
    assert payload["assurance"]["limits"]["response_truncated"] is True
    assert "truncated_scope" in {item["kind"] for item in payload["unknowns"]}


def test_reference_target_outside_repo_root_degrades_without_raising(
    tmp_path: Path,
) -> None:
    from arcgraph.core.precise_references import typescript_language_service_references

    result = typescript_language_service_references(
        repo_root=tmp_path,
        files=[],
        target={"path": "../escape.ts", "start_line": 1, "name": "target"},
    )

    assert result["status"] == "unavailable"
    assert "outside the repository root" in result["reason"]


def test_out_of_root_candidate_files_are_excluded_and_disclosed(
    tmp_path: Path,
) -> None:
    from arcgraph.core.precise_references import typescript_language_service_references

    if shutil.which("node") is None:
        pytest.skip("Node.js is required for TypeScript reference tests.")
    repo = tmp_path / "repo"
    source = repo / "src" / "service.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
export function target(value: number): number { return value + 1; }
export function first(): number { return target(1); }
""".strip() + "\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside" / "lib.ts"
    outside.parent.mkdir(parents=True)
    outside.write_text("export const noop = 1;\n", encoding="utf-8")

    result = typescript_language_service_references(
        repo_root=repo,
        files=["src/service.ts", "../outside/lib.ts"],
        target={"path": "src/service.ts", "start_line": 1, "name": "target"},
    )

    # Whatever the backend outcome (TS may be unavailable in this env), the
    # escaping candidate is excluded and disclosed instead of raising.
    assert result["summary"]["out_of_root_files_excluded"] == 1


def test_changed_file_target_verifies_references_via_resolved_ids(
    tmp_path: Path,
) -> None:
    output, _source = _typescript_index(tmp_path)

    payload = RiskProvider(QueryEngine(output)).get_risk(
        changed_files=["src/service.ts"], verify_references=True
    )

    entries = payload["precise_references"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["verified_symbols"]
    assert all("No graph node resolved" not in warning for warning in entry["warnings"])
    # A file resolves to every symbol it contains; the cap and the disclosed
    # remainder must agree.
    assert entry["unverified_symbol_count"] >= 0
    if entry["unverified_symbol_count"]:
        assert entry["status"] == "partial"
        assert any("unverified" in warning for warning in entry["warnings"])
    assert (
        payload["assurance"]["evidence"]["target_reference_verification"]
        != "unavailable"
    )


def test_reference_ordering_is_codepoint_stable_not_locale_dependent() -> None:
    """Ordering must match the SCIP backend's Python codepoint sort so the
    truncated subset is identical on every machine and ICU build."""

    import json
    import subprocess
    from arcgraph.core.precise_references import (
        typescript_language_service_references,  # noqa: F401
    )

    script = Path("arcgraph/pipeline/typescript_references.mjs").resolve()
    source = script.read_text(encoding="utf-8")
    assert ".localeCompare(" not in source
    paths = ["B.ts", "a.ts", "Z.ts", "_x.ts"]
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "const items = JSON.parse(process.argv[1]).map((path) => ({path, line: 1, column: 1}));"
            "items.sort((left, right) => {"
            "  if (left.path !== right.path) return left.path < right.path ? -1 : 1;"
            "  return left.line - right.line || left.column - right.column;"
            "});"
            "console.log(JSON.stringify(items.map((item) => item.path)));",
            json.dumps(paths),
        ],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        pytest.skip("Node.js is required for reference ordering checks.")
    assert json.loads(completed.stdout) == sorted(paths)


def test_dropped_reference_entries_are_disclosed_in_truncation() -> None:
    """Entries beyond max_results are dropped whole, so every reference they
    held is omitted — not just the part each kept entry itself trimmed."""

    from arcgraph.providers.risk_provider import _risk_truncation

    entries = [
        {"target": "a", "summary": {"total": 10, "returned": 4}},
        {"target": "b", "summary": {"total": 7, "returned": 7}},
        {"target": "c", "summary": {"total": 5, "returned": 5}},
    ]

    truncation = _risk_truncation(
        {},
        reports=[],
        precise_references=entries,
        max_results=1,
    )

    assert truncation["truncated"] is True
    assert truncation["truncated_counts"]["precise_references"] == 2
    # 6 omitted inside the kept entry, plus 7 + 5 from the dropped entries.
    assert truncation["truncated_counts"]["precise_references.references"] == 18


def test_nested_resolved_ids_are_bounded_and_summarized(tmp_path: Path) -> None:
    """A file target resolves to every symbol it declares, and the same
    resolution is emitted twice (report and assurance), so the id list is
    bounded for presentation and says how much it dropped."""

    from arcgraph.core.graph_store import GraphStoreWriter
    from arcgraph.core.schemas import IndexMetadata, Node

    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id=f"fn:wide.symbol_{index}",
            kind="function",
            name=f"symbol_{index}",
            qualname=f"wide.symbol_{index}",
            path="src/wide.py",
        )
        for index in range(40)
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="wide-file",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=nodes,
        edges=[],
        warnings=[],
    )

    payload = RiskProvider(QueryEngine(output_dir)).get_risk(
        ["src/wide.py"], max_results=5
    )

    resolution = payload["target_reports"][0]["target_resolution"]
    assert len(resolution["resolved_ids"]) == 5
    assert resolution["resolved_id_summary"] == {
        "total": 40,
        "returned": 5,
        "omitted": 35,
    }
    # Assurance re-emits the same resolution, so it is bounded there too.
    assert len(payload["assurance"]["target_resolutions"][0]["resolved_ids"]) == 5


def test_omitted_resolved_ids_reach_response_truncation(tmp_path: Path) -> None:
    """Bounding the nested id lists is a presentation omission like any
    other: left out of truncation, assurance calls the response complete."""

    from arcgraph.core.graph_store import GraphStoreWriter
    from arcgraph.core.schemas import IndexMetadata, Node

    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id=f"fn:wide.symbol_{index}",
            kind="function",
            name=f"symbol_{index}",
            qualname=f"wide.symbol_{index}",
            path="src/wide.py",
        )
        for index in range(40)
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="wide-truncation",
            repo_root=str(tmp_path),
            source_roots=["src"],
            capabilities={"coverage": "available", "runtime_trace": "available"},
        ),
        files=[],
        nodes=nodes,
        edges=[],
        warnings=[],
    )

    payload = RiskProvider(QueryEngine(output_dir)).get_risk(
        ["src/wide.py"], max_results=5
    )

    assert payload["truncation"]["truncated"] is True
    assert (
        payload["truncation"]["truncated_counts"][
            "target_reports.target_resolution.resolved_ids"
        ]
        == 35
    )
    assert payload["assurance"]["limits"]["response_truncated"] is True


def _wide_file_index(tmp_path: Path, *, covered: int = 1):
    from arcgraph.core.graph_store import GraphStoreWriter
    from arcgraph.core.schemas import Edge, IndexMetadata, Node

    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id=f"fn:wide.s{index}",
            kind="function",
            name=f"s{index}",
            qualname=f"wide.s{index}",
            path="src/wide.py",
        )
        for index in range(40)
    ]
    nodes.append(
        Node(id="test:t", kind="test", name="t", qualname="tests.t", path="tests/t.py")
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="wide-coverage",
            repo_root=str(tmp_path),
            source_roots=["src"],
            capabilities={
                "coverage": "available",
                "runtime_trace": "available",
                "precision": "precision_available",
            },
        ),
        files=[],
        nodes=nodes,
        edges=[
            Edge(source="test:t", target=f"fn:wide.s{index}", kind="covers")
            for index in range(covered)
        ],
        warnings=[],
    )
    return output_dir


def test_one_covered_symbol_does_not_cover_a_whole_file_target(
    tmp_path: Path,
) -> None:
    """A file target resolves to every symbol it declares, so coverage of one
    says nothing about the other thirty-nine."""

    output_dir = _wide_file_index(tmp_path, covered=1)

    payload = RiskProvider(QueryEngine(output_dir)).get_risk(["src/wide.py"])

    assert len(payload["test_gaps"]) > 1
    assert payload["assurance"]["evidence"]["coverage"] == "partial"
    assert "test_coverage" in {item["kind"] for item in payload["unknowns"]}


def test_every_serialized_surface_bounds_its_resolution_ids(tmp_path: Path) -> None:
    """Every payload that reaches an agent passes through the shared read
    contract, so a new surface cannot ship an unbounded id list by forgetting
    to call the helper."""

    from arcgraph.core.schemas import ContextRequest
    from arcgraph.providers.context_provider import ContextProvider

    output_dir = _wide_file_index(tmp_path)
    provider = ContextProvider(QueryEngine(output_dir))

    similar = provider.find_similar("src/wide.py", max_results=5)
    assert len(similar["target_resolution"]["resolved_ids"]) == 5
    assert similar["target_resolution"]["resolved_id_summary"]["omitted"] == 35

    request = ContextRequest(targets=["src/wide.py"], max_results=5)
    relation = provider.compact_callers("src/wide.py", request)
    assert relation["target_resolution"]["resolved_id_summary"]["omitted"] == 35
    assert relation["truncation"]["truncated"] is True
    assert (
        relation["truncation"]["truncated_counts"]["target_resolution.resolved_ids"]
        == 35
    )

    # The ids ride along inside assurance and inside per-target reports too,
    # so every nested carrier is checked, not only the top-level copy.
    for name, payload in (
        ("compact_impact", provider.compact_impact("src/wide.py", request)),
        ("get_context", provider.get_context(request)),
        ("find_similar", provider.find_similar("src/wide.py", max_results=5)),
    ):
        for nested in payload.get("assurance", {}).get("target_resolutions", []):
            assert len(nested.get("resolved_ids", [])) <= 5, name
        for report in payload.get("target_reports", []):
            assert (
                len(report.get("target_resolution", {}).get("resolved_ids", [])) <= 5
            ), name


def test_raw_impact_agrees_with_its_own_test_gaps(tmp_path: Path) -> None:
    """The engine's own assurance must not contradict the gaps in the same
    payload just because a provider is not involved."""

    output_dir = _wide_file_index(tmp_path, covered=0)

    payload = QueryEngine(output_dir).impact("src/wide.py")

    assert payload["test_gaps"]
    assert payload["assurance"]["posture"] != "bounded_support"
    assert any(
        "coverage" in item.lower() for item in payload["assurance"]["non_claims"]
    )


def test_file_level_coverage_edge_does_not_cover_every_symbol(
    tmp_path: Path,
) -> None:
    """Cobertura and Istanbul emit a module covers edge when ANY line in the
    file ran, and a symbol edge only for symbols whose own lines ran. The
    module edge therefore covers the module target and nothing else."""

    from arcgraph.core.graph_store import GraphStoreWriter
    from arcgraph.core.schemas import Edge, IndexMetadata, Node

    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id=f"fn:wide.s{index}",
            kind="function",
            name=f"s{index}",
            qualname=f"wide.s{index}",
            path="src/wide.py",
        )
        for index in range(40)
    ]
    nodes.append(
        Node(
            id="mod:wide",
            kind="module",
            name="wide",
            qualname="wide",
            path="src/wide.py",
        )
    )
    nodes.append(
        Node(
            id="coverage_run:aggregate",
            kind="coverage_run",
            name="aggregate",
            qualname="aggregate",
        )
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="file-level-coverage",
            repo_root=str(tmp_path),
            source_roots=["src"],
            capabilities={"coverage": "available", "runtime_trace": "available"},
        ),
        files=[],
        nodes=nodes,
        edges=[
            Edge(
                source="coverage_run:aggregate",
                target="mod:wide",
                kind="covers",
                confidence="runtime-only",
                properties={"level": "file", "path": "src/wide.py"},
            ),
            Edge(
                source="coverage_run:aggregate",
                target="fn:wide.s0",
                kind="covers",
                confidence="runtime-only",
                properties={"level": "symbol", "path": "src/wide.py"},
            ),
        ],
        warnings=[],
    )
    provider = RiskProvider(QueryEngine(output_dir))

    whole_file = provider.get_risk(["src/wide.py"])
    assert whole_file["test_gaps"]
    assert whole_file["assurance"]["evidence"]["coverage"] == "partial"

    # The symbol whose own lines ran is genuinely covered.
    covered = provider.get_risk(["wide.s0"])
    assert covered["test_gaps"] == []
    assert covered["assurance"]["evidence"]["coverage"] == "available"

    # A sibling symbol in the same file is not, despite the module edge.
    uncovered = provider.get_risk(["wide.s7"])
    assert len(uncovered["test_gaps"]) == 1
    assert uncovered["assurance"]["evidence"]["coverage"] == "partial"


def test_contract_bounds_both_copies_of_the_resolved_ids(tmp_path: Path) -> None:
    """The ids appear twice — in the resolution and as resolved_targets — so
    bounding one while emitting the other leaks the list just summarized."""

    from arcgraph.providers.context_provider import ContextProvider

    output_dir = _wide_file_index(tmp_path)
    payload = ContextProvider(QueryEngine(output_dir)).find_similar(
        "src/wide.py", max_results=5
    )

    assert len(payload["target_resolution"]["resolved_ids"]) == 5
    assert len(payload["resolved_targets"]) == 5
    assert payload["target_resolution"]["resolved_id_summary"]["total"] > 5
