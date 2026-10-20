"""Shared test caches must fail closed and identify their last consumer."""

import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from arcgraph.tests.time_limits import SUBPROCESS_HANG_SECONDS
from arcgraph.tests import semantic_installation_support as support


@pytest.fixture
def installation(tmp_path):
    home = tmp_path / "home"
    (home / "installation/package").mkdir(parents=True)
    (home / "installation/package/entry.js").write_bytes(b"original")
    (home / "installation/archive.tgz").write_bytes(b"locked archive")
    (home / "active.json").write_bytes(b"pointer")
    (home / "empty").mkdir()
    return support.VerifiedPyright(home)


@pytest.mark.parametrize(
    "mutation",
    [
        "bytes",
        "archive",
        "pointer",
        "added_file",
        "removed_file",
        "added_directory",
        "removed_directory",
        "mode",
        "replacement",
        "link",
        "missing_home",
    ],
)
def test_receipt_rejects_content_structure_and_identity_drift(installation, mutation):
    home = installation.home
    entry = home / "installation/package/entry.js"
    installation.last_test = "test_consumer[last]"
    if mutation == "bytes":
        entry.write_bytes(b"replaced")  # same length
    elif mutation == "archive":
        (home / "installation/archive.tgz").write_bytes(b"other archive")
    elif mutation == "pointer":
        (home / "active.json").write_bytes(b"changed")
    elif mutation == "added_file":
        (home / "extra").touch()
    elif mutation == "removed_file":
        entry.unlink()
    elif mutation == "added_directory":
        (home / "extra").mkdir()
    elif mutation == "removed_directory":
        (home / "empty").rmdir()
    elif mutation == "mode":
        entry.chmod(entry.stat().st_mode ^ 0o200)
    elif mutation == "replacement":
        replacement = home / "replacement"
        replacement.write_bytes(entry.read_bytes())
        replacement.chmod(entry.stat().st_mode)
        os.replace(replacement, entry)
    elif mutation == "link":
        entry.unlink()
        entry.symlink_to(home / "active.json")
    else:
        home.rename(home.with_name("moved"))
    with pytest.raises(AssertionError, match=r"last test: test_consumer\[last\]"):
        installation.verify()


def consumer(nodeid, finalizers):
    return SimpleNamespace(
        node=SimpleNamespace(nodeid=nodeid), addfinalizer=finalizers.append
    )


def test_read_only_consumers_share_home_and_restore_environment(
    installation, monkeypatch
):
    before = {name: os.environ.get(name) for name in support.cache_environment(Path())}
    for nodeid in ("first", "second"):
        finalizers = []
        with monkeypatch.context() as patch:
            installation.use(consumer(nodeid, finalizers), patch)
            assert all(
                os.environ[name] == value
                for name, value in support.cache_environment(installation.home).items()
            )
            assert (installation.home / "active.json").read_bytes() == b"pointer"
            assert installation.last_test == nodeid
            assert len(finalizers) == 1
            finalizers[0]()
        assert {name: os.environ.get(name) for name in before} == before
    installation.verify()


def test_checkpoint_refuses_mutation_before_assigning_another_user(
    installation, monkeypatch
):
    finalizers = []
    installation.use(consumer("previous", finalizers), monkeypatch)
    (installation.home / "active.json").write_bytes(b"changed")
    with pytest.raises(AssertionError, match="last test: previous"):
        finalizers[0]()
    with pytest.raises(AssertionError, match="last test: previous"):
        installation.use(consumer("next", []), monkeypatch)


@pytest.mark.skipif(
    sys.platform != "darwin", reason="UF_IMMUTABLE regression requires macOS chflags"
)
def test_receipt_rejects_immutable_flag_before_assigning_another_user(
    installation, monkeypatch, request
):
    finalizers = []
    installation.use(consumer(request.node.nodeid, finalizers), monkeypatch)
    entry = installation.home / "installation/package/entry.js"
    flags = entry.stat().st_flags
    try:
        os.chflags(entry, flags | stat.UF_IMMUTABLE)
        assert entry.stat().st_flags & stat.UF_IMMUTABLE
        for checkpoint in (
            finalizers[0],
            lambda: installation.use(consumer("next", []), monkeypatch),
            installation.verify,
        ):
            with pytest.raises(AssertionError) as error:
                checkpoint()
            assert f"last test: {request.node.nodeid};" in str(error.value)
            assert installation.last_test == request.node.nodeid
    finally:
        os.chflags(entry, flags)
    installation.verify()


@pytest.mark.parametrize("changed", [False, True])
def test_real_pytest_session_installs_once_and_checks_teardown(tmp_path, changed):
    # A small offline stand-in exercises the real session fixture/finalizer.
    # The full suite's real installation still uses the locked archive validator.
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "from arcgraph.tests.conftest import shared_pyright as shared_pyright\n"
        "from arcgraph.tests import semantic_installation_support as support\n"
        "def pytest_configure(config):\n"
        "    def install(args):\n"
        "        assert args == ['setup', 'semantic', '--from', '/offline.tgz']\n"
        "        home = Path(os.environ['HOME'])\n"
        "        (home / 'package').write_bytes(b'verified')\n"
        "        Path('installs').open('a').write('install\\n')\n"
        "        return 0\n"
        "    def validate():\n"
        "        assert (Path(os.environ['HOME']) / 'package').read_bytes() == b'verified'\n"
        "        Path('installs').open('a').write('validate\\n')\n"
        "    support.main = install\n"
        "    support.validate_installation = validate\n",
        encoding="utf-8",
    )
    (tmp_path / "test_consumers.py").write_text(
        "def test_first(shared_pyright, request, monkeypatch):\n"
        "    shared_pyright.use(request, monkeypatch)\n"
        "def test_last(shared_pyright, request, monkeypatch):\n"
        "    shared_pyright.last_test = request.node.nodeid\n"
        + (
            "    (shared_pyright.home / 'package').write_bytes(b'changed')\n"
            if changed
            else "    shared_pyright.use(request, monkeypatch)\n"
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            "-p",
            "no:cov",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTEST_ADDOPTS": "",
            "ARCGRAPH_TEST_PYRIGHT_TGZ": "/offline.tgz",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=SUBPROCESS_HANG_SECONDS,
        check=False,
    )
    assert (tmp_path / "installs").read_text(encoding="utf-8").splitlines() == [
        "install",
        "validate",
    ]
    if changed:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "last test: test_consumers.py::test_last" in result.stdout
        assert "2 passed, 1 error" in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout
