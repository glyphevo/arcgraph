from __future__ import annotations

import json
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

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


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


def test_publishing_current_replaces_a_symlinked_current_pointer(
    tmp_path: Path,
) -> None:
    output = tmp_path / "arcgraph"
    _write_build(output, "index-1")
    target = _outside(tmp_path)
    (output / "current.json").unlink()
    _link(output / "current.json", target)

    _write_build(output, "index-2")

    assert target.read_text(encoding="utf-8") == "keep"
    assert not (output / "current.json").is_symlink()
    assert '"index_version": "index-2"' in (output / "current.json").read_text(
        encoding="utf-8"
    )


def _built_fixture(tmp_path: Path) -> Path:
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.pipeline.indexer import ArcGraphIndexer

    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    return output_dir


def _semantic_stats(output_dir: Path, *extra: str) -> int:
    from arcgraph.interfaces.cli import main

    return main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "semantic-stats",
            *extra,
        ]
    )


def test_default_semantic_stats_file_replaces_a_link(tmp_path: Path) -> None:
    output_dir = _built_fixture(tmp_path)
    stats = output_dir / "metrics" / "semantic-stats.json"
    stats.parent.mkdir(parents=True, exist_ok=True)
    target = _outside(tmp_path)
    _link(stats, target)

    assert _semantic_stats(output_dir) == 0

    assert target.read_text(encoding="utf-8") == "keep"
    assert not stats.is_symlink()
    reported = json.loads(stats.read_text(encoding="utf-8"))["metrics_path"]
    assert Path(reported) == stats.parent.resolve() / stats.name


def test_semantic_stats_output_named_by_the_user_is_still_written_in_place(
    tmp_path: Path,
) -> None:
    output_dir = _built_fixture(tmp_path)
    target = _outside(tmp_path)
    named = tmp_path / "named-stats.json"
    _link(named, target)

    assert _semantic_stats(output_dir, "--output", str(named)) == 0

    assert named.is_symlink()
    assert target.read_text(encoding="utf-8") != "keep"


def test_workbench_replaces_links_at_its_own_file_names(tmp_path: Path) -> None:
    from arcgraph.interfaces.workbench import write_workbench

    workbench = tmp_path / "workbench"
    workbench.mkdir()
    targets = {}
    for name in ("graph_data.json", "status_data.json", "index.html"):
        target = tmp_path / f"outside-{name}"
        target.write_text("keep", encoding="utf-8")
        _link(workbench / name, target)
        targets[name] = target

    write_workbench(output_dir=workbench, graph_payload={}, status_payload={})

    for name, target in targets.items():
        assert target.read_text(encoding="utf-8") == "keep", name
        assert not (workbench / name).is_symlink(), name
    assert (workbench / "index.html").stat().st_size > len("keep")


def _force_graph(
    output_dir: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> dict:
    from arcgraph.interfaces.cli import main

    capsys.readouterr()
    code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "visual",
            "force",
            *extra,
        ]
    )
    assert code == 0
    return json.loads(capsys.readouterr().out)


def test_default_force_graph_file_replaces_a_link_and_reports_where_it_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _built_fixture(tmp_path)
    graph = output_dir / "reports" / "force-graph.json"
    graph.parent.mkdir(parents=True)
    target = _outside(tmp_path)
    _link(graph, target)

    result = _force_graph(output_dir, capsys)

    assert target.read_text(encoding="utf-8") == "keep"
    assert not graph.is_symlink()
    assert Path(result["path"]) == graph.parent.resolve() / graph.name


def test_force_graph_output_named_by_the_user_is_still_written_in_place(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _built_fixture(tmp_path)
    target = _outside(tmp_path)
    named = tmp_path / "named-graph.json"
    _link(named, target)

    result = _force_graph(output_dir, capsys, "--output", str(named))

    assert named.is_symlink()
    assert target.read_text(encoding="utf-8") != "keep"
    assert Path(result["path"]) == target.resolve()


def test_visual_smoke_replaces_links_at_its_own_file_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcgraph.interfaces import visual_smoke
    from arcgraph.tests.test_visual_smoke import _fake_runner, _FakeServer

    smoke = tmp_path / "smoke"
    smoke.mkdir()
    targets = {}
    for name in ("scenario.js", "result.json"):
        target = tmp_path / f"outside-{name}"
        target.write_text("keep", encoding="utf-8")
        _link(smoke / name, target)
        targets[name] = target
    monkeypatch.setattr(visual_smoke.shutil, "which", lambda name: "npx")
    monkeypatch.setattr(
        visual_smoke, "create_visual_workbench_server", lambda *a, **k: _FakeServer()
    )

    result = visual_smoke.run_visual_smoke(
        object(),  # type: ignore[arg-type]
        options=visual_smoke.VisualSmokeOptions(output_dir=smoke),
        command_runner=_fake_runner([]),
    )

    for name, target in targets.items():
        assert target.read_text(encoding="utf-8") == "keep", name
        assert not (smoke / name).is_symlink(), name
    assert Path(result["artifacts"]["result_json"]) == smoke.resolve() / "result.json"


def test_visual_smoke_screenshots_replace_links_at_their_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcgraph.interfaces import visual_smoke
    from arcgraph.tests.test_visual_smoke import _fake_runner, _FakeServer

    commands: list[list[str]] = []
    base = _fake_runner(commands)

    class WritingRunner:
        """Writes the screenshot the way Node's writeFile does: through links."""

        def run(self, command: list[str], *, timeout_s: float):
            completed = base.run(command, timeout_s=timeout_s)
            if "screenshot" in command:
                with open(command[command.index("--filename") + 1], "wb") as handle:
                    handle.write(b"PNG")
            return completed

    smoke = tmp_path / "smoke"
    smoke.mkdir()
    targets = {}
    for name in ("overview.png", "focus-drawer.png"):
        target = tmp_path / f"outside-{name}"
        target.write_text("keep", encoding="utf-8")
        _link(smoke / name, target)
        targets[name] = target
    monkeypatch.setattr(visual_smoke.shutil, "which", lambda name: "npx")
    monkeypatch.setattr(
        visual_smoke, "create_visual_workbench_server", lambda *a, **k: _FakeServer()
    )

    result = visual_smoke.run_visual_smoke(
        object(),  # type: ignore[arg-type]
        options=visual_smoke.VisualSmokeOptions(output_dir=smoke),
        command_runner=WritingRunner(),
    )

    for name, target in targets.items():
        assert target.read_text(encoding="utf-8") == "keep", name
        assert not (smoke / name).is_symlink(), name
        assert (smoke / name).read_bytes() == b"PNG", name
    assert Path(result["artifacts"]["overview_screenshot"]) == (
        smoke.resolve() / "overview.png"
    )
    assert not [p for p in smoke.iterdir() if p.name.startswith(".")]
    packages = {c[c.index("--package") + 1] for c in commands if "--package" in c}
    # Releases after 0.1.22 must be checked for link-following writes before use.
    assert packages == {"@playwright/cli@0.1.22"}
