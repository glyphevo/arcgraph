"""Runtime package identity and provenance reporting."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from arcgraph import READ_SCHEMA_VERSION, SCHEMA_VERSION, __version__

# Identity of the running code cannot change inside one process; only the
# working-tree flags can. A short reuse window keeps repeated index_status
# calls cheap while an interactive edit still shows up promptly.
_VERSION_INFO_TTL_SECONDS = 5.0
_VERSION_INFO_CACHE: tuple[float, dict[str, Any]] | None = None


def version_info(*, refresh: bool = False) -> dict[str, Any]:
    """Return an auditable identity for the exact ArcGraph code being executed.

    Hashing every runtime file and shelling out to git costs seconds on large
    trees, and `index_status` — the cheapest documented check — reports this
    on every call. Results are therefore reused for
    _VERSION_INFO_TTL_SECONDS; callers that must observe live working-tree
    state (the `version` command) pass ``refresh=True``.
    """

    global _VERSION_INFO_CACHE
    if not refresh:
        cached = _VERSION_INFO_CACHE
        if cached is not None:
            captured_at, payload = cached
            if time.monotonic() - captured_at < _VERSION_INFO_TTL_SECONDS:
                return copy.deepcopy(payload)

    payload = _build_version_info()
    _VERSION_INFO_CACHE = (time.monotonic(), copy.deepcopy(payload))
    return payload


def _build_version_info() -> dict[str, Any]:
    package_dir = Path(__file__).resolve().parent
    source_root = package_dir.parent
    source_checkout = (source_root / "pyproject.toml").is_file() and (
        source_root / ".git"
    ).exists()
    runtime_sha256 = _runtime_fingerprint(package_dir)
    source = _source_provenance(source_root) if source_checkout else None
    artifact = None if source_checkout else _installed_artifact_provenance(package_dir)
    build = None if source_checkout else _embedded_build_provenance(package_dir)

    if source is not None:
        suffix = f"g{source['commit_sha'][:12]}"
        if not source["working_tree_clean"]:
            suffix += f".dirty.{runtime_sha256[:12]}"
        display_version = f"{__version__}+{suffix}"
        provenance_status = "source_checkout"
    elif artifact and _mapping_value(artifact, "sha256"):
        source_label = _build_source_label(build)
        display_version = f"{__version__}+{source_label}wheel.{artifact['sha256'][:12]}"
        provenance_status = "verified_artifact_hash"
    else:
        display_version = f"{__version__}+runtime.{runtime_sha256[:12]}"
        provenance_status = "runtime_fingerprint_only"

    return {
        "schema_version": "1.0.0",
        "status": "available",
        "product_version": __version__,
        "display_version": display_version,
        "read_schema_version": READ_SCHEMA_VERSION,
        "index_schema_version": SCHEMA_VERSION,
        "execution_mode": (
            "source_checkout" if source_checkout else "installed_distribution"
        ),
        "provenance_status": provenance_status,
        "source_provenance": source,
        "build_provenance": build,
        "artifact_provenance": artifact,
        "runtime_fingerprint": {
            "algorithm": "sha256",
            "sha256": runtime_sha256,
            "scope": "installed_arcgraph_runtime_files",
        },
        "non_claims": _identity_non_claims(source=source, build=build),
    }


def _build_source_label(build: dict[str, Any] | None) -> str:
    source = _mapping_value(build, "source", {})
    commit = _mapping_value(source, "commit_sha")
    if not isinstance(commit, str):
        return ""
    label = f"g{commit[:12]}"
    if _mapping_value(source, "working_tree_clean") is False:
        status_hash = _mapping_value(source, "working_tree_status_sha256")
        if isinstance(status_hash, str):
            label += f".dirty.{status_hash[:12]}"
    return f"{label}."


def _identity_non_claims(
    *, source: dict[str, Any] | None, build: dict[str, Any] | None
) -> list[str]:
    if source is not None:
        return []
    build_source = _mapping_value(build, "source", {})
    if not _mapping_value(build_source, "commit_sha"):
        return [
            "The installed wheel does not itself claim a Git commit; bind its artifact SHA-256 to the release provenance manifest."
        ]
    if _mapping_value(build_source, "working_tree_clean") is False:
        return [
            "The wheel was built from a dirty working tree; its commit alone cannot reproduce the artifact. Use the artifact SHA-256 and dirty-state fingerprint."
        ]
    return []


def _embedded_build_provenance(package_dir: Path) -> dict[str, Any] | None:
    path = package_dir / "_build_provenance.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or _mapping_value(value, "schema_version") != "1.0.0"
    ):
        return None
    source = _mapping_value(value, "source")
    if not isinstance(source, dict):
        return None
    commit = _mapping_value(source, "commit_sha")
    tree = _mapping_value(source, "tree_sha")
    status_hash = _mapping_value(source, "working_tree_status_sha256")
    clean = _mapping_value(source, "working_tree_clean")
    if not _is_hex_digest(commit, 40) or not _is_hex_digest(tree, 40):
        return None
    if not isinstance(clean, bool) or not _is_hex_digest(status_hash, 64):
        return None
    return value


def _installed_artifact_provenance(package_dir: Path) -> dict[str, Any] | None:
    try:
        distribution = importlib.metadata.distribution("arcgraph")
    except importlib.metadata.PackageNotFoundError:
        return None
    try:
        installed_package = Path(distribution.locate_file("arcgraph")).resolve()
        if installed_package != package_dir:
            return None
        direct_url_text = distribution.read_text("direct_url.json")
        direct_url = json.loads(direct_url_text) if direct_url_text else {}
    except (OSError, TypeError, json.JSONDecodeError):
        direct_url = {}

    archive_info = _mapping_value(direct_url, "archive_info", {})
    hashes = _mapping_value(archive_info, "hashes", {})
    sha256 = _mapping_value(hashes, "sha256")
    if sha256 is None and isinstance(archive_info, dict):
        legacy_hash = _mapping_value(archive_info, "hash")
        if isinstance(legacy_hash, str) and legacy_hash.startswith("sha256="):
            sha256 = legacy_hash.split("=", 1)[1]
    if not _is_hex_digest(sha256, 64):
        sha256 = None
    return {
        "sha256": sha256,
        "identity_source": (
            "pep610_direct_url_archive_hash" if sha256 else "unavailable"
        ),
    }


def _source_provenance(source_root: Path) -> dict[str, Any] | None:
    try:
        commit = _git(source_root, ["rev-parse", "HEAD"]).decode("ascii").strip()
        tree = _git(source_root, ["rev-parse", "HEAD^{tree}"]).decode("ascii").strip()
        status = _git(
            source_root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    return {
        "commit_sha": commit,
        "tree_sha": tree,
        "working_tree_clean": not status,
        "working_tree_status_sha256": hashlib.sha256(status).hexdigest(),
    }


def _git(source_root: Path, args: list[str]) -> bytes:
    completed = subprocess.run(
        ["git", "-C", os.fspath(source_root), *args],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("git provenance query failed")
    return completed.stdout


def _runtime_fingerprint(package_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package_dir.rglob("*")):
        relative = path.relative_to(package_dir)
        if "__pycache__" in relative.parts or "tests" in relative.parts:
            continue
        if path.is_symlink():
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0symlink\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            continue
        if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _is_hex_digest(value: Any, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _mapping_value(mapping: Any, key: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        return default
    return mapping[key]
