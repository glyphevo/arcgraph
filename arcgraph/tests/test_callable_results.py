"""Callable annotations describe invocation results, not name-based guesses."""

from pathlib import Path

import pytest

from arcgraph.tests.test_generator_and_dynamic_exports import _targets
from arcgraph.core.graph_store import GraphStoreReader

CLIENT = """from httpx2 import Client
class TestClient(Client):
    def websocket_connect(self): pass
class OtherClient:
    def websocket_connect(self): pass
"""


@pytest.mark.parametrize(
    "annotation,imports",
    [
        ("Callable[..., TestClient]", "from typing import Callable"),
        ("Callable[[int, str], TestClient]", "from typing import Callable"),
        ("typing.Callable[..., TestClient]", "import typing"),
        ("collections.abc.Callable[..., TestClient]", "import collections.abc"),
        ("Factory", "from typing import Callable\nFactory = Callable[..., TestClient]"),
    ],
)
def test_callable_parameter_result(tmp_path: Path, annotation: str, imports: str):
    targets = _targets(
        tmp_path,
        {
            "client": CLIENT,
            "user": "from pkg.client import TestClient\n"
            + imports
            + f"\ndef use(factory: {annotation}):\n    client = factory(1, 'x')\n    client.websocket_connect()\n    client.get('/')\n",
        },
        v2=True,
    )
    assert "method:pkg.client.TestClient.websocket_connect" in targets
    assert "extsym:httpx2.Client.get" not in targets


@pytest.mark.parametrize("kind", ["variable", "factory", "imported_alias", "inline"])
def test_callable_annotation_sources(tmp_path: Path, kind: str):
    prefix = "from typing import Callable\nfrom pkg.client import TestClient\n"
    body = {
        "variable": "def use():\n    factory: Callable[..., TestClient] = supplier\n    client = factory()\n    client.websocket_connect()\n",
        "factory": "def make() -> Callable[..., TestClient]: return supplier\ndef use():\n    factory = make()\n    client = factory()\n    client.websocket_connect()\n",
        "imported_alias": "from pkg.contract import Factory\ndef use(factory: Factory):\n    client = factory()\n    client.websocket_connect()\n",
        "inline": "def make() -> Callable[..., TestClient]: return supplier\ndef use():\n    client = make()()\n    client.websocket_connect()\n",
    }[kind]
    targets = _targets(
        tmp_path,
        {
            "client": CLIENT,
            "contract": prefix + "Factory = Callable[..., TestClient]\n",
            "user": prefix + body,
        },
        v2=True,
    )
    assert "method:pkg.client.TestClient.websocket_connect" in targets


@pytest.mark.parametrize("typing_only", [False, True])
def test_protocol_call_annotation_contract(tmp_path: Path, typing_only: bool):
    protocol = "class Factory(Protocol):\n    def __call__(self, app: object) -> TestClient: ...\n"
    if typing_only:
        protocol = (
            "if TYPE_CHECKING:\n"
            + "".join("    " + line for line in protocol.splitlines(True))
            + "else:\n    class Factory: pass\n"
        )
    targets = _targets(
        tmp_path,
        {
            "client": CLIENT,
            "contract": "from typing import Protocol, TYPE_CHECKING\nfrom pkg.client import TestClient\n"
            + protocol,
            "user": "from pkg.contract import Factory\ndef use(factory: Factory):\n    client = factory(None)\n    client.websocket_connect()\n    client.get('/')\n",
        },
        v2=True,
    )
    assert "method:pkg.client.TestClient.websocket_connect" in targets
    assert "extsym:httpx2.Client.get" not in targets


@pytest.mark.parametrize(
    "prefix,annotation,extra",
    [
        ("from typing import Callable, Any", "Callable[..., Any]", ""),
        (
            "from typing import Callable, TypeVar\nT = TypeVar('T')",
            "Callable[..., T]",
            "",
        ),
        (
            "from typing import Callable, ParamSpec\nP = ParamSpec('P')",
            "Callable[P, TestClient]",
            "",
        ),
        (
            "from typing import Callable, TypeVar\nT = TypeVar('T')",
            "Callable[[T], TestClient]",
            "",
        ),
        (
            "from typing import Callable\nCallable = object",
            "Callable[..., TestClient]",
            "",
        ),
        (
            "from typing import Callable",
            "Callable[..., TestClient]",
            "    factory = other\n",
        ),
        (
            "from typing import Callable",
            "Callable[..., TestClient]",
            "    if flag: factory = other\n",
        ),
        ("from typing import Callable", "Callable[..., TestClient] | None", ""),
        (
            "from typing import Callable\nif flag: Alias = Callable[..., TestClient]",
            "Alias",
            "",
        ),
    ],
)
def test_uncertain_callable_is_not_result_evidence(
    tmp_path: Path, prefix: str, annotation: str, extra: str
):
    targets = _targets(
        tmp_path,
        {
            "client": CLIENT,
            "user": "from pkg.client import TestClient\n"
            + prefix
            + f"\ndef use(factory: {annotation}):\n"
            + extra
            + "    client = factory()\n    client.websocket_connect()\n",
        },
        v2=True,
    )
    assert "method:pkg.client.TestClient.websocket_connect" not in targets
    node = next(
        n
        for n in GraphStoreReader.from_current(tmp_path / "out").read_nodes()
        if n.id == "fn:pkg.user.use"
    )
    assert not any(
        r.get("name") == "client" and r.get("strategy") == "callable_result_annotation"
        for r in node.properties.get("type_refs", [])
    )


@pytest.mark.parametrize(
    "protocol",
    [
        "class Factory(Protocol):\n    @overload\n    def __call__(self, x: int) -> TestClient: ...\n    @overload\n    def __call__(self, x: str) -> TestClient: ...\n",
        "class Factory(Protocol):\n    def __call__(self) -> TestClient: ...\n    def __call__(self) -> TestClient: ...\n",
        "class Factory(Protocol):\n    def __call__(self) -> TestClient:\n        yield 1\n",
        "class Factory(Protocol[T]):\n    def __call__(self) -> TestClient: ...\n",
        "class Factory:\n    def __call__(self) -> TestClient: ...\n",
        "if flag:\n    class Factory(Protocol):\n        def __call__(self) -> TestClient: ...\n",
    ],
)
def test_ambiguous_protocol_is_not_result_evidence(tmp_path: Path, protocol: str):
    targets = _targets(
        tmp_path,
        {
            "client": CLIENT,
            "user": "from typing import Protocol, TypeVar, overload\nfrom pkg.client import TestClient\nT = TypeVar('T')\n"
            + protocol
            + "\ndef use(factory: Factory):\n    client = factory()\n    client.websocket_connect()\n",
        },
        v2=True,
    )
    assert "method:pkg.client.TestClient.websocket_connect" not in targets
    node = next(
        n
        for n in GraphStoreReader.from_current(tmp_path / "out").read_nodes()
        if n.id == "fn:pkg.user.use"
    )
    assert not any(
        r.get("name") == "client" and r.get("strategy") == "callable_result_annotation"
        for r in node.properties.get("type_refs", [])
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "from typing import Callable\nfrom pkg.client import TestClient\nAlias = Callable[..., TestClient]\nAlias = object\n",
        "from typing import Callable\nfrom pkg.client import TestClient\nAlias = Callable[..., TestClient]\ndef change():\n    global Alias\n    Alias = object\n",
        "from typing import Callable\nfrom pkg.client import TestClient\nimport typing\ntyping = object\nAlias = typing.Callable[..., TestClient]\n",
        "from typing import Protocol, TYPE_CHECKING\nfrom pkg.client import TestClient\nTYPE_CHECKING = True\nif TYPE_CHECKING:\n    class Alias(Protocol):\n        def __call__(self) -> TestClient: ...\nelse:\n    class Alias: pass\n",
    ],
)
def test_callable_contract_origin_must_be_stable(tmp_path: Path, prefix: str):
    targets = _targets(
        tmp_path,
        {
            "client": CLIENT,
            "user": prefix
            + "def use(factory: Alias):\n    client = factory()\n    client.websocket_connect()\n",
        },
        v2=True,
    )
    assert "method:pkg.client.TestClient.websocket_connect" not in targets
    node = next(
        n
        for n in GraphStoreReader.from_current(tmp_path / "out").read_nodes()
        if n.id == "fn:pkg.user.use"
    )
    assert not any(
        r.get("name") == "client" and r.get("strategy") == "callable_result_annotation"
        for r in node.properties.get("type_refs", [])
    )


def test_callable_forward_alias_cycle_is_unknown(tmp_path: Path):
    targets = _targets(
        tmp_path,
        {
            "client": CLIENT,
            "user": "A = B\nB = A\ndef use(factory: A):\n    client = factory()\n    client.websocket_connect()\n",
        },
        v2=True,
    )
    assert "method:pkg.client.TestClient.websocket_connect" not in targets
    node = next(
        n
        for n in GraphStoreReader.from_current(tmp_path / "out").read_nodes()
        if n.id == "fn:pkg.user.use"
    )
    assert not any(
        r.get("name") == "client" and r.get("strategy") == "callable_result_annotation"
        for r in node.properties.get("type_refs", [])
    )


@pytest.mark.parametrize(
    "contract",
    [
        "import typing\ntyping.Callable = object\nAlias = typing.Callable[..., TestClient]\n",
        "import typing\ndef change():\n    typing.Callable = object\nAlias = typing.Callable[..., TestClient]\n",
        "from typing import Protocol, TypeVar\nT = TypeVar('T')\nclass Alias(Protocol):\n    def __call__(self, arg: T) -> TestClient: ...\n",
        "from typing import TYPE_CHECKING, Protocol\nif TYPE_CHECKING:\n    class Alias(Protocol):\n        def __call__(self) -> TestClient: ...\nelse:\n    class Alias: pass\nclass Alias: pass\n",
    ],
)
def test_modified_or_generic_callable_contract_is_unknown(
    tmp_path: Path, contract: str
):
    targets = _targets(
        tmp_path,
        {
            "client": CLIENT,
            "user": "from pkg.client import TestClient\n"
            + contract
            + "def use(factory: Alias):\n    client = factory()\n    client.websocket_connect()\n",
        },
        v2=True,
    )
    assert "method:pkg.client.TestClient.websocket_connect" not in targets
    node = next(
        n
        for n in GraphStoreReader.from_current(tmp_path / "out").read_nodes()
        if n.id == "fn:pkg.user.use"
    )
    assert not any(
        r.get("name") == "client" and r.get("strategy") == "callable_result_annotation"
        for r in node.properties.get("type_refs", [])
    )


def test_callable_builtin_result(tmp_path: Path):
    targets = _targets(
        tmp_path,
        {
            "user": "from typing import Callable\ndef use(factory: Callable[[], str]):\n    value = factory()\n    value.strip()\n"
        },
        v2=True,
    )
    assert "extsym:builtins.str.strip" in targets
    node = next(
        n
        for n in GraphStoreReader.from_current(tmp_path / "out").read_nodes()
        if n.id == "fn:pkg.user.use"
    )
    assert any(
        r.get("name") == "value"
        and r.get("strategy") == "callable_result_annotation"
        and r.get("type_id") == "builtin:str"
        for r in node.properties.get("type_refs", [])
    )


def test_callable_alias_annotation_survives_sync(tmp_path: Path):
    from arcgraph.pipeline.reindexer import ArcGraphReindexer
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.pipeline.indexer import ArcGraphIndexer

    modules = {
        "client": CLIENT,
        "contract": "from typing import Callable\nfrom pkg.client import TestClient\nFactory = Callable[..., TestClient]\n",
        "user": "from pkg.contract import Factory\ndef use(factory: Factory):\n    client = factory()\n    client.websocket_connect()\n",
    }
    assert "method:pkg.client.TestClient.websocket_connect" in _targets(
        tmp_path, modules, v2=True
    )
    repo = tmp_path / "repo"
    provider = repo / "src/pkg/contract.py"
    provider.write_text(
        "from typing import Callable, Any\nFactory = Callable[..., Any]\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(
        repo_root=repo, output_dir=tmp_path / "out", source_roots=[SourceRoot("src")]
    ).reindex_changed()
    ArcGraphIndexer(
        repo_root=repo,
        output_dir=tmp_path / "full",
        source_roots=[SourceRoot("src")],
        enable_v2_call_resolution=True,
    ).build()

    def edges(directory):
        return {
            (e.source, e.target, e.kind)
            for e in GraphStoreReader.from_current(directory).read_edges()
            if e.source == "fn:pkg.user.use"
        }

    assert edges(tmp_path / "out") == edges(tmp_path / "full")
    assert not any(
        target == "method:pkg.client.TestClient.websocket_connect"
        for _, target, _ in edges(tmp_path / "out")
    )


def test_callable_contract_result_cache(tmp_path: Path, monkeypatch):
    from collections import Counter
    from arcgraph.analyzers.callable_types import CallableContracts

    counts = Counter()
    original = CallableContracts._result

    def counted(self, expression, source, seen):
        if not seen:
            counts[(id(self), source.path, source.id, expression)] += 1
        return original(self, expression, source, seen)

    monkeypatch.setattr(CallableContracts, "_result", counted)
    modules = {
        "client": CLIENT,
        "user": "from pkg.client import TestClient\nfrom typing import Callable\ndef make() -> Callable[..., TestClient]: return supplier\ndef use():\n    factory = make()\n    factory().websocket_connect()\n",
    }
    modules.update(
        {f"other{i}": f"def other{i}() -> int: return {i}\n" for i in range(5)}
    )
    _targets(tmp_path, modules, v2=True)
    assert counts and max(counts.values()) == 1


@pytest.mark.parametrize("kind", ["module", "captured"])
def test_callable_variable_annotation_in_outer_scope(tmp_path: Path, kind: str):
    prefix = "from typing import Callable\nfrom pkg.client import TestClient\n"
    body = "factory: Callable[..., TestClient] = supplier\ndef use():\n    client = factory()\n    client.websocket_connect()\n"
    if kind == "captured":
        body = "def outer():\n    factory: Callable[..., TestClient] = supplier\n    def use():\n        client = factory()\n        client.websocket_connect()\n    return use\n"
    _targets(tmp_path, {"client": CLIENT, "user": prefix + body}, v2=True)
    scope = "fn:pkg.user.use" if kind == "module" else "fn:pkg.user.outer.use"
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    assert any(
        e.source == scope
        and e.target == "method:pkg.client.TestClient.websocket_connect"
        for e in edges
    )


@pytest.mark.parametrize("quoted", [False, True])
def test_callable_protocol_keeps_its_type_identity(tmp_path: Path, quoted: bool):
    source = "from typing import Protocol\nfrom pkg.client import TestClient\nclass Factory(Protocol):\n    def __call__(self) -> TestClient: ...\n    def options(self): ...\nclass Other:\n    def options(self): pass\ndef use(factory: Factory):\n    factory.options()\n    client = factory()\n    client.websocket_connect()\n"
    if quoted:
        source = source.replace("factory: Factory", "factory: 'Factory'")
    targets = _targets(tmp_path, {"client": CLIENT, "user": source}, v2=True)
    assert "method:pkg.user.Factory.options" in targets
    assert "method:pkg.client.TestClient.websocket_connect" in targets
    node = next(
        n
        for n in GraphStoreReader.from_current(tmp_path / "out").read_nodes()
        if n.id == "fn:pkg.user.use"
    )
    assert any(
        r.get("name") == "factory"
        and r.get("type_id") == "class:pkg.user.Factory"
        and r.get("callable_result")
        for r in node.properties.get("type_refs", [])
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "import typing\ntyping.Protocol = object\nclass Alias(typing.Protocol):\n    def __call__(self) -> TestClient: ...\n",
        "import typing\nfrom typing import Protocol\ntyping.TYPE_CHECKING = True\nif typing.TYPE_CHECKING:\n    class Alias(Protocol):\n        def __call__(self) -> TestClient: ...\nelse:\n    class Alias: pass\n",
    ],
)
def test_modified_protocol_module_member_is_not_a_contract(tmp_path: Path, prefix: str):
    targets = _targets(
        tmp_path,
        {
            "client": CLIENT,
            "user": "from pkg.client import TestClient\n"
            + prefix
            + "def use(factory: Alias):\n    client = factory()\n    client.websocket_connect()\n",
        },
        v2=True,
    )
    assert "method:pkg.client.TestClient.websocket_connect" not in targets


def test_callable_result_uses_exact_provider_identity(tmp_path: Path):
    targets = _targets(
        tmp_path,
        {
            "client": CLIENT,
            "user": "from typing import Callable\nfrom external import TestClient as Foreign\ndef use(factory: Callable[..., Foreign]):\n    client = factory()\n    client.websocket_connect()\n",
        },
        v2=True,
    )
    assert "method:pkg.client.TestClient.websocket_connect" not in targets
    node = next(
        n
        for n in GraphStoreReader.from_current(tmp_path / "out").read_nodes()
        if n.id == "fn:pkg.user.use"
    )
    assert any(
        r.get("name") == "client" and r.get("type_id") == "extsym:external.TestClient"
        for r in node.properties.get("type_refs", [])
    )
