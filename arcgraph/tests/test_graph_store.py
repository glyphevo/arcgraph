from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.core.schemas import (
    BuildWarning,
    FileRecord,
    IndexMetadata,
    Node,
    SemanticDiagnostic,
)


def test_graph_store_reader_falls_back_to_immutable_uri(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    class FakeConnection:
        row_factory = None

    def fake_connect(database: object, *args: object, **kwargs: object):
        calls.append((database, kwargs))
        if len(calls) == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return FakeConnection()

    monkeypatch.setattr("arcgraph.core.graph_store.sqlite3.connect", fake_connect)

    reader = GraphStoreReader(tmp_path / "index.sqlite", {})
    conn = reader.connect()

    assert calls[0][0] == tmp_path / "index.sqlite"
    assert calls[0][1]["factory"].__name__ == "ClosingSQLiteConnection"
    fallback_uri = str(calls[1][0])
    assert fallback_uri.startswith("file:")
    assert fallback_uri.endswith("?mode=ro&immutable=1")
    assert calls[1][1]["uri"] is True
    assert calls[1][1]["factory"].__name__ == "ClosingSQLiteConnection"
    assert conn.row_factory is sqlite3.Row


def test_graph_store_reader_context_manager_closes_connection(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "index.sqlite"
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO files DEFAULT VALUES")

    reader = GraphStoreReader(sqlite_path, {})
    with reader.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_graph_store_writer_closes_every_sqlite_handle_it_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sqlite3.Connection.__exit__`` commits or rolls back but never closes.

    A plain ``with sqlite3.connect(...)`` in the write path therefore held the
    file handle until garbage collection. Publishing one build leaked several,
    which is what produced the bulk of the suite's ResourceWarning noise.
    """

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(conn)
        return conn

    monkeypatch.setattr("arcgraph.core.graph_store.sqlite3.connect", recording_connect)

    metadata = IndexMetadata(
        index_version="closes-index",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    files = [
        FileRecord(
            path="src/app.py",
            abs_path=str(tmp_path / "src" / "app.py"),
            source_root="src",
            module="app",
            file_hash="abc",
            line_count=1,
        )
    ]
    nodes = [Node(id="mod:app", kind="module", name="app", path="src/app.py")]

    GraphStoreWriter(tmp_path / "arcgraph").write(metadata, files, nodes, [], [])

    assert opened, "the write path is expected to open at least one connection"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_graph_store_writer_checkpoints_sqlite_for_immutable_reads(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    metadata = IndexMetadata(
        index_version="test-index",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    files = [
        FileRecord(
            path="src/app.py",
            abs_path=str(tmp_path / "src" / "app.py"),
            source_root="src",
            module="app",
            file_hash="abc",
            line_count=1,
        )
    ]
    nodes = [
        Node(
            id="mod:app",
            kind="module",
            name="app",
            path="src/app.py",
        )
    ]

    build_dir = GraphStoreWriter(output_dir).write(metadata, files, nodes, [], [])
    sqlite_path = build_dir / "index.sqlite"
    uri = sqlite_path.resolve().as_uri() + "?mode=ro&immutable=1"

    with sqlite3.connect(uri, uri=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone() == (1,)


def test_build_warning_frontend_name_round_trips_through_sqlite(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="warning-provenance",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[],
        edges=[],
        warnings=[
            BuildWarning(
                kind="similarity_bucket_skipped",
                message="TypeScript bucket was too large.",
                frontend_name="typescript-static",
            )
        ],
    )

    assert GraphStoreReader.from_current(output_dir).read_warnings() == [
        BuildWarning(
            kind="similarity_bucket_skipped",
            message="TypeScript bucket was too large.",
            frontend_name="typescript-static",
        )
    ]


def test_build_warning_reader_accepts_legacy_table_without_frontend_name(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute("""
            CREATE TABLE warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                path TEXT,
                message TEXT NOT NULL
            )
            """)
        conn.execute(
            "INSERT INTO warnings(kind, path, message) VALUES (?, ?, ?)",
            ("legacy_warning", "src/app.ts", "legacy warning"),
        )

    assert GraphStoreReader(sqlite_path, {}).read_warnings() == [
        BuildWarning(
            kind="legacy_warning",
            path="src/app.ts",
            message="legacy warning",
        )
    ]


@pytest.mark.parametrize("reversed_order", [False, True])
def test_ambiguous_diagnostic_subject_does_not_inherit_lifecycle(
    tmp_path: Path,
    reversed_order: bool,
) -> None:
    """Two current diagnostics sharing one old subject are both new.

    Nothing distinguishes a corrected attribution from a genuinely new sibling
    here, so handing the old lifecycle to whichever is iterated first would
    make the published result depend on input order.
    """

    output_dir = tmp_path / "arcgraph"
    previous = SemanticDiagnostic(
        diagnostic_id="diagnostic:legacy",
        index_version="one",
        diagnostic_kind="similarity_bucket_skipped",
        message="same subject",
        path="src/app.ts",
        start_line=7,
        frontend_name="frontend-a",
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="one", repo_root=str(tmp_path), source_roots=["src"]
        ),
        files=[],
        nodes=[],
        edges=[],
        warnings=[],
        diagnostics=[previous],
    )

    siblings = [
        previous.model_copy(
            update={
                "diagnostic_id": f"diagnostic:{name}",
                "index_version": "two",
                "frontend_name": name,
            }
        )
        for name in ("frontend-b", "frontend-c")
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="two", repo_root=str(tmp_path), source_roots=["src"]
        ),
        files=[],
        nodes=[],
        edges=[],
        warnings=[],
        diagnostics=list(reversed(siblings)) if reversed_order else siblings,
    )

    diagnostics = GraphStoreReader.from_current(output_dir).read_diagnostics()
    statuses = {
        diagnostic.diagnostic_id: diagnostic.properties["diagnostic_lifecycle_status"]
        for diagnostic in diagnostics
    }
    assert statuses == {
        "diagnostic:frontend-b": "new",
        "diagnostic:frontend-c": "new",
    }
    assert all(diagnostic.first_seen_index == "two" for diagnostic in diagnostics)


def test_diagnostic_lifecycle_survives_corrected_frontend_attribution(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    first = SemanticDiagnostic(
        diagnostic_id="diagnostic:legacy",
        index_version="one",
        diagnostic_kind="similarity_bucket_skipped",
        message="same subject",
        path="src/app.ts",
        start_line=7,
        frontend_name="python-v1-compat-shim",
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="one",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[],
        edges=[],
        warnings=[],
        diagnostics=[first],
    )

    corrected = first.model_copy(
        update={
            "diagnostic_id": "diagnostic:corrected",
            "index_version": "two",
            "frontend_name": "typescript-static",
        }
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="two",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[],
        edges=[],
        warnings=[],
        diagnostics=[corrected],
    )

    diagnostics = GraphStoreReader.from_current(output_dir).read_diagnostics()
    assert len(diagnostics) == 1
    assert diagnostics[0].frontend_name == "typescript-static"
    assert diagnostics[0].first_seen_index == "one"
    assert diagnostics[0].last_seen_index == "two"
    assert diagnostics[0].seen_count == 2
    assert diagnostics[0].properties["diagnostic_lifecycle_status"] == "existing"


def test_same_warning_from_two_frontends_keeps_distinct_diagnostics() -> None:
    from arcgraph.core.semantic import diagnostics_from_warnings
    from arcgraph.core.schemas import BuildWarning, IndexMetadata

    metadata = IndexMetadata(
        index_version="test-version",
        repo_root="/tmp/repo",
        source_roots=["src"],
        commit_sha=None,
    )
    warnings = [
        BuildWarning(
            kind="typescript_import_unresolved",
            message="Unresolved import './x'.",
            path="src/a.ts",
            frontend_name="typescript-static",
        ),
        BuildWarning(
            kind="typescript_import_unresolved",
            message="Unresolved import './x'.",
            path="src/a.ts",
            frontend_name="python-v1-compat-shim",
        ),
    ]
    diagnostics = diagnostics_from_warnings(metadata, warnings)

    # The SQLite diagnostics table replaces on diagnostic_id: identical
    # kind/path/message from two frontends must not collapse to one row.
    ids = {diagnostic.diagnostic_id for diagnostic in diagnostics}
    assert len(diagnostics) == 2
    assert len(ids) == 2
