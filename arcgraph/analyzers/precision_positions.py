"""Coordinates used only by the optional precision evidence importer."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


class PrecisionPositions:
    """Translate SCIP columns to AST bytes and locate actual callee tokens.

    The caches live for one import. They do not establish evidence freshness;
    snapshot validation must happen before invoking the importer.
    """

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.sources: dict[str, str | None] = {}
        self.callees: dict[str, dict[tuple[int, int], tuple[int, ...]]] = {}

    def source(self, path: str) -> str | None:
        if path not in self.sources:
            candidate = (self.repo_root / path).resolve()
            try:
                self.sources[path] = (
                    candidate.read_text(encoding="utf-8-sig")
                    if candidate.is_relative_to(self.repo_root)
                    else None
                )
            except (OSError, UnicodeError):
                self.sources[path] = None
        return self.sources[path]

    def normalize_scip(
        self, record: dict[str, Any], document: dict[str, Any], path: str
    ) -> dict[str, Any]:
        value = record.get("range")
        if value is None:
            return record  # ArcGraph's line/column contract already uses AST bytes.
        if (
            not isinstance(value, list)
            or len(value) not in {3, 4}
            or any(type(item) is not int or item < 0 for item in value)
        ):
            raise ValueError(
                "SCIP range must contain three or four nonnegative integers"
            )
        start_line, start_column = value[:2]
        end_line, end_column = (start_line, value[2]) if len(value) == 3 else value[2:]
        if (end_line, end_column) <= (start_line, start_column):
            raise ValueError("SCIP range must have an exclusive end after its start")
        encoding = document.get(
            "position_encoding", document.get("positionEncoding", 0)
        )
        encodings = {
            "UnspecifiedPositionEncoding": 0,
            "UTF8CodeUnitOffsetFromLineStart": 1,
            "UTF16CodeUnitOffsetFromLineStart": 2,
            "UTF32CodeUnitOffsetFromLineStart": 3,
        }
        encoding = (
            encodings.get(encoding, encoding) if isinstance(encoding, str) else encoding
        )
        if type(encoding) is not int or encoding not in {0, 1, 2, 3}:
            raise ValueError(f"Unsupported SCIP position encoding: {encoding!r}")
        source = self.source(path)
        if source is None:
            raise ValueError("Source text is unavailable for SCIP column conversion")
        lines = source.split("\n")
        if end_line >= len(lines):
            raise ValueError("SCIP range is outside the source document")
        columns = []
        for line, column in ((start_line, start_column), (end_line, end_column)):
            text = lines[line]
            if encoding == 0:
                # Legacy ASCII offsets have the same meaning in all three encodings.
                if not text[:column].isascii():
                    raise ValueError(
                        "SCIP position encoding is unspecified on a non-ASCII prefix"
                    )
                columns.append(self._byte_column(text, column, 1))
            else:
                columns.append(self._byte_column(text, column, encoding))
        normalized = (
            [start_line, *columns]
            if len(value) == 3
            else [start_line, columns[0], end_line, columns[1]]
        )
        return {**record, "range": normalized}

    @staticmethod
    def _byte_column(text: str, column: int, encoding: int) -> int:
        units = 0
        byte_column = 0
        for char in text:
            if units == column:
                return byte_column
            units += (
                len(char.encode("utf-8"))
                if encoding == 1
                else len(char.encode("utf-16-le")) // 2 if encoding == 2 else 1
            )
            byte_column += len(char.encode("utf-8"))
            if units > column:
                raise ValueError("SCIP column splits an encoded character")
        if units == column:
            return byte_column
        raise ValueError("SCIP column is outside the source line")

    def callee_span(self, path: str, line: int, column: int) -> tuple[int, ...] | None:
        if path not in self.callees:
            spans: dict[tuple[int, int], tuple[int, ...]] = {}
            source = self.source(path)
            try:
                tree = ast.parse(source) if source is not None else None
            except (SyntaxError, ValueError):
                tree = None
            if tree is not None:
                lines = source.split("\n")
                for call in ast.walk(tree):
                    if not isinstance(call, ast.Call):
                        continue
                    func = call.func
                    if isinstance(func, ast.Name):
                        span = (
                            func.lineno,
                            func.col_offset,
                            func.end_lineno,
                            func.end_col_offset,
                        )
                    elif isinstance(func, ast.Attribute):
                        # AST identifiers undergo NFKC normalization; token widths
                        # must come from the original text (e.g. K becomes K).
                        prefix = (
                            lines[func.end_lineno - 1]
                            .encode("utf-8")[: func.end_col_offset]
                            .decode("utf-8")
                        )
                        start = len(prefix)
                        # Include XID_Continue characters such as combining marks;
                        # tokenize.NAME does not cover every valid Python identifier.
                        while start and ("a" + prefix[start - 1]).isidentifier():
                            start -= 1
                        start_byte = func.end_col_offset - len(
                            prefix[start:].encode("utf-8")
                        )
                        span = (
                            func.end_lineno,
                            start_byte,
                            func.end_lineno,
                            func.end_col_offset,
                        )
                    else:
                        continue
                    spans[(call.lineno, call.col_offset)] = span
            self.callees[path] = spans
        return self.callees[path].get((line, column))
