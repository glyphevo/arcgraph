from __future__ import annotations

import os
from pathlib import Path

import pytest

from arcgraph.change.contracts import BuildPin
from arcgraph.change.errors import ChangeStoreCorrupt
from arcgraph.change.pins import BuildPinManager
from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.core.schemas import FileRecord, IndexMetadata, Node


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")


def _write_build(output_dir: Path, index_version: str) -> Path:
    metadata = IndexMetadata(
        index_version=index_version,
        repo_root=str(output_dir.parent),
        source_roots=["src"],
    )
    files = [
        FileRecord(
            path="src/app.py",
            abs_path=str(output_dir.parent / "src" / "app.py"),
            source_root="src",
            module="app",
            file_hash="abc",
            line_count=1,
        )
    ]
    nodes = [Node(id="mod:app", kind="module", name="app", path="src/app.py")]
    return GraphStoreWriter(output_dir).write(metadata, files, nodes, [], [])


def test_reader_rejects_a_symlinked_build_inside_builds(tmp_path: Path) -> None:
    output = tmp_path / "arcgraph"
    build = _write_build(output, "index-1")
    moved = build.with_name("moved")
    build.rename(moved)
    _link(build, moved)

    with pytest.raises(FileNotFoundError, match="symlink"):
        GraphStoreReader.from_build(output, "index-1")
    with pytest.raises(FileNotFoundError, match="symlink"):
        GraphStoreReader.from_current(output)


def test_reader_rejects_a_symlinked_builds_directory(tmp_path: Path) -> None:
    output = tmp_path / "arcgraph"
    _write_build(output, "index-1")
    (output / "builds").rename(output / "real-builds")
    _link(output / "builds", output / "real-builds")

    with pytest.raises(FileNotFoundError, match="symlink"):
        GraphStoreReader.from_build(output, "index-1")


def test_reader_reports_a_symlink_loop_as_missing(tmp_path: Path) -> None:
    output = tmp_path / "arcgraph"
    (output / "builds").mkdir(parents=True)
    loop = output / "builds" / "index-1"
    _link(loop, loop)

    with pytest.raises(FileNotFoundError):
        GraphStoreReader.from_build(output, "index-1")


def test_reader_and_writer_work_when_the_output_directory_is_a_symlink(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real-output"
    real.mkdir()
    alias = tmp_path / "output-alias"
    _link(alias, real)

    _write_build(alias, "index-1")

    reader = GraphStoreReader.from_current(alias)
    assert reader.metadata["index_version"] == "index-1"
    assert (real / "builds" / "index-1" / "index.sqlite").is_file()


def test_writer_refuses_a_symlinked_builds_directory_and_leaves_its_target(
    tmp_path: Path,
) -> None:
    output = tmp_path / "arcgraph"
    output.mkdir()
    elsewhere = output / "elsewhere"
    elsewhere.mkdir()
    _link(output / "builds", elsewhere)

    with pytest.raises(RuntimeError, match="symlink"):
        _write_build(output, "index-1")
    assert list(elsewhere.iterdir()) == []


def test_pin_rejects_a_symlinked_build(tmp_path: Path) -> None:
    output = tmp_path / "arcgraph"
    build = _write_build(output, "index-1")
    moved = build.with_name("moved")
    build.rename(moved)
    _link(build, moved)
    manager = BuildPinManager(output, repo_id="repo")
    pin = BuildPin.model_construct(
        build_relative_path="builds/index-1", index_version="index-1"
    )

    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        manager._validate_build(pin)


def test_pin_accepts_a_regular_build(tmp_path: Path) -> None:
    output = tmp_path / "arcgraph"
    _write_build(output, "index-1")
    manager = BuildPinManager(output, repo_id="repo")
    pin = BuildPin.model_construct(
        build_relative_path="builds/index-1", index_version="index-1"
    )

    manager._validate_build(pin)


def test_pin_reports_an_unsafe_build_name_without_claiming_an_escape(
    tmp_path: Path,
) -> None:
    manager = BuildPinManager(tmp_path / "arcgraph", repo_id="repo")
    pin = BuildPin.model_construct(
        build_relative_path="builds/-bad", index_version="-bad"
    )

    with pytest.raises(ChangeStoreCorrupt, match="not a safe location") as caught:
        manager._validate_build(pin)
    assert "escapes" not in str(caught.value)


def test_build_paths_reject_links_that_resolution_leaves_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same simulation as the change-store test: resolve() keeps the link.
    output = tmp_path.resolve() / "arcgraph"
    build = _write_build(output, "index-1")
    moved = build.with_name("moved")
    build.rename(moved)
    _link(build, moved)
    current = output / "current.json"
    current.rename(output / "elsewhere.json")
    _link(current, output / "elsewhere.json")
    monkeypatch.setattr(
        Path, "resolve", lambda self, strict=False: Path(os.path.abspath(self))
    )

    with pytest.raises(FileNotFoundError, match="symlink"):
        GraphStoreReader.from_build(output, "index-1")
    with pytest.raises(FileNotFoundError, match="symlink"):
        GraphStoreReader.from_current(output)
