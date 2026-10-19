"""The Windows fixture adapter executes real scripts and ignores native commands."""

from pathlib import Path
import shutil
import subprocess
import sys

from arcgraph.tests.script_execution_support import script_command, script_popen


def test_python_fixture_translation_executes_the_checked_file(tmp_path):
    script = tmp_path / "python.exe"
    script.write_text(
        f"#!{sys.executable}\nimport sys; print(sys.argv[-1])\n",
        encoding="utf-8",
        newline="\n",
    )
    command = script_command([str(script), "argument"], tmp_path, sys.executable, None)
    result = subprocess.check_output(command, text=True, encoding="utf-8")
    assert result.strip() == "argument"
    assert command[1] == str(script)
    popen = script_popen(subprocess.Popen, tmp_path, sys.executable, None)
    with popen(
        args=[str(script), "keyword-argument"],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as process:
        output, _ = process.communicate()
        assert isinstance(process, subprocess.Popen)
    assert output.strip() == "keyword-argument"
    assert script_command(
        [str(script)], tmp_path / "unrelated", sys.executable, None
    ) == [str(script)]
    assert script_command(
        [sys.executable, "--version"], tmp_path, sys.executable, None
    ) == [
        sys.executable,
        "--version",
    ]
    script.write_bytes(b"MZnative executable")
    assert script_command([str(script)], tmp_path, sys.executable, None) == [
        str(script)
    ]


def test_shell_fixture_translation_keeps_arguments_and_contents(tmp_path):
    script = tmp_path / "node"
    script.write_text('#!/bin/sh\nprintf "%s" "$1"\n', encoding="utf-8", newline="\n")
    shell = shutil.which("sh")
    if shell is None:
        git = shutil.which("git")
        shell = str(Path(git).resolve().parents[1] / "usr/bin/sh.exe")
    command = script_command([str(script), "v20.0.0"], tmp_path, sys.executable, shell)
    assert subprocess.check_output(command, text=True, encoding="utf-8") == "v20.0.0"


def test_shadow_reports_unavailable_resource_without_fabricated_usage(monkeypatch):
    from scripts import semantic_shadow

    monkeypatch.setattr(semantic_shadow, "resource", None)
    assert semantic_shadow.usage() == {"self": None, "children": None}
