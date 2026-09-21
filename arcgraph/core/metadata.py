"""Index metadata helpers."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path


def current_commit(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def make_index_version(commit_sha: str | None = None) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if commit_sha:
        return f"{timestamp}-{commit_sha[:8]}"
    return f"{timestamp}-nogit"
