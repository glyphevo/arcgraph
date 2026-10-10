"""Follow explicit module exports without guessing a class by its short name."""

from arcgraph.core.schemas import Node


def runtime_exported_class(qualified: str, lexical) -> str | None:
    """Reuse runtime star/export policy for a concrete class-factory argument.

    Never pick a same-short-name class. Every import must reach the same
    indexed definition, with mutations, export masks and cycles respected.
    """
    # Calls imports this module; runtime lookup avoids an import cycle and
    # keeps __all__ policy in its existing single implementation.
    from arcgraph.analyzers.calls.analyzer import CallAnalyzer

    analyzer = CallAnalyzer()
    seen: set[str] = set()
    for _ in range(32):
        if qualified in seen:
            return None
        seen.add(qualified)
        module_name, _, name = qualified.rpartition(".")
        module = lexical.nodes.get("mod:" + module_name)
        if module is None:
            return None
        held = analyzer._module_name_value(module, name, lexical, None, frozenset())
        if held is None:
            return None
        if held[0] == "def":
            target = lexical.nodes.get(held[1])
            return target.id if target is not None and target.kind == "class" else None
        if held[0] != "import" or not held[1]:
            return None
        qualified = held[1]
    return None


def exported_class(qualified: str, nodes: dict[str, Node]) -> str | None:
    """Resolve a bounded chain of stable module bindings, including re-exports."""
    seen: set[str] = set()
    for _ in range(32):
        if qualified in seen:
            return None
        seen.add(qualified)
        parts = qualified.split(".")
        for split in range(len(parts) - 1, 0, -1):
            module = nodes.get("mod:" + ".".join(parts[:split]))
            if module is not None:
                break
        else:
            return None
        if any(
            b.get("kind") == "star_import"
            for b in module.properties.get("bindings", [])
        ):
            return None
        name = parts[split]
        bindings = [
            b
            for b in module.properties.get("bindings", [])
            if b.get("name") == name and b.get("kind") != "re_export"
        ]
        if len(bindings) != 1 or module.properties.get("ambiguous_definition"):
            return None
        binding = bindings[0]
        if binding.get("static_only") or not binding.get("scope_direct"):
            return None
        tail = parts[split + 1 :]
        if binding.get("kind") == "class_definition" and not tail:
            target = nodes.get(str(binding.get("target", "")))
            return target.id if target is not None and target.kind == "class" else None
        if binding.get("kind") != "import_alias":
            return None
        target = binding.get("target_qualname") or binding.get("value")
        if not isinstance(target, str) or not target:
            return None
        qualified = ".".join([target, *tail])
    return None
