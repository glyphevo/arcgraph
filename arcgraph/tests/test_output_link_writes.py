from __future__ import annotations

import os
from pathlib import Path

import pytest

from arcgraph.core.evidence_manifest import (
    build_evidence_manifest,
    evidence_manifest_path,
    evidence_sidecar_path,
    write_evidence_sidecar,
)
from arcgraph.tests.test_build_store_symlinks import _link, _write_build


def _outside(tmp_path: Path) -> Path:
    target = tmp_path / "outside.txt"
    target.write_text("keep", encoding="utf-8")
    return target


def test_publishing_current_does_not_write_through_a_link_left_at_the_old_temp_name(
    tmp_path: Path,
) -> None:
    output = tmp_path / "arcgraph"
    output.mkdir()
    target = _outside(tmp_path)
    _link(output / "current.json.tmp", target)

    _write_build(output, "index-1")

    assert target.read_text(encoding="utf-8") == "keep"
    assert not (output / "current.json").is_symlink()
    assert '"index_version": "index-1"' in (output / "current.json").read_text(
        encoding="utf-8"
    )


def test_evidence_sidecar_replaces_a_link_instead_of_writing_through_it(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    coverage = repo_root / "output" / "arcgraph" / "coverage.xml"
    coverage.parent.mkdir(parents=True)
    coverage.write_text("<coverage />", encoding="utf-8")
    target = _outside(tmp_path)
    sidecar = evidence_sidecar_path(coverage)
    _link(sidecar, target)

    write_evidence_sidecar(
        repo_root=repo_root,
        artifact_path=coverage,
        kind="coverage",
        commit_sha="abc",
        source_roots=["src"],
        tool_name="coverage",
    )

    assert target.read_text(encoding="utf-8") == "keep"
    assert not sidecar.is_symlink()
    assert '"kind": "coverage"' in sidecar.read_text(encoding="utf-8")


def test_live_evidence_manifest_replaces_a_link_instead_of_writing_through_it(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = repo_root / "output" / "arcgraph"
    manifest = evidence_manifest_path(output_dir)
    manifest.parent.mkdir(parents=True)
    target = _outside(tmp_path)
    _link(manifest, target)

    build_evidence_manifest(
        repo_root=repo_root,
        output_dir=output_dir,
        commit_sha="abc",
        source_roots=["src"],
        write_live=True,
    )

    assert target.read_text(encoding="utf-8") == "keep"
    assert not manifest.is_symlink()
    assert '"manifest_version"' in manifest.read_text(encoding="utf-8")


def test_replace_text_file_matches_write_text_bytes_and_mode(tmp_path: Path) -> None:
    from arcgraph.core.utils import replace_text_file

    text = '{\n  "a": "é"\n}'
    reference = tmp_path / "reference.json"
    reference.write_text(text, encoding="utf-8")
    replaced = tmp_path / "replaced.json"
    replaced.write_text("old", encoding="utf-8")

    replace_text_file(replaced, text)

    assert replaced.read_bytes() == reference.read_bytes()
    if os.name != "nt":
        assert replaced.stat().st_mode == reference.stat().st_mode
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "reference.json",
        "replaced.json",
    ]


def test_replace_text_file_removes_its_temp_file_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcgraph.core import utils

    destination = tmp_path / "current.json"
    destination.write_text("old", encoding="utf-8")

    def refuse(*_args: object) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(utils.os, "replace", refuse)
    with pytest.raises(OSError, match="rename refused"):
        utils.replace_text_file(destination, "new")

    assert destination.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["current.json"]
