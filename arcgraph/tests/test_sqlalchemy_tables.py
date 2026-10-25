"""SQLAlchemy table declarations exclude migration-local schema snapshots."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arcgraph.adapters.sqlalchemy_adapter import SQLAlchemyAdapter
from arcgraph.core.schemas import FileRecord, Node


def _analyze(root: Path, sources: dict[str, str]):
    files = []
    trees = {}
    nodes = []
    for path, source in sources.items():
        absolute = root / path
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_text(source, encoding="utf-8")
        module = path.removesuffix(".py").replace("/", ".")
        files.append(
            FileRecord(
                path=path,
                abs_path=str(absolute),
                source_root=".",
                module=module,
                file_hash="fixture",
                line_count=len(source.splitlines()),
            )
        )
        trees[path] = ast.parse(source)
        for stmt in trees[path].body:
            if isinstance(stmt, ast.ClassDef):
                nodes.append(
                    Node(
                        id=f"class:{module}.{stmt.name}",
                        kind="class",
                        name=stmt.name,
                        qualname=f"{module}.{stmt.name}",
                        path=path,
                    )
                )
    return SQLAlchemyAdapter().analyze(files, trees, nodes, {f.module for f in files})


@pytest.mark.parametrize("marker", ["alembic.ini", "env.py"])
def test_migration_snapshots_do_not_define_tables(tmp_path: Path, marker: str) -> None:
    migration = tmp_path / "pkg" / "schema_history"
    (migration / "versions").mkdir(parents=True)
    (migration / marker).write_text("", encoding="utf-8")
    model = 'class Category:\n    __tablename__ = "categories"\n'
    result = _analyze(
        tmp_path,
        {
            "pkg/models.py": model,
            "pkg/schema_history/versions/old.py": model,
            "pkg/schema_history/helpers.py": model,
        },
    )
    assert [(node.id, node.path) for node in result.nodes] == [
        ("table:categories", "pkg/models.py")
    ]
    assert [(edge.source, edge.target, edge.kind) for edge in result.edges] == [
        ("class:pkg.models.Category", "table:categories", "maps_to")
    ]


def test_alembic_versions_path_excludes_tables_without_markers(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        {"pkg/alembic/versions/old.py": 'class Old:\n    __tablename__ = "old"\n'},
    )
    assert result.nodes == []
    assert result.edges == []


@pytest.mark.parametrize("directory", ["versions", "alembic", "schema_history"])
def test_directory_name_alone_does_not_hide_source_tables(
    tmp_path: Path, directory: str
) -> None:
    (tmp_path / directory).mkdir()
    (tmp_path / directory / "env.py").write_text("", encoding="utf-8")
    result = _analyze(
        tmp_path,
        {f"{directory}/models.py": 'class Source:\n    __tablename__ = "source"\n'},
    )
    assert [node.id for node in result.nodes] == ["table:source"]


def test_migration_markers_outside_repository_do_not_hide_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "versions").mkdir()
    (tmp_path / "env.py").write_text("", encoding="utf-8")
    result = _analyze(
        tmp_path / "repo",
        {"models.py": 'class Source:\n    __tablename__ = "source"\n'},
    )
    assert [node.id for node in result.nodes] == ["table:source"]
