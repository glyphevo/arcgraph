"""pytest test case and fixture extraction."""

from __future__ import annotations

import ast

from arcgraph.adapters.common import (
    AdapterAnalysis,
    SemanticAdapter,
    call_name,
    iter_symbol_defs,
    literal_str,
    make_adapter_edge,
    symbol_id_for_def,
)
from arcgraph.adapters.pytest_fixtures import FixtureCatalog
from arcgraph.core.ids import pytest_case_id
from arcgraph.core.schemas import Edge, FileRecord, Node


class PytestAdapter(SemanticAdapter):
    name = "pytest"
    capabilities = ("entrypoint:test_case", "resource:pytest_fixture", "edge:injects")
    required_facts = ("function",)
    BUILTIN_FIXTURE_ARGS = {
        "anyio_backend",
        "benchmark",
        "cache",
        "capfd",
        "capfdbinary",
        "caplog",
        "capsys",
        "capsysbinary",
        "cls",
        "doctest_namespace",
        "event_loop",
        "monkeypatch",
        "pytestconfig",
        "record_property",
        "record_testsuite_property",
        "record_xml_attribute",
        "recwarn",
        "request",
        "self",
        "tmp_path",
        "tmp_path_factory",
        "tmpdir",
        "tmpdir_factory",
    }

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        analysis = AdapterAnalysis()
        catalog = FixtureCatalog(files, parsed_files, module_names, nodes)
        fixture_count = 0
        test_count = 0
        injection_count = 0
        unresolved_fixture_args = 0

        for fixture in catalog.fixtures:
            file_record, stmt = fixture.file, fixture.stmt
            qualname = fixture.node_id.removeprefix("fixture:")
            analysis.nodes.append(
                Node(
                    id=fixture.node_id,
                    kind="pytest_fixture",
                    name=fixture.name,
                    qualname=qualname,
                    path=file_record.path,
                    start_line=stmt.lineno,
                    end_line=stmt.end_lineno,
                    properties={
                        "handler": fixture.handler_id,
                        "autouse": fixture.autouse,
                    },
                )
            )
            analysis.edges.append(
                self._edge(
                    fixture.node_id,
                    fixture.handler_id,
                    "invokes",
                    file_record,
                    stmt,
                    f"pytest fixture {fixture.name}",
                )
            )
            fixture_count += 1

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            for stmt, qualified_name, id_factory in iter_symbol_defs(tree):
                if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not catalog.collection.is_test(
                    file_record, stmt, qualified_name.rpartition(".")[0]
                ):
                    continue
                qualname = f"{file_record.module}.{qualified_name}"
                source_id = symbol_id_for_def(file_record, qualified_name, id_factory)
                test_node_id = pytest_case_id(qualname)
                marks = self._marks(stmt)
                analysis.nodes.append(
                    Node(
                        id=test_node_id,
                        kind="test_case",
                        name=stmt.name,
                        qualname=qualname,
                        path=file_record.path,
                        start_line=stmt.lineno,
                        end_line=getattr(stmt, "end_lineno", stmt.lineno),
                        properties={
                            "handler": source_id,
                            "marks": marks,
                            "parametrized": "parametrize" in marks,
                        },
                    )
                )
                analysis.edges.append(
                    self._edge(
                        test_node_id,
                        source_id,
                        "invokes",
                        file_record,
                        stmt,
                        f"pytest test {stmt.name}",
                    )
                )
                test_count += 1
                owner = qualified_name.rpartition(".")[0]
                parametrized_args = catalog.direct_parameters(stmt, file_record, owner)
                requested_fixtures = [
                    *catalog.parameters(stmt, owner),
                    *self._usefixtures_args(stmt),
                    *catalog.autouse_names(file_record, owner),
                ]
                for arg in dict.fromkeys(requested_fixtures):
                    if arg in parametrized_args:
                        continue
                    fixture = catalog.resolve(arg, file_record, owner)
                    if fixture is None:
                        if (
                            arg not in self.BUILTIN_FIXTURE_ARGS
                            and arg not in parametrized_args
                        ):
                            unresolved_fixture_args += 1
                        continue
                    analysis.edges.append(
                        self._edge(
                            test_node_id,
                            fixture.node_id,
                            "injects",
                            file_record,
                            stmt,
                            f"pytest fixture argument {arg}",
                        )
                    )
                    injection_count += 1

        analysis.nodes.sort(key=lambda node: node.id)
        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        analysis.metrics = {
            "test_cases": test_count,
            "fixtures": fixture_count,
            "fixture_injections": injection_count,
            "unresolved_fixture_injections": unresolved_fixture_args,
            "discovered_registrations": test_count + fixture_count,
            "resolved_handlers": test_count + fixture_count,
            "unresolved_registrations": 0,
        }
        return analysis

    @staticmethod
    def _marks(stmt: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        marks: set[str] = set()
        for decorator in stmt.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            name = call_name(target)
            if name and name.startswith("pytest.mark."):
                marks.add(name.rsplit(".", 1)[-1])
        return sorted(marks)

    @staticmethod
    def _usefixtures_args(stmt: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        names: list[str] = []
        for decorator in stmt.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            name = call_name(decorator.func)
            if name not in {"pytest.mark.usefixtures", "mark.usefixtures"}:
                continue
            names.extend(value for arg in decorator.args if (value := literal_str(arg)))
        return names

    @staticmethod
    def _edge(
        source: str,
        target: str,
        kind: str,
        file_record: FileRecord,
        node: ast.AST,
        detail: str,
    ) -> Edge:
        return make_adapter_edge(
            source=source,
            target=target,
            kind=kind,
            confidence="confirmed",
            evidence_kind="pytest_adapter",
            file_record=file_record,
            node=node,
            detail=detail,
            semantic_role=f"{kind}:pytest",
        )
