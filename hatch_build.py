"""Hatch build hook that embeds reproducible source provenance in artifacts."""

from __future__ import annotations

import atexit
import hashlib
import json
import subprocess
from functools import partial
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Bind a wheel/sdist to the exact source state used to build it."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del version
        root = Path(self.root)
        provenance_path = root / "arcgraph" / "_build_provenance.json"
        self._provenance_path = provenance_path
        self._original = (
            provenance_path.read_bytes() if provenance_path.exists() else None
        )
        # finalize only runs after a successful build, so the restore must also
        # be registered for interpreter exit; otherwise a failed or interrupted
        # build leaves a generated file that a later build would inherit.
        self._restore = partial(_restore_provenance, provenance_path, self._original)
        atexit.register(self._restore)

        existing = _read_existing(provenance_path)
        git_source = _git_provenance(root)
        if git_source is not None:
            source = git_source
        elif (root / ".git").exists():
            # A git checkout whose provenance query failed must not inherit
            # provenance from a file on disk: that file can only be a leftover
            # of an earlier build, and embedding it would bind the artifact to
            # a commit that did not produce it. Empty source degrades honestly
            # and fails the readiness smoke's commit check.
            source = {}
        else:
            # No repository at all: this is the sdist -> wheel chain, where the
            # packaged file is the only provenance that can exist.
            source = existing.get("source", {})
        payload = {
            "schema_version": "1.0.0",
            "build_version": str(self.metadata.version),
            "source": source,
        }
        provenance_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        build_data.setdefault("artifacts", []).append("arcgraph/_build_provenance.json")

    def finalize(
        self,
        version: str,
        build_data: dict[str, Any],
        artifact_path: str,
    ) -> None:
        del version, build_data, artifact_path
        self._restore()
        atexit.unregister(self._restore)


def _restore_provenance(path: Path, original: bytes | None) -> None:
    """Return the generated file to its pre-build state. Idempotent."""

    try:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
    except OSError:
        pass


def _read_existing(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _git_provenance(root: Path) -> dict[str, Any] | None:
    try:
        commit = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
        tree = _git(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
        status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    except (OSError, RuntimeError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    return {
        "commit_sha": commit,
        "tree_sha": tree,
        "working_tree_clean": not status,
        "working_tree_status_sha256": hashlib.sha256(status).hexdigest(),
    }


def _git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("git provenance query failed")
    return completed.stdout
