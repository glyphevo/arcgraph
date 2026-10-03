from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from arcgraph.change.contracts import (
    BaselineReference,
    BuildIdentity,
    SOURCE_PROVIDER_VERSION,
    WorkingTreeIdentity,
)
from arcgraph.change.changed_regions import ChangedRegionMapper
from arcgraph.change.errors import (
    BaselineGitObjectMissing,
    BaselineSourceIdentityMismatch,
    BaselineSourceUnavailable,
)
from arcgraph.change.source_provider import (
    BaselineSourceProvider,
    CurrentSourceProvider,
    normalize_implementation_region,
)
from arcgraph.core.schemas import Node


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _baseline(repo: Path) -> BaselineReference:
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    return BaselineReference(
        repo_id="repo",
        baseline_id="baseline",
        build_identity=BuildIdentity(
            repo_id="repo",
            index_version="index",
            build_relative_path="builds/index",
            commit_sha=head,
            git_tree_sha=tree,
            file_manifest_digest="files",
            summary_digest="summary",
            index_sqlite_sha256="sqlite",
            index_sqlite_size=1,
        ),
        working_tree_identity=WorkingTreeIdentity(
            repo_id="repo",
            head_sha=head,
            clean=True,
            status_digest="clean",
        ),
        pin_id="pin",
        baseline_source_identity={
            "repo_id": "repo",
            "commit_sha": head,
            "git_tree_sha": tree,
            "source_provider_version": SOURCE_PROVIDER_VERSION,
        },
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text(
        "def f():\n    return 1\n",
        encoding="utf-8",
    )
    _git(repo.parent, "init", repo.name)
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "ArcGraph Tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return repo


def test_comment_only_change_has_same_normalized_implementation_digest(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    baseline = _baseline(repo)
    baseline_source = BaselineSourceProvider(repo, baseline)
    current_source = CurrentSourceProvider(repo, repo_id="repo")
    (repo / "src" / "app.py").write_text(
        "# comment\ndef f():\n    return 1\n",
        encoding="utf-8",
    )
    mapper = ChangedRegionMapper(
        repo,
        repo_id="repo",
        baseline_source=baseline_source,
        current_source=current_source,
        baseline_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=1,
                end_line=2,
            )
        ],
        current_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=2,
                end_line=3,
            )
        ],
    )

    paths = mapper.changed_paths()
    regions = mapper.changed_regions(paths)

    assert [item.change_kind for item in paths] == ["modified"]
    assert (
        regions[0].baseline_normalized_implementation_digest
        == regions[0].current_normalized_implementation_digest
    )
    assert regions[0].classification == "non_implementation"
    assert regions[0].mapping_status == "not_applicable"


def test_comment_in_a_mapped_symbol_is_not_misclassified_as_an_addition(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    baseline = _baseline(repo)
    (repo / "src" / "app.py").write_text(
        "def f():\n    # retained source comment\n    return 1\n",
        encoding="utf-8",
    )
    mapper = ChangedRegionMapper(
        repo,
        repo_id="repo",
        baseline_source=BaselineSourceProvider(repo, baseline),
        current_source=CurrentSourceProvider(repo, repo_id="repo"),
        baseline_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=1,
                end_line=2,
            )
        ],
        current_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=1,
                end_line=3,
            )
        ],
    )

    region = mapper.changed_regions()[0]

    assert region.baseline_symbol_id == "fn:app.f"
    assert region.current_symbol_id == "fn:app.f"
    assert region.classification == "non_implementation"
    assert region.mapping_status == "not_applicable"
    assert (
        region.baseline_normalized_implementation_digest
        == region.current_normalized_implementation_digest
    )


def test_untracked_source_is_visible_as_a_changed_path_and_region(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    baseline = _baseline(repo)
    (repo / "src" / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
    mapper = ChangedRegionMapper(
        repo,
        repo_id="repo",
        baseline_source=BaselineSourceProvider(repo, baseline),
        current_source=CurrentSourceProvider(repo, repo_id="repo"),
        baseline_nodes=[],
        current_nodes=[
            Node(
                id="mod:new",
                kind="module",
                name="new",
                path="src/new.py",
                start_line=1,
                end_line=1,
            )
        ],
    )

    paths = mapper.changed_paths()
    regions = mapper.changed_regions(paths)

    assert paths[-1].change_kind == "untracked"
    assert any(region.classification == "untracked" for region in regions)


def test_deleted_source_keeps_the_baseline_symbol_mapping(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    baseline = _baseline(repo)
    (repo / "src" / "app.py").unlink()
    mapper = ChangedRegionMapper(
        repo,
        repo_id="repo",
        baseline_source=BaselineSourceProvider(repo, baseline),
        current_source=CurrentSourceProvider(repo, repo_id="repo"),
        baseline_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=1,
                end_line=2,
            )
        ],
        current_nodes=[],
    )

    paths = mapper.changed_paths()
    regions = mapper.changed_regions(paths)

    assert [item.change_kind for item in paths] == ["deleted"]
    assert regions[0].baseline_symbol_id == "fn:app.f"
    assert regions[0].mapping_status == "mapped"


def test_hunk_with_blank_boundary_maps_the_added_symbol(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    baseline = _baseline(repo)
    (repo / "src" / "app.py").write_text(
        "def f():\n" "    return 1\n" "\n" "\n" "def added():\n" "    return 2\n",
        encoding="utf-8",
    )
    mapper = ChangedRegionMapper(
        repo,
        repo_id="repo",
        baseline_source=BaselineSourceProvider(repo, baseline),
        current_source=CurrentSourceProvider(repo, repo_id="repo"),
        baseline_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=1,
                end_line=2,
            )
        ],
        current_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=1,
                end_line=2,
            ),
            Node(
                id="fn:app.added",
                kind="function",
                name="added",
                path="src/app.py",
                start_line=5,
                end_line=6,
            ),
        ],
    )

    regions = mapper.changed_regions()

    assert len(regions) == 1
    assert regions[0].new_start_line == 3
    assert regions[0].new_end_line == 6
    assert regions[0].current_symbol_id == "fn:app.added"
    assert regions[0].classification == "added"
    assert regions[0].mapping_status == "mapped"
    assert regions[0].evidence[0]["new_symbol_range"] == [5, 6]
    expected = normalize_implementation_region(
        "src/app.py",
        (repo / "src" / "app.py").read_bytes(),
        start_line=5,
        end_line=6,
    )
    document = CurrentSourceProvider(repo, repo_id="repo").read("src/app.py")
    assert (
        regions[0].current_normalized_implementation_digest
        == expected.normalized_implementation_digest
    )
    assert (
        regions[0].current_normalized_implementation_digest
        != document.normalization.normalized_implementation_digest
    )


def test_mapped_symbol_digests_exclude_unrelated_symbols(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src" / "app.py").write_text(
        "def f():\n    return 1\n\n\ndef untouched():\n    return 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "add an unrelated symbol")
    baseline = _baseline(repo)
    (repo / "src" / "app.py").write_text(
        "def f():\n    return 3\n\n\ndef untouched():\n    return 2\n",
        encoding="utf-8",
    )
    baseline_source = BaselineSourceProvider(repo, baseline)
    mapper = ChangedRegionMapper(
        repo,
        repo_id="repo",
        baseline_source=baseline_source,
        current_source=CurrentSourceProvider(repo, repo_id="repo"),
        baseline_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=1,
                end_line=2,
            ),
            Node(
                id="fn:app.untouched",
                kind="function",
                name="untouched",
                path="src/app.py",
                start_line=5,
                end_line=6,
            ),
        ],
        current_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=1,
                end_line=2,
            ),
            Node(
                id="fn:app.untouched",
                kind="function",
                name="untouched",
                path="src/app.py",
                start_line=5,
                end_line=6,
            ),
        ],
    )

    region = mapper.changed_regions()[0]
    baseline_document = baseline_source.read("src/app.py")
    expected = normalize_implementation_region(
        "src/app.py",
        baseline_document.content,
        start_line=1,
        end_line=2,
    )

    assert region.baseline_symbol_id == "fn:app.f"
    assert region.current_symbol_id == "fn:app.f"
    assert region.baseline_raw_source_digest == expected.raw_source_digest
    assert (
        region.baseline_normalized_implementation_digest
        == expected.normalized_implementation_digest
    )
    assert (
        region.baseline_normalized_implementation_digest
        != baseline_document.normalization.normalized_implementation_digest
    )


def test_baseline_provider_rejects_dirty_or_mismatched_baseline_identity(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    baseline = _baseline(repo)

    dirty = baseline.model_copy(
        update={
            "working_tree_identity": baseline.working_tree_identity.model_copy(
                update={"clean": False}
            )
        }
    )
    with pytest.raises(BaselineSourceUnavailable):
        BaselineSourceProvider(repo, dirty)

    mismatched = baseline.model_copy(
        update={
            "baseline_source_identity": {
                **baseline.baseline_source_identity,
                "git_tree_sha": "0" * 40,
            }
        }
    )
    with pytest.raises(BaselineSourceIdentityMismatch):
        BaselineSourceProvider(repo, mismatched)


def test_baseline_provider_reports_missing_git_object(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    baseline = _baseline(repo)
    missing_sha = "a" * 40
    missing = baseline.model_copy(
        update={
            "build_identity": baseline.build_identity.model_copy(
                update={"commit_sha": missing_sha}
            ),
            "working_tree_identity": baseline.working_tree_identity.model_copy(
                update={"head_sha": missing_sha}
            ),
        }
    )

    with pytest.raises(BaselineGitObjectMissing):
        BaselineSourceProvider(repo, missing)


def test_rename_retains_both_paths_and_symbol_mappings(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    baseline = _baseline(repo)
    _git(repo, "mv", "src/app.py", "src/renamed.py")
    mapper = ChangedRegionMapper(
        repo,
        repo_id="repo",
        baseline_source=BaselineSourceProvider(repo, baseline),
        current_source=CurrentSourceProvider(repo, repo_id="repo"),
        baseline_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/app.py",
                start_line=1,
                end_line=2,
            )
        ],
        current_nodes=[
            Node(
                id="fn:app.f",
                kind="function",
                name="f",
                path="src/renamed.py",
                start_line=1,
                end_line=2,
            )
        ],
    )

    paths = mapper.changed_paths()
    regions = mapper.changed_regions(paths)

    assert len(paths) == 1
    assert paths[0].change_kind == "renamed"
    assert paths[0].old_path == "src/app.py"
    assert paths[0].new_path == "src/renamed.py"
    assert regions[0].baseline_symbol_id == "fn:app.f"
    assert regions[0].current_symbol_id == "fn:app.f"
    assert regions[0].old_path == "src/app.py"
    assert regions[0].old_path_comparison_key == "src/app.py"
    assert regions[0].new_path == "src/renamed.py"
    assert regions[0].new_path_comparison_key == "src/renamed.py"
    assert regions[0].classification == "renamed"
    assert regions[0].mapping_status == "mapped"
