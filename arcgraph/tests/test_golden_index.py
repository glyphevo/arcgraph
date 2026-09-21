from __future__ import annotations

import json
from pathlib import Path

from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.scanner import SourceRoot

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
GOLDEN_ROOT = Path(__file__).parent / "golden" / "sample_project"
SCHEMA_0_6_GOLDEN_ROOT = (
    Path(__file__).parent / "golden" / "schema_0_6" / "sample_project"
)


def test_fixture_index_matches_golden(tmp_path: Path) -> None:
    metadata, build_dir = ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=tmp_path / "arcgraph",
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        enable_v2_call_resolution=False,
    ).build()

    assert metadata.warning_count == 0
    assert metadata.capabilities["receiver_resolution"] == "legacy"
    assert _canonical_nodes(build_dir / "nodes.jsonl") == _read_jsonl(
        GOLDEN_ROOT / "nodes.jsonl"
    )
    assert _canonical_edges(build_dir / "edges.jsonl") == _read_jsonl(
        GOLDEN_ROOT / "edges.jsonl"
    )


def test_schema_0_6_golden_scaffold_declares_required_artifacts() -> None:
    manifest = json.loads(
        (SCHEMA_0_6_GOLDEN_ROOT / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["schema_version"] == "0.6.0"
    assert manifest["artifacts"] == [
        "semantic_facts.jsonl",
        "nodes.jsonl",
        "edges.jsonl",
        "diagnostics.jsonl",
        "merge_metrics.json",
    ]
    assert manifest["update_policy"] == "explicit-reviewer-approved"
    for artifact in manifest["artifacts"]:
        assert (SCHEMA_0_6_GOLDEN_ROOT / artifact).exists()


def test_schema_0_6_platform_artifacts_match_golden(tmp_path: Path) -> None:
    metadata, build_dir = ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=tmp_path / "arcgraph",
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        enable_v2_call_resolution=False,
    ).build()

    assert metadata.schema_version == "1.0.0"
    assert _schema_0_6_nodes(build_dir / "nodes.jsonl") == _schema_0_6_nodes(
        SCHEMA_0_6_GOLDEN_ROOT / "nodes.jsonl"
    )
    assert _schema_0_6_edges(build_dir / "edges.jsonl") == _read_jsonl(
        SCHEMA_0_6_GOLDEN_ROOT / "edges.jsonl"
    )
    assert _schema_0_6_semantic_facts(
        build_dir / "semantic_facts.jsonl"
    ) == _read_jsonl(SCHEMA_0_6_GOLDEN_ROOT / "semantic_facts.jsonl")
    assert _schema_0_6_diagnostics(build_dir / "diagnostics.jsonl") == _read_jsonl(
        SCHEMA_0_6_GOLDEN_ROOT / "diagnostics.jsonl"
    )
    assert _schema_0_6_merge_metrics(build_dir / "merge_metrics.json") == json.loads(
        (SCHEMA_0_6_GOLDEN_ROOT / "merge_metrics.json").read_text(encoding="utf-8")
    )


def _canonical_nodes(path: Path) -> list[dict[str, object]]:
    rows = _read_jsonl(path)
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "path": row.get("path"),
            "qualname": row.get("qualname"),
        }
        for row in rows
    ]


def _canonical_edges(path: Path) -> list[dict[str, object]]:
    rows = _read_jsonl(path)
    return [
        {
            "confidence": row["confidence"],
            "kind": row["kind"],
            "source": row["source"],
            "target": row["target"],
        }
        for row in rows
    ]


def _schema_0_6_nodes(path: Path) -> list[dict[str, object]]:
    return [
        {
            "canonical_identity": _stable_canonical_identity(
                row.get("canonical_identity")
            ),
            "id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "path": row.get("path"),
            "qualname": row.get("qualname"),
            "start_line": row.get("start_line"),
        }
        for row in _read_jsonl(path)
    ]


def _stable_canonical_identity(value: object) -> object:
    if not isinstance(value, str):
        return value
    parts = value.split(" ", 4)
    if len(parts) != 5 or parts[0] != "ArcGraph":
        return value
    parts[3] = "<version>"
    return " ".join(parts)


def _schema_0_6_edges(path: Path) -> list[dict[str, object]]:
    return [
        {
            "confidence": row.get("confidence"),
            "evidence": [
                {
                    "kind": item.get("kind"),
                    "path": item.get("path"),
                    "start_line": item.get("start_line"),
                }
                for item in row.get("evidence", [])
            ],
            "kind": row["kind"],
            "resolution_status": (row.get("resolution") or {}).get("status"),
            "semantic_role": row.get("semantic_role"),
            "source": row["source"],
            "target": row["target"],
        }
        for row in _read_jsonl(path)
    ]


def _schema_0_6_semantic_facts(path: Path) -> list[dict[str, object]]:
    return [
        {
            "confidence": row.get("confidence"),
            "edge_kind": row.get("edge_kind"),
            "fact_id": row.get("fact_id"),
            "fact_kind": row.get("fact_kind"),
            "frontend_name": row.get("frontend_name"),
            "node_id": row.get("node_id"),
            "path": row.get("path"),
            "semantic_role": row.get("semantic_role"),
            "source": row.get("source"),
            "start_line": row.get("start_line"),
            "target": row.get("target"),
        }
        for row in _read_jsonl(path)
    ]


def _schema_0_6_diagnostics(path: Path) -> list[dict[str, object]]:
    rows = _read_jsonl(path)
    return [
        {
            "diagnostic_id": row.get("diagnostic_id"),
            "diagnostic_kind": row.get("diagnostic_kind"),
            "frontend_name": row.get("frontend_name"),
            "message": row.get("message"),
            "path": row.get("path"),
            "severity": row.get("severity"),
            "start_line": row.get("start_line"),
            "properties": {
                key: props.get(key)
                for key in (
                    "raw_expression",
                    "failed_strategy",
                    "source_scope",
                    "expression_kind",
                )
                if key in props
            },
        }
        for row in rows
        for props in [row.get("properties") or {}]
    ]


def _schema_0_6_merge_metrics(path: Path) -> dict[str, object]:
    metrics = json.loads(path.read_text(encoding="utf-8"))
    return {
        "confidence_counts": metrics.get("confidence_counts"),
        "conflicts_count": metrics.get("conflicts_count"),
        "diagnostics_count": metrics.get("diagnostics_count"),
        "edge_kind_counts": metrics.get("edge_kind_counts"),
        "edges_out": metrics.get("edges_out"),
        "facts_in": metrics.get("facts_in"),
        "facts_merged": metrics.get("facts_merged"),
        "nodes_out": metrics.get("nodes_out"),
        "truncated_count": metrics.get("truncated_count"),
        "unresolved_count": metrics.get("unresolved_count"),
    }


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
