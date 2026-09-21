"""On-demand precise reference backends."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

TYPESCRIPT_REFERENCE_EXTENSIONS = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mts",
    ".cts",
    ".mjs",
    ".cjs",
}


def typescript_language_service_references(
    *,
    repo_root: Path,
    files: Iterable[str],
    target: dict[str, Any],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    target_path = _relative_path_inside_root(repo_root, target.get("path"))
    if target_path is None:
        return {
            "status": "unavailable",
            "backend": "typescript_language_service",
            "reason": (
                "Target path is missing or resolves outside the repository root."
            ),
        }
    candidate_paths: set[str] = set()
    out_of_root_files = 0
    for value in files:
        if Path(str(value)).suffix.lower() not in TYPESCRIPT_REFERENCE_EXTENSIONS:
            continue
        relative = _relative_path_inside_root(repo_root, value)
        if relative is None:
            out_of_root_files += 1
        else:
            candidate_paths.add(relative)
    indexed_files = sorted(candidate_paths)
    script = (
        Path(__file__).resolve().parents[1] / "pipeline" / "typescript_references.mjs"
    )
    payload = {
        "repo_root": str(repo_root),
        "files": indexed_files,
        "target": {
            "path": target_path,
            "start_line": target.get("start_line"),
            "name": target.get("name"),
        },
    }
    try:
        completed = subprocess.run(
            ["node", str(script)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            timeout=max(1.0, min(timeout_seconds, 120.0)),
            cwd=repo_root,
        )
    except FileNotFoundError:
        return {
            "status": "unavailable",
            "backend": "typescript_language_service",
            "reason": "Node.js is unavailable.",
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "unavailable",
            "backend": "typescript_language_service",
            "reason": "TypeScript Language Service reference lookup timed out.",
        }
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result = {
            "status": "unavailable",
            "reason": "TypeScript reference backend returned invalid JSON.",
        }
    if not isinstance(result, dict):
        result = {"status": "unavailable", "reason": "Invalid backend payload."}
    result.setdefault("backend", "typescript_language_service")
    if completed.returncode != 0 and result.get("status") == "available":
        result = {
            "status": "unavailable",
            "backend": "typescript_language_service",
            "reason": completed.stderr.strip()
            or f"Node exited {completed.returncode}.",
        }
    if out_of_root_files:
        summary = result.setdefault("summary", {})
        if isinstance(summary, dict):
            summary["out_of_root_files_excluded"] = out_of_root_files
    return result


def _relative_path_inside_root(repo_root: Path, value: Any) -> str | None:
    """Return the repo-relative path, or None when it escapes the root.

    Any indexed path may resolve outside the repository root (symlinked
    source dirs, pnpm layouts); escaping paths are excluded rather than
    raised so a single symlink cannot fail the whole lookup.
    """
    if not isinstance(value, str) or not value:
        return None
    candidate = (repo_root / value).resolve()
    try:
        relative = candidate.relative_to(repo_root)
    except ValueError:
        return None
    return relative.as_posix()
