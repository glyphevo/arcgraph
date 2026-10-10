"""Persisted callable contracts must retain their defining binding identity."""

import ast

from arcgraph.analyzers.callable_types import CallableContracts
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer


def contracts(tmp_path, provider):
    (tmp_path / "provider.py").write_text(provider, encoding="utf-8")
    (tmp_path / "consumer.py").write_text(
        "from provider import Factory, Alias\ndef use(): pass\n", encoding="utf-8"
    )
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    resolver = CallableContracts(graph.nodes)
    source = next(n for n in graph.nodes if n.id == "fn:consumer.use")
    return resolver, source


def test_imported_alias_is_rechecked_in_its_definition_module(tmp_path):
    resolver, source = contracts(
        tmp_path,
        "from typing import Callable\nclass Box: pass\nclass Other: pass\n"
        "Factory = Callable[..., Box]\nAlias = Box\n"
        "if flag: Factory = Callable[..., Other]\n",
    )
    # The importer has one stable import; the provider has a conditional write.
    assert resolver.name(ast.Name(id="Factory"), source) == "provider.Factory"
    assert resolver.result("Factory", source) is None


def test_callable_result_alias_is_not_a_concrete_class_binding(tmp_path):
    resolver, source = contracts(
        tmp_path,
        "from typing import Callable\nclass Box: pass\n"
        "Alias = Callable[..., Box]\nFactory = Callable[..., Alias]\n",
    )
    assert resolver.name(ast.Name(id="Alias"), source) == "provider.Alias"
    assert resolver.concrete(ast.Name(id="Alias"), source) is None
    assert resolver.result("Factory", source) is None
