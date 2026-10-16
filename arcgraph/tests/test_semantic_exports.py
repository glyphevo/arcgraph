"""Review regressions for semantic file deltas and literal package exports."""

import ast
from types import SimpleNamespace

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.ops import sync_index
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.semantic.journal import load
from arcgraph.semantic.reuse import dependency_index
from arcgraph.tests.test_semantic_incremental import (
    active_build,
    assert_full,
    configure,
    count_collection,
    generation_directory,
    native_calls,
    write_files,
)
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def test_semantic_sync_reports_modified_file(prepared, tmp_path):
    project, _, _, _ = prepared
    configure(project)
    write_files(project, {"demo.py": "def changed(): return 1\n"})
    output = tmp_path / "sync"
    active_build(project, output)
    write_files(project, {"demo.py": "def changed(): return 2\n"})
    result = sync_index(repo_root=project, output_dir=output)
    assert result["action"] == "reindexed"
    assert result["changed"] == {"added": [], "modified": ["demo.py"], "deleted": []}


MUTATIONS = {
    "append": '__all__ = []\n__all__.append("helper")\n',
    "extend": '__all__ = []\n__all__.extend(["helper"])\n',
    "remove": '__all__ = ["helper", "dropped", "dropped"]\n__all__.remove("dropped")\n',
    "iadd": '__all__ = []\n__all__ += ["helper"]\n',
}


@pytest.mark.parametrize("mutation", MUTATIONS)
@pytest.mark.parametrize("initializer", ["__init__.py", "__init__.pyi"])
def test_literal_export_mutations_sync_equals_full(
    prepared, tmp_path, monkeypatch, mutation, initializer
):
    project, _, _, _ = prepared
    configure(project)
    write_files(
        project,
        {
            "pkg/" + initializer: MUTATIONS[mutation],
            "demo.py": "from pkg import *\ndef use(): return helper.make().run()\n",
            "unrelated.py": "def keep(): return 1\n",
        },
    )
    output = tmp_path / "sync"
    initial = active_build(project, output)
    before = native_calls(initial, "demo.py")
    counts = count_collection(monkeypatch)
    noop = ArcGraphReindexer(project, output).reindex_changed()
    assert noop["status"] == "unchanged"
    assert "reason" not in noop
    assert counts == []

    for index, name in enumerate(["A", "B"]):
        write_files(
            project,
            {
                "pkg/helper.py": f"class {name}:\n def run(self): return {index}\ndef make(): return {name}()\n"
            },
        )
        result = ArcGraphReindexer(project, output).reindex_changed()
        assert result["reason"] == "semantic_core_incremental"
        assert result["changed"] == {
            "added": ["pkg/helper.py"] if index == 0 else [],
            "modified": ["pkg/helper.py"] if index else [],
            "deleted": [],
        }
        current = GraphStoreReader.from_current(output).sqlite_path.parent
        detail = load(generation_directory(current) / "transport.json")
        assert detail["reuse"]["definitions"] >= 1
        assert "demo.py" in detail["reuse"]["affected_paths"]
        after = native_calls(current, "demo.py")
        assert after != before
        before = after
        assert_full(project, output, tmp_path / ("full-" + str(index)))


def exports(source):
    path = "pkg/__init__.py"
    index = dependency_index(
        [SimpleNamespace(path=path, module="pkg")],
        {path: ast.parse(source)},
        [SourceRoot(".")],
        sources={path: source},
    )
    return None if index is None else set(index["imports"][path]) - {"pkg"}


@pytest.mark.parametrize(
    "source,names",
    [
        ('__all__ = ["a"]; __all__.append("b")', {"a", "b"}),
        ('__all__ = ("a",); __all__.extend(["b", "c"])', {"a", "b", "c"}),
        ('__all__ = ["a", "b", "b"]; __all__.remove("b")', {"a"}),
        ('__all__ = ["a"]; __all__ += ["b"]', {"a", "b"}),
        ('__all__ = ["a"]; __all__ = ["b"]; __all__.remove("a")', {"b"}),
        (
            '__all__ = ["a", "b"]\nif flag:\n __all__.remove("a")\nelse:\n __all__.append("c")',
            {"a", "b", "c"},
        ),
    ],
)
def test_literal_export_mutations_follow_binder_rules(source, names):
    assert exports(source) == {"pkg.__init__", *("pkg." + name for name in names)}


@pytest.mark.parametrize(
    "tail",
    [
        '__all__ += ("b",)',
        '__all__.extend(("b",))',
        "__all__.append(unknown)",
        '__all__.extend(["b", unknown])',
        '__all__ += ["b", unknown]',
        "__all__.remove(unknown)",
        '__all__.remove("hel" "per")',
        '__all__.append("hel" "per")',
        '__all__.append(name="b")',
        '__all__.append(*["b"])',
        '__all__.remove("a", "b")',
        "__all__.pop()",
        "alias = __all__",
        '__all__ = ["a"] + ["b"]',
        '__all__ = ["hel" "per"]',
        "from other import __all__",
        'def mutate():\n __all__.remove("helper")',
        'class C:\n __all__.remove("helper")',
        'change = lambda: __all__.remove("helper")',
        'while flag:\n __all__.remove("helper")',
    ],
)
def test_unproven_export_mutations_disable_reuse(tail):
    assert exports('__all__ = ["helper"]\n' + tail) is None
