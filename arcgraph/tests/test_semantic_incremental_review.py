"""Review regressions: implicit exports, external roots and graph-bound reuse."""

import ast
import gzip
import hashlib
import json
from types import SimpleNamespace

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.semantic.journal import dump, load
from arcgraph.semantic.reuse import dependency_index, guard_external_dependencies
from arcgraph.semantic.snapshot import sha
from arcgraph.tests.test_semantic_incremental import (
    active_build,
    assert_full,
    configure,
    count_collection,
    generation_directory,
    graph_records,
    native_calls,
    write_files,
)
from arcgraph.tests.test_semantic_isolation import prepared as prepared


@pytest.mark.parametrize("initializer", ["__init__.py", "__init__.pyi"])
@pytest.mark.parametrize("existing", [False, True], ids=["add", "modify"])
def test_package_all_submodule_sync_equals_full(
    prepared, tmp_path, initializer, existing
):
    project, _, _, _ = prepared
    configure(project)
    files = {
        "demo.py": "from pkg import *\ndef use(): return helper.make().run()\n",
        "pkg/" + initializer: '__all__ = ["helper"]\n',
        "unrelated.py": "def keep(): return 1\n",
    }
    if existing:
        files["pkg/helper.py"] = "value = 1\n"
    write_files(project, files)
    output = tmp_path / "sync"
    initial = active_build(project, output)
    before = native_calls(initial, "demo.py")
    write_files(
        project,
        {
            "pkg/helper.py": "class A:\n def run(self): return 1\ndef make(): return A()\n"
        },
    )
    ArcGraphReindexer(project, output).reindex_changed()
    assert_full(project, output, tmp_path / "full")
    current = GraphStoreReader.from_current(output).sqlite_path.parent
    assert native_calls(current, "demo.py") != before
    detail = load(generation_directory(current) / "transport.json")
    assert detail["reuse"]["definitions"] == 1
    assert "demo.py" in detail["reuse"]["affected_paths"]


@pytest.mark.parametrize(
    "source",
    [
        '__all__ = ["helper"] + ["other"]',
        '__all__ = ["helper"]; __all__ += ["other"]',
        '__all__ = []; __all__.extend(["helper"])',
        '__all__ = []; __all__.append("helper")',
        "from exports import __all__",
        "from exports import names as __all__",
        "import exports as __all__",
        "__all__ = exports.names",
        '__all__ = ["helper", unknown]',
        '__all__ = ["helper"]; alias = __all__',
        '__all__ = alias = ["helper"]',
        "def __all__(): pass",
        "try: pass\nexcept Exception as __all__: pass",
        "match ['helper']:\n case __all__: pass",
        "match ['helper']:\n case [*__all__]: pass",
        "match {}:\n case {**__all__}: pass",
    ],
)
def test_dynamic_package_all_disables_dependency_reuse(source):
    files = [SimpleNamespace(path="pkg/__init__.py", module="pkg")]
    assert (
        dependency_index(files, {files[0].path: ast.parse(source)}, [SourceRoot(".")])
        is None
    )


@pytest.mark.parametrize(
    "path,module", [("pkg/__init__.py", "pkg"), ("__init__.py", "__init__")]
)
@pytest.mark.parametrize(
    "source", ['__all__ = ("helper",)', '__all__: list[str] = ["helper"]']
)
def test_literal_package_all_includes_submodule_dependency(path, module, source):
    result = dependency_index(
        [SimpleNamespace(path=path, module=module)],
        {path: ast.parse(source)},
        [SourceRoot(".")],
    )
    assert ("pkg.helper" if module == "pkg" else "helper") in result["imports"][path]


@pytest.mark.parametrize("name", ["utils", "models"])
def test_common_module_edit_keeps_unrelated_responses(
    prepared, tmp_path, monkeypatch, name
):
    project, _, _, _ = prepared
    configure(project)
    write_files(
        project,
        {
            "demo.py": f"from {name} import changed\ndef use(): return changed()\n",
            name + ".py": "def changed(): return 1\n",
            "unrelated.py": "def keep(): return 1\n",
        },
    )
    output = tmp_path / "sync"
    active_build(project, output)
    counts = count_collection(monkeypatch)
    write_files(project, {name + ".py": "def changed(): return 2\n"})
    ArcGraphReindexer(project, output).reindex_changed()
    assert counts == [2], "Only the provider and its importer need new queries"
    assert_full(project, output, tmp_path / "full")


def test_external_guard_uses_only_absolute_module_roots(tmp_path):
    environment, typeshed = tmp_path / "environment", tmp_path / "typeshed"
    environment.mkdir()
    typeshed.mkdir()
    (typeshed / "imports.pyi").write_text(
        "from .utils import models\nfrom ..config import app\n"
        "from absolute.sub import (\n models as utils,\n)\n"
        "import first.sub as config, second as app; from third import utils\n"
        "def gen(utils, models):\n yield from utils\n yield from models\n",
        encoding="utf-8",
    )
    names = ["utils", "models", "config", "app", "absolute", "first", "second", "third"]
    index = {"modules": {n + ".py": [n] for n in names}, "imports": {}}
    assert guard_external_dependencies(index, environment, typeshed)[
        "external_roots"
    ] == ["absolute", "first", "second", "third"]
    (typeshed / "broken.pyi").write_text("utils = (models\n", encoding="utf-8")
    blocked = guard_external_dependencies(index, environment, typeshed)[
        "external_roots"
    ]
    assert {"utils", "models"}.issubset(
        blocked
    ), "Tokenization must fail closed for the entire file"


def test_path_pth_allows_unchanged_and_incremental_sync(
    prepared, tmp_path, monkeypatch
):
    project, environment, markers, _ = prepared
    configure(project)
    outside = tmp_path / "editable"
    outside.mkdir()
    (outside / "hidden.py").write_text("def target(): return 1\n", encoding="utf-8")
    site = next(environment.glob("lib/python*/site-packages"))
    (site / "editable.pth").write_text(str(outside) + "\n", encoding="utf-8")
    write_files(
        project,
        {
            "demo.py": "def changed(): return 1\n",
            "unrelated.py": "def keep(): return 1\n",
        },
    )
    output = tmp_path / "sync"
    active_build(project, output)
    counts = count_collection(monkeypatch)
    assert ArcGraphReindexer(project, output).reindex_changed()["status"] == "unchanged"
    assert counts == []
    write_files(project, {"demo.py": "def changed(): return 2\n"})
    ArcGraphReindexer(project, output).reindex_changed()
    assert counts == [1]
    assert_full(project, output, tmp_path / "full")
    assert not any(p.exists() for p in markers.values())


@pytest.mark.parametrize("prefix", ["", "\ufeff"], ids=["plain", "bom"])
def test_path_pth_target_content_invalidates_reuse(
    prepared, tmp_path, monkeypatch, prefix
):
    project, environment, _, _ = prepared
    configure(project)
    outside = tmp_path / "editable"
    outside.mkdir()
    path = outside / "pth_hidden.py"
    path.write_text("from leaf import A\ndef make(): return A()\n", encoding="utf-8")
    site = next(environment.glob("lib/python*/site-packages"))
    (site / "editable.pth").write_text(prefix + str(outside) + "\n", encoding="utf-8")
    write_files(
        project,
        {
            "demo.py": "from pth_hidden import make\ndef use(): return make().run()\n",
            "leaf.py": "class A:\n def run(self): return 1\nclass B:\n def run(self): return 2\n",
        },
    )
    output = tmp_path / "sync"
    initial = active_build(project, output)
    before = native_calls(initial, "demo.py")
    counts = count_collection(monkeypatch)
    path.write_text("from leaf import B\ndef make(): return B()\n", encoding="utf-8")
    result = ArcGraphReindexer(project, output).reindex_changed()
    assert result["status"] == "reindexed"
    assert counts == [5], "An external search input change forces full collection"
    current = GraphStoreReader.from_current(output).sqlite_path.parent
    assert native_calls(current, "demo.py") != before
    assert_full(project, output, tmp_path / "full")


def test_pth_change_during_collection_is_not_adopted(prepared, tmp_path, monkeypatch):
    from arcgraph.semantic.transport import collect

    project, environment, _, _ = prepared
    configure(project)
    outside = tmp_path / "editable"
    outside.mkdir()
    path = outside / "helper.py"
    path.write_text("def target(): return 1\n", encoding="utf-8")
    site = next(environment.glob("lib/python*/site-packages"))
    (site / "editable.pth").write_text(str(outside) + "\n", encoding="utf-8")

    def changed(*args, **kwargs):
        result = collect(*args, **kwargs)
        path.write_text("def target(): return 2\n", encoding="utf-8")
        return result

    monkeypatch.setattr("arcgraph.semantic.build.collect", changed)
    metadata, _ = ArcGraphIndexer(project, tmp_path / "out").build()
    status = metadata.toolchain_status["semantic_core"]
    assert status["status"] == "unavailable"
    assert "input_changed_during_run" in status["reason"]


def forge_responses(directory):
    path = directory / "responses.jsonl"
    lines = gzip.decompress(path.read_bytes()).splitlines(keepends=True)
    body = []
    changed = 0
    for line in lines[1:-1]:
        row = json.loads(line)
        if row.get("method") == "callHierarchy/outgoingCalls":
            row["result"] = []
            line = (json.dumps(row, separators=(",", ":")) + "\n").encode()
            changed += 1
        body.append(line)
    assert changed > 0
    footer = {
        "complete": True,
        "count": len(body),
        "checksum": hashlib.sha256(b"".join(body)).hexdigest(),
    }
    path.write_bytes(
        gzip.compress(
            lines[0] + b"".join(body) + (json.dumps(footer) + "\n").encode(), mtime=0
        )
    )
    manifest = load(directory / "manifest.json")
    manifest["artifacts"]["responses.jsonl"] = sha(path.read_bytes())
    dump(manifest, directory / "manifest.json")


@pytest.mark.parametrize(
    "source_edit", [False, True], ids=["no-source-edit", "source-edit"]
)
def test_consistent_forgery_cannot_enter_next_generation(
    prepared, tmp_path, monkeypatch, source_edit
):
    project, _, _, _ = prepared
    configure(project)
    write_files(
        project,
        {
            "demo.py": "def changed(): return 1\n",
            "keep.py": "def target(): return 1\ndef use(): return target()\n",
        },
    )
    output = tmp_path / "sync"
    initial = active_build(project, output)
    forge_responses(generation_directory(initial))
    if source_edit:
        write_files(project, {"demo.py": "def changed(): return 2\n"})
    counts = count_collection(monkeypatch)
    ArcGraphReindexer(project, output).reindex_changed()
    assert counts == [
        3
    ], "A journal and manifest rewritten together must force full collection"
    assert_full(project, output, tmp_path / "full")


@pytest.mark.parametrize("roots", ['["src", "lib"]', '["lib"]'])
@pytest.mark.parametrize("if_stale", [False, True])
def test_default_sync_redetects_configured_source_roots(
    prepared, tmp_path, roots, if_stale
):
    project, _, _, _ = prepared
    write_files(
        project,
        {
            "src/demo.py": "from extra import target\ndef use(): return target()\n",
            "lib/extra.py": "def target(): return 1\n",
        },
    )
    path = project / "pyproject.toml"
    initial = path.read_text(encoding="utf-8").replace(
        'source_roots=["."]', 'source_roots=["src"]'
    )
    path.write_text(initial, encoding="utf-8")
    output = tmp_path / "sync"
    ArcGraphIndexer(project, output).build()
    path.write_text(initial.replace('["src"]', roots), encoding="utf-8")
    if if_stale:
        from arcgraph.interfaces.ops import sync_index

        result = sync_index(repo_root=project, output_dir=output, if_stale=True)
        assert result.get("action") == "reindexed"
        assert result["published"]
    else:
        ArcGraphReindexer(project, output).reindex_changed()
    reference = tmp_path / "full"
    ArcGraphIndexer(project, reference).build()
    assert graph_records(output) == graph_records(reference)
    assert (
        GraphStoreReader.from_current(output).metadata["source_root_specs"]
        == GraphStoreReader.from_current(reference).metadata["source_root_specs"]
    )


def test_default_sync_redetects_automatic_source_roots(tmp_path):
    project = tmp_path / "project"
    write_files(
        project, {"one/__init__.py": "", "one/demo.py": "def keep(): return 1\n"}
    )
    output = tmp_path / "sync"
    ArcGraphIndexer(project, output).build()
    write_files(
        project, {"two/__init__.py": "", "two/extra.py": "def added(): return 2\n"}
    )
    ArcGraphReindexer(project, output).reindex_changed()
    reference = tmp_path / "full"
    ArcGraphIndexer(project, reference).build()
    assert graph_records(output) == graph_records(reference)


@pytest.mark.parametrize("strategy", [None, "legacy_roots"])
def test_default_sync_preserves_explicit_and_legacy_roots(tmp_path, strategy):
    from arcgraph.core.scanner import SourceRootDetection

    project = tmp_path / "project"
    write_files(
        project,
        {
            "src/demo.py": "def keep(): return 1\n",
            "pyproject.toml": '[tool.arcgraph]\nsource_roots=["src"]\n',
        },
    )
    output = tmp_path / "sync"
    roots = [SourceRoot("src")]
    detection = SourceRootDetection(strategy, ("src",)) if strategy else None
    ArcGraphIndexer(
        project, output, source_roots=roots, source_root_detection=detection
    ).build()
    write_files(
        project,
        {
            "lib/extra.py": "def added(): return 2\n",
            "pyproject.toml": '[tool.arcgraph]\nsource_roots=["lib"]\n',
        },
    )
    ArcGraphReindexer(project, output).reindex_changed()
    reference = tmp_path / "full"
    ArcGraphIndexer(
        project, reference, source_roots=roots, source_root_detection=detection
    ).build()
    assert graph_records(output) == graph_records(reference)


@pytest.mark.parametrize("prefix", ["", "\ufeff"], ids=["plain", "bom"])
def test_pth_directory_identity_covers_relative_paths_and_additions(tmp_path, prefix):
    from arcgraph.semantic.search_paths import search_identity

    environment = tmp_path / "environment"
    site = environment / "lib/python3.11/site-packages"
    site.mkdir(parents=True)
    (site / "editable.pth").write_text(prefix + "vendor\n", encoding="utf-8")
    vendor = site / "vendor"
    vendor.mkdir()
    before = search_identity(environment)
    assert before == search_identity(environment, virtual=True)
    (vendor / "helper.pyi").write_text("def target() -> int: ...\n", encoding="utf-8")
    after = search_identity(environment)
    assert before != after
    assert after == search_identity(environment, virtual=True)
    (vendor / "helper.pyi").unlink()
    assert search_identity(environment) == before


def test_relative_pth_source_alias_invalidates_importers(
    prepared, tmp_path, monkeypatch
):
    project, environment, _, _ = prepared
    configure(project)
    site = next(environment.glob("lib/python*/site-packages"))
    (site / "source.pth").write_text("../../../../source/nested\n", encoding="utf-8")
    write_files(
        project,
        {
            "nested/helper.py": "class A: pass\n",
            "demo.py": "from helper import A\ndef use(x: A): return x.run()\n",
            "unrelated.py": "def keep(): return 1\n",
        },
    )
    output = tmp_path / "sync"
    initial = active_build(project, output)
    before = native_calls(initial, "demo.py")
    counts = count_collection(monkeypatch)
    assert ArcGraphReindexer(project, output).reindex_changed()["status"] == "unchanged"
    assert counts == []
    write_files(project, {"nested/helper.py": "class A:\n def run(self): return 1\n"})
    ArcGraphReindexer(project, output).reindex_changed()
    assert counts == [3]
    current = GraphStoreReader.from_current(output).sqlite_path.parent
    assert native_calls(current, "demo.py") != before
    assert_full(project, output, tmp_path / "full")
