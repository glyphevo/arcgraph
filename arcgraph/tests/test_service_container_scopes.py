from __future__ import annotations

import ast

import pytest

from arcgraph.adapters.service_container_adapter import ServiceContainerAdapter
from arcgraph.analyzers.symbols import SymbolAnalyzer
from arcgraph.core.schemas import FileRecord


def _edges(body: str):
    source = (
        "class MemoryService:\n    def status(self):\n        return 'ok'\n\n" + body
    )
    tree = ast.parse(source)
    record = FileRecord(
        path="src/pkg/scope.py",
        abs_path="/repo/src/pkg/scope.py",
        source_root="src",
        module="pkg.scope",
        file_hash="fixture",
        line_count=len(source.splitlines()),
    )
    nodes = SymbolAnalyzer().analyze(record, tree).nodes
    return (
        ServiceContainerAdapter()
        .analyze([record], {record.path: tree}, nodes, {record.module})
        .edges
    )


@pytest.mark.parametrize(
    "nested",
    [
        "def inner():\n        client = MemoryService()\n        return client.status()",
        "async def inner():\n        return MemoryService()",
        "class Inner:\n        def run(self):\n            return client.status()",
        "inner = lambda: client.status()",
    ],
)
def test_service_calls_and_provider_facts_do_not_escape_nested_scopes(
    nested: str,
) -> None:
    edges = _edges(
        f"def outer():\n    client = MemoryService()\n    {nested}\n    return inner\n"
    )
    assert not any(edge.kind in {"calls", "provides"} for edge in edges)


def test_nested_assignment_does_not_type_an_unannotated_outer_parameter() -> None:
    edges = _edges(
        "def outer(client):\n"
        "    def inner():\n        client = MemoryService()\n"
        "    return client.status()\n"
    )
    assert edges == []


def test_service_body_calls_remain_and_defaults_are_not_calls_of_the_callee() -> None:
    edges = _edges(
        "client = MemoryService()\n\n"
        "def direct(client: MemoryService):\n    return client.status()\n\n"
        "def defined(client: MemoryService, value=client.status()):\n    pass\n\n"
        "def outer(client: MemoryService):\n"
        "    def inner(value=client.status()):\n        pass\n"
        "    return inner\n"
    )
    assert {(e.source, e.target) for e in edges if e.kind == "calls"} == {
        ("fn:pkg.scope.direct", "method:pkg.scope.MemoryService.status"),
        ("fn:pkg.scope.outer", "method:pkg.scope.MemoryService.status"),
    }


def test_nested_class_body_executes_in_function_but_method_body_does_not():
    edges = _edges(
        "def outer():\n    client = MemoryService()\n    class Local:\n        state = client.status()\n        def deferred(self):\n            return client.status()\n"
    )
    calls = [e for e in edges if e.kind == "calls"]
    assert len(calls) == 1
    assert calls[0].source == "fn:pkg.scope.outer"
    assert calls[0].target == "method:pkg.scope.MemoryService.status"
    assert calls[0].evidence[0].start_line == 8


def test_class_assignment_does_not_escape_to_outer_function():
    edges = _edges(
        "def outer(client):\n    class Local:\n        client = MemoryService()\n        state = client.status()\n    return client.status()\n"
    )
    calls = [e for e in edges if e.kind == "calls"]
    assert len(calls) == 1
    assert calls[0].evidence[0].start_line == 8


def test_unknown_class_binding_masks_outer_service():
    edges = _edges(
        "def outer():\n    client = MemoryService()\n    class Local:\n        client = unknown()\n        state = client.status()\n"
    )
    assert not any(e.kind == "calls" for e in edges)


@pytest.mark.parametrize(
    "body",
    [
        "        client = MemoryService()\n        client = unknown()\n        result = client.status()\n",
        "        result = client.status()\n        client = MemoryService()\n",
        "        client = MemoryService()\n        result = [client.status() for _ in values]\n",
    ],
)
def test_class_local_type_cannot_escape_its_valid_binding(body):
    edges = _edges("def outer():\n    class Local:\n" + body)
    assert not any(e.kind == "calls" for e in edges)


def test_class_header_write_invalidates_earlier_local_type():
    edges = _edges(
        "def outer():\n    class Local:\n        client = MemoryService()\n        def method(arg=(client := unknown())): pass\n        result = client.status()\n"
    )
    assert not any(e.kind == "calls" for e in edges)
