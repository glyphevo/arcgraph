from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import BuildWarning
from arcgraph.interfaces.ci import run_ci_checks

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def test_reindex_changed_refreshes_modified_file(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    ArcGraphIndexer(repo_root, output_dir, roots).build()

    service_path = repo_root / "src" / "pkg" / "service.py"
    service_path.write_text(
        service_path.read_text(encoding="utf-8")
        + "\n\ndef new_helper() -> str:\n    return build_message('new')\n",
        encoding="utf-8",
    )

    result = ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    symbol = QueryEngine(output_dir).symbol("pkg.service.new_helper")

    assert result["status"] == "reindexed"
    assert result["changed"]["modified"] == ["src/pkg/service.py"]
    assert symbol["matches"][0]["id"] == "fn:pkg.service.new_helper"
    assert QueryEngine(output_dir).current()["freshness"]["status"] == "fresh"
    assert (
        QueryEngine(output_dir).current()["evidence_manifest"]["summary"]["total"] == 4
    )
    assert _semantic_fact_count(Path(result["build_dir"]) / "index.sqlite") > 0
    assert (Path(result["build_dir"]) / "merge_metrics.json").exists()


def test_build_auto_prunes_history_after_success(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    indexer: ArcGraphIndexer | None = None

    for _ in range(4):
        indexer = ArcGraphIndexer(repo_root, output_dir, roots)
        indexer.build()

    build_dirs = _build_dirs(output_dir)
    current = QueryEngine(output_dir).current()

    assert indexer is not None
    assert indexer.last_cleanup_result is not None
    assert indexer.last_cleanup_result["status"] == "pruned"
    assert len(build_dirs) == 3
    assert current["index_version"] in {path.name for path in build_dirs}


def test_reindex_changed_auto_prunes_history_after_success(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    ArcGraphIndexer(repo_root, output_dir, roots).build()
    oldest = _write_complete_build(output_dir, "20000101T000000000000Z-old")
    retained_old = _write_complete_build(output_dir, "20000102T000000000000Z-old")

    service_path = repo_root / "src" / "pkg" / "service.py"
    service_path.write_text(
        service_path.read_text(encoding="utf-8") + "\n# touched for cleanup\n",
        encoding="utf-8",
    )

    result = ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()

    assert result["status"] == "reindexed"
    assert result["cleanup"]["status"] == "pruned"
    assert result["cleanup"]["deleted_count"] == 1
    assert not oldest.exists()
    assert retained_old.exists()
    assert len(_build_dirs(output_dir)) == 3


def test_reindex_changed_preserves_cross_file_call_edges_from_modified_file(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    ArcGraphIndexer(repo_root, output_dir, roots).build()

    api_path = repo_root / "src" / "pkg" / "api.py"
    api_path.write_text(
        api_path.read_text(encoding="utf-8") + "\n# touched for reindex\n",
        encoding="utf-8",
    )

    result = ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    callees = {
        node["id"]
        for node in QueryEngine(output_dir).callees("pkg.api.hello")["callees"]
    }

    assert result["changed"]["modified"] == ["src/pkg/api.py"]
    assert "fn:pkg.service.build_message" in callees


def test_reindex_changed_preserves_v2_receiver_resolution_mode(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    ArcGraphIndexer(
        repo_root,
        output_dir,
        roots,
        enable_v2_call_resolution=True,
    ).build()

    api_path = repo_root / "src" / "pkg" / "api.py"
    api_path.write_text(
        api_path.read_text(encoding="utf-8") + "\n# touched for opt-in reindex\n",
        encoding="utf-8",
    )

    result = ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    engine = QueryEngine(output_dir)
    stats = engine.semantic_stats()
    greeter_callers = engine.callers("pkg.service.Greeter")

    assert result["changed"]["modified"] == ["src/pkg/api.py"]
    assert stats["capabilities"]["receiver_resolution"] == "available"
    assert stats["metrics"]["by_edge_kind"]["constructs"] > 0
    assert any(
        edge["kind"] == "constructs" and edge["source"] == "fn:pkg.api.hello"
        for edge in greeter_callers["edges"]
    )


def test_reindex_changed_preserves_full_index_summary_metrics(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    ArcGraphIndexer(
        repo_root,
        output_dir,
        roots,
        enable_v2_call_resolution=True,
    ).build()
    before = QueryEngine(output_dir).current()

    api_path = repo_root / "src" / "pkg" / "api.py"
    api_path.write_text(
        api_path.read_text(encoding="utf-8") + "\n# touched for metrics\n",
        encoding="utf-8",
    )

    ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    after = QueryEngine(output_dir).current()

    assert after["adapter_metrics"] == before["adapter_metrics"]
    assert after["precision_metrics"] == before["precision_metrics"]
    assert after["runtime_metrics"] == before["runtime_metrics"]


def test_reindex_changed_preserves_frontend_contract_metadata(tmp_path: Path) -> None:
    """A source-only reindex must not erase capabilities it cannot regenerate."""

    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    _metadata, build_dir = ArcGraphIndexer(repo_root, output_dir, roots).build()

    summary_path = build_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["extractor_metadata"] = {
        "fixture-extractor": {"language": "fixture", "version": "1"}
    }
    summary["toolchain_status"] = {
        "fixture-extractor": {"status": "available", "version": "1"}
    }
    summary["capabilities"]["external_toolchains"] = "available"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    before = QueryEngine(output_dir).current()
    assert before["language_tiers"]

    api_path = repo_root / "src" / "pkg" / "api.py"
    api_path.write_text(
        api_path.read_text(encoding="utf-8") + "\n# touched for metadata\n",
        encoding="utf-8",
    )

    ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    after = QueryEngine(output_dir).current()

    assert after["language_tiers"] == before["language_tiers"]
    assert after["extractor_metadata"] == before["extractor_metadata"]
    assert after["toolchain_status"] == before["toolchain_status"]
    assert after["capabilities"]["language_tiers"] == "available"
    assert after["capabilities"]["external_toolchains"] == "available"


def test_reindex_changed_removes_deleted_file_edges(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    ArcGraphIndexer(repo_root, output_dir, roots).build()

    (repo_root / "src" / "pkg" / "service.py").unlink()

    result = ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    engine = QueryEngine(output_dir)
    stats = engine.semantic_stats()
    ci_result = run_ci_checks(engine)
    stale_check = next(
        check for check in ci_result["checks"] if check["name"] == "stale_target_edges"
    )
    stale_properties = _diagnostic_properties(
        Path(result["build_dir"]) / "index.sqlite",
        "stale_target",
    )

    assert result["changed"]["deleted"] == ["src/pkg/service.py"]
    assert engine.symbol("pkg.service.build_message")["matches"] == []
    assert _dangling_edge_count(Path(result["build_dir"]) / "index.sqlite") == 0
    assert stats["metrics"]["by_diagnostic_kind"]["stale_target"] == 1
    assert any("target_stale:" in warning for warning in stats["warnings"])
    assert stale_properties["stale_edges_count"] > 0
    # Imported annotation consumers and explicitly collected pytest files are
    # refreshed automatically. Both must match a full build; still-unrefreshed
    # structural consumers retain the warning.
    assert "src/pkg/api.py" not in stale_properties["reanalysis_recommended_paths"]
    assert (
        "tests/service_cases.py" not in stale_properties["reanalysis_recommended_paths"]
    )
    full_output = tmp_path / "full"
    ArcGraphIndexer(repo_root, full_output, roots).build()

    def api_calls(output: Path):
        reader = GraphStoreReader.from_current(output)
        sources = {
            n.id
            for n in reader.read_nodes()
            if n.path in {"src/pkg/api.py", "tests/service_cases.py"}
        }
        return sorted(
            (e.source, e.target, e.kind, e.resolution.status)
            for e in reader.read_edges()
            if e.source in sources and e.kind in {"calls", "uses", "dynamic_call"}
        )

    assert api_calls(output_dir) == api_calls(full_output)
    assert stale_check["status"] == "warn"
    assert (
        stale_check["details"]["stale_edges_count"]
        == stale_properties["stale_edges_count"]
    )


def test_reindex_changed_prunes_unreferenced_pathless_external_symbols(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    service_path = repo_root / "src" / "pkg" / "service.py"
    original_source = service_path.read_text(encoding="utf-8")
    service_path.write_text(
        original_source
        + "\n\ndef transient_lookup(cache: dict[str, str]) -> str | None:\n"
        "    return cache.get('token')\n",
        encoding="utf-8",
    )
    _, build_dir = ArcGraphIndexer(
        repo_root,
        output_dir,
        roots,
        enable_v2_call_resolution=True,
    ).build()

    before_sqlite = build_dir / "index.sqlite"
    assert _node_count(before_sqlite, "extsym:builtins.dict.get") == 1
    assert _edge_target_count(before_sqlite, "extsym:builtins.dict.get") == 1

    service_path.write_text(original_source, encoding="utf-8")
    result = ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    after_sqlite = Path(result["build_dir"]) / "index.sqlite"

    assert result["changed"]["modified"] == ["src/pkg/service.py"]
    assert _edge_target_count(after_sqlite, "extsym:builtins.dict.get") == 0
    assert _node_count(after_sqlite, "extsym:builtins.dict.get") == 0
    assert _dangling_edge_count(after_sqlite) == 0


def test_reindex_changed_indexes_added_file(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    ArcGraphIndexer(repo_root, output_dir, roots).build()

    extra_path = repo_root / "src" / "pkg" / "extra.py"
    extra_path.write_text("def extra() -> str:\n    return 'extra'\n", encoding="utf-8")

    result = ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    symbol = QueryEngine(output_dir).symbol("pkg.extra.extra")

    assert result["changed"]["added"] == ["src/pkg/extra.py"]
    assert symbol["matches"][0]["id"] == "fn:pkg.extra.extra"


def test_reindex_changed_recomputes_similarity_edges(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    ArcGraphIndexer(repo_root, output_dir, roots).build()
    engine = QueryEngine(output_dir)

    assert {
        item["node"]["id"]
        for item in engine.similar("pkg.service.normalize_title")["similar"]
    } == {"fn:pkg.service.normalize_label"}

    service_path = repo_root / "src" / "pkg" / "service.py"
    source = service_path.read_text(encoding="utf-8")
    service_path.write_text(
        source.replace(
            "def normalize_label(value: str) -> str:\n"
            "    cleaned = value.strip()\n"
            "    return cleaned.lower()\n",
            "def normalize_label(value: str) -> str:\n" "    return value.upper()\n",
        ),
        encoding="utf-8",
    )

    ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    similar = QueryEngine(output_dir).similar("pkg.service.normalize_title")

    assert similar["similar"] == []


def test_reindex_rejects_stale_similarity_algorithms_without_publishing(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src"), SourceRoot("tests", "tests")]
    _metadata, build_dir = ArcGraphIndexer(repo_root, output_dir, roots).build()
    sqlite_path = build_dir / "index.sqlite"
    with sqlite3.connect(sqlite_path) as conn:
        rows = conn.execute(
            "SELECT source, target, properties_json FROM edges WHERE kind = 'similar_to'"
        ).fetchall()
        assert rows
        for source, target, properties_json in rows:
            properties = json.loads(properties_json)
            properties["algorithm"] = "python_ast_token_ngrams_v0"
            conn.execute(
                """
                UPDATE edges SET properties_json = ?
                WHERE source = ? AND target = ? AND kind = 'similar_to'
                """,
                (json.dumps(properties, sort_keys=True), source, target),
            )
    pointer_before = (output_dir / "current.json").read_text(encoding="utf-8")

    api_path = repo_root / "src" / "pkg" / "api.py"
    api_path.write_text(
        api_path.read_text(encoding="utf-8") + "\n# unrelated reindex\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="full ArcGraph build"):
        ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()

    assert (output_dir / "current.json").read_text(encoding="utf-8") == pointer_before
    similar_edges = [
        edge
        for edge in GraphStoreReader.from_current(output_dir).read_edges()
        if edge.kind == "similar_to"
    ]
    assert any(
        edge.properties.get("algorithm") == "python_ast_token_ngrams_v0"
        for edge in similar_edges
    )


def test_reindex_warning_deduplication_uses_warning_identity_fields() -> None:
    duplicate = BuildWarning(
        kind="typescript_express_route_unmounted",
        message="Router-local route has no static mount.",
        path="src/router.ts",
        frontend_name="typescript-static",
    )
    other_frontend = duplicate.model_copy(update={"frontend_name": "fixture"})
    other_path = duplicate.model_copy(update={"path": "src/other.ts"})

    deduplicated = ArcGraphReindexer._deduplicated_warnings(
        [duplicate, duplicate.model_copy(), other_frontend, other_path]
    )

    assert deduplicated == [duplicate, other_frontend, other_path]


def test_typescript_similarity_warning_bucket_matching_formats() -> None:
    matches = ArcGraphReindexer._typescript_similarity_warning_matches_buckets

    json_style = BuildWarning(
        kind="similarity_bucket_approximated",
        message='Approximated TypeScript similarity bucket "src/core" with 3 candidates.',
        frontend_name="typescript-static",
    )
    repr_style = BuildWarning(
        kind="similarity_edges_capped",
        message="Kept edges in TypeScript similarity bucket 'src/core' ranking.",
        frontend_name="typescript-static",
    )
    legacy_unquoted = BuildWarning(
        kind="similarity_bucket_approximated",
        message="Approximated TypeScript similarity bucket src/core with 3 candidates.",
        frontend_name="typescript-static",
    )

    assert matches(json_style, {"src/core"}) is True
    assert matches(repr_style, {"src/core"}) is True
    # Pre-JSON-quoting messages never matched; full rebuilds regenerate them.
    assert matches(legacy_unquoted, {"src/core"}) is False
    assert matches(json_style, {"src/other"}) is False
    assert matches(json_style, set()) is False


def test_python_similarity_warning_is_replaced_when_candidate_count_changes(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "dense.py").write_text(
        "\n\n".join(
            f"def f{index:03d}(value):\n    return value" for index in range(205)
        )
        + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src")]
    ArcGraphIndexer(repo_root, output_dir, roots).build()

    for revision in range(3):
        (source_root / f"added_{revision}.py").write_text(
            f"def added_{revision}(value):\n    return value\n",
            encoding="utf-8",
        )
        ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
        warnings = [
            warning
            for warning in GraphStoreReader.from_current(output_dir).read_warnings()
            if warning.kind == "similarity_bucket_skipped"
            and warning.frontend_name == "python-v1-compat-shim"
        ]
        assert len(warnings) == 1
        assert f"with {206 + revision} candidates" in warnings[0].message


def _python_similarity_state(output: Path) -> dict[str, object]:
    reader = GraphStoreReader.from_current(output)
    return {
        "edges": sum(1 for edge in reader.read_edges() if edge.kind == "similar_to"),
        "warnings": [
            warning.message
            for warning in reader.read_warnings()
            if warning.kind == "similarity_bucket_skipped"
            and warning.frontend_name == "python-v1-compat-shim"
        ],
        "functions": sum(1 for node in reader.read_nodes() if node.kind == "function"),
    }


def test_shared_call_config_is_pathless_with_surviving_callsite(
    tmp_path: Path,
) -> None:
    """Shared resources have no owner; surviving edges retain callsite evidence."""
    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "a.py").write_text(
        "import os\n\n"
        "def read_a() -> str | None:\n"
        "    return os.getenv('SHARED')\n",
        encoding="utf-8",
    )
    (source_root / "b.py").write_text(
        "import os\n\n"
        "def read_b() -> str | None:\n"
        "    return os.environ.get('SHARED')\n",
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(repo_root, incremental_output, roots).build()

    config_id = "config:env:SHARED"
    before = next(
        node
        for node in GraphStoreReader.from_current(incremental_output).read_nodes()
        if node.id == config_id
    )
    assert before.path is None
    assert before.properties == {
        "config_kind": "env",
        "key": "SHARED",
        "frontend_name": "shared-config-resource",
        "frontend_version": "1",
        "language": "neutral",
    }

    (source_root / "a.py").write_text(
        "import os\n\n"
        "def read_a() -> str | None:\n"
        "    return os.getenv('OTHER')\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(repo_root, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(repo_root, full_output, roots).build()

    def config_state(output: Path) -> dict[str, object]:
        node = next(
            candidate
            for candidate in GraphStoreReader.from_current(output).read_nodes()
            if candidate.id == config_id
        )
        return {
            "path": node.path,
            "start_line": node.start_line,
            "end_line": node.end_line,
            "properties": dict(node.properties),
        }

    incremental_state = config_state(incremental_output)
    assert incremental_state["path"] is None
    assert incremental_state["properties"] == {
        "config_kind": "env",
        "key": "SHARED",
        "frontend_name": "shared-config-resource",
        "frontend_version": "1",
        "language": "neutral",
    }
    witnesses = [
        e
        for e in GraphStoreReader.from_current(incremental_output).read_edges()
        if e.target == config_id
    ]
    assert len(witnesses) == 1
    assert witnesses[0].properties["callsite"]["raw_expression"] == "os.environ.get"
    assert witnesses[0].evidence[0].path == "src/b.py"
    assert incremental_state == config_state(full_output)


def test_shared_pydantic_config_is_pathless_with_surviving_witness(
    tmp_path: Path,
) -> None:
    """Pydantic declarations stay on witnesses, not on the shared key."""
    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "a.py").write_text(
        "from pydantic import Field\n"
        "from pydantic_settings import BaseSettings\n\n"
        "class SettingsA(BaseSettings):\n"
        '    alpha: str = Field(default="x", alias="SHARED")\n',
        encoding="utf-8",
    )
    (source_root / "b.py").write_text(
        "from pydantic import Field\n"
        "from pydantic_settings import BaseSettings\n\n"
        "class SettingsB(BaseSettings):\n"
        '    beta: str = Field(default="y", alias="SHARED")\n',
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(repo_root, incremental_output, roots).build()

    config_id = "config:env:SHARED"
    before = next(
        node
        for node in GraphStoreReader.from_current(incremental_output).read_nodes()
        if node.id == config_id
    )
    assert before.path is None
    assert before.properties == {
        "config_kind": "env",
        "key": "SHARED",
        "frontend_name": "shared-config-resource",
        "frontend_version": "1",
        "language": "neutral",
    }

    (source_root / "a.py").write_text(
        "from pydantic import Field\n"
        "from pydantic_settings import BaseSettings\n\n"
        "class SettingsA(BaseSettings):\n"
        '    alpha: str = Field(default="x", alias="OTHER")\n',
        encoding="utf-8",
    )
    ArcGraphReindexer(repo_root, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(repo_root, full_output, roots).build()

    def config_state(output: Path) -> dict[str, object]:
        node = next(
            candidate
            for candidate in GraphStoreReader.from_current(output).read_nodes()
            if candidate.id == config_id
        )
        return {
            "path": node.path,
            "start_line": node.start_line,
            "end_line": node.end_line,
            "properties": dict(node.properties),
        }

    incremental_state = config_state(incremental_output)
    assert incremental_state["path"] is None
    assert incremental_state["properties"] == {
        "config_kind": "env",
        "key": "SHARED",
        "frontend_name": "shared-config-resource",
        "frontend_version": "1",
        "language": "neutral",
    }
    witnesses = [
        e
        for e in GraphStoreReader.from_current(incremental_output).read_edges()
        if e.target == config_id
    ]
    assert {e.source for e in witnesses} == {
        "class:b.SettingsB",
        "field:b.SettingsB.beta",
    }
    assert all(v.path == "src/b.py" for e in witnesses for v in e.evidence)
    assert any(
        v.detail == "SettingsB.beta -> SHARED" for e in witnesses for v in e.evidence
    )
    assert incremental_state == config_state(full_output)


def test_shared_config_identity_is_independent_of_frontend_owner(
    tmp_path: Path,
) -> None:
    """Cross-frontend resources stay canonical while references retain provenance."""
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for cross-frontend config rescue.")

    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "a.py").write_text(
        "from pydantic_settings import BaseSettings\n\n"
        "class SettingsA(BaseSettings):\n"
        '    shared: str = "x"\n',
        encoding="utf-8",
    )
    (source_root / "b.ts").write_text(
        "export function readShared(): string | undefined {"
        " return process.env.SHARED; }\n",
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(repo_root, incremental_output, roots).build()

    config_id = "config:env:SHARED"
    before = next(
        node
        for node in GraphStoreReader.from_current(incremental_output).read_nodes()
        if node.id == config_id
    )
    assert before.path is None
    assert before.properties == {
        "config_kind": "env",
        "key": "SHARED",
        "frontend_name": "shared-config-resource",
        "frontend_version": "1",
        "language": "neutral",
    }

    (source_root / "a.py").write_text(
        "from pydantic_settings import BaseSettings\n\n"
        "class SettingsA(BaseSettings):\n"
        '    other: str = "y"\n',
        encoding="utf-8",
    )
    ArcGraphReindexer(repo_root, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(repo_root, full_output, roots).build()

    def config_state(output: Path) -> dict[str, object]:
        node = next(
            candidate
            for candidate in GraphStoreReader.from_current(output).read_nodes()
            if candidate.id == config_id
        )
        return {
            "path": node.path,
            "start_line": node.start_line,
            "end_line": node.end_line,
            "properties": dict(node.properties),
        }

    incremental_state = config_state(incremental_output)
    assert incremental_state["path"] is None
    assert incremental_state["properties"] == {
        "config_kind": "env",
        "key": "SHARED",
        "frontend_name": "shared-config-resource",
        "frontend_version": "1",
        "language": "neutral",
    }
    witnesses = [
        e
        for e in GraphStoreReader.from_current(incremental_output).read_edges()
        if e.target == config_id
    ]
    assert len(witnesses) == 1
    assert witnesses[0].properties["syntax"] == "process.env"
    assert witnesses[0].evidence[0].path == "src/b.ts"
    assert incremental_state == config_state(full_output)


def test_reindex_refreshes_typescript_similarity_when_python_call_target_changes(
    tmp_path: Path,
) -> None:
    """TS buckets that only reference a deleted Python route must still rescore."""
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for cross-frontend similarity refresh.")

    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "routes.py").write_text(
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n"
        '@app.get("/health")\n'
        "def health():\n"
        '    return {"ok": True}\n',
        encoding="utf-8",
    )
    (source_root / "a.ts").write_text(
        "export async function alpha(): Promise<Response> {\n"
        '  const response = await fetch("/health");\n'
        "  if (!response.ok) return response;\n"
        "  return response;\n"
        "}\n",
        encoding="utf-8",
    )
    (source_root / "b.ts").write_text(
        "export async function beta(): Promise<Response> {\n"
        '  const response = await fetch("/health");\n'
        "  if (!response.ok) return response;\n"
        "  return response;\n"
        "}\n",
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(repo_root, incremental_output, roots).build()

    before = next(
        edge
        for edge in GraphStoreReader.from_current(incremental_output).read_edges()
        if edge.kind == "similar_to"
    )
    assert any(
        reason.startswith("call_overlap=")
        for reason in before.properties.get("reasons", [])
    )

    (source_root / "routes.py").unlink()
    ArcGraphReindexer(repo_root, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(repo_root, full_output, roots).build()

    def similarity_state(output: Path) -> dict[str, object]:
        reader = GraphStoreReader.from_current(output)
        edge = next(item for item in reader.read_edges() if item.kind == "similar_to")
        profiles = {
            node.name: list(
                node.properties.get("similarity", {}).get("call_targets", [])
            )
            for node in reader.read_nodes()
            if node.name in {"alpha", "beta"}
        }
        return {
            "score": edge.properties.get("score"),
            "reasons": list(edge.properties.get("reasons", [])),
            "profiles": profiles,
        }

    incremental_state = similarity_state(incremental_output)
    assert incremental_state["profiles"] == {"alpha": [], "beta": []}
    assert not any(
        reason.startswith("call_overlap=") for reason in incremental_state["reasons"]
    )
    assert incremental_state == similarity_state(full_output)


def test_scorer_only_typescript_refresh_skips_express_mount_guard(
    tmp_path: Path,
) -> None:
    """Deleting a Python call target must rescore TS buckets even with Express present."""
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for scorer-only Express guard coverage.")

    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "routes.py").write_text(
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n"
        '@app.get("/health")\n'
        "def health():\n"
        '    return {"ok": True}\n',
        encoding="utf-8",
    )
    (source_root / "a.ts").write_text(
        "export async function alpha(): Promise<Response> {\n"
        '  const response = await fetch("/health");\n'
        "  if (!response.ok) return response;\n"
        "  return response;\n"
        "}\n",
        encoding="utf-8",
    )
    (source_root / "b.ts").write_text(
        "export async function beta(): Promise<Response> {\n"
        '  const response = await fetch("/health");\n'
        "  if (!response.ok) return response;\n"
        "  return response;\n"
        "}\n",
        encoding="utf-8",
    )
    (source_root / "server.ts").write_text(
        'import express from "express";\nexport const app = express();\n',
        encoding="utf-8",
    )
    (repo_root / "package.json").write_text(
        '{"type":"module","dependencies":{"express":"^4.0.0"}}\n',
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(repo_root, incremental_output, roots).build()

    (source_root / "routes.py").unlink()
    result = ArcGraphReindexer(repo_root, incremental_output, roots).reindex_changed()
    assert result["status"] == "reindexed"

    full_output = tmp_path / "full"
    ArcGraphIndexer(repo_root, full_output, roots).build()
    incremental_edge = next(
        edge
        for edge in GraphStoreReader.from_current(incremental_output).read_edges()
        if edge.kind == "similar_to"
    )
    full_edge = next(
        edge
        for edge in GraphStoreReader.from_current(full_output).read_edges()
        if edge.kind == "similar_to"
    )
    assert list(incremental_edge.properties.get("reasons", [])) == list(
        full_edge.properties.get("reasons", [])
    )
    assert not any(
        reason.startswith("call_overlap=")
        for reason in incremental_edge.properties.get("reasons", [])
    )


def test_python_similarity_deletion_below_skip_threshold_matches_full_build(
    tmp_path: Path,
) -> None:
    """Pure deletion that unblocks a skipped bucket must rescore all survivors."""
    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "dense.py").write_text(
        "\n\n".join(
            f"def f{index:03d}(value):\n    return value" for index in range(200)
        )
        + "\n",
        encoding="utf-8",
    )
    (source_root / "extra.py").write_text(
        "def extra(value):\n    return value\n",
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(repo_root, incremental_output, roots).build()
    before = _python_similarity_state(incremental_output)
    assert before["functions"] == 201
    assert before["edges"] == 0
    assert len(before["warnings"]) == 1

    (source_root / "extra.py").unlink()
    ArcGraphReindexer(repo_root, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(repo_root, full_output, roots).build()

    assert _python_similarity_state(incremental_output) == _python_similarity_state(
        full_output
    )
    assert _python_similarity_state(incremental_output)["edges"] == 19900
    assert _python_similarity_state(incremental_output)["warnings"] == []


def test_python_similarity_crossing_skip_threshold_drops_stale_edges(
    tmp_path: Path,
) -> None:
    """Adding callables that trip the skip cap must erase prior similar_to edges."""
    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "dense.py").write_text(
        "\n\n".join(
            f"def f{index:03d}(value):\n    return value" for index in range(199)
        )
        + "\n",
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(repo_root, incremental_output, roots).build()
    assert _python_similarity_state(incremental_output)["edges"] == 19701

    (source_root / "a.py").write_text(
        "def a(value):\n    return value\n", encoding="utf-8"
    )
    (source_root / "b.py").write_text(
        "def b(value):\n    return value\n", encoding="utf-8"
    )
    ArcGraphReindexer(repo_root, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(repo_root, full_output, roots).build()

    assert _python_similarity_state(incremental_output) == _python_similarity_state(
        full_output
    )
    assert _python_similarity_state(incremental_output)["edges"] == 0
    assert len(_python_similarity_state(incremental_output)["warnings"]) == 1


def _copy_fixture(tmp_path: Path) -> Path:
    repo_root = tmp_path / "sample_project"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    return repo_root


def _build_dirs(output_dir: Path) -> list[Path]:
    return sorted(path for path in (output_dir / "builds").iterdir() if path.is_dir())


def _write_complete_build(output_dir: Path, name: str) -> Path:
    build_dir = output_dir / "builds" / name
    build_dir.mkdir(parents=True)
    (build_dir / "index.sqlite").write_bytes(b"sqlite")
    (build_dir / "summary.json").write_text("{}", encoding="utf-8")
    return build_dir


def _dangling_edge_count(sqlite_path: Path) -> int:
    with sqlite3.connect(sqlite_path) as conn:
        return conn.execute("""
            SELECT COUNT(*)
            FROM edges e
            LEFT JOIN nodes s ON s.id = e.source
            LEFT JOIN nodes t ON t.id = e.target
            WHERE s.id IS NULL OR t.id IS NULL
            """).fetchone()[0]


def _semantic_fact_count(sqlite_path: Path) -> int:
    with sqlite3.connect(sqlite_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM semantic_facts").fetchone()[0]


def _node_count(sqlite_path: Path, node_id: str) -> int:
    with sqlite3.connect(sqlite_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()[0]


def _edge_target_count(sqlite_path: Path, target: str) -> int:
    with sqlite3.connect(sqlite_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM edges WHERE target = ?",
            (target,),
        ).fetchone()[0]


def _diagnostic_properties(
    sqlite_path: Path, diagnostic_kind: str
) -> dict[str, object]:
    with sqlite3.connect(sqlite_path) as conn:
        row = conn.execute(
            "SELECT properties_json FROM diagnostics WHERE diagnostic_kind = ?",
            (diagnostic_kind,),
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def test_reindex_replaces_emit_skip_warning_instead_of_accumulating(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for TypeScript reindex tests.")
    repo_root = tmp_path / "repo"
    src = repo_root / "src"
    src.mkdir(parents=True)
    (src / "util.ts").write_text("export const util = 1;\n", encoding="utf-8")
    (src / "util.js").write_text(
        "exports.util = 1;\n//# sourceMappingURL=util.js.map\n", encoding="utf-8"
    )
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src")]
    ArcGraphIndexer(repo_root, output_dir, roots).build()

    def emit_skip_warnings() -> list[BuildWarning]:
        return [
            warning
            for warning in GraphStoreReader.from_current(output_dir).read_warnings()
            if warning.kind == "typescript_emit_artifact_skipped"
        ]

    first = emit_skip_warnings()
    assert len(first) == 1
    assert "Skipped 1 runtime file(s)" in first[0].message

    # A second artifact appears: the summary must be replaced, not joined by
    # a stale copy with the old count.
    (src / "extra.ts").write_text("export const extra = 1;\n", encoding="utf-8")
    (src / "extra.js").write_text(
        "exports.extra = 1;\n//# sourceMappingURL=extra.js.map\n", encoding="utf-8"
    )
    ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    second = emit_skip_warnings()
    assert len(second) == 1
    assert "Skipped 2 runtime file(s)" in second[0].message

    # Both artifacts lose their provenance markers and become source: the
    # summary must disappear entirely.
    (src / "util.js").write_text("exports.util = 1;\n", encoding="utf-8")
    (src / "extra.js").write_text("exports.extra = 1;\n", encoding="utf-8")
    ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    assert emit_skip_warnings() == []


def test_reindex_refreshes_emit_warning_when_only_skipped_set_changes(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for TypeScript reindex tests.")
    repo_root = tmp_path / "repo"
    src = repo_root / "src"
    src.mkdir(parents=True)
    (src / "util.ts").write_text("export const util = 1;\n", encoding="utf-8")
    (src / "util2.ts").write_text("export const util2 = 1;\n", encoding="utf-8")
    (src / "util.js").write_text(
        "exports.util = 1;\n//# sourceMappingURL=util.js.map\n", encoding="utf-8"
    )
    output_dir = tmp_path / "arcgraph"
    roots = [SourceRoot("src")]
    ArcGraphIndexer(repo_root, output_dir, roots).build()

    def emit_skip_warnings() -> list[BuildWarning]:
        return [
            warning
            for warning in GraphStoreReader.from_current(output_dir).read_warnings()
            if warning.kind == "typescript_emit_artifact_skipped"
        ]

    assert "Skipped 1 runtime file(s)" in emit_skip_warnings()[0].message

    # A skipped emit artifact never enters the scanned file set, so this add
    # produces no file delta -- the summary itself must count as a change.
    (src / "util2.js").write_text(
        "exports.util2 = 1;\n//# sourceMappingURL=util2.js.map\n", encoding="utf-8"
    )
    result = ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    assert result["status"] != "unchanged"
    warnings = emit_skip_warnings()
    assert len(warnings) == 1
    assert "Skipped 2 runtime file(s)" in warnings[0].message

    (src / "util.js").unlink()
    (src / "util2.js").unlink()
    result = ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    assert result["status"] != "unchanged"
    assert emit_skip_warnings() == []
