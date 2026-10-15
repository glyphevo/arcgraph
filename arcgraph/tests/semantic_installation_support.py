"""One verified, read-only pyright cache per pytest worker, never a shared venv."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import stat

import pytest

from arcgraph.interfaces.cli import main
from arcgraph.semantic.install import validate_installation


def cache_environment(home):
    return {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / "cache"),
        "LOCALAPPDATA": str(home / "local"),
    }


def content_receipt(root):
    """Include the pointer, archive, names, modes and bytes; ignore access times."""
    result = {}
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise AssertionError(f"Link or special file in shared pyright: {path}")
        result[path.relative_to(root).as_posix()] = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if stat.S_ISREG(info.st_mode)
                else None
            ),
        )
    return result


@dataclass
class VerifiedPyright:
    home: Path
    receipt: dict = field(init=False)
    last_test: str = "<session setup>"

    def __post_init__(self):
        self.receipt = content_receipt(self.home)

    def verify(self):
        try:
            current = content_receipt(self.home)
            changed = sorted(
                name
                for name in self.receipt.keys() | current.keys()
                if self.receipt.get(name) != current.get(name)
            )
            if changed:
                raise AssertionError("Changed entries: " + ", ".join(changed[:5]))
        except (OSError, AssertionError) as exc:
            raise AssertionError(
                f"Shared pyright installation changed; last test: {self.last_test}; {exc}"
            ) from exc

    def use(self, request, monkeypatch):
        # Check before assigning the next user, so a delayed change names the
        # previous user. Check again after each test and at session teardown.
        self.verify()
        self.last_test = request.node.nodeid
        request.addfinalizer(self.verify)
        for name, value in cache_environment(self.home).items():
            monkeypatch.setenv(name, value)


@contextmanager
def verified_pyright(home, archive):
    home.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        for name, value in cache_environment(home).items():
            patch.setenv(name, value)
        assert main(["setup", "semantic", "--from", archive]) == 0
        # Recompute bytes against the pinned archive before recording a receipt.
        # Never replace or bypass the product's validation on subsequent uses.
        validate_installation()
    installation = VerifiedPyright(home)
    try:
        yield installation
    finally:
        installation.verify()
