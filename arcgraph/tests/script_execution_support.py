"""Run fixture shebang programs on Windows as POSIX would, without a shell flag."""

from pathlib import Path


def script_command(argv, root, python, shell):
    if not isinstance(argv, (tuple, list)) or not argv:
        return argv
    path = Path(argv[0])
    if not path.is_absolute() or not path.is_relative_to(root):
        return argv
    try:
        with path.open("rb") as stream:
            first = stream.readline(4096).rstrip(b"\r\n")
    except OSError:
        return argv
    if first == ("#!" + python).encode("utf-8"):
        return [python, str(path), *argv[1:]]
    if first == b"#!/bin/sh":
        if shell is None:
            raise RuntimeError("Git's sh is required for shell-based test fixtures")
        return [shell, str(path), *argv[1:]]
    return argv


def script_popen(original, root, python, shell):
    class FixturePopen(original):
        def __init__(self, args, *positional, **kwargs):
            super().__init__(
                script_command(args, root, python, shell), *positional, **kwargs
            )

    return FixturePopen
