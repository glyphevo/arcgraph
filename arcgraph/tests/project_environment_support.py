"""Explicit project interpreters, independent of the pytest host's layout."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

MODERN_ON_PATH = shutil.which("python3.14")
LEGACY_ON_PATH = shutil.which("python3.11")
TEST_IMPORT_PATH = tuple(sys.path)


def project_site_packages(environment):
    windows = environment / "Lib/site-packages"
    return (
        windows
        if windows.is_dir()
        else next((environment / "lib").glob("python*/site-packages"))
    )


def runtime_environment(root):
    environment = root / ".venv"
    if not (environment / "pyvenv.cfg").is_file():
        subprocess.run(
            [sys.executable, "-I", "-m", "venv", "--without-pip", str(environment)],
            check=True,
            capture_output=True,
            # A hang guard only: creating a venv on a slow CI runner took over 30 s.
            timeout=120,
        )
    return environment


def modern_python():
    for executable in (
        os.environ.get("ARCGRAPH_TEST_PROJECT_PYTHON"),
        MODERN_ON_PATH,
    ):
        if not executable:
            continue
        result = subprocess.run(
            [executable, "-I", "-c", "import sys; print(sys.version_info >= (3, 14))"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        if result.stdout.strip() == "True":
            return str(Path(executable).absolute())
    message = "A provisioned Python 3.14 is required; this test never downloads"
    if os.environ.get("ARCGRAPH_REQUIRE_SEMANTIC") == "1":
        pytest.fail(message)
    pytest.skip(message)


def legacy_python():
    if sys.version_info[:2] == (3, 11):
        return sys.executable
    executable = os.environ.get("ARCGRAPH_TEST_LEGACY_PYTHON") or LEGACY_ON_PATH
    if executable:
        return executable
    message = "A provisioned Python 3.11 is required for the legacy overload contract"
    if os.environ.get("ARCGRAPH_REQUIRE_SEMANTIC") == "1":
        pytest.fail(message)
    pytest.skip(message)


def isolated_test_python(root):
    """Give isolated CLI children the test installation's dependencies explicitly."""
    environment = runtime_environment(root / "test-runner")
    test_dependencies(environment)
    return str(
        environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )


def test_dependencies(environment):
    """Expose the invoking test installation in an explicitly selected fixture venv."""
    site = (
        environment / "Lib/site-packages"
        if os.name == "nt"
        else next((environment / "lib").glob("python*/site-packages"))
    )
    (site / "00_test_installation.pth").write_text(
        "import sys; sys.path[:0] = " + repr(list(TEST_IMPORT_PATH)) + "\n",
        encoding="utf-8",
    )
