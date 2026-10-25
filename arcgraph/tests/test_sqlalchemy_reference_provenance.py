"""Foreign-key graph facts must not claim a precise SCIP reference backend."""

from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import Evidence, IndexMetadata
from arcgraph.pipeline.indexer import ArcGraphIndexer


def _table_index(tmp_path: Path) -> Path:
    (tmp_path / "models.py").write_text(
        "from sqlalchemy import Table, Column, ForeignKey, Integer, MetaData\n"
        "metadata = MetaData()\n"
        'users = Table("users", metadata, Column("id", Integer, primary_key=True))\n'
        'links = Table("links", metadata, Column("user_id", Integer, ForeignKey("users.id")))\n',
        encoding="utf-8",
    )
    out = tmp_path / "output"
    ArcGraphIndexer(tmp_path, out, [SourceRoot(".")]).build()
    return out


@pytest.mark.parametrize("backend", ["auto", "scip"])
def test_foreign_keys_are_not_presented_as_scip_references(
    tmp_path: Path, backend: str
) -> None:
    out = _table_index(tmp_path)
    reader = GraphStoreReader.from_current(out)
    foreign_keys = [edge for edge in reader.read_edges() if edge.kind == "references"]
    assert [(edge.source, edge.target) for edge in foreign_keys] == [
        ("table:links", "table:users")
    ]
    payload = QueryEngine(out).references("table:users", backend=backend)
    assert payload["freshness"]["stale"] is False
    assert payload["status"] == "partial"
    assert payload["backend"] is None
    assert payload["references"] == []
    assert "No precise-reference backend" in payload["warnings"][0]


@pytest.mark.parametrize("backend", ["auto", "scip"])
def test_imported_precise_evidence_survives_a_mixed_foreign_key_edge(
    tmp_path: Path, backend: str
) -> None:
    out = _table_index(tmp_path)
    reader = GraphStoreReader.from_current(out)
    edges = reader.read_edges()
    foreign_key = next(edge for edge in edges if edge.kind == "references")
    foreign_key.evidence.append(
        Evidence(
            kind="scip_protocol_reference",
            path="models.py",
            start_line=1,
            end_line=1,
            column=0,
        )
    )
    metadata = IndexMetadata.model_validate(
        {
            key: value
            for key, value in reader.metadata.items()
            if key in IndexMetadata.model_fields
        }
    ).model_copy(update={"index_version": "mixed-precise-evidence"})
    GraphStoreWriter(out).write(
        metadata,
        files=reader.read_files(),
        nodes=reader.read_nodes(),
        edges=edges,
        warnings=reader.read_warnings(),
    )
    payload = QueryEngine(out).references("table:users", backend=backend)
    assert payload["freshness"]["stale"] is False
    assert payload["status"] == "available"
    assert payload["backend"] == "scip"
    assert [item["line"] for item in payload["references"]] == [1]
    persisted = next(
        edge
        for edge in GraphStoreReader.from_current(out).read_edges()
        if edge.source == "table:links" and edge.target == "table:users"
    )
    assert {item.kind for item in persisted.evidence} == {
        "sqlalchemy_resource",
        "scip_protocol_reference",
    }
