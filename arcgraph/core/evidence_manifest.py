"""Evidence manifest helpers for external ArcGraph input artifacts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET

from arcgraph.core.schemas import SCHEMA_VERSION

EVIDENCE_MANIFEST_RELATIVE_PATH = Path("evidence") / "manifest.json"
EVIDENCE_HEALTH_STATUSES = {
    "missing",
    "available",
    "stale",
    "partial",
    "invalid",
    "out_of_scope",
}
EVIDENCE_KINDS = (
    "precision_scip",
    "precision_pyright",
    "coverage",
    "runtime_trace",
)
EVIDENCE_SIDECAR_SUFFIX = ".arcgraph.json"


def evidence_manifest_path(output_dir: Path) -> Path:
    return output_dir / EVIDENCE_MANIFEST_RELATIVE_PATH


def evidence_sidecar_path(artifact_path: Path) -> Path:
    return artifact_path.with_name(f"{artifact_path.name}{EVIDENCE_SIDECAR_SUFFIX}")


def write_evidence_sidecar(
    *,
    repo_root: Path,
    artifact_path: str | Path,
    kind: str,
    commit_sha: str | None,
    source_roots: list[str],
    tool_name: str,
    tool_version: str | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    path = _artifact_path(repo_root, repo_root / "output" / "arcgraph", artifact_path)
    if not path.exists():
        raise FileNotFoundError(f"Evidence artifact does not exist: {path}")
    sidecar_path = evidence_sidecar_path(path)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "path": _display_path(repo_root, path),
        "commit_sha": commit_sha,
        "source_roots": list(source_roots),
        "tool_name": tool_name,
        "tool_version": tool_version or "unknown",
        "generated_at": _utc_now(),
        "scope": "repository",
    }
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "written",
        "kind": kind,
        "artifact_path": _display_path(repo_root, path),
        "sidecar_path": _display_path(repo_root, sidecar_path),
        "metadata": payload,
    }


def build_evidence_manifest(
    *,
    repo_root: Path,
    output_dir: Path,
    commit_sha: str | None,
    source_roots: list[str],
    precision_metrics: dict[str, Any] | None = None,
    coverage_metrics: dict[str, Any] | None = None,
    runtime_metrics: dict[str, Any] | None = None,
    scip_index_path: str | Path | None = None,
    pyright_export_path: str | Path | None = None,
    coverage_path: str | Path | None = None,
    runtime_trace_path: str | Path | None = None,
    write_live: bool = True,
) -> dict[str, Any]:
    """Build and optionally persist the live manifest used by a new index."""

    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    precision_metrics = precision_metrics or {}
    coverage_metrics = coverage_metrics or {}
    runtime_metrics = runtime_metrics or {}
    source_root_values = list(source_roots)
    artifacts = [
        _artifact_record(
            kind="precision_scip",
            repo_root=repo_root,
            output_dir=output_dir,
            path=_artifact_path(
                repo_root,
                output_dir,
                scip_index_path
                or _dict_value(precision_metrics, "scip_index_path")
                or output_dir / "scip-index.json",
            ),
            metric_status=_dict_value(precision_metrics, "scip_status"),
            commit_sha=commit_sha,
            source_roots=source_root_values,
            tool_name="scip",
            payload_kind="json",
        ),
        _artifact_record(
            kind="precision_pyright",
            repo_root=repo_root,
            output_dir=output_dir,
            path=_artifact_path(
                repo_root,
                output_dir,
                pyright_export_path
                or _dict_value(precision_metrics, "pyright_export_path")
                or output_dir / "pyright-export.json",
            ),
            metric_status=_dict_value(precision_metrics, "pyright_status"),
            commit_sha=commit_sha,
            source_roots=source_root_values,
            tool_name="pyright-langserver",
            payload_kind="json",
        ),
        _artifact_record(
            kind="coverage",
            repo_root=repo_root,
            output_dir=output_dir,
            path=_artifact_path(
                repo_root,
                output_dir,
                coverage_path
                or _dict_value(coverage_metrics, "path")
                or output_dir / "coverage.xml",
            ),
            metric_status=_dict_value(coverage_metrics, "status"),
            commit_sha=commit_sha,
            source_roots=source_root_values,
            tool_name=str(_dict_value(coverage_metrics, "format") or "coverage"),
            payload_kind="coverage",
        ),
        _artifact_record(
            kind="runtime_trace",
            repo_root=repo_root,
            output_dir=output_dir,
            path=_artifact_path(
                repo_root,
                output_dir,
                runtime_trace_path
                or _dict_value(runtime_metrics, "trace_path")
                or output_dir / "runtime-trace.json",
            ),
            metric_status=_dict_value(runtime_metrics, "status"),
            commit_sha=commit_sha,
            source_roots=source_root_values,
            tool_name=str(_dict_value(runtime_metrics, "source") or "runtime_trace"),
            tool_version=_string_or_unknown(
                _dict_value(runtime_metrics, "frontend_version")
            ),
            artifact_commit_sha=_string_or_none(
                _dict_value(runtime_metrics, "commit_sha")
            ),
            payload_kind="json",
        ),
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_version": "1.0",
        "generated_at": _utc_now(),
        "repo_root": str(repo_root),
        "output_dir": str(output_dir),
        "commit_sha": commit_sha,
        "source_roots": source_root_values,
        "manifest_path": _display_path(repo_root, evidence_manifest_path(output_dir)),
        "artifacts": artifacts,
    }
    manifest["summary"] = _summary(artifacts)
    if write_live:
        path = evidence_manifest_path(output_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return manifest


def load_live_evidence_manifest(
    *,
    repo_root: Path,
    output_dir: Path,
    commit_sha: str | None,
    source_roots: list[str],
) -> dict[str, Any]:
    path = evidence_manifest_path(output_dir)
    if not path.exists():
        return build_evidence_manifest(
            repo_root=repo_root,
            output_dir=output_dir,
            commit_sha=commit_sha,
            source_roots=source_roots,
            write_live=False,
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "manifest_version": "1.0",
            "generated_at": None,
            "repo_root": str(repo_root.resolve()),
            "output_dir": str(output_dir.resolve()),
            "commit_sha": commit_sha,
            "source_roots": list(source_roots),
            "manifest_path": _display_path(repo_root, path),
            "artifacts": [],
            "summary": {"status": "invalid", "total": 0, "by_status": {}},
            "warnings": [f"Could not read evidence manifest: {type(exc).__name__}"],
        }
    return refresh_evidence_manifest_health(
        manifest,
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha=commit_sha,
        source_roots=source_roots,
    )


def refresh_evidence_manifest_health(
    manifest: dict[str, Any],
    *,
    repo_root: Path,
    output_dir: Path,
    commit_sha: str | None,
    source_roots: list[str],
) -> dict[str, Any]:
    refreshed = deepcopy(manifest)
    artifacts = [
        _refresh_artifact(
            artifact,
            repo_root=repo_root.resolve(),
            output_dir=output_dir.resolve(),
            commit_sha=commit_sha,
            source_roots=source_roots,
        )
        for artifact in _list_value(refreshed, "artifacts")
        if isinstance(artifact, dict)
    ]
    refreshed["artifacts"] = artifacts
    refreshed["summary"] = _summary(artifacts)
    return refreshed


def evidence_plan(
    *,
    repo_root: Path,
    output_dir: Path,
    commit_sha: str | None,
    source_roots: list[str],
    profile: str = "default",
    project_name: str | None = None,
    python_full_requirements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = load_live_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha=commit_sha,
        source_roots=source_roots,
    )
    actions = [
        _action_for_artifact(
            artifact,
            source_roots=source_roots,
            project_name=project_name or repo_root.name,
            profile=profile,
            python_full_requirements=python_full_requirements,
        )
        for artifact in _list_value(manifest, "artifacts")
        if isinstance(artifact, dict) and _dict_value(artifact, "status") != "available"
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if not actions else "warn",
        "profile": profile,
        "manifest_path": str(evidence_manifest_path(output_dir)),
        "live_manifest_exists": evidence_manifest_path(output_dir).exists(),
        "evidence_health": manifest,
        "actions": actions,
    }


def evidence_status(
    *,
    repo_root: Path,
    output_dir: Path,
    commit_sha: str | None,
    source_roots: list[str],
    snapshot_manifest: dict[str, Any] | None,
    capabilities: dict[str, Any] | None,
) -> dict[str, Any]:
    live_manifest = load_live_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha=commit_sha,
        source_roots=source_roots,
    )
    refreshed_snapshot = (
        refresh_evidence_manifest_health(
            snapshot_manifest,
            repo_root=repo_root,
            output_dir=output_dir,
            commit_sha=commit_sha,
            source_roots=source_roots,
        )
        if isinstance(snapshot_manifest, dict)
        else None
    )
    comparison = _manifest_comparison(live_manifest, refreshed_snapshot)
    live_summary = _dict_value(live_manifest, "summary", {})
    snapshot_summary = (
        _dict_value(refreshed_snapshot, "summary", {})
        if isinstance(refreshed_snapshot, dict)
        else {}
    )
    live_status = _dict_value(live_summary, "status", "missing")
    comparison_status = _dict_value(comparison, "status")
    status = (
        "pass"
        if live_status == "available"
        and comparison_status == "matched"
        and isinstance(refreshed_snapshot, dict)
        else "warn"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "current_commit_sha": commit_sha,
        "source_roots": list(source_roots),
        "manifest_path": str(evidence_manifest_path(output_dir)),
        "live_manifest_exists": evidence_manifest_path(output_dir).exists(),
        "live_manifest": {
            "summary": live_summary,
            "artifacts": _list_value(live_manifest, "artifacts"),
        },
        "snapshot_manifest": {
            "exists": isinstance(refreshed_snapshot, dict),
            "summary": snapshot_summary,
            "artifacts": (
                _list_value(refreshed_snapshot, "artifacts")
                if isinstance(refreshed_snapshot, dict)
                else []
            ),
        },
        "capability_summary": _capability_summary(capabilities or {}),
        "live_vs_snapshot": comparison,
        "warnings": _status_warnings(live_status, refreshed_snapshot, comparison),
    }


def artifact_status(manifest: dict[str, Any] | None, kind: str) -> str | None:
    if not isinstance(manifest, dict):
        return None
    for artifact in _list_value(manifest, "artifacts"):
        if isinstance(artifact, dict) and _dict_value(artifact, "kind") == kind:
            status = _dict_value(artifact, "status")
            return str(status) if status else None
    return None


def precision_health_status(manifest: dict[str, Any] | None) -> str | None:
    statuses = [
        status
        for status in [
            artifact_status(manifest, "precision_scip"),
            artifact_status(manifest, "precision_pyright"),
        ]
        if status
    ]
    if not statuses:
        return None
    if "available" in statuses:
        return "available"
    if any(status != "missing" for status in statuses):
        return "partial"
    return "missing"


def evidence_health_check_status(manifest: dict[str, Any] | None) -> str:
    if not isinstance(manifest, dict):
        return "missing"
    summary = _dict_value(manifest, "summary", {})
    if not isinstance(summary, dict):
        return "missing"
    status = _dict_value(summary, "status")
    return str(status) if status else "missing"


def _artifact_record(
    *,
    kind: str,
    repo_root: Path,
    output_dir: Path,
    path: Path,
    metric_status: Any,
    commit_sha: str | None,
    source_roots: list[str],
    tool_name: str,
    payload_kind: str,
    tool_version: str | None = None,
    artifact_commit_sha: str | None = None,
) -> dict[str, Any]:
    record = {
        "kind": kind,
        "path": _display_path(repo_root, path),
        "exists": path.exists(),
        "hash": None,
        "bytes": 0,
        "status": _metric_status(metric_status),
        "reason": _metric_reason(metric_status),
        "commit_sha": artifact_commit_sha,
        "source_roots": [],
        "tool_name": tool_name,
        "tool_version": tool_version,
        "generated_at": None,
        "scope": "repository",
    }
    if not path.exists():
        record["status"] = "missing"
        record["reason"] = "artifact_missing"
        return record

    record["hash"] = _sha256(path)
    record["bytes"] = path.stat().st_size
    payload, invalid_reason = _read_payload(path, payload_kind)
    if invalid_reason is not None:
        record["status"] = "invalid"
        record["reason"] = invalid_reason
        return record
    _merge_payload_metadata(record, payload)
    sidecar_payload, sidecar_invalid_reason = _read_sidecar_payload(path)
    if sidecar_invalid_reason is not None:
        record["status"] = "invalid"
        record["reason"] = sidecar_invalid_reason
        return record
    _merge_payload_metadata(record, sidecar_payload)
    _apply_scope_status(record, commit_sha=commit_sha, source_roots=source_roots)
    return record


def _refresh_artifact(
    artifact: dict[str, Any],
    *,
    repo_root: Path,
    output_dir: Path,
    commit_sha: str | None,
    source_roots: list[str],
) -> dict[str, Any]:
    refreshed = dict(artifact)
    path = _artifact_path(repo_root, output_dir, _dict_value(refreshed, "path"))
    refreshed["exists"] = path.exists()
    if not path.exists():
        refreshed["status"] = "missing"
        refreshed["reason"] = "artifact_missing"
        refreshed["bytes"] = 0
        return refreshed

    current_hash = _sha256(path)
    refreshed["bytes"] = path.stat().st_size
    previous_hash = _dict_value(refreshed, "hash")
    if previous_hash and previous_hash != current_hash:
        refreshed["status"] = "stale"
        refreshed["reason"] = "artifact_hash_changed"
    else:
        refreshed["hash"] = current_hash
        if _dict_value(refreshed, "status") == "missing":
            refreshed["status"] = "available"
            refreshed["reason"] = None
    sidecar_payload, sidecar_invalid_reason = _read_sidecar_payload(path)
    if sidecar_invalid_reason is not None:
        refreshed["status"] = "invalid"
        refreshed["reason"] = sidecar_invalid_reason
        return refreshed
    _merge_payload_metadata(refreshed, sidecar_payload)
    _apply_scope_status(refreshed, commit_sha=commit_sha, source_roots=source_roots)
    return refreshed


def _apply_scope_status(
    record: dict[str, Any],
    *,
    commit_sha: str | None,
    source_roots: list[str],
) -> None:
    artifact_commit = _dict_value(record, "commit_sha")
    if artifact_commit and commit_sha and artifact_commit != commit_sha:
        record["status"] = "out_of_scope"
        record["reason"] = "commit_mismatch"
        return
    artifact_roots = _dict_value(record, "source_roots")
    if artifact_roots and sorted(artifact_roots) != sorted(source_roots):
        record["status"] = "out_of_scope"
        record["reason"] = "source_roots_mismatch"


def _merge_payload_metadata(record: dict[str, Any], payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    if record["kind"] == "runtime_trace":
        trace_run = _dict_value(payload, "trace_run", {})
        if isinstance(trace_run, dict):
            record["commit_sha"] = _string_or_none(_dict_value(trace_run, "commit_sha"))
            record["tool_name"] = str(
                _dict_value(trace_run, "source") or record["tool_name"]
            )
            record["tool_version"] = _string_or_unknown(
                _dict_value(trace_run, "frontend_version")
            )
            record["generated_at"] = _string_or_none(
                _dict_value(trace_run, "started_at")
            )
        return
    metadata = _dict_value(payload, "arcgraph_metadata") or _dict_value(
        payload, "arcgraph"
    )
    source = metadata if isinstance(metadata, dict) else payload
    record["commit_sha"] = (
        _string_or_none(_dict_value(source, "commit_sha"))
        or _string_or_none(_dict_value(payload, "commit_sha"))
        or _dict_value(record, "commit_sha")
    )
    roots = _dict_value(source, "source_roots") or _dict_value(payload, "source_roots")
    if isinstance(roots, list):
        record["source_roots"] = [str(root) for root in roots if root]
    record["generated_at"] = _string_or_none(
        _dict_value(source, "generated_at")
        or _dict_value(source, "created_at")
        or _dict_value(payload, "generated_at")
        or _dict_value(payload, "created_at")
    )
    record["tool_version"] = (
        _string_or_none(_dict_value(source, "tool_version"))
        or _string_or_none(_dict_value(payload, "tool_version"))
        or _dict_value(record, "tool_version")
        or "unknown"
    )
    tool_name = (
        _dict_value(source, "tool_name")
        or _dict_value(source, "generator")
        or _dict_value(payload, "generator")
    )
    if tool_name:
        record["tool_name"] = str(tool_name)


def _summary(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for artifact in artifacts:
        status = str(_dict_value(artifact, "status") or "missing")
        by_status[status] = _dict_value(by_status, status, 0) + 1
    if not artifacts:
        status = "missing"
    elif all(_dict_value(artifact, "status") == "available" for artifact in artifacts):
        status = "available"
    elif all(_dict_value(artifact, "status") == "missing" for artifact in artifacts):
        status = "missing"
    else:
        status = "partial"
    return {"status": status, "total": len(artifacts), "by_status": by_status}


def _action_for_artifact(
    artifact: dict[str, Any],
    *,
    source_roots: list[str],
    project_name: str,
    profile: str,
    python_full_requirements: dict[str, Any] | None,
) -> dict[str, Any]:
    kind = str(_dict_value(artifact, "kind"))
    commands = _suggested_commands(kind, source_roots, project_name)
    follow_up_inputs = {
        "precision_scip": ["--scip-index", "output/arcgraph/scip-index.json"],
        "precision_pyright": [
            "--pyright-export",
            "output/arcgraph/pyright-export.json",
        ],
        "coverage": ["--coverage", "output/arcgraph/coverage.xml"],
        "runtime_trace": ["--runtime-trace", "output/arcgraph/runtime-trace.json"],
    }
    gains = {
        "precision_scip": "Can add confirmed reference/call evidence and reduce AST fallback reliance.",
        "precision_pyright": "Can add type/reference evidence and improve precision health.",
        "coverage": "Can upgrade test recommendations from heuristic to heuristic-and-coverage when coverage edges map to targets.",
        "runtime_trace": "Can add runtime-only facts for dynamic calls, dispatch, queues, and resource flow.",
    }
    return {
        "kind": kind,
        "status": _dict_value(artifact, "status", "missing"),
        "reason": _dict_value(artifact, "reason"),
        "artifact_path": _dict_value(artifact, "path"),
        "path": _dict_value(artifact, "path"),
        "suggested_commands": commands,
        "suggested_command": " && ".join(commands) if commands else None,
        "follow_up_build_inputs": _dict_value(follow_up_inputs, kind, []),
        "expected_confidence_gain": _dict_value(gains, kind),
        "blocks_python_full": _blocks_python_full(
            kind, profile, python_full_requirements
        ),
    }


def _suggested_commands(
    kind: str, source_roots: list[str], project_name: str
) -> list[str]:
    if kind == "precision_scip":
        return [
            "arcgraph precision scip-python "
            "--output output/arcgraph/scip-index.json "
            "--index-file output/arcgraph/index.scip "
            f"--project-name {_shell_arg(project_name)}"
        ]
    if kind == "precision_pyright":
        return [
            "arcgraph precision pyright "
            "--output output/arcgraph/pyright-export.json --python-version 3.11"
        ]
    if kind == "coverage":
        cov_args = " ".join(
            f"--cov={_shell_arg(root)}" for root in _coverage_roots(source_roots)
        )
        return [
            "python -m pytest "
            f"{cov_args} --cov-report=xml:output/arcgraph/coverage.xml",
            "arcgraph evidence stamp "
            "--kind coverage --path output/arcgraph/coverage.xml "
            "--tool-name coverage",
        ]
    if kind == "runtime_trace":
        return [
            "arcgraph trace run --output output/arcgraph/runtime-trace.json -- TESTS..."
        ]
    return []


def _coverage_roots(source_roots: list[str]) -> list[str]:
    roots = [root for root in source_roots if root]
    return roots or ["."]


def _blocks_python_full(
    kind: str, profile: str, requirements: dict[str, Any] | None
) -> bool:
    if profile != "python_full" or not isinstance(requirements, dict):
        return False
    requirement_key = {
        "precision_scip": "precision",
        "precision_pyright": "precision",
        "coverage": "coverage",
        "runtime_trace": "runtime_trace",
    }.get(kind)
    if requirement_key is None:
        return False
    requirement = _dict_value(requirements, requirement_key, {})
    return bool(
        isinstance(requirement, dict)
        and _dict_value(requirement, "blocks_python_full") is True
    )


def _shell_arg(value: str) -> str:
    if value and not any(char.isspace() for char in value):
        return value
    escaped = value.replace('"', '\\"')
    return f'"{escaped}"'


def _manifest_comparison(
    live_manifest: dict[str, Any], snapshot_manifest: dict[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(snapshot_manifest, dict):
        return {
            "status": "snapshot_missing",
            "summary_mismatch": True,
            "artifact_mismatches": [],
        }
    live_summary = _dict_value(live_manifest, "summary", {})
    snapshot_summary = _dict_value(snapshot_manifest, "summary", {})
    summary_mismatch = _dict_value(live_summary, "status") != _dict_value(
        snapshot_summary, "status"
    )
    mismatches = []
    live_by_kind = _artifacts_by_kind(live_manifest)
    snapshot_by_kind = _artifacts_by_kind(snapshot_manifest)
    for kind in EVIDENCE_KINDS:
        live_artifact = _dict_value(live_by_kind, kind, {})
        snapshot_artifact = _dict_value(snapshot_by_kind, kind, {})
        changed_fields = [
            field
            for field in ("status", "reason", "hash", "path")
            if _dict_value(live_artifact, field)
            != _dict_value(snapshot_artifact, field)
        ]
        if changed_fields:
            mismatches.append(
                {
                    "kind": kind,
                    "changed_fields": changed_fields,
                    "live_status": _dict_value(live_artifact, "status"),
                    "snapshot_status": _dict_value(snapshot_artifact, "status"),
                    "live_reason": _dict_value(live_artifact, "reason"),
                    "snapshot_reason": _dict_value(snapshot_artifact, "reason"),
                }
            )
    status = "mismatch" if summary_mismatch or mismatches else "matched"
    return {
        "status": status,
        "summary_mismatch": summary_mismatch,
        "artifact_mismatches": mismatches,
    }


def _artifacts_by_kind(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for artifact in _list_value(manifest, "artifacts"):
        if isinstance(artifact, dict):
            kind = _dict_value(artifact, "kind")
            if isinstance(kind, str):
                artifacts[kind] = artifact
    return artifacts


def _capability_summary(capabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        "precision": _dict_value(capabilities, "precision", "ast_fallback_only"),
        "precise_references": _dict_value(
            capabilities, "precise_references", "ast-fallback"
        ),
        "coverage": _dict_value(capabilities, "coverage", "unavailable"),
        "runtime_trace": _dict_value(capabilities, "runtime_trace", "unavailable"),
        "test_recommendations": _dict_value(
            capabilities, "test_recommendations", "heuristic"
        ),
    }


def _status_warnings(
    live_status: Any,
    snapshot_manifest: dict[str, Any] | None,
    comparison: dict[str, Any],
) -> list[str]:
    warnings = []
    if live_status != "available":
        warnings.append("Live evidence is not fully available.")
    if not isinstance(snapshot_manifest, dict):
        warnings.append("Current index has no evidence manifest snapshot.")
    if _dict_value(comparison, "status") not in {"matched", "snapshot_missing"}:
        warnings.append("Live evidence and index snapshot differ.")
    return warnings


def _artifact_path(repo_root: Path, output_dir: Path, value: Any) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value:
        path = Path(value)
    else:
        path = output_dir / "missing"
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def _display_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _metric_status(value: Any) -> str:
    status = str(value or "missing")
    if status == "available":
        return "available"
    if status in {"partial", "stale"}:
        return "partial"
    return "missing"


def _metric_reason(value: Any) -> str | None:
    status = str(value or "")
    if status in {"", "missing", "unavailable"}:
        return "not_configured"
    if status == "stale":
        return "metric_stale"
    if status == "partial":
        return "metric_partial"
    return None


def _read_payload(path: Path, payload_kind: str) -> tuple[Any | None, str | None]:
    try:
        if payload_kind == "coverage" and path.suffix.lower() == ".xml":
            ET.parse(path)
            return None, None
        if payload_kind in {"json", "coverage"}:
            return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError, ET.ParseError) as exc:
        return None, f"parse_error:{type(exc).__name__}"
    return None, None


def _read_sidecar_payload(path: Path) -> tuple[Any | None, str | None]:
    sidecar_path = evidence_sidecar_path(path)
    if not sidecar_path.exists():
        return None, None
    try:
        return json.loads(sidecar_path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"sidecar_parse_error:{type(exc).__name__}"


def _dict_value(mapping: dict[str, Any], key: str, default: Any = None) -> Any:
    try:
        return mapping[key]
    except KeyError:
        return default


def _list_value(mapping: dict[str, Any], key: str) -> list[Any]:
    value = _dict_value(mapping, key, [])
    return value if isinstance(value, list) else []


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _string_or_none(value: Any) -> str | None:
    return str(value) if value else None


def _string_or_unknown(value: Any) -> str:
    return str(value) if value else "unknown"
