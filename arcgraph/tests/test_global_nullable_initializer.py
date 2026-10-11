"""An explicit global declaration participates in a local conditional join."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def calls(tmp_path, annotation):
    _targets(
        tmp_path,
        {
            "user": "import logging\nshared: logging.Logger | None = None\ndef configured()"
            + annotation
            + ":\n    return "
            + (
                "logging.getLogger()"
                if annotation == " -> logging.Logger"
                else "object()"
            )
            + '\ndef use():\n    global shared\n    if shared is None:\n        shared = configured()\n    shared.getChild("x")\n'
        },
        v2=True,
    )
    return {
        (e.target, e.confidence)
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use"
    }


def test_known_nullable_global_initializer_retains_the_declared_entry_type(tmp_path):
    assert ("extsym:logging.Logger.getChild", "inferred") in calls(
        tmp_path, " -> logging.Logger"
    )


@pytest.mark.parametrize("annotation", ["", " -> object"])
def test_unknown_or_different_global_write_is_not_discarded(tmp_path, annotation):
    assert ("extsym:logging.Logger.getChild", "inferred") not in calls(
        tmp_path, annotation
    )
