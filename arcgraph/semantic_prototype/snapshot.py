"""Frozen source views and conversions; never read an ambient working tree."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tokenize

from arcgraph.analyzers.precision_positions import PrecisionPositions
from .contract import Snapshot, Source, Span


def canonical(raw: bytes) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def capture(
    root: Path,
    paths: list[str],
    destination: Path,
    *,
    target_python: str,
    platform: str,
    environment: tuple[tuple[str, str], ...] = (("dependencies", "unknown"),),
) -> Snapshot:
    """Capture even uncommitted contents; no symlinks or implicit discovery."""
    if destination.exists():
        raise ValueError("snapshot destination must be new")
    rows = []
    contents = []
    for rel in sorted(paths):
        # Source validates the spelling before any disk read.
        Source(path=rel, raw_digest="", text_digest="")
        path = root / rel
        if any(
            p.is_symlink() for p in [path, *path.parents]
        ) or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("symlink or escaped source")
        raw = path.read_bytes()
        rows.append(
            Source(
                path=rel, raw_digest=sha(raw), text_digest=sha(canonical(raw).encode())
            )
        )
        contents.append((rel, raw))
    snapshot = Snapshot(
        files=tuple(rows),
        target_python=target_python,
        platform=platform,
        environment=environment,
    )
    destination.mkdir(parents=True)
    for rel, raw in contents:
        path = destination / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    verify(snapshot, destination)
    return snapshot


def verify(snapshot: Snapshot, root: Path) -> None:
    for f in snapshot.files:
        path = root / f.path
        if (
            any(p.is_symlink() for p in [path, *path.parents])
            or sha(path.read_bytes()) != f.raw_digest
            or sha(canonical(path.read_bytes()).encode()) != f.text_digest
        ):
            raise ValueError("frozen source changed")


def column(text: str, line: int, col: int, unit: str) -> int:
    enc = {"utf-8": 1, "utf-16": 2, "unicode_scalar": 3}
    if (
        type(line) is not int
        or type(col) is not int
        or line < 0
        or col < 0
        or line >= len(text.split("\n"))
        or unit not in enc
    ):
        raise ValueError("unconvertible position")
    return PrecisionPositions._byte_column(text.split("\n")[line], col, enc[unit])


def native_span(text: str, value: dict, unit: str) -> Span:
    a, b = value["start"], value["end"]
    return Span(
        start=(a["line"], column(text, a["line"], a["character"], unit)),
        end=(b["line"], column(text, b["line"], b["character"], unit)),
    )


def native_position(text: str, position: tuple[int, int], unit: str) -> dict:
    line, byte = position
    column(text, line, byte, "utf-8")
    prefix = text.split("\n")[line].encode()[:byte].decode()
    col = (
        len(prefix.encode("utf-16-le")) // 2
        if unit == "utf-16"
        else len(prefix.encode()) if unit == "utf-8" else len(prefix)
    )
    return {"line": line, "character": col}
