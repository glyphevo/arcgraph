"""Content identities for the pinned backend's static .pth search directories."""

import os
from pathlib import Path
import tempfile

from .contract import digest
from .snapshot import sha

# Match JavaScript trim() and /^import\s/, used by pinned pyright. Python's
# default strip()/\s differ for BOM and some control characters.
PTH_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def pth_roots(environment, *, virtual=False):
    """Resolve paths in the frozen layout, without executing import lines.

    For freshness, map environment-contained paths back to the live environment.
    Relative paths escaping it are resolved at the same depth below TMPDIR as a
    SemanticBuild, rather than relative to the original project's environment.
    """
    if environment is None:
        return []
    environment = Path(environment).resolve()
    staging = (
        Path(tempfile.gettempdir()).resolve() / "arcgraph-build-semantic-identity"
        if virtual
        else environment.parent
    )
    frozen = staging / "environment" if virtual else environment
    sites = sorted(environment.glob("lib/python*/site-packages"))
    if (environment / "Lib/site-packages").is_dir():
        sites.append(environment / "Lib/site-packages")
    roots = []
    for site in sites:
        for path in sorted(site.glob("*.pth")):
            raw = path.read_bytes()
            # pyright 1.1.414's getPathsFromPthFiles uses this size bound.
            if not 0 < len(raw) < 65536:
                continue
            for line in raw.decode("utf-8").split("\n"):
                line = line.strip(PTH_WHITESPACE)
                if (
                    not line
                    or line.startswith("#")
                    or line.startswith("import")
                    and len(line) > 6
                    and line[6] in PTH_WHITESPACE
                ):
                    continue
                target = (frozen / site.relative_to(environment) / line).resolve()
                if target.is_relative_to(frozen):
                    relative = target.relative_to(frozen)
                    roots.append(
                        ("environment/" + relative.as_posix(), environment / relative)
                    )
                elif target.is_relative_to(staging / "source"):
                    # These bytes already belong to the captured source snapshot.
                    roots.append(
                        (
                            "source/"
                            + target.relative_to(staging / "source").as_posix(),
                            None,
                        )
                    )
                elif target.is_relative_to(staging):
                    # Non-source staging directories are not stable search inputs.
                    raise ValueError("Unmodelled relative semantic search directory")
                else:
                    roots.append((str(target), target))
    return roots


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


def python_files(root):
    """Include directory aliases in external import guards, without cycles."""
    pending, seen = [root] if root.is_dir() else [], set()
    while pending:
        directory = pending.pop()
        resolved = directory.resolve(strict=True)
        if resolved in seen:
            continue
        seen.add(resolved)
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir():
                    if entry.name != "__pycache__":
                        pending.append(Path(entry.path))
                elif Path(entry.name).suffix in {".py", ".pyi"}:
                    yield Path(entry.path)


def search_identity(environment, *, virtual=False):
    return {
        name: (
            "captured-source"
            if path is None
            else directory_identity(path) if path.is_dir() else "absent-directory"
        )
        for name, path in pth_roots(environment, virtual=virtual)
    }
