"""Small shared helpers for ArcGraph core modules."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
from typing import Any

from arcgraph.core.sharing_retry import retry_sharing_violation

_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


def replace_text_file(path: Path, text: str) -> None:
    """Write *text* to *path* through a new file renamed over it.

    The new file is created exclusively next to *path*, so a symlink already
    standing at *path* is replaced instead of written through.  It is created
    with mode 0o666 under the process umask, as ``Path.write_text`` would.
    """

    for _ in range(100):
        temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            descriptor = os.open(temp_path, _TEMP_FLAGS, 0o666)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"Could not create a temporary file next to {path}")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        retry_sharing_violation(lambda: os.replace(temp_path, path))
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


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
