"""Lightweight TypeRef summaries for Python AST files."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from arcgraph.analyzers.calls.lexical import LexicalScopes
from arcgraph.analyzers.exports import exported_class
from arcgraph.analyzers.external_types import (
    EXTERNAL_METHOD_RETURN_OWNERS,
    EXTERNAL_METHOD_RETURN_TYPES,
    PATH_TYPE_IDS,
    may_be_path_segment,
)
from arcgraph.analyzers.imports import ImportAnalyzer
from arcgraph.analyzers.type_unions import (
    contains_union,
    single_value_type,
    tuple_element_type,
    type_identity,
    union_alternatives,
    unknown_union_result,
)
from arcgraph.core.ids import class_id, type_ref_id
from arcgraph.core.schemas import Evidence, FileRecord, Node

_BUILTIN_TYPES = {
    "bool",
    "bytes",
    "complex",
    "dict",
    "float",
    "frozenset",
    "int",
    "list",
    "object",
    "set",
    "str",
    "tuple",
}
_NONE_NAMES = {"None", "NoneType"}
_TYPING_ORIGINS = {
    "Annotated",
    "Callable",
    "ClassVar",
    "Final",
    "Iterable",
    "Iterator",
    "Literal",
    "Mapping",
    "MutableMapping",
    "Optional",
    "Protocol",
    "Sequence",
    "Type",
    "Union",
}
_TRANSPARENT_ORIGINS = {"Annotated", "ClassVar", "Final", "Optional", "Type", "Union"}
_SCOPE_KINDS = {"module", "class", "function", "method"}
_EXTERNAL_CONSTRUCTOR_TYPES = {
    "extsym:argparse.ArgumentParser",
    "extsym:pathlib.Path",
    "extsym:threading.Thread",
    "extsym:tempfile.TemporaryDirectory",
}
_PYDANTIC_MODEL_BASES = {
    "pydantic.BaseModel",
    "pydantic.BaseSettings",
    "pydantic_settings.BaseSettings",
}
_PYDANTIC_INSTANCE_CLASSMETHOD_STRATEGIES = {
    "model_construct": "pydantic_model_construct",
    "model_validate": "pydantic_model_validate",
    "parse_obj": "pydantic_parse_obj",
}


@dataclass(frozen=True)
class _ResolvedType:
    expression: str
    type_id: str | None = None
    symbol_id: str | None = None
    origin: str | None = None
    type_args: tuple[dict[str, Any], ...] = ()
    status: str = "unresolved"


@dataclass
class TypeRefAnalysis:
    type_refs_by_scope: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    diagnostics_by_scope: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    type_refs_by_binding: dict[str, dict[str, Any]] = field(default_factory=dict)

    def attach_to_nodes(self, nodes: list[Node]) -> None:
        by_id = {node.id: node for node in nodes}
        for scope_id, type_refs in self.type_refs_by_scope.items():
            node = by_id.get(scope_id)
            if node is not None and type_refs:
                existing = node.properties.get("type_refs", [])
                if not isinstance(existing, list):
                    existing = []
                node.properties["type_refs"] = sorted(
                    [*existing, *type_refs],
                    key=lambda item: (
                        item.get("line") or 0,
                        item.get("column") or 0,
                        item.get("name") or "",
                        item.get("strategy") or "",
                    ),
                )
                self._attach_binding_type_refs(node)
        for scope_id, diagnostics in self.diagnostics_by_scope.items():
            node = by_id.get(scope_id)
            if node is not None and diagnostics:
                existing = node.properties.get("type_diagnostics", [])
                if not isinstance(existing, list):
                    existing = []
                node.properties["type_diagnostics"] = [*existing, *diagnostics]

    def _attach_binding_type_refs(self, node: Node) -> None:
        bindings = node.properties.get("bindings", [])
        if not isinstance(bindings, list):
            return
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            binding_ref = self.type_refs_by_binding.get(str(binding.get("binding_id")))
            if binding_ref is None:
                continue
            binding["type_ref_id"] = binding_ref["type_ref_id"]
            binding["type_ref_strategy"] = binding_ref["strategy"]
            binding["type_ref_confidence"] = binding_ref["confidence"]
            type_id = binding_ref.get("type_id")
            if isinstance(type_id, str):
                binding["type_ref"] = type_id


class TypeRefAnalyzer:
    """Collect V2.2 TypeRef summaries without changing call resolution."""

    def analyze(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        nodes: list[Node],
        module_names: set[str],
    ) -> TypeRefAnalysis:
        context = _TypeContext(file_record, tree, nodes, module_names)
        analysis = TypeRefAnalysis()
        context.attach_type_traits()

        for node in context.current_scope_nodes():
            annotations = [node.properties.get("returns")]
            annotations.extend(
                binding.get("annotation")
                for binding in node.properties.get("bindings", [])
                if isinstance(binding, dict)
            )
            # Keep the dependency marker even when the imported type is
            # currently missing, so a later provider restoration refreshes it.
            if any(
                context.uses_imported_annotation(value) for value in annotations
            ) or context.uses_imported_call(node):
                node.properties["imported_type_input"] = True
            self._record_return_type(node, context, analysis)
            self._record_binding_type_refs(node, context, analysis)

        return analysis

    def _record_return_type(
        self,
        node: Node,
        context: "_TypeContext",
        analysis: TypeRefAnalysis,
    ) -> None:
        returns = node.properties.get("returns")
        if not isinstance(returns, str) or not returns:
            return
        resolved = context.resolve_annotation(returns, use_imports=True)
        type_ref = self._type_ref_record(
            context,
            scope_id=node.id,
            scope_kind=node.kind,
            name="return",
            subject_kind="return",
            strategy="return_annotation",
            confidence="confirmed" if resolved.type_id else "unresolved",
            source_expression=returns,
            resolved=resolved,
            line=node.start_line,
            end_line=node.end_line,
            column=None,
        )
        analysis.type_refs_by_scope.setdefault(node.id, []).append(type_ref)
        if contains_union(type_ref):
            node.properties["union_type_input"] = True
        if not resolved.type_id and context.looks_custom_type(returns):
            self._add_diagnostic(
                analysis,
                node.id,
                "type_resolution_unresolved",
                "return",
                returns,
                context.file_record.path,
                node.start_line,
                None,
            )

    def _record_binding_type_refs(
        self,
        node: Node,
        context: "_TypeContext",
        analysis: TypeRefAnalysis,
    ) -> None:
        bindings = node.properties.get("bindings", [])
        if not isinstance(bindings, list):
            return

        local_types: dict[str, dict[str, Any]] = {}
        ordered = sorted(
            (item for item in bindings if isinstance(item, dict)),
            key=lambda item: (
                item.get("line") or 0,
                item.get("column") or 0,
                item.get("name") or "",
                item.get("kind") or "",
            ),
        )
        if node.properties.get("comprehension_contexts"):
            # The binding visitor follows generator evaluation order. Textual
            # order is different for result expressions and nested generators.
            ordered = [item for item in bindings if isinstance(item, dict)]
        for binding in ordered:
            value_types = local_types
            is_comprehension = binding.get("kind") == "comprehension_target"
            if is_comprehension:
                value_types = local_types.copy()
                for name, provider in binding.get("comprehension_inputs", {}).items():
                    for field in list(value_types):
                        if field.startswith(name + "."):
                            del value_types[field]
                    value_types[name] = analysis.type_refs_by_binding.get(provider) or {
                        **unknown_union_result(name),
                        "comprehension_binding": True,
                    }
            type_ref = self._binding_type_ref(binding, node, context, value_types)
            if is_comprehension and type_ref is not None:
                type_ref["comprehension_binding"] = True
                type_ref["typed_value_evidence"] = True
            if type_ref is None:
                previous = local_types.get(str(binding.get("name")))
                if previous and (
                    previous.get("typed_value_evidence")
                    or union_alternatives(previous) is not None
                ):
                    local_types[str(binding["name"])] = unknown_union_result(
                        str(binding.get("value") or binding["name"])
                    )
                continue
            analysis.type_refs_by_scope.setdefault(node.id, []).append(type_ref)
            if contains_union(type_ref):
                node.properties["union_type_input"] = True
            binding_id = binding.get("binding_id")
            if isinstance(binding_id, str):
                analysis.type_refs_by_binding[binding_id] = type_ref
            if is_comprehension:
                continue
            if (
                isinstance(type_ref.get("type_id"), str)
                or union_alternatives(type_ref) is not None
            ):
                local_types[str(binding["name"])] = type_ref
                found = context.lexical.lookup(node, str(binding["name"]))
                preserved = context.preserve_nullable_binding(
                    node, str(binding["name"]), type_ref
                )
                if (
                    union_alternatives(type_ref) is not None
                    and binding.get("annotation")
                    and found
                    and not context.lexical.stable(*found)
                    and not preserved
                ):
                    # A later write invalidates propagation through aliases too.
                    local_types[str(binding["name"])] = unknown_union_result(
                        str(binding["name"])
                    )
                context.register_class_instance_attr(
                    node,
                    str(binding["name"]),
                    type_ref,
                )

            if (
                type_ref.get("resolution_status") == "unresolved"
                and isinstance(binding.get("annotation"), str)
                and context.looks_custom_type(binding["annotation"])
            ):
                self._add_diagnostic(
                    analysis,
                    node.id,
                    "type_resolution_unresolved",
                    str(binding["name"]),
                    binding["annotation"],
                    context.file_record.path,
                    self._int_or_none(binding.get("line")),
                    self._int_or_none(binding.get("column")),
                    binding_id=binding_id if isinstance(binding_id, str) else None,
                )

    def _binding_type_ref(
        self,
        binding: dict[str, Any],
        node: Node,
        context: "_TypeContext",
        local_types: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        name = binding.get("name")
        if not isinstance(name, str) or not name:
            return None

        annotation = binding.get("annotation")
        if isinstance(annotation, str) and annotation:
            resolved = context.resolve_annotation(annotation, use_imports=True)
            return self._type_ref_record(
                context,
                scope_id=node.id,
                scope_kind=node.kind,
                name=name,
                subject_kind="binding",
                strategy="annotation",
                confidence="confirmed" if resolved.type_id else "unresolved",
                source_expression=annotation,
                resolved=resolved,
                line=self._int_or_none(binding.get("line")),
                end_line=self._int_or_none(
                    binding.get("evidence", {}).get("end_line")
                    if isinstance(binding.get("evidence"), dict)
                    else None
                ),
                column=self._int_or_none(binding.get("column")),
                binding_id=(
                    binding.get("binding_id")
                    if isinstance(binding.get("binding_id"), str)
                    else None
                ),
            )

        value = binding.get("value")
        if isinstance(value, str) and value:
            propagated = self._propagated_type_ref(
                value,
                name,
                binding,
                node,
                context,
                local_types,
            )
            if propagated is not None:
                return propagated
        return None

    def _propagated_type_ref(
        self,
        value: str,
        name: str,
        binding: dict[str, Any],
        node: Node,
        context: "_TypeContext",
        local_types: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        parsed = context.parse_expression(value)
        if parsed is None:
            return None

        source = (
            unknown_union_result(value)
            if binding.get("comprehension_write")
            else (
                context.resolve_conditional_value(parsed, node, local_types, binding)
                if isinstance(parsed, ast.IfExp)
                else context.resolve_scoped_value(parsed, node, local_types)
            )
        )
        untyped_comprehension = (
            binding.get("kind") == "comprehension_target" and source is None
        )
        if binding.get("kind") == "instance_attribute":
            joined = context.typing_only_attribute(node, name, local_types)
            if joined is not None:
                source = joined
        if source is None:
            if binding.get("kind") != "comprehension_target":
                return None
            source = unknown_union_result(value)

        if binding.get("kind") == "with_as":
            context_manager_kind = binding.get("context_manager_kind")
            if context_manager_kind not in {"sync", "async"}:
                return None
            entered = context.context_manager_enter_source(
                source,
                is_async=context_manager_kind == "async",
            )
            if entered is None:
                return None
            source = entered

        if binding.get("kind") in {"for_target", "comprehension_target"}:
            iterated = self._iterated_type_source(source)
            if iterated is not None:
                source = iterated
            elif binding.get("kind") == "comprehension_target":
                untyped_comprehension = (
                    untyped_comprehension or union_alternatives(source) is None
                )
                source = unknown_union_result(value)

        if binding.get("unpack_path"):
            source = self._unpacked_type_source(source, binding["unpack_path"], value)

        strategy = source.get("strategy")
        if binding.get("kind") == "with_as":
            strategy = (
                "async_context_manager_enter"
                if binding.get("context_manager_kind") == "async"
                else "context_manager_enter"
            )
        elif binding.get("kind") == "instance_attribute" and isinstance(
            parsed, ast.Name
        ):
            strategy = "instance_attribute_propagation"
        elif binding.get("kind") in {"for_target", "comprehension_target"}:
            strategy = "iteration_element"
        elif isinstance(parsed, (ast.Attribute, ast.Name)):
            strategy = "assignment_propagation"
        if not isinstance(strategy, str):
            strategy = "assignment_propagation"

        result = self._type_ref_record(
            context,
            scope_id=node.id,
            scope_kind=node.kind,
            name=name,
            subject_kind="binding",
            strategy=strategy,
            confidence="inferred",
            source_expression=value,
            resolved=_ResolvedType(
                expression=str(source.get("type_expression") or value),
                type_id=(
                    source.get("type_id")
                    if isinstance(source.get("type_id"), str)
                    else None
                ),
                symbol_id=(
                    source.get("symbol_id")
                    if isinstance(source.get("symbol_id"), str)
                    else None
                ),
                origin=(
                    source.get("origin")
                    if isinstance(source.get("origin"), str)
                    else None
                ),
                type_args=(
                    tuple(
                        item
                        for item in source.get("type_args", [])
                        if isinstance(item, dict)
                    )
                    if isinstance(source.get("type_args"), list)
                    else ()
                ),
                status=(
                    "resolved"
                    if isinstance(source.get("type_id"), str)
                    else "unresolved"
                ),
            ),
            line=self._int_or_none(binding.get("line")),
            end_line=self._int_or_none(
                binding.get("evidence", {}).get("end_line")
                if isinstance(binding.get("evidence"), dict)
                else None
            ),
            column=self._int_or_none(binding.get("column")),
            binding_id=(
                binding.get("binding_id")
                if isinstance(binding.get("binding_id"), str)
                else None
            ),
            inferred_from_type_ref_id=(
                source.get("type_ref_id")
                if isinstance(source.get("type_ref_id"), str)
                else None
            ),
        )

        if untyped_comprehension:
            result["untyped_comprehension"] = True
        if binding.get("unpack_path") or source.get("typed_value_evidence"):
            result["typed_value_evidence"] = True
        return result

    @staticmethod
    def _unpacked_type_source(
        source: dict[str, Any], path: list[list[int]], expression: str
    ) -> dict[str, Any]:
        result = source
        for index, size in path:
            result = single_value_type(result)
            args: list[dict[str, Any]] = result.get("type_args", [])
            if (
                result.get("type_id") != "builtin:tuple"
                or size < 0
                or len(args) != size
                or any(arg.get("type_expression") == "..." for arg in args)
            ):
                return unknown_union_result(expression)
            result = args[index]
        return {**result, "strategy": "tuple_element", "source_expression": expression}

    @staticmethod
    def _iterated_type_source(source: dict[str, Any]) -> dict[str, Any] | None:
        wrapped = union_alternatives(source) is not None
        source = single_value_type(source)
        type_id = source.get("type_id")
        origin = source.get("origin")
        type_args = source.get("type_args")
        if not isinstance(type_args, list) or not type_args:
            return None
        if type_id == "builtin:tuple":
            item = tuple_element_type(source)
        elif type_id in {
            "builtin:list",
            "builtin:tuple",
            "builtin:set",
            "builtin:frozenset",
        }:
            item = type_args[0]
        elif origin in {"Iterable", "Iterator", "Sequence"}:
            item = type_args[0]
        elif type_id == "builtin:dict":
            item = type_args[0]
        else:
            return None
        if not isinstance(item, dict):
            return None
        propagated = dict(item)
        propagated["strategy"] = "iteration_element"
        if wrapped or source.get("typed_value_evidence"):
            propagated["typed_value_evidence"] = True
        propagated["source_expression"] = str(source.get("source_expression") or "")
        if isinstance(source.get("type_ref_id"), str):
            propagated["type_ref_id"] = source["type_ref_id"]
        return propagated

    def _type_ref_record(
        self,
        context: "_TypeContext",
        *,
        scope_id: str,
        scope_kind: str,
        name: str,
        subject_kind: str,
        strategy: str,
        confidence: str,
        source_expression: str,
        resolved: _ResolvedType,
        line: int | None,
        end_line: int | None,
        column: int | None,
        binding_id: str | None = None,
        inferred_from_type_ref_id: str | None = None,
    ) -> dict[str, Any]:
        evidence = Evidence(
            kind=f"ast_type_ref:{strategy}",
            path=context.file_record.path,
            start_line=line,
            end_line=end_line or line,
            column=column,
            detail=name,
        )
        record: dict[str, Any] = {
            "type_ref_id": type_ref_id(
                scope_id,
                name,
                subject_kind,
                strategy,
                context.file_record.path,
                line,
                column,
                source_expression,
            ),
            "scope_id": scope_id,
            "scope_kind": scope_kind,
            "name": name,
            "subject_kind": subject_kind,
            "strategy": strategy,
            "confidence": confidence,
            "path": context.file_record.path,
            "line": line,
            "column": column,
            "source_expression": source_expression,
            "type_expression": resolved.expression,
            "resolution_status": resolved.status,
            "fallbacks": [strategy],
            "evidence": evidence.model_dump(exclude_none=True),
        }
        self._set_optional(record, "binding_id", binding_id)
        self._set_optional(record, "type_id", resolved.type_id)
        self._set_optional(record, "symbol_id", resolved.symbol_id)
        self._set_optional(record, "origin", resolved.origin)
        if resolved.type_args:
            record["type_args"] = list(resolved.type_args)
        self._set_optional(
            record, "inferred_from_type_ref_id", inferred_from_type_ref_id
        )
        return record

    def _add_diagnostic(
        self,
        analysis: TypeRefAnalysis,
        scope_id: str,
        kind: str,
        name: str,
        expression: str,
        path: str,
        line: int | None,
        column: int | None,
        *,
        binding_id: str | None = None,
    ) -> None:
        diagnostic: dict[str, Any] = {
            "kind": kind,
            "scope_id": scope_id,
            "name": name,
            "expression": expression,
            "path": path,
            "line": line,
            "column": column,
            "message": f"Could not resolve TypeRef for {name!r}: {expression}.",
        }
        self._set_optional(diagnostic, "binding_id", binding_id)
        analysis.diagnostics_by_scope.setdefault(scope_id, []).append(diagnostic)

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        return value if isinstance(value, int) else None

    @staticmethod
    def _set_optional(record: dict[str, Any], key: str, value: Any | None) -> None:
        if value is not None:
            record[key] = value


class _TypeContext:
    def __init__(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        nodes: list[Node],
        module_names: set[str],
    ) -> None:
        self.file_record = file_record
        self.tree = tree
        self.nodes = nodes
        self.parameter_defaults: dict[int, dict[str, str]] = self._parameter_defaults(
            tree
        )
        self.module_names = module_names
        self.lexical = LexicalScopes(
            [node for node in nodes if node.path == file_record.path], {}
        )
        self.aliases = self._import_aliases(tree.body)
        self.classes_by_id = {node.id: node for node in nodes if node.kind == "class"}
        self.export_nodes = {
            node.id: node for node in nodes if node.kind in {"module", "class"}
        }
        self.classes_by_name: dict[str, list[Node]] = {}
        for node in self.classes_by_id.values():
            self.classes_by_name.setdefault(node.name, []).append(node)
        self.return_types_by_function_name: dict[str, list[_ResolvedType]] = {}
        self.return_types_by_function_qualname: dict[str, _ResolvedType] = {}
        self.return_types_by_method: dict[tuple[str, str], _ResolvedType] = {}
        self.property_return_types_by_method: dict[tuple[str, str], _ResolvedType] = {}
        self.class_instance_attrs: dict[str, dict[str, dict[str, Any]]] = {}
        self._build_return_type_maps()

    def current_scope_nodes(self) -> list[Node]:
        return [
            node
            for node in sorted(self.nodes, key=lambda item: item.id)
            if node.kind in _SCOPE_KINDS and node.path == self.file_record.path
        ]

    def uses_imported_annotation(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        expression = self.parse_expression(value)
        if expression is None:
            return False
        return any(
            (isinstance(node, ast.Name) and node.id in self.aliases)
            or (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value != value
                and self.uses_imported_annotation(node.value)
            )
            for node in ast.walk(expression)
        )

    def uses_imported_call(self, scope: Node) -> bool:
        # Unannotated constructors and inline calls depend on import/export
        # bindings too. Record inputs, not only successful resolutions: missing
        # providers must remain refreshable when they return. Scope aliases also
        # cover imports inside functions and captured imports in nested helpers.
        names = set(self.aliases) | set(self.lexical.aliases(scope))
        if not names:
            return False
        for callsite in scope.properties.get("callsites", []):
            expression = self.parse_expression(str(callsite.get("call_expression", "")))
            if expression is not None and any(
                isinstance(part, ast.Name) and part.id in names
                for part in ast.walk(expression)
            ):
                return True
        return False

    def attach_type_traits(self) -> None:
        for node in self.current_scope_nodes():
            if node.kind != "class":
                continue
            bases = node.properties.get("bases", [])
            if not isinstance(bases, list):
                continue
            base_names = {str(base).strip("\"'").rsplit(".", 1)[-1] for base in bases}
            traits: dict[str, bool] = {}
            if "Protocol" in base_names:
                traits["protocol"] = True
            if base_names & {"ABC", "ABCMeta"}:
                traits["abstract_base"] = True
            if traits:
                node.properties["type_traits"] = traits

    def resolve_annotation(
        self, expression: str, *, use_imports: bool
    ) -> _ResolvedType:
        parsed = self.parse_expression(expression.strip())
        if parsed is None:
            return _ResolvedType(expression=expression)
        return self._resolve_type_node(parsed, expression, use_imports=use_imports)

    def parse_expression(self, expression: str) -> ast.AST | None:
        try:
            return ast.parse(expression, mode="eval").body
        except SyntaxError:
            stripped = expression.strip("\"'")
            if stripped == expression:
                return None
            try:
                return ast.parse(stripped, mode="eval").body
            except SyntaxError:
                return None

    def resolve_value(
        self,
        node: ast.AST,
        local_types: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        while isinstance(node, ast.Await):
            node = node.value
        if isinstance(node, ast.Name):
            return local_types.get(node.id)
        literal = self._literal_value_type(node)
        if literal is not None:
            return self._value_type_record(
                literal,
                strategy="literal",
                source_expression=self._unparse(node),
            )
        if isinstance(node, ast.Call):
            return self._resolve_call(node, local_types)
        return None

    def resolve_scoped_value(
        self,
        node: ast.AST,
        scope_node: Node,
        local_types: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        if (
            isinstance(node, ast.BoolOp)
            and isinstance(node.op, ast.Or)
            and len(node.values) == 2
            and isinstance(node.values[0], ast.Name)
            and node.values[0].id in scope_node.properties.get("nullable_bindings", {})
        ):
            source = local_types.get(node.values[0].id)
            fallback = node.values[1]
            literal = self._literal_value_type(fallback)
            empty = not isinstance(
                fallback, (ast.Dict, ast.List, ast.Set, ast.Tuple)
            ) or not (
                fallback.keys if isinstance(fallback, ast.Dict) else fallback.elts
            )
            if (
                source
                and literal
                and empty
                and literal.type_id == source.get("type_id")
            ):
                return {
                    **single_value_type(source),
                    "strategy": "nullable_default",
                    "typed_value_evidence": True,
                }
            if source:
                return unknown_union_result(self._unparse(node))
        while isinstance(node, ast.Await):
            node = node.value
        if isinstance(node, ast.Attribute):
            full_name = self._unparse(node)
            if full_name in local_types:
                return local_types[full_name]
            if (
                isinstance(node.value, ast.Name)
                and node.value.id in {"self", "cls"}
                and not local_types.get(node.value.id, {}).get("comprehension_binding")
            ):
                class_qualname = self._class_qualname_for_scope(scope_node)
                if class_qualname:
                    attr_ref = self.class_instance_attrs.get(class_qualname, {}).get(
                        node.attr
                    )
                    if attr_ref is not None:
                        return attr_ref
                    property_ref = self._property_type_ref(
                        class_qualname,
                        node.attr,
                        source_expression=full_name,
                    )
                    if property_ref is not None:
                        return property_ref
            receiver = self.resolve_scoped_value(node.value, scope_node, local_types)
            if union_alternatives(receiver) is not None and not receiver.get("type_id"):
                return unknown_union_result(self._unparse(node))
            type_id = receiver.get("type_id") if receiver else None
            if node.attr == "parent" and type_id in PATH_TYPE_IDS:
                return self._path_value_type(receiver, node, strategy="path_parent")
            if isinstance(type_id, str) and type_id.startswith("class:"):
                class_qualname = type_id.removeprefix("class:")
                return self.class_instance_attrs.get(class_qualname, {}).get(
                    node.attr
                ) or self._property_type_ref(
                    class_qualname,
                    node.attr,
                    source_expression=full_name,
                )
            return None
        if isinstance(node, ast.Subscript):
            receiver = self.resolve_scoped_value(node.value, scope_node, local_types)
            if not receiver:
                return None
            if union_alternatives(receiver) is not None and not receiver.get("type_id"):
                return unknown_union_result(self._unparse(node))
            return self._subscript_value_type_source(receiver)
        if isinstance(node, ast.Call):
            return self._resolve_call(node, local_types, scope_node=scope_node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = self.resolve_scoped_value(node.left, scope_node, local_types)
            if (
                left is not None
                and left.get("type_id") in PATH_TYPE_IDS
                and self._may_be_path_segment(node.right, scope_node, local_types)
            ):
                return self._path_value_type(left, node, strategy="path_join")
            return None
        return self.resolve_value(node, local_types)

    def _may_be_path_segment(
        self,
        node: ast.expr,
        scope_node: Node,
        local_types: dict[str, dict[str, Any]],
    ) -> bool:
        return may_be_path_segment(
            node,
            lambda operand: self.resolve_scoped_value(operand, scope_node, local_types),
        )

    def _path_value_type(
        self,
        source: dict[str, Any],
        node: ast.AST,
        *,
        strategy: str,
    ) -> dict[str, Any] | None:
        # A union such as ``Path | None`` does not say which member is joined.
        if union_alternatives(source) is not None:
            return None
        type_id = str(source["type_id"])
        return self._value_type_record(
            _ResolvedType(
                expression=type_id.rsplit(".", 1)[-1],
                type_id=type_id,
                symbol_id=type_id,
                status="resolved",
            ),
            strategy=strategy,
            source_expression=self._unparse(node),
        )

    def preserve_nullable_binding(
        self, scope: Node, name: str, ref: dict[str, Any]
    ) -> bool:
        """All writes retain the one annotated non-None runtime class."""
        members = union_alternatives(ref)
        if members is None or not ref.get("type_id"):
            return False
        found = self.lexical.lookup(scope, name)
        if (
            not found
            or found[0].id != scope.id
            or (scope.id, name) in self.lexical.mutated
        ):
            return False
        bindings: list[dict[str, Any]] = found[1]
        first = bindings[0]
        if first.get("kind") not in {
            "parameter",
            "annotated_assignment",
        } or not first.get("annotation"):
            return False
        if first.get("kind") != "parameter" and [
            first.get("line"),
            first.get("column"),
        ] not in scope.properties.get("scope_body_positions", []):
            return False
        expected = str(ref["type_id"])
        for binding in bindings:
            if binding.get("kind") == "parameter":
                default = self.parameter_defaults.get(scope.start_line, {}).get(name)
                if default is not None and default != "None":
                    return False
                continue
            if binding.get("kind") not in {"assignment", "annotated_assignment"}:
                return False
            value = self.parse_expression(str(binding.get("value") or ""))
            if (
                isinstance(value, ast.BoolOp)
                and isinstance(value.op, ast.Or)
                and len(value.values) == 2
                and isinstance(value.values[0], ast.Name)
                and value.values[0].id == name
            ):
                value = value.values[1]
            if isinstance(value, ast.Constant) and value.value is None:
                continue
            literal = self._literal_value_type(value) if value is not None else None
            if literal and literal.type_id == expected:
                if isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Tuple)) and (
                    value.keys if isinstance(value, ast.Dict) else value.elts
                ):
                    return False
                continue
            if isinstance(value, ast.Call):
                constructor = self._name(value.func)
                root = constructor.split(".")[0] if constructor else None
                definition = self.lexical.lookup(scope, root) if root else None
                if (
                    constructor
                    and definition
                    and self._stable_conditional_binding(definition)
                ):
                    item: dict[str, Any] = definition[1][0]
                    if item.get("kind") == "class_definition" and constructor != root:
                        return False
                    actual = (
                        item.get("target")
                        if item.get("kind") == "class_definition"
                        else f"extsym:{self._resolve_alias(constructor, use_imports=True)}"
                    )
                    if actual == expected and item.get("kind") in {
                        "class_definition",
                        "import_alias",
                    }:
                        continue
            return False
        scope.properties.setdefault("nullable_bindings", {})[name] = {
            "line": first.get("line"),
            "column": first.get("column"),
            "type_id": expected,
        }
        return True

    def resolve_conditional_value(
        self,
        expression: ast.IfExp,
        scope: Node,
        local_types: dict[str, dict[str, Any]],
        binding: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Join proven class instances, never guess the unknown fallback arm.

        This deliberately accepts only a stable local assignment, annotated
        parameters and named class constructors. General alias/dataflow and
        arbitrary factory calls need their own evidence rules.
        """
        scope.properties["conditional_type_input"] = True
        found = self.lexical.lookup(scope, str(binding.get("name")))
        if (
            scope.kind not in {"function", "method"}
            or binding.get("kind") != "assignment"
            or not found
            or found[0].id != scope.id
            or not self._stable_conditional_binding(found)
            or any(isinstance(n, ast.NamedExpr) for n in ast.walk(expression))
        ):
            return None
        branches = []
        for truth, arm in ((True, expression.body), (False, expression.orelse)):
            source = self._conditional_branch_type(arm, scope, local_types)
            if source is None:
                return None
            candidates: set[str] = self._conditional_class_candidates(source)
            if isinstance(arm, ast.Name) and self._excludes_none(
                expression.test, arm.id, truth
            ):
                candidates.discard("builtin:None")
            if len(candidates) != 1:
                return None
            type_id = next(iter(candidates))
            if not type_id.startswith("class:"):
                return None
            branches.append(type_id)
        if branches[0] != branches[1]:
            return None
        # The call resolver must apply lexical availability/mutation checks to
        # these new facts, including aliases and calls before the assignment.
        scope.properties["conditional_type_inference"] = True
        self.lexical.strict.add(scope.id)
        return {
            "type_id": branches[0],
            "symbol_id": branches[0],
            "type_expression": branches[0].removeprefix("class:"),
            "strategy": "conditional_join",
        }

    def _conditional_branch_type(
        self, arm: ast.AST, scope: Node, local_types: dict[str, dict[str, Any]]
    ) -> dict[str, Any] | None:
        name = arm.id if isinstance(arm, ast.Name) else None
        if isinstance(arm, ast.Call) and isinstance(arm.func, ast.Name):
            name = arm.func.id
        if name is None:
            return None
        found = self.lexical.lookup(scope, name)
        if not found or not self._stable_conditional_binding(found):
            return None
        definition: dict[str, Any] = found[1][0]
        if isinstance(arm, ast.Name):
            if found[0].id != scope.id or definition.get("kind") != "parameter":
                return None
            source = local_types.get(name)
            default = self.parameter_defaults.get(scope.start_line, {}).get(name)
            if default is not None:
                if default != "None" or source is None:
                    return None
                # A None default is runtime evidence even when the annotation
                # omitted Optional. Only an explicit identity guard removes it.
                return {
                    "origin": "Union",
                    "type_args": [
                        source,
                        {"type_id": "builtin:None", "origin": "None"},
                    ],
                }
            return source
        if definition.get("kind") not in {"class_definition", "import_alias"}:
            return None
        source = self._resolve_call(arm, local_types, scope_node=scope)
        if source is None:
            return None
        expected = definition.get("target")
        if definition.get("kind") == "import_alias":
            qualified = str(definition.get("target_qualname", ""))
            expected = (
                exported_class(qualified, self.export_nodes) or f"class:{qualified}"
            )
        return source if source.get("type_id") == expected else None

    def _conditional_class_candidates(self, source: dict[str, Any]) -> set[str]:
        origin = source.get("origin")
        if origin in {"Union", "Optional"}:
            candidates: set[str] = {"builtin:None"} if origin == "Optional" else set()
            for arg in source.get("type_args", []):
                candidates.update(self._conditional_class_candidates(arg))
            return candidates or {"unknown"}
        if origin is not None and origin != "None":
            return {"unknown"}
        type_id = str(source.get("type_id") or "unknown")
        if type_id.startswith("class:"):
            # The legacy annotation summary can select a unique short class
            # name. A new join requires the actual module/import identity.
            parsed = self.parse_expression(str(source.get("type_expression") or ""))
            name = self._name(parsed) if parsed is not None else None
            module = self.lexical.modules.get(self.file_record.path)
            found = (
                self.lexical.lookup(module, name.split(".")[0])
                if module and name
                else None
            )
            if not name or not found or not self._stable_conditional_binding(found):
                return {"unknown"}
            definition: dict[str, Any] = found[1][0]
            if definition.get("kind") not in {"class_definition", "import_alias"} or (
                (
                    exported_class(
                        self._resolve_alias(name, use_imports=True), self.export_nodes
                    )
                    or f"class:{self._resolve_alias(name, use_imports=True)}"
                )
                != type_id
            ):
                return {"unknown"}
        return {type_id}

    def _stable_conditional_binding(
        self, found: tuple[Node, list[dict[str, Any]], int | None]
    ) -> bool:
        owner, bindings, _ = found
        if not self.lexical.stable(*found):
            return False
        binding: dict[str, Any] = bindings[0]
        return owner.kind != "module" or any(
            (stmt.lineno, stmt.col_offset)
            == (binding.get("line"), binding.get("column"))
            for stmt in self.tree.body
        )

    @staticmethod
    def _excludes_none(test: ast.AST, name: str, truth: bool) -> bool:
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            return False
        left, right = test.left, test.comparators[0]
        if isinstance(left, ast.Constant) and left.value is None:
            left, right = right, left
        return (
            isinstance(left, ast.Name)
            and left.id == name
            and isinstance(right, ast.Constant)
            and right.value is None
            and (
                (truth and isinstance(test.ops[0], ast.IsNot))
                or (not truth and isinstance(test.ops[0], ast.Is))
            )
        )

    @staticmethod
    def _parameter_defaults(tree: ast.Module) -> dict[int, dict[str, str]]:
        result = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = [*node.args.posonlyargs, *node.args.args]
            defaults = {
                arg.arg: ast.unparse(value)
                for arg, value in zip(
                    args[len(args) - len(node.args.defaults) :], node.args.defaults
                )
            }
            defaults.update(
                {
                    arg.arg: ast.unparse(value)
                    for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults)
                    if value is not None
                }
            )
            result[node.lineno] = defaults
        return result

    @staticmethod
    def _subscript_value_type_source(source: dict[str, Any]) -> dict[str, Any] | None:
        wrapped = union_alternatives(source) is not None
        source = single_value_type(source)
        type_id = source.get("type_id")
        type_args = source.get("type_args")
        if not isinstance(type_args, list) or not type_args:
            return None
        if type_id == "builtin:dict" and len(type_args) >= 2:
            item = type_args[1]
        elif type_id == "builtin:tuple":
            item = tuple_element_type(source)
        elif type_id == "builtin:list":
            item = type_args[0]
        else:
            return None
        if not isinstance(item, dict):
            return None
        propagated = dict(item)
        propagated["strategy"] = "subscript_value"
        if wrapped or source.get("typed_value_evidence"):
            propagated["typed_value_evidence"] = True
        propagated["source_expression"] = str(source.get("source_expression") or "")
        if isinstance(source.get("type_ref_id"), str):
            propagated["type_ref_id"] = source["type_ref_id"]
        return propagated

    def _literal_value_type(self, node: ast.AST) -> _ResolvedType | None:
        type_name: str | None = None
        if isinstance(node, (ast.Dict, ast.DictComp)):
            type_name = "dict"
        elif isinstance(node, (ast.List, ast.ListComp)):
            type_name = "list"
        elif isinstance(node, (ast.Set, ast.SetComp)):
            type_name = "set"
        elif isinstance(node, ast.Tuple):
            type_name = "tuple"
        elif isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool):
                type_name = "bool"
            elif value is None:
                type_name = "None"
            elif isinstance(value, str):
                type_name = "str"
            elif isinstance(value, bytes):
                type_name = "bytes"
            elif isinstance(value, int):
                type_name = "int"
            elif isinstance(value, float):
                type_name = "float"
            elif isinstance(value, complex):
                type_name = "complex"
        if type_name is None:
            return None
        return self._resolve_named_type(type_name, use_imports=False)

    def looks_custom_type(self, expression: str) -> bool:
        parsed = self.parse_expression(expression)
        if parsed is None:
            return False
        name = self._primary_type_name(parsed)
        if not name:
            return False
        short = name.rsplit(".", 1)[-1]
        return short not in _BUILTIN_TYPES and short not in _TYPING_ORIGINS

    def _build_return_type_maps(self) -> None:
        for node in self.nodes:
            if node.kind not in {"function", "method"} or not node.qualname:
                continue
            returns = node.properties.get("returns")
            if not isinstance(returns, str) or not returns:
                continue
            resolved = self.resolve_annotation(
                returns,
                use_imports=node.path == self.file_record.path,
            )
            if (
                not resolved.type_id
                and union_alternatives(
                    self._value_type_record(
                        resolved,
                        strategy="return_annotation",
                        source_expression=returns,
                    )
                )
                is None
            ):
                continue
            if node.kind == "function":
                # A nested factory is not visible to unrelated lexical scopes.
                if not node.properties.get("lexical_parent"):
                    self.return_types_by_function_name.setdefault(node.name, []).append(
                        resolved
                    )
                self.return_types_by_function_qualname[node.qualname] = resolved
            elif node.kind == "method":
                class_qualname = node.qualname.rsplit(".", 1)[0]
                self.return_types_by_method[(class_qualname, node.name)] = resolved
                if self._is_property_method(node):
                    self.property_return_types_by_method[
                        (class_qualname, node.name)
                    ] = resolved

    def typing_only_attribute(
        self, scope: Node, name: str, local_types: dict[str, dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Join only the constructor's explicit optional-injection pattern.

        Recovering a typing-only annotation must not make a mixed or later
        overwritten instance attribute look like that parameter's class.
        """
        inputs = [
            b
            for b in scope.properties.get("bindings", [])
            if b.get("name") == name
            and str(b.get("value")) in local_types
            and any(
                member.get("typing_only_annotation")
                for member in (union_alternatives(local_types[str(b["value"])]) or [])
            )
        ]
        if not inputs:
            return None
        unknown = unknown_union_result(name)
        if scope.kind != "method" or scope.name != "__init__" or len(inputs) != 1:
            return unknown
        receiver = self.lexical.lookup(scope, "self")
        if (
            not receiver
            or not self.lexical.stable(*receiver)
            or receiver[1][0].get("kind") != "parameter"
        ):
            return unknown
        parameter = str(inputs[0]["value"])
        source = local_types[parameter]
        if self.parameter_defaults.get(scope.start_line, {}).get(parameter) not in {
            None,
            "None",
        }:
            return unknown
        expected = source.get("type_id")
        found = self.lexical.lookup(scope, parameter)
        if (
            not found
            or not self.lexical.stable(*found)
            or found[1][0].get("kind") != "parameter"
        ):
            return unknown
        function = next(
            (
                n
                for n in ast.walk(self.tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.lineno == scope.start_line
            ),
            None,
        )
        owner = next(
            (
                n
                for n in ast.walk(self.tree)
                if isinstance(n, ast.ClassDef) and function in n.body
            ),
            None,
        )
        if owner is None or function is None:
            return unknown
        writes = [
            n
            for n in ast.walk(owner)
            if isinstance(n, ast.Attribute)
            and isinstance(n.ctx, (ast.Store, ast.Del))
            and self._unparse(n) == name
        ]
        if len(writes) != 2:
            return unknown
        for branch in function.body:
            if not isinstance(branch, ast.If) or not branch.body or not branch.orelse:
                continue
            arms = [branch.body[-1], branch.orelse[-1]]
            if not all(
                isinstance(a, ast.Assign)
                and len(a.targets) == 1
                and a.targets[0] in writes
                for a in arms
            ):
                continue
            for truth, arm, fallback in (
                (True, arms[0], arms[1]),
                (False, arms[1], arms[0]),
            ):
                if not (
                    isinstance(arm.value, ast.Name)
                    and arm.value.id == parameter
                    and self._excludes_none(branch.test, parameter, truth)
                ):
                    continue
                if not isinstance(fallback.value, ast.Call):
                    return unknown
                constructor = self._name(fallback.value.func)
                root = constructor.split(".")[0] if constructor else ""
                imported = self.lexical.lookup(scope, root)
                if not imported or not self._stable_conditional_binding(imported):
                    return unknown
                definition = imported[1][0]
                if (
                    imported[0].id == scope.id
                    and definition.get("line", 0) >= branch.lineno
                ):
                    return unknown
                if definition.get("kind") == "import_alias":
                    qualified = (
                        str(definition.get("target_qualname", ""))
                        + constructor[len(root) :]
                    )
                    actual = (
                        exported_class(qualified, self.export_nodes)
                        or f"class:{qualified}"
                    )
                elif (
                    definition.get("kind") == "class_definition" and constructor == root
                ):
                    actual = definition.get("target")
                else:
                    return unknown
                if actual == expected:
                    return {
                        "type_id": expected,
                        "symbol_id": expected,
                        "type_expression": str(expected).removeprefix("class:"),
                        "strategy": "typing_only_injection_join",
                    }
        return unknown

    def register_class_instance_attr(
        self,
        scope_node: Node,
        name: str,
        type_ref: dict[str, Any],
    ) -> None:
        if not isinstance(type_ref.get("type_id"), str):
            return
        class_qualname: str | None = None
        attr_name: str | None = None
        if scope_node.kind == "class" and scope_node.qualname:
            if "." in name or name == "return":
                return
            class_qualname = scope_node.qualname
            attr_name = name
        elif (
            scope_node.kind == "method"
            and scope_node.qualname
            and name.startswith("self.")
        ):
            class_qualname = scope_node.qualname.rsplit(".", 1)[0]
            attr_name = name.removeprefix("self.")
        if not class_qualname or not attr_name:
            return
        self.class_instance_attrs.setdefault(class_qualname, {}).setdefault(
            attr_name,
            type_ref,
        )

    def context_manager_enter_source(
        self,
        source: dict[str, Any],
        *,
        is_async: bool,
    ) -> dict[str, Any] | None:
        type_id = source.get("type_id")
        if not isinstance(type_id, str) or not type_id.startswith("class:"):
            return None
        method_name = "__aenter__" if is_async else "__enter__"
        class_qualname = type_id.removeprefix("class:")
        resolved = self.return_types_by_method.get((class_qualname, method_name))
        if resolved is None:
            return None
        enter_source = self._value_type_record(
            resolved,
            strategy=(
                "async_context_manager_enter" if is_async else "context_manager_enter"
            ),
            source_expression=str(
                source.get("source_expression") or source.get("type_expression") or ""
            ),
        )
        if isinstance(source.get("type_ref_id"), str):
            enter_source["type_ref_id"] = source["type_ref_id"]
        return enter_source

    def _resolve_call(
        self,
        node: ast.Call,
        local_types: dict[str, dict[str, Any]],
        *,
        scope_node: Node | None = None,
    ) -> dict[str, Any] | None:
        cast_type = self._resolve_cast_call(node)
        if cast_type is not None:
            return self._value_type_record(
                cast_type,
                strategy="cast",
                source_expression=self._unparse(node),
            )

        func_name = self._name(node.func)
        if func_name:
            constructed = self._resolve_named_type(func_name, use_imports=True)
            if scope_node is not None and scope_node.id in self.lexical.strict:
                root = self.lexical.root_name(func_name)
                found = self.lexical.lookup(scope_node, root) if root else None
                if found is not None:
                    binding = found[1][0]
                    kind = binding.get("kind")
                    if (
                        not self.lexical.stable(*found)
                        or kind not in {"class_definition", "import_alias"}
                        or (
                            kind == "class_definition"
                            and binding.get("target") != constructed.type_id
                        )
                    ):
                        constructed = _ResolvedType(expression=func_name)
            if (
                constructed.type_id == "extsym:tempfile.TemporaryDirectory"
                and scope_node is not None
            ):
                root = self.lexical.root_name(func_name)
                found = self.lexical.lookup(scope_node, root) if root else None
                if (
                    not found
                    or not self._stable_conditional_binding(found)
                    or found[1][0].get("kind") != "import_alias"
                ):
                    constructed = _ResolvedType(expression=func_name)
            if constructed.type_id and (
                constructed.type_id.startswith("class:")
                or constructed.type_id.startswith("builtin:")
                or constructed.type_id in _EXTERNAL_CONSTRUCTOR_TYPES
            ):
                return self._value_type_record(
                    constructed,
                    strategy="constructor",
                    source_expression=self._unparse(node),
                )

        if isinstance(node.func, ast.Attribute):
            receiver = (
                self.resolve_scoped_value(node.func.value, scope_node, local_types)
                if scope_node is not None
                else self.resolve_value(node.func.value, local_types)
            )
            if receiver is None:
                receiver = self._class_reference_value(node.func.value)
            if receiver is not None:
                if union_alternatives(receiver) is not None and not receiver.get(
                    "type_id"
                ):
                    return unknown_union_result(self._unparse(node))
                type_id = receiver.get("type_id")
                builtin_return = self._builtin_method_return_type(
                    node,
                    receiver,
                    local_types,
                    scope_node=scope_node,
                )
                if builtin_return is not None:
                    return self._value_type_record(
                        builtin_return,
                        strategy="builtin_method_return",
                        source_expression=self._unparse(node),
                    )
                external_return = self._external_method_return_type(
                    receiver, node.func.attr
                )
                if external_return is not None:
                    return self._value_type_record(
                        external_return,
                        strategy="external_method_return",
                        source_expression=self._unparse(node),
                    )
                if (
                    receiver.get("strategy") == "class_reference"
                    and node.func.attr in _PYDANTIC_INSTANCE_CLASSMETHOD_STRATEGIES
                    and isinstance(type_id, str)
                    and type_id.startswith("class:")
                ):
                    class_qualname = type_id.removeprefix("class:")
                    if self._is_pydantic_model_class(class_qualname):
                        return self._value_type_record(
                            _ResolvedType(
                                expression=class_qualname.rsplit(".", 1)[-1],
                                type_id=type_id,
                                symbol_id=type_id,
                                status="resolved",
                            ),
                            strategy=_PYDANTIC_INSTANCE_CLASSMETHOD_STRATEGIES[
                                node.func.attr
                            ],
                            source_expression=self._unparse(node),
                        )
                if isinstance(type_id, str) and type_id.startswith("class:"):
                    class_qualname = type_id.removeprefix("class:")
                    resolved = self.return_types_by_method.get(
                        (class_qualname, node.func.attr)
                    )
                    if resolved is not None:
                        return self._value_type_record(
                            resolved,
                            strategy="provider_return_annotation",
                            source_expression=self._unparse(node),
                        )
            return None

        if func_name:
            resolved = self._function_return_type(func_name, scope_node)
            if resolved is not None:
                return self._value_type_record(
                    resolved,
                    strategy="factory_return_annotation",
                    source_expression=self._unparse(node),
                )
        return None

    def _external_method_return_type(
        self,
        receiver: dict[str, Any],
        method_name: str,
    ) -> _ResolvedType | None:
        """Resolve a documented factory method on a known external type."""

        owner = self._external_receiver_qualname(receiver)
        if owner is None:
            return None
        returned = EXTERNAL_METHOD_RETURN_TYPES.get((owner, method_name))
        if returned is None:
            return None
        type_id = f"extsym:{returned}"
        return _ResolvedType(
            expression=returned.rsplit(".", 1)[-1],
            type_id=type_id,
            symbol_id=type_id,
            status="resolved",
        )

    def _external_receiver_qualname(self, receiver: dict[str, Any]) -> str | None:
        """Name the external type a receiver is, or inherits from in this file."""

        type_id = receiver.get("type_id")
        if not isinstance(type_id, str):
            return None
        if type_id.startswith("extsym:"):
            return type_id.removeprefix("extsym:")
        if type_id.startswith("class:"):
            return self._external_base_qualname(type_id.removeprefix("class:"))
        return None

    def _external_base_qualname(self, class_qualname: str) -> str | None:
        node = self.classes_by_id.get(class_id(class_qualname))
        if node is None or node.path != self.file_record.path:
            return None
        bases = node.properties.get("bases", [])
        if not isinstance(bases, list):
            return None
        for base in bases:
            if not isinstance(base, str):
                continue
            simple_base = base.strip("\"'").split("[", 1)[0].strip()
            if not simple_base:
                continue
            if simple_base in EXTERNAL_METHOD_RETURN_OWNERS:
                return simple_base
            resolved = self._resolve_alias(simple_base, use_imports=True)
            if resolved in EXTERNAL_METHOD_RETURN_OWNERS:
                return resolved
        return None

    def _is_pydantic_model_class(self, class_qualname: str) -> bool:
        node = self.classes_by_id.get(class_id(class_qualname))
        if node is None or node.path != self.file_record.path:
            return False
        bases = node.properties.get("bases", [])
        if not isinstance(bases, list):
            return False
        for base in bases:
            if not isinstance(base, str):
                continue
            simple_base = base.strip("\"'").split("[", 1)[0].strip()
            if not simple_base:
                continue
            resolved = self._resolve_alias(simple_base, use_imports=True)
            if simple_base in _PYDANTIC_MODEL_BASES:
                return True
            if resolved in _PYDANTIC_MODEL_BASES:
                return True
        return False

    def _resolve_cast_call(self, node: ast.Call) -> _ResolvedType | None:
        """Resolve ``typing.cast(T, value)`` as an explicit static type source."""
        if len(node.args) < 2 or not self._is_typing_cast(node.func):
            return None
        expression = self._unparse(node.args[0])
        resolved = self._resolve_type_node(
            node.args[0],
            expression,
            use_imports=True,
        )
        if not resolved.type_id:
            return None
        return resolved

    def _is_typing_cast(self, func: ast.AST) -> bool:
        name = self._name(func)
        if not name:
            return False
        return self._resolve_alias(name, use_imports=True) == "typing.cast"

    def _builtin_method_return_type(
        self,
        node: ast.Call,
        receiver: dict[str, Any],
        local_types: dict[str, dict[str, Any]],
        *,
        scope_node: Node | None,
    ) -> _ResolvedType | None:
        if not isinstance(node.func, ast.Attribute):
            return None
        type_id = receiver.get("type_id")
        method_name = node.func.attr
        if type_id != "builtin:dict" or method_name not in {"get", "setdefault"}:
            return None

        default = None
        if len(node.args) >= 2:
            default = node.args[1]
        else:
            for keyword in node.keywords:
                if keyword.arg == "default":
                    default = keyword.value
                    break
        if default is not None:
            default_source = (
                self.resolve_scoped_value(default, scope_node, local_types)
                if scope_node is not None
                else self.resolve_value(default, local_types)
            )
            if default_source is not None and isinstance(
                default_source.get("type_id"), str
            ):
                return self._resolved_type_from_record(default_source)

        type_args = receiver.get("type_args")
        if isinstance(type_args, list) and len(type_args) >= 2:
            value_type = type_args[1]
            if isinstance(value_type, dict) and isinstance(
                value_type.get("type_id"), str
            ):
                return self._resolved_type_from_record(value_type)
        return None

    @staticmethod
    def _resolved_type_from_record(record: dict[str, Any]) -> _ResolvedType:
        return _ResolvedType(
            expression=str(record.get("type_expression") or record.get("type_id")),
            type_id=(
                record.get("type_id")
                if isinstance(record.get("type_id"), str)
                else None
            ),
            symbol_id=(
                record.get("symbol_id")
                if isinstance(record.get("symbol_id"), str)
                else (
                    record.get("type_id")
                    if isinstance(record.get("type_id"), str)
                    else None
                )
            ),
            origin=(
                record.get("origin") if isinstance(record.get("origin"), str) else None
            ),
            type_args=(
                tuple(
                    item
                    for item in record.get("type_args", [])
                    if isinstance(item, dict)
                )
                if isinstance(record.get("type_args"), list)
                else ()
            ),
            status=(
                str(record.get("resolution_status"))
                if isinstance(record.get("resolution_status"), str)
                else "resolved"
            ),
        )

    def _class_reference_value(self, node: ast.AST) -> dict[str, Any] | None:
        name = self._name(node)
        if not name:
            return None
        resolved = self._resolve_named_type(name, use_imports=True)
        if not resolved.type_id or not resolved.type_id.startswith("class:"):
            return None
        return self._value_type_record(
            resolved,
            strategy="class_reference",
            source_expression=self._unparse(node),
        )

    def _function_return_type(
        self, name: str, scope_node: Node | None = None
    ) -> _ResolvedType | None:
        if scope_node is not None and scope_node.id in self.lexical.strict:
            root = self.lexical.root_name(name)
            found = self.lexical.lookup(scope_node, root) if root else None
            if found is not None:
                if not self.lexical.stable(*found):
                    return None
                binding = found[1][0]
                if binding.get("kind") == "function_definition":
                    return self.return_types_by_function_qualname.get(
                        str(binding.get("target_qualname"))
                    )
                if binding.get("kind") != "import_alias":
                    return None
        resolved_name = self._resolve_alias(name, use_imports=True)
        exact = self.return_types_by_function_qualname.get(resolved_name)
        if exact is not None:
            return exact
        if "." in name:
            return None
        candidates = self.return_types_by_function_name.get(name, [])
        if len(candidates) == 1:
            return candidates[0]
        return None

    @staticmethod
    def _class_qualname_for_scope(node: Node) -> str | None:
        if node.kind == "class":
            return node.qualname
        if node.kind == "method" and node.qualname:
            return node.qualname.rsplit(".", 1)[0]
        return None

    def _property_type_ref(
        self,
        class_qualname: str,
        attr_name: str,
        *,
        source_expression: str,
    ) -> dict[str, Any] | None:
        resolved = self.property_return_types_by_method.get((class_qualname, attr_name))
        if resolved is None:
            return None
        return self._value_type_record(
            resolved,
            strategy="return_annotation",
            source_expression=source_expression,
        )

    @staticmethod
    def _is_property_method(node: Node) -> bool:
        decorators = node.properties.get("decorators", [])
        if not isinstance(decorators, list):
            return False
        for decorator in decorators:
            if not isinstance(decorator, str):
                continue
            name = decorator.split("(", 1)[0].strip().rsplit(".", 1)[-1]
            if name in {"property", "cached_property"}:
                return True
        return False

    def _value_type_record(
        self,
        resolved: _ResolvedType,
        *,
        strategy: str,
        source_expression: str,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "strategy": strategy,
            "type_expression": resolved.expression,
            "source_expression": source_expression,
            "resolution_status": resolved.status,
        }
        if resolved.type_id:
            record["type_id"] = resolved.type_id
        if resolved.symbol_id:
            record["symbol_id"] = resolved.symbol_id
        if resolved.origin:
            record["origin"] = resolved.origin
        if resolved.type_args:
            record["type_args"] = list(resolved.type_args)
        return record

    def _resolve_type_node(
        self,
        node: ast.AST,
        expression: str,
        *,
        use_imports: bool,
    ) -> _ResolvedType:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self.resolve_annotation(node.value, use_imports=use_imports)
        if isinstance(node, ast.Constant) and node.value is None:
            return self._resolve_named_type("None", use_imports=use_imports)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            args = [
                self._type_arg(node.left, use_imports=use_imports),
                self._type_arg(node.right, use_imports=use_imports),
            ]
            args = self._validated_union_args(args, use_imports=use_imports)
            primary = self._single_union_arg(args)
            return _ResolvedType(
                expression=expression,
                type_id=primary.get("type_id") if primary else None,
                symbol_id=primary.get("symbol_id") if primary else None,
                origin="Union",
                type_args=tuple(args),
                status="resolved" if primary else "unresolved",
            )
        if isinstance(node, ast.Subscript):
            return self._resolve_subscript(node, expression, use_imports=use_imports)
        name = self._name(node)
        if name:
            return self._resolve_named_type(name, use_imports=use_imports)
        return _ResolvedType(expression=expression)

    def _resolve_subscript(
        self,
        node: ast.Subscript,
        expression: str,
        *,
        use_imports: bool,
    ) -> _ResolvedType:
        origin = self._name(node.value) or self._unparse(node.value)
        resolved_origin = self._resolve_alias(origin, use_imports=use_imports)
        short_origin = (
            resolved_origin
            if resolved_origin.startswith(("typing.", "typing_extensions."))
            else origin
        ).rsplit(".", 1)[-1]
        args = tuple(self._slice_type_args(node.slice, use_imports=use_imports))

        if short_origin in _TRANSPARENT_ORIGINS:
            if short_origin in {"Union", "Optional"}:
                module = self.lexical.modules.get(self.file_record.path)
                found = (
                    self.lexical.lookup(module, origin.split(".")[0])
                    if module
                    else None
                )
                definition: dict[str, Any] = found[1][0] if found else {}
                if (
                    use_imports
                    and found
                    and (
                        not self._stable_conditional_binding(found)
                        or definition.get("kind") != "import_alias"
                        or not resolved_origin.startswith(
                            ("typing.", "typing_extensions.")
                        )
                    )
                ):
                    args = (
                        {
                            "type_expression": expression,
                            "resolution_status": "unresolved",
                        },
                    )
                args = tuple(
                    self._validated_union_args(list(args), use_imports=use_imports)
                )
                primary = self._single_union_arg(list(args))
            else:
                primary = args[0] if args and args[0].get("type_id") else None
            return _ResolvedType(
                expression=expression,
                type_id=primary.get("type_id") if primary else None,
                symbol_id=primary.get("symbol_id") if primary else None,
                origin=short_origin,
                type_args=args,
                status="resolved" if primary else "unresolved",
            )

        origin_type = self._resolve_named_type(origin, use_imports=use_imports)
        return _ResolvedType(
            expression=expression,
            type_id=origin_type.type_id,
            symbol_id=origin_type.symbol_id,
            origin=short_origin,
            type_args=args,
            status="resolved" if origin_type.type_id else "unresolved",
        )

    def _slice_type_args(
        self, node: ast.AST, *, use_imports: bool
    ) -> list[dict[str, Any]]:
        if isinstance(node, ast.Tuple):
            return [self._type_arg(item, use_imports=use_imports) for item in node.elts]
        return [self._type_arg(node, use_imports=use_imports)]

    def _type_arg(self, node: ast.AST, *, use_imports: bool) -> dict[str, Any]:
        if isinstance(node, ast.Constant) and node.value is Ellipsis:
            return {"type_expression": "...", "resolution_status": "unresolved"}
        expression = self._unparse(node)
        resolved = self._resolve_type_node(node, expression, use_imports=use_imports)
        record = {
            "type_expression": resolved.expression,
            "resolution_status": resolved.status,
        }
        if resolved.type_id:
            record["type_id"] = resolved.type_id
        if resolved.symbol_id:
            record["symbol_id"] = resolved.symbol_id
        if resolved.origin:
            record["origin"] = resolved.origin
        if resolved.type_args:
            record["type_args"] = list(resolved.type_args)
        return record

    def _validated_union_args(
        self, args: list[dict[str, Any]], *, use_imports: bool
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for arg in args:
            if (
                use_imports
                and arg.get("origin") is None
                and str(arg.get("type_id", "")).startswith("class:")
                and self._conditional_class_candidates(arg) == {"unknown"}
            ):
                if self._type_checking_annotation(arg):
                    arg = {**arg, "typing_only_annotation": True}
                else:
                    arg = {
                        k: v
                        for k, v in arg.items()
                        if k not in {"type_id", "symbol_id"}
                    }
                    arg["resolution_status"] = "unresolved"
            result.append(arg)
        return result

    def _type_checking_annotation(self, ref: dict[str, Any]) -> bool:
        """Accept an unambiguous typing-only import as annotation evidence.

        This exception is deliberately outside lexical runtime lookup and the
        conditional-value join: a typing-only import does not bind a runtime
        constructor. Only direct imports in a proven top-level typing guard
        qualify; rebinding, alternate arms and nested conditions stay unknown.
        """
        expression = self.parse_expression(str(ref.get("type_expression", "")))
        name = self._name(expression) if expression is not None else None
        module = self.lexical.modules.get(self.file_record.path)
        if not name or module is None:
            return False
        bindings = self.lexical.bindings.get(module.id, {}).get(name.split(".")[0], [])
        if len(bindings) != 1:
            return False
        binding = bindings[0]
        if binding.get("kind") != "type_checking_import_alias":
            return False
        target = self._resolve_alias(name, use_imports=True)
        if (exported_class(target, self.export_nodes) or f"class:{target}") != ref.get(
            "type_id"
        ):
            return False
        for stmt in self.tree.body:
            if not isinstance(stmt, ast.If) or stmt.orelse:
                continue
            guard_name = self._name(stmt.test)
            if (
                not guard_name
                or self._resolve_alias(guard_name, use_imports=True)
                != "typing.TYPE_CHECKING"
            ):
                continue
            root = guard_name.split(".")[0]
            guard = self.lexical.lookup(module, root)
            if not guard or not self._stable_conditional_binding(guard):
                continue
            if any(
                isinstance(item, (ast.Import, ast.ImportFrom))
                and (item.lineno, item.col_offset)
                == (binding.get("line"), binding.get("column"))
                for item in stmt.body
            ):
                return True
        return False

    @staticmethod
    def _single_union_arg(args: list[dict[str, Any]]) -> dict[str, Any] | None:
        members = union_alternatives({"origin": "Union", "type_args": args}) or []
        non_none = [arg for arg in members if arg.get("type_id") != "builtin:None"]
        if not non_none or any(not arg.get("type_id") for arg in non_none):
            return None
        first = non_none[0]
        return (
            first
            if all(type_identity(arg) == type_identity(first) for arg in non_none)
            else None
        )

    def _resolve_named_type(self, name: str, *, use_imports: bool) -> _ResolvedType:
        cleaned = name.strip("\"'")
        short = cleaned.rsplit(".", 1)[-1]
        if short in _NONE_NAMES:
            type_id = "builtin:None"
            return _ResolvedType(
                expression=cleaned,
                type_id=type_id,
                symbol_id=type_id,
                origin="None",
                status="resolved",
            )
        if short in _BUILTIN_TYPES:
            type_id = f"builtin:{short}"
            return _ResolvedType(
                expression=cleaned,
                type_id=type_id,
                symbol_id=type_id,
                origin=short,
                status="resolved",
            )
        if short in _TYPING_ORIGINS or cleaned.startswith("typing."):
            type_id = f"typing:{short}"
            return _ResolvedType(
                expression=cleaned,
                type_id=type_id,
                symbol_id=type_id,
                origin=short,
                status="resolved",
            )

        resolved = self._resolve_alias(cleaned, use_imports=use_imports)
        candidate_id = class_id(resolved)
        if candidate_id not in self.classes_by_id and use_imports:
            candidate_id = exported_class(resolved, self.export_nodes) or candidate_id
        if candidate_id in self.classes_by_id:
            return _ResolvedType(
                expression=cleaned,
                type_id=candidate_id,
                symbol_id=candidate_id,
                status="resolved",
            )

        matches = self.classes_by_name.get(short, [])
        # An explicit import identifies its provider. A missing member must
        # not bind to an unrelated class just because its short name matches.
        explicit_import = use_imports and cleaned.split(".", 1)[0] in self.aliases
        if len(matches) == 1 and not explicit_import:
            match = matches[0]
            return _ResolvedType(
                expression=cleaned,
                type_id=match.id,
                symbol_id=match.id,
                status="resolved",
            )

        if use_imports and self._is_external_alias(cleaned):
            type_id = f"extsym:{resolved}"
            return _ResolvedType(
                expression=cleaned,
                type_id=type_id,
                symbol_id=type_id,
                status="resolved",
            )
        return _ResolvedType(expression=cleaned)

    def _resolve_alias(self, name: str, *, use_imports: bool) -> str:
        if not use_imports:
            return name
        parts = name.split(".")
        if parts[0] in self.aliases:
            return ".".join([self.aliases[parts[0]], *parts[1:]])
        if "." in name:
            return name
        return f"{self.file_record.module}.{name}"

    def _is_external_alias(self, name: str) -> bool:
        parts = name.split(".")
        if parts[0] not in self.aliases:
            return False
        resolved = self.aliases[parts[0]]
        return not any(
            resolved == module or resolved.startswith(f"{module}.")
            for module in self.module_names
        )

    def _import_aliases(self, body: list[ast.stmt]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for stmt in body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    local = alias.asname or alias.name.split(".", 1)[0]
                    aliases[local] = alias.name
            elif isinstance(stmt, ast.ImportFrom):
                base = ImportAnalyzer._resolve_import_from_base(self.file_record, stmt)
                if not base:
                    continue
                for alias in stmt.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    aliases[local] = f"{base}.{alias.name}"
            elif isinstance(stmt, ast.If) and self._is_type_checking_guard(stmt.test):
                aliases.update(self._import_aliases(stmt.body))
        return aliases

    @staticmethod
    def _is_type_checking_guard(test: ast.AST) -> bool:
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING"
        return False

    def _primary_type_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Subscript):
            short_origin = (self._name(node.value) or "").rsplit(".", 1)[-1]
            if short_origin in _TRANSPARENT_ORIGINS:
                for item in self._slice_nodes(node.slice):
                    name = self._primary_type_name(item)
                    if name and name.rsplit(".", 1)[-1] not in _NONE_NAMES:
                        return name
            return self._name(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._primary_type_name(node.left) or self._primary_type_name(
                node.right
            )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parsed = self.parse_expression(node.value)
            return self._primary_type_name(parsed) if parsed is not None else None
        return self._name(node)

    @staticmethod
    def _slice_nodes(node: ast.AST) -> list[ast.AST]:
        return list(node.elts) if isinstance(node, ast.Tuple) else [node]

    def _name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    @staticmethod
    def _unparse(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return node.__class__.__name__
