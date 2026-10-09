"""Evidence-backed recovery of the M30 release-gate candidates."""

from __future__ import annotations

import argparse
import builtins
import inspect

import pytest

from arcgraph.analyzers.calls import CallAnalyzer
from arcgraph.analyzers.calls.constants import BUILTIN_CALLS, BUILTIN_METHODS_BY_TYPE
from arcgraph.interfaces.cli_visual import add_visual_parser
from arcgraph.tests.test_calls import _analyzed_nodes

PREFIX = "fn:pkg.calls."


@pytest.mark.parametrize(
    "block",
    [
        "if flag:\n",
        "for item in items:\n",
        "while flag:\n",
        "try:\n",
        "try:\n        pass\n    except Exception:\n",
        "match flag:\n        case 1:\n",
    ],
)
def test_conditional_local_definition_resolves_only_after_definition_in_same_block(
    block,
):
    indent = "            " if block.startswith("match") else "        "
    body = (
        "def outer(flag, items):\n    "
        + block
        + indent
        + "helper()  # before definition\n"
        + indent
        + "def helper():\n"
        + indent
        + "    return 1\n"
        + indent
        + "helper()  # dominated call\n"
    )
    if block == "try:\n":
        body += "    except Exception:\n        helper()  # other branch\n"
    body += "    helper()  # outside block\n"
    nodes = _analyzed_nodes(body)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    calls = [
        e
        for e in edges
        if e.source == PREFIX + "outer" and e.target == PREFIX + "outer.helper"
    ]
    assert len(calls) == 1
    line = next(
        i for i, text in enumerate(body.splitlines(), 1) if "# dominated call" in text
    )
    assert calls[0].evidence[0].start_line == line


def test_reassigned_conditional_helper_does_not_gain_an_edge():
    nodes = _analyzed_nodes(
        "def outer(flag, other):\n    if flag:\n"
        "        def helper():\n            return 1\n"
        "        helper = other\n        helper()\n"
    )
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    assert not any(e.target == PREFIX + "outer.helper" for e in edges)


def test_captured_local_class_constructor_keeps_definition_identity_only():
    nodes = _analyzed_nodes(
        "class Result:\n    def report(self):\n        return 0\n"
        "def outer():\n"
        "    class Result:\n        pass\n"
        "    def inner():\n        Result.report()\n        return Result()\n"
        "    Result()\n    return inner\n"
    )
    result = CallAnalyzer(enable_v2=True).analyze(nodes)
    constructors = [
        e
        for e in result.edges
        if e.kind == "constructs" and e.target.startswith("local:")
    ]
    assert {e.source for e in constructors} == {
        PREFIX + "outer",
        PREFIX + "outer.inner",
    }
    assert len({e.target for e in constructors}) == 1
    target = next(n for n in result.nodes if n.id == constructors[0].target)
    assert target.qualname == "pkg.calls.outer.<locals>.Result"
    assert target.properties["scope"] == PREFIX + "outer"
    assert not any(e.target == "method:pkg.calls.Result.report" for e in result.edges)


@pytest.mark.parametrize(
    "name",
    ["FileExistsError", "ModuleNotFoundError", "BrokenPipeError", "WindowsError"],
)
def test_builtin_exceptions_resolve_and_local_shadow_stays_unknown(name):
    nodes = _analyzed_nodes(
        f'def outer():\n    def inner():\n        raise {name}("x")\n    return inner\n'
        f'def other({name}):\n    def inner():\n        raise {name}("x")\n    return inner\n'
    )
    result = CallAnalyzer(enable_v2=True).analyze(nodes)
    resolved = [e for e in result.edges if e.target == "extsym:builtins." + name]
    assert len(resolved) == 1
    assert resolved[0].source == PREFIX + "outer.inner"
    assert resolved[0].resolution.strategy == "builtin_function"


@pytest.mark.parametrize("name", ["compile", "oct", "format", "divmod", "vars", "exit"])
def test_builtin_functions_resolve_and_local_shadow_stays_unknown(name):
    # Each was missing from a hand-kept subset of the builtins and so stayed a
    # release-blocking unresolved call.
    nodes = _analyzed_nodes(
        f"def outer():\n    def inner():\n        {name}(1)\n    return inner\n"
        f"def other({name}):\n    def inner():\n        {name}(1)\n    return inner\n"
    )
    result = CallAnalyzer(enable_v2=True).analyze(nodes)
    resolved = [e for e in result.edges if e.target == "extsym:builtins." + name]
    assert len(resolved) == 1
    assert resolved[0].source == PREFIX + "outer.inner"
    assert resolved[0].resolution.strategy == "builtin_function"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (
            "def format(value):\n    return value\n"
            "def outer():\n    return format(1)\n",
            PREFIX + "format",
        ),
        (
            "def outer():\n    def format(value):\n        return value\n"
            "    return format(1)\n",
            PREFIX + "outer.format",
        ),
        (
            "from os.path import join as format\n"
            "def outer():\n    return format(1)\n",
            "extsym:os.path.join",
        ),
        (
            "def outer():\n    from os.path import join as format\n"
            "    return format(1)\n",
            "extsym:os.path.join",
        ),
    ],
    ids=["module_def", "local_def", "module_import", "local_import"],
)
def test_definition_or_import_shadows_a_builtin(source, target):
    result = CallAnalyzer(enable_v2=True).analyze(_analyzed_nodes(source))
    calls = [e for e in result.edges if e.source == PREFIX + "outer"]
    assert [e.target for e in calls] == [target]


def test_builtin_calls_cover_every_public_builtin():
    # It cannot see a name the list has and the interpreter lacks, nor an
    # interpreter version it does not run on.
    missing = sorted(
        name
        for name in dir(builtins)
        if (not name.startswith("_") or name == "__import__")
        and callable(getattr(builtins, name))
        and name not in BUILTIN_CALLS
    )
    assert missing == []


def test_visual_parser_runtime_and_explicit_argparse_factory_contract():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_visual_parser(subparsers)
    assert parser.parse_args(["visual", "force"]).visual_command == "force"
    nodes = _analyzed_nodes(
        "import argparse\nfrom typing import Any\n"
        # The application helper retains Any per M11. Exercise receiver type
        # propagation with an explicitly typed analysis fixture, not a production
        # annotation added to raise the self-index metric.
        + inspect.getsource(add_visual_parser).replace(
            "subparsers: Any", "subparsers: argparse._SubParsersAction"
        )
    )
    result = CallAnalyzer(enable_v2=True).analyze(nodes)
    assert any(
        e.target == "extsym:argparse._SubParsersAction.add_parser" for e in result.edges
    )
    assert any(
        e.target == "extsym:argparse.ArgumentParser.add_argument" for e in result.edges
    )
    # Every call through a parser factory result carries a real receiver type;
    # this check does not accept the legacy receiver-name heuristic.
    parser_calls = [
        e
        for e in result.edges
        if e.source == PREFIX + "add_visual_parser"
        and e.properties.get("callsite", {})
        .get("receiver_expression", "")
        .startswith("visual")
    ]
    assert len(parser_calls) >= 60
    assert all(e.resolution.strategy == "external_receiver_type" for e in parser_calls)


def test_builtin_methods_cover_every_public_method():
    # A method missing from the list is never linked on a typed receiver.
    for name, methods in BUILTIN_METHODS_BY_TYPE.items():
        builtin_type = getattr(builtins, name)
        missing = sorted(
            method
            for method in dir(builtin_type)
            if not method.startswith("_")
            and callable(getattr(builtin_type, method))
            and method not in methods
        )
        assert missing == [], name


def test_bare_builtin_wins_over_a_unique_name_elsewhere(tmp_path):
    from pathlib import Path

    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.pipeline.indexer import ArcGraphIndexer

    package = Path(tmp_path) / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "helpers.py").write_text(
        "def format(value):\n    return value\n", encoding="utf-8"
    )
    (package / "plain.py").write_text(
        "def use():\n    return format(1)\n", encoding="utf-8"
    )
    (package / "starred.py").write_text(
        "from pkg.helpers import *\n\n\ndef use():\n    return format(1)\n",
        encoding="utf-8",
    )
    output = Path(tmp_path) / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
    ).build()
    targets = {
        edge.source: edge.target
        for edge in GraphStoreReader.from_current(output).read_edges()
        if edge.source.endswith(".use")
        and edge.resolution.status == "resolved"
        and (edge.properties.get("callsite") or edge.properties.get("callsites"))
    }
    # plain.py neither defines nor imports format, so the call is the builtin,
    # not the one function of that name elsewhere in the project; a star
    # import can bring the project's own.
    assert targets["fn:pkg.plain.use"] == "extsym:builtins.format"
    assert targets["fn:pkg.starred.use"] == "fn:pkg.helpers.format"


@pytest.mark.parametrize("enable_v2", [True, False], ids=["v2", "legacy"])
def test_legacy_mode_alone_matches_a_method_by_its_name(enable_v2):
    nodes = _analyzed_nodes(
        "class Accumulator:\n    def add(self, value):\n        return value\n"
        "def outer(item):\n    item['targets'].add(1)\n"
    )
    result = CallAnalyzer(enable_v2=enable_v2).analyze(nodes)
    linked = {e.target for e in result.edges if e.source == PREFIX + "outer"}
    # Legacy mode, kept for indexes built in it, still matches the one add of
    # the project; with receiver resolution, its name alone links nothing.
    assert ("method:pkg.calls.Accumulator.add" in linked) is not enable_v2


def test_windows_error_builtin_alias_is_covered_on_every_host(monkeypatch):
    # Model the actual Windows builtin on POSIX too; shadowing is tested above.
    monkeypatch.setattr(builtins, "WindowsError", OSError, raising=False)
    assert builtins.WindowsError is OSError
    test_builtin_calls_cover_every_public_builtin()
