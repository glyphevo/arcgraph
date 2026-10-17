"""Completed control-flow proofs also make function-local imports available."""

import textwrap

import pytest

from arcgraph.analyzers.calls.lexical import LexicalScopes
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_reviewed_receiver_contracts import call_edges

POSITIVE_BODIES = [
    """
    try:
        IMPORT
    except ImportError:
        return None
    CALL
    """,
    """
    try:
        IMPORT
    except ImportError:
        raise RuntimeError('unavailable')
    CALL
    """,
    """
    try:
        value = 1
    except Exception:
        return None
    else:
        IMPORT
    CALL
    """,
    """
    try:
        IMPORT
    except ImportError:
        return None
    except Exception:
        raise
    CALL
    """,
    """
    for value in values:
        try:
            IMPORT
        except ImportError:
            continue
        CALL
    """,
    """
    try:
        IMPORT
        value = 1
    except ImportError:
        return None
    CALL
    """,
    """
    for value in values:
        try:
            IMPORT
        except ImportError:
            break
        CALL
    """,
]

NEGATIVE_BODIES = [
    """
    try:
        IMPORT
    except ImportError:
        pass
    CALL
    """,
    """
    try:
        IMPORT
    except ImportError:
        if flag:
            return None
    CALL
    """,
    """
    try:
        IMPORT
    except ImportError:
        bound = None
    CALL
    """,
    """
    try:
        IMPORT
    except ImportError:
        CALL
        return None
    """,
    """
    try:
        IMPORT
    except ImportError:
        return None
    finally:
        CALL
    """,
    """
    try:
        if flag:
            IMPORT
    except ImportError:
        return None
    CALL
    """,
    """
    for value in values:
        try:
            IMPORT
        except ImportError:
            continue
    CALL
    """,
]


def import_source(body, module_import, nested):
    declaration = (
        "from . import provider as bound"
        if module_import
        else "from .provider import normalize as bound"
    )
    callee = "bound.normalize" if module_import else "bound"
    body = textwrap.dedent(body).replace("IMPORT", declaration)
    body = body.replace("CALL", f"{callee}('settled')")
    prefix = "def use(flag=False, values=()):\n"
    if nested:
        prefix += "    def unrelated():\n        pass\n"
    return prefix + textwrap.indent(body, "    ")


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("module_import", [False, True])
@pytest.mark.parametrize("body", POSITIVE_BODIES)
def test_import_is_available_after_its_settling_statement(
    tmp_path, body, module_import, nested
):
    edges = call_edges(tmp_path, import_source(body, module_import, nested))
    calls = [e for e in edges if e.target == "fn:pkg.provider.normalize"]
    assert calls
    assert all(e.confidence == "confirmed" for e in calls)
    strategy = "imported_module_attribute" if module_import else "import_alias"
    assert all(e.resolution.strategy == strategy for e in calls)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("module_import", [False, True])
@pytest.mark.parametrize("body", NEGATIVE_BODIES)
def test_import_settlement_does_not_prove_an_unbound_path(
    tmp_path, body, module_import, nested
):
    edges = call_edges(tmp_path, import_source(body, module_import, nested))
    assert not any(
        e.target == "fn:pkg.provider.normalize" and e.confidence != "heuristic"
        for e in edges
    )


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("body", POSITIVE_BODIES + NEGATIVE_BODIES)
def test_assignment_uses_the_same_availability_proof(tmp_path, body, nested):
    source = import_source(body, False, nested).replace(
        "from .provider import normalize as bound", "bound = lambda value: value"
    )
    call_edges(tmp_path, source)
    nodes = list(GraphStoreReader.from_current(tmp_path / "out").read_nodes())
    scope = next(node for node in nodes if node.id == "fn:pkg.user.use")
    callsite = next(
        call for call in scope.properties["callsites"] if call["name"] == "bound"
    )
    binding = LexicalScopes(nodes, {}).binding(scope, "bound", callsite)
    assert (binding is not None) is (body in POSITIVE_BODIES)
