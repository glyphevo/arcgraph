"""Keep shipped documentation linked and limited to public product guidance."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_DIRECTORIES = ("docs/_internal/",)


def _public_markdown() -> list[Path]:
    # Ignore local operator material even when it exists in a developer checkout.
    return [
        ROOT / "README.md",
        ROOT / "RELEASE_NOTES.md",
        *sorted(
            p
            for p in (ROOT / "docs").rglob("*.md")
            if not any(
                p.relative_to(ROOT).as_posix().startswith(prefix)
                for prefix in PRIVATE_DIRECTORIES
            )
        ),
    ]


def test_public_docs_have_no_broken_local_markdown_links() -> None:
    missing = []
    for path in _public_markdown():
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            target = link.split("#", 1)[0]
            if not target or ":" in target or not target.endswith(".md"):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.is_file() or any(
                resolved.is_relative_to(ROOT / prefix) for prefix in PRIVATE_DIRECTORIES
            ):
                missing.append((path.relative_to(ROOT).as_posix(), target))
    assert not missing, missing


def test_tracked_tree_excludes_private_documentation() -> None:
    # Use HEAD rather than local ignored files; this is also checked after
    # assembling the candidate and filtering its history.
    paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    ).splitlines()
    assert not [p for p in paths if p.startswith(PRIVATE_DIRECTORIES)]
    assert "AGENTS.md" not in paths


def test_public_migration_docs_preserve_rebuild_and_read_limits() -> None:
    notes = (ROOT / "docs" / "release_notes" / "v0.1.0.md").read_text(encoding="utf-8")
    for required in (
        "Rebuild existing indexes",
        "analysis_truncated_scope",
        "truncated_scope",
        "1.4.0",
        "64 KiB",
        "32 KiB",
        "not a minimal dependency graph",
    ):
        assert required in notes


def test_release_notes_index_lists_every_per_version_note() -> None:
    """RELEASE_NOTES.md is an index, so it must not fall behind the notes."""

    index = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    notes = sorted((ROOT / "docs" / "release_notes").glob("*.md"))
    assert notes, "docs/release_notes has no per-version notes"
    for note in notes:
        assert f"docs/release_notes/{note.name}" in index, note.name

    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    assert version in index
