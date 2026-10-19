"""Real backend equivalence and observable incremental collection obligations."""

import json

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.semantic import transport
from arcgraph.semantic.journal import load, open_artifact
from arcgraph.semantic.external_targets import file_path
from arcgraph.semantic.reuse import rebase
from arcgraph.semantic.uri_paths import workspace_path
from arcgraph.tests.project_environment_support import project_site_packages
from arcgraph.tests.test_batch30_build import (
    configure,
    generation_directory,
    graph_records,
)
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def write_files(project, files):
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            path.unlink()
        else:
            path.write_text(text, encoding="utf-8", newline="\n")


def active_build(project, output):
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    return directory


def assert_full(project, output, reference):
    active_build(project, reference)
    assert (
        GraphStoreReader.from_current(output).metadata["toolchain_status"][
            "semantic_core"
        ]["status"]
        == "active"
    )
    assert graph_records(output) == graph_records(reference)


def count_collection(monkeypatch):
    counts = []
    original = transport.collect

    def counted(*args, **kwargs):
        result = original(*args, **kwargs)
        counts.append(result["requests"]["prepare"])
        return result

    # build imports the callable directly, so observe its entry too.
    monkeypatch.setattr("arcgraph.semantic.build.collect", counted)
    return counts


def native_calls(directory, path):
    with open_artifact(generation_directory(directory) / "responses.jsonl") as handle:
        rows = [json.loads(line) for line in handle]
    root = rows[1]["params"]["rootUri"]
    return [
        json.dumps(rebase(row["result"], [(root, "file:///workspace")]), sort_keys=True)
        for row in rows
        if row.get("method") == "callHierarchy/outgoingCalls"
        and workspace_path(file_path(root), row["params"]["item"]["uri"]) == path
    ]


def test_unchanged_sync_does_not_start_pyright(prepared, tmp_path, monkeypatch):
    project, _, _, _ = prepared
    configure(project)
    output = tmp_path / "sync"
    active_build(project, output)
    counts = count_collection(monkeypatch)
    result = ArcGraphReindexer(project, output).reindex_changed()
    assert result["status"] == "unchanged"
    assert counts == [], "Unchanged sync must not enter pyright collection"


def test_only_affected_definitions_are_queried(prepared, tmp_path, monkeypatch):
    project, _, _, _ = prepared
    configure(project)
    write_files(
        project,
        {
            "demo.py": "def changed(): return 1\n",
            "unrelated.py": "def first(): return 1\ndef second(): return first()\n",
        },
    )
    output = tmp_path / "sync"
    active_build(project, output)
    counts = count_collection(monkeypatch)
    write_files(project, {"demo.py": "def changed(): return 2\n"})
    ArcGraphReindexer(project, output).reindex_changed()
    assert counts == [1], "Only the changed definition needs a backend request"
    assert_full(project, output, tmp_path / "full")


@pytest.mark.parametrize(
    "files,edit",
    [
        (
            {
                "provider.py": "class A:\n def run(self): pass\nclass B:\n def run(self): pass\ndef make(): return A()\n",
                "demo.py": "from provider import make\ndef use(): return make().run()\n",
            },
            {
                "provider.py": "class A:\n def run(self): pass\nclass B:\n def run(self): pass\ndef make(): return B()\n"
            },
        ),
        (
            {
                "base.py": "class Base:\n def run(self): return 1\n",
                "middle.py": "from base import Base\nclass Middle(Base): pass\n",
                "demo.py": "from middle import Middle\nclass Child(Middle): pass\ndef use(x: Child): return x.run()\n",
            },
            {"base.py": "class Base:\n def changed(self): return 2\n"},
        ),
        (
            {
                "pkg/__init__.py": "from .one import target\n",
                "pkg/one.py": "def target(): return 1\n",
                "pkg/two.py": "def target(): return 2\n",
                "demo.py": "from pkg import target\ndef use(): return target()\n",
            },
            {"pkg/__init__.py": "from .two import target\n"},
        ),
        (
            {
                "provider.py": "def make(): return 1\n",
                "provider.pyi": "class A:\n def run(self) -> int: ...\ndef make() -> A: ...\n",
                "demo.py": "from provider import make\ndef use(): return make().run()\n",
            },
            {
                "provider.pyi": "class B:\n def other(self) -> int: ...\ndef make() -> B: ...\n"
            },
        ),
        (
            {
                "provider.py": "def target(): return 1\n",
                "demo.py": "from provider import target\ndef use(): return target()\n",
            },
            {"provider.py": None},
        ),
        (
            {
                "provider.py": "def target(): return 1\n",
                "middle.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n from provider import *\n",
                "demo.py": "from middle import target\ndef use(): return target()\n",
            },
            {"provider.py": "def replacement(): return 2\n"},
        ),
        (
            {
                "__init__.py": "class A: pass\n",
                "demo.py": "from __init__ import A\ndef use(x: A): return x.run()\n",
            },
            {"__init__.py": "class A:\n def run(self): return 1\n"},
        ),
    ],
    ids=[
        "return-type",
        "mro",
        "package-reexport",
        "stub",
        "delete",
        "static-star",
        "root-initializer",
    ],
)
def test_cross_file_sync_equals_full(prepared, tmp_path, files, edit):
    project, _, _, _ = prepared
    configure(project)
    write_files(project, files)
    write_files(
        project,
        {
            "unrelated.py": "from collections.abc import MutableMapping\ndef keep(kwargs: MutableMapping[str, int]): return kwargs.setdefault('x', 1)\n"
        },
    )
    output = tmp_path / "sync"
    initial = active_build(project, output)
    before = native_calls(initial, "demo.py")
    write_files(project, edit)
    ArcGraphReindexer(project, output).reindex_changed()
    current = GraphStoreReader.from_current(output).sqlite_path.parent
    assert (
        native_calls(current, "demo.py") != before
    ), "The unchanged importer must receive a different native answer"
    assert_full(project, output, tmp_path / "full")


def test_reused_typeshed_is_unique_across_generations_and_sessions(
    prepared, tmp_path, monkeypatch
):
    project, _, _, _ = prepared
    configure(project)
    write_files(
        project,
        {
            "demo.py": "def changed(): return 1\n",
            "unrelated.py": "from collections.abc import MutableMapping\ndef one(kwargs: MutableMapping[str, int]): return kwargs.setdefault('x', 1)\ndef two(d: dict[str, int]): return d.setdefault('y', 2)\n",
        },
    )
    monkeypatch.setattr(transport, "SESSION_DEFINITIONS", 1)
    output = tmp_path / "sync"
    active_build(project, output)
    for index in range(2):
        write_files(project, {"demo.py": f"def changed(): return {index+2}\n"})
        ArcGraphReindexer(project, output).reindex_changed()
        current = GraphStoreReader.from_current(output).sqlite_path.parent
        detail = load(generation_directory(current) / "transport.json")
        assert detail["requests"]["prepare"] == 1
        assert detail["reuse"]["definitions"] == 2
        files = load(generation_directory(current) / "external-targets.json")
        names = [(row["origin"], row["relative"]) for row in files.values()]
        assert len(names) == len(set(names))
        monkeypatch.setattr(transport, "SESSION_DEFINITIONS", 10**9)
        assert_full(project, output, tmp_path / ("full-" + str(index)))


@pytest.mark.parametrize("change", ["environment", "artifact", "configuration"])
def test_unscanned_changes_refresh_semantics(prepared, tmp_path, change):
    project, environment, _, _ = prepared
    configure(project)
    output = tmp_path / "sync"
    directory = active_build(project, output)
    if change == "environment":
        path = project_site_packages(environment) / "dep_example/__init__.py"
        path.write_text(
            "def target(): return 'changed'\n", encoding="utf-8", newline="\n"
        )
    elif change == "artifact":
        path = generation_directory(directory) / "responses.jsonl"
        path.write_bytes(path.read_bytes() + b"tampered")
    else:
        path = project / "pyproject.toml"
        path.write_text(
            path.read_text(encoding="utf-8") + "# changed\n",
            encoding="utf-8",
            newline="\n",
        )
    result = ArcGraphReindexer(project, output).reindex_changed()
    assert result["status"] == "reindexed"
    assert_full(project, output, tmp_path / "full")


def test_switching_semantic_core_replaces_supports(prepared, tmp_path):
    project, _, _, _ = prepared
    configure(project)
    output = tmp_path / "sync"
    active_build(project, output)
    path = project / "pyproject.toml"
    for index, (old, new) in enumerate((("true", "false"), ("false", "true"))):
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "semantic_core = " + old, "semantic_core = " + new
            ),
            encoding="utf-8",
            newline="\n",
        )
        ArcGraphReindexer(project, output).reindex_changed()
        reference = tmp_path / ("full-" + str(index))
        ArcGraphIndexer(project, reference).build()
        assert graph_records(output) == graph_records(reference)


def test_environment_can_reexport_an_unresolved_workspace_member(prepared, tmp_path):
    project, environment, _, _ = prepared
    configure(project)
    write_files(
        project,
        {
            "provider.py": "class A: pass\n",
            "demo.py": "from dep_example import target\ndef use(): return target().run()\n",
        },
    )
    dependency = project_site_packages(environment) / "dep_example/__init__.py"
    dependency.write_text(
        "from provider import A\ndef target(): return A()\n",
        encoding="utf-8",
        newline="\n",
    )
    output = tmp_path / "sync"
    initial = active_build(project, output)
    before = native_calls(initial, "demo.py")
    write_files(project, {"provider.py": "class A:\n def run(self): return 1\n"})
    ArcGraphReindexer(project, output).reindex_changed()
    current = GraphStoreReader.from_current(output).sqlite_path.parent
    assert native_calls(current, "demo.py") != before
    assert_full(project, output, tmp_path / "full")


def test_physical_import_alias_under_a_source_root(prepared, tmp_path):
    from arcgraph.core.scanner import SourceRoot

    project, _, _, _ = prepared
    configure(project)
    write_files(
        project,
        {
            "src/provider.py": "class A: pass\n",
            "src/demo.py": "from src.provider import A\ndef use(x: A): return x.run()\n",
        },
    )
    roots = [SourceRoot("src")]
    output = tmp_path / "sync"
    metadata, initial = ArcGraphIndexer(project, output, source_roots=roots).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    before = native_calls(initial, "src/demo.py")
    write_files(project, {"src/provider.py": "class A:\n def run(self): return 1\n"})
    ArcGraphReindexer(project, output).reindex_changed()
    current = GraphStoreReader.from_current(output).sqlite_path.parent
    assert native_calls(current, "src/demo.py") != before
    reference = tmp_path / "full"
    metadata, _ = ArcGraphIndexer(project, reference, source_roots=roots).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    assert graph_records(output) == graph_records(reference)


def test_pyproject_scope_change_retracts_old_semantic_supports(prepared, tmp_path):
    project, _, _, _ = prepared
    configure(project)
    write_files(project, {"narrow/keep.py": "def keep(): return 1\n"})
    output = tmp_path / "sync"
    active_build(project, output)
    path = project / "pyproject.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'source_roots=["."]', 'source_roots=["narrow"]'
        ),
        encoding="utf-8",
        newline="\n",
    )
    ArcGraphReindexer(project, output).reindex_changed()
    assert_full(project, output, tmp_path / "full")


def test_typescript_only_edit_reuses_every_python_definition(
    prepared, tmp_path, monkeypatch
):
    project, _, _, _ = prepared
    configure(project)
    write_files(project, {"sample.ts": "export function first() { return 1; }\n"})
    output = tmp_path / "sync"
    active_build(project, output)
    counts = count_collection(monkeypatch)
    write_files(project, {"sample.ts": "export function second() { return 2; }\n"})
    ArcGraphReindexer(project, output).reindex_changed()
    assert counts == [0]
    assert_full(project, output, tmp_path / "full")


def test_reuse_rejects_a_changed_position_encoding(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from arcgraph.semantic.journal import dump

    root = tmp_path / "root"
    root.mkdir()
    output = tmp_path / "generation"
    output.mkdir()
    plan = output / "plan.json"
    dump({"snapshot_id": "snapshot", "definitions": []}, plan)

    class Peer:
        stderr = []

        def request(self, method, params):
            assert method == "initialize"
            return {
                "capabilities": {
                    "callHierarchyProvider": True,
                    "positionEncoding": "utf-16",
                }
            }

        def notify(self, method, params):
            pass

    @contextmanager
    def session(*args, **kwargs):
        yield Peer(), {"node": "node", "typeshed": str(tmp_path / "typeshed")}

    monkeypatch.setattr(transport, "session", session)
    with pytest.raises(ValueError, match="encoding"):
        transport.collect(
            plan,
            root,
            {},
            output,
            reuse=SimpleNamespace(encoding="utf-8", affected=set()),
        )


def test_verified_empty_search_path_can_hit_unchanged(prepared, tmp_path):
    project, environment, _, _ = prepared
    configure(project)
    outside = tmp_path / "unverified-search-root"
    outside.mkdir()
    site = project_site_packages(environment)
    (site / "unverified.pth").write_text(
        str(outside) + "\n", encoding="utf-8", newline="\n"
    )
    output = tmp_path / "sync"
    active_build(project, output)
    result = ArcGraphReindexer(project, output).reindex_changed()
    assert (
        result["status"] == "unchanged"
    ), "Content-bound empty search directories can count as unchanged"
    assert_full(project, output, tmp_path / "full")
