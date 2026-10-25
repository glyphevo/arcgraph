"""Prepared type passes share structure, preserving file-sensitive resolution."""

import ast
from dataclasses import asdict

import pytest

from arcgraph.analyzers.bindings import BindingAnalysis, BindingAnalyzer
from arcgraph.analyzers.symbols import SymbolAnalyzer
from arcgraph.analyzers.types import TypeRefAnalysis, TypeRefAnalyzer, _TypeContext
from arcgraph.core.schemas import FileRecord, Node
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def project():
    sources = {
        "provider": (
            "from typing import Optional\n"
            "from pkg.model import Box as Alias\n"
            "def make() -> Optional[Alias]: return None\n"
        ),
        "model": "class Box: pass\n",
        "consumer": "value = 1\n",
    }
    files = {
        name: FileRecord(
            path=f"pkg/{name}.py",
            abs_path=f"/repo/pkg/{name}.py",
            source_root=".",
            module=f"pkg.{name}",
            file_hash="test",
            line_count=len(source.splitlines()),
        )
        for name, source in sources.items()
    }
    trees = {name: ast.parse(source) for name, source in sources.items()}
    nodes = []
    modules = {file.module for file in files.values()}
    for name, file in files.items():
        nodes.append(
            Node(
                id=f"mod:{file.module}",
                kind="module",
                name=name,
                qualname=file.module,
                path=file.path,
            )
        )
        nodes.extend(SymbolAnalyzer().analyze(file, trees[name]).nodes)
    for name, file in files.items():
        BindingAnalyzer().analyze(file, trees[name], modules).attach_to_nodes(nodes)
    return files, trees, nodes, modules


def capture_contexts(monkeypatch):
    contexts = []
    original = _TypeContext.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        contexts.append(self)

    monkeypatch.setattr(_TypeContext, "__init__", capture)
    return contexts


def test_prepared_context_structure_shared_and_repreparation_invalidates(monkeypatch):
    files, trees, nodes, modules = project()
    contexts = capture_contexts(monkeypatch)
    analyzer = TypeRefAnalyzer()
    analyzer.prepare_return_analysis(nodes)
    for name in ("provider", "consumer"):
        analyzer.analyze(files[name], trees[name], nodes, modules)
    first, second = contexts
    for field in ("_nodes_by_path", "classes_by_id", "export_nodes", "classes_by_name"):
        assert getattr(first, field) is getattr(second, field), field
    assert first.methods_by_class is second.methods_by_class
    assert first.lexical is not second.lexical
    assert first.aliases is not second.aliases
    assert first.class_instance_attrs is not second.class_instance_attrs
    analyzer.prepare_return_analysis(nodes)
    analyzer.analyze(files["provider"], trees["provider"], nodes, modules)
    assert contexts[-1]._nodes_by_path is not first._nodes_by_path


def test_standalone_and_different_node_list_do_not_reuse_prepared_structure(
    monkeypatch,
):
    files, trees, nodes, modules = project()
    contexts = capture_contexts(monkeypatch)
    analyzer = TypeRefAnalyzer()
    for _ in range(2):
        analyzer.analyze(files["provider"], trees["provider"], nodes, modules)
    assert contexts[0]._nodes_by_path is not contexts[1]._nodes_by_path
    analyzer.prepare_return_analysis(nodes)
    analyzer.analyze(files["provider"], trees["provider"], nodes, modules)
    analyzer.analyze(files["provider"], trees["provider"], list(nodes), modules)
    assert contexts[-1]._nodes_by_path is not contexts[-2]._nodes_by_path


@pytest.mark.parametrize("order", [("provider", "consumer"), ("consumer", "provider")])
def test_return_views_preserve_provider_alias_and_foreign_resolution(
    monkeypatch, order
):
    files, trees, nodes, modules = project()
    contexts = capture_contexts(monkeypatch)
    analyzer = TypeRefAnalyzer()
    analyzer.prepare_return_analysis(nodes)
    for name in order:
        analyzer.analyze(files[name], trees[name], nodes, modules)
    for name, context in zip(order, contexts):
        result = asdict(context.return_types_by_function_qualname["pkg.provider.make"])
        assert result == {
            "expression": "Optional[Alias]",
            "type_id": "class:pkg.model.Box" if name == "provider" else None,
            "symbol_id": "class:pkg.model.Box" if name == "provider" else None,
            "origin": "Optional",
            "type_args": (
                {
                    "type_expression": "Alias",
                    "resolution_status": (
                        "resolved" if name == "provider" else "unresolved"
                    ),
                    **(
                        {
                            "type_id": "class:pkg.model.Box",
                            "symbol_id": "class:pkg.model.Box",
                        }
                        if name == "provider"
                        else {}
                    ),
                },
            ),
            "status": "resolved" if name == "provider" else "unresolved",
            "callable_result": None,
        }


def test_pytest_provider_and_consumer_contexts_share_the_prepared_index(
    tmp_path, monkeypatch
):
    contexts = capture_contexts(monkeypatch)
    _targets(
        tmp_path,
        {
            "conftest": "import pytest\n@pytest.fixture\ndef item() -> int: return 1\n",
            "test_use": "def test_use(item, tmp_path): assert item\n",
        },
        v2=True,
    )
    assert len(contexts) >= 4
    assert len({id(context._nodes_by_path) for context in contexts}) == 1


@pytest.mark.parametrize("kind", ["bindings", "type_refs"])
def test_attachment_uses_shared_node_objects_without_iterating_nodes(kind):
    class NoIteration(list):
        def __iter__(self):
            raise AssertionError("attachment rebuilt the shared mapping")

    node = Node(id="fn:sample.run", kind="function", name="run")
    nodes = NoIteration([node])
    record = {"name": "value", "line": 1}
    analysis = (
        BindingAnalysis(bindings_by_scope={node.id: [record]})
        if kind == "bindings"
        else TypeRefAnalysis(type_refs_by_scope={node.id: [record]})
    )
    analysis.attach_to_nodes(nodes, by_id={node.id: node})
    assert node.properties[kind] == [record]


def test_shared_return_slots_preserve_same_name_order_and_last_eligible_override(
    monkeypatch,
):
    files, trees, nodes, modules = project()
    nodes.extend(
        [
            Node(
                id=f"fn:duplicate{i}",
                kind="function",
                name="make",
                qualname="pkg.duplicate.make",
                path=f"duplicate{i}.py",
                properties={"returns": returns},
            )
            for i, returns in enumerate(("int", "str", "Unknown"))
        ]
    )
    contexts = capture_contexts(monkeypatch)
    analyzer = TypeRefAnalyzer()
    analyzer.prepare_return_analysis(nodes)
    analyzer.analyze(files["consumer"], trees["consumer"], nodes, modules)
    shared = contexts[-1]
    # The existing standalone builder is an independent oracle for ordering.
    independent = _TypeContext(files["consumer"], trees["consumer"], nodes, modules)
    for name in (
        "return_types_by_function_name",
        "return_types_by_function_qualname",
        "return_types_by_method",
        "property_return_types_by_method",
    ):
        assert dict(getattr(shared, name)) == dict(getattr(independent, name))
    assert [item.type_id for item in shared.return_types_by_function_name["make"]] == [
        None,
        "builtin:int",
        "builtin:str",
    ]
    assert (
        shared.return_types_by_function_qualname["pkg.duplicate.make"].type_id
        == "builtin:str"
    )
