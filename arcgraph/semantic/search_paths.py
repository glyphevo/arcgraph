"""Content identities for the pinned backend's static .pth search directories."""

import os
from pathlib import Path

from .import_scan import (
    PTH_WHITESPACE as PTH_WHITESPACE,
    pth_roots as pth_roots,
    python_files as python_files,
)
from .contract import digest
from .snapshot import sha


def directory_identity(root, ancestors=frozenset()):
    """Hash importable text, additions/deletions and symlink destinations.

    Traverse directory aliases as pyright can; cycles and unreadable directories
    fail closed. Non-Python build artifacts do not invalidate editable sources.
    """
    resolved = root.resolve(strict=True)
    if resolved in ancestors:
        raise ValueError("Cyclic semantic search directory")
    rows = []
    with os.scandir(root) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            path = Path(entry.path)
            if entry.is_dir():
                if entry.name == "__pycache__":
                    continue
                checksum = directory_identity(path, ancestors | {resolved})
                if checksum != digest([]) or entry.is_symlink():
                    rows.append(
                        (
                            entry.name,
                            str(path.resolve()) if entry.is_symlink() else None,
                            checksum,
                        )
                    )
            elif path.suffix in {".py", ".pyi"} or path.name == "py.typed":
                rows.append(
                    (
                        entry.name,
                        str(path.resolve()) if entry.is_symlink() else None,
                        sha(path.read_bytes()),
                    )
                )
    return digest(rows)


def search_identity(environment, *, virtual=False):
    return {
        name: (
            "captured-source"
            if path is None
            else directory_identity(path) if path.is_dir() else "absent-directory"
        )
        for name, path in pth_roots(environment, virtual=virtual)
    }
