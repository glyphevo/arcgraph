"""Review C: external runtime definitions and conservative placeholder mapping."""

from arcgraph.semantic.journal import load

import ast

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.semantic.external_targets import definition
from arcgraph.semantic.snapshot import sha
from arcgraph.tests.test_batch30_build import configure, generation_directory
from arcgraph.tests.test_semantic_isolation import prepared as prepared

GROUPS = {
    "overload": (
        "    @overload\n    def ping(self, value: int) -> int: ...\n"
        "    @overload\n    def ping(self, value: str) -> str: ...\n"
        "    def ping(self, value):\n        return value\n"
    ),
    "split": (
        "    if TYPE_CHECKING:\n        def ping(self): ...\n"
        "    else:\n        def ping(self):\n            return 2\n"
    ),
    "typing_then_runtime": (
        "    if TYPE_CHECKING:\n        def ping(self): ...\n"
        "    def ping(self):\n        return 3\n"
    ),
}


def lookup(text, line):
    name = "ping"
    column = text.splitlines()[line].index(name)
    row = {
        "origin": "dependency",
        "relative": "example.py",
        "module": "example",
        "text": text,
        "text_digest": sha(text.encode()),
    }
    item = {
        "name": name,
        "selectionRange": {
            "start": {"line": line, "character": column},
            "end": {"line": line, "character": column + len(name)},
        },
    }
    return definition(row, item, "utf-16")


@pytest.mark.parametrize("group", GROUPS)
def test_external_declaration_selections_bind_unique_runtime_body(group):
    text = "from typing import TYPE_CHECKING, overload\nclass Client:\n" + GROUPS[group]
    functions = [n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef)]
    implementation = next(n for n in functions if isinstance(n.body[0], ast.Return))
    for selected in functions:
        result = lookup(text, selected.lineno - 1)
        assert result is not None, (group, selected.lineno)
        assert result["id"] == "extsym:example.Client.ping"
        assert result["declaration_only"] is False
        assert result["span"].start[0] == implementation.lineno - 1
        assert result["name_span"].start[0] == implementation.lineno - 1


@pytest.mark.parametrize("body", ["...", "pass", "raise NotImplementedError"])
def test_external_placeholder_needs_native_or_concrete_override(body):
    text = f"class Client:\n    def ping(self): {body}\n"
    assert lookup(text, 1) is None
    text += "class Concrete(Client):\n    def ping(self): return 1\n"
    parent = lookup(text, 1)
    assert parent is not None and parent["declaration_only"] is True
    child = lookup(text, 3)
    assert child is not None and child["declaration_only"] is False


def test_external_runtime_groups_and_placeholders_keep_mapping_accounts(
    prepared, tmp_path
):
    project, environment, _, _ = prepared
    site = next(
        environment.glob("lib/python*/site-packages"), environment / "Lib/site-packages"
    )
    text = "from typing import TYPE_CHECKING, overload\n"
    source = "from example import Overload, Split, Both, Ellipsis, Pass, Ambiguous\n"
    for name, group in zip(("Overload", "Split", "Both"), GROUPS.values()):
        text += f"class {name}:\n" + group
        argument = "1" if name == "Overload" else ""
        source += (
            f"def use_{name.lower()}(client: {name}): return client.ping({argument})\n"
        )
    for name, body in (("Ellipsis", "..."), ("Pass", "pass")):
        text += f"class {name}:\n    def ping(self): {body}\n"
        source += f"def use_{name.lower()}(client: {name}): return client.ping()\n"
    text += (
        "class Ambiguous:\n    if FLAG:\n        def ping(self): return 1\n"
        "    else:\n        def ping(self): return 2\n"
    )
    source += "def use_ambiguous(client: Ambiguous): return client.ping()\n"
    (site / "example.py").write_text(text, encoding="utf-8")
    (project / "demo.py").write_text(source, encoding="utf-8")
    configure(project)
    output = tmp_path / "out"
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    primary = load(generation_directory(directory) / "pyright.json")
    assert primary["raw_count"] == len(primary["mapping"])
    edges = list(GraphStoreReader.from_current(output).read_edges())
    for name in ("Overload", "Split", "Both"):
        target = f"extsym:example.{name}.ping"
        assert any(
            m["stage"] == "mapped" and m["target"] == target for m in primary["mapping"]
        ), target
        assert any(
            e.target == target and any(x.kind == "type_inference" for x in e.evidence)
            for e in edges
        ), target
        assert any(
            c["target"] == target and c["semantics"] == "implementation"
            for a in primary["answers"]
            for c in a["candidates"]
        ), target
    for name in ("Ellipsis", "Pass", "Ambiguous"):
        target = f"extsym:example.{name}.ping"
        assert not any(m.get("target") == target for m in primary["mapping"]), target
        assert not any(
            e.target == target and any(x.kind == "type_inference" for x in e.evidence)
            for e in edges
        ), target
    assert (
        sum(m["stage"] == "symbol" and m["target"] is None for m in primary["mapping"])
        >= 3
    )
    assert sum(a["outcome"] == "unknown" for a in primary["answers"]) == 3


def test_testcase_overlay_does_not_hide_function_execution_owner(prepared, tmp_path):
    project, _, _, _ = prepared
    (project / "demo.py").unlink()
    path = project / "test_demo.py"
    path.write_text(
        "def test_lookup():\n    resolutions = {'no_guess': set()}\n"
        "    assert resolutions.get('no_guess', set()) == set()\n",
        encoding="utf-8",
    )
    configure(project)
    output = tmp_path / "out"
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    reader = GraphStoreReader.from_current(output)
    owners = [n for n in reader.read_nodes() if n.qualname == "test_demo.test_lookup"]
    assert {n.kind for n in owners} == {"function", "test_case"}
    primary = load(generation_directory(directory) / "pyright.json")
    assert any(
        c["target"] == "extsym:builtins.dict.get" and c["semantics"] == "implementation"
        for a in primary["answers"]
        for c in a["candidates"]
    )
    bridge = load(generation_directory(directory) / "bridge.json")
    assert bridge["graph_losses"] == []
    assert any(
        e.source == "fn:test_demo.test_lookup"
        and e.target == "extsym:builtins.dict.get"
        and any(x.kind == "type_inference" for x in e.evidence)
        for e in reader.read_edges()
    )
