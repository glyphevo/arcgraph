"""Read coverage JSON while discarding redundant coverage.py symbol summaries.

Only the file-level executed lines and contexts are used by the importer.
Validate redundant objects one context-name array at a time without retaining
the report's repeated per-symbol context lists.
"""

from __future__ import annotations

import json
from io import BytesIO, TextIOWrapper
from pathlib import Path
from typing import Any, Callable, TextIO

_OMIT = object()


class _CoverageJSONReader:
    def __init__(self, stream: TextIO, chunk_size: int, retain_files: bool) -> None:
        self.stream = stream
        self.chunk_size = chunk_size
        self.buffer = ""
        self.position = 0
        self.offset = 0
        self.eof = False
        self.decoder = json.JSONDecoder()
        self.retain_files = retain_files

    def _read(self) -> None:
        self.offset += self.position
        self.buffer = self.buffer[self.position :]
        self.position = 0
        chunk = self.stream.read(max(self.chunk_size, len(self.buffer)))
        self.eof = not chunk
        self.buffer += chunk

    def _peek(self) -> str:
        while True:
            while (
                self.position < len(self.buffer)
                and self.buffer[self.position] in " \t\r\n"
            ):
                self.position += 1
            if self.position < len(self.buffer):
                return self.buffer[self.position]
            if self.eof:
                return ""
            self._read()

    def _expect(self, character: str, message: str) -> None:
        if self._peek() != character:
            raise json.JSONDecodeError(message, self.buffer, self.position)
        self.position += 1

    def value(self) -> Any:
        self._peek()
        # Keep lookahead before decoding: a partial value constructs an
        # exception that counts every preceding newline in the whole buffer.
        if (
            not self.eof
            and self.position
            and len(self.buffer) - self.position < self.chunk_size
        ):
            self._read()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
            except json.JSONDecodeError:
                if self.eof:
                    raise
                self._read()
                continue
            # Numbers can end at a chunk boundary or before a partial exponent.
            if not self.eof and (
                end == len(self.buffer) or self.buffer[end] not in " \t\r\n,]}:"
            ):
                del value
                self._read()
                continue
            self.position = end
            return value

    def object(
        self, reader: Callable[[str], Any] | None = None, *, keep: bool = True
    ) -> dict[str, Any]:
        self._expect("{", "Expecting value")
        result = {}
        if self._peek() == "}":
            self.position += 1
            return result
        while True:
            if self._peek() != '"':
                raise json.JSONDecodeError(
                    "Expecting property name enclosed in double quotes",
                    self.buffer,
                    self.position,
                )
            key = self.value()
            self._expect(":", "Expecting ':' delimiter")
            value = reader(key) if reader else self.value()
            if keep and value is not _OMIT:
                result[key] = value
            del value
            if self._peek() == "}":
                self.position += 1
                return result
            self._expect(",", "Expecting ',' delimiter")

    def file_field(self, key: str) -> Any:
        if key not in {"functions", "classes"}:
            return self.value()
        return self.discard_value()

    def discard_value(self, key: str = "") -> Any:
        # In a coverage.py summary the large values are context-name arrays.
        # Validate one such array at a time rather than keeping a whole
        # function/class's repeated context dictionaries until its end.
        if self._peek() == "{":
            self.object(self.discard_value, keep=False)
        else:
            self.value()
        return _OMIT

    def file_record(self, key: str) -> Any:
        return self.object(self.file_field) if self._peek() == "{" else self.value()

    def root_field(self, key: str) -> Any:
        if key == "files" and self._peek() == "{":
            if not self.retain_files:
                return self.discard_value()
            return self.object(self.file_record)
        return self.value()

    def document(self) -> Any:
        try:
            payload = (
                self.object(self.root_field) if self._peek() == "{" else self.value()
            )
            if self._peek():
                raise json.JSONDecodeError("Extra data", self.buffer, self.position)
            return payload
        except json.JSONDecodeError as exc:
            if self.offset:
                self._restore_error_position(exc)
            raise

    def _restore_error_position(self, exc: json.JSONDecodeError) -> None:
        """Recount the prefix only on failure, without retaining it in memory."""
        position = self.offset + exc.pos
        exc.pos = position
        exc.args = (f"{exc.msg} at character {position}",)
        try:
            self.stream.seek(0)
            remaining = position
            line = column = 1
            while remaining:
                chunk = self.stream.read(min(self.chunk_size, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                newlines = chunk.count("\n")
                line += newlines
                column = (
                    len(chunk) - chunk.rfind("\n") if newlines else column + len(chunk)
                )
        except (OSError, AttributeError):
            # Nonseekable private streams can still report an absolute offset.
            return
        exc.lineno, exc.colno = line, column
        exc.args = (f"{exc.msg}: line {line} column {column} (char {position})",)


def load_coverage_json(
    stream: TextIO, *, chunk_size: int = 4 * 1024 * 1024, retain_files: bool = True
) -> Any:
    """Preserve other JSON coverage formats and validate discarded sections too."""
    return _CoverageJSONReader(stream, chunk_size, retain_files).document()


def read_coverage_json(
    path: Path, *, frozen_inputs=None, retain_files: bool = True
) -> Any:
    """Read the same live or captured artifact as the other artifact consumers."""
    with (
        TextIOWrapper(BytesIO(frozen_inputs.read_bytes(path)), encoding="utf-8")
        if frozen_inputs is not None
        else path.open(encoding="utf-8")
    ) as stream:
        return load_coverage_json(stream, retain_files=retain_files)
