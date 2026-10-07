"""Call edge analyzer with an opt-in receiver-aware resolver."""

from __future__ import annotations

import ast
import builtins
import sys
from collections import defaultdict
from typing import Any

from arcgraph.analyzers.exports import exported_class
from arcgraph.analyzers.calls.constants import (
    ARGPARSE_METHODS,
    BUILTIN_CALLS,
    BUILTIN_METHODS_BY_TYPE,
    BUILTIN_TYPE_ATTRIBUTE_METHODS,
    COMMON_BOUNDARY_METHOD_TARGETS,
    DB_METHOD_OWNERS,
    EXTERNAL_TARGET_KINDS,
    PEP249_METHODS_BY_CLASS,
    GUESSED_METHODS_BY_OWNER,
    MOCK_ASSERT_METHOD_OWNERS,
    NETWORKX_GRAPH_METHOD_OWNERS,
    PROMETHEUS_METRIC_METHOD_OWNERS,
    SQLALCHEMY_STATEMENT_METHOD_OWNERS,
    ARGPARSE_METHOD_OWNERS,
    COMMON_MAPPING_RECEIVER_NAMES,
    COMMON_MAPPING_RECEIVER_SUFFIXES,
    COMMON_SEQUENCE_RECEIVER_NAMES,
    COMMON_SEQUENCE_RECEIVER_SUFFIXES,
    COMMON_SET_RECEIVER_NAMES,
    COMMON_SET_RECEIVER_SUFFIXES,
    COMMON_TEXT_RECEIVER_NAMES,
    COMMON_TEXT_RECEIVER_SUFFIXES,
    CONFIG_ATTRIBUTE_CALLS,
    CONFIG_CALLS,
    DB_CONNECTION_METHODS,
    DECLARATION_CALLS,
    DEFINITION_TIME_CONTEXTS,
    DYNAMIC_CALLS,
    FASTAPI_ROUTE_METHODS,
    HTTP_CLIENT_METHODS,
    LOGGER_METHODS,
    MOCK_ASSERT_METHODS,
    PROMETHEUS_METRIC_METHODS,
    PROMETHEUS_METRIC_NAME_HINTS,
    REDIS_METHODS,
    SOURCE_KINDS,
    SQLALCHEMY_RESULT_METHODS,
    SQLALCHEMY_SESSION_METHODS,
    SQLALCHEMY_STATEMENT_METHODS,
    TARGET_KINDS,
    _ARGPARSE_UNIQUE_METHODS,
    _CLICK_GROUP_METHODS,
    _FASTAPI_APP_METHODS,
    _MONKEYPATCH_METHODS,
    _NETWORKX_GRAPH_METHODS,
    _RE_MATCH_METHODS,
    _TYPER_APP_METHODS,
)
from arcgraph.analyzers.calls.context import (
    CallAnalysis,
    _CallResolutionContext,
    _ResolvedCallTarget,
    binding_in_effect,
)
from arcgraph.analyzers.calls.lexical import LexicalScopes, import_binding_target
from arcgraph.analyzers.stdlib_functions import CAPITALISED_STDLIB_FUNCTIONS
from arcgraph.analyzers.external_types import (
    EXTERNAL_METHODS_BY_TYPE,
    GENERATOR_TYPE_ID,
    LOWERCASE_STDLIB_CLASSES,
    NEVER_TYPE_ID,
    PATH_TYPE_IDS,
    function_return_type,
    literal_type_name,
    mapping_default,
    mapping_value_type,
    may_be_path_segment,
    sliced_value_type,
    method_return_type,
    type_id_of_qualname,
)
from arcgraph.analyzers.type_unions import (
    single_value_type,
    tuple_element_type,
    union_alternatives,
    unknown_union_result,
)
from arcgraph.core.ids import (
    callsite_id,
    stable_callsite_subject,
    stable_callsite_subject_key,
)
from arcgraph.core.schemas import Edge, Evidence, Node

# Type ids that name no type: a receiver annotated Any is of unknown type.
_UNKNOWN_TYPE_IDS = frozenset({"extsym:typing.Any", "typing:Any"})
# Strategies that pick a callee by its name alone, without a binding or a type.
_NAME_GUESS_STRATEGIES = frozenset(
    {
        "common_boundary_method",
        "receiver_name_boundary_method",
        "unique_method_fallback",
        "unique_short_name_fallback",
    }
)


_IMPORT_BINDING_KINDS = frozenset({"import_alias", "type_checking_import_alias"})
# Module bindings that bind no value: a name listed in __all__, a global
# declaration.
_DECLARATION_KINDS = frozenset({"re_export", "global", "nonlocal"})
# What a module name holds, beside ("def", node id) and ("import", qualname).
_NO_VALUE: tuple[str, str | None] = ("unbound", None)
_OTHER_VALUE: tuple[str, str | None] = ("other", None)


class CallAnalyzer:
    """Infer call-like edges from AST callsite metadata.

    The default mode preserves the legacy AST fallback behavior. V2.3 receiver
    resolution is opt-in until query profiles and golden fixtures are updated.
    """

    def __init__(self, *, enable_v2: bool = False) -> None:
        self.enable_v2 = enable_v2

    def analyze(
        self, nodes: list[Node], source_nodes: list[Node] | None = None
    ) -> CallAnalysis:
        context = _CallResolutionContext(nodes)
        source_callsite_nodes = [
            node for node in (source_nodes or nodes) if node.kind in SOURCE_KINDS
        ]

        generated_nodes: dict[str, Node] = {}
        edges: list[Edge] = []
        for source in source_callsite_nodes:
            callsites = source.properties.get("callsites", [])
            if not isinstance(callsites, list):
                continue
            occurrence_by_subject: dict[tuple[str, ...], int] = {}
            occurrence_by_callsite_id: dict[str, int] = {}

            for callsite in callsites:
                if not isinstance(callsite, dict):
                    continue
                identified_callsite = self._with_stable_callsite_identity(
                    source,
                    callsite,
                    occurrence_by_subject=occurrence_by_subject,
                    occurrence_by_callsite_id=occurrence_by_callsite_id,
                )
                context.callsite_position = (
                    source.id,
                    int(callsite.get("line") or 0),
                    int(callsite.get("column") or 0),
                )
                resolved = self._resolve_callsite(source, identified_callsite, context)
                if resolved is None and self.enable_v2:
                    resolved = self._semantic_callsite_target(
                        source, identified_callsite, context
                    )
                if resolved is None:
                    continue

                raw_name = str(identified_callsite.get("name"))
                edge_kind = self._edge_kind(source, identified_callsite, resolved)
                if resolved.target.id.startswith(
                    (
                        "config:",
                        "decl:",
                        "extsym:",
                        "local:",
                        "protocol:",
                        "unresolved:",
                    )
                ):
                    generated_nodes.setdefault(resolved.target.id, resolved.target)
                edges.append(
                    Edge(
                        source=source.id,
                        target=resolved.target.id,
                        kind=edge_kind,
                        confidence=resolved.confidence,
                        resolution={
                            "status": resolved.resolution_status,
                            "strategy": resolved.strategy,
                            "candidate_count": resolved.candidate_count,
                            "callsite_id": self._callsite_id(
                                source, identified_callsite, raw_name
                            ),
                            "fallbacks": self._fallbacks(resolved.strategy),
                        },
                        evidence=[
                            Evidence(
                                kind=self._evidence_kind(source, callsite, edge_kind),
                                path=source.path,
                                start_line=self._int_or_none(
                                    identified_callsite.get("line")
                                ),
                                column=self._int_or_none(
                                    identified_callsite.get("column")
                                ),
                                detail=raw_name,
                            )
                        ],
                        properties={
                            "callsite": self._callsite_properties(
                                source, identified_callsite, resolved, edge_kind
                            )
                        },
                    )
                )

        # --- Emit 'extends' edges for class inheritance hierarchy ---
        class_nodes = [node for node in (source_nodes or nodes) if node.kind == "class"]
        for class_node in class_nodes:
            bases = class_node.properties.get("bases", [])
            if not isinstance(bases, list):
                continue
            for base_name in bases:
                if not isinstance(base_name, str) or not base_name:
                    continue
                result = self._resolve_extends_target(class_node, base_name, context)
                if result is None:
                    continue
                extends_target, extends_confidence = result
                if extends_target.id.startswith("extsym:"):
                    generated_nodes.setdefault(extends_target.id, extends_target)
                edges.append(
                    Edge(
                        source=class_node.id,
                        target=extends_target.id,
                        kind="extends",
                        confidence=extends_confidence,
                        evidence=[
                            Evidence(
                                kind="ast_class_base",
                                path=class_node.path,
                                start_line=class_node.start_line,
                                detail=base_name,
                            )
                        ],
                    )
                )

        # --- Emit 'implements' overlay edges ---
        # Only concrete classes extending user-defined abstract bases get
        # an additional ``implements`` edge.  Direct subclasses of
        # ``typing.Protocol`` / ``abc.ABC`` are *declaring* an abstraction,
        # not implementing one.
        all_node_ids = {n.id for n in nodes}
        all_node_ids.update(generated_nodes)
        nodes_by_id: dict[str, Node] = {n.id: n for n in nodes}
        nodes_by_id.update(generated_nodes)

        for edge in list(edges):
            if edge.kind != "extends":
                continue
            source_node = nodes_by_id.get(edge.source)
            target_node = nodes_by_id.get(edge.target)
            if source_node is None or target_node is None:
                continue
            # Source must be a concrete class (no abstract_kind)
            if source_node.properties.get("abstract_kind"):
                continue
            # Target must be a user-defined abstract class (has abstract_kind)
            target_abstract = target_node.properties.get("abstract_kind")
            if not target_abstract:
                continue
            evidence_kind = (
                "ast_protocol_impl" if target_abstract == "protocol" else "ast_abc_impl"
            )
            edges.append(
                Edge(
                    source=edge.source,
                    target=edge.target,
                    kind="implements",
                    confidence=edge.confidence,
                    evidence=[
                        Evidence(
                            kind=evidence_kind,
                            path=source_node.path,
                            start_line=source_node.start_line,
                            detail=f"implements {target_node.name}",
                        )
                    ],
                )
            )

        # --- Emit 'overrides' edges for method overriding ---
        # For each child class that has an extends edge to a parent class
        # within the current graph, check for same-name methods and emit
        # ``overrides`` edges.  External parents are skipped (no evidence).
        for edge in list(edges):
            if edge.kind != "extends":
                continue
            parent_node = nodes_by_id.get(edge.target)
            child_node = nodes_by_id.get(edge.source)
            if (
                parent_node is None
                or child_node is None
                or parent_node.kind != "class"
                or child_node.kind != "class"
            ):
                continue
            # Only check parents within the current graph (not external)
            if parent_node.id.startswith("extsym:"):
                continue
            parent_qualname = parent_node.qualname
            child_qualname = child_node.qualname
            if not parent_qualname or not child_qualname:
                continue
            # Find methods of both classes via the context index
            for (
                cls_qn,
                method_name,
            ), parent_method in context.methods_by_class.items():
                if cls_qn != parent_qualname:
                    continue
                child_method = context.methods_by_class.get(
                    (child_qualname, method_name)
                )
                if child_method is None:
                    continue
                # Skip __init__ and other dunder methods that are not
                # meaningful overrides for architecture analysis
                if method_name.startswith("__") and method_name.endswith("__"):
                    continue
                edges.append(
                    Edge(
                        source=child_method.id,
                        target=parent_method.id,
                        kind="overrides",
                        confidence=edge.confidence,
                        evidence=[
                            Evidence(
                                kind="ast_method_override",
                                path=child_method.path,
                                start_line=child_method.start_line,
                                detail=f"{child_node.name}.{method_name} overrides {parent_node.name}.{method_name}",
                            )
                        ],
                    )
                )

        return CallAnalysis(
            nodes=sorted(generated_nodes.values(), key=lambda node: node.id),
            edges=sorted(edges, key=lambda edge: (edge.source, edge.target, edge.kind)),
        )

    def _resolve_callsite(
        self,
        source: Node,
        callsite: dict[str, Any],
        context: _CallResolutionContext,
    ) -> _ResolvedCallTarget | None:
        if self.enable_v2 and not callsite.get("receiver"):
            name = str(callsite.get("name") or "")
            if (
                name.isidentifier()
                and context.comprehension_type_ref(source, name) is not None
            ):
                return None
        if self.enable_v2 and callsite.get("receiver") and callsite.get("attribute"):
            proven = self._evidence_receiver_target(source, callsite, context)
            if proven is not None:
                return proven
        if self.enable_v2:
            root = context.lexical.root_name(callsite.get("name"))
            found = context.lexical.lookup(source, root) if root else None
            if found and (
                any(b.get("pytest_parameter") for b in found[1])
                or (
                    callsite.get("receiver")
                    and self._hidden_by_local_value(source, root, context)
                )
            ):
                # Fixture inputs require binding evidence, even outside closures,
                # and so does a receiver that is a local value hiding an import,
                # a module definition or a builtin of its name. Bypass every
                # import, class-name and unique-method fallback for this receiver.
                receiver, attribute = callsite.get("receiver"), callsite.get(
                    "attribute"
                )
                if (
                    context.lexical.stable(*found)
                    and context.lexical.type_ref(source, root) is not None
                    and isinstance(receiver, str)
                    and isinstance(attribute, str)
                ):
                    return self._typed_receiver_target(
                        source, receiver, attribute, context
                    )
                return None
        if self.enable_v2 and context.lexical.blocked(source, callsite):
            return None
        if self.enable_v2:
            resolved = self._resolve_target_v2(source, callsite, context)
            if resolved is not None:
                return resolved
            if source.id in context.lexical.strict:
                return None
            if not callsite.get("receiver") and self._called_local_value(
                source, str(callsite.get("name") or ""), context
            ):
                return None
            receiver = callsite.get("receiver")
            if receiver in {"self", "cls"} and not self._own_receiver(
                source, str(receiver), context
            ):
                # A self or cls that is not the method's own receiver is an
                # ordinary name, which the fallback does not match by name.
                return None

        target = self._resolve_target(
            source,
            callsite,
            context.by_name,
            context.by_qualname,
            after_v2=self.enable_v2,
            context=context,
        )
        if target is None:
            return None
        raw_name = str(callsite.get("name"))
        return _ResolvedCallTarget(
            target=target,
            strategy=self._resolution_strategy(raw_name),
            candidate_count=1,
        )

    def _resolve_target_v2(
        self,
        source: Node,
        callsite: dict[str, Any],
        context: _CallResolutionContext,
    ) -> _ResolvedCallTarget | None:
        raw_name = callsite.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            return None

        receiver_expression = self._str_or_none(callsite.get("receiver"))
        attribute = self._str_or_none(callsite.get("attribute"))
        if receiver_expression and attribute:
            if self._is_config_callsite(callsite):
                return None

            imported_attribute = self._import_alias_target(
                source,
                f"{receiver_expression}.{attribute}",
                context,
                strategy="imported_module_attribute",
            )
            if imported_attribute is not None:
                return imported_attribute

            root = context.lexical.root_name(receiver_expression)
            if (
                source.id in context.lexical.strict
                and root is not None
                and context.lexical.type_ref(source, root) is not None
            ):
                # A lexical receiver type takes precedence over class-name and
                # method-name heuristics, including when that type lacks the method.
                return self._typed_receiver_target(
                    source, receiver_expression, attribute, context
                )

            class_attribute = self._resolve_class_attribute_method(
                receiver_expression,
                attribute,
                context,
            )
            if class_attribute is not None:
                return _ResolvedCallTarget(
                    target=class_attribute,
                    strategy="class_attribute_method",
                    candidate_count=1,
                    receiver_expression=receiver_expression,
                )

            same_class = self._resolve_same_class_method(source, attribute, context)
            if (
                same_class is not None
                and receiver_expression in {"self", "cls"}
                and self._own_receiver(source, receiver_expression, context)
            ):
                return _ResolvedCallTarget(
                    target=same_class,
                    strategy="same_class_receiver",
                    candidate_count=1,
                    receiver_expression=receiver_expression,
                )
            parent_method = self._super_method_target(
                source, receiver_expression, attribute, context
            )
            if parent_method is not None:
                return parent_method

            ast_visitor_target = self._ast_node_visitor_method_target(
                source,
                receiver_expression,
                attribute,
                context,
            )
            if ast_visitor_target is not None:
                return _ResolvedCallTarget(
                    target=ast_visitor_target,
                    strategy="ast_node_visitor_method",
                    candidate_count=1,
                    confidence="heuristic",
                    edge_kind="uses",
                    receiver_expression=receiver_expression,
                )

            builtin_type_attribute = self._builtin_type_attribute_target(
                receiver_expression,
                attribute,
            )
            if builtin_type_attribute is not None:
                return _ResolvedCallTarget(
                    target=builtin_type_attribute,
                    strategy="builtin_type_attribute",
                    candidate_count=1,
                    confidence="confirmed",
                    edge_kind="uses",
                    receiver_expression=receiver_expression,
                )

            typed_target = self._typed_receiver_target(
                source, receiver_expression, attribute, context
            )
            if typed_target is not None:
                return typed_target
            if self._known_type_lacks_method(
                source, receiver_expression, attribute, context
            ):
                # The receiver's type is known to have no such method, so no
                # guess from the method's name may stand in for it.
                return self._generated_dynamic_target(source, callsite, context)

            receiver_name_target = self._receiver_name_boundary_method_target(
                source,
                receiver_expression,
                attribute,
                context,
            )
            if receiver_name_target is not None:
                return _ResolvedCallTarget(
                    target=receiver_name_target,
                    strategy="receiver_name_boundary_method",
                    candidate_count=1,
                    confidence="heuristic",
                    edge_kind=(
                        None if receiver_name_target.kind in TARGET_KINDS else "uses"
                    ),
                    receiver_expression=receiver_expression,
                )

            common_target = self._common_boundary_method_target(
                receiver_expression,
                attribute,
            )
            if common_target is not None:
                return _ResolvedCallTarget(
                    target=common_target,
                    strategy="common_boundary_method",
                    candidate_count=1,
                    confidence="heuristic",
                    edge_kind="uses",
                    receiver_expression=receiver_expression,
                )

            mock_target = self._mock_assert_method_target(
                source, receiver_expression, attribute
            )
            if mock_target is not None:
                return _ResolvedCallTarget(
                    target=mock_target,
                    strategy="mock_assert_method",
                    candidate_count=1,
                    confidence="heuristic",
                    edge_kind="uses",
                    receiver_expression=receiver_expression,
                )

            unique_target = self._unique_method_fallback_target(
                source,
                receiver_expression,
                attribute,
                context,
            )
            if unique_target is not None:
                return _ResolvedCallTarget(
                    target=unique_target,
                    strategy="unique_method_fallback",
                    candidate_count=1,
                    confidence="heuristic",
                    receiver_expression=receiver_expression,
                )

            return None

        return self._resolve_name_target(source, raw_name, context, callsite=callsite)

    def _evidence_receiver_target(
        self,
        source: Node,
        callsite: dict[str, Any],
        context: _CallResolutionContext,
    ) -> _ResolvedCallTarget | None:
        """Resolve proven values before considering any name-based fallback."""
        receiver = str(callsite["receiver"])
        ref: dict[str, Any] | None = self._receiver_type_ref(source, receiver, context)
        if ref and ref.get("untyped_comprehension"):
            # Missing element evidence is not an ambiguous union. Retain only
            # the existing generic boundary policy, never a project-class guess.
            boundary = self._common_boundary_method_target(
                receiver, str(callsite["attribute"])
            )
            if boundary is not None:
                return _ResolvedCallTarget(
                    target=boundary,
                    strategy="common_boundary_method",
                    candidate_count=1,
                    confidence="heuristic",
                    edge_kind="uses",
                    receiver_expression=receiver,
                )
            return self._generated_dynamic_target(source, callsite, context)
        operands = self._barrier_operands(source, receiver, ref, context)
        if operands:
            if all(
                self._barrier_allows(
                    source, callsite, receiver, expression, value, context
                )
                for expression, value in operands
            ):
                typed = self._typed_receiver_target(
                    source, receiver, str(callsite["attribute"]), context
                )
                if typed is not None:
                    return typed
            # Do not re-enter class-name, boundary, config or legacy guesses.
            return self._generated_dynamic_target(source, callsite, context)
        return None

    def _barrier_allows(
        self,
        source: Node,
        callsite: dict[str, Any],
        receiver: str,
        expression: str,
        ref: dict[str, Any],
        context: _CallResolutionContext,
    ) -> bool:
        """Whether the value of ``expression``, behind an evidence barrier or a
        union, is available at ``callsite``."""

        barrier_callsite = (
            callsite
            if expression == receiver
            else {**callsite, "call_expression": expression}
        )
        root = context.lexical.root_name(expression)
        found = context.lexical.lookup(source, root) if root else None
        root_ref = context.scope_type_refs.get(source.id, {}).get(root or "")
        annotated_union = union_alternatives(root_ref) is not None and bool(
            found and any(binding.get("annotation") for binding in found[1])
        )
        # Preserve existing nullable factory/field inference. Explicitly
        # annotated union bindings must also survive writes and deletion.
        proof: dict[str, Any] = source.properties.get("nullable_bindings", {}).get(
            root or "", {}
        )
        available_proof = bool(proof) and (
            callsite.get("line", 0),
            callsite.get("column", 0),
        ) > (proof.get("line", 0), proof.get("column", 0))
        available_value = bool(
            ref.get("typed_value_evidence")
        ) and context.value_ref_available(source, ref, int(callsite.get("line") or 0))
        return (
            available_proof
            or available_value
            or bool(ref.get("comprehension_binding"))
            or not context.lexical.blocked(
                source,
                barrier_callsite,
                require_stable=annotated_union or bool(ref.get("typed_value_evidence")),
            )
        )

    def _barrier_operands(
        self,
        source: Node,
        receiver: str,
        ref: dict[str, Any] | None,
        context: _CallResolutionContext,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Each part of ``receiver`` whose evidence barrier or union applies.

        A path join is typed from one operand, the left one when it is a path
        and otherwise the right one, as ``_receiver_type_ref_node`` types it,
        and carries that operand's evidence: ``(base / segment).parent`` takes
        its value from ``base``. The other operand is evaluated as well, so its
        own barrier applies too, at any depth of joins.
        """

        if not ref:
            # An untyped receiver is not linked by type, so no barrier applies.
            return []
        operands: list[tuple[str, dict[str, Any]]] = []
        try:
            node: ast.expr | None = ast.parse(receiver, mode="eval").body
        except SyntaxError:
            node = None
        changed = False
        while node is not None:
            inner = node
            while isinstance(inner, (ast.Call, ast.Attribute, ast.Subscript)):
                inner = inner.func if isinstance(inner, ast.Call) else inner.value
            if not (isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Div)):
                break
            left = self._receiver_type_ref_node(source, inner.left, context)
            if self._type_id(left) in PATH_TYPE_IDS:
                node, other = inner.left, inner.right
            else:
                node, other = inner.right, inner.left
            operands += self._barrier_operands(
                source,
                self._unparse(other),
                self._receiver_type_ref_node(source, other, context),
                context,
            )
            changed = True
        if union_alternatives(ref) is not None or ref.get("typed_value_evidence"):
            primary = self._unparse(node) if changed and node is not None else receiver
            operands.insert(0, (primary, ref))
        return operands

    def _typed_receiver_target(
        self,
        source: Node,
        receiver_expression: str,
        attribute: str,
        context: _CallResolutionContext,
    ) -> _ResolvedCallTarget | None:
        receiver_type = self._receiver_type_ref(
            source,
            receiver_expression,
            context,
        )
        owner = context.by_id.get(self._type_id(receiver_type) or "")
        if owner is not None and owner.properties.get("bases"):
            source.properties["inherited_type_input"] = True
        target = self._method_target_from_type(receiver_type, attribute, context)
        if target is not None:
            return _ResolvedCallTarget(
                target=target,
                strategy="receiver_type",
                candidate_count=1,
                receiver_expression=receiver_expression,
                receiver_type=self._type_id(receiver_type),
                receiver_type_ref_id=self._type_ref_id(receiver_type),
            )

        if str(self._type_id(receiver_type) or "").startswith("class:"):
            source.properties["inherited_type_input"] = True
        inherited = self._inherited_method_target(receiver_type, attribute, context)
        if inherited is not None:
            return _ResolvedCallTarget(
                target=inherited,
                strategy="inherited_receiver_type",
                candidate_count=1,
                confidence="inferred",
                edge_kind="uses" if inherited.kind in EXTERNAL_TARGET_KINDS else None,
                receiver_expression=receiver_expression,
                receiver_type=self._type_id(receiver_type),
                receiver_type_ref_id=self._type_ref_id(receiver_type),
            )
        protocol = self._typing_mapping_target(receiver_type, attribute, context)
        if protocol is not None:
            return _ResolvedCallTarget(
                target=protocol,
                strategy="typing_protocol_method",
                candidate_count=1,
                confidence="inferred",
                edge_kind="uses",
                receiver_expression=receiver_expression,
                receiver_type=self._type_id(receiver_type),
                receiver_type_ref_id=self._type_ref_id(receiver_type),
            )

        builtin_target = self._builtin_method_target(receiver_type, attribute)
        if builtin_target is not None:
            return _ResolvedCallTarget(
                target=builtin_target,
                strategy="builtin_receiver_type",
                candidate_count=1,
                confidence="confirmed",
                edge_kind="uses",
                receiver_expression=receiver_expression,
                receiver_type=self._type_id(receiver_type),
                receiver_type_ref_id=self._type_ref_id(receiver_type),
            )

        external_target = self._external_method_target(receiver_type, attribute)
        if external_target is not None:
            return _ResolvedCallTarget(
                target=external_target,
                strategy="external_receiver_type",
                candidate_count=1,
                confidence="heuristic",
                edge_kind="uses",
                receiver_expression=receiver_expression,
                receiver_type=self._type_id(receiver_type),
                receiver_type_ref_id=self._type_ref_id(receiver_type),
            )

        return None

    def _resolve_name_target(
        self,
        source: Node,
        raw_name: str,
        context: _CallResolutionContext,
        *,
        callsite: dict[str, Any] | None = None,
    ) -> _ResolvedCallTarget | None:
        if raw_name == "cls" and context.lexical.own_class_receiver(source):
            class_target = self._class_constructor_target(source, context)
            if class_target is not None:
                return _ResolvedCallTarget(
                    target=class_target,
                    strategy="class_receiver_constructor",
                    candidate_count=1,
                )

        if source.id in context.lexical.strict:
            lexical_binding = context.lexical.binding(source, raw_name)
            if lexical_binding and lexical_binding.get("kind") == "import_alias":
                return self._import_alias_target(source, raw_name, context)
        local_definition = self._local_definition_target(
            source, raw_name, context, callsite=callsite
        )
        if local_definition is not None:
            return _ResolvedCallTarget(
                target=local_definition,
                strategy="local_definition",
                candidate_count=1,
            )

        local_alias = self._local_callable_alias_target(source, raw_name, context)
        if local_alias is not None:
            return local_alias
        if self._called_local_value(source, raw_name, context):
            # Past the local's own definition and alias, its name links nothing:
            # not the module's, the builtin, nor a function of that name elsewhere.
            return None

        same_module_target = self._resolve_same_module(
            source,
            raw_name,
            context.by_name,
            context.callsite_position,
            context.lexical,
        )
        if same_module_target:
            return self._resolved_name_target(same_module_target, "same_module_symbol")

        exact_target = context.by_qualname.get(raw_name)
        if exact_target:
            return self._resolved_name_target(exact_target, "qualified_symbol")

        imported_target = self._import_alias_target(source, raw_name, context)
        if imported_target is not None:
            return imported_target

        if "." in raw_name:
            return None
        held = self._module_name_at(
            source, raw_name, context.lexical, context.callsite_position
        )
        if held is not None and held[0] == "def" and held[1] in context.by_id:
            # A def that a star import brought from another module.
            return _ResolvedCallTarget(
                target=context.by_id[held[1]],
                strategy="import_alias",
                candidate_count=1,
                confidence="confirmed",
            )
        if held is not None and held[0] == "import" and held[1]:
            # An import that a star import brought, or one the aliases above
            # did not read.
            return self._imported_qualname_target(held[1], context, "import_alias")
        if self._module_binds(source, raw_name, context):
            # The module binds the name where it is read, to something other
            # than its one definition: an assignment, another def, a binding
            # under if or try. It is not the builtin, nor a symbol elsewhere.
            return None

        builtin_target = self._builtin_function_target(raw_name)
        if builtin_target is not None and (
            self._module_qualname(source) not in context.star_import_modules
        ):
            # A bare name that is neither defined, nor imported, nor bound in
            # this module is the builtin, however many symbols elsewhere in the
            # project share the name; only a star import could bring another.
            return _ResolvedCallTarget(
                target=builtin_target,
                strategy="builtin_function",
                candidate_count=1,
                confidence="confirmed",
                edge_kind="uses",
            )
        # A bare name reaches no method, nor a class nested in another class,
        # nor a definition of its own module that the rules above refused,
        # nor one the module's star imports are known not to bring.
        if self._star_imports_exclude(source, raw_name, held, context.lexical):
            candidates = []
        else:
            candidates = [
                candidate
                for candidate in context.by_name.get(raw_name, [])
                if self._is_module_level(candidate)
                and self._module_qualname(candidate) != self._module_qualname(source)
            ]
        if len(candidates) == 1:
            return self._resolved_name_target(candidates[0], "unique_short_name")
        if builtin_target is not None:
            return _ResolvedCallTarget(
                target=builtin_target,
                strategy="builtin_function",
                candidate_count=1,
                confidence="confirmed",
                edge_kind="uses",
            )
        return None

    def _resolved_name_target(self, target: Node, strategy: str) -> _ResolvedCallTarget:
        if target.kind == "class":
            strategy = "constructor"
        return _ResolvedCallTarget(target=target, strategy=strategy, candidate_count=1)

    def _unique_short_name_fallback(
        self,
        raw_name: str,
        context: _CallResolutionContext,
        *,
        receiver_expression: str | None = None,
    ) -> _ResolvedCallTarget | None:
        short_name = raw_name.rsplit(".", 1)[-1]
        candidates = context.by_name.get(short_name, [])
        if len(candidates) != 1:
            return None
        return _ResolvedCallTarget(
            target=candidates[0],
            strategy="unique_short_name_fallback",
            candidate_count=1,
            confidence="heuristic",
            receiver_expression=receiver_expression,
        )

    def _receiver_type_ref(
        self,
        source: Node,
        expression: str,
        context: _CallResolutionContext,
    ) -> dict[str, Any] | None:
        parsed = self._parse_expression(expression)
        if parsed is None:
            return None
        return self._receiver_type_ref_node(source, parsed, context)

    def _receiver_type_ref_node(
        self,
        source: Node,
        node: ast.AST,
        context: _CallResolutionContext,
    ) -> dict[str, Any] | None:
        scope_refs = context.scope_type_refs.get(source.id, {})
        module_scope_refs = (
            context.module_type_refs_by_path.get(source.path or "", {})
            if source.kind != "module"
            else {}
        )
        if isinstance(node, ast.Name):
            scoped = context.comprehension_type_ref(source, node.id)
            if scoped is not None:
                return scoped
            if source.id in context.lexical.strict:
                return context.lexical.type_ref(source, node.id)
            return context.type_ref_at(source, node.id) or module_scope_refs.get(
                node.id
            )

        if isinstance(node, ast.Attribute):
            full_name = self._unparse(node)
            root = context.lexical.root_name(full_name)
            scoped_root = context.comprehension_type_ref(source, root) if root else None
            if (
                scoped_root is None
                and full_name in scope_refs
                and not context.not_yet_bound(source, full_name)
            ):
                return context.type_ref_at(source, full_name)
            # An attribute not yet bound in this function is not unbound, as a
            # local would be: it holds what its object held on entry, read
            # below from the object's class.
            if scoped_root is None and full_name in module_scope_refs:
                return module_scope_refs[full_name]
            if (
                scoped_root is None
                and isinstance(node.value, ast.Name)
                and node.value.id in {"self", "cls"}
                and self._own_receiver(source, node.value.id, context)
            ):
                class_qualname = self._class_qualname(source)
                if class_qualname:
                    return context.class_instance_attrs.get(class_qualname, {}).get(
                        node.attr
                    )
            receiver_type = self._receiver_type_ref_node(source, node.value, context)
            if union_alternatives(receiver_type) is not None and not self._type_id(
                receiver_type
            ):
                return unknown_union_result(self._unparse(node))
            receiver_type_id = self._type_id(receiver_type)
            if receiver_type_id and receiver_type_id.startswith("class:"):
                class_qualname = receiver_type_id.removeprefix("class:")
                field = context.class_instance_attrs.get(class_qualname, {}).get(
                    node.attr
                )
                if field is not None:
                    return field
            if node.attr == "parent":
                parent = self._path_value_type_ref(receiver_type, "path_parent")
                if parent is not None:
                    return parent
            if scoped_root is not None:
                return unknown_union_result(full_name)
            return None

        literal = self._literal_receiver_type(node)
        if literal is not None:
            return {
                "type_id": literal,
                "type_expression": literal.split(":", 1)[1],
                "strategy": "literal",
            }

        if isinstance(node, ast.Subscript):
            receiver_type = self._receiver_type_ref_node(source, node.value, context)
            if union_alternatives(receiver_type) is not None and not self._type_id(
                receiver_type
            ):
                return unknown_union_result(self._unparse(node))
            if isinstance(node.slice, ast.Slice):
                return sliced_value_type(receiver_type)
            return self._subscript_value_type_ref(receiver_type)

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                receiver_ref = self._receiver_type_ref_node(
                    source, node.func.value, context
                )
                documented = self._external_method_return_type_ref(
                    receiver_ref, node.func.attr
                )
                if documented is not None:
                    return documented
                element = self._mapping_value_type_ref(
                    source, node, receiver_ref, context
                )
                if element is not None:
                    return element
                value = single_value_type(receiver_ref) if receiver_ref else {}
                if value.get("type_id") in {
                    "typing:Mapping",
                    "typing:MutableMapping",
                    "extsym:collections.abc.Mapping",
                    "extsym:collections.abc.MutableMapping",
                } or (
                    receiver_ref
                    and (
                        union_alternatives(receiver_ref) is not None
                        or receiver_ref.get("typed_value_evidence")
                    )
                ):
                    resolved = self._resolve_call_node(source, node, context)
                    if resolved is not None:
                        known = self._external_return_type_ref(resolved.target)
                        if known is not None:
                            return {**known, "typed_value_evidence": True}
                        if resolved.target.kind not in EXTERNAL_TARGET_KINDS:
                            returned = context.return_type_by_target.get(
                                resolved.target.id
                            )
                            if returned is not None:
                                return returned
                    return unknown_union_result(self._unparse(node))
            resolved = self._resolve_call_node(source, node, context)
            if resolved is None:
                if isinstance(node.func, ast.Attribute):
                    receiver = self._receiver_type_ref_node(
                        source, node.func.value, context
                    )
                    if union_alternatives(receiver) is not None:
                        return unknown_union_result(self._unparse(node))
                return None
            return self._type_ref_from_target(
                resolved.target,
                context,
                node,
                imported=self._bound_by_import(source, node.func, context),
                guessed=resolved.strategy in _NAME_GUESS_STRATEGIES,
            )

        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):

            def resolve(operand: ast.expr) -> dict[str, Any] | None:
                return self._receiver_type_ref_node(source, operand, context)

            left = resolve(node.left)
            if self._type_id(left) in PATH_TYPE_IDS and may_be_path_segment(
                node.right, resolve
            ):
                return self._path_value_type_ref(left, "path_join")
            # ``segment / path`` is a path of the right operand's flavour, by
            # its ``__rtruediv__``.
            right = resolve(node.right)
            if self._type_id(right) in PATH_TYPE_IDS and may_be_path_segment(
                node.left, resolve
            ):
                return self._path_value_type_ref(right, "path_join")
            return None

        return None

    @staticmethod
    def _literal_receiver_type(node: ast.AST) -> str | None:
        """The type id of a literal, a display, a generator expression or an
        f-string called on directly, as the type analyzer gives one assigned
        to a name."""

        if isinstance(node, ast.JoinedStr):
            return "builtin:str"
        if isinstance(node, ast.GeneratorExp):
            return GENERATOR_TYPE_ID
        type_name = literal_type_name(node)
        return None if type_name in {None, "None"} else f"builtin:{type_name}"

    @classmethod
    def _path_value_type_ref(
        cls,
        receiver: dict[str, Any] | None,
        strategy: str,
    ) -> dict[str, Any] | None:
        """A path derived from ``receiver``, of the same pathlib flavour."""

        # A union such as ``Path | None`` does not say which member is used.
        if not receiver or union_alternatives(receiver) is not None:
            return None
        type_id = cls._type_id(receiver)
        if type_id is None or type_id not in PATH_TYPE_IDS:
            return None
        return cls._derived_type_ref(
            receiver,
            {"type_id": type_id, "type_expression": type_id.removeprefix("extsym:")},
            strategy,
        )

    @classmethod
    def _external_method_return_type_ref(
        cls,
        receiver: dict[str, Any] | None,
        method_name: str,
    ) -> dict[str, Any] | None:
        """The documented return type of a method on a known external or
        builtin type."""

        # A union receiver keeps the evidence rules applied to its calls.
        if not receiver or union_alternatives(receiver) is not None:
            return None
        type_id = cls._type_id(receiver)
        if type_id is None:
            return None
        returned = method_return_type(type_id, method_name)
        if returned is None:
            return None
        strategy = (
            "builtin_method_return"
            if type_id.startswith("builtin:")
            else "external_method_return"
        )
        return cls._derived_type_ref(receiver, returned, strategy)

    def _mapping_value_type_ref(
        self,
        source: Node,
        node: ast.Call,
        receiver: dict[str, Any] | None,
        context: _CallResolutionContext,
    ) -> dict[str, Any] | None:
        """The value ``dict.get`` or ``dict.setdefault`` returns, by the rule
        the type analyzer applies to a local assigned from the same call."""

        if (
            not receiver
            or union_alternatives(receiver) is not None
            or not isinstance(node.func, ast.Attribute)
        ):
            return None
        default = mapping_default(node)
        value = mapping_value_type(
            receiver,
            node.func.attr,
            (
                self._receiver_type_ref_node(source, default, context)
                if default is not None
                else None
            ),
        )
        if value is None:
            return None
        # An element keeps its container's evidence, as a subscript does.
        element: dict[str, Any] = {**value, "strategy": "builtin_method_return"}
        if receiver.get("typed_value_evidence"):
            element["typed_value_evidence"] = True
        if isinstance(receiver.get("type_ref_id"), str):
            element["type_ref_id"] = receiver["type_ref_id"]
        return element

    @staticmethod
    def _derived_type_ref(
        receiver: dict[str, Any],
        returned: dict[str, Any],
        strategy: str,
    ) -> dict[str, Any]:
        derived: dict[str, Any] = {**returned, "strategy": strategy}
        # A derived value keeps its receiver's evidence, as a subscript does,
        # and is available exactly where the receiver's binding is, since it
        # is computed from that value alone.
        if receiver.get("typed_value_evidence"):
            derived["typed_value_evidence"] = True
        for key in ("type_ref_id", "binding_id"):
            if isinstance(receiver.get(key), str):
                derived[key] = receiver[key]
        return derived

    @staticmethod
    def _subscript_value_type_ref(
        receiver_type: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not receiver_type:
            return None
        wrapped = union_alternatives(receiver_type) is not None
        receiver_type = single_value_type(receiver_type)
        type_id = receiver_type.get("type_id")
        type_args = receiver_type.get("type_args")
        if not isinstance(type_args, list) or not type_args:
            return None
        if type_id == "builtin:dict" and len(type_args) >= 2:
            item = type_args[1]
        elif type_id == "builtin:tuple":
            item = tuple_element_type(receiver_type)
        elif type_id == "builtin:list":
            item = type_args[0]
        else:
            return None
        if not isinstance(item, dict):
            return None
        propagated = dict(item)
        propagated["strategy"] = "subscript_value"
        if wrapped or receiver_type.get("typed_value_evidence"):
            propagated["typed_value_evidence"] = True
        if isinstance(receiver_type.get("type_ref_id"), str):
            propagated["type_ref_id"] = receiver_type["type_ref_id"]
        return propagated

    @staticmethod
    def _own_receiver(source: Node, name: str, context: _CallResolutionContext) -> bool:
        """In a method, whether self or cls is still its own receiver; outside
        one, such as in a closure over a method's self, the name is left as
        it was read before."""

        return source.kind != "method" or context.lexical.own_receiver(source, name)

    @staticmethod
    def _hidden_by_local_value(
        source: Node, root: str | None, context: _CallResolutionContext
    ) -> bool:
        """Whether, outside a strict scope, ``root`` is a local value that
        hides an import, a definition or a builtin of its name."""

        return (
            root is not None
            and source.id not in context.lexical.strict
            and context.lexical.shadowed(source, root)
        )

    @staticmethod
    def _called_local_value(
        source: Node, name: str, context: _CallResolutionContext
    ) -> bool:
        """Whether a bare call of ``name`` outside a strict scope calls a local
        value, which neither a module name, a builtin nor a function of that
        name elsewhere is."""

        return (
            source.id not in context.lexical.strict
            and name.isidentifier()
            and context.lexical.local_value(source, name)
        )

    def _resolve_call_node(
        self,
        source: Node,
        node: ast.Call,
        context: _CallResolutionContext,
    ) -> _ResolvedCallTarget | None:
        root = context.lexical.root_name(self._unparse(node.func))
        if isinstance(node.func, ast.Attribute) and self._hidden_by_local_value(
            source, root, context
        ):
            # Only the local's own type resolves the call, as for a call at the
            # top of an expression.
            found = context.lexical.lookup(source, root) if root else None
            if (
                found is not None
                and context.lexical.stable(*found)
                and context.lexical.type_ref(source, str(root)) is not None
            ):
                return self._typed_receiver_target(
                    source, self._unparse(node.func.value), node.func.attr, context
                )
            return None
        if isinstance(node.func, ast.Attribute):
            receiver_expression = self._unparse(node.func.value)
            # The same rules as a call at the top of an expression, so that
            # self.factory().method() reads the factory's return type.
            if receiver_expression in {
                "self",
                "cls",
            } and self._own_receiver(source, receiver_expression, context):
                same_class = self._resolve_same_class_method(
                    source, node.func.attr, context
                )
                if same_class is not None:
                    return _ResolvedCallTarget(
                        target=same_class,
                        strategy="same_class_receiver",
                        candidate_count=1,
                        receiver_expression=receiver_expression,
                    )
            parent_method = self._super_method_target(
                source, receiver_expression, node.func.attr, context
            )
            if parent_method is not None:
                return parent_method
            receiver_type = self._receiver_type_ref(
                source, receiver_expression, context
            )
            if union_alternatives(receiver_type) is not None:
                return self._typed_receiver_target(
                    source, receiver_expression, node.func.attr, context
                )
            target = self._method_target_from_type(
                receiver_type, node.func.attr, context
            )
            if target is not None:
                return _ResolvedCallTarget(
                    target=target,
                    strategy="receiver_type",
                    candidate_count=1,
                    receiver_expression=receiver_expression,
                    receiver_type=self._type_id(receiver_type),
                    receiver_type_ref_id=self._type_ref_id(receiver_type),
                )
            if receiver_type is None:
                class_attribute = self._resolve_class_attribute_method(
                    receiver_expression, node.func.attr, context
                )
                if class_attribute is not None:
                    return _ResolvedCallTarget(
                        target=class_attribute,
                        strategy="class_attribute_method",
                        candidate_count=1,
                        receiver_expression=receiver_expression,
                    )
            builtin_target = self._builtin_method_target(receiver_type, node.func.attr)
            if builtin_target is not None:
                return _ResolvedCallTarget(
                    target=builtin_target,
                    strategy="builtin_receiver_type",
                    candidate_count=1,
                    confidence="confirmed",
                    edge_kind="uses",
                    receiver_expression=receiver_expression,
                    receiver_type=self._type_id(receiver_type),
                    receiver_type_ref_id=self._type_ref_id(receiver_type),
                )
            external_target = self._external_method_target(
                receiver_type, node.func.attr
            )
            if external_target is not None:
                return _ResolvedCallTarget(
                    target=external_target,
                    strategy="external_receiver_type",
                    candidate_count=1,
                    confidence="heuristic",
                    edge_kind="uses",
                    receiver_expression=receiver_expression,
                    receiver_type=self._type_id(receiver_type),
                    receiver_type_ref_id=self._type_ref_id(receiver_type),
                )
            if self._known_type_lacks_method(
                source, receiver_expression, node.func.attr, context
            ):
                return None
            receiver_name_target = self._receiver_name_boundary_method_target(
                source,
                receiver_expression,
                node.func.attr,
                context,
            )
            if receiver_name_target is not None:
                return _ResolvedCallTarget(
                    target=receiver_name_target,
                    strategy="receiver_name_boundary_method",
                    candidate_count=1,
                    confidence="heuristic",
                    edge_kind=(
                        None if receiver_name_target.kind in TARGET_KINDS else "uses"
                    ),
                    receiver_expression=receiver_expression,
                )
            common_target = self._common_boundary_method_target(
                receiver_expression,
                node.func.attr,
            )
            if common_target is not None:
                return _ResolvedCallTarget(
                    target=common_target,
                    strategy="common_boundary_method",
                    candidate_count=1,
                    confidence="heuristic",
                    edge_kind="uses",
                    receiver_expression=receiver_expression,
                )

        raw_name = self._name(node.func) or self._unparse(node.func)
        return self._resolve_name_target(source, raw_name, context)

    def _typing_mapping_target(
        self, ref: dict[str, Any] | None, method: str, context: _CallResolutionContext
    ) -> Node | None:
        if not ref or method not in {"get", "keys", "items", "values", "__getitem__"}:
            return None
        value = single_value_type(ref)
        if value.get("type_id") not in {
            "typing:Mapping",
            "typing:MutableMapping",
            "extsym:typing.Mapping",
            "extsym:typing.MutableMapping",
            "extsym:collections.abc.Mapping",
            "extsym:collections.abc.MutableMapping",
        }:
            return None
        expression = self._parse_expression(str(value.get("type_expression", "")))
        if isinstance(expression, ast.Subscript):
            expression = expression.value
        name = self._name(expression) if expression is not None else None
        module = context.lexical.modules.get(ref.get("path"))
        found = (
            context.lexical.lookup(module, name.split(".")[0])
            if name and module
            else None
        )
        if not found or not context.lexical.stable(*found):
            return None
        binding: dict[str, Any] = found[1][0]
        imported = str(binding.get("target_qualname", ""))
        qualified = ".".join([imported, *name.split(".")[1:]])
        if binding.get("kind") != "import_alias" or qualified not in {
            "typing.Mapping",
            "typing.MutableMapping",
            "collections.abc.Mapping",
            "collections.abc.MutableMapping",
        }:
            return None
        return self._external_symbol_node(
            f"collections.abc.Mapping.{method}",
            name=method,
            source="typing_protocol_method",
        )

    def _inherited_method_target(
        self,
        ref: dict[str, Any] | None,
        method: str,
        context: _CallResolutionContext,
        *,
        after: str | None = None,
    ) -> Node | None:
        """The method a class inherits, in the order of its method resolution
        order; with ``after``, the one found past that class in the order, as
        ``super()`` finds it. A base that is not a project class, such as
        object or an external class, has methods unknown here, so the search
        stops there."""

        start = context.by_id.get(self._type_id(ref) or "")
        if start is None or start.kind != "class":
            return None
        order = self._method_resolution_order(start, context, ())
        if order is None:
            return None
        begin = 0
        if after is not None:
            keys = [self._resolution_order_key(entry) for entry in order]
            if after not in keys:
                return None
            begin = keys.index(after) + 1
        for position, entry in enumerate(order[begin:], begin):
            if isinstance(entry, str):
                if entry == "pydantic.BaseModel" and method in {
                    "model_dump",
                    "model_dump_json",
                }:
                    return self._external_symbol_node(
                        f"pydantic.BaseModel.{method}",
                        name=method,
                        source="inherited_receiver_type",
                    )
                return None
            if position:
                candidate = self._method_target_from_type(
                    {"type_id": entry.id}, method, context
                )
                if candidate is not None and not any(
                    str(d).split("(", 1)[0] in {"property", "cached_property"}
                    for d in candidate.properties.get("decorators", [])
                ):
                    return candidate
            class_bindings: list[dict[str, Any]] = entry.properties.get("bindings", [])
            if any(b.get("name") == method for b in class_bindings):
                return None
        return None

    def _method_resolution_order(
        self,
        class_node: Node,
        context: _CallResolutionContext,
        visiting: tuple[str, ...],
    ) -> list[Node | str] | None:
        """The C3 linearization of a project class, or None if a base may be
        a project class not settled here, or the bases admit no order.

        A class outside the project stands for itself alone, its own bases
        left out. Under a single base that loses nothing the search reaches:
        it stops at that class. Under several bases the merge decides where
        a shared ancestor goes, and a class outside the project may inherit a
        project class, as code outside the indexed roots can; leaving its
        bases out could then move that project class ahead of it. So there
        the order is unknown, unless every such class is a builtin or of the
        standard library, which inherit no project class."""

        if class_node.id in visiting:
            return None
        cached = context.method_resolution_orders.get(class_node.id, False)
        if cached is not False:
            return cached
        bases: list[Node | str] = []
        for base in class_node.properties.get("bases", []):
            resolved = self._base_class(class_node, str(base), context)
            if resolved is None:
                context.method_resolution_orders[class_node.id] = None
                return None
            bases.append(resolved)
        sequences: list[list[Node | str]] = []
        for base_entry in bases:
            if isinstance(base_entry, str):
                sequences.append([base_entry])
                continue
            inherited = self._method_resolution_order(
                base_entry, context, (*visiting, class_node.id)
            )
            if inherited is None:
                context.method_resolution_orders[class_node.id] = None
                return None
            sequences.append(list(inherited))
        if len(bases) > 1 and any(
            isinstance(entry, str) and not self._inherits_no_project_class(entry)
            for sequence in sequences
            for entry in sequence
        ):
            context.method_resolution_orders[class_node.id] = None
            return None
        sequences.append(list(bases))
        order: list[Node | str] | None = [class_node]
        key = self._resolution_order_key
        while order is not None and any(sequences):
            sequences = [sequence for sequence in sequences if sequence]
            tails = {key(entry) for sequence in sequences for entry in sequence[1:]}
            head = next(
                (
                    sequence[0]
                    for sequence in sequences
                    if key(sequence[0]) not in tails
                ),
                None,
            )
            if head is None:
                order = None
                break
            order.append(head)
            sequences = [
                sequence[1:] if key(sequence[0]) == key(head) else sequence
                for sequence in sequences
            ]
        context.method_resolution_orders[class_node.id] = order
        return order

    @staticmethod
    def _inherits_no_project_class(qualname: str) -> bool:
        """Whether a class outside the project is a builtin or of the
        standard library, which cannot inherit a class of the project."""

        return qualname.split(".", 1)[0] in {"builtins", *sys.stdlib_module_names}

    @staticmethod
    def _resolution_order_key(entry: Node | str) -> str:
        return entry if isinstance(entry, str) else entry.id

    def _base_class(
        self, class_node: Node, base: str, context: _CallResolutionContext
    ) -> Node | str | None:
        """A base as the project class it names, or as the name of a class
        outside the project (a builtin such as object, or an import from
        outside); None if it may name a project class not settled here."""

        module = context.lexical.modules.get(class_node.path)
        if module is None:
            return None
        # Generic[T] and Base[int] are their classes.
        base = base.split("[", 1)[0].strip()
        found = context.lexical.lookup(module, base.split(".")[0])
        if not found:
            # Not bound in the module, so a builtin, unless a star import may
            # have brought it.
            if "." in base or module.qualname in context.star_import_modules:
                return None
            return "builtins." + base
        if not context.lexical.stable(*found):
            return None
        binding: dict[str, Any] = found[1][0]
        if binding.get("kind") == "class_definition":
            if "." in base:
                return None
            target = context.by_id.get(str(binding.get("target", "")))
        elif binding.get("kind") == "import_alias":
            qualified = ".".join(
                [str(binding.get("target_qualname", "")), *base.split(".")[1:]]
            )
            target = context.by_id.get(
                exported_class(qualified, context.lexical.nodes) or f"class:{qualified}"
            )
            if target is None:
                return qualified
        else:
            return None
        if target is None or target.kind != "class":
            return None
        return target

    def _method_target_from_type(
        self,
        type_ref: dict[str, Any] | None,
        method_name: str,
        context: _CallResolutionContext,
    ) -> Node | None:
        type_id = self._type_id(type_ref)
        if not type_id or not type_id.startswith("class:"):
            return None
        class_qualname = type_id.removeprefix("class:")
        candidate = context.methods_by_class.get((class_qualname, method_name))
        owner = context.by_id.get(type_id)
        class_bindings: list[dict[str, Any]] = (
            owner.properties.get("bindings", []) if owner else []
        )
        bindings: list[dict[str, Any]] = (
            [b for b in class_bindings if b.get("name") == method_name] if owner else []
        )
        if (
            bindings
            and (
                any(b.get("kind") != "method_definition" for b in bindings)
                or any(
                    str(d).split("(", 1)[0].rsplit(".", 1)[-1]
                    in {"property", "cached_property"}
                    for b in bindings
                    for d in b.get("decorators", [])
                )
                or (
                    len(bindings) > 1
                    and any(not b.get("scope_direct") for b in bindings)
                )
            )
        ) or (
            candidate
            and any(
                str(d).split("(", 1)[0] in {"property", "cached_property"}
                for d in candidate.properties.get("decorators", [])
            )
        ):
            return None
        return candidate

    def _type_ref_from_target(
        self,
        target: Node,
        context: _CallResolutionContext,
        call: ast.Call,
        *,
        imported: bool,
        guessed: bool = False,
    ) -> dict[str, Any] | None:
        if target.kind == "class":
            return {
                "type_id": target.id,
                "type_expression": target.name,
                "strategy": "constructor",
            }
        if target.id.startswith("extsym:"):
            # Boundary returns are written for callees guessed from receiver
            # names, such as conn.execute; nothing else follows from a guess.
            external_return = self._external_return_type_ref(target)
            if external_return is not None:
                return external_return
            if guessed:
                return None
            qualname = target.qualname or target.id.removeprefix("extsym:")
            owner, _, method = qualname.rpartition(".")
            # A documented function return is read only through a name that is
            # still its import, as the type analyzer reads an assigned call.
            documented = (
                function_return_type(qualname, call) if imported else None
            ) or (
                method_return_type(type_id_of_qualname(owner), method)
                if owner
                else None
            )
            if documented is not None:
                return {**documented, "strategy": "external_method_return"}
            if not self._is_external_class(qualname):
                # A function or method of undocumented return gives a value of
                # no known type. Taking the callee itself as that type named
                # targets that do not exist, such as builtins.dict.get.get.
                return None
            if qualname.startswith("builtins."):
                # set() is a builtin set, whose methods the builtin lists know,
                # not an external symbol that would accept any method name.
                return {
                    "type_id": type_id_of_qualname(qualname),
                    "type_expression": qualname.removeprefix("builtins."),
                    "strategy": "constructor",
                }
            return {
                "type_id": target.id,
                "type_expression": target.qualname or target.name,
                "strategy": "external_symbol",
            }
        return context.return_type_by_target.get(target.id)

    def _bound_by_import(
        self, source: Node, func: ast.expr, context: _CallResolutionContext
    ) -> bool:
        """Whether the root name of ``func`` is a stable import where it is
        called, not a parameter or an assignment of the same name."""

        root = context.lexical.root_name(self._unparse(func))
        found = context.lexical.lookup(source, root) if root else None
        return (
            found is not None
            and context.lexical.stable(*found)
            and found[1][0].get("kind") == "import_alias"
        )

    @staticmethod
    def _is_external_class(qualname: str) -> bool:
        """Whether calling the external ``qualname`` constructs an instance."""

        name = qualname.rsplit(".", 1)[-1]
        if qualname.startswith("builtins."):
            return isinstance(getattr(builtins, name, None), type)
        if name[:1].isupper():
            return qualname not in CAPITALISED_STDLIB_FUNCTIONS
        return qualname in LOWERCASE_STDLIB_CLASSES

    @staticmethod
    def _external_return_type_ref(target: Node) -> dict[str, Any] | None:
        qualname = target.qualname or target.id.removeprefix("extsym:")
        if qualname in {
            "pydantic.BaseModel.model_dump",
            "pydantic.BaseModel.model_dump_json",
        }:
            result_type = "str" if qualname.endswith("_json") else "dict"
            return {
                "type_id": f"builtin:{result_type}",
                "type_expression": result_type,
                "strategy": "external_return_boundary",
            }
        method_name = qualname.rsplit(".", 1)[-1]
        if qualname == "sqlite3.Connection.execute":
            # sqlite3 documents its execute shortcut as returning a cursor;
            # PEP 249 defines no execute on a connection, nor what it returns.
            return {
                "type_id": "extsym:sqlite3.Cursor",
                "type_expression": "sqlite3.Cursor",
                "strategy": "external_return_boundary",
            }
        if qualname in {
            "sqlalchemy.select",
            "sqlalchemy.sql.select",
            "sqlalchemy.sql.expression.select",
        }:
            return {
                "type_id": "extsym:sqlalchemy.sql.Select",
                "type_expression": "sqlalchemy.sql.Select",
                "strategy": "external_return_boundary",
            }
        if qualname in {
            "sqlalchemy.orm.Session.execute",
            "sqlalchemy.engine.Connection.execute",
        }:
            return {
                "type_id": "extsym:sqlalchemy.engine.Result",
                "type_expression": "sqlalchemy.engine.Result",
                "strategy": "external_return_boundary",
            }
        if qualname.startswith("sqlalchemy.sql.Select.") and (
            method_name in SQLALCHEMY_STATEMENT_METHODS
        ):
            return {
                "type_id": "extsym:sqlalchemy.sql.Select",
                "type_expression": "sqlalchemy.sql.Select",
                "strategy": "external_return_boundary",
            }
        if qualname.startswith("sqlalchemy.engine.Result.") and (
            method_name in SQLALCHEMY_RESULT_METHODS
        ):
            return {
                "type_id": "extsym:sqlalchemy.engine.Result",
                "type_expression": "sqlalchemy.engine.Result",
                "strategy": "external_return_boundary",
            }
        return None

    def _class_constructor_target(
        self, source: Node, context: _CallResolutionContext
    ) -> Node | None:
        class_qualname = self._class_qualname(source)
        if not class_qualname:
            return None
        return context.by_qualname.get(class_qualname)

    def _local_definition_target(
        self,
        source: Node,
        raw_name: str,
        context: _CallResolutionContext,
        *,
        callsite: dict[str, Any] | None = None,
    ) -> Node | None:
        if source.kind not in {"class", "function", "method", "module"}:
            return None
        if "." in raw_name:
            return None
        if source.id in context.lexical.strict:
            binding = context.lexical.binding(source, raw_name, callsite)
            if not binding or binding.get("kind") not in {
                "function_definition",
                "class_definition",
            }:
                return None
            target = context.by_id.get(str(binding.get("target")))
            if target is not None:
                return target
            # Preserve the defining scope's identity for a captured local class.
            # Its construction is visible even though its body is not indexed.
            found = context.lexical.lookup(source, raw_name)
            if found is not None:
                source = found[0]
        bindings = source.properties.get("bindings", [])
        if not isinstance(bindings, list):
            return None
        matches = [
            binding
            for binding in bindings
            if isinstance(binding, dict)
            and binding.get("name") == raw_name
            and binding.get("kind") in {"function_definition", "class_definition"}
        ]
        if len(matches) != 1:
            return None
        binding = matches[0]
        target_id = self._str_or_none(binding.get("target"))
        if target_id is not None and target_id in context.by_id:
            return None
        current_binding_id = self._str_or_none(binding.get("binding_id"))
        if current_binding_id is None:
            return None
        kind = "class" if binding.get("kind") == "class_definition" else "function"
        qualname = (
            f"{source.qualname}.<locals>.{raw_name}"
            if source.qualname
            else f"{source.id}.<locals>.{raw_name}"
        )
        evidence = binding.get("evidence")
        return Node(
            id=f"local:{current_binding_id}",
            kind=kind,
            name=raw_name,
            qualname=qualname,
            path=source.path,
            start_line=self._int_or_none(binding.get("line")),
            end_line=self._int_or_none(
                evidence.get("end_line") if isinstance(evidence, dict) else None
            ),
            properties={"local_definition": True, "scope": source.id},
        )

    def _local_callable_alias_target(
        self,
        source: Node,
        raw_name: str,
        context: _CallResolutionContext,
    ) -> _ResolvedCallTarget | None:
        if source.kind not in {"function", "method"} or "." in raw_name:
            return None
        bindings = source.properties.get("bindings", [])
        if not isinstance(bindings, list):
            return None
        matches = [
            binding
            for binding in bindings
            if isinstance(binding, dict)
            and binding.get("name") == raw_name
            and binding.get("kind") in {"assignment", "annotated_assignment"}
        ]
        if len(matches) != 1:
            return None
        value = self._str_or_none(matches[0].get("value"))
        if not value or value == raw_name:
            return None
        position = context.callsite_position
        if (
            position is not None
            and position[0] == source.id
            and not binding_in_effect(matches[0], position)
        ):
            # Before its assignment ends the name is not yet the alias.
            return None
        parsed = self._parse_expression(value)
        if isinstance(parsed, ast.Name):
            # helper = str: the name it is bound to, read as a bare call is,
            # unless that name is itself a local value of this scope; a class
            # method's own cls is its class, as cls() is.
            if parsed.id == "cls" and context.lexical.own_class_receiver(source):
                class_target = self._class_constructor_target(source, context)
                if class_target is None:
                    return None
                return _ResolvedCallTarget(
                    target=class_target,
                    strategy="local_callable_alias",
                    candidate_count=1,
                )
            if self._called_local_value(source, parsed.id, context):
                return None
            named = self._resolve_name_target(source, parsed.id, context)
            if named is None:
                return None
            return _ResolvedCallTarget(
                target=named.target,
                strategy="local_callable_alias",
                candidate_count=named.candidate_count,
                confidence=named.confidence,
                edge_kind=named.edge_kind,
            )
        if not isinstance(parsed, ast.Attribute):
            return None
        resolved = self._resolve_expression_target(source, parsed, context)
        if resolved is None:
            return None
        return _ResolvedCallTarget(
            target=resolved.target,
            strategy="local_callable_alias",
            candidate_count=resolved.candidate_count,
            confidence=resolved.confidence,
            edge_kind=resolved.edge_kind,
            receiver_expression=resolved.receiver_expression,
            receiver_type=resolved.receiver_type,
            receiver_type_ref_id=resolved.receiver_type_ref_id,
        )

    def _resolve_expression_target(
        self,
        source: Node,
        node: ast.AST,
        context: _CallResolutionContext,
    ) -> _ResolvedCallTarget | None:
        if not isinstance(node, ast.Attribute):
            return None
        receiver_expression = self._unparse(node.value)
        return self._resolve_target_v2(
            source,
            {
                "name": self._name(node) or self._unparse(node),
                "receiver": receiver_expression,
                "attribute": node.attr,
            },
            context,
        )

    def _semantic_callsite_target(
        self,
        source: Node,
        callsite: dict[str, Any],
        context: _CallResolutionContext,
    ) -> _ResolvedCallTarget | None:
        if self._is_config_callsite(callsite):
            return self._generated_config_target(source, callsite)
        if self._is_declaration_callsite(callsite):
            return self._generated_declaration_target(source, callsite)
        if self._is_dynamic_callsite(callsite):
            return self._generated_dynamic_target(source, callsite, context)
        return None

    def _edge_kind(
        self,
        source: Node,
        callsite: dict[str, Any],
        resolved: _ResolvedCallTarget,
    ) -> str:
        if not self.enable_v2:
            return "calls"
        if resolved.edge_kind == "dynamic_call":
            return resolved.edge_kind
        if self._is_logger_call(callsite, resolved):
            return "logs"
        if self._is_config_callsite(callsite):
            return "configures"
        if self._is_declaration_callsite(callsite):
            return "declares"
        context = self._str_or_none(callsite.get("context"))
        if context in DEFINITION_TIME_CONTEXTS:
            return "initializes"
        if resolved.target.kind == "class":
            return "constructs"
        if resolved.edge_kind:
            return resolved.edge_kind
        if resolved.target.kind in EXTERNAL_TARGET_KINDS:
            return "uses"
        return "calls"

    def _is_logger_call(
        self, callsite: dict[str, Any], resolved: _ResolvedCallTarget
    ) -> bool:
        attribute = self._str_or_none(callsite.get("attribute"))
        if attribute not in LOGGER_METHODS:
            return False
        receiver = (resolved.receiver_expression or "").lower()
        if receiver.endswith("logger") or receiver in {"log", "logging"}:
            return True
        receiver_type = (
            resolved.receiver_type or resolved.target.qualname or ""
        ).lower()
        return "logger" in receiver_type

    def _known_type_lacks_method(
        self,
        source: Node,
        receiver_expression: str,
        method_name: str,
        context: _CallResolutionContext,
    ) -> bool:
        receiver_type = self._receiver_type_ref(source, receiver_expression, context)
        if union_alternatives(receiver_type) is not None:
            return False
        type_id = self._type_id(receiver_type)
        if type_id is None:
            return False
        if type_id == NEVER_TYPE_ID:
            # A call that always raises gives no value to call a method on.
            return True
        if type_id.startswith("builtin:"):
            known = BUILTIN_METHODS_BY_TYPE.get(type_id.removeprefix("builtin:"))
        else:
            known = EXTERNAL_METHODS_BY_TYPE.get(type_id)
        return known is not None and method_name not in known

    def _external_method_target(
        self,
        type_ref: dict[str, Any] | None,
        method_name: str,
    ) -> Node | None:
        type_id = self._type_id(type_ref)
        if not type_id or not type_id.startswith("extsym:"):
            return None
        known = EXTERNAL_METHODS_BY_TYPE.get(type_id)
        if known is not None and method_name not in known:
            # The type is known not to have the method.
            return None
        qualname = f"{type_id.removeprefix('extsym:')}.{method_name}"
        return Node(
            id=f"extsym:{qualname}",
            kind="external_symbol",
            name=method_name,
            qualname=qualname,
            properties={
                "source": "receiver_type",
                "receiver_type": type_id,
                "boundary": "external",
            },
        )

    def _builtin_method_target(
        self,
        type_ref: dict[str, Any] | None,
        method_name: str,
    ) -> Node | None:
        type_id = self._type_id(type_ref)
        if not type_id or not type_id.startswith("builtin:"):
            return None
        builtin_type = type_id.removeprefix("builtin:")
        if method_name not in BUILTIN_METHODS_BY_TYPE.get(builtin_type, set()):
            return None
        return self._external_symbol_node(
            f"builtins.{builtin_type}.{method_name}",
            name=method_name,
            source="builtin_receiver_type",
            receiver_type=type_id,
        )

    def _common_boundary_method_target(
        self,
        receiver_expression: str,
        method_name: str,
    ) -> Node | None:
        if receiver_expression in {"self", "cls"}:
            return None
        owner = COMMON_BOUNDARY_METHOD_TARGETS.get(method_name)
        if owner is None:
            return None
        receiver_tail = self._receiver_tail(receiver_expression)
        if owner in {
            "collections.abc.Mapping",
            "collections.abc.MutableMapping",
        } and not self._looks_like_mapping_receiver(receiver_tail):
            return None
        if owner == "collections.abc.MutableSequence" and not (
            self._looks_like_sequence_receiver(receiver_tail)
        ):
            return None
        if owner == "builtins.set" and not self._looks_like_set_receiver(receiver_tail):
            return None
        if method_name in {"decode", "encode"} and not self._looks_like_text_receiver(
            receiver_tail
        ):
            return None
        return self._guessed_symbol(owner, method_name, source="common_boundary_method")

    @staticmethod
    def _protocol_symbol(owner: str, method_name: str) -> Node | None:
        """A guessed method of a protocol with no module to import, such as
        PEP 249's, named by the protocol class that defines it."""

        if method_name not in PEP249_METHODS_BY_CLASS.get(owner, frozenset()):
            return None
        qualname = f"{owner}.{method_name}"
        return Node(
            id=f"protocol:{qualname}",
            kind="protocol_symbol",
            name=method_name,
            qualname=qualname,
            properties={
                "source": "receiver_name_boundary_method",
                "boundary": "protocol",
                "protocol": "PEP 249",
            },
        )

    def _guessed_symbol(
        self,
        owner: str,
        method_name: str,
        *,
        source: str = "receiver_name_boundary_method",
    ) -> Node | None:
        """The external method a name-based guess links: only one listed in
        GUESSED_METHODS_BY_OWNER, whose entries are checked to exist."""

        if method_name not in GUESSED_METHODS_BY_OWNER.get(owner, frozenset()):
            return None
        return self._external_symbol_node(
            f"{owner}.{method_name}", name=method_name, source=source
        )

    @staticmethod
    def _receiver_tail(receiver_expression: str) -> str:
        return receiver_expression.strip().lower().rsplit(".", 1)[-1].strip("_")

    @classmethod
    def _looks_like_mapping_receiver(cls, receiver_tail: str) -> bool:
        return receiver_tail in COMMON_MAPPING_RECEIVER_NAMES or receiver_tail.endswith(
            COMMON_MAPPING_RECEIVER_SUFFIXES
        )

    @classmethod
    def _looks_like_sequence_receiver(cls, receiver_tail: str) -> bool:
        return (
            receiver_tail in COMMON_SEQUENCE_RECEIVER_NAMES
            or receiver_tail.endswith(COMMON_SEQUENCE_RECEIVER_SUFFIXES)
        )

    @classmethod
    def _looks_like_set_receiver(cls, receiver_tail: str) -> bool:
        return receiver_tail in COMMON_SET_RECEIVER_NAMES or receiver_tail.endswith(
            COMMON_SET_RECEIVER_SUFFIXES
        )

    @classmethod
    def _looks_like_text_receiver(cls, receiver_tail: str) -> bool:
        return receiver_tail in COMMON_TEXT_RECEIVER_NAMES or receiver_tail.endswith(
            COMMON_TEXT_RECEIVER_SUFFIXES
        )

    def _receiver_name_boundary_method_target(
        self,
        source: Node,
        receiver_expression: str,
        method_name: str,
        context: _CallResolutionContext,
    ) -> Node | None:
        if receiver_expression in {"self", "cls"}:
            return None
        receiver = receiver_expression.strip()
        receiver_lower = receiver.lower()
        receiver_tail = receiver_lower.rsplit(".", 1)[-1]

        service_container_target = self._service_container_method_target(
            receiver_tail,
            method_name,
            context,
        )
        if service_container_target is not None:
            return service_container_target

        project_method_target = self._receiver_name_project_method_target(
            receiver_tail,
            method_name,
            context,
        )
        if project_method_target is not None:
            return project_method_target

        if method_name in LOGGER_METHODS and self._looks_like_logger_receiver(
            receiver_tail
        ):
            return self._guessed_symbol("logging.Logger", method_name)

        if method_name in FASTAPI_ROUTE_METHODS and self._looks_like_router_receiver(
            receiver_tail
        ):
            return self._guessed_symbol("fastapi.APIRouter", method_name)

        if (
            method_name in SQLALCHEMY_SESSION_METHODS
            and self._looks_like_session_receiver(receiver_tail)
        ):
            return self._guessed_symbol("sqlalchemy.orm.Session", method_name)

        if (
            method_name in SQLALCHEMY_STATEMENT_METHODS
            and self._looks_like_statement_receiver(receiver_lower)
        ):
            return self._guessed_symbol(
                SQLALCHEMY_STATEMENT_METHOD_OWNERS[method_name], method_name
            )

        if (
            method_name in SQLALCHEMY_RESULT_METHODS
            and self._looks_like_result_receiver(receiver_lower)
        ):
            return self._guessed_symbol("sqlalchemy.engine.Result", method_name)

        if (
            method_name in PROMETHEUS_METRIC_METHODS
            and self._looks_like_prometheus_receiver(receiver)
        ):
            return self._guessed_symbol(
                PROMETHEUS_METRIC_METHOD_OWNERS[method_name], method_name
            )

        if method_name in REDIS_METHODS and self._looks_like_redis_receiver(
            receiver_tail
        ):
            return self._guessed_symbol("redis.asyncio.Redis", method_name)

        if method_name in ARGPARSE_METHODS and (
            self._looks_like_argparse_receiver(receiver_tail)
            or (
                method_name in _ARGPARSE_UNIQUE_METHODS
                and self._looks_like_argparse_receiver_broad(receiver_tail)
            )
        ):
            return self._guessed_symbol(
                ARGPARSE_METHOD_OWNERS[method_name], method_name
            )

        if method_name in HTTP_CLIENT_METHODS and self._looks_like_http_receiver(
            receiver_tail
        ):
            return self._guessed_symbol("httpx.Client", method_name)

        if method_name in DB_CONNECTION_METHODS and self._looks_like_db_receiver(
            receiver_tail
        ):
            owner = DB_METHOD_OWNERS[method_name]
            looks_like_cursor = "cursor" in receiver_tail
            if method_name == "close" and looks_like_cursor:
                owner = "pep249.Cursor"
            if method_name in {"fetchall", "fetchone"} and not looks_like_cursor:
                # A connection has no fetch method, nor a driver shortcut for
                # one as it has for execute.
                return None
            return self._protocol_symbol(owner, method_name)

        if method_name == "add_recognizer" and receiver_tail == "registry":
            return self._guessed_symbol(
                "presidio_analyzer.RecognizerRegistry", method_name
            )

        if method_name == "exec_module" and receiver_tail == "loader":
            return self._guessed_symbol("importlib.abc.InspectLoader", method_name)

        if method_name == "isoformat" and self._looks_like_datetime_receiver(
            receiver_lower
        ):
            return self._guessed_symbol("datetime.datetime", method_name)

        if method_name in _MONKEYPATCH_METHODS and receiver_tail == "monkeypatch":
            return self._guessed_symbol("pytest.MonkeyPatch", method_name)

        if method_name in HTTP_CLIENT_METHODS and (
            self._looks_like_test_client(receiver_tail)
            or (receiver_tail == "client" and self._is_test_file(source.path))
        ):
            return self._guessed_symbol("starlette.testclient.TestClient", method_name)

        # --- re.Match methods ---
        if method_name in _RE_MATCH_METHODS and receiver_tail == "match":
            return self._guessed_symbol("re.Match", method_name)

        # --- asyncio.Task methods ---
        if method_name in {"cancel", "done", "result"} and receiver_tail == "task":
            return self._guessed_symbol("asyncio.Task", method_name)

        # --- networkx.Graph methods ---
        if method_name in _NETWORKX_GRAPH_METHODS and receiver_tail == "graph":
            return self._guessed_symbol(
                NETWORKX_GRAPH_METHOD_OWNERS[method_name], method_name
            )

        # --- pytest benchmark / item ---
        if method_name == "pedantic" and receiver_tail == "benchmark":
            return self._guessed_symbol(
                "pytest_benchmark.fixture.BenchmarkFixture", method_name
            )

        if method_name == "add_marker" and receiver_tail == "item":
            return self._guessed_symbol("pytest.Item", method_name)

        # --- click.testing.CliRunner ---
        if method_name == "invoke" and receiver_tail == "runner":
            return self._guessed_symbol("click.testing.CliRunner", method_name)

        # --- typer / FastAPI app ---
        if method_name in _TYPER_APP_METHODS and (
            receiver_tail in {"app", "export_app"} or receiver_tail.endswith("_app")
        ):
            return self._guessed_symbol("typer.Typer", method_name)

        if method_name in _FASTAPI_APP_METHODS and receiver_tail == "app":
            return self._guessed_symbol("fastapi.FastAPI", method_name)

        if method_name in _CLICK_GROUP_METHODS and self._looks_like_click_receiver(
            receiver_tail
        ):
            return self._guessed_symbol("click.Group", method_name)

        # --- pydantic model_validate ---
        if method_name == "model_validate" and receiver_tail.endswith("model"):
            return self._guessed_symbol("pydantic.BaseModel", method_name)

        # --- ast.NodeVisitor ---
        if method_name == "generic_visit" and receiver_expression == "self":
            return self._guessed_symbol("ast.NodeVisitor", method_name)

        # --- presidio recognizer ---
        if method_name == "analyze" and receiver_tail in {
            "recognizer",
            "analyzer",
        }:
            return self._guessed_symbol("presidio_analyzer.AnalyzerEngine", method_name)

        # --- psutil.Process ---
        if method_name == "memory_info" and receiver_tail == "process":
            return self._guessed_symbol("psutil.Process", method_name)

        # --- datetime.strftime ---
        if method_name == "strftime" and self._looks_like_datetime_receiver(
            receiver_lower
        ):
            return self._guessed_symbol("datetime.datetime", method_name)

        # --- SQLAlchemy column ordering ---
        if method_name in {"desc", "asc"} and receiver_tail.endswith(
            ("column", "expr", "field")
        ):
            return self._guessed_symbol("sqlalchemy.sql.ColumnElement", method_name)

        # --- file handle IO ---
        if method_name in {"write", "read", "readline", "readlines", "flush"} and (
            receiver_tail
            in {
                "buffer",
                "handle",
                "fh",
                "fp",
                "out",
                "outfile",
                "stream",
                "wfile",
                "f",
            }
        ):
            return self._guessed_symbol("typing.IO", method_name)

        # --- httpx/requests Response ---
        if method_name in {"raise_for_status", "json"} and receiver_tail == "response":
            return self._guessed_symbol("httpx.Response", method_name)

        return None

    _MOCK_RECEIVER_PREFIXES = (
        "mock",
        "Mock",
        "mocker",
        "patch",
        "spy",
        "stub",
    )

    def _mock_assert_method_target(
        self,
        source: Node,
        receiver_expression: str | None,
        method_name: str,
    ) -> Node | None:
        """Recognize unittest.mock assertion methods as external boundary symbols.

        Patterns like ``mock_session.execute.assert_called_once`` involve a mock
        object whose final attribute is a well-known assert method.  We resolve
        these as ``extsym:unittest.mock.Mock.<method>`` so they no longer count
        as unresolved chain calls.

        To avoid misclassifying production code, the strategy only activates
        when at least one of the following is true:

        * The source file is in a test module (directory segment ``tests/`` or
          ``test/``, or basename matching ``test_*.py`` / ``*_test.py`` /
          ``conftest.py``).
        * The root of the receiver expression starts with a mock-related prefix
          (``mock``, ``Mock``, ``mocker``, ``patch``, ``spy``, ``stub``).
        """
        if method_name not in MOCK_ASSERT_METHODS:
            return None

        # Guard: require test-file context or mock-prefixed receiver.
        in_test_file = self._is_test_file(source.path)
        receiver_root = (receiver_expression or "").split(".")[0].strip()
        has_mock_receiver = receiver_root.startswith(self._MOCK_RECEIVER_PREFIXES)
        if not in_test_file and not has_mock_receiver:
            return None

        return self._guessed_symbol(
            MOCK_ASSERT_METHOD_OWNERS[method_name],
            method_name,
            source="mock_assert_method",
        )

    @staticmethod
    def _is_test_file(path: str | None) -> bool:
        """Return True if *path* belongs to a test module.

        Uses path-segment matching rather than substring search so that
        production paths like ``contest/``, ``latest/``, ``attestation/``
        are not misidentified.
        """
        if not path:
            return False
        # Normalise to forward slashes for uniform splitting.
        normalised = path.replace("\\", "/").lower()
        segments = normalised.split("/")
        # Directory segment check: any segment is literally "tests" or "test".
        if any(seg in ("tests", "test") for seg in segments):
            return True
        # Basename check: test_*.py, *_test.py, conftest.py.
        basename = segments[-1] if segments else ""
        if basename.startswith("test_") and basename.endswith(".py"):
            return True
        if basename.endswith("_test.py"):
            return True
        if basename == "conftest.py":
            return True
        return False

    def _unique_method_fallback_target(
        self,
        source: Node,
        receiver_expression: str | None,
        method_name: str,
        context: _CallResolutionContext,
    ) -> Node | None:
        """Last-resort resolver: match when exactly one class defines the method.

        Iterates ``context.methods_by_class`` to find all classes that define
        a method named *method_name*.  If exactly one class matches **and**:

        1. The receiver root is NOT a known import alias, and
        2. The receiver name has lexical affinity with the candidate class name
           (e.g. ``service`` matches ``InternalService``, ``greeter`` matches
           ``Greeter``, but ``external`` does NOT match ``InternalService``).

        Returns ``None`` when the conditions are not met.
        """
        if not receiver_expression:
            return None
        receiver_root = receiver_expression.split(".")[0].strip()
        import_aliases = self._visible_import_aliases(source, context)
        if receiver_root in import_aliases:
            return None
        candidates = [
            (class_qualname, target)
            for (class_qualname, name), target in context.methods_by_class.items()
            if name == method_name
        ]
        if len(candidates) != 1:
            return None
        class_qualname, target = candidates[0]
        if not self._receiver_matches_class(receiver_root, class_qualname):
            return None
        return target

    @staticmethod
    def _receiver_matches_class(receiver_name: str, class_qualname: str) -> bool:
        """Check if receiver name has lexical affinity with a class name.

        Converts the class basename from PascalCase to snake_case segments,
        then checks if the receiver name matches any trailing segment
        combination.  For example:

        - ``service`` matches ``InternalService`` (segment: ``service``)
        - ``memory_service`` matches ``MemoryService`` (segments: ``memory_service``)
        - ``greeter`` matches ``Greeter`` (segment: ``greeter``)
        - ``external`` does NOT match ``InternalService``
        """
        class_basename = class_qualname.rsplit(".", 1)[-1]
        # Convert PascalCase to snake_case segments:
        # "InternalService" → ["internal", "service"]
        segments: list[str] = []
        current: list[str] = []
        for char in class_basename:
            if char.isupper() and current:
                segments.append("".join(current).lower())
                current = [char]
            else:
                current.append(char)
        if current:
            segments.append("".join(current).lower())
        if not segments:
            return False
        # Check if receiver matches any trailing segment combination:
        # ["internal", "service"] → try "service", then "internal_service"
        receiver_lower = receiver_name.lower().strip("_")
        for i in range(len(segments)):
            suffix = "_".join(segments[i:])
            if receiver_lower == suffix:
                return True
        return False

    _EXTENDS_RESULT = tuple[Node, str]  # (target, confidence)

    def _resolve_extends_target(
        self,
        class_node: Node,
        base_name: str,
        context: _CallResolutionContext,
    ) -> _EXTENDS_RESULT | None:
        """Resolve a class base name to a target Node for 'extends' edges.

        Returns ``(target_node, confidence)`` or ``None`` to skip.

        Resolution order:
        1. Skip ``object`` (implicit default, no useful edge).
        2. Normalize the base expression: strip ``[...]`` (subscript/generic)
           and ``(...)`` (call) to extract the root symbol name.
        3. Try same-module qualname lookup → ``confirmed``.
        4. Try import alias resolution → ``confirmed``.
        5. Try global unique class name match → ``heuristic``.
        6. Unresolved bare names → ``None`` (skip, no evidence to confirm).
        """
        # Normalize: strip subscript Generic[T] and call Base() notation
        simple_name = self._normalize_base_expression(base_name)
        if not simple_name or simple_name == "object":
            return None

        # 1. Same-module qualname
        if class_node.qualname:
            module = class_node.qualname.rsplit(".", 1)[0]
            local_qualname = f"{module}.{simple_name}"
            if local_qualname in context.by_qualname:
                return context.by_qualname[local_qualname], "confirmed"

        # 2. Import alias resolution
        import_aliases = self._visible_import_aliases(class_node, context)
        if simple_name in import_aliases:
            target_qualname = import_aliases[simple_name]
            if target_qualname in context.by_qualname:
                return context.by_qualname[target_qualname], "confirmed"
            # Import target is external — create extsym with confirmed
            return (
                self._external_symbol_node(
                    target_qualname,
                    name=simple_name,
                    source="extends_import",
                ),
                "confirmed",
            )
        # Dotted base like ``abc.ABC`` — check aliases for prefix
        if "." in simple_name:
            prefix = simple_name.split(".")[0]
            if prefix in import_aliases:
                resolved_module = import_aliases[prefix]
                rest = simple_name.split(".", 1)[1]
                ext_qualname = f"{resolved_module}.{rest}"
                if ext_qualname in context.by_qualname:
                    return context.by_qualname[ext_qualname], "confirmed"
                return (
                    self._external_symbol_node(
                        ext_qualname,
                        name=rest.rsplit(".", 1)[-1],
                        source="extends_import",
                    ),
                    "confirmed",
                )

        # 3. Global name lookup — match by basename across all classes
        candidates = [
            node
            for node in context.target_nodes
            if node.kind == "class"
            and node.name == simple_name
            and node.id != class_node.id
        ]
        if len(candidates) == 1:
            return candidates[0], "heuristic"

        # 4. Unresolved — no evidence, skip edge entirely
        return None

    @staticmethod
    def _normalize_base_expression(base_name: str) -> str:
        """Extract root symbol from a base class expression.

        Handles:
        - ``BaseRepository[Memory]`` → ``BaseRepository`` (subscript)
        - ``Generic[T]`` → ``Generic`` (generic)
        - ``Base()`` → ``Base`` (call)
        - ``abc.ABC`` → ``abc.ABC`` (dotted, preserved)
        """
        # Strip subscript [...] first, then call (...)
        name = base_name.split("[")[0].split("(")[0].strip()
        return name

    def _ast_node_visitor_method_target(
        self,
        source: Node,
        receiver_expression: str,
        method_name: str,
        context: _CallResolutionContext,
    ) -> Node | None:
        if receiver_expression != "self" or method_name not in {
            "generic_visit",
            "visit",
        }:
            return None
        class_qualname = self._class_qualname(source)
        if not class_qualname:
            return None
        class_target = context.by_qualname.get(class_qualname)
        bases = class_target.properties.get("bases", []) if class_target else []
        if not isinstance(bases, list):
            return None
        if not any(str(base).endswith("NodeVisitor") for base in bases):
            return None
        return self._external_symbol_node(
            f"ast.NodeVisitor.{method_name}",
            name=method_name,
            source="ast_node_visitor_method",
        )

    def _resolve_class_attribute_method(
        self,
        receiver_expression: str,
        method_name: str,
        context: _CallResolutionContext,
    ) -> Node | None:
        receiver = receiver_expression.strip()
        if "." in receiver:
            class_target = context.by_qualname.get(receiver)
        else:
            candidates = [
                candidate
                for candidate in context.by_name.get(receiver, [])
                if candidate.kind == "class"
            ]
            class_target = candidates[0] if len(candidates) == 1 else None
        if class_target is None or not class_target.qualname:
            return None
        return context.methods_by_class.get((class_target.qualname, method_name))

    def _service_container_method_target(
        self,
        receiver_tail: str,
        method_name: str,
        context: _CallResolutionContext,
    ) -> Node | None:
        if receiver_tail != "container":
            return None
        return context.methods_by_class.get(
            ("shared.service_container.ServiceContainer", method_name)
        )

    def _receiver_name_project_method_target(
        self,
        receiver_tail: str,
        method_name: str,
        context: _CallResolutionContext,
    ) -> Node | None:
        if not self._looks_like_project_receiver(receiver_tail):
            return None
        hint = self._project_receiver_hint(receiver_tail)
        if not hint:
            return None
        candidates = [
            target
            for (
                class_qualname,
                current_method,
            ), target in context.methods_by_class.items()
            if current_method == method_name
            and self._class_matches_receiver_hint(class_qualname, receiver_tail, hint)
        ]
        if len(candidates) != 1:
            return None
        return candidates[0]

    @staticmethod
    def _looks_like_logger_receiver(receiver_tail: str) -> bool:
        return receiver_tail in {"log", "logger", "logging"} or receiver_tail.endswith(
            "logger"
        )

    @staticmethod
    def _looks_like_router_receiver(receiver_tail: str) -> bool:
        return receiver_tail == "router" or receiver_tail.endswith("_router")

    @staticmethod
    def _looks_like_click_receiver(receiver_tail: str) -> bool:
        return receiver_tail in {"cli", "cmd", "command", "group", "ops"} or (
            receiver_tail.endswith("_cli")
            or receiver_tail.endswith("_command")
            or receiver_tail.endswith("_group")
        )

    @staticmethod
    def _looks_like_session_receiver(receiver_tail: str) -> bool:
        return receiver_tail == "session" or receiver_tail.endswith("_session")

    @staticmethod
    def _looks_like_statement_receiver(receiver_lower: str) -> bool:
        tail = receiver_lower.rsplit(".", 1)[-1]
        return (
            tail in {"stmt", "statement", "query", "base_stmt"}
            or tail.endswith("_stmt")
            or ".where(" in receiver_lower
            or ".order_by(" in receiver_lower
            or ".limit(" in receiver_lower
        )

    @staticmethod
    def _looks_like_result_receiver(receiver_lower: str) -> bool:
        tail = receiver_lower.rsplit(".", 1)[-1]
        if (
            tail == "result"
            or tail.endswith("_result")
            or ".scalars(" in receiver_lower
        ):
            return True
        if ".execute(" not in receiver_lower:
            return False
        # A connection or cursor named so is taken for PEP 249's, as the
        # database guess takes it, whose execute returns nothing defined.
        executor = receiver_lower.rsplit(".execute(", 1)[0].rsplit(".", 1)[-1]
        return not CallAnalyzer._looks_like_db_receiver(executor.strip("_"))

    @classmethod
    def _looks_like_prometheus_receiver(cls, receiver_expression: str) -> bool:
        tail = receiver_expression.rsplit(".", 1)[-1]
        if ".labels(" in receiver_expression:
            return True
        if not tail.isupper():
            return False
        return any(hint in tail for hint in PROMETHEUS_METRIC_NAME_HINTS)

    @staticmethod
    def _looks_like_redis_receiver(receiver_tail: str) -> bool:
        return receiver_tail in {"redis", "redis_client"} or receiver_tail.endswith(
            "_redis"
        )

    @staticmethod
    def _looks_like_argparse_receiver(receiver_tail: str) -> bool:
        return (
            receiver_tail in {"parser", "subparser", "subparsers"}
            or receiver_tail.endswith("_parser")
            or receiver_tail.endswith("_subparser")
            or receiver_tail.endswith("_subparsers")
        )

    @staticmethod
    def _looks_like_argparse_receiver_broad(receiver_tail: str) -> bool:
        """Broader argparse receiver check for unique methods like add_argument.

        Matches strict patterns plus names containing 'arg' or 'parser',
        and common argparse constructs like argument groups.
        """
        return (
            receiver_tail in {"parser", "subparser", "subparsers", "group"}
            or "parser" in receiver_tail
            or receiver_tail.endswith(("_args", "_arguments", "_group"))
        )

    @staticmethod
    def _looks_like_http_receiver(receiver_tail: str) -> bool:
        return receiver_tail in {
            "api_client",
            "async_client",
            "http_client",
            "test_client",
        } or receiver_tail.endswith(
            ("_api_client", "_async_client", "_http_client", "_test_client")
        )

    @staticmethod
    def _looks_like_test_client(receiver_tail: str) -> bool:
        """Match only explicitly test-related client patterns.

        Does NOT match generic patterns like ``github_client`` or ``http_client``
        — those are handled by ``_looks_like_http_receiver`` with ``httpx.Client``.
        """
        return (
            receiver_tail.startswith("client_with_")
            or receiver_tail == "test_client"
            or receiver_tail.endswith("_test_client")
        )

    @staticmethod
    def _looks_like_db_receiver(receiver_tail: str) -> bool:
        return receiver_tail in {
            "conn",
            "connection",
            "cursor",
            "db",
        } or receiver_tail.endswith(("_conn", "_connection", "_cursor"))

    @staticmethod
    def _looks_like_project_receiver(receiver_tail: str) -> bool:
        if (
            "mock" in receiver_tail
            or "fake" in receiver_tail
            or "stub" in receiver_tail
        ):
            return False
        return (
            receiver_tail.endswith("_service")
            or receiver_tail.endswith("_repo")
            or receiver_tail.endswith("_repository")
            or receiver_tail.endswith("_store")
            or receiver_tail.endswith("_splitter")
            or receiver_tail in {"repo", "repository", "store", "svc"}
        )

    @staticmethod
    def _project_receiver_hint(receiver_tail: str) -> str | None:
        for suffix in ("_repository", "_service", "_repo"):
            if receiver_tail.endswith(suffix):
                return receiver_tail.removesuffix(suffix).replace("_", "")
        for suffix in ("_store", "_splitter"):
            if receiver_tail.endswith(suffix):
                return receiver_tail.removesuffix(suffix).replace("_", "")
        if receiver_tail in {"repo", "repository", "store", "svc"}:
            return receiver_tail
        return None

    @staticmethod
    def _class_matches_receiver_hint(
        class_qualname: str,
        receiver_tail: str,
        hint: str,
    ) -> bool:
        class_name = class_qualname.rsplit(".", 1)[-1].lower()
        compact_class = class_name.replace("_", "")
        if receiver_tail in {"repo", "repository"}:
            return class_name.endswith("repository")
        if receiver_tail == "svc":
            return class_name.endswith("service")
        if receiver_tail == "store":
            return class_name.endswith("store")
        return hint in compact_class and class_name.endswith(
            ("repository", "service", "splitter", "store")
        )

    @staticmethod
    def _looks_like_datetime_receiver(receiver_lower: str) -> bool:
        tail = receiver_lower.rsplit(".", 1)[-1]
        return tail.endswith(("_at", "_time", "_date", "datetime", "timestamp"))

    def _builtin_type_attribute_target(
        self,
        receiver_expression: str,
        method_name: str,
    ) -> Node | None:
        owner = receiver_expression.strip()
        if method_name not in BUILTIN_TYPE_ATTRIBUTE_METHODS.get(owner, set()):
            return None
        return self._external_symbol_node(
            f"builtins.{owner}.{method_name}",
            name=method_name,
            source="builtin_type_attribute",
        )

    def _builtin_function_target(self, raw_name: str) -> Node | None:
        if raw_name not in BUILTIN_CALLS:
            return None
        return self._external_symbol_node(
            f"builtins.{raw_name}",
            name=raw_name,
            source="builtin_function",
        )

    def _import_alias_target(
        self,
        source: Node,
        raw_name: str,
        context: _CallResolutionContext,
        *,
        strategy: str = "import_alias",
    ) -> _ResolvedCallTarget | None:
        parts = raw_name.split(".")
        if not parts or not all(part.isidentifier() for part in parts):
            # Only a dotted name is an import path; hashlib.sha256(data).hexdigest
            # is a call on a value, not a symbol of hashlib.
            return None
        aliases = self._visible_import_aliases(source, context)
        resolved_base = aliases.get(parts[0])
        if not resolved_base:
            return None
        if (
            source.id not in context.lexical.strict
            # A function's or class's own import is not the module's; the
            # module's own imports are.
            and (
                source.kind == "module"
                or parts[0] not in context.import_aliases_by_scope.get(source.id, {})
            )
            and not self._module_import_holds(source, parts[0], resolved_base, context)
        ):
            # Where it is read, the module's name may hold another binding:
            # an assignment after the import, or a def in its except.
            return None
        qualname = ".".join([resolved_base, *parts[1:]])
        return self._imported_qualname_target(qualname, context, strategy)

    def _imported_qualname_target(
        self, qualname: str, context: _CallResolutionContext, strategy: str
    ) -> _ResolvedCallTarget:
        internal_target = context.by_qualname.get(qualname)
        if internal_target is not None:
            return _ResolvedCallTarget(
                target=internal_target,
                strategy=strategy,
                candidate_count=1,
                confidence="confirmed",
            )
        return _ResolvedCallTarget(
            target=self._external_symbol_node(
                qualname,
                name=qualname.rsplit(".", 1)[-1],
                source="import_alias",
            ),
            strategy=strategy,
            candidate_count=1,
            confidence="heuristic",
            edge_kind="uses",
        )

    def _visible_import_aliases(
        self,
        source: Node,
        context: _CallResolutionContext,
    ) -> dict[str, str]:
        if source.id in context.lexical.strict:
            return context.lexical.aliases(source)
        aliases: dict[str, str] = {}
        if source.path:
            aliases.update(context.module_import_aliases_by_path.get(source.path, {}))
        aliases.update(context.import_aliases_by_scope.get(source.id, {}))
        return aliases

    @staticmethod
    def _external_symbol_node(
        qualname: str,
        *,
        name: str,
        source: str,
        receiver_type: str | None = None,
    ) -> Node:
        properties: dict[str, Any] = {
            "source": source,
            "boundary": "external",
        }
        if receiver_type:
            properties["receiver_type"] = receiver_type
        return Node(
            id=f"extsym:{qualname}",
            kind="external_symbol",
            name=name,
            qualname=qualname,
            properties=properties,
        )

    def _generated_config_target(
        self, source: Node, callsite: dict[str, Any]
    ) -> _ResolvedCallTarget:
        key = self._config_key(callsite)
        if key is None:
            subject = callsite["_stable_callsite_subject"]
            identity = callsite_id(
                *stable_callsite_subject_key(subject),
                callsite["_stable_callsite_occurrence"],
            ).removeprefix("callsite:")
            node_id = f"config:dynamic:{identity}"
            name = "unknown configuration key"
            properties = {"key_resolution": "unknown", "source_scope": source.id}
        else:
            node_id = f"config:{key}"
            name = key
            properties = {"config_kind": "env", "key": key.removeprefix("env:")}
        target = Node(
            id=node_id,
            kind="config",
            name=name,
            qualname=key or node_id,
            properties=properties,
        )
        return _ResolvedCallTarget(
            target=target,
            strategy="config_access",
            candidate_count=1,
            confidence="heuristic",
            edge_kind="configures",
        )

    def _generated_declaration_target(
        self, source: Node, callsite: dict[str, Any]
    ) -> _ResolvedCallTarget:
        raw_name = str(callsite.get("name"))
        current_callsite_id = self._callsite_id(source, callsite, raw_name)
        short_name = self._short_name(raw_name)
        target = Node(
            id=f"decl:{current_callsite_id.removeprefix('callsite:')}",
            kind="declaration",
            name=short_name,
            qualname=f"{source.qualname or source.id}.{short_name}",
            path=source.path,
            start_line=self._int_or_none(callsite.get("line")),
            properties={
                "call": callsite.get("call_expression") or raw_name,
                "reason": "class_body_declaration",
                "raw_expression": raw_name,
                "source_scope": source.id,
                "stable_callsite_subject": callsite["_stable_callsite_subject"],
                "stable_callsite_occurrence": callsite["_stable_callsite_occurrence"],
            },
        )
        return _ResolvedCallTarget(
            target=target,
            strategy="declaration_call",
            candidate_count=1,
            confidence="heuristic",
            edge_kind="declares",
        )

    def _generated_dynamic_target(
        self,
        source: Node,
        callsite: dict[str, Any],
        context: _CallResolutionContext,
    ) -> _ResolvedCallTarget:
        raw_name = str(callsite.get("name"))
        current_callsite_id = self._callsite_id(source, callsite, raw_name)
        receiver_expression = self._str_or_none(callsite.get("receiver"))
        receiver_type = (
            self._receiver_type_ref(source, receiver_expression, context)
            if receiver_expression
            else None
        )
        alternatives = union_alternatives(receiver_type)
        if receiver_type and receiver_type.get("untyped_comprehension"):
            alternatives = None
        target = Node(
            id=f"unresolved:{current_callsite_id.removeprefix('callsite:')}",
            kind="diagnostic",
            name=raw_name,
            path=source.path,
            start_line=self._int_or_none(callsite.get("line")),
            properties={
                "diagnostic_kind": "unresolved_dynamic_callsite",
                "callsite_id": current_callsite_id,
                "call": callsite.get("call_expression") or raw_name,
                "reason": self._dynamic_reason(callsite),
                "source_scope": source.id,
                "raw_expression": raw_name,
                "stable_callsite_subject": callsite["_stable_callsite_subject"],
                "stable_callsite_occurrence": callsite["_stable_callsite_occurrence"],
            },
        )
        if receiver_type and receiver_type.get("typed_value_evidence"):
            target.properties["reason"] = "known_value_method_unavailable"
        if receiver_type and receiver_type.get("untyped_comprehension"):
            target.properties["reason"] = "unknown_comprehension_element"
        if alternatives is not None:
            target.properties["reason"] = (
                "ambiguous_union_receiver"
                if not self._type_id(receiver_type)
                else "unresolved_union_receiver"
            )
            target.properties["receiver_type_options"] = alternatives
        self._set_optional(
            target.properties,
            "receiver_expression",
            receiver_expression,
        )
        self._set_optional(
            target.properties,
            "receiver_type",
            self._type_id(receiver_type),
        )
        return _ResolvedCallTarget(
            target=target,
            strategy=(
                "union_receiver" if alternatives is not None else "dynamic_dispatch"
            ),
            candidate_count=0,
            confidence="unresolved",
            resolution_status="unresolved",
            edge_kind="dynamic_call",
            receiver_expression=receiver_expression,
            receiver_type=self._type_id(receiver_type),
            receiver_type_ref_id=self._type_ref_id(receiver_type),
        )

    def _is_config_callsite(self, callsite: dict[str, Any]) -> bool:
        raw_name = self._str_or_none(callsite.get("name"))
        if not raw_name:
            return False
        normalized = raw_name.strip()
        short = self._short_name(normalized)
        if short in CONFIG_CALLS or normalized in CONFIG_ATTRIBUTE_CALLS:
            return True
        receiver = (self._str_or_none(callsite.get("receiver")) or "").lower()
        if receiver in {"os.environ", "environ"} and short == "get":
            return True
        return "settings" in normalized.lower() and short.startswith("get")

    def _is_declaration_callsite(self, callsite: dict[str, Any]) -> bool:
        context = self._str_or_none(callsite.get("context"))
        if context != "class_body":
            return False
        raw_name = self._str_or_none(callsite.get("name"))
        if not raw_name:
            return False
        return self._short_name(raw_name) in DECLARATION_CALLS

    def _is_dynamic_callsite(self, callsite: dict[str, Any]) -> bool:
        raw_name = self._str_or_none(callsite.get("name"))
        if not raw_name:
            return False
        short = self._short_name(raw_name)
        if short in DYNAMIC_CALLS or raw_name.startswith("getattr("):
            return True
        if raw_name.endswith(".import_module"):
            return True
        return bool(callsite.get("receiver") and callsite.get("attribute"))

    def _dynamic_reason(self, callsite: dict[str, Any]) -> str:
        raw_name = str(callsite.get("name") or "")
        if raw_name.startswith("getattr(") or self._short_name(raw_name) == "getattr":
            return "dynamic_attribute_name"
        if raw_name.endswith(".import_module") or raw_name == "__import__":
            return "dynamic_import"
        return "unknown_receiver_type"

    def _config_key(self, callsite: dict[str, Any]) -> str | None:
        expression = self._str_or_none(callsite.get("call_expression"))
        if not expression:
            return None
        parsed = self._parse_expression(expression)
        if not isinstance(parsed, ast.Call) or not parsed.args:
            return None
        first_arg = parsed.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            return f"env:{first_arg.value}"
        return None

    @staticmethod
    def _evidence_kind(
        source: Node,
        callsite: dict[str, Any],
        edge_kind: str,
    ) -> str:
        if edge_kind == "configures":
            return "ast_config_call"
        if edge_kind == "declares":
            return "ast_declaration_call"
        if edge_kind == "dynamic_call":
            return "ast_dynamic_call"
        if edge_kind == "logs":
            return "ast_log_call"
        if edge_kind == "constructs":
            return "ast_constructor_call"
        if edge_kind == "initializes":
            return "ast_definition_time_call"
        if callsite.get("receiver"):
            return "ast_attribute_call"
        if source.kind == "module":
            return "ast_module_call"
        if source.kind == "class":
            return "ast_class_body_call"
        return "ast_call"

    def _callsite_properties(
        self,
        source: Node,
        callsite: dict[str, Any],
        resolved: _ResolvedCallTarget,
        edge_kind: str,
    ) -> dict[str, Any]:
        raw_name = str(callsite.get("name"))
        properties: dict[str, Any] = {
            "callsite_id": self._callsite_id(source, callsite, raw_name),
            "raw_expression": raw_name,
            "context": callsite.get("context"),
            "edge_kind": edge_kind,
            "resolution_strategy": resolved.strategy,
            "candidate_count": resolved.candidate_count,
            "stable_callsite_subject": callsite["_stable_callsite_subject"],
            "stable_callsite_occurrence": callsite["_stable_callsite_occurrence"],
        }
        self._set_optional(
            properties, "call_expression", callsite.get("call_expression")
        )
        # Where the call is, which orders the calls a merged edge stands for
        # as the source does.
        self._set_optional(properties, "line", self._int_or_none(callsite.get("line")))
        self._set_optional(
            properties, "column", self._int_or_none(callsite.get("column"))
        )
        self._set_optional(
            properties, "receiver_expression", resolved.receiver_expression
        )
        self._set_optional(properties, "receiver_type", resolved.receiver_type)
        self._set_optional(
            properties, "receiver_type_ref_id", resolved.receiver_type_ref_id
        )
        self._set_optional(properties, "attribute", callsite.get("attribute"))
        self._set_optional(
            properties, "semantic_reason", resolved.target.properties.get("reason")
        )
        self._set_optional(
            properties,
            "target_symbol",
            (
                resolved.target.qualname
                if resolved.target.kind in EXTERNAL_TARGET_KINDS
                else None
            ),
        )
        return properties

    @staticmethod
    def _fallbacks(strategy: str) -> list[str]:
        if strategy == "receiver_type":
            return ["annotation", "assignment", "provider_return_annotation"]
        if strategy == "external_receiver_type":
            return ["annotation", "external_symbol_boundary"]
        if strategy == "builtin_receiver_type":
            return ["annotation", "builtin_type_boundary"]
        if strategy == "builtin_function":
            return ["language_builtin"]
        if strategy == "common_boundary_method":
            return ["common_python_method_boundary"]
        if strategy == "receiver_name_boundary_method":
            return ["receiver_name_boundary"]
        if strategy in {"import_alias", "imported_module_attribute"}:
            return ["import_alias"]
        if strategy == "same_class_receiver":
            return ["same_class_method"]
        if strategy == "class_attribute_method":
            return ["class_attribute_method"]
        if strategy == "ast_node_visitor_method":
            return ["ast_node_visitor_method"]
        if strategy == "class_receiver_constructor":
            return ["classmethod_receiver"]
        if strategy == "local_definition":
            return ["local_definition_binding"]
        if strategy == "local_callable_alias":
            return ["local_callable_alias"]
        if strategy == "unique_short_name_fallback":
            return ["short_name"]
        if strategy == "config_access":
            return ["config_access_pattern"]
        if strategy == "declaration_call":
            return ["declaration_call_pattern"]
        if strategy == "dynamic_dispatch":
            return ["dynamic_dispatch_pattern"]
        return [strategy]

    @staticmethod
    def _by_name(nodes: list[Node]) -> dict[str, list[Node]]:
        by_name: dict[str, list[Node]] = defaultdict(list)
        for node in nodes:
            by_name[node.name].append(node)
        return by_name

    def _resolve_target(
        self,
        source: Node,
        callsite: dict[str, Any],
        by_name: dict[str, list[Node]],
        by_qualname: dict[str, Node],
        *,
        after_v2: bool = False,
        context: _CallResolutionContext | None = None,
    ) -> Node | None:
        raw_name = callsite.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            return None

        receiver_expression = self._str_or_none(callsite.get("receiver"))
        if source.kind == "method" and (
            raw_name.startswith("self.") or raw_name.startswith("cls.")
        ):
            class_qualname = self._class_qualname(source)
            if class_qualname:
                method_name = raw_name.split(".", 1)[1]
                target = by_qualname.get(f"{class_qualname}.{method_name}")
                if target:
                    return target

        exact_target = by_qualname.get(raw_name)
        if exact_target and not (receiver_expression and exact_target.id == source.id):
            return exact_target
        if (
            after_v2
            and receiver_expression
            and receiver_expression
            not in {
                "self",
                "cls",
            }
        ):
            # Receiver resolution has found no target for this method call,
            # and its name alone does not say which function it is:
            # item["targets"].add() is not the one function named add in this
            # module or in the project. Legacy mode keeps its name matching.
            return None

        same_module_target = self._resolve_same_module(
            source,
            raw_name,
            by_name,
            context.callsite_position if context else None,
            context.lexical if context else None,
        )
        if same_module_target and not (
            receiver_expression and same_module_target.id == source.id
        ):
            return same_module_target

        if "." in raw_name:
            return None
        if context is not None and not receiver_expression:
            brought = self._star_brought_node(source, raw_name, context)
            if brought is not None:
                return brought
        if (
            context is not None
            and not receiver_expression
            and (
                self._module_binds(source, raw_name, context, imports_allowed=True)
                or self._star_imports_exclude(
                    source,
                    raw_name,
                    self._module_name_at(
                        source, raw_name, context.lexical, context.callsite_position
                    ),
                    context.lexical,
                )
            )
        ):
            return None

        short_name = raw_name.rsplit(".", 1)[-1]
        candidates = [
            candidate
            for candidate in by_name.get(short_name, [])
            if not (receiver_expression and candidate.id == source.id)
            # A bare name reaches no method, nor a class nested in another,
            # nor a definition of its own module that was refused above.
            and (
                receiver_expression
                or self._is_module_level(candidate)
                and self._module_qualname(candidate) != self._module_qualname(source)
            )
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _super_method_target(
        self,
        source: Node,
        receiver_expression: str,
        method: str,
        context: _CallResolutionContext,
    ) -> _ResolvedCallTarget | None:
        """``super().method()`` in a method: the method past the method's
        class in that class's order; ``super(C, obj).method()``: the method
        past C in the order of obj's class, when obj's class is known."""

        if not receiver_expression.startswith("super("):
            return None
        if context.lexical.lookup(source, "super") is not None:
            # A local, parameter or import named super is not the builtin.
            return None
        if receiver_expression == "super()":
            if source.kind != "method":
                return None
            class_node = context.by_qualname.get(self._class_qualname(source) or "")
            instance_class = class_node
        else:
            arguments = self._explicit_super_arguments(receiver_expression)
            if arguments is None:
                return None
            class_node = self._explicit_super_class(source, arguments[0], context)
            instance_class = self._super_instance_class(source, arguments[1], context)
        if (
            class_node is None
            or class_node.kind != "class"
            or instance_class is None
            or instance_class.kind != "class"
        ):
            return None
        target = self._inherited_method_target(
            {"type_id": instance_class.id}, method, context, after=class_node.id
        )
        if target is None:
            return None
        return _ResolvedCallTarget(
            target=target,
            strategy="super_receiver",
            candidate_count=1,
            receiver_expression=receiver_expression,
        )

    @staticmethod
    def _explicit_super_arguments(
        receiver_expression: str,
    ) -> tuple[str, ast.expr] | None:
        """The class name and the object of ``super(C, obj)``."""

        try:
            parsed = ast.parse(receiver_expression, mode="eval").body
        except SyntaxError:
            return None
        if not (
            isinstance(parsed, ast.Call)
            and isinstance(parsed.func, ast.Name)
            and len(parsed.args) == 2
            and isinstance(parsed.args[0], ast.Name)
            and not isinstance(parsed.args[1], ast.Starred)
        ):
            return None
        return parsed.args[0].id, parsed.args[1]

    @staticmethod
    def _explicit_super_class(
        source: Node,
        name: str,
        context: _CallResolutionContext,
    ) -> Node | None:
        """The project class C of ``super(C, obj)``, by a stable binding of
        its name, defined in the module or imported."""

        found = context.lexical.lookup(source, name)
        if found is None or not context.lexical.stable(*found):
            return None
        binding: dict[str, Any] = found[1][0]
        if binding.get("kind") == "class_definition":
            return context.by_id.get(str(binding.get("target", "")))
        if binding.get("kind") == "import_alias":
            qualified = str(binding.get("target_qualname", ""))
            return context.by_id.get(
                exported_class(qualified, context.lexical.nodes) or f"class:{qualified}"
            )
        return None

    def _super_instance_class(
        self,
        source: Node,
        instance: ast.expr,
        context: _CallResolutionContext,
    ) -> Node | None:
        """The class whose order ``super(C, obj)`` follows: the method's class
        for its own self or cls, otherwise the known class of obj."""

        if isinstance(instance, ast.Name) and context.lexical.own_receiver(
            source, instance.id
        ):
            return context.by_qualname.get(self._class_qualname(source) or "")
        type_id = self._type_id(self._receiver_type_ref_node(source, instance, context))
        return context.by_id.get(type_id or "")

    def _resolve_same_class_method(
        self,
        source: Node,
        method_name: str,
        context: _CallResolutionContext,
    ) -> Node | None:
        class_qualname = self._class_qualname(source)
        if class_qualname:
            return context.methods_by_class.get((class_qualname, method_name))
        return None

    def _resolve_same_module(
        self,
        source: Node,
        raw_name: str,
        by_name: dict[str, list[Node]],
        position: tuple[str, int, int] | None = None,
        lexical: LexicalScopes | None = None,
    ) -> Node | None:
        if "." in raw_name:
            return None

        module = self._module_qualname(source)
        if not module:
            return None

        candidates = [
            candidate
            for candidate in by_name.get(raw_name, [])
            if candidate.qualname and self._module_qualname(candidate) == module
        ]
        if source.kind == "class":
            if position is None or position[0] != source.id:
                return candidates[0] if len(candidates) == 1 else None
            # A class body runs in order: a name it has bound by the call is
            # its own; one it has not is the module's, for a class at the top
            # of the module.
            held = [
                binding
                for binding in source.properties.get("bindings", [])
                if isinstance(binding, dict)
                and binding.get("name") == raw_name
                and binding_in_effect(binding, position)
            ]
            if held:
                last = max(
                    held,
                    key=lambda b: (int(b.get("line") or 0), int(b.get("column") or 0)),
                )
                return next((c for c in candidates if c.id == last.get("target")), None)
            if source.qualname != f"{module}.{source.name}":
                # A nested class's body also sees the names around it, which
                # this does not read. The index has no such class node yet.
                return candidates[0] if len(candidates) == 1 else None
        # A function, a method, the module or the body of a class at its top
        # sees the module's own definition of a name, not a method or a class
        # nested in another class that shares it.
        candidates = [
            candidate for candidate in candidates if self._is_module_level(candidate)
        ]
        if len(candidates) != 1:
            return None
        if lexical is not None and not self._module_definition_holds(
            source, raw_name, candidates[0], lexical, position
        ):
            return None
        return candidates[0]

    def _is_module_level(self, node: Node) -> bool:
        return node.qualname == f"{self._module_qualname(node)}.{node.name}"

    def _module_definition_holds(
        self,
        source: Node,
        name: str,
        definition: Node,
        lexical: LexicalScopes,
        position: tuple[str, int, int] | None,
    ) -> bool:
        """Whether ``name`` holds the module's ``definition`` where ``source``
        reads it."""

        held = self._module_name_at(source, name, lexical, position)
        return held == ("def", definition.id)

    def _module_name_at(
        self,
        source: Node,
        name: str,
        lexical: LexicalScopes,
        position: tuple[str, int, int] | None,
    ) -> tuple[str, str | None] | None:
        """What the module's ``name`` holds where ``source`` reads it.

        A function or method runs after the module has, so it reads what the
        module binds the name to last; the module's own code and a class body
        run in order, so they read what it is bound to by the call. See
        _module_name_value for the values."""

        module = lexical.modules.get(source.path)
        if module is None:
            return None
        limit = (
            position
            if source.kind in {"module", "class"}
            and position is not None
            and position[0] == source.id
            else None
        )
        return self._module_name_value(module, name, lexical, limit, frozenset())

    def _module_name_value(
        self,
        module: Node,
        name: str,
        lexical: LexicalScopes,
        limit: tuple[str, int, int] | None,
        seen: frozenset[str],
    ) -> tuple[str, str | None] | None:
        """What ``module`` binds ``name`` to, by its end or by ``limit``:
        ("def", node id) for a def or class with a node, ("import", what it
        imports), _OTHER_VALUE for anything else or for bindings that do not
        agree, _NO_VALUE if nothing binds it; None if unknown, as where a
        function rebinds it through global or a star import may bind it.

        A binding the binding pass marks may_not_run, under if, with, match,
        a loop or a try's body, handlers or else, or in a loop's else that a
        break may skip, may not have run, so the one before it may hold too,
        back to one that runs: one not so marked, one in whose block the call
        is, or past the statement it is in, where that statement surely binds
        the name, as if and else both binding it. With none, the name may be
        unbound. A star import binds what its module exports; a binding only
        for type checkers binds nothing at run time."""

        if (module.path, name) in lexical.global_writes:
            return None
        own = lexical.bindings.get(module.id, {})
        events = [
            binding
            for binding in [*own.get(name, []), *own.get("*", [])]
            if binding.get("kind") not in _DECLARATION_KINDS
            and not binding.get("static_only")
            and (limit is None or binding_in_effect(binding, limit))
        ]

        def at(binding: dict[str, Any]) -> tuple[int, int]:
            return int(binding.get("line") or 0), int(binding.get("column") or 0)

        def within(point: tuple[int, int], span: Any) -> bool:
            return (
                isinstance(span, list)
                and len(span) == 4
                and (span[0], span[1]) <= point <= (span[2], span[3])
            )

        events.sort(key=at)
        point = (limit[1], limit[2]) if limit is not None else None
        values: list[tuple[str, str | None]] = []
        settled = False
        settling: list[int] | None = None
        for event in reversed(events):
            if settling is not None and at(event) < (settling[0], settling[1]):
                # Every binding in the statement that surely binds the name.
                settled = True
                break
            if event.get("kind") == "star_import":
                value = self._star_import_value(
                    event, name, lexical, seen | {module.id}
                )
                if value is None:
                    return None
                if value == _NO_VALUE:
                    continue
            else:
                value = self._binding_value(event, lexical)
            values.append(value)
            if not event.get("may_not_run") or (
                point is not None and within(point, event.get("block"))
            ):
                settled = True
                break
            span = event.get("settles_with")
            # Code inside that statement reads it before it has ended.
            if (
                settling is None
                and isinstance(span, list)
                and len(span) == 4
                and (point is None or point > (span[2], span[3]))
            ):
                settling = span
        if not values:
            return _NO_VALUE
        if not settled and settling is None:
            # No binding surely ran: the name may be unbound there.
            values.append(_NO_VALUE)
        return values[0] if all(v == values[0] for v in values) else _OTHER_VALUE

    @staticmethod
    def _binding_value(
        binding: dict[str, Any], lexical: LexicalScopes
    ) -> tuple[str, str | None]:
        kind = binding.get("kind")
        if kind in {"function_definition", "class_definition"}:
            node = lexical.nodes.get(str(binding.get("target")))
            line = int(binding.get("line") or 0)
            # A second def of the name has no node of its own and takes the
            # first one's id; its line is outside the first one's span.
            if node is not None and (node.start_line or 0) <= line <= (
                node.end_line or node.start_line or 0
            ):
                return ("def", node.id)
            return _OTHER_VALUE
        if kind in _IMPORT_BINDING_KINDS:
            target = import_binding_target(binding)
            return ("import", target) if target else _OTHER_VALUE
        return _OTHER_VALUE

    def _star_import_value(
        self,
        star: dict[str, Any],
        name: str,
        lexical: LexicalScopes,
        seen: frozenset[str],
    ) -> tuple[str, str | None] | None:
        """What ``from module import *`` binds ``name`` to: what the module
        binds it to by its end, if the module exports it; _NO_VALUE if it
        does not; None if the module is not indexed or imports back here."""

        # An indexed module is named by its node id, another by its name.
        module = lexical.nodes.get(str(star.get("target_module")))
        if module is None or module.kind != "module" or module.id in seen:
            return None
        own = lexical.bindings.get(module.id, {})
        if own.get("__all__"):
            exported = self._declared_exports(module, own["__all__"])
            if exported is None:
                # Whether the name is exported is not known: the module's
                # value if it is, nothing if it is not.
                value = self._module_name_value(module, name, lexical, None, seen)
                if value is None or value == _NO_VALUE:
                    return value
                return _OTHER_VALUE
            if name not in exported:
                return _NO_VALUE
        elif name.startswith("_"):
            return _NO_VALUE
        return self._module_name_value(module, name, lexical, None, seen)

    @staticmethod
    def _declared_exports(
        module: Node, declared: list[dict[str, Any]]
    ) -> set[str] | None:
        """The names a module's __all__ lists, empty or not; None where it is
        bound more than once or under a block that may not run, is not a
        literal list or tuple of strings, or is changed by a call such as
        __all__.append()."""

        if len(declared) != 1 or declared[0].get("may_not_run"):
            return None
        if any(
            isinstance(callsite, dict) and callsite.get("receiver") == "__all__"
            for callsite in module.properties.get("callsites", [])
        ):
            return None
        try:
            listed = ast.literal_eval(str(declared[0].get("value")))
        except (SyntaxError, ValueError):
            return None
        if not isinstance(listed, (list, tuple)) or not all(
            isinstance(item, str) for item in listed
        ):
            return None
        return set(listed)

    def _star_imports_exclude(
        self,
        source: Node,
        name: str,
        held: tuple[str, str | None] | None,
        lexical: LexicalScopes,
    ) -> bool:
        """Whether the module star-imports and none of its star imports, all
        read, binds ``name``, so the name is not a symbol elsewhere."""

        if held != _NO_VALUE:
            return False
        module = lexical.modules.get(source.path)
        own = lexical.bindings.get(module.id, {}) if module is not None else {}
        return any(not star.get("static_only") for star in own.get("*", []))

    def _star_brought_node(
        self, source: Node, name: str, context: _CallResolutionContext
    ) -> Node | None:
        """The project symbol a star import alone binds ``name`` to where
        ``source`` reads it: a def of another module, or a symbol that module
        imports. The legacy resolver otherwise matches it only by name, which
        fails where the project has another symbol of that name."""

        lexical = context.lexical
        module = lexical.modules.get(source.path)
        if module is None or lexical.bindings.get(module.id, {}).get(name):
            return None
        held = self._module_name_at(source, name, lexical, context.callsite_position)
        if held is None or not held[1]:
            return None
        if held[0] == "def":
            return context.by_id.get(held[1])
        if held[0] == "import":
            return context.by_qualname.get(held[1])
        return None

    def _imported_elsewhere(
        self, value: tuple[str, str | None], source: Node, lexical: LexicalScopes
    ) -> bool:
        """Whether a module value is an import: its own import, or a def that
        a star import brought from another module."""

        if value[0] == "import":
            return True
        node = lexical.nodes.get(str(value[1])) if value[0] == "def" else None
        return node is not None and node.path != source.path

    def _module_import_holds(
        self,
        source: Node,
        name: str,
        imported: str,
        context: _CallResolutionContext,
    ) -> bool:
        """Whether the module's ``name`` holds the import of ``imported``
        where ``source`` reads it, and nothing else may."""

        held = self._module_name_at(
            source, name, context.lexical, context.callsite_position
        )
        return held == ("import", imported)

    def _module_binds(
        self,
        source: Node,
        name: str,
        context: _CallResolutionContext,
        *,
        imports_allowed: bool = False,
    ) -> bool:
        """Whether a module binding of ``name`` may hold where ``source``
        reads it, so that the name is neither a builtin nor a symbol of that
        name elsewhere. With ``imports_allowed``, as for the legacy resolver,
        which matches an imported name to a symbol by name, only a binding
        other than an import counts."""

        lexical = context.lexical
        held = self._module_name_at(source, name, lexical, context.callsite_position)
        if held is not None:
            if held == _NO_VALUE:
                return False
            return not (
                imports_allowed and self._imported_elsewhere(held, source, lexical)
            )
        # A star import or a global write may bind it too: still the module's
        # if the module binds it, otherwise as unknown as any name beside a
        # star import.
        if (source.path, name) in lexical.global_writes:
            return True
        module = lexical.modules.get(source.path)
        own = lexical.bindings.get(module.id, {}) if module is not None else {}
        return any(
            not (imports_allowed and binding.get("kind") in _IMPORT_BINDING_KINDS)
            for binding in own.get(name, [])
            if binding.get("kind") not in _DECLARATION_KINDS
            and not binding.get("static_only")
        )

    @staticmethod
    def _module_qualname(node: Node) -> str | None:
        if node.properties.get("lexical_parent"):
            return str(node.properties["module"])
        if not node.qualname:
            return None
        if node.kind == "module":
            return node.qualname
        if node.kind == "method":
            class_name = node.properties.get("class")
            if isinstance(class_name, str):
                marker = f".{class_name}."
                if marker in node.qualname:
                    return node.qualname.split(marker, 1)[0]
            return ".".join(node.qualname.split(".")[:-2])
        return ".".join(node.qualname.split(".")[:-1])

    @staticmethod
    def _class_qualname(node: Node) -> str | None:
        if node.kind != "method" or not node.qualname:
            return None
        return ".".join(node.qualname.split(".")[:-1])

    @staticmethod
    def _resolution_strategy(raw_name: str) -> str:
        return "ast_attribute_resolution" if "." in raw_name else "ast_name_resolution"

    def _callsite_id(
        self,
        source: Node,
        callsite: dict[str, Any],
        raw_name: str,
    ) -> str:
        return callsite_id(
            source.id,
            source.path,
            self._int_or_none(callsite.get("line")),
            self._int_or_none(callsite.get("column")),
            raw_name,
        )

    def _with_stable_callsite_identity(
        self,
        source: Node,
        callsite: dict[str, Any],
        *,
        occurrence_by_subject: dict[tuple[str, ...], int],
        occurrence_by_callsite_id: dict[str, int],
    ) -> dict[str, Any]:
        raw_name = str(callsite.get("name"))
        current_callsite_id = self._callsite_id(source, callsite, raw_name)
        existing_occurrence = occurrence_by_callsite_id.get(current_callsite_id)
        subject = stable_callsite_subject(
            source.id,
            raw_name,
            context=callsite.get("context"),
            call_expression=callsite.get("call_expression"),
            receiver_expression=callsite.get("receiver"),
            attribute=callsite.get("attribute"),
        )
        if existing_occurrence is None:
            subject_key = stable_callsite_subject_key(subject)
            occurrence = occurrence_by_subject.get(subject_key, 0) + 1
            occurrence_by_subject[subject_key] = occurrence
            occurrence_by_callsite_id[current_callsite_id] = occurrence
        else:
            occurrence = existing_occurrence
        return {
            **callsite,
            "_stable_callsite_subject": subject,
            "_stable_callsite_occurrence": occurrence,
        }

    @staticmethod
    def _parse_expression(expression: str) -> ast.AST | None:
        try:
            return ast.parse(expression, mode="eval").body
        except SyntaxError:
            return None

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

    @staticmethod
    def _type_id(type_ref: dict[str, Any] | None) -> str | None:
        if type_ref is None:
            return None
        type_id = type_ref.get("type_id")
        if type_id in _UNKNOWN_TYPE_IDS:
            # Any says nothing about the value; typing.Any has no methods, so
            # a call on it named targets such as typing.Any.execute.
            return None
        return type_id if isinstance(type_id, str) else None

    @staticmethod
    def _type_ref_id(type_ref: dict[str, Any] | None) -> str | None:
        if type_ref is None:
            return None
        type_ref_id = type_ref.get("type_ref_id")
        return type_ref_id if isinstance(type_ref_id, str) else None

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        return value if isinstance(value, int) else None

    @staticmethod
    def _str_or_none(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _short_name(value: str) -> str:
        return value.rsplit(".", 1)[-1]

    @staticmethod
    def _set_optional(record: dict[str, Any], key: str, value: Any | None) -> None:
        if value is not None:
            record[key] = value
