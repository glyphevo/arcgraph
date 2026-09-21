"""Small shared helpers for ArcGraph core modules."""

from __future__ import annotations

import json
from typing import Any


def int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def estimated_tokens(payload: Any) -> int:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return max(1, len(serialized) // 4)


def strip_source_snippets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_source_snippets(item)
            for key, item in value.items()
            if key not in {"snippet", "source_snippet"}
        }
    if isinstance(value, list):
        return [strip_source_snippets(item) for item in value]
    return value
