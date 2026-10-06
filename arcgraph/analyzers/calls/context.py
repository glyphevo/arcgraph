"""Call resolution context: indexes and intermediate data structures."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from arcgraph.analyzers.calls.lexical import LexicalScopes
from arcgraph.analyzers.calls.constants import TARGET_KINDS
from arcgraph.core.schemas import Confidence, Edge, Node
from arcgraph.analyzers.type_unions import union_alternatives, unknown_union_result


@dataclass
class CallAnalysis:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _ResolvedCallTarget:
    target: Node
    strategy: str
    candidate_count: int
    confidence: Confidence = "inferred"
    resolution_status: str = "resolved"
    edge_kind: str | None = None
    receiver_expression: str | None = None
    receiver_type: str | None = None
    receiver_type_ref_id: str | None = None


def _by_name(nodes: list[Node]) -> dict[str, list[Node]]:
    by_name: dict[str, list[Node]] = defaultdict(list)
    for node in nodes:
        by_name[node.name].append(node)
    return by_name


class _CallResolutionContext:
    def __init__(self, nodes: list[Node]) -> None:
        self.callsite_position: tuple[str, int, int] | None = None
        self.target_nodes = [node for node in nodes if node.kind in TARGET_KINDS]
        self.by_name = _by_name(
            [
                node
                for node in self.target_nodes
                if not node.properties.get("lexical_parent")
            ]
        )
        self.by_id = {node.id: node for node in self.target_nodes}
        self.by_qualname = {
            node.qualname: node for node in self.target_nodes if node.qualname
        }
        self.methods_by_class: dict[tuple[str, str], Node] = {}
        self.return_type_by_target: dict[str, dict[str, Any]] = {}
        self.scope_type_refs: dict[str, dict[str, dict[str, Any]]] = {}
        self.module_type_refs_by_path: dict[str, dict[str, dict[str, Any]]] = {}
        self.class_instance_attrs: dict[str, dict[str, dict[str, Any]]] = {}
        self.import_aliases_by_scope: dict[str, dict[str, str]] = {}
        self.module_import_aliases_by_path: dict[str, dict[str, str]] = {}
        self._build_type_indexes(nodes)
        self._build_binding_indexes(nodes)
        # Modules with ``from m import *``, through which any name may arrive.
        self.star_import_modules = {
            node.qualname
            for node in nodes
            if node.kind == "module"
            and node.qualname
            and any(
                isinstance(binding, dict) and binding.get("kind") == "star_import"
                for binding in node.properties.get("bindings", [])
            )
        }
        self.lexical = LexicalScopes(nodes, self.scope_type_refs)
        # Each project class's method resolution order, once computed.
        self.method_resolution_orders: dict[str, list[Any] | None] = {}

    def type_ref_at(self, source: Node, name: str) -> dict[str, Any] | None:
        fallback = self.scope_type_refs.get(source.id, {}).get(name)
        if not self.callsite_position or self.callsite_position[0] != source.id:
            return fallback
        recorded_refs: list[dict[str, Any]] = source.properties.get("type_refs", [])
        refs: list[dict[str, Any]] = [
            r
            for r in recorded_refs
            if r.get("name") == name
            and r.get("subject_kind") == "binding"
            and not r.get("comprehension_binding")
        ]
        if len(refs) < 2:
            return fallback
        line = self.callsite_position[1]
        recorded_bindings: list[dict[str, Any]] = source.properties.get("bindings", [])
        bindings: list[dict[str, Any]] = [
            b
            for b in recorded_bindings
            if b.get("name") == name
            and b.get("kind") != "comprehension_target"
            and (b.get("line", 0) < line or b.get("kind") == "parameter")
        ]
        if not bindings:
            return unknown_union_result(name)
        latest: dict[str, Any] = max(
            bindings, key=lambda b: (b.get("line", 0), b.get("column", 0))
        )
        binding_id = latest.get("binding_id")
        return next(
            (r for r in refs if r.get("binding_id") == binding_id),
            unknown_union_result(name),
        )

    def comprehension_type_ref(self, source: Node, name: str) -> dict[str, Any] | None:
        if not source.properties.get("comprehension_type_input"):
            return None
        if not self.callsite_position or self.callsite_position[0] != source.id:
            return None
        if name in source.properties.get("comprehension_writes", []):
            return {**unknown_union_result(name), "comprehension_binding": True}
        position = self.callsite_position[1:]
        contexts: list[dict[str, Any]] = source.properties.get(
            "comprehension_contexts", []
        )
        for entry in reversed(contexts):
            region = entry["range"]
            if tuple(region[:2]) <= position < tuple(region[2:]):
                environment: dict[str, str | None] = entry["bindings"]
                if name in environment:
                    provider = environment[name]
                    refs: list[dict[str, Any]] = source.properties.get("type_refs", [])
                    return next(
                        (
                            ref
                            for ref in refs
                            if provider and ref.get("binding_id") == provider
                        ),
                        {**unknown_union_result(name), "comprehension_binding": True},
                    )
                break
        # Comprehension targets never become locals of the surrounding scope.
        bindings: list[dict[str, Any]] = source.properties.get("bindings", [])
        if any(
            b.get("name") == name and b.get("kind") == "comprehension_target"
            for b in bindings
        ):
            if not self.lexical.lookup(source, name):
                return {**unknown_union_result(name), "comprehension_binding": True}
        return None

    def value_ref_available(self, source: Node, ref: dict[str, Any], line: int) -> bool:
        recorded_bindings: list[dict[str, Any]] = source.properties.get("bindings", [])
        binding: dict[str, Any] | None = next(
            (
                b
                for b in recorded_bindings
                if b.get("binding_id") == ref.get("binding_id")
            ),
            None,
        )
        if not binding:
            return False
        name = str(binding.get("name"))
        if (source.id, name) in self.lexical.mutated:
            return False
        writes: list[dict[str, Any]] = [
            b
            for b in recorded_bindings
            if b.get("name") == name and b.get("line", 0) <= line
        ]
        if writes and max(
            writes, key=lambda b: (b.get("line", 0), b.get("column", 0))
        ).get("binding_id") != binding.get("binding_id"):
            return False
        region = binding.get("value_region")
        if isinstance(region, list) and len(region) == 2:
            return region[0] <= line <= region[1]
        return (
            binding.get("kind") == "assignment"
            and binding.get("evidence", {}).get("end_line", line) < line
            and [binding.get("line"), binding.get("column")]
            in source.properties.get("scope_body_positions", [])
        )

    def _build_type_indexes(self, nodes: list[Node]) -> None:
        for node in self.target_nodes:
            if node.kind == "method" and node.qualname:
                class_qualname = node.qualname.rsplit(".", 1)[0]
                self.methods_by_class[(class_qualname, node.name)] = node

        for node in nodes:
            scope_refs = self._scope_type_refs(node)
            if scope_refs:
                self.scope_type_refs[node.id] = scope_refs
                if node.kind == "module" and node.path:
                    self.module_type_refs_by_path[node.path] = scope_refs
                if node.kind == "class" and node.qualname:
                    attrs = self.class_instance_attrs.setdefault(node.qualname, {})
                    for name, type_ref in scope_refs.items():
                        if "." in name or name == "return":
                            continue
                        if (
                            isinstance(type_ref.get("type_id"), str)
                            or union_alternatives(type_ref) is not None
                        ):
                            attrs.setdefault(name, type_ref)
            return_ref = scope_refs.get("return")
            if return_ref and (
                isinstance(return_ref.get("type_id"), str)
                or union_alternatives(return_ref) is not None
            ):
                self.return_type_by_target[node.id] = return_ref
            if node.kind != "method" or not node.qualname:
                continue
            class_qualname = node.qualname.rsplit(".", 1)[0]
            if return_ref and self._is_property_method(node):
                self.class_instance_attrs.setdefault(class_qualname, {}).setdefault(
                    node.name,
                    return_ref,
                )
            attrs = self.class_instance_attrs.setdefault(class_qualname, {})
            for name, type_ref in scope_refs.items():
                if name.startswith("self.") and (
                    isinstance(type_ref.get("type_id"), str)
                    or union_alternatives(type_ref) is not None
                ):
                    attrs.setdefault(name.removeprefix("self."), type_ref)

    def _build_binding_indexes(self, nodes: list[Node]) -> None:
        for node in nodes:
            aliases = self._scope_import_aliases(node)
            if not aliases:
                continue
            self.import_aliases_by_scope[node.id] = aliases
            if node.kind == "module" and node.path:
                self.module_import_aliases_by_path[node.path] = aliases

    @staticmethod
    def _scope_type_refs(node: Node) -> dict[str, dict[str, Any]]:
        refs: dict[str, dict[str, Any]] = {}
        type_refs = node.properties.get("type_refs", [])
        if isinstance(type_refs, list):
            for item in type_refs:
                if not isinstance(item, dict):
                    continue
                if item.get("comprehension_binding"):
                    continue
                name = item.get("name")
                if isinstance(name, str) and name:
                    refs[name] = item

        bindings = node.properties.get("bindings", [])
        if isinstance(bindings, list):
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                if binding.get("kind") == "comprehension_target":
                    continue
                name = binding.get("name")
                type_id = binding.get("type_ref")
                if not isinstance(name, str) or not isinstance(type_id, str):
                    continue
                refs.setdefault(
                    name,
                    {
                        "name": name,
                        "type_id": type_id,
                        "type_ref_id": binding.get("type_ref_id"),
                        "confidence": binding.get("type_ref_confidence"),
                        "strategy": binding.get("type_ref_strategy"),
                    },
                )
        return refs

    @staticmethod
    def _scope_import_aliases(node: Node) -> dict[str, str]:
        aliases: dict[str, str] = {}
        bindings = node.properties.get("bindings", [])
        if not isinstance(bindings, list):
            return aliases
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            if binding.get("static_only"):
                continue
            if binding.get("kind") not in {"import_alias", "star_import"}:
                continue
            name = binding.get("name")
            if not isinstance(name, str) or not name or name == "*":
                continue
            target = (
                binding.get("target_qualname")
                or binding.get("value")
                or binding.get("target_module")
            )
            if isinstance(target, str) and target:
                aliases[name] = target
        return aliases

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
