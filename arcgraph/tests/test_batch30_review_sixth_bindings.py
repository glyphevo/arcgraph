"""Class-scope bindings must not prove an overwritten external method."""

from arcgraph.semantic.journal import load

import ast
from textwrap import indent

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.semantic.contract import Snapshot
from arcgraph.semantic.external_targets import definition
from arcgraph.semantic.snapshot import sha
from arcgraph.tests.test_batch30_build import configure, generation_directory
from arcgraph.tests.test_semantic_isolation import prepared as prepared

BINDINGS = {
    "for_target": "for ping in (None,):\n    pass\n",
    "for_unpack": "for marker, *ping in ((1, None),):\n    pass\n",
    "for_body": "for marker in (1,):\n    ping = None\n",
    "for_else": "for marker in ():\n    pass\nelse:\n    ping = None\n",
    "with_target": "with contextlib.nullcontext(None) as ping:\n    pass\n",
    "with_unpack": "with contextlib.nullcontext((None, 1)) as (ping, marker):\n    pass\n",
    "with_body": "with contextlib.nullcontext(None):\n    ping = None\n",
    "import_alias": "import json as ping\n",
    "import_plain": "import ping\n",
    "import_dotted": "import ping.child\n",
    "from_alias": "from json import loads as ping\n",
    "from_plain": "from example_dep import ping\n",
    "try_body": "try:\n    ping = None\nexcept Exception:\n    pass\n",
    "try_except": "try:\n    pass\nexcept Exception:\n    ping = None\n",
    "except_target": "try:\n    pass\nexcept Exception as ping:\n    pass\n",
    "try_else": "try:\n    pass\nexcept Exception:\n    pass\nelse:\n    ping = None\n",
    "try_finally": "try:\n    pass\nfinally:\n    ping = None\n",
    "try_star": "try:\n    pass\nexcept* Exception:\n    ping = None\n",
    "while_body": "while FLAG:\n    ping = None\n",
    "while_else": "while FLAG:\n    pass\nelse:\n    ping = None\n",
    "match_body": "match marker:\n    case 1:\n        ping = None\n",
    "match_capture": "match marker:\n    case ping:\n        pass\n",
    "match_star": "match marker:\n    case [*ping]:\n        pass\n",
    "match_rest": "match marker:\n    case {**ping}:\n        pass\n",
    "walrus_expr": "(ping := None)\n",
    "walrus_value": "marker = (ping := None)\n",
    "walrus_if": "if (ping := None):\n    pass\n",
    "walrus_while": "while (ping := None):\n    pass\n",
    "walrus_with": "with contextlib.nullcontext(ping := None):\n    pass\n",
    "walrus_match_guard": "match marker:\n    case 1 if (ping := None):\n        pass\n",
    "walrus_default": "def other(value=(ping := None)):\n    return value\n",
    "walrus_class_base": "class Inner((ping := Base)):\n    pass\n",
    "nested_compounds": (
        "try:\n    while FLAG:\n        with contextlib.nullcontext(None):\n"
        "            for marker in ():\n                ping = None\n"
        "except Exception:\n    pass\n"
    ),
}


def class_text(binding, before=False, name="Client"):
    method = "def ping(self): return 1\n"
    body = binding + method if before else method + binding
    return (
        "from typing import TYPE_CHECKING\nimport sys\n"
        + f"class {name}:\n"
        + indent(body, "    ")
    )


def lookup(text):
    node = next(
        n
        for n in ast.walk(ast.parse(text))
        if isinstance(n, ast.FunctionDef) and n.name == "ping"
    )
    line = node.lineno - 1
    column = text.splitlines()[line].index("ping")
    return definition(
        dict(
            origin="dependency",
            relative="example.py",
            module="example",
            text=text,
            text_digest=sha(text.encode()),
        ),
        dict(
            name="ping",
            selectionRange=dict(
                start=dict(line=line, character=column),
                end=dict(line=line, character=column + 4),
            ),
        ),
        "utf-16",
        Snapshot(files=(), target_python="3.11", platform="darwin"),
    )


@pytest.mark.parametrize("binding", BINDINGS.values(), ids=BINDINGS)
def test_later_class_binding_invalidates_the_method(binding):
    assert lookup(class_text(binding)) is None


@pytest.mark.parametrize("binding", BINDINGS.values(), ids=BINDINGS)
def test_binding_before_the_method_keeps_the_method(binding):
    result = lookup(class_text(binding, before=True))
    assert result is not None
    assert result["qualname"] == "example.Client.ping"
    assert result["declaration_only"] is False


@pytest.mark.parametrize(
    "binding",
    [
        "def other(self):\n" + indent(BINDINGS["nested_compounds"], "    "),
        "class Inner:\n" + indent(BINDINGS["nested_compounds"], "    "),
        "marker = lambda: (ping := None)\n",
        "for other.ping in (None,):\n    pass\n",
        "with contextlib.nullcontext(None) as other.ping:\n    pass\n",
        "for marker in (1,):\n    other.ping = None\n",
        "with contextlib.nullcontext(None) as slots[0]:\n    pass\n",
        "if sys.version_info >= (3, 99):\n" + indent(BINDINGS["try_body"], "    "),
        "if TYPE_CHECKING:\n" + indent(BINDINGS["for_target"], "    "),
    ],
)
def test_other_scopes_attribute_targets_and_inactive_branches_keep_the_method(binding):
    assert lookup(class_text(binding)) is not None


def test_real_class_bindings_withdraw_execution_without_redirecting(prepared, tmp_path):
    project, environment, _, _ = prepared
    site = next(
        environment.glob("lib/python*/site-packages"), environment / "Lib/site-packages"
    )
    kinds = (
        "for_target",
        "with_target",
        "import_alias",
        "from_alias",
        "try_body",
        "walrus_expr",
    )
    classes = {
        f"{'Before' if before else 'After'}{i}": (BINDINGS[kind], before)
        for i, kind in enumerate(kinds)
        for before in (False, True)
    }
    (site / "example.py").write_text(
        "import contextlib\n"
        + "\n".join(
            class_text(binding, before, name)
            for name, (binding, before) in classes.items()
        ),
        encoding="utf-8",
    )
    (project / "demo.py").write_text(
        "from example import "
        + ", ".join(classes)
        + "\n"
        + "\n".join(
            f"def use_{name}(client: {name}): return client.ping()" for name in classes
        ),
        encoding="utf-8",
    )
    configure(project)
    output = tmp_path / "out"
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    generation = generation_directory(directory)
    primary = load(generation / "pyright.json")
    projection = load(generation / "projection.json")
    edges = list(GraphStoreReader.from_current(output).read_edges())
    for name, (_, before) in classes.items():
        target = f"extsym:example.{name}.ping"
        source = f"fn:demo.use_{name}"
        matches = [e for e in edges if e.source == source and e.target == target]
        assert matches, name
        candidates = [
            c
            for a in primary["answers"]
            for c in a["candidates"]
            if c["target"] == target
        ]
        projected = [e for e in projection["edges"] if e["key"][1] == target]
        if before:
            assert any(c["semantics"] == "implementation" for c in candidates), name
            assert any(e["relation"] == "execution" for e in projected), name
            assert any(
                v.kind == "type_inference" for e in matches for v in e.evidence
            ), name
        else:
            assert not candidates, name
            assert not projected, name
            assert all(
                {v.kind for v in e.evidence} == {"ast_attribute_call"} for e in matches
            ), name
            assert not any(m.get("target") == target for m in primary["mapping"]), name
            assert not any(
                e.source == source
                and any(v.kind == "type_inference" for v in e.evidence)
                for e in edges
            ), name
