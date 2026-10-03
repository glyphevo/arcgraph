"""Make the process's text streams UTF-8 regardless of the locale's code page."""

from __future__ import annotations

import sys


def use_utf8_stdio() -> None:
    """Write stdout and stderr as UTF-8.

    When stdout is a pipe, Python encodes it with the locale's code page, which
    on Windows is often cp1252: printing a path or symbol it cannot represent
    then raises ``UnicodeEncodeError`` after the work is done. Only the
    encoding changes; each stream keeps its error handler. The MCP stdio
    transport writes to the streams' binary buffers and is not affected.
    Streams without ``reconfigure`` (pytest capture, ``StringIO``) are left
    alone.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # Naming an encoding resets errors to "strict" unless it is passed.
            reconfigure(encoding="utf-8", errors=getattr(stream, "errors", None))
        except (OSError, ValueError):
            # A closed or detached stream has nothing left to protect.
            continue
