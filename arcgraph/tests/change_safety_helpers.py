"""Shared deterministic fixtures for Surgical Change Safety tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

from arcgraph.change.contracts import (
    BaselineReference,
    BuildIdentity,
    ChangeIntent,
    ChangePlanRevision,
    ChangeTarget,
    CodeIdentity,
    SOURCE_PROVIDER_VERSION,
    WorkingTreeIdentity,
)
from arcgraph.change.identities import capture_build_identity, capture_code_identity
from arcgraph.change.planner import ChangePlanner
from arcgraph.change.service import ChangeSafetyService
from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.core.schemas import FileRecord, IndexMetadata, Node


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text(
        "def f():\n    return 1\n",
        encoding="utf-8",
    )
    (repo / "src" / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo.parent, "init", repo.name)
    git(repo, "config", "user.email", "tests@example.invalid")
    git(repo, "config", "user.name", "ArcGraph Tests")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    return repo


def write_build(
    repo: Path,
    output: Path,
    *,
    index_version: str,
    commit_sha: str,
    nodes: list[Node] | None = None,
) -> Path:
    files: list[FileRecord] = []
    for path in sorted((repo / "src").glob("*.py")):
        content = path.read_bytes()
        files.append(
            FileRecord(
                path=path.relative_to(repo).as_posix(),
                abs_path=str(path),
                source_root="src",
                module=path.stem,
                file_hash=hashlib.sha256(content).hexdigest(),
                line_count=max(1, len(content.decode("utf-8").splitlines())),
                file_size=len(content),
            )
        )
    GraphStoreWriter(output).write(
        IndexMetadata(
            index_version=index_version,
            repo_root=str(repo),
            source_roots=["src"],
            commit_sha=commit_sha,
        ),
        files,
        nodes
        or [
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=1,
                end_line=2,
            ),
            Node(
                id="mod:other",
                kind="module",
                name="other",
                path="src/other.py",
                start_line=1,
                end_line=1,
            ),
        ],
        [],
        [],
    )
    return output / "builds" / index_version


def baseline_reference(
    repo: Path,
    output: Path,
    *,
    index_version: str,
    pin_id: str = "pin-baseline",
) -> BaselineReference:
    build = capture_build_identity(
        repo,
        output,
        repo_id="repo",
        index_version=index_version,
    )
    commit_sha = build.commit_sha or ""
    return BaselineReference(
        repo_id="repo",
        baseline_id="baseline",
        build_identity=build,
        working_tree_identity=WorkingTreeIdentity(
            repo_id="repo",
            head_sha=commit_sha,
            clean=True,
            status_digest="clean",
        ),
        pin_id=pin_id,
        baseline_source_identity={
            "repo_id": "repo",
            "commit_sha": commit_sha,
            "git_tree_sha": build.git_tree_sha,
            "source_provider_version": SOURCE_PROVIDER_VERSION,
        },
    )


def simple_build_identity(index_version: str = "index") -> BuildIdentity:
    return BuildIdentity(
        repo_id="repo",
        index_version=index_version,
        build_relative_path=f"builds/{index_version}",
        file_manifest_digest="manifest",
        summary_digest="summary",
        index_sqlite_sha256="sqlite",
        index_sqlite_size=1,
    )


def activated_approved_plan(
    tmp_path: Path,
) -> tuple[Path, Path, ChangeSafetyService, ChangePlanRevision]:
    """Create a real clean baseline, active pin, and approved one-scope plan."""

    repo = init_repo(tmp_path)
    output = tmp_path / "output"
    commit_sha = git(repo, "rev-parse", "HEAD")
    write_build(repo, output, index_version="index", commit_sha=commit_sha)
    baseline = baseline_reference(repo, output, index_version="index")
    nodes = GraphStoreReader.from_current(output).read_nodes()
    plan = ChangePlanner(repo, repo_id="repo").plan(
        ChangeIntent(
            repo_id="repo",
            intent_id="intent",
            task="explicit local function target",
            targets=[ChangeTarget(repo_id="repo", kind="symbol", value="fn:app.f")],
        ),
        baseline,
        nodes,
        plan_id="plan",
    )
    service = ChangeSafetyService(repo, output, repo_id="repo")
    service.activate_revision(plan)
    service.approve(
        plan.plan_id,
        plan.revision,
        plan.plan_content_digest,
        actor="test",
        reason="approved fixture",
    )
    return repo, output, service, plan


def current_code_identity(repo: Path, output: Path) -> CodeIdentity:
    build = capture_build_identity(repo, output, repo_id="repo")
    paths = [
        record.path for record in GraphStoreReader.from_current(output).iter_files()
    ]
    return capture_code_identity(
        repo,
        repo_id="repo",
        build_identity=build,
        source_paths=paths,
    )
