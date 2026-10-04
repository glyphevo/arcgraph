from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from arcgraph.core.cleanup import plan_arcgraph_output_cleanup
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_build_store_symlinks import _link, _write_build


def _link_current(output: Path) -> None:
    current = output / "current.json"
    elsewhere = output / "elsewhere.json"
    shutil.copy(current, elsewhere)
    current.unlink()
    _link(current, elsewhere)


def test_reader_rejects_a_symlinked_current_pointer(tmp_path: Path) -> None:
    output = tmp_path / "arcgraph"
    _write_build(output, "index-1")
    _link_current(output)

    with pytest.raises(FileNotFoundError, match="symlink"):
        GraphStoreReader.from_current(output)


def test_cleanup_refuses_to_prune_with_a_symlinked_current_pointer(
    tmp_path: Path,
) -> None:
    output = tmp_path / "arcgraph"
    _write_build(output, "index-1")
    _write_build(output, "index-2")
    _link_current(output)

    plan = plan_arcgraph_output_cleanup(output, keep_builds=1)

    assert plan.current_build is None
    assert plan.candidates == []
    assert any("symlinked current.json" in warning for warning in plan.warnings)


def test_reader_and_cleanup_accept_a_regular_current_pointer(tmp_path: Path) -> None:
    output = tmp_path / "arcgraph"
    _write_build(output, "index-1")
    _write_build(output, "index-2")

    assert GraphStoreReader.from_current(output).metadata["index_version"] == (
        "index-2"
    )
    plan = plan_arcgraph_output_cleanup(output, keep_builds=1)
    assert plan.current_build == "builds/index-2"
