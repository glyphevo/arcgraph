"""Stable ArcGraph identifier helpers."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import quote


def module_id(module: str) -> str:
    return f"mod:{module}"


def class_id(qualname: str) -> str:
    return f"class:{qualname}"


def function_id(qualname: str) -> str:
    return f"fn:{qualname}"


def method_id(qualname: str) -> str:
    return f"method:{qualname}"


def component_id(qualname: str) -> str:
    return f"component:{qualname}"


def interface_id(qualname: str) -> str:
    return f"interface:{qualname}"


def type_alias_id(qualname: str) -> str:
    return f"type_alias:{qualname}"


def enum_id(qualname: str) -> str:
    return f"enum:{qualname}"


def route_id(method: str, path: str) -> str:
    return f"route:{method.upper()}:{path}"


def worker_id(name: str) -> str:
    return f"worker:{name}"


def mcp_tool_id(name: str) -> str:
    return f"mcp_tool:{name}"


def queue_id(name: str) -> str:
    return f"queue:{name}"


def table_id(name: str) -> str:
    return f"table:{name}"


def log_sink_id(name: str) -> str:
    return f"log:{quote(name, safe=':._/-')}"


def config_key_id(key: str) -> str:
    return f"config:{quote(key, safe=':._/-')}"


def pydantic_model_id(qualname: str) -> str:
    return f"schema:{quote(qualname, safe=':._/-')}"


def model_field_id(qualname: str) -> str:
    return f"field:{quote(qualname, safe=':._/-')}"


def cli_command_id(name: str) -> str:
    return f"cli:{quote(name, safe=':._/-')}"


def cli_option_id(command: str, option: str) -> str:
    return f"{cli_command_id(command)}:option:{quote(option, safe=':._/-')}"


def pytest_case_id(qualname: str) -> str:
    return f"test:{quote(qualname, safe=':._/-')}"


def pytest_fixture_id(qualname: str) -> str:
    return f"fixture:{quote(qualname, safe=':._/-')}"


def external_package_id(package: str) -> str:
    return f"ext:{package}"


def source_root_id(path: str) -> str:
    """Stable ID for a source_root structural node."""
    return f"source_root:{quote(path, safe=':._/-')}"


def package_id(qualname: str) -> str:
    """Stable ID for a package (directory with __init__.py) structural node."""
    return f"package:{quote(qualname, safe=':._/-')}"


def canonical_identity(
    short_id: str,
    *,
    manager: str = "local",
    package: str = "workspace",
    version: str = "snapshot",
) -> str:
    descriptor = quote(short_id, safe=":._/-")
    return f"ArcGraph {manager} {package} {version} {descriptor}"


def semantic_fact_id(*parts: object) -> str:
    return _content_hash_id("fact", *parts)


def diagnostic_id(*parts: object) -> str:
    return _content_hash_id("diagnostic", *parts)


def callsite_id(*parts: object) -> str:
    return _content_hash_id("callsite", *parts)


def stable_callsite_subject(
    source_scope: str,
    raw_expression: str,
    *,
    context: Any = None,
    call_expression: Any = None,
    receiver_expression: Any = None,
    attribute: Any = None,
) -> dict[str, str | None]:
    """Project a callsite without source coordinates.

    The occurrence of this subject within a source scope is tracked separately.
    Keeping coordinates out of this projection lets Change Safety distinguish a
    source move from a semantic callsite change.
    """

    return {
        "source_scope": source_scope,
        "raw_expression": raw_expression,
        "context": _optional_text(context),
        "call_expression": _optional_text(call_expression),
        "receiver_expression": _optional_text(receiver_expression),
        "attribute": _optional_text(attribute),
    }


def stable_callsite_subject_key(subject: dict[str, str | None]) -> tuple[str, ...]:
    """Return the fixed-order key used to count repeated semantic callsites."""

    return tuple(
        subject.get(key) or ""
        for key in (
            "source_scope",
            "raw_expression",
            "context",
            "call_expression",
            "receiver_expression",
            "attribute",
        )
    )


def binding_id(*parts: object) -> str:
    return _content_hash_id("binding", *parts)


def type_ref_id(*parts: object) -> str:
    return _content_hash_id("type_ref", *parts)


def _content_hash_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def normalize_symbol_query(query: str) -> list[str]:
    if ":" in query:
        return [query]
    return [
        method_id(query),
        function_id(query),
        component_id(query),
        class_id(query),
        interface_id(query),
        type_alias_id(query),
        enum_id(query),
        module_id(query),
    ]
