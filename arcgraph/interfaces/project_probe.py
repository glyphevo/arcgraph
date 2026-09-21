"""Filesystem probes that classify a repository for `arcgraph doctor`."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

IGNORED_PROJECT_PROBE_DIRS = frozenset(
    {".git", "node_modules", "output", "dist", "build", ".venv"}
)
_NODE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"})
_COUNTED_NODE_SUFFIXES = _NODE_SUFFIXES | {".mjs", ".cjs"}


def walk_project_files(root: Path) -> Iterator[Path]:
    """Yield project files, pruning ignored directories before descending.

    rglob cannot prune, so a post-hoc filter still enumerates every entry
    under .git and node_modules — millions in a monorepo. This applies the
    same predicate at the directory level, where it can skip the subtree.
    """

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in IGNORED_PROJECT_PROBE_DIRS and not name.startswith(".")
        )
        current = Path(dirpath)
        for name in sorted(filenames):
            yield current / name


def detect_project_languages(repo_root: Path) -> set[str]:
    languages: set[str] = set()
    if (repo_root / "package.json").is_file() or any(repo_root.glob("tsconfig*.json")):
        languages.add("node")
    if (repo_root / "pyproject.toml").is_file() or (repo_root / "setup.py").is_file():
        languages.add("python")
    if not languages:
        for path in walk_project_files(repo_root):
            if path.suffix in _NODE_SUFFIXES:
                languages.add("node")
            elif path.suffix == ".py":
                languages.add("python")
            if len(languages) == 2:
                break
    return languages


def source_file_counts(root_paths: list[Path]) -> dict[str, int]:
    counts = {"python": 0, "typescript_javascript": 0}
    seen: set[Path] = set()
    for root in root_paths:
        for path in walk_project_files(root):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            if path.suffix == ".py":
                counts["python"] += 1
            elif path.suffix in _COUNTED_NODE_SUFFIXES:
                counts["typescript_javascript"] += 1
    return counts


def ignored_project_probe_path(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(
        part in IGNORED_PROJECT_PROBE_DIRS or part.startswith(".")
        for part in parts[:-1]
    )
