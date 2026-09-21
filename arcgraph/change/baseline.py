"""Fail-closed baseline creation and validation."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from arcgraph.change.contracts import (
    BaselineReference,
    BuildIdentity,
    SOURCE_PROVIDER_VERSION,
    WorkingTreeIdentity,
    canonical_digest,
)
from arcgraph.change.errors import (
    BaselineGitObjectMissing,
    BaselineIntegrityMismatch,
    BaselineSourceIdentityMismatch,
    BaselineSourceUnavailable,
)
from arcgraph.change.identities import (
    capture_build_identity,
    capture_working_tree_identity,
    git_head_sha,
    git_tree_sha,
    verify_build_identity,
)


def create_baseline_reference(
    repo_root: Path,
    output_dir: Path,
    *,
    repo_id: str,
    pin_id: str,
    baseline_id: str | None = None,
) -> BaselineReference:
    """Create an executable baseline only from a clean, readable Git commit."""

    working_tree = capture_working_tree_identity(repo_root, repo_id=repo_id)
    if not working_tree.clean:
        raise BaselineSourceUnavailable(
            "an executable baseline requires a clean working tree"
        )
    if not working_tree.head_sha:
        raise BaselineSourceUnavailable("an executable baseline requires a Git HEAD")
    build_identity = capture_build_identity(repo_root, output_dir, repo_id=repo_id)
    _validate_build_matches_clean_worktree(repo_root, build_identity, working_tree)
    source_identity = baseline_source_identity(
        repo_root,
        repo_id=repo_id,
        commit_sha=working_tree.head_sha,
        git_tree_sha=working_tree_tree_or_raise(repo_root, working_tree.head_sha),
    )
    return BaselineReference(
        repo_id=repo_id,
        baseline_id=baseline_id or f"baseline-{uuid.uuid4().hex}",
        build_identity=build_identity,
        working_tree_identity=working_tree,
        pin_id=pin_id,
        baseline_source_identity=source_identity,
    )


def validate_baseline_reference(
    repo_root: Path,
    output_dir: Path,
    baseline: BaselineReference,
) -> None:
    """Recheck immutable graph bytes and the baseline Git source trust root."""

    verify_build_identity(output_dir, baseline.build_identity)
    working_tree = baseline.working_tree_identity
    if not working_tree.clean or not working_tree.head_sha:
        raise BaselineSourceUnavailable(
            "baseline was not captured from a clean Git commit"
        )
    tree = working_tree_tree_or_raise(repo_root, working_tree.head_sha)
    if baseline.build_identity.commit_sha != working_tree.head_sha:
        raise BaselineSourceIdentityMismatch(
            "baseline build commit does not match baseline Git HEAD"
        )
    if baseline.build_identity.git_tree_sha != tree:
        raise BaselineSourceIdentityMismatch(
            "baseline build tree does not match baseline Git tree"
        )
    expected = baseline_source_identity(
        repo_root,
        repo_id=baseline.repo_id,
        commit_sha=working_tree.head_sha,
        git_tree_sha=tree,
    )
    if canonical_digest(expected) != canonical_digest(
        baseline.baseline_source_identity
    ):
        raise BaselineSourceIdentityMismatch(
            "baseline source identity changed or is malformed"
        )


def baseline_source_identity(
    repo_root: Path,
    *,
    repo_id: str,
    commit_sha: str,
    git_tree_sha: str,
) -> dict[str, Any]:
    """Return the versioned source-provider identity for an immutable Git tree."""

    if not git_head_sha(repo_root):
        raise BaselineSourceUnavailable("Git repository state is unavailable")
    if not git_tree_sha:
        raise BaselineGitObjectMissing("baseline Git tree cannot be resolved")
    return {
        "repo_id": repo_id,
        "commit_sha": commit_sha,
        "git_tree_sha": git_tree_sha,
        "source_provider_version": SOURCE_PROVIDER_VERSION,
    }


def working_tree_tree_or_raise(repo_root: Path, commit_sha: str) -> str:
    tree = git_tree_sha(repo_root, commit_sha)
    if not tree:
        raise BaselineGitObjectMissing(
            f"baseline Git object is missing or unreadable: {commit_sha}"
        )
    return tree


def _validate_build_matches_clean_worktree(
    repo_root: Path,
    build_identity: BuildIdentity,
    working_tree: WorkingTreeIdentity,
) -> None:
    if build_identity.commit_sha != working_tree.head_sha:
        raise BaselineIntegrityMismatch(
            "current graph build does not match clean Git HEAD"
        )
    tree = working_tree_tree_or_raise(repo_root, working_tree.head_sha or "")
    if build_identity.git_tree_sha != tree:
        raise BaselineIntegrityMismatch(
            "current graph build tree does not match clean Git tree"
        )
