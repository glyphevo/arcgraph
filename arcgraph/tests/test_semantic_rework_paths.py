"""Trusted filesystem anchors may be links; executable contents may not."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from arcgraph.interfaces.cli import handle_doctor
from arcgraph.semantic.install import cache_root
from arcgraph.tests.test_semantic_isolation import prepared as prepared
from arcgraph.tests.project_environment_support import isolated_test_python


@pytest.mark.private_pyright
@pytest.mark.parametrize("anchor", ["temporary", "home", "cache", "macos"])
@pytest.mark.parametrize("entry", ["doctor", "shadow"])
def test_linked_trusted_anchor_supports_doctor_and_shadow(
    prepared, tmp_path, monkeypatch, anchor, entry
):
    project, _, markers, _ = prepared
    if anchor == "macos":
        if sys.platform != "darwin":
            pytest.skip("Real /var/folders temporary anchor is macOS-only")
        native = Path(os.environ.get("ARCGRAPH_TEST_PROJECT_PYTHON", ""))
        assert native.is_file()
        # The sandbox permits this user's real temporary directory; /var is a link.
        base = Path(
            subprocess.check_output(
                ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
                text=True,
                encoding="utf-8",
                env={"PYTHONDONTWRITEBYTECODE": "1"},
            ).strip()
        )  # nosec B603
        assert base.is_dir() and base != base.resolve()
        monkeypatch.setenv("TMPDIR", str(base))
    elif anchor == "temporary":
        real = tmp_path / "real-temporary"
        real.mkdir()
        link = tmp_path / "temporary-link"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setenv("TMPDIR", str(link))
    elif anchor == "home":
        link = tmp_path / "home-link"
        link.symlink_to(Path.home(), target_is_directory=True)
        monkeypatch.setenv("HOME", str(link))
        monkeypatch.setenv("USERPROFILE", str(link))
    else:
        original = cache_root()
        moved = tmp_path / "real-cache"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", None)
    output = tmp_path / "shadow"
    if entry == "doctor":
        doctor = handle_doctor(
            argparse.Namespace(repo_root=str(project), output_dir=str(output))
        )
        semantic = next(c for c in doctor["checks"] if c["name"] == "semantic_core")
        assert semantic["status"] == "pass", semantic
        assert not [name for name, path in markers.items() if path.exists()]
        return
    script = Path(__file__).resolve().parents[2] / "scripts/semantic_shadow.py"
    process = subprocess.run(
        [
            isolated_test_python(tmp_path),
            "-I",
            "-B",
            str(script),
            "--repo-root",
            str(project),
            "--output-dir",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )  # nosec B603
    assert process.returncode == 0, process.stderr + "".join(
        p.read_text(encoding="utf-8")
        for p in (output / "semantic-shadow").glob("*-stderr.txt")
    )
    assert json.loads(
        (output / "semantic-shadow/run.json").read_text(encoding="utf-8")
    )["complete"]
    assert not [name for name, path in markers.items() if path.exists()]
