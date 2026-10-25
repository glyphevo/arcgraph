"""SQLAlchemy table declarations exclude migration-local schema snapshots."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arcgraph.adapters.sqlalchemy_adapter import SQLAlchemyAdapter, _is_migration_file
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
        ("table:categories", "pkg/models.py"),
        ("table:categories", "pkg/schema_history/helpers.py"),
    ]
    assert [(edge.source, edge.target, edge.kind) for edge in result.edges] == [
        ("class:pkg.models.Category", "table:categories", "maps_to"),
        ("class:pkg.schema_history.helpers.Category", "table:categories", "maps_to"),
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


@pytest.mark.parametrize(
    "imports,table,fk",
    [
        ("import sqlalchemy as sa", "sa.Table", "sa.ForeignKey"),
        ("from sqlalchemy import Table, ForeignKey", "Table", "ForeignKey"),
        ("from sqlalchemy import Table as T, ForeignKey as FK", "T", "FK"),
        ("from sqlalchemy.sql.schema import Table, ForeignKey", "Table", "ForeignKey"),
    ],
)
def test_module_table_declarations_and_literal_foreign_keys(
    tmp_path: Path, imports: str, table: str, fk: str
) -> None:
    result = _analyze(
        tmp_path,
        {
            "pkg/models.py": (
                imports + "\n"
                'class User:\n    __tablename__ = "users"\n'
                f'links = {table}("links", Base.metadata,\n'
                f'    Column("user_id", {fk}("users.id")),\n'
                f'    Column("owner_id", {fk}("users.id")),\n'
                f'    Column("dynamic", {fk}(target)),\n'
                f'    Column("plain", {fk}("not_a_column")))\n'
            )
        },
    )
    by_id = {node.id: node for node in result.nodes}
    assert set(by_id) == {"table:links", "table:users"}
    link = by_id["table:links"]
    assert link.qualname == "links"
    assert link.path == "pkg/models.py"
    assert (link.start_line, link.end_line) == (4, 8)
    assert link.properties == {
        "orm": "sqlalchemy",
        "table_name": "links",
        "declaration": "table",
    }
    references = [edge for edge in result.edges if edge.kind == "references"]
    assert [(edge.source, edge.target) for edge in references] == [
        ("table:links", "table:users"),
        ("table:links", "table:users"),
    ]
    assert [edge.evidence[0].start_line for edge in references] == [5, 6]
    assert all(edge.evidence[0].kind == "sqlalchemy_resource" for edge in references)


def test_dynamic_table_names_are_skipped_with_a_diagnostic(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        {"models.py": "from sqlalchemy import Table\nlinks = Table(name, metadata)\n"},
    )
    assert result.nodes == []
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].path == "models.py"
    assert "literal" in result.diagnostics[0].message
    assert "line 2" in result.diagnostics[0].message


def test_table_recognition_excludes_other_apis_nested_calls_and_migrations(
    tmp_path: Path,
) -> None:
    result = _analyze(
        tmp_path,
        {
            "models.py": (
                "from other import Table\n"
                'fake = Table("fake", metadata)\n'
                "import sqlalchemy as sa\n"
                'def build():\n    return sa.Table("nested", metadata)\n'
                'class Container:\n    nested = sa.Table("class_table", metadata)\n'
            ),
            "alembic/versions/old.py": (
                'import sqlalchemy as sa\nold = sa.Table("old", metadata)\n'
                'op.create_table("old_op")\n'
            ),
        },
    )
    assert result.nodes == []
    assert result.edges == []
    assert result.diagnostics == []


def test_foreign_table_without_a_declaration_has_no_invented_location(
    tmp_path: Path,
) -> None:
    result = _analyze(
        tmp_path,
        {
            "models.py": (
                "from sqlalchemy import Table, ForeignKey\n"
                'links = Table("links", metadata, Column(ForeignKey("public.users.id")))\n'
            )
        },
    )
    by_id = {node.id: node for node in result.nodes}
    target = by_id["table:public.users"]
    assert target.path is None
    assert target.properties["resolution_status"] == "unresolved"
    assert [(edge.source, edge.target, edge.kind) for edge in result.edges] == [
        ("table:links", "table:public.users", "references")
    ]


@pytest.mark.parametrize("directory", ["", "pkg", "svc/db"])
@pytest.mark.parametrize("marker", ["env.py", "alembic.ini"])
def test_migration_marker_only_excludes_files_under_versions(
    tmp_path: Path, directory: str, marker: str
) -> None:
    history = tmp_path / directory
    (history / "versions").mkdir(parents=True)
    (history / marker).write_text("", encoding="utf-8")
    prefix = directory + "/" if directory else ""
    result = _analyze(
        tmp_path,
        {
            prefix + "models.py": 'class Source:\n    __tablename__ = "source"\n',
            prefix + "versions/old.py": 'class Old:\n    __tablename__ = "old"\n',
        },
    )
    assert [(node.id, node.path) for node in result.nodes] == [
        ("table:source", prefix + "models.py")
    ]


@pytest.mark.parametrize(
    "path",
    ["pkg/../models.py", "pkg/../pkg/../models.py", "alembic/versions/../../models.py"],
)
def test_migration_ancestor_checks_normalize_dot_dot_without_leaving_repo(
    tmp_path: Path, path: str
) -> None:
    (tmp_path / "versions").mkdir()
    (tmp_path / "env.py").write_text("", encoding="utf-8")
    result = _analyze(
        tmp_path / "repo", {path: 'class Source:\n    __tablename__ = "source"\n'}
    )
    assert [node.id for node in result.nodes] == ["table:source"]


def test_normalized_absolute_path_and_relative_dot_dot_stay_in_repository(
    tmp_path: Path,
) -> None:
    (tmp_path / "versions").mkdir()
    (tmp_path / "env.py").write_text("", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    record = FileRecord(
        path="pkg/../models.py",
        abs_path=str(repo / "models.py"),
        source_root=".",
        module="models",
        file_hash="fixture",
        line_count=1,
    )
    assert _is_migration_file(record) is False


@pytest.mark.parametrize(
    "assignment",
    ['links = Table("links", metadata)', 'links: Table = Table("links", metadata)'],
)
def test_sql_schema_table_alias_and_annotated_assignments(
    tmp_path: Path, assignment: str
) -> None:
    result = _analyze(
        tmp_path,
        {"models.py": "from sqlalchemy.sql.schema import Table\n" + assignment + "\n"},
    )
    assert [node.id for node in result.nodes] == ["table:links"]
    assert result.diagnostics == []


def test_main_table_alias_with_annotated_assignment(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        {
            "models.py": 'import sqlalchemy as sa\nlinks: sa.Table = sa.Table("links", metadata)\n'
        },
    )
    assert [node.id for node in result.nodes] == ["table:links"]


@pytest.mark.parametrize(
    "declaration",
    [
        'links = sa.Table("links", metadata).alias("named")',
        'links = sa.sql.expression.Table("links", metadata)',
        'sa.Table("links", metadata)',
    ],
)
def test_unsupported_sqlalchemy_table_forms_are_diagnosed(
    tmp_path: Path, declaration: str
) -> None:
    result = _analyze(
        tmp_path, {"models.py": "import sqlalchemy as sa\n" + declaration + "\n"}
    )
    assert result.nodes == []
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].kind == "adapter_quality_warning"
    assert "Table" in result.diagnostics[0].message
    assert "line 2" in result.diagnostics[0].message
