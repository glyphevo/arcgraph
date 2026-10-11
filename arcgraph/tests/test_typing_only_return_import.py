"""A typing-only return name needs the same stable import in the factory."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def targets(tmp_path, runtime, declared="from pathlib import Path"):
    _targets(
        tmp_path,
        {
            "provider": "from __future__ import annotations\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    "
            + declared
            + "\ndef make() -> Path:\n    "
            + runtime
            + "\n",
            "user": "from pkg.provider import make\ndef use():make().exists()\n",
        },
        v2=True,
    )
    return {
        (e.target, e.confidence)
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use"
    }


def test_matching_runtime_import_proves_the_typing_only_factory_result(tmp_path):
    assert ("extsym:pathlib.Path.exists", "inferred") in targets(
        tmp_path, 'from pathlib import Path\n    return Path("a")'
    )


@pytest.mark.parametrize(
    "runtime",
    [
        'from pathlib import PurePath as Path\n    return Path("a")',
        "from pathlib import Path\n    Path=object\n    return Path()",
        'if flag:\n        from pathlib import Path\n    return Path("a")',
    ],
)
def test_runtime_mismatch_rebinding_or_conditional_import_cannot_prove_path(
    tmp_path, runtime
):
    assert ("extsym:pathlib.Path.exists", "inferred") not in targets(tmp_path, runtime)


def test_a_concrete_runtime_import_cannot_override_a_different_typing_import(tmp_path):
    assert ("extsym:pathlib.Path.exists", "inferred") not in targets(
        tmp_path,
        'from pathlib import Path\n    return Path("a")',
        declared="from pathlib import PurePath as Path",
    )
