"""Import analyzer for Python AST files."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from arcgraph.core.ids import external_package_id, module_id
from arcgraph.core.schemas import Edge, Evidence, FileRecord, evidence_key


@dataclass
class ImportAnalysis:
    edges: list[Edge] = field(default_factory=list)
    external_packages: set[str] = field(default_factory=set)


class ImportAnalyzer:
    def analyze(
        self, file_record: FileRecord, tree: ast.AST, module_names: set[str]
    ) -> ImportAnalysis:
        analysis = ImportAnalysis()
        edge_map: dict[tuple[str, str, str], Edge] = {}

        for node, scope in self._import_nodes(tree):
            candidates = self._candidates(file_record, node, scope)
            for candidate, evidence in candidates:
                resolved = self._resolve_internal(candidate, module_names)
                if resolved:
                    target = module_id(resolved)
                else:
                    package = candidate.split(".", 1)[0]
                    if not package:
                        continue
                    analysis.external_packages.add(package)
                    target = external_package_id(package)

                key = (module_id(file_record.module), target, "imports")
                edge = edge_map.get(key)
                if edge is None:
                    edge = Edge(
                        source=key[0], target=key[1], kind=key[2], evidence=[evidence]
                    )
                    edge_map[key] = edge
                elif not self._has_evidence(edge, evidence):
                    edge.evidence.append(evidence)

        analysis.edges = sorted(
            edge_map.values(), key=lambda edge: (edge.source, edge.target, edge.kind)
        )
        return analysis

    @classmethod
    def _import_nodes(
        cls, tree: ast.AST
    ) -> list[tuple[ast.Import | ast.ImportFrom, str]]:
        del cls
        visitor = _RuntimeImportVisitor()
        visitor.visit(tree)
        return visitor.nodes

    def _candidates(
        self, file_record: FileRecord, node: ast.AST, scope: str
    ) -> list[tuple[str, Evidence]]:
        if isinstance(node, ast.Import):
            return [
                (
                    alias.name,
                    Evidence(
                        kind=self._evidence_kind(scope),
                        path=file_record.path,
                        start_line=node.lineno,
                        end_line=getattr(node, "end_lineno", node.lineno),
                        column=node.col_offset,
                        detail=f"import {alias.name}",
                    ),
                )
                for alias in node.names
            ]

        if isinstance(node, ast.ImportFrom):
            base = self._resolve_import_from_base(file_record, node)
            if not base:
                return []

            evidence = Evidence(
                kind=self._evidence_kind(scope),
                path=file_record.path,
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                column=node.col_offset,
                detail=f"from {'.' * node.level}{node.module or ''} import ...",
            )
            candidates = [(base, evidence)]
            candidates.extend(
                (f"{base}.{alias.name}", evidence)
                for alias in node.names
                if alias.name != "*"
            )
            return candidates

        return []

    @staticmethod
    def _evidence_kind(scope: str) -> str:
        return "ast_local_import" if scope == "local" else "ast_import"

    @staticmethod
    def _resolve_import_from_base(
        file_record: FileRecord, node: ast.ImportFrom
    ) -> str | None:
        if node.level == 0:
            return node.module

        module_parts = file_record.module.split(".")
        package_parts = module_parts if file_record.is_package else module_parts[:-1]
        keep_count = len(package_parts) - node.level + 1
        if keep_count < 0:
            return None

        base_parts = package_parts[:keep_count]
        if node.module:
            base_parts.extend(node.module.split("."))
        return ".".join(part for part in base_parts if part)

    @staticmethod
    def _resolve_internal(candidate: str, module_names: set[str]) -> str | None:
        if candidate in module_names:
            return candidate

        parts = candidate.split(".")
        for idx in range(len(parts), 0, -1):
            prefix = ".".join(parts[:idx])
            if prefix in module_names:
                return prefix
        return None

    @staticmethod
    def _has_evidence(edge: Edge, evidence: Evidence) -> bool:
        candidate_key = evidence_key(evidence)
        return any(evidence_key(item) == candidate_key for item in edge.evidence)


class _RuntimeImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.nodes: list[tuple[ast.Import | ast.ImportFrom, str]] = []
        self._function_depth = 0

    def visit_Import(self, node: ast.Import) -> None:
        self.nodes.append((node, self._scope()))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.nodes.append((node, self._scope()))

    def visit_If(self, node: ast.If) -> None:
        if self._is_type_checking_guard(node.test):
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_body(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    def _visit_function_body(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        self._function_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self._function_depth -= 1

    def _scope(self) -> str:
        return "local" if self._function_depth else "module"

    @staticmethod
    def _is_type_checking_guard(test: ast.AST) -> bool:
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING"
        return False
