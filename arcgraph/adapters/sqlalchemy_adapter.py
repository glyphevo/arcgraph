"""SQLAlchemy ORM model and resource access extraction."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from arcgraph.adapters.common import (
    AdapterAnalysis,
    ImportAliases,
    NodeIndex,
    SemanticAdapter,
    annotation_name,
    call_name,
    import_aliases,
    iter_symbol_defs,
    literal_str,
    symbol_id_for_def,
)
from arcgraph.core.ids import table_id
from arcgraph.core.schemas import Edge, Evidence, FileRecord, Node


@dataclass(frozen=True)
class TableFact:
    node_id: str
    table_name: str
    model_id: str | None = None
    model_name: str | None = None


class SQLAlchemyAdapter(SemanticAdapter):
    """Extract SQLAlchemy tables and conservative read/write edges."""

    name = "sqlalchemy"
    capabilities = ("resource:table", "edge:maps_to", "edge:reads", "edge:writes")
    required_facts = ("class", "function", "method")

    READ_FUNCTIONS = {"select", "select_from"}
    READ_METHODS = {"get"}
    WRITE_FUNCTIONS = {"insert", "update", "delete"}
    WRITE_METHODS = {"add", "add_all", "merge", "delete"}

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        analysis = AdapterAnalysis()
        node_index = NodeIndex(nodes)
        table_by_model_name = self._tables_from_context(nodes)

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            for class_stmt in [
                item for item in tree.body if isinstance(item, ast.ClassDef)
            ]:
                fact = self._model_table_fact(
                    file_record, class_stmt, imports, node_index
                )
                if fact is None:
                    continue
                table_by_model_name[class_stmt.name] = fact
                analysis.nodes.append(
                    Node(
                        id=fact.node_id,
                        kind="table",
                        name=fact.table_name,
                        qualname=fact.table_name,
                        path=file_record.path,
                        start_line=class_stmt.lineno,
                        end_line=getattr(class_stmt, "end_lineno", class_stmt.lineno),
                        properties={
                            "orm": "sqlalchemy",
                            "table_name": fact.table_name,
                            "model": fact.model_id,
                            "model_name": fact.model_name,
                        },
                    )
                )
                if fact.model_id:
                    analysis.edges.append(
                        self._edge(
                            fact.model_id,
                            fact.node_id,
                            "maps_to",
                            file_record,
                            class_stmt,
                            f"{fact.model_name}.__tablename__ = {fact.table_name}",
                        )
                    )

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            for stmt, qualified_name, id_factory in iter_symbol_defs(tree):
                source_id = symbol_id_for_def(file_record, qualified_name, id_factory)
                locals_by_name = self._model_locals(
                    stmt, file_record, imports, node_index, table_by_model_name
                )
                analysis.edges.extend(
                    self._resource_edges(
                        stmt,
                        file_record,
                        imports,
                        node_index,
                        table_by_model_name,
                        locals_by_name,
                        source_id,
                    )
                )

        analysis.nodes.sort(key=lambda node: node.id)
        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        return analysis

    def _tables_from_context(self, nodes: list[Node]) -> dict[str, TableFact]:
        facts: dict[str, TableFact] = {}
        for node in nodes:
            if node.kind != "table":
                continue
            model_name = node.properties.get("model_name")
            table_name = node.properties.get("table_name") or node.name
            if isinstance(model_name, str) and isinstance(table_name, str):
                facts[model_name] = TableFact(
                    node_id=node.id,
                    table_name=table_name,
                    model_id=self._str_or_none(node.properties.get("model")),
                    model_name=model_name,
                )
        return facts

    def _model_table_fact(
        self,
        file_record: FileRecord,
        class_stmt: ast.ClassDef,
        imports: ImportAliases,
        node_index: NodeIndex,
    ) -> TableFact | None:
        table_name: str | None = None
        for item in class_stmt.body:
            if not isinstance(item, ast.Assign):
                continue
            if any(
                self._target_name(target) == "__tablename__" for target in item.targets
            ):
                table_name = literal_str(item.value)
                break
        if table_name is None:
            return None

        model = node_index.class_for_annotation(class_stmt.name, file_record, imports)
        return TableFact(
            node_id=table_id(table_name),
            table_name=table_name,
            model_id=model.id if model else None,
            model_name=class_stmt.name,
        )

    def _model_locals(
        self,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        file_record: FileRecord,
        imports: ImportAliases,
        node_index: NodeIndex,
        table_by_model_name: dict[str, TableFact],
    ) -> dict[str, TableFact]:
        locals_by_name: dict[str, TableFact] = {}
        for arg in [
            *stmt.args.posonlyargs,
            *stmt.args.args,
            *stmt.args.kwonlyargs,
        ]:
            target = self._table_from_annotation(
                annotation_name(arg.annotation),
                file_record,
                imports,
                node_index,
                table_by_model_name,
            )
            if target:
                locals_by_name[arg.arg] = target

        for node in ast.walk(stmt):
            if isinstance(node, ast.Assign):
                target = self._table_from_expr(
                    node.value,
                    file_record,
                    imports,
                    node_index,
                    table_by_model_name,
                    locals_by_name,
                )
                if target:
                    for item in node.targets:
                        if isinstance(item, ast.Name):
                            locals_by_name[item.id] = target
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = self._table_from_annotation(
                    annotation_name(node.annotation),
                    file_record,
                    imports,
                    node_index,
                    table_by_model_name,
                )
                if target:
                    locals_by_name[node.target.id] = target
        return locals_by_name

    def _resource_edges(
        self,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        file_record: FileRecord,
        imports: ImportAliases,
        node_index: NodeIndex,
        table_by_model_name: dict[str, TableFact],
        locals_by_name: dict[str, TableFact],
        source_id: str,
    ) -> list[Edge]:
        edges: list[Edge] = []
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node.func)
            if not name:
                continue
            operation = name.rsplit(".", 1)[-1]
            if operation in self.READ_FUNCTIONS:
                targets = self._tables_from_args(
                    node.args,
                    file_record,
                    imports,
                    node_index,
                    table_by_model_name,
                    locals_by_name,
                )
                edges.extend(
                    self._table_edges(source_id, targets, "reads", file_record, node)
                )
            elif operation in self.READ_METHODS:
                targets = self._tables_from_args(
                    node.args[:1],
                    file_record,
                    imports,
                    node_index,
                    table_by_model_name,
                    locals_by_name,
                )
                edges.extend(
                    self._table_edges(source_id, targets, "reads", file_record, node)
                )
            elif operation in self.WRITE_FUNCTIONS:
                targets = self._tables_from_args(
                    node.args,
                    file_record,
                    imports,
                    node_index,
                    table_by_model_name,
                    locals_by_name,
                )
                edges.extend(
                    self._table_edges(source_id, targets, "writes", file_record, node)
                )
            elif operation in self.WRITE_METHODS:
                targets = self._tables_from_args(
                    node.args,
                    file_record,
                    imports,
                    node_index,
                    table_by_model_name,
                    locals_by_name,
                )
                edges.extend(
                    self._table_edges(source_id, targets, "writes", file_record, node)
                )
        return edges

    def _tables_from_args(
        self,
        args: list[ast.expr],
        file_record: FileRecord,
        imports: ImportAliases,
        node_index: NodeIndex,
        table_by_model_name: dict[str, TableFact],
        locals_by_name: dict[str, TableFact],
    ) -> list[TableFact]:
        facts: dict[str, TableFact] = {}
        for arg in args:
            for table in self._tables_from_expr(
                arg,
                file_record,
                imports,
                node_index,
                table_by_model_name,
                locals_by_name,
            ):
                facts[table.node_id] = table
        return sorted(facts.values(), key=lambda fact: fact.node_id)

    def _tables_from_expr(
        self,
        expr: ast.AST,
        file_record: FileRecord,
        imports: ImportAliases,
        node_index: NodeIndex,
        table_by_model_name: dict[str, TableFact],
        locals_by_name: dict[str, TableFact],
    ) -> list[TableFact]:
        if isinstance(expr, ast.Name):
            local = locals_by_name.get(expr.id)
            if local:
                return [local]
            table = self._table_from_annotation(
                expr.id, file_record, imports, node_index, table_by_model_name
            )
            return [table] if table else []

        if isinstance(expr, ast.Attribute):
            base = call_name(expr.value)
            table = self._table_from_annotation(
                base, file_record, imports, node_index, table_by_model_name
            )
            return [table] if table else []

        if isinstance(expr, ast.Call):
            table = self._table_from_annotation(
                call_name(expr.func),
                file_record,
                imports,
                node_index,
                table_by_model_name,
            )
            return [table] if table else []

        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            tables: dict[str, TableFact] = {}
            for item in expr.elts:
                for table in self._tables_from_expr(
                    item,
                    file_record,
                    imports,
                    node_index,
                    table_by_model_name,
                    locals_by_name,
                ):
                    tables[table.node_id] = table
            return sorted(tables.values(), key=lambda fact: fact.node_id)

        return []

    def _table_from_expr(
        self,
        expr: ast.AST,
        file_record: FileRecord,
        imports: ImportAliases,
        node_index: NodeIndex,
        table_by_model_name: dict[str, TableFact],
        locals_by_name: dict[str, TableFact],
    ) -> TableFact | None:
        tables = self._tables_from_expr(
            expr,
            file_record,
            imports,
            node_index,
            table_by_model_name,
            locals_by_name,
        )
        return tables[0] if len(tables) == 1 else None

    def _table_from_annotation(
        self,
        annotation: str | None,
        file_record: FileRecord,
        imports: ImportAliases,
        node_index: NodeIndex,
        table_by_model_name: dict[str, TableFact],
    ) -> TableFact | None:
        model = node_index.class_for_annotation(annotation, file_record, imports)
        if model is None:
            return None
        return table_by_model_name.get(model.name)

    def _table_edges(
        self,
        source_id: str,
        targets: list[TableFact],
        kind: str,
        file_record: FileRecord,
        node: ast.AST,
    ) -> list[Edge]:
        return [
            self._edge(
                source_id,
                target.node_id,
                kind,
                file_record,
                node,
                f"{kind} {target.table_name}",
            )
            for target in targets
        ]

    @staticmethod
    def _target_name(target: ast.AST) -> str | None:
        if isinstance(target, ast.Name):
            return target.id
        if isinstance(target, ast.Attribute):
            return target.attr
        return None

    @staticmethod
    def _str_or_none(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _edge(
        source: str,
        target: str,
        kind: str,
        file_record: FileRecord,
        node: ast.AST,
        detail: str,
    ) -> Edge:
        return Edge(
            source=source,
            target=target,
            kind=kind,
            confidence="confirmed",
            evidence=[
                Evidence(
                    kind="sqlalchemy_resource",
                    path=file_record.path,
                    start_line=getattr(node, "lineno", None),
                    end_line=getattr(node, "end_lineno", getattr(node, "lineno", None)),
                    column=getattr(node, "col_offset", None),
                    detail=detail,
                )
            ],
        )
