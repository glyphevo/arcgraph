"""Preserve union alternatives independently of a primary receiver summary."""

from __future__ import annotations

import builtins
import datetime
import pathlib
from typing import Any

_RUNTIME_CLASSES = {
    **{
        f"builtin:{name}": value
        for name, value in vars(builtins).items()
        if isinstance(value, type)
    },
    **{
        f"extsym:pathlib.{name}": value
        for name, value in vars(pathlib).items()
        if isinstance(value, type)
    },
    **{
        f"extsym:datetime.{name}": value
        for name, value in vars(datetime).items()
        if isinstance(value, type)
    },
    "builtin:None": type(None),
}


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


def nullable_default_type(source, fallback):
    """T | None or T has type T, including the same generic arguments."""
    members = union_alternatives(source)
    if members is None or not any(
        member.get("type_id") == "builtin:None" for member in members
    ):
        return None
    value = single_value_type(source)
    if (
        union_alternatives(value) is not None
        or not value.get("type_id")
        or not fallback
        or union_alternatives(fallback) is not None
        or type_identity(value) != type_identity(fallback)
    ):
        return None
    return {**value, "strategy": "nullable_default", "typed_value_evidence": True}


def guarded_value_type(properties, name, position, ref, bindings, *, mutated=False):
    """Use only a bounded local guard with no intervening or captured write."""
    if mutated or any(b.get("kind") in {"global", "nonlocal"} for b in bindings):
        return None
    for entry in reversed(properties.get("receiver_narrowings", [])):
        span = entry["range"]
        if entry["name"] != name or not tuple(span[:2]) <= position <= tuple(span[2:]):
            continue
        if any(
            tuple(loop[:2]) <= position <= tuple(loop[2:])
            for loop in entry.get("loop_writes", [])
        ):
            return None
        if any(
            tuple(entry["check"])
            < tuple(b.get("statement_end") or (b.get("line", 0), b.get("column", 0)))
            <= position
            for b in bindings
        ):
            if entry.get("type_id") and ref:
                # The check was invalidated. Keep the current value as type
                # evidence so a failed method lookup cannot fall back by name.
                return {**ref, "typed_value_evidence": True}
            return None
        ref = ref or {}
        if entry.get("exclude_none"):
            # Absence alone cannot prove a concrete type for Any or a None-only
            # binding summary. Leave existing concrete guard paths available.
            if union_alternatives(ref) is None:
                continue
            members = [
                m
                for m in union_alternatives(ref) or []
                if m.get("type_id") != "builtin:None"
            ]
            ref = single_value_type(ref)
            if not ref.get("type_id"):
                common = {m.get("type_id") for m in members}
                if len(common) != 1 or not common <= {
                    "builtin:dict",
                    "builtin:list",
                    "builtin:tuple",
                    "builtin:set",
                }:
                    # Presence cannot select one member of a heterogeneous
                    # union. It also cannot erase an earlier concrete guard.
                    continue
                target = next(iter(common))
                ref = {
                    "type_id": target,
                    "type_expression": target[8:],
                    "erased_container_arguments": True,
                }
        else:
            target = entry["type_id"]
            if entry.get("normalized_builtin"):
                ref = {
                    "type_id": target,
                    "type_expression": target.partition(":")[2],
                }
            members = union_alternatives(ref)
            if members is not None:
                matches = [
                    member for member in members if member.get("type_id") == target
                ]
                # Do not approximate subclass matching or select between two
                # instantiations such as list[A] and list[B].
                runtime_target = _RUNTIME_CLASSES.get(target)
                if (
                    not matches
                    and runtime_target
                    and any(
                        m.get("type_id") in {"typing:Any", "extsym:typing.Any"}
                        or (m.get("type_id") is None and not m.get("type_args"))
                        or (
                            m.get("type_id") in _RUNTIME_CLASSES
                            and issubclass(
                                runtime_target, _RUNTIME_CLASSES[m["type_id"]]
                            )
                        )
                        for m in members
                    )
                ):
                    ref = {
                        "type_id": target,
                        "type_expression": target.partition(":")[2],
                    }
                    matches = [ref]
                if len(matches) > 1 and target in {
                    "builtin:dict",
                    "builtin:list",
                    "builtin:tuple",
                    "builtin:set",
                }:
                    # The check proves this container class, but cannot select
                    # between dict[K, A] and dict[K, B]. Erase arguments rather
                    # than choosing a branch; derived elements stay unknown.
                    matches = [{"type_id": target, "type_expression": target[8:]}]
                if len(matches) != 1:
                    return unknown_union_result(name)
                for member in members:
                    other_id = member.get("type_id")
                    if other_id == target:
                        continue
                    other = _RUNTIME_CLASSES.get(other_id)
                    runtime_target = _RUNTIME_CLASSES.get(target)
                    if other and runtime_target:
                        if issubclass(other, runtime_target) or (
                            matches[0].get("type_args")
                            and issubclass(runtime_target, other)
                        ):
                            return unknown_union_result(name)
                    elif other_id != "builtin:None":
                        if (
                            other_id is None
                            and runtime_target is not None
                            and not matches[0].get("type_args")
                        ):
                            # A real builtin check proves its runtime class,
                            # even if the value arrived through an unknown
                            # write. It proves no generic arguments.
                            continue
                        if other_id in entry.get("disjoint_types", []):
                            continue
                        if not matches[0].get("type_args") and other_id in {
                            "typing:Any",
                            "extsym:typing.Any",
                        }:
                            continue
                        if (
                            other
                            and entry.get("nominal_type")
                            and not matches[0].get("type_args")
                        ):
                            # A pre-existing standard class cannot derive from
                            # this plain project class. No generic args to hide.
                            continue
                        # Unknown inheritance or instance-check hooks cannot
                        # prove this alternative disjoint from the target.
                        return unknown_union_result(name)
                ref = {
                    **{
                        k: v
                        for k, v in ref.items()
                        if k
                        not in {
                            "type_id",
                            "symbol_id",
                            "origin",
                            "type_args",
                            "type_expression",
                        }
                    },
                    **matches[0],
                }
            elif ref.get("type_id") != target:
                ref = {
                    **ref,
                    "type_id": target,
                    "origin": None,
                    "type_args": [],
                    "type_expression": target.partition(":")[2],
                }
        type_id = entry.get("type_id", ref.get("type_id"))
        return {
            **ref,
            "type_id": type_id,
            "symbol_id": type_id,
            "type_expression": ref.get("type_expression", type_id.partition(":")[2]),
            "strategy": "receiver_guard",
            "isinstance_receiver": not entry.get("exclude_none", False),
            "narrowed_receiver": True,
            "typed_value_evidence": True,
        }
    return None
