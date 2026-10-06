"""Evidence-backed recovery of the M30 release-gate candidates."""

from __future__ import annotations

import argparse
import builtins
import inspect

import pytest

from arcgraph.analyzers.calls import CallAnalyzer
from arcgraph.analyzers.calls.constants import BUILTIN_CALLS
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
    "name", ["FileExistsError", "ModuleNotFoundError", "BrokenPipeError"]
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


def test_builtin_calls_cover_every_public_builtin():
    missing = sorted(
        name
        for name in dir(builtins)
        if not name.startswith("_")
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
