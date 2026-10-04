from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from arcgraph.change import paths
from arcgraph.change.errors import ChangeStoreCorrupt, OutputContainmentError
from arcgraph.change.paths import resolve_under_root
from arcgraph.change.pins import BuildPinManager
from arcgraph.change.store import ChangeStateStore
from arcgraph.tests.test_change_store import _revision


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")


def _store_with_revision(output: Path) -> ChangeStateStore:
    store = ChangeStateStore(output, repo_id="repo")
    store.write_revision(_revision())
    return store


def test_store_path_rejects_a_symlinked_record_inside_the_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    (root / "plans").mkdir(parents=True)
    other = root / "plans" / "other.json"
    other.write_text("{}", encoding="utf-8")
    _link(root / "plans" / "current.json", other)

    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        resolve_under_root(root, "plans", "current.json")


def test_store_path_rejects_a_symlinked_directory_inside_the_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    (root / "moved").mkdir(parents=True)
    _link(root / "plans", root / "moved")

    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        resolve_under_root(root, "plans", "missing.json")


def test_store_path_reports_an_escaping_symlink_as_containment(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    _link(root / "record.json", outside)

    with pytest.raises(OutputContainmentError):
        resolve_under_root(root, "record.json")


def test_store_path_reports_a_symlink_loop_as_corruption(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    loop = root / "loop"
    _link(loop, loop)

    # Where resolve() raises for the loop the message says so; Python 3.12+
    # on Windows leaves the loop in place and the link check reports it.
    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        resolve_under_root(root, "loop", "record.json")


def test_store_path_allows_a_root_behind_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "plans").mkdir(parents=True)
    alias = tmp_path / "alias"
    _link(alias, real)

    resolved = resolve_under_root(alias, "plans", "current.json")

    assert resolved == real.resolve() / "plans" / "current.json"


def test_store_path_does_not_mistake_a_case_difference_for_a_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    (root / "Plans").mkdir(parents=True)

    # Case-insensitive file systems resolve to the on-disk name on Windows
    # and keep the given name elsewhere; neither is a symlink.
    resolved = resolve_under_root(root, "plans", "current.json")

    assert resolved.name == "current.json"


def test_store_path_compares_resolved_paths_in_normalized_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve() / "store"
    original_resolve = Path.resolve

    def resolve_like_windows(self: Path, strict: bool = False) -> Path:
        resolved = original_resolve(self, strict=strict)
        if resolved.name == "current.json":
            return resolved.with_name("CURRENT.JSON")
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_like_windows)
    # Pin both behaviors so the test does not depend on the host's normcase.
    monkeypatch.setattr(paths.os.path, "normcase", lambda value: value)
    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        resolve_under_root(root, "current.json")

    monkeypatch.setattr(paths.os.path, "normcase", lambda value: value.lower())
    assert resolve_under_root(root, "current.json").name == "CURRENT.JSON"


def test_store_rejects_reading_a_symlinked_revision(tmp_path: Path) -> None:
    store = _store_with_revision(tmp_path / "output")
    record = store.revision_path("plan", 1)
    moved = record.with_name("moved.json")
    record.rename(moved)
    _link(record, moved)

    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        store.read_revision("plan", 1)


def test_store_rejects_writing_through_a_symlink_and_keeps_its_target(
    tmp_path: Path,
) -> None:
    store = _store_with_revision(tmp_path / "output")
    record = store.revision_path("plan", 1)
    target = store.root / "target.json"
    target.write_text("{}", encoding="utf-8")
    record.unlink()
    _link(record, target)

    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        store.write_revision(_revision())
    assert target.read_text(encoding="utf-8") == "{}"
    assert record.is_symlink()


def test_store_rejects_a_symlinked_store_root(tmp_path: Path) -> None:
    output = tmp_path / "output"
    store = _store_with_revision(output)
    real = output / "real-store"
    store.root.rename(real)
    _link(output / "change-safety", real)

    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        ChangeStateStore(output, repo_id="repo")


def test_store_works_when_the_output_directory_is_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real-output"
    real.mkdir()
    alias = tmp_path / "output-alias"
    _link(alias, real)

    store = _store_with_revision(alias)

    assert store.read_revision("plan", 1).plan_id == "plan"
    store.health_check()
    assert (real / "change-safety").is_dir()


def test_pin_store_rejects_a_symlinked_root_and_record(tmp_path: Path) -> None:
    output = tmp_path / "output"
    (output / "real-pins").mkdir(parents=True)
    _link(output / "pins", output / "real-pins")
    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        BuildPinManager(output, repo_id="repo")

    output = tmp_path / "output-2"
    manager = BuildPinManager(output, repo_id="repo")
    records = manager.root / "records"
    records.mkdir(parents=True)
    elsewhere = manager.root / "elsewhere.json"
    elsewhere.write_text(json.dumps({"pin_id": "pin"}), encoding="utf-8")
    _link(records / "pin.json", elsewhere)
    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        manager.get("pin")


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-only")
def test_store_rejects_a_junction_as_its_root(tmp_path: Path) -> None:
    output = tmp_path / "output"
    real = output / "real-store"
    real.mkdir(parents=True)
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(output / "change-safety"), str(real)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr}")

    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        ChangeStateStore(output, repo_id="repo")


def test_store_path_rejects_a_link_that_resolution_leaves_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python 3.12+ on Windows returns a path through a symlink loop unchanged
    # instead of raising; simulate that so every platform covers the check.
    root = tmp_path.resolve() / "store"
    root.mkdir()
    target = root / "target.json"
    target.write_text("{}", encoding="utf-8")
    _link(root / "record.json", target)
    monkeypatch.setattr(
        Path, "resolve", lambda self, strict=False: Path(os.path.abspath(self))
    )

    with pytest.raises(ChangeStoreCorrupt, match="symlink"):
        resolve_under_root(root, "record.json")
