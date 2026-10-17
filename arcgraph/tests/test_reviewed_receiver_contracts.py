"""Follow-on calls must follow declared interfaces and real overridden methods."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def call_edges(tmp_path, source):
    _targets(
        tmp_path,
        {"provider": "def normalize(value):\n    return value\n", "user": source},
        v2=True,
    )
    return [
        edge
        for edge in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if edge.source == "fn:pkg.user.use" and edge.resolution.status == "resolved"
    ]


@pytest.mark.parametrize("assigned", [False, True])
@pytest.mark.parametrize("fetch", ["all()", "fetchall()", "fetchmany(2)"])
@pytest.mark.parametrize(
    "result", ["Result", "ScalarResult", "MappingResult", "CursorResult"]
)
def test_sqlalchemy_fetched_rows_keep_the_declared_sequence_interface(
    tmp_path, result, fetch, assigned
):
    prefix = f"    rows = result.{fetch}\n" if assigned else ""
    value = "rows" if assigned else f"result.{fetch}"
    source = (
        f"from sqlalchemy.engine import {result}\ndef use(result: {result}):\n" + prefix
    )
    for method, args in [
        ("append", "None"),
        ("sort", ""),
        ("extend", "[]"),
        ("pop", ""),
        ("count", "None"),
        ("index", "None"),
    ]:
        source += f"    {value}.{method}({args})\n"
    targets = {edge.target for edge in call_edges(tmp_path, source)}
    # SQLAlchemy annotates these results as Sequence, not a mutable list.
    for method in ["append", "sort", "extend", "pop"]:
        assert not any(target.endswith("." + method) for target in targets)
    for method in ["count", "index"]:
        assert f"extsym:collections.abc.Sequence.{method}" in targets
        assert f"extsym:builtins.list.{method}" not in targets


@pytest.mark.parametrize("module", ["sqlalchemy.engine", "sqlalchemy.engine.base"])
@pytest.mark.parametrize("middle", ["", ".columns(0)", ".unique()", ".yield_per(2)"])
def test_connection_execute_retains_cursor_overrides_through_self_returns(
    tmp_path, module, middle
):
    source = (
        f"from {module} import Connection\nfrom sqlalchemy import text\n"
        "def use(conn: Connection):\n"
        f"    conn.execute(text('select 1')){middle}.yield_per(5).close()\n"
    )
    targets = {edge.target for edge in call_edges(tmp_path, source)}
    assert "extsym:sqlalchemy.engine.CursorResult.yield_per" in targets
    assert "extsym:sqlalchemy.engine.CursorResult.close" in targets
    assert "extsym:sqlalchemy.engine.Result.yield_per" not in targets
    assert "extsym:sqlalchemy.engine.Result.close" not in targets


def test_session_execute_retains_result_implementation(tmp_path):
    targets = {
        edge.target
        for edge in call_edges(
            tmp_path,
            "from sqlalchemy.orm import Session\nfrom sqlalchemy import text\n"
            "def use(session: Session):\n"
            "    session.execute(text('select 1')).yield_per(5).close()\n",
        )
    }
    assert "extsym:sqlalchemy.engine.Result.yield_per" in targets
    assert "extsym:sqlalchemy.engine.CursorResult.yield_per" not in targets


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("module_import", [False, True])
@pytest.mark.parametrize("block", ["if flag", "try", "with suppress(ImportError)"])
def test_function_import_scope_does_not_depend_on_nested_functions(
    tmp_path, block, module_import, nested
):
    declaration = (
        "from . import provider as local"
        if module_import
        else "from .provider import normalize as local"
    )
    callee = "local.normalize" if module_import else "local"
    source = "from contextlib import suppress\ndef use(flag):\n"
    if nested:
        source += "    def unrelated():\n        pass\n"
    source += f"    {block}:\n        {declaration}\n        {callee}('inside')\n"
    if block == "try":
        source += f"    finally:\n        {callee}('outside')\n"
    else:
        source += f"    {callee}('outside')\n"
    calls = [
        edge
        for edge in call_edges(tmp_path, source)
        if edge.target == "fn:pkg.provider.normalize" and edge.confidence != "heuristic"
    ]
    positions = {
        fact["call_expression"]
        for edge in calls
        for fact in (
            edge.properties.get("callsites") or [edge.properties.get("callsite", {})]
        )
    }
    assert f"{callee}('inside')" in positions
    assert f"{callee}('outside')" not in positions


def test_unavailable_local_import_never_falls_back_to_module_alias(tmp_path):
    edges = call_edges(
        tmp_path,
        "from .provider import normalize\ndef use(flag):\n"
        "    if flag:\n        from .provider import normalize\n"
        "    normalize('outside')\n",
    )
    assert all(
        edge.confidence == "heuristic"
        for edge in edges
        if edge.target == "fn:pkg.provider.normalize"
    )


@pytest.mark.parametrize("nested", [False, True])
def test_unavailable_import_cannot_type_a_later_constructor_result(tmp_path, nested):
    source = "def use(flag):\n"
    if nested:
        source += "    def unrelated():\n        pass\n"
    source += (
        "    if flag:\n        from io import BytesIO as Buffer\n"
        "    value = Buffer()\n    value.getvalue()\n"
    )
    assert not any(
        edge.target.endswith(".getvalue") for edge in call_edges(tmp_path, source)
    )


def test_repeated_identical_import_keeps_a_completed_binding(tmp_path):
    edges = call_edges(
        tmp_path,
        "def use(flag):\n    from .provider import normalize\n    normalize('before')\n    if flag:\n        from .provider import normalize\n    normalize('after')\n",
    )
    calls = [edge for edge in edges if edge.target == "fn:pkg.provider.normalize"]
    assert calls and all(edge.confidence == "confirmed" for edge in calls)


@pytest.mark.parametrize("nested", [False, True])
def test_possibly_unbound_external_import_has_only_boundary_confidence(
    tmp_path, nested
):
    source = "def use(flag):\n"
    if nested:
        source += "    def unrelated():\n        pass\n"
    source += "    if flag:\n        from json import dumps\n    dumps('outside')\n"
    calls = [e for e in call_edges(tmp_path, source) if e.target == "extsym:json.dumps"]
    assert calls and all(
        e.confidence == "heuristic"
        and e.resolution.strategy == "possibly_unbound_import"
        for e in calls
    )


@pytest.mark.parametrize("module_import", [False, True])
def test_conditional_callback_alias_reads_its_completed_import(tmp_path, module_import):
    declaration = (
        "from . import provider as local"
        if module_import
        else "from .provider import normalize as local"
    )
    value = "local.normalize" if module_import else "local"
    edges = call_edges(
        tmp_path,
        "def use(callback=None):\n    if callback is None:\n"
        f"        {declaration}\n        callback = {value}\n"
        "    callback('outside')\n",
    )
    calls = [edge for edge in edges if edge.target == "fn:pkg.provider.normalize"]
    # The import completed when the alias was captured. The caller can supply
    # another callback, so this is a possible target, never a definite one.
    assert calls and all(edge.confidence == "heuristic" for edge in calls)


def test_conditional_callback_alias_does_not_prove_a_return_type(tmp_path):
    edges = call_edges(
        tmp_path,
        "def use(callback=None):\n    if callback is None:\n"
        "        from io import BytesIO\n        callback = BytesIO\n"
        "    callback().getvalue()\n",
    )
    assert not any(edge.target.endswith(".getvalue") for edge in edges)
