from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from arcgraph.change.errors import OutputContainmentError, RepositoryPathError
from arcgraph.change.paths import normalize_repository_path, resolve_under_root


def test_repository_path_uses_posix_display_and_case_aware_comparison_key(
    tmp_path: Path,
) -> None:
    normalized = normalize_repository_path(
        "Src\\Module.py", tmp_path, case_sensitive=False
    )

    assert normalized.display_path == "Src/Module.py"
    assert normalized.comparison_key == "src/module.py"


def test_repository_path_uses_git_case_semantics_not_only_os_name(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "core.ignorecase", "true"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    normalized = normalize_repository_path("Src/Module.py", tmp_path)

    assert normalized.comparison_key == "src/module.py"


@pytest.mark.parametrize(
    "value",
    ["/tmp/outside.py", "C:\\outside.py", "\\\\server\\share\\a.py", "src/../a.py", ""],
)
def test_repository_path_rejects_unsafe_or_foreign_path_syntax(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(RepositoryPathError):
        normalize_repository_path(value, tmp_path)


def test_repository_path_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")

    with pytest.raises(RepositoryPathError):
        normalize_repository_path("link.py", tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction regression is Windows-only")
def test_repository_path_rejects_junction_escape_without_symlink_privilege(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret\n", encoding="utf-8")
    junction = repo / "junction"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr}")

    with pytest.raises(RepositoryPathError):
        normalize_repository_path("junction/secret.py", repo)


def test_output_containment_rejects_identifier_traversal(tmp_path: Path) -> None:
    with pytest.raises(OutputContainmentError):
        resolve_under_root(tmp_path / "output", "plans", "../outside")
