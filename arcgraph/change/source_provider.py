"""Read-only baseline and current source providers for deterministic diffs."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess
from typing import Any

from arcgraph.change.contracts import (
    SOURCE_NORMALIZATION_PROJECTION_VERSION,
    SOURCE_PROVIDER_VERSION,
    BaselineReference,
    canonical_digest,
)
from arcgraph.change.errors import (
    BaselineGitObjectMissing,
    BaselineSourceIdentityMismatch,
    BaselineSourceUnavailable,
)
from arcgraph.change.paths import NormalizedRepositoryPath, normalize_repository_path

_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True, slots=True)
class SourceNormalization:
    raw_source_digest: str
    normalized_implementation_digest: str | None
    status: str
    projection_version: str = SOURCE_NORMALIZATION_PROJECTION_VERSION


@dataclass(frozen=True, slots=True)
class SourceDocument:
    path: NormalizedRepositoryPath
    content: bytes
    normalization: SourceNormalization
    source_identity: dict[str, Any]


class BaselineSourceProvider:
    """Read exact old-source blobs from the baseline Git commit only."""

    def __init__(self, repo_root: Path, baseline: BaselineReference) -> None:
        self.repo_root = repo_root.resolve()
        self.baseline = baseline
        commit_sha = baseline.working_tree_identity.head_sha
        if not baseline.working_tree_identity.clean or not commit_sha:
            raise BaselineSourceUnavailable(
                "baseline source provider requires a clean baseline Git commit"
            )
        if not _COMMIT.fullmatch(commit_sha):
            raise BaselineSourceIdentityMismatch(
                "baseline commit SHA has invalid syntax"
            )
        if baseline.build_identity.commit_sha != commit_sha:
            raise BaselineSourceIdentityMismatch(
                "baseline build commit does not match baseline source commit"
            )
        self.commit_sha = commit_sha
        self.git_tree_sha = _git_tree_or_raise(self.repo_root, commit_sha)
        if self.git_tree_sha != baseline.build_identity.git_tree_sha:
            raise BaselineSourceIdentityMismatch(
                "baseline source tree does not match baseline build tree"
            )
        expected = {
            "repo_id": baseline.repo_id,
            "commit_sha": commit_sha,
            "git_tree_sha": self.git_tree_sha,
            "source_provider_version": SOURCE_PROVIDER_VERSION,
        }
        if canonical_digest(expected) != canonical_digest(
            baseline.baseline_source_identity
        ):
            raise BaselineSourceIdentityMismatch(
                "baseline source provider identity differs"
            )

    def read(self, path: str | Path) -> SourceDocument:
        normalized = normalize_repository_path(path, self.repo_root)
        spec = f"{self.commit_sha}:{normalized.display_path}"
        content = _git_bytes(self.repo_root, ["show", "--no-ext-diff", spec])
        return SourceDocument(
            path=normalized,
            content=content,
            normalization=normalize_implementation_source(
                normalized.display_path, content
            ),
            source_identity={
                "repo_id": self.baseline.repo_id,
                "mode": "baseline_git_commit",
                "commit_sha": self.commit_sha,
                "git_tree_sha": self.git_tree_sha,
                "source_provider_version": SOURCE_PROVIDER_VERSION,
            },
        )


class CurrentSourceProvider:
    """Read either a fixed current commit or the explicit current working tree."""

    def __init__(
        self,
        repo_root: Path,
        *,
        repo_id: str,
        commit_sha: str | None = None,
        git_tree_sha: str | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.repo_id = repo_id
        self.commit_sha = commit_sha
        self.git_tree_sha = git_tree_sha
        if commit_sha is not None and not _COMMIT.fullmatch(commit_sha):
            raise BaselineSourceIdentityMismatch(
                "current commit SHA has invalid syntax"
            )
        if commit_sha is not None and git_tree_sha is None:
            self.git_tree_sha = _git_tree_or_raise(self.repo_root, commit_sha)

    def read(self, path: str | Path) -> SourceDocument:
        normalized = normalize_repository_path(path, self.repo_root)
        if self.commit_sha:
            content = _git_bytes(
                self.repo_root,
                [
                    "show",
                    "--no-ext-diff",
                    f"{self.commit_sha}:{normalized.display_path}",
                ],
            )
            mode = "current_git_commit"
        else:
            disk_path = self.repo_root / Path(*normalized.display_path.split("/"))
            if disk_path.is_symlink() or not disk_path.is_file():
                raise BaselineSourceUnavailable(
                    f"current source path cannot be read: {normalized.display_path}"
                )
            content = disk_path.read_bytes()
            mode = "current_working_tree"
        return SourceDocument(
            path=normalized,
            content=content,
            normalization=normalize_implementation_source(
                normalized.display_path, content
            ),
            source_identity={
                "repo_id": self.repo_id,
                "mode": mode,
                "commit_sha": self.commit_sha,
                "git_tree_sha": self.git_tree_sha,
                "source_provider_version": SOURCE_PROVIDER_VERSION,
            },
        )


def normalize_implementation_source(path: str, content: bytes) -> SourceNormalization:
    """Produce a conservative, non-LLM implementation projection.

    Python uses the standard AST with attributes omitted.  This intentionally
    retains literals, operators, control-flow nodes, calls, returns, and
    expression structure while removing comments, whitespace, and line-ending
    noise.  Unsupported languages and invalid syntax are ``unknown`` rather
    than falsely classified as a no-op.
    """

    return _normalize_python_projection(path, content, content)


def normalize_implementation_region(
    path: str,
    content: bytes,
    *,
    start_line: int,
    end_line: int,
) -> SourceNormalization:
    """Project one graph-mapped source region, rather than its whole file.

    A ChangedRegion is authorized at Symbol granularity.  Its source evidence
    must therefore be derived from the mapped Symbol's exact raw line range and
    from the smallest enclosing Python AST node.  Parsing the complete document
    preserves context while the selected AST node keeps unrelated symbols out
    of the comparison projection.
    """

    region = _source_region_bytes(content, start_line, end_line)
    raw_content = region if region is not None else content
    return _normalize_python_projection(
        path,
        content,
        raw_content,
        start_line=start_line,
        end_line=end_line,
    )


def _normalize_python_projection(
    path: str,
    content: bytes,
    raw_content: bytes,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
) -> SourceNormalization:
    raw_digest = hashlib.sha256(raw_content).hexdigest()
    if Path(path).suffix.lower() not in {".py", ".pyi"}:
        return SourceNormalization(raw_digest, None, "unknown")
    try:
        source = content.decode("utf-8")
        tree = ast.parse(source, filename=path)
    except (SyntaxError, UnicodeDecodeError):
        return SourceNormalization(raw_digest, None, "unknown")
    if start_line is None and end_line is None:
        projection_node: ast.AST | None = tree
    elif start_line is not None and end_line is not None:
        projection_node = _smallest_ast_region(
            tree,
            start_line,
            end_line,
            source_line_count=max(1, len(content.splitlines())),
        )
    else:
        projection_node = None
    if projection_node is None:
        return SourceNormalization(raw_digest, None, "unknown")
    projection = ast.dump(
        projection_node,
        annotate_fields=True,
        include_attributes=False,
    )
    return SourceNormalization(
        raw_digest,
        hashlib.sha256(projection.encode("utf-8")).hexdigest(),
        "normalized",
    )


def _source_region_bytes(
    content: bytes,
    start_line: int,
    end_line: int,
) -> bytes | None:
    lines = content.splitlines(keepends=True)
    if start_line < 1 or end_line < start_line or end_line > len(lines):
        return None
    return b"".join(lines[start_line - 1 : end_line])


def _smallest_ast_region(
    tree: ast.Module,
    start_line: int,
    end_line: int,
    *,
    source_line_count: int,
) -> ast.AST | None:
    if start_line < 1 or end_line < start_line:
        return None
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(getattr(node, "lineno", None), int)
        and isinstance(getattr(node, "end_lineno", None), int)
        and node.lineno <= start_line
        and node.end_lineno >= end_line
    ]
    if candidates:
        return min(
            candidates,
            key=lambda node: (
                int(getattr(node, "end_lineno")) - int(getattr(node, "lineno")),
                type(node).__name__,
            ),
        )
    if start_line == 1 and end_line >= source_line_count:
        return tree
    return None


def _git_tree_or_raise(repo_root: Path, commit_sha: str) -> str:
    value = _git_bytes(repo_root, ["rev-parse", f"{commit_sha}^{{tree}}"])
    tree = value.decode("ascii", errors="strict").strip()
    if not _COMMIT.fullmatch(tree):
        raise BaselineGitObjectMissing("Git did not return a valid tree object id")
    return tree


def _git_bytes(repo_root: Path, args: list[str]) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        lowered = detail.lower()
        if (
            "bad object" in lowered
            or "path '" in lowered
            or "unknown revision" in lowered
            or "ambiguous argument" in lowered
            or "invalid object name" in lowered
        ):
            raise BaselineGitObjectMissing(detail or "Git object is unavailable")
        raise BaselineSourceUnavailable(detail or "Git source provider is unavailable")
    return result.stdout
