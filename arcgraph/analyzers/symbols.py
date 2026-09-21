"""Symbol analyzer for Python AST files."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Iterable

from arcgraph.analyzers.python_scopes import nested_functions
from arcgraph.core.expression_kinds import call_expression_kind
from arcgraph.core.ids import class_id, function_id, method_id, module_id
from arcgraph.core.schemas import Edge, Evidence, FileRecord, Node


@dataclass
class SymbolAnalysis:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    module_callsites: list[dict[str, object]] = field(default_factory=list)


class SymbolAnalyzer:
    # Base names whose direct subclass is *declaring* a protocol/ABC, not
    # implementing one.  Only user-defined subclasses of these are checked by
    # the call analyzer for ``implements`` overlay edges.
    _PROTOCOL_NAMES: frozenset[str] = frozenset(
        {
            "Protocol",
            "typing.Protocol",
            "typing_extensions.Protocol",
        }
    )
    _ABC_NAMES: frozenset[str] = frozenset(
        {
            "ABC",
            "abc.ABC",
            "ABCMeta",
            "abc.ABCMeta",
        }
    )

    def analyze(self, file_record: FileRecord, tree: ast.Module) -> SymbolAnalysis:
        analysis = SymbolAnalysis()
        analysis.module_callsites = list(self._module_body_callsites(tree))

        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                self._add_class(file_record, stmt, analysis)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._add_function(file_record, stmt, analysis)

        counts: dict[str, int] = {}
        for node in analysis.nodes:
            counts[node.id] = counts.get(node.id, 0) + 1
        for node in analysis.nodes:
            if node.properties.get("lexical_parent") and counts[node.id] > 1:
                node.properties["ambiguous_definition"] = True
        analysis.nodes.sort(key=lambda node: node.id)
        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        return analysis

    def _add_class(
        self, file_record: FileRecord, stmt: ast.ClassDef, analysis: SymbolAnalysis
    ) -> None:
        qualname = f"{file_record.module}.{stmt.name}"
        bases = [self._unparse(base) for base in stmt.bases]
        abstract_kind = self._detect_abstract_kind(bases)

        node = Node(
            id=class_id(qualname),
            kind="class",
            name=stmt.name,
            qualname=qualname,
            path=file_record.path,
            start_line=stmt.lineno,
            end_line=getattr(stmt, "end_lineno", stmt.lineno),
            properties={
                "decorators": self._decorators(stmt.decorator_list),
                "bases": bases,
                "callsites": list(self._class_body_callsites(stmt)),
                **(
                    {
                        "abstract_kind": abstract_kind,
                    }
                    if abstract_kind
                    else {}
                ),
            },
        )
        analysis.nodes.append(node)
        analysis.edges.append(
            self._defines_edge(
                file_record, module_id(file_record.module), node.id, stmt
            )
        )

        has_abstract_methods = False
        for child in stmt.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_abstract = self._is_abstract_method(child)
                if is_abstract:
                    has_abstract_methods = True
                self._add_method(
                    file_record,
                    stmt.name,
                    qualname,
                    child,
                    analysis,
                    is_abstract_method=is_abstract,
                )

        if has_abstract_methods:
            node.properties["has_abstract_methods"] = True
            # If the class has abstract methods but wasn't detected from bases,
            # infer abstract_kind="abc" (e.g. using ABCMeta as metaclass).
            if not abstract_kind:
                node.properties["abstract_kind"] = "abc"

    def _add_function(
        self,
        file_record: FileRecord,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        analysis: SymbolAnalysis,
        *,
        parent: Node | None = None,
    ) -> None:
        qualname = f"{parent.qualname if parent else file_record.module}.{stmt.name}"
        props = self._function_properties(stmt)
        if parent is not None:
            props.update(lexical_parent=parent.id, module=file_record.module)
        node = Node(
            id=function_id(qualname),
            kind="function",
            name=stmt.name,
            qualname=qualname,
            path=file_record.path,
            start_line=stmt.lineno,
            end_line=getattr(stmt, "end_lineno", stmt.lineno),
            properties=props,
        )
        analysis.nodes.append(node)
        analysis.edges.append(
            self._defines_edge(
                file_record,
                parent.id if parent else module_id(file_record.module),
                node.id,
                stmt,
            )
        )
        for child in nested_functions(stmt.body):
            self._add_function(file_record, child, analysis, parent=node)

    def _add_method(
        self,
        file_record: FileRecord,
        class_name: str,
        class_qualname: str,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        analysis: SymbolAnalysis,
        *,
        is_abstract_method: bool = False,
    ) -> None:
        qualname = f"{class_qualname}.{stmt.name}"
        props = {**self._function_properties(stmt), "class": class_name}
        if is_abstract_method:
            props["is_abstract_method"] = True
        node = Node(
            id=method_id(qualname),
            kind="method",
            name=stmt.name,
            qualname=qualname,
            path=file_record.path,
            start_line=stmt.lineno,
            end_line=getattr(stmt, "end_lineno", stmt.lineno),
            properties=props,
        )
        analysis.nodes.append(node)
        analysis.edges.append(
            self._defines_edge(file_record, class_id(class_qualname), node.id, stmt)
        )
        for child in nested_functions(stmt.body):
            self._add_function(file_record, child, analysis, parent=node)

    @staticmethod
    def _defines_edge(
        file_record: FileRecord, source: str, target: str, stmt: ast.AST
    ) -> Edge:
        return Edge(
            source=source,
            target=target,
            kind="defines",
            evidence=[
                Evidence(
                    kind="ast_definition",
                    path=file_record.path,
                    start_line=getattr(stmt, "lineno", None),
                    end_line=getattr(stmt, "end_lineno", getattr(stmt, "lineno", None)),
                    column=getattr(stmt, "col_offset", None),
                )
            ],
        )

    @classmethod
    def _detect_abstract_kind(cls, bases: list[str]) -> str | None:
        """Return ``"protocol"`` or ``"abc"`` if any base is a known abstract type."""
        for base in bases:
            # Strip subscripts: ``Protocol[T]`` → ``Protocol``
            simple = base.split("[", 1)[0].strip()
            if simple in cls._PROTOCOL_NAMES:
                return "protocol"
            if simple in cls._ABC_NAMES:
                return "abc"
        return None

    @staticmethod
    def _is_abstract_method(
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> bool:
        """Return ``True`` if *stmt* has an ``@abstractmethod`` decorator."""
        for decorator in stmt.decorator_list:
            name: str | None = None
            if isinstance(decorator, ast.Name):
                name = decorator.id
            elif isinstance(decorator, ast.Attribute):
                name = decorator.attr
            if name == "abstractmethod":
                return True
        return False

    def _function_properties(
        self, stmt: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> dict[str, object]:
        return {
            "async": isinstance(stmt, ast.AsyncFunctionDef),
            "decorators": self._decorators(stmt.decorator_list),
            "params": self._params(stmt.args),
            "scope_body_positions": [
                [child.lineno, child.col_offset] for child in stmt.body
            ],
            "returns": self._unparse(stmt.returns) if stmt.returns else None,
            "callsites": list(self._callsites(stmt)),
        }

    def _callsites(
        self, stmt: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> Iterable[dict[str, object]]:
        yield from self._scope_callsites("function_body", *stmt.body)

    def _module_body_callsites(self, tree: ast.Module) -> Iterable[dict[str, object]]:
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from self._definition_header_callsites(stmt)
                continue
            yield from self._scope_callsites("module_body", stmt)

    def _class_body_callsites(self, stmt: ast.ClassDef) -> Iterable[dict[str, object]]:
        yield from self._scope_callsites("decorator", *stmt.decorator_list)
        yield from self._scope_callsites(
            "class_body",
            *stmt.bases,
            *(keyword.value for keyword in stmt.keywords),
        )
        for child in stmt.body:
            if isinstance(child, ast.ClassDef):
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from self._definition_header_callsites(child)
                continue
            yield from self._scope_callsites("class_body", child)

    def _definition_header_callsites(
        self, stmt: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> Iterable[dict[str, object]]:
        yield from self._scope_callsites("decorator", *stmt.decorator_list)
        yield from self._scope_callsites(
            "definition_default",
            *stmt.args.defaults,
            *(default for default in stmt.args.kw_defaults if default is not None),
        )

    def _scope_callsites(
        self, context: str, *roots: ast.AST
    ) -> Iterable[dict[str, object]]:
        visitor = _ScopedCallsiteVisitor(self, context)
        for root in roots:
            visitor.visit(root)
        return visitor.callsites

    def _call_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._call_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    @staticmethod
    def _params(args: ast.arguments) -> list[str]:
        params = [arg.arg for arg in (*args.posonlyargs, *args.args)]
        if args.vararg:
            params.append(f"*{args.vararg.arg}")
        params.extend(arg.arg for arg in args.kwonlyargs)
        if args.kwarg:
            params.append(f"**{args.kwarg.arg}")
        return params

    def _decorators(self, decorators: list[ast.expr]) -> list[str]:
        return [self._unparse(decorator) for decorator in decorators]

    @staticmethod
    def _unparse(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return node.__class__.__name__


class _ScopedCallsiteVisitor(ast.NodeVisitor):
    def __init__(self, analyzer: SymbolAnalyzer, context: str) -> None:
        self.analyzer = analyzer
        self.context = context
        self.callsites: list[dict[str, object]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = self.analyzer._call_name(node.func)
        if name is None and isinstance(node.func, ast.Call):
            name = self.analyzer._unparse(node.func)
        if name:
            receiver = self._receiver_expression(node.func)
            self.callsites.append(
                {
                    "name": name,
                    "line": node.lineno,
                    "column": node.col_offset,
                    "context": self.context,
                    "call_expression": self.analyzer._unparse(node),
                    "receiver": receiver,
                    "attribute": (
                        node.func.attr if isinstance(node.func, ast.Attribute) else None
                    ),
                    "expression_kind": call_expression_kind(name),
                }
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None

    def _receiver_expression(self, node: ast.AST) -> str | None:
        if not isinstance(node, ast.Attribute):
            return None
        return self.analyzer._unparse(node.value)
