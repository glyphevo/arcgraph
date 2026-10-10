"""External definitions follow source contracts, not the receiver spelling."""

from types import SimpleNamespace
import pytest
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets

PREFERRED = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx2 as httpx\nelse:\n    try: import httpx2 as httpx\n    except ModuleNotFoundError: import httpx\nclass Client(httpx.Client): pass\ndef use(client: Client): client.get('/')\n"


def test_agreeing_typing_and_first_runtime_import_prefer_the_definition(tmp_path):
    targets = _targets(tmp_path, {"user": PREFERRED}, v2=True)
    assert "extsym:httpx2.Client.get" in targets
    assert "extsym:httpx.Client.get" not in targets
    nodes = GraphStoreReader.from_current(tmp_path / "out").read_nodes()
    assert any(
        n.kind == "diagnostic"
        and n.properties.get("reason") == "alternate_external_definition"
        and n.properties.get("definition_candidates") == ["extsym:httpx.Client.get"]
        for n in nodes
    )


@pytest.mark.parametrize("prefix", ["TYPE_CHECKING=flag\n", "httpx=other\n"])
def test_shadowed_or_inconsistent_imports_do_not_select_a_definition(tmp_path, prefix):
    source = (
        PREFERRED.replace("if TYPE_CHECKING:", prefix + "if TYPE_CHECKING:")
        if prefix.startswith("TYPE")
        else PREFERRED.replace("class Client", prefix + "class Client")
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert "extsym:httpx2.Client.get" not in targets


@pytest.mark.parametrize(
    "imported,base,own",
    [
        ("starlette.testclient", "httpx", False),
        ("starlette.testclient", "httpx2", False),
        ("fastapi.testclient", "httpx", False),
        ("starlette.testclient", "httpx", True),
    ],
)
def test_external_testclient_constructor_uses_installed_source_definition(
    tmp_path, monkeypatch, imported, base, own
):
    root = tmp_path / "installed"
    (root / "starlette").mkdir(parents=True)
    (root / "fastapi").mkdir()
    (root / "starlette/testclient.py").write_text(
        "import "
        + base
        + "\nclass TestClient("
        + base
        + ".Client):\n    "
        + ("def get(self,*args): pass" if own else "pass")
        + "\n",
        encoding="utf-8",
    )
    (root / "fastapi/testclient.py").write_text(
        "from starlette.testclient import TestClient\n", encoding="utf-8"
    )
    # Parent fails on the actual missing edge before importing the new helper.
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version="fixture", locate_file=lambda path: root / path
        ),
    )
    targets = _targets(
        tmp_path,
        {
            "user": "from "
            + imported
            + " import TestClient\ndef use(app):\n    client=TestClient(app)\n    client.get('/')\n"
        },
        v2=True,
    )
    expected = "starlette.testclient.TestClient.get" if own else base + ".Client.get"
    assert "extsym:" + expected in targets


def test_missing_dependency_source_does_not_invent_testclient_members(
    tmp_path, monkeypatch
):
    import importlib.metadata

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", missing)
    targets = _targets(
        tmp_path,
        {
            "user": "from starlette.testclient import TestClient\ndef use(app):\n    client=TestClient(app)\n    client.get('/')\n"
        },
        v2=True,
    )
    assert not any(t.endswith(".get") for t in targets)


def test_only_httpx_source_is_never_retargeted_to_httpx2(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "import httpx\nclass Client(httpx.Client): pass\ndef use(client: Client): client.get('/')\n"
        },
        v2=True,
    )
    assert "extsym:httpx.Client.get" in targets
    assert "extsym:httpx2.Client.get" not in targets


@pytest.mark.parametrize(
    "source",
    [
        "import httpx\nhttpx=unknown\nclass TestClient(httpx.Client):pass\n",
        "import httpx\nclass TestClient(unknown.Base,httpx.Client):pass\n",
        "class TestClient(unknown.Base):pass\n",
        "import httpx\nclass TestClient(httpx.Client):\n    @property\n    def get(self):return 1\n",
    ],
)
def test_dependency_source_without_a_stable_single_base_does_not_claim_members(
    tmp_path, monkeypatch, source
):
    import importlib.metadata

    root = tmp_path / "installed"
    (root / "starlette").mkdir(parents=True)
    (root / "starlette/testclient.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version="fixture", locate_file=lambda path: root / path
        ),
    )
    targets = _targets(
        tmp_path,
        {
            "user": "from starlette.testclient import TestClient\ndef use(app):\n    client=TestClient(app)\n    client.get('/')\n"
        },
        v2=True,
    )
    assert not any(t.endswith(".get") for t in targets)


def test_multiple_factory_bindings_use_the_callsite_value_even_when_name_masks_a_global(
    tmp_path,
):
    source = "from typing import Callable\nimport httpx2\nclass Client(httpx2.Client):pass\nclient=unknown\ndef use(factory: Callable[...,Client]):\n    client=factory()\n    client.get('first')\n    client=factory()\n    client.get('second')\n    client=factory()\n    client.get('third')\n"
    _targets(tmp_path, {"user": source}, v2=True)
    edges = [
        e
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use" and e.target == "extsym:httpx2.Client.get"
    ]
    assert (
        edges
        and sum(
            len(e.properties.get("callsites") or [e.properties.get("callsite")])
            for e in edges
        )
        == 3
    )


def test_unknown_later_factory_binding_does_not_reuse_an_earlier_value(tmp_path):
    source = "from typing import Callable\nimport httpx2\nclass Client(httpx2.Client):pass\nclient=unknown\ndef use(factory: Callable[...,Client],other):\n    client=factory()\n    client.get('first')\n    client=other\n    client.get('second')\n"
    _targets(tmp_path, {"user": source}, v2=True)
    edges = [
        e
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use" and e.target == "extsym:httpx2.Client.get"
    ]
    assert (
        edges
        and sum(
            len(e.properties.get("callsites") or [e.properties.get("callsite")])
            for e in edges
        )
        == 1
    )


def test_two_star_branches_with_the_same_reexport_have_one_definition(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "a": "def helper():return 1\n",
            "b": "from pkg.a import helper\n",
            "user": "if flag:\n    from pkg.a import *\nelse:\n    from pkg.b import *\ndef use():helper()\n",
        },
        v2=True,
    )
    assert "fn:pkg.a.helper" in targets


def test_two_star_branches_with_distinct_definitions_are_not_picked(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "a": "def helper():return 1\n",
            "b": "def helper():return 2\n",
            "user": "if flag:\n    from pkg.a import *\nelse:\n    from pkg.b import *\ndef use():helper()\n",
        },
        v2=True,
    )
    assert not {"fn:pkg.a.helper", "fn:pkg.b.helper"} & targets


@pytest.mark.parametrize(
    "available,owner",
    [({"httpx"}, "httpx.Client"), ({"httpx", "httpx2"}, "httpx2.Client")],
)
def test_installed_conditional_client_uses_the_available_primary_or_fallback(
    tmp_path, monkeypatch, available, owner
):
    import importlib.metadata

    root = tmp_path / "installed"
    (root / "starlette").mkdir(parents=True)
    (root / "starlette/testclient.py").write_text(
        PREFERRED.replace("class Client", "class TestClient").split("def use")[0],
        encoding="utf-8",
    )

    def distribution(name):
        if name in {"httpx", "httpx2"} and name not in available:
            raise importlib.metadata.PackageNotFoundError(name)
        return SimpleNamespace(version="fixture", locate_file=lambda path: root / path)

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    targets = _targets(
        tmp_path,
        {
            "user": "from starlette.testclient import TestClient\ndef use(app):\n    client=TestClient(app)\n    client.get('/')\n"
        },
        v2=True,
    )
    assert "extsym:" + owner + ".get" in targets
