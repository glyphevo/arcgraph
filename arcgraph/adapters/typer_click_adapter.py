"""Typer and Click CLI command extraction."""

from __future__ import annotations

import ast

from arcgraph.adapters.common import (
    AdapterAnalysis,
    ImportAliases,
    SemanticAdapter,
    call_name,
    import_aliases,
    keyword,
    literal_str,
    make_adapter_edge,
    resolve_alias,
)
from arcgraph.core.ids import cli_command_id, cli_option_id, function_id
from arcgraph.core.schemas import Edge, FileRecord, Node


class TyperClickAdapter(SemanticAdapter):
    name = "typer_click"
    capabilities = ("entrypoint:cli_command", "resource:cli_option", "edge:invokes")
    required_facts = ("function",)

    TYPER_FACTORY = {"typer.Typer", "Typer"}
    CLICK_COMMANDS = {"click.command", "click.group", "command", "group"}
    CLICK_OPTIONS = {"click.option", "option"}

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        del nodes
        analysis = AdapterAnalysis()
        command_count = 0
        option_count = 0

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            typer_apps = self._typer_apps(tree, imports)
            click_groups = self._click_groups(tree, imports)
            for stmt in tree.body:
                if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                command = self._command_registration(
                    stmt,
                    imports,
                    typer_apps,
                    click_groups,
                )
                if command is None:
                    continue
                command_name, framework, decorator = command
                handler_id = function_id(f"{file_record.module}.{stmt.name}")
                option_names = self._options(stmt, imports)
                command_node = Node(
                    id=cli_command_id(command_name),
                    kind="cli_command",
                    name=command_name,
                    qualname=command_name,
                    path=file_record.path,
                    start_line=getattr(decorator, "lineno", stmt.lineno),
                    end_line=getattr(stmt, "end_lineno", stmt.lineno),
                    properties={
                        "framework": framework,
                        "handler": handler_id,
                        "options": option_names,
                    },
                )
                analysis.nodes.append(command_node)
                analysis.edges.append(
                    self._edge(
                        command_node.id,
                        handler_id,
                        "invokes",
                        "cli_command",
                        file_record,
                        decorator,
                        f"{framework} command {command_name} -> {stmt.name}",
                    )
                )
                command_count += 1
                for option_name in option_names:
                    option_node = Node(
                        id=cli_option_id(command_name, option_name),
                        kind="cli_option",
                        name=option_name,
                        qualname=f"{command_name}.{option_name}",
                        path=file_record.path,
                        start_line=getattr(decorator, "lineno", stmt.lineno),
                        properties={
                            "command": command_name,
                            "handler": handler_id,
                            "framework": framework,
                        },
                    )
                    analysis.nodes.append(option_node)
                    analysis.edges.append(
                        self._edge(
                            command_node.id,
                            option_node.id,
                            "declares",
                            "cli_option",
                            file_record,
                            decorator,
                            f"{command_name} option {option_name}",
                        )
                    )
                    option_count += 1

        analysis.nodes.sort(key=lambda node: node.id)
        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        analysis.metrics = {
            "cli_commands": command_count,
            "cli_options": option_count,
            "discovered_registrations": command_count,
            "resolved_handlers": command_count,
            "unresolved_registrations": 0,
        }
        return analysis

    def _typer_apps(self, tree: ast.Module, imports: ImportAliases) -> dict[str, str]:
        apps: dict[str, str] = {}
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
                continue
            call = resolve_alias(call_name(stmt.value.func), imports)
            if call not in self.TYPER_FACTORY:
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    apps[target.id] = ""
        for stmt in tree.body:
            if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
                continue
            call = stmt.value
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr != "add_typer" or not call.args:
                continue
            parent = call_name(call.func.value)
            child = call_name(call.args[0])
            if parent not in apps or child not in apps:
                continue
            name = literal_str(keyword(call, "name"))
            if not name:
                name = child.replace("_app", "").replace("_", "-")
            apps[child] = self._join_command(apps[parent], name)
        return apps

    def _click_groups(self, tree: ast.Module, imports: ImportAliases) -> dict[str, str]:
        groups: dict[str, str] = {}
        for stmt in tree.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in stmt.decorator_list:
                target = (
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                if resolve_alias(call_name(target), imports) in {
                    "click.group",
                    "group",
                }:
                    groups[stmt.name] = self._command_name(stmt, decorator)
        return groups

    def _command_registration(
        self,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        imports: ImportAliases,
        typer_apps: dict[str, str],
        click_groups: dict[str, str],
    ) -> tuple[str, str, ast.AST] | None:
        for decorator in stmt.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            name = call_name(target)
            resolved = resolve_alias(name, imports)
            if isinstance(target, ast.Attribute):
                receiver = call_name(target.value)
                if receiver in typer_apps and target.attr in {"command", "callback"}:
                    return (
                        self._join_command(
                            typer_apps[receiver],
                            self._command_name(stmt, decorator),
                        ),
                        "typer",
                        decorator,
                    )
                if receiver in click_groups and target.attr == "command":
                    return (
                        self._join_command(
                            click_groups[receiver],
                            self._command_name(stmt, decorator),
                        ),
                        "click",
                        decorator,
                    )
            if resolved in self.CLICK_COMMANDS:
                return (self._command_name(stmt, decorator), "click", decorator)
        return None

    @staticmethod
    def _command_name(
        stmt: ast.FunctionDef | ast.AsyncFunctionDef, decorator: ast.AST
    ) -> str:
        if isinstance(decorator, ast.Call):
            if decorator.args and (value := literal_str(decorator.args[0])):
                return value
            if value := literal_str(keyword(decorator, "name")):
                return value
        return stmt.name.replace("_", "-")

    @staticmethod
    def _join_command(prefix: str, name: str) -> str:
        if not prefix:
            return name
        if not name:
            return prefix
        return f"{prefix} {name}"

    def _options(
        self,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        imports: ImportAliases,
    ) -> list[str]:
        options: set[str] = set()
        for decorator in stmt.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if resolve_alias(call_name(decorator.func), imports) in self.CLICK_OPTIONS:
                options.update(self._option_names_from_args(decorator.args))

        positional_args = [*stmt.args.posonlyargs, *stmt.args.args]
        defaults = [None] * (len(positional_args) - len(stmt.args.defaults)) + list(
            stmt.args.defaults
        )
        for arg, default in zip(positional_args, defaults, strict=False):
            if self._is_typer_option(default, imports):
                options.add(arg.arg)
        for arg, default in zip(
            stmt.args.kwonlyargs, stmt.args.kw_defaults, strict=False
        ):
            if self._is_typer_option(default, imports):
                options.add(arg.arg)
        return sorted(options)

    @staticmethod
    def _option_names_from_args(args: list[ast.expr]) -> set[str]:
        names: set[str] = set()
        for arg in args:
            value = literal_str(arg)
            if not value or not value.startswith("-"):
                continue
            names.add(value.lstrip("-").replace("-", "_"))
        return names

    def _is_typer_option(self, node: ast.AST | None, imports: ImportAliases) -> bool:
        if not isinstance(node, ast.Call):
            return False
        return resolve_alias(call_name(node.func), imports) in {
            "typer.Option",
            "Option",
        }

    @staticmethod
    def _edge(
        source: str,
        target: str,
        kind: str,
        evidence_kind: str,
        file_record: FileRecord,
        node: ast.AST,
        detail: str,
    ) -> Edge:
        return make_adapter_edge(
            source=source,
            target=target,
            kind=kind,
            confidence="confirmed",
            evidence_kind=evidence_kind,
            file_record=file_record,
            node=node,
            detail=detail,
        )
