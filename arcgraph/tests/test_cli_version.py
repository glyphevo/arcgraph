from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from arcgraph import __version__
from arcgraph.interfaces.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_global_version_uses_the_package_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == f"arcgraph {__version__}\n"


def test_source_checkout_wrapper_reports_the_same_version() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/arcgraph.py", "--version"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.stdout == f"arcgraph {__version__}\n"
    assert completed.stderr == ""
