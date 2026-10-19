"""Session resources for semantic tests; xdist gives each worker its own copy."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from arcgraph.tests.semantic_installation_support import verified_pyright
from arcgraph.tests.script_execution_support import script_popen


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "private_pyright: keep an independent install for cache mutation/safety tests",
    )


@pytest.fixture(autouse=True)
def windows_fixture_scripts(monkeypatch, tmp_path_factory):
    if os.name != "nt":
        return
    root = tmp_path_factory.getbasetemp()
    git = shutil.which("git")
    shell = None
    if git:
        directory = Path(git).resolve().parents[1]
        for candidate in (directory / "usr/bin/sh.exe", directory / "bin/sh.exe"):
            if candidate.is_file():
                shell = str(candidate)
                break
    shell = shell or shutil.which("sh")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        script_popen(subprocess.Popen, root, sys.executable, shell),
    )


@pytest.fixture(scope="session")
def shared_pyright(tmp_path_factory):
    # Requested lazily by prepared, after its dependency/skip checks. Workers
    # never share a writable HOME or a project interpreter/environment.
    home = tmp_path_factory.mktemp("shared-pyright") / "home"
    with verified_pyright(home, os.environ["ARCGRAPH_TEST_PYRIGHT_TGZ"]) as install:
        yield install
