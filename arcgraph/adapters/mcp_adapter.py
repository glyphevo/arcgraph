"""MCP tool extraction for ArcGraph."""

from __future__ import annotations

import ast

from arcgraph.adapters.common import (
    AdapterAnalysis,
    SemanticAdapter,
    keyword,
    literal_str,
)
from arcgraph.core.ids import function_id, mcp_tool_id
from arcgraph.core.schemas import Edge, Evidence, FileRecord, Node


class MCPAdapter(SemanticAdapter):
    name = "mcp"
    capabilities = ("entrypoint:mcp_tool", "edge:invokes")
    required_facts = ("function",)

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        del nodes, module_names
        analysis = AdapterAnalysis()
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            for stmt in tree.body:
                if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                tool_call = self._tool_registration(stmt)
                if tool_call is None:
                    continue
                tool_name = literal_str(keyword(tool_call, "name"))
                if not tool_name:
                    continue
                node_id = mcp_tool_id(tool_name)
                handler_id = function_id(f"{file_record.module}.{stmt.name}")
                group = self._group_name(keyword(tool_call, "group"))
                analysis.nodes.append(
                    Node(
                        id=node_id,
                        kind="mcp_tool",
                        name=tool_name,
                        qualname=tool_name,
                        path=file_record.path,
                        start_line=getattr(tool_call, "lineno", stmt.lineno),
                        end_line=getattr(stmt, "end_lineno", stmt.lineno),
                        properties={"handler": handler_id, "group": group},
                    )
                )
                analysis.edges.append(
                    Edge(
                        source=node_id,
                        target=handler_id,
                        kind="invokes",
                        confidence="confirmed",
                        evidence=[
                            Evidence(
                                kind="mcp_tool_registration",
                                path=file_record.path,
                                start_line=getattr(tool_call, "lineno", None),
                                end_line=getattr(
                                    tool_call,
                                    "end_lineno",
                                    getattr(tool_call, "lineno", None),
                                ),
                                column=getattr(tool_call, "col_offset", None),
                                detail=f"@register_tool(name={tool_name!r})",
                            )
                        ],
                    )
                )
        analysis.nodes.sort(key=lambda node: node.id)
        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        return analysis

    @staticmethod
    def _tool_registration(
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> ast.Call | None:
        for decorator in stmt.decorator_list:
            if isinstance(decorator, ast.Call):
                func = decorator.func
                if isinstance(func, ast.Name) and func.id == "register_tool":
                    return decorator
        return None

    @staticmethod
    def _group_name(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Attribute):
            return node.attr
        return literal_str(node)
