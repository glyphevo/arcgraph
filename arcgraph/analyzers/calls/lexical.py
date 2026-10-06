"""Conservative lexical lookup for indexed nested Python callables."""

from __future__ import annotations

import ast
from collections import defaultdict
from typing import Any

from arcgraph.analyzers.calls.constants import BUILTIN_CALLS
from arcgraph.core.schemas import Node
from arcgraph.analyzers.type_unions import union_alternatives

# Bindings that name an import or a definition rather than a local value.
_DEFINITION_KINDS = frozenset(
    {
        "class_definition",
        "function_definition",
        "import_alias",
        "method_definition",
        "re_export",
        "star_import",
        "type_checking_import_alias",
    }
)


class LexicalScopes:
    def __init__(
        self, nodes: list[Node], type_refs: dict[str, dict[str, dict[str, Any]]]
    ) -> None:
        self.nodes = {node.id: node for node in nodes}
        self.modules = {node.path: node for node in nodes if node.kind == "module"}
        self.type_refs = type_refs
        self.bindings: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.mutated: set[tuple[str, str]] = set()
        self.strict = {
            node.id
            for node in nodes
            if node.properties.get("lexical_parent")
            or node.properties.get("conditional_type_inference")
        }
        self.strict.update(
            str(node.properties["lexical_parent"])
            for node in nodes
            if node.properties.get("lexical_parent")
        )
        for node in nodes:
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for binding in node.properties.get("bindings", []):
                if (
                    not isinstance(binding, dict)
                    or binding.get("kind") == "comprehension_target"
                ):
                    continue
                if isinstance(binding, dict) and isinstance(binding.get("name"), str):
                    grouped[binding["name"]].append(binding)
            self.bindings[node.id] = dict(grouped)
        # Even a write in a sibling callback can change a captured cell. Do not
        # choose a type based on definition order or assume callback execution order.
        for node in nodes:
            for name, bindings in self.bindings[node.id].items():
                if any(b.get("kind") == "nonlocal" for b in bindings):
                    for owner in self.chain(node)[1:]:
                        if name in self.bindings.get(owner.id, {}):
                            self.mutated.add((owner.id, name))
                            break

    def chain(self, source: Node) -> list[Node]:
        result = [source]
        seen = {source.id}
        while parent_id := result[-1].properties.get("lexical_parent"):
            parent = self.nodes.get(str(parent_id))
            if parent is None or parent.id in seen or parent.path != source.path:
                break
            result.append(parent)
            seen.add(parent.id)
        module = self.modules.get(source.path)
        if module is not None and module.id not in seen:
            result.append(module)
        return result

    def lookup(
        self, source: Node, name: str
    ) -> tuple[Node, list[dict[str, Any]], int | None] | None:
        chain = self.chain(source)
        for i, owner in enumerate(chain):
            bindings = self.bindings.get(owner.id, {}).get(name)
            if bindings:
                # Module definitions remain visible to function bodies regardless
                # of textual order; only captured function locals use this bound.
                limit = (
                    chain[i - 1].start_line if i and owner.kind != "module" else None
                )
                return owner, bindings, limit
        return None

    def stable(
        self,
        owner: Node,
        bindings: list[dict[str, Any]],
        limit: int | None,
        *,
        position: tuple[int, int] | None = None,
    ) -> bool:
        if len(bindings) != 1 or owner.properties.get("ambiguous_definition"):
            return False
        binding = bindings[0]
        if (owner.id, binding["name"]) in self.mutated or binding.get("static_only"):
            return False
        kind = binding.get("kind")
        if kind not in {
            "parameter",
            "assignment",
            "annotated_assignment",
            "import_alias",
            "function_definition",
            "class_definition",
            "method_definition",
        }:
            return False
        line = binding.get("line")
        if limit is not None and isinstance(line, int) and line > limit:
            return False
        body_positions = owner.properties.get("scope_body_positions")
        if (
            kind != "parameter"
            and body_positions is not None
            and [line, binding.get("column")] not in body_positions
        ):
            region = binding.get("definition_region")
            if not (
                kind == "function_definition"
                and position is not None
                and isinstance(region, list)
                and len(region) == 2
                and region[0] < list(position) <= region[1]
            ):
                return False
        return True

    def binding(
        self, source: Node, name: str, callsite: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        found = self.lookup(source, name)
        position = (
            self.callsite_position(callsite)
            if found and found[0].id == source.id
            else None
        )
        if found and self.stable(*found, position=position):
            return found[1][0]
        return None

    def type_ref(self, source: Node, name: str) -> dict[str, Any] | None:
        found = self.lookup(source, name)
        if not found or not self.stable(*found):
            return None
        owner, bindings, _ = found
        ref = self.type_refs.get(owner.id, {}).get(name)
        if ref and (ref.get("type_id") or union_alternatives(ref) is not None):
            return ref
        if owner.kind == "method" and name in {"self", "cls"} and owner.qualname:
            decorators = owner.properties.get("decorators", [])
            if (
                "staticmethod" not in decorators
                and bindings[0].get("kind") == "parameter"
            ):
                return {
                    "type_id": f"class:{owner.qualname.rsplit('.', 1)[0]}",
                    "strategy": "lexical_receiver",
                    "confidence": "inferred",
                }
        return None

    def aliases(self, source: Node) -> dict[str, str]:
        result = {}
        names = {
            name
            for owner in self.chain(source)
            for name in self.bindings.get(owner.id, {})
        }
        for name in names:
            binding = self.binding(source, name)
            if binding and binding.get("kind") == "import_alias":
                target = binding.get("target_qualname") or binding.get("value")
                if isinstance(target, str):
                    result[name] = target
        return result

    def blocked(
        self, source: Node, callsite: dict[str, Any], *, require_stable: bool = False
    ) -> bool:
        if not require_stable and source.id not in self.strict:
            return False
        if source.properties.get("ambiguous_definition"):
            return True
        root = self.root_name(callsite.get("call_expression") or callsite.get("name"))
        if root is None:
            return False
        found = self.lookup(source, root)
        if not found:
            return root not in BUILTIN_CALLS
        owner, bindings, limit = found
        if owner.id == source.id:
            limit = callsite.get("line")
        position = self.callsite_position(callsite) if owner.id == source.id else None
        if not self.stable(owner, bindings, limit, position=position):
            return True
        binding = bindings[0]
        if (
            owner.id == source.id
            and binding.get("line") == callsite.get("line")
            and binding.get("column", 0) > callsite.get("column", 0)
        ):
            return True
        if (
            binding.get("kind") == "class_definition"
            and binding.get("target") not in self.nodes
            and callsite.get("attribute")
        ):
            return True
        if binding.get("kind") in {
            "import_alias",
            "function_definition",
            "class_definition",
            "method_definition",
        }:
            return False
        # An unknown local masks ancestors and same-name global/class heuristics.
        return self.type_ref(source, root) is None

    def shadowed(self, source: Node, name: str) -> bool:
        """Whether ``name`` is a local value of ``source`` that hides a module
        binding or a builtin of that name. A parameter, an assignment, a del or
        any other binding of a value makes the name local in the whole body,
        even before that binding; a global declaration does not."""

        if source.kind == "module":
            return False
        own = self.bindings.get(source.id, {}).get(name, [])
        if not own or any(b.get("kind") in {"global", "nonlocal"} for b in own):
            return False
        if all(b.get("kind") in _DEFINITION_KINDS for b in own):
            return False
        module = self.modules.get(source.path)
        if module is not None and name in self.bindings.get(module.id, {}):
            return True
        return name in BUILTIN_CALLS

    @staticmethod
    def callsite_position(callsite: dict[str, Any] | None) -> tuple[int, int] | None:
        if (
            callsite
            and isinstance(callsite.get("line"), int)
            and isinstance(callsite.get("column"), int)
        ):
            return callsite["line"], callsite["column"]
        return None

    @staticmethod
    def root_name(expression: object) -> str | None:
        try:
            expr = ast.parse(str(expression), mode="eval").body
        except SyntaxError:
            return None
        while isinstance(expr, (ast.Call, ast.Attribute, ast.Subscript)):
            expr = expr.func if isinstance(expr, ast.Call) else expr.value
        return expr.id if isinstance(expr, ast.Name) else None
