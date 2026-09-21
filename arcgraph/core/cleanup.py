"""Safe cleanup helpers for generated ArcGraph output."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import time
from typing import Any

from arcgraph.change.contracts import CHANGE_CONTRACT_VERSION
from arcgraph.core.evidence_manifest import evidence_manifest_path
from arcgraph.core.operation_lock import arcgraph_operation_lock
from arcgraph.core.schemas import SCHEMA_VERSION

DEFAULT_BUILD_RETENTION = 3
# Sizes change only when a build is published or pruned, so a short reuse
# window keeps repeated index_status calls off the file system.
_STORAGE_STATUS_TTL_SECONDS = 5.0
_STORAGE_STATUS_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_INPUT_ARTIFACT_PATTERNS = (
    "coverage.xml",
    "index*.scip",
    "pyright-export*.json",
    "pyright-precision.json",
    "runtime-trace.json",
    "scip-*.json",
)


@dataclass(frozen=True)
class CleanupCandidate:
    kind: str
    path: Path
    relative_path: str
    bytes: int
    reason: str
    index_version: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.relative_path,
            "bytes": self.bytes,
            "mb": round(self.bytes / (1024 * 1024), 2),
            "reason": self.reason,
            **({"index_version": self.index_version} if self.index_version else {}),
        }


@dataclass(frozen=True)
class CleanupPlan:
    output_dir: Path
    keep_builds: int
    current_build: str | None
    kept_builds: list[str]
    candidates: list[CleanupCandidate]
    warnings: list[str]
    pinned_builds: list[str] = field(default_factory=list)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(candidate.bytes for candidate in self.candidates)

    def to_payload(
        self,
        *,
        dry_run: bool,
        deleted: list[CleanupCandidate] | None = None,
        errors: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        deleted = deleted or []
        errors = errors or []
        if errors:
            status = "partial"
        elif dry_run and self.candidates:
            status = "dry_run"
        elif not dry_run and deleted:
            status = "pruned"
        else:
            status = "pass"
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "dry_run": dry_run,
            "output_dir": str(self.output_dir),
            "keep_builds": self.keep_builds,
            "current_build": self.current_build,
            "kept_builds": self.kept_builds,
            "pinned_builds": self.pinned_builds,
            "candidate_count": len(self.candidates),
            "reclaimable_bytes": self.reclaimable_bytes,
            "reclaimable_mb": round(self.reclaimable_bytes / (1024 * 1024), 2),
            "candidates": [candidate.to_payload() for candidate in self.candidates],
            "deleted_count": len(deleted),
            "deleted_bytes": sum(candidate.bytes for candidate in deleted),
            "deleted": [candidate.to_payload() for candidate in deleted],
            "warnings": self.warnings,
            "errors": errors,
        }
        if errors:
            payload["_exit_code"] = 1
        return payload


def plan_arcgraph_output_cleanup(
    output_dir: Path,
    *,
    keep_builds: int = DEFAULT_BUILD_RETENTION,
    include_input_artifacts: bool = False,
) -> CleanupPlan:
    """Plan cleanup for one ArcGraph output directory.

    The default scope is intentionally narrow: stale build directories under
    ``builds/`` only. The current build from ``current.json`` is always kept,
    and cleanup is disabled when that current build cannot be resolved safely.
    """
    output_dir = output_dir.resolve()
    keep_builds = max(1, keep_builds)
    warnings: list[str] = []
    builds_dir = output_dir / "builds"
    builds_root = _safe_resolve(builds_dir)

    if not output_dir.exists():
        return CleanupPlan(
            output_dir=output_dir,
            keep_builds=keep_builds,
            current_build=None,
            kept_builds=[],
            candidates=[],
            warnings=[f"Output directory does not exist: {output_dir}"],
        )
    if not builds_dir.is_dir() or builds_dir.is_symlink():
        return CleanupPlan(
            output_dir=output_dir,
            keep_builds=keep_builds,
            current_build=None,
            kept_builds=[],
            candidates=[],
            warnings=[f"No safe builds directory found at {builds_dir}"],
        )

    current_build, current_warning = _current_build(output_dir, builds_root)
    if current_warning:
        warnings.append(current_warning)
    if current_build is None:
        warnings.append("No current build resolved; refusing to prune build history.")
        return CleanupPlan(
            output_dir=output_dir,
            keep_builds=keep_builds,
            current_build=None,
            kept_builds=[],
            candidates=[],
            warnings=warnings,
        )

    builds = _safe_build_dirs(output_dir, builds_dir, builds_root, warnings)
    current = next(
        (build for build in builds if _same_path(build["resolved"], current_build)),
        None,
    )
    if current is None:
        warnings.append(
            "Current build directory is missing; refusing to prune build history."
        )
        return CleanupPlan(
            output_dir=output_dir,
            keep_builds=keep_builds,
            current_build=_relative_to_output(output_dir, current_build),
            kept_builds=[],
            candidates=[],
            warnings=warnings,
        )

    pinned_versions, pin_warning = _pinned_build_versions(output_dir)
    if pin_warning:
        warnings.append(pin_warning)
        warnings.append(
            "Pinned build state is unreadable; refusing to prune build history."
        )
        return CleanupPlan(
            output_dir=output_dir,
            keep_builds=keep_builds,
            current_build=_relative_to_output(output_dir, current["path"]),
            kept_builds=[_relative_to_output(output_dir, current["path"])],
            candidates=[],
            warnings=warnings,
        )
    pinned_builds = sorted(
        build["path"].name for build in builds if build["path"].name in pinned_versions
    )

    incomplete_candidates = [
        CleanupCandidate(
            kind="build",
            path=build["path"],
            relative_path=_relative_to_output(output_dir, build["path"]),
            bytes=_path_size(build["path"]),
            reason="incomplete or unpublished build",
            index_version=build["path"].name,
        )
        for build in builds
        if (
            build is not current
            and build["path"].name not in pinned_versions
            and not _build_is_complete(build["path"])
        )
    ]
    retention_builds = [
        build
        for build in builds
        if build is current or _build_is_complete(build["path"])
    ]

    kept = [current]
    kept.extend(
        build
        for build in retention_builds
        if build is not current and build["path"].name in pinned_versions
    )
    previous = [
        build
        for build in retention_builds
        if build is not current and build["path"].name not in pinned_versions
    ]
    previous.sort(key=lambda item: item["path"].name, reverse=True)
    kept.extend(previous[: keep_builds - 1])
    kept_paths = {build["resolved"] for build in kept}

    candidates = incomplete_candidates + [
        CleanupCandidate(
            kind="build",
            path=build["path"],
            relative_path=_relative_to_output(output_dir, build["path"]),
            bytes=_path_size(build["path"]),
            reason=f"older than the {keep_builds} retained build(s)",
            index_version=build["path"].name,
        )
        for build in retention_builds
        if (
            build["resolved"] not in kept_paths
            and build["path"].name not in pinned_versions
        )
    ]
    if include_input_artifacts:
        candidates.extend(_input_artifact_candidates(output_dir, warnings))

    return CleanupPlan(
        output_dir=output_dir,
        keep_builds=keep_builds,
        current_build=_relative_to_output(output_dir, current["path"]),
        kept_builds=[
            _relative_to_output(output_dir, build["path"])
            for build in sorted(kept, key=lambda item: item["path"].name, reverse=True)
        ],
        candidates=sorted(candidates, key=lambda item: (item.kind, item.relative_path)),
        warnings=warnings,
        pinned_builds=pinned_builds,
    )


def prune_arcgraph_output(
    output_dir: Path,
    *,
    keep_builds: int = DEFAULT_BUILD_RETENTION,
    include_input_artifacts: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan and optionally delete stale generated ArcGraph output."""
    if dry_run:
        plan = plan_arcgraph_output_cleanup(
            output_dir,
            keep_builds=keep_builds,
            include_input_artifacts=include_input_artifacts,
        )
        return plan.to_payload(dry_run=True)

    preflight = plan_arcgraph_output_cleanup(
        output_dir,
        keep_builds=keep_builds,
        include_input_artifacts=include_input_artifacts,
    )
    if not preflight.candidates:
        return preflight.to_payload(dry_run=False)

    with arcgraph_operation_lock(output_dir):
        plan = plan_arcgraph_output_cleanup(
            output_dir,
            keep_builds=keep_builds,
            include_input_artifacts=include_input_artifacts,
        )
        return _delete_planned_candidates(plan)


def prune_arcgraph_output_after_publish(
    output_dir: Path,
    *,
    keep_builds: int = DEFAULT_BUILD_RETENTION,
) -> dict[str, Any]:
    """Prune build history after a build has published ``current.json``.

    This is intended for build/reindex callers that already hold the
    ArcGraph operation lock. It deliberately does not delete root-level
    precision, coverage, or runtime input artifacts.
    """
    try:
        plan = plan_arcgraph_output_cleanup(
            output_dir,
            keep_builds=keep_builds,
            include_input_artifacts=False,
        )
    except Exception as exc:  # pragma: no cover - defensive filesystem boundary
        return _cleanup_error_payload(output_dir, keep_builds, exc)
    if not plan.candidates:
        return plan.to_payload(dry_run=False)
    return _delete_planned_candidates(plan)


def arcgraph_output_storage_status(
    output_dir: Path,
    *,
    keep_builds: int = DEFAULT_BUILD_RETENTION,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return a read-only disk lifecycle summary and a safe prune preview.

    Status assembly never deletes files. The suggested command is a dry run
    unless an operator adds ``--apply`` explicitly.

    Sizing stats every file under the output directory, which `index_status`
    reports on every call. Results are reused for
    _STORAGE_STATUS_TTL_SECONDS per output directory; sizes only change when
    a build is published or pruned, and callers that must see that
    immediately pass ``refresh=True``.
    """

    output_dir = output_dir.resolve()
    # keep_builds changes which builds are candidates and the command the
    # payload suggests, so it is part of the identity of a cached answer.
    cache_key = (str(output_dir), int(keep_builds))
    if not refresh:
        cached = _STORAGE_STATUS_CACHE.get(cache_key)
        if cached is not None and (
            time.monotonic() - cached[0] < _STORAGE_STATUS_TTL_SECONDS
        ):
            return copy.deepcopy(cached[1])
    payload = _storage_status_payload(output_dir, keep_builds=keep_builds)
    _STORAGE_STATUS_CACHE[cache_key] = (time.monotonic(), copy.deepcopy(payload))
    return payload


def _storage_status_payload(
    output_dir: Path,
    *,
    keep_builds: int,
) -> dict[str, Any]:
    plan = plan_arcgraph_output_cleanup(output_dir, keep_builds=keep_builds)
    total_bytes = _path_size(output_dir) if output_dir.exists() else 0
    builds_dir = output_dir / "builds"
    build_count = 0
    current_build_bytes = 0
    if builds_dir.is_dir() and not builds_dir.is_symlink():
        try:
            builds_root = builds_dir.resolve()
            build_warnings: list[str] = []
            builds = _safe_build_dirs(
                output_dir, builds_dir, builds_root, build_warnings
            )
            build_count = len(builds)
        except OSError:
            builds = []
        if plan.current_build:
            current_path = output_dir / plan.current_build
            current_build_bytes = _path_size(current_path)

    return {
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "build_count": build_count,
        "current_build": plan.current_build,
        "current_build_bytes": current_build_bytes,
        "pinned_builds": plan.pinned_builds,
        "cleanup_candidate_count": len(plan.candidates),
        "reclaimable_bytes": plan.reclaimable_bytes,
        "reclaimable_mb": round(plan.reclaimable_bytes / (1024 * 1024), 2),
        "safe_prune": {
            "command": f"arcgraph ops prune --keep-builds {plan.keep_builds}",
            "mode": "dry_run",
            "apply_requires_explicit_flag": "--apply",
        },
        "status_is_read_only": True,
        "warnings": plan.warnings,
    }


def _cleanup_error_payload(
    output_dir: Path,
    keep_builds: int,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "partial",
        "dry_run": False,
        "output_dir": str(output_dir.resolve()),
        "keep_builds": max(1, keep_builds),
        "current_build": None,
        "kept_builds": [],
        "pinned_builds": [],
        "candidate_count": 0,
        "reclaimable_bytes": 0,
        "reclaimable_mb": 0,
        "candidates": [],
        "deleted_count": 0,
        "deleted_bytes": 0,
        "deleted": [],
        "warnings": ["Automatic cleanup failed after index publication."],
        "errors": [
            {
                "path": str(output_dir),
                "error": type(exc).__name__,
                "message": str(exc),
            }
        ],
    }


def _delete_planned_candidates(plan: CleanupPlan) -> dict[str, Any]:
    deleted: list[CleanupCandidate] = []
    errors: list[dict[str, str]] = []
    for candidate in plan.candidates:
        try:
            _delete_candidate(plan.output_dir, candidate)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(
                {
                    "path": candidate.relative_path,
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            )
        else:
            deleted.append(candidate)
    return plan.to_payload(dry_run=False, deleted=deleted, errors=errors)


def _current_build(
    output_dir: Path, builds_root: Path
) -> tuple[Path | None, str | None]:
    current_path = output_dir / "current.json"
    try:
        current = json.loads(current_path.read_text(encoding="utf-8"))
        build_rel = Path(str(current["build_dir"]))
    except FileNotFoundError:
        return None, f"No current.json found at {current_path}"
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        return (
            None,
            f"Could not read current build from {current_path}: {type(exc).__name__}",
        )

    if build_rel.is_absolute() or not build_rel.parts or build_rel.parts[0] != "builds":
        return None, f"Refusing unsafe current build path: {build_rel}"

    try:
        current_build = (output_dir / build_rel).resolve()
        current_build.relative_to(builds_root)
    except (OSError, ValueError) as exc:
        return None, f"Current build is outside builds directory: {type(exc).__name__}"
    return current_build, None


def _safe_build_dirs(
    output_dir: Path,
    builds_dir: Path,
    builds_root: Path,
    warnings: list[str],
) -> list[dict[str, Any]]:
    builds: list[dict[str, Any]] = []
    for child in builds_dir.iterdir():
        if child.is_symlink():
            warnings.append(
                f"Skipped symlink build entry: {_relative_to_output(output_dir, child)}"
            )
            continue
        if not child.is_dir():
            continue
        try:
            resolved = child.resolve()
            resolved.relative_to(builds_root)
        except (OSError, ValueError) as exc:
            warnings.append(
                f"Skipped unsafe build entry {_relative_to_output(output_dir, child)}: {type(exc).__name__}"
            )
            continue
        builds.append({"path": child, "resolved": resolved})
    return builds


def _build_is_complete(build_dir: Path) -> bool:
    return (build_dir / "index.sqlite").is_file() and (
        build_dir / "summary.json"
    ).is_file()


def _input_artifact_candidates(
    output_dir: Path,
    warnings: list[str],
) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    seen: set[Path] = set()
    manifest_path = evidence_manifest_path(output_dir).resolve()
    for pattern in _INPUT_ARTIFACT_PATTERNS:
        for path in output_dir.glob(pattern):
            if path in seen:
                continue
            seen.add(path)
            if path.resolve(strict=False) == manifest_path:
                continue
            if path.is_symlink() or not path.is_file():
                warnings.append(
                    f"Skipped unsafe input artifact: {_relative_to_output(output_dir, path)}"
                )
                continue
            candidates.append(
                CleanupCandidate(
                    kind="input_artifact",
                    path=path,
                    relative_path=_relative_to_output(output_dir, path),
                    bytes=_path_size(path),
                    reason="generated precision, coverage, or runtime input artifact",
                )
            )
    return candidates


def _pinned_build_versions(output_dir: Path) -> tuple[set[str], str | None]:
    """Read active Change Safety pins by their on-disk format.

    Cleanup reads the pin files directly rather than going through the change
    services that write them, so pruning cannot pull the planning stack into a
    storage operation. It is not independent of that domain: it imports
    ``CHANGE_CONTRACT_VERSION`` and refuses to delete a build whose pin records
    were written under a contract version it does not recognise. That import is
    the explicit inward dependency described in `docs/architecture.md`, also
    declared in ``[[tool.arcgraph.ci.layer_exemptions]]``.

    Cleanup is intentionally conservative: a malformed or unsafe pin index is
    an uncertainty boundary, so callers retain all build history rather than
    risking a pinned baseline deletion.
    """

    pins_root = output_dir / "pins"
    active_path = pins_root / "active.json"
    legacy_directory = pins_root / "active"
    if not active_path.exists():
        if legacy_directory.exists():
            return set(), "Pinned build index uses an unsupported legacy layout."
        if pins_root.exists():
            if pins_root.is_symlink() or not pins_root.is_dir():
                return set(), "Pinned build store is unsafe."
            if (pins_root / "records").exists():
                return set(), "Pinned build index is missing."
        return set(), None
    if active_path.is_symlink() or not active_path.is_file():
        return set(), "Pinned build index is unsafe."
    try:
        payload = json.loads(active_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(), "Pinned build index cannot be read."
    if not isinstance(payload, dict):
        return set(), "Pinned build index payload is not an object."
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("change_contract_version") != CHANGE_CONTRACT_VERSION
        or not isinstance(payload.get("repo_id"), str)
        or not payload["repo_id"]
    ):
        return set(), "Pinned build index contract is invalid."
    pins_by_index_version = payload.get("pins_by_index_version")
    if not isinstance(pins_by_index_version, dict):
        return set(), "Pinned build index has invalid pin mapping."
    versions: set[str] = set()
    for index_version, pin_ids in pins_by_index_version.items():
        if (
            not isinstance(index_version, str)
            or not index_version
            or "/" in index_version
            or "\\" in index_version
            or index_version in {".", ".."}
        ):
            return set(), "Pinned build index has an unsafe build version."
        if (
            not isinstance(pin_ids, list)
            or not pin_ids
            or not all(isinstance(pin_id, str) and pin_id for pin_id in pin_ids)
        ):
            return set(), "Pinned build index has invalid pin ids."
        if any(
            "/" in pin_id or "\\" in pin_id or "\x00" in pin_id or pin_id in {".", ".."}
            for pin_id in pin_ids
        ):
            return set(), "Pinned build index has an unsafe pin id."
        if pin_ids != sorted(set(pin_ids)):
            return set(), "Pinned build index pin ids are not stable."
        if pin_ids:
            for pin_id in pin_ids:
                record_path = pins_root / "records" / f"{pin_id}.json"
                if record_path.is_symlink() or not record_path.is_file():
                    return set(), "Pinned build record is missing or unsafe."
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return set(), "Pinned build record cannot be read."
                if not isinstance(record, dict):
                    return set(), "Pinned build record is not an object."
                if (
                    record.get("schema_version") != SCHEMA_VERSION
                    or record.get("change_contract_version") != CHANGE_CONTRACT_VERSION
                    or record.get("repo_id") != payload["repo_id"]
                    or record.get("pin_id") != pin_id
                    or record.get("index_version") != index_version
                    or record.get("build_relative_path") != f"builds/{index_version}"
                ):
                    return set(), "Pinned build record does not match active index."
            versions.add(index_version)
    return versions, None


def _delete_candidate(output_dir: Path, candidate: CleanupCandidate) -> None:
    output_root = output_dir.resolve()
    path = candidate.path
    if candidate.kind == "build":
        builds_root = (output_root / "builds").resolve()
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"Refusing to delete non-directory build entry: {path}")
        resolved = path.resolve()
        resolved.relative_to(builds_root)
        shutil.rmtree(path)
        return
    if candidate.kind == "input_artifact":
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Refusing to delete unsafe input artifact: {path}")
        resolved = path.resolve()
        resolved.relative_to(output_root)
        if resolved.parent != output_root:
            raise RuntimeError(f"Refusing to delete nested input artifact: {path}")
        path.unlink()
        return
    raise RuntimeError(f"Unknown cleanup candidate kind: {candidate.kind}")


def _path_size(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_symlink() or not child.is_file():
            continue
        try:
            total += child.stat().st_size
        except OSError:
            continue
    return total


def _relative_to_output(output_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(output_dir).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(output_dir.resolve()).as_posix()
        except (OSError, ValueError):
            return str(path)


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return left == right
