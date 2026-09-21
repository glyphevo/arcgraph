"""Repository-path and output-store containment helpers.

All Change Safety paths enter through this module.  It keeps a user-facing
display path separate from the platform-aware comparison key, and it refuses
foreign absolute syntax before native ``Path`` parsing can reinterpret it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Iterable

from arcgraph.change.errors import OutputContainmentError, RepositoryPathError

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class NormalizedRepositoryPath:
    display_path: str
    comparison_key: str


def is_any_os_absolute(value: str) -> bool:
    """Detect POSIX, Windows-drive, and UNC absolute syntax on every host."""

    return bool(
        value
        and (
            value.startswith("/")
            or value.startswith("\\\\")
            or _WINDOWS_DRIVE.match(value) is not None
        )
    )


def normalize_repository_path(
    value: str | Path,
    repo_root: Path,
    *,
    case_sensitive: bool | None = None,
    tracked_paths: Iterable[str] | None = None,
) -> NormalizedRepositoryPath:
    """Normalize a repository-relative path and prove containment.

    This entrypoint accepts repository-relative paths only.  Call
    :func:`normalize_disk_path` for a prevalidated filesystem path.  Backslash
    input is normalized to a POSIX display path only after rejecting UNC and
    drive syntax; this lets a Windows caller provide a Git-relative path while
    still persisting one portable representation.
    """

    raw = str(value)
    if not raw or not raw.strip() or "\x00" in raw:
        raise RepositoryPathError("path must be a non-empty path without NUL")
    if is_any_os_absolute(raw):
        raise RepositoryPathError("absolute, drive, and UNC paths are not allowed")

    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if str(path) in {"", "."} or any(part in {"", ".", ".."} for part in path.parts):
        raise RepositoryPathError(
            "path traversal and empty path components are not allowed"
        )

    root = repo_root.resolve()
    candidate = (root / Path(*path.parts)).resolve(strict=False)
    if not _is_relative_to(candidate, root):
        raise RepositoryPathError(
            "path escapes the repository through traversal or symlink"
        )

    display_path = "/".join(path.parts)
    comparison_key = _comparison_key(
        display_path,
        case_sensitive=case_sensitive,
        repo_root=root,
    )
    if tracked_paths is not None:
        tracked = {
            _comparison_key(
                item.replace("\\", "/"),
                case_sensitive=case_sensitive,
                repo_root=root,
            ): item.replace("\\", "/")
            for item in tracked_paths
        }
        display_path = tracked.get(comparison_key, display_path)
    return NormalizedRepositoryPath(
        display_path=display_path,
        comparison_key=comparison_key,
    )


def normalize_disk_path(
    path: str | Path,
    repo_root: Path,
    *,
    case_sensitive: bool | None = None,
    tracked_paths: Iterable[str] | None = None,
) -> NormalizedRepositoryPath:
    """Convert an existing repository-contained disk path to a portable path."""

    root = repo_root.resolve()
    candidate = Path(path).resolve(strict=False)
    if not _is_relative_to(candidate, root):
        raise RepositoryPathError("disk path is outside the repository root")
    relative = candidate.relative_to(root).as_posix()
    return normalize_repository_path(
        relative,
        root,
        case_sensitive=case_sensitive,
        tracked_paths=tracked_paths,
    )


def resolve_under_root(root: Path, *components: str) -> Path:
    """Resolve a controlled store location without allowing traversal or symlinks.

    Identifiers such as plan, evidence, and pin IDs are passed as individual
    components.  A component never gets to introduce a directory separator.
    """

    if not components:
        raise OutputContainmentError("a contained path needs at least one component")
    root_resolved = root.resolve(strict=False)
    for component in components:
        if (
            not isinstance(component, str)
            or not component
            or "\x00" in component
            or "/" in component
            or "\\" in component
            or component in {".", ".."}
            or not _SAFE_COMPONENT.fullmatch(component)
        ):
            raise OutputContainmentError(f"unsafe output path component {component!r}")
    candidate = root_resolved.joinpath(*components).resolve(strict=False)
    if not _is_relative_to(candidate, root_resolved):
        raise OutputContainmentError("output path escapes its containment root")
    return candidate


def ensure_contained_path(root: Path, candidate: Path) -> Path:
    """Resolve *candidate* and reject it unless it remains underneath *root*."""

    root_resolved = root.resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if not _is_relative_to(resolved, root_resolved):
        raise OutputContainmentError("path is outside the controlled output root")
    return resolved


def repository_path_comparison_key(value: str, repo_root: Path) -> str:
    """Return the same comparison key used by repository path normalization."""

    return _comparison_key(
        value.replace("\\", "/"),
        case_sensitive=None,
        repo_root=repo_root.resolve(),
    )


def _comparison_key(
    value: str,
    *,
    case_sensitive: bool | None,
    repo_root: Path,
) -> str:
    effective_case_sensitive = (
        case_sensitive
        if case_sensitive is not None
        else _repository_case_sensitive(str(repo_root))
    )
    return value if effective_case_sensitive else value.casefold()


@lru_cache(maxsize=64)
def _repository_case_sensitive(repo_root: str) -> bool:
    """Prefer Git's detected filesystem semantics, then a platform fallback."""

    if not (Path(repo_root) / ".git").exists():
        return os.name != "nt" and sys.platform != "darwin"
    try:
        completed = subprocess.run(
            ["git", "-C", repo_root, "config", "--bool", "core.ignorecase"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        value = completed.stdout.strip().casefold()
        if value in {"true", "false"}:
            return value == "false"
    return os.name != "nt" and sys.platform != "darwin"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
