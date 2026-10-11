"""Lossless encoding for internal SQLite payloads, independent of public schemas."""

import json
import sqlite3
import zlib
from typing import Any

try:
    from pydantic_core import from_json
except ImportError:  # Older compatible Pydantic cores retain the stdlib path.
    from_json = json.loads

STORAGE_VERSION = 1
_CANONICAL = json.JSONEncoder(sort_keys=True, separators=(",", ":"))
_ORDERED = json.JSONEncoder(separators=(",", ":"))


class SchemaVersionError(RuntimeError):
    """An index requires an explicit rebuild before it can be read."""


def encode_json(value: Any) -> bytes:
    if value == {}:
        return b"{}"
    if value == []:
        return b"[]"
    raw = _CANONICAL.encode(value).encode("utf-8")
    return b"Z" + zlib.compress(raw, 1) if len(raw) > 256 else raw


def decode_json(raw: bytes) -> str:
    # Accept plain JSON as well so small payloads need no compression envelope.
    return (zlib.decompress(raw[1:]) if raw.startswith(b"Z") else raw).decode("utf-8")


def load_json(raw):
    """Use the existing native parser, preserving stdlib edge cases and errors."""
    if raw == "{}":
        return {}
    if raw == "[]":
        return []
    try:
        return from_json(raw)
    except (ValueError, TypeError):
        return json.loads(raw)


sqlite3.register_converter("JSONZ", decode_json)


def reuse_encoder(payloads, *, ordered=False):
    """Reuse bytes only after exact canonical JSON equality, never by graph id."""

    def encode(value):
        if value == {}:
            return b"{}"
        if value == []:
            return b"[]"
        raw = (_ORDERED if ordered else _CANONICAL).encode(value)
        existing = payloads.get(raw)
        if existing is not None:
            return existing
        if ordered:
            raw = _CANONICAL.encode(value)
            existing = payloads.get(raw)
            if existing is not None:
                return existing
        data = raw.encode("utf-8")
        return b"Z" + zlib.compress(data, 1) if len(data) > 256 else data

    return encode
