"""Factory annotations are interpreted in their declaration module."""

import pytest
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets

UNRELATED = "class Connection:\n    def execute(self, sql):pass\n"
PROVIDER = "import sqlite3\nclass Store:\n    @classmethod\n    def make(cls) -> 'Store':return cls()\n    def connect(self) -> sqlite3.Connection:\n        return sqlite3.connect(':memory:')\n"


def test_foreign_qualified_return_is_not_an_unrelated_short_name(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "other": UNRELATED,
            "provider": PROVIDER,
            "user": "from pkg.provider import Store\ndef use():\n    connection=Store.make().connect()\n    connection.execute('select 1')\n",
        },
        v2=True,
    )
    assert "extsym:sqlite3.Connection.execute" in targets
    assert "method:pkg.other.Connection.execute" not in targets


def test_foreign_return_provides_the_actual_manager_contract(tmp_path):
    _targets(
        tmp_path,
        {
            "other": UNRELATED,
            "provider": PROVIDER,
            "user": "from pathlib import Path\nfrom pkg.provider import Store\ndef use(p: Path):\n    store=Store.make()\n    with store.connect():\n        q=p / 'a'\n    q.exists()\n",
        },
        v2=True,
    )
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    assert any(
        e.source == "fn:pkg.user.use" and e.target == "extsym:pathlib.Path.exists"
        for e in edges
    )


@pytest.mark.parametrize(
    "rebind", ["sqlite3=object()", "if flag:\n    sqlite3=object()"]
)
def test_foreign_return_does_not_invent_a_type_after_import_rebinding(tmp_path, rebind):
    targets = _targets(
        tmp_path,
        {
            "other": UNRELATED,
            "provider": "from __future__ import annotations\nimport sqlite3\n"
            + rebind
            + "\ndef connect() -> sqlite3.Connection:pass\n",
            "user": "from pkg.provider import connect\ndef use():connect().execute('select 1')\n",
        },
        v2=True,
    )
    assert "extsym:sqlite3.Connection.execute" not in targets
    assert "method:pkg.other.Connection.execute" not in targets


def test_a_function_body_binding_does_not_shadow_its_return_declaration(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "other": UNRELATED,
            "provider": "import sqlite3\nimport sqlite3 as library\ndef connect() -> sqlite3.Connection:\n    sqlite3=object()\n    return library.connect(':memory:')\n",
            "user": "from pkg.provider import connect\ndef use():connect().execute('select 1')\n",
        },
        v2=True,
    )
    assert "extsym:sqlite3.Connection.execute" in targets
    assert "method:pkg.other.Connection.execute" not in targets
