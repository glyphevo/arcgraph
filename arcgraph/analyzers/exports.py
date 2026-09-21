"""Follow explicit module exports without guessing a class by its short name."""

from arcgraph.core.schemas import Node


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
