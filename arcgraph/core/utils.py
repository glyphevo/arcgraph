"""Small shared helpers for ArcGraph core modules."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def symlink_below(root: Path, named: Path) -> bool:
    """Whether an existing component of *named* below *root* is a symlink.

    Callers first compare *named* with its ``resolve()`` result; this catches
    the links resolution leaves in place.  Python 3.12 and later on Windows
    return a path through a symlink loop unchanged instead of raising.
    """

    current = root
    for part in named.relative_to(root).parts:
        current = current / part
        if os.path.islink(current):
            return True
    return False


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
