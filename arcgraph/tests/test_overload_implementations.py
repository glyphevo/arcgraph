"""Overload declarations describe a callable; the implementation owns its node."""

import ast

import pytest

from arcgraph.analyzers.calls import CallAnalyzer
from arcgraph.tests.test_calls import _analyzed_nodes


@pytest.mark.parametrize(
    "prefix,decorator",
    [
        ("from typing import overload", "overload"),
        ("import typing", "typing.overload"),
        ("from typing import overload as ovl", "ovl"),
        ("import typing as t", "t.overload"),
    ],
)
@pytest.mark.parametrize("async_", [False, True])
def test_overload_call_targets_implementation(prefix, decorator, async_):
    keyword = "async " if async_ else ""
    source = (
        f"{prefix}\n@{decorator}\n{keyword}def ov(x: int) -> int: ...\n"
        f"@{decorator}\n{keyword}def ov(x: str) -> str: ...\n"
        f"{keyword}def ov(x): return x\ndef use(): return ov(1)\n"
    )
    nodes = _analyzed_nodes(source)
    calls = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    assert any(
        e.source == "fn:pkg.calls.use"
        and e.target == "fn:pkg.calls.ov"
        and e.resolution.status == "resolved"
        for e in calls
    )
    implementations = [n for n in nodes if n.id == "fn:pkg.calls.ov"]
    assert len(implementations) == 1
    implementation = implementations[0]
    assert implementation.start_line == 6
    assert implementation.properties["decorators"] == []
    assert implementation.properties["returns"] is None
    assert (
        len([b for b in implementation.properties["bindings"] if b["name"] == "x"]) == 1
    )


@pytest.mark.parametrize("scope", ["method", "nested"])
def test_overload_families_in_lexical_scopes(scope):
    family = (
        "@overload\ndef ov(x: int) -> int: ...\n"
        "@overload\ndef ov(x: str) -> str: ...\n"
        "def ov(x): return x\n"
    )
    body = "\n".join("    " + line for line in family.splitlines())
    if scope == "method":
        source = (
            "from typing import overload\nclass K:\n"
            + body.replace("def ov(x", "def ov(self, x")
            + "\ndef use(): return K().ov(1)\n"
        )
        target = "method:pkg.calls.K.ov"
    else:
        source = (
            "from typing import overload\ndef use():\n" + body + "\n    return ov(1)\n"
        )
        target = "fn:pkg.calls.use.ov"
    nodes = _analyzed_nodes(source)
    assert any(
        e.source == "fn:pkg.calls.use" and e.target == target
        for e in CallAnalyzer(enable_v2=True).analyze(nodes).edges
    )
    assert len([n for n in nodes if n.id == target]) == 1


@pytest.mark.parametrize(
    "prefix,decorator",
    [
        ("def overload(f): return f", "overload"),
        ("from typing import overload\noverload = lambda f: f", "overload"),
        ("import typing\ntyping = unknown", "typing.overload"),
        ("from typing import overload\nfrom elsewhere import *", "overload"),
    ],
)
def test_unknown_or_shadowed_overload_is_not_collapsed(prefix, decorator):
    source = (
        f"{prefix}\n@{decorator}\ndef ov(x: int): ...\n"
        "def ov(x): return x\ndef use(): return ov(1)\n"
    )
    assert len([n for n in _analyzed_nodes(source) if n.id == "fn:pkg.calls.ov"]) == 2


def test_without_implementation_overloads_are_not_an_execution_definition():
    source = (
        "from typing import overload\n@overload\ndef ov(x: int) -> int: ...\n"
        "@overload\ndef ov(x: str) -> str: ...\ndef use(): return ov(1)\n"
    )
    nodes = _analyzed_nodes(source)
    assert not any(
        e.source == "fn:pkg.calls.use" and e.target == "fn:pkg.calls.ov"
        for e in CallAnalyzer(enable_v2=True).analyze(nodes).edges
    )


def test_real_overload_implementation_matches_python_runtime():
    namespace = {}
    source = (
        "from typing import overload\n@overload\ndef ov(x: int) -> int: ...\n"
        "@overload\ndef ov(x: str) -> str: ...\ndef ov(x): return x\n"
    )
    exec(compile(ast.parse(source), "probe", "exec"), namespace)
    assert namespace["ov"](1) == 1
    assert namespace["ov"]("text") == "text"
