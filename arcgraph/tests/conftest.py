"""Session resources for semantic tests; xdist gives each worker its own copy."""

import os

import pytest

from arcgraph.tests.semantic_installation_support import verified_pyright


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "private_pyright: keep an independent install for cache mutation/safety tests",
    )


@pytest.fixture(scope="session")
def shared_pyright(tmp_path_factory):
    # Requested lazily by prepared, after its dependency/skip checks. Workers
    # never share a writable HOME or a project interpreter/environment.
    home = tmp_path_factory.mktemp("shared-pyright") / "home"
    with verified_pyright(home, os.environ["ARCGRAPH_TEST_PYRIGHT_TGZ"]) as install:
        yield install
