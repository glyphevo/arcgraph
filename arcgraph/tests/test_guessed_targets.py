"""A name-based guess links only a method that exists.

GUESSED_METHODS_BY_OWNER lists every external method a guess may link. A
standard library method is checked against the running interpreter. A
third-party method is checked when its package imports here, and must in any
case be in THIRD_PARTY_VERIFIED, which records the version it was checked
against; a guess added for a package this suite does not install fails until
it is checked and recorded. That record cannot see a later release that drops
a method.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

import pytest

from arcgraph.analyzers.calls.constants import GUESSED_METHODS_BY_OWNER
from arcgraph.tests.test_path_receiver_types import _resolutions_by_function

# Checked on 2026-10-06: at run time where the package imported (fastapi
# 0.141.1, starlette 1.6.0, pydantic 2.13.5, click 8.5.0, httpx 0.28.1,
# pytest 9.1.1 in the project environment; redis 8.1.0, networkx 3.7,
# psutil 7.2.2, prometheus-client 0.26.0, pytest-benchmark 5.3.0 in a
# throwaway one), and by reading the class and its bases in the source where
# it did not import without its dependencies (sqlalchemy 2.1.3, typer 0.27.2,
# presidio-analyzer 2.2.364).
THIRD_PARTY_VERIFIED: dict[str, str] = {
    "click.Group": "click 8.5.0",
    "click.testing.CliRunner": "click 8.5.0",
    "fastapi.APIRouter": "fastapi 0.141.1",
    "fastapi.FastAPI": "fastapi 0.141.1",
    "httpx.Client": "httpx 0.28.1",
    "httpx.Response": "httpx 0.28.1",
    "networkx.DiGraph": "networkx 3.7",
    "networkx.Graph": "networkx 3.7",
    "presidio_analyzer.AnalyzerEngine": "presidio-analyzer 2.2.364",
    "presidio_analyzer.RecognizerRegistry": "presidio-analyzer 2.2.364",
    "prometheus_client.Counter": "prometheus-client 0.26.0",
    "prometheus_client.Gauge": "prometheus-client 0.26.0",
    "prometheus_client.Histogram": "prometheus-client 0.26.0",
    "prometheus_client.metrics.MetricWrapperBase": "prometheus-client 0.26.0",
    "psutil.Process": "psutil 7.2.2",
    "pydantic.BaseModel": "pydantic 2.13.5",
    "pytest.Item": "pytest 9.1.1",
    "pytest.MonkeyPatch": "pytest 9.1.1",
    "pytest_benchmark.fixture.BenchmarkFixture": "pytest-benchmark 5.3.0",
    "redis.asyncio.Redis": "redis 8.1.0",
    "sqlalchemy.engine.Result": "sqlalchemy 2.1.3",
    "sqlalchemy.orm.Session": "sqlalchemy 2.1.3",
    "sqlalchemy.sql.ColumnElement": "sqlalchemy 2.1.3",
    "sqlalchemy.sql.Select": "sqlalchemy 2.1.3",
    "sqlalchemy.sql.dml.UpdateBase": "sqlalchemy 2.1.3",
    "starlette.testclient.TestClient": "starlette 1.6.0",
    "typer.Typer": "typer 0.27.2",
}
# PEP 249 is a protocol with no module to import; how its guesses are named is
# an open decision (LEDGER E245), so they are not checked here yet.
PROTOCOL_PENDING = frozenset({"dbapi.Connection"})


def _owner_class(owner: str) -> object:
    module_name, _, class_name = owner.rpartition(".")
    return getattr(importlib.import_module(module_name), class_name)


def _is_standard_library(owner: str) -> bool:
    return owner.split(".", 1)[0] in sys.stdlib_module_names


def test_every_owner_is_checked_somewhere():
    unchecked = sorted(
        owner
        for owner in GUESSED_METHODS_BY_OWNER
        if not _is_standard_library(owner)
        and owner not in THIRD_PARTY_VERIFIED
        and owner not in PROTOCOL_PENDING
    )
    assert unchecked == []


@pytest.mark.parametrize(
    "owner",
    sorted(o for o in GUESSED_METHODS_BY_OWNER if _is_standard_library(o)),
)
def test_standard_library_guesses_exist(owner):
    owner_class = _owner_class(owner)
    missing = sorted(
        method
        for method in GUESSED_METHODS_BY_OWNER[owner]
        if not hasattr(owner_class, method)
    )
    assert missing == [], owner


@pytest.mark.parametrize("owner", sorted(THIRD_PARTY_VERIFIED))
def test_third_party_guesses_exist_where_installed(owner):
    try:
        owner_class = _owner_class(owner)
    except ImportError:
        pytest.skip(f"{owner.split('.', 1)[0]} is not installed here")
    # networkx's nodes and edges are cached properties: attributes that are
    # called on an instance, not methods of the class.
    missing = sorted(
        method
        for method in GUESSED_METHODS_BY_OWNER[owner]
        if not hasattr(owner_class, method)
    )
    assert missing == [], owner


SOURCE = """from prometheus_client import Counter

REQUESTS_TOTAL = Counter("requests", "requests", ["path"])


def guesses(items_map, rows, loader, stream, parser, stmt, graph, path):
    items_map.get("a")
    items_map.setdefault("a", 1)
    rows.append(1)
    loader.exec_module(None)
    stream.write("x")
    parser.add_parser("x")
    stmt.returning(1)
    graph.successors(1)
    REQUESTS_TOTAL.labels(path).inc()


def no_guess(app, circuit_breaker):
    app.route("/")
    circuit_breaker.record_failure()
"""


def test_guesses_name_methods_that_exist():
    with tempfile.TemporaryDirectory() as directory:
        resolutions = _resolutions_by_function(Path(directory), SOURCE)
    assert {target for target, _ in resolutions["guesses"]} == {
        # builtins has no mapping or sequence; the abstract classes do.
        "extsym:collections.abc.Mapping.get",
        "extsym:collections.abc.MutableMapping.setdefault",
        "extsym:collections.abc.MutableSequence.append",
        # importlib.abc.Loader does not define exec_module, nor io.IOBase write.
        "extsym:importlib.abc.InspectLoader.exec_module",
        "extsym:typing.IO.write",
        # add_parser is the subparsers action's, returning an update's or an
        # insert's, successors a directed graph's.
        "extsym:argparse._SubParsersAction.add_parser",
        "extsym:sqlalchemy.sql.dml.UpdateBase.returning",
        "extsym:networkx.DiGraph.successors",
        # MetricWrapperBase is not exported at the top, and inc is Counter's.
        "extsym:prometheus_client.metrics.MetricWrapperBase.labels",
        "extsym:prometheus_client.Counter.inc",
    }
    # FastAPI has no route, and the circuit_breaker package no CircuitBreaker.
    assert resolutions.get("no_guess", set()) == set()


def test_a_guess_outside_the_list_links_nothing():
    from arcgraph.analyzers.calls import CallAnalyzer

    analyzer = CallAnalyzer(enable_v2=True)
    assert analyzer._guessed_symbol("logging.Logger", "warning") is not None
    assert analyzer._guessed_symbol("logging.Logger", "nonexistent") is None
    assert analyzer._guessed_symbol("circuit_breaker.CircuitBreaker", "open") is None
