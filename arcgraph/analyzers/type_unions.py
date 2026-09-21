"""Preserve union alternatives independently of a primary receiver summary."""

from __future__ import annotations

from typing import Any


def union_alternatives(ref: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Flatten unions on the value path, not unions inside container elements."""
    if not ref:
        return None
    origin = ref.get("origin")
    args: list[dict[str, Any]] = ref.get("type_args", [])
    if origin in {"Annotated", "ClassVar", "Final", "Type"}:
        return union_alternatives(args[0]) if args else None
    if origin not in {"Union", "Optional"}:
        return None
    members: list[dict[str, Any]] = []
    for arg in args:
        nested = union_alternatives(arg)
        members.extend(nested if nested is not None else [arg])
    if origin == "Optional":
        members.append({"type_id": "builtin:None", "type_expression": "None"})
    return members


def type_identity(ref: dict[str, Any]) -> tuple:
    """Include generic arguments: list[A] and list[B] are not interchangeable."""
    return (
        ref.get("type_id"),
        tuple(type_identity(arg) for arg in ref.get("type_args", [])),
    )


def unknown_union_result(expression: str) -> dict[str, Any]:
    """Retain the barrier after an operation whose union result is unknown."""
    return {
        "origin": "Union",
        "type_expression": expression,
        "resolution_status": "unresolved",
        "strategy": "unresolved_union_result",
        "type_args": [
            {"type_expression": expression, "resolution_status": "unresolved"}
        ],
    }


def contains_union(ref: dict[str, Any]) -> bool:
    """Include unions nested inside containers for provider invalidation."""
    return ref.get("origin") in {"Union", "Optional"} or any(
        contains_union(arg) for arg in ref.get("type_args", [])
    )


def single_value_type(ref: dict[str, Any]) -> dict[str, Any]:
    """View the sole non-None value without confusing wrapper args with elements."""
    members = union_alternatives(ref)
    if members is not None:
        values = [item for item in members if item.get("type_id") != "builtin:None"]
        if (
            not values
            or not ref.get("type_id")
            or any(type_identity(item) != type_identity(values[0]) for item in values)
        ):
            return ref
        value = single_value_type(values[0])
    elif ref.get("origin") in {"Annotated", "ClassVar", "Final"} and ref.get(
        "type_args"
    ):
        value = single_value_type(ref["type_args"][0])
    else:
        return ref
    result = {
        k: v
        for k, v in ref.items()
        if k not in {"origin", "type_args", "type_id", "symbol_id", "type_expression"}
    }
    result.update(value)
    return result


def tuple_element_type(ref: dict[str, Any]) -> dict[str, Any]:
    """Without a fixed index, only homogeneous tuple members prove one type."""
    args: list[dict[str, Any]] = ref.get("type_args", [])
    if len(args) == 2 and args[1].get("type_expression") == "...":
        return args[0]
    if args and all(type_identity(arg) == type_identity(args[0]) for arg in args):
        return args[0]
    return unknown_union_result(str(ref.get("type_expression") or "tuple"))
