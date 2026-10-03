"""Regression coverage for review F1/F3, including their conservative guards."""

from pathlib import Path
import pytest
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer


def graph(tmp_path, sources):
    for name, source in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )


@pytest.mark.parametrize(
    "annotation,imports",
    [
        ("Client", "from pkg import Client"),
        ("'Client'", "from pkg import Client"),
        ("pkg.Client", "import pkg"),
        ("Public", "from pkg import Client as Public"),
        ("list[Client]", "from pkg import Client"),
    ],
)
def test_reexported_class(tmp_path: Path, annotation, imports):
    call = "[x.send() for x in c]" if annotation.startswith("list") else "c.send()"
    result = graph(
        tmp_path,
        {
            "pkg/client.py": "class Client:\n    def send(self): pass\n",
            "pkg/api.py": "from .client import Client as Public\n",
            "pkg/__init__.py": "from .api import Public as Client\n",
            "consumer.py": f"{imports}\ndef use(c: {annotation}):\n    return {call}\n",
            "other.py": "class Client:\n    def send(self): pass\n",
        },
    )
    assert {
        e.target
        for e in result.edges
        if e.source == "fn:consumer.use" and e.kind == "calls"
    } == {"method:pkg.client.Client.send"}


@pytest.mark.parametrize(
    "export",
    [
        "from .client import Client\nClient = unknown()\n",
        "if flag:\n    from .client import Client\n",
        "from .api import Client\n",  # cycle
        "class Missing: pass\n",
    ],
)
def test_unstable_export_does_not_select_same_named_class(tmp_path, export):
    result = graph(
        tmp_path,
        {
            "pkg/__init__.py": export,
            "pkg/api.py": "from pkg import Client\n",
            "pkg/client.py": "class Client:\n    def send(self): pass\n",
            "consumer.py": "from pkg import Client\ndef use(c: Client):\n    c.send()\n",
        },
    )
    assert not any(
        e.kind == "calls" and e.source == "fn:consumer.use" for e in result.edges
    )


def test_base_class_reexport(tmp_path):
    result = graph(
        tmp_path,
        {
            "pkg/__init__.py": "from .client import Client\n",
            "pkg/client.py": "class Client:\n    def send(self): pass\n",
            "consumer.py": "from pkg import Client\nclass Child(Client): pass\ndef use(c: Child):\n    c.send()\n",
        },
    )
    assert any(
        e.source == "fn:consumer.use" and e.target == "method:pkg.client.Client.send"
        for e in result.edges
    )


@pytest.mark.parametrize(
    "decorator,imports",
    [
        ("overload", "from typing import overload"),
        ("typing.overload", "import typing"),
        ("ov", "from typing import overload as ov"),
    ],
)
def test_overloads_resolve_to_method(tmp_path, decorator, imports):
    result = graph(
        tmp_path,
        {
            "svc.py": f"{imports}\nclass Store:\n    @{decorator}\n    def get(self, x: int) -> int: ...\n    @{decorator}\n    def get(self, x: str) -> str: ...\n    def get(self, x): return x\ndef use(s: Store):\n    s.get(1)\n"
        },
    )
    assert any(
        e.source == "fn:svc.use"
        and e.target == "method:svc.Store.get"
        and e.resolution.status == "resolved"
        for e in result.edges
    )


@pytest.mark.parametrize(
    "body",
    [
        "    def get(self): pass\n    get = unknown()\n",
        "    def get(self): pass\n    @property\n    def get(self): return unknown()\n",
        "    if flag:\n        def get(self): pass\n    else:\n        def get(self): pass\n",
    ],
)
def test_replacement_and_conditional_methods_stay_unresolved(tmp_path, body):
    result = graph(
        tmp_path,
        {"svc.py": "class Store:\n" + body + "def use(s: Store):\n    s.get()\n"},
    )
    assert not any(
        e.source == "fn:svc.use"
        and e.target == "method:svc.Store.get"
        and e.resolution.status == "resolved"
        for e in result.edges
    )


@pytest.mark.parametrize("annotation", ["Client | None", "list[Client]"])
def test_reexport_with_explicit_all_and_nullable_type(tmp_path, annotation):
    call = "[x.send() for x in c]" if annotation.startswith("list") else "c.send()"
    result = graph(
        tmp_path,
        {
            "pkg/__init__.py": 'from .client import Client\n__all__ = ["Client"]\n',
            "pkg/client.py": "class Client:\n    def send(self): pass\n",
            "consumer.py": f"from pkg import Client\ndef use(c: {annotation}):\n    {call}\n",
        },
    )
    assert any(
        e.source == "fn:consumer.use" and e.target == "method:pkg.client.Client.send"
        for e in result.edges
    )


def test_reexports_remain_correct_across_incremental_provider_edits(tmp_path):
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer
    from arcgraph.core.graph_store import GraphStoreReader

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg/__init__.py").write_text(
        "from .client import Client\n", encoding="utf-8"
    )
    provider = repo / "pkg/client.py"
    source = "class Client:\n    def send(self): pass\n"
    provider.write_text(source, encoding="utf-8")
    (repo / "consumer.py").write_text(
        "from pkg import Client\ndef use(c: Client | None):\n    c.send()\n",
        encoding="utf-8",
    )
    (repo / "other.py").write_text(
        "class Client:\n    def send(self): pass\n", encoding="utf-8"
    )
    index = tmp_path / "index"
    roots = [SourceRoot(".")]
    ArcGraphIndexer(repo, index, roots).build()
    for i, src in enumerate(
        ["class Renamed: pass\n", source, "class Client(", source, None, source]
    ):
        if src is None:
            provider.unlink()
        else:
            provider.write_text(src, encoding="utf-8")
        ArcGraphReindexer(repo, index, roots).reindex_changed()
        full = tmp_path / f"full-{i}"
        ArcGraphIndexer(repo, full, roots).build()

        def snapshot(out):
            r = GraphStoreReader.from_current(out)
            return [
                (e.target, e.resolution.status, e.resolution.strategy)
                for e in r.read_edges()
                if e.source == "fn:consumer.use"
                and e.kind in {"calls", "uses", "dynamic_call"}
            ]

        assert snapshot(index) == snapshot(full)
        assert (
            "method:pkg.client.Client.send" in {t for t, _, _ in snapshot(index)}
        ) == (src == source)
        assert "method:other.Client.send" not in {t for t, _, _ in snapshot(index)}


@pytest.mark.parametrize(
    "imports,body,resolves",
    [
        ("from pkg import Client", "c = Client(); return c.send()", True),
        ("import pkg", "c = pkg.Client(); return c.send()", True),
        ("from pkg import Client as Public", "c = Public(); return c.send()", True),
        ("from pkg import Client", "return Client().send()", False),
    ],
)
@pytest.mark.parametrize("multihop", [False, True])
def test_unannotated_reexport_consumers_follow_export_switches(
    tmp_path, imports, body, resolves, multihop
):
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer
    from arcgraph.core.graph_store import GraphStoreReader

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    roots = [SourceRoot(".")]
    source = "class Client:\n    def send(self): pass\n"
    (repo / "pkg/client.py").write_text(source, encoding="utf-8")
    (repo / "pkg/v2.py").write_text(source, encoding="utf-8")
    export = repo / ("pkg/api.py" if multihop else "pkg/__init__.py")
    if multihop:
        (repo / "pkg/__init__.py").write_text(
            "from .api import Client\n", encoding="utf-8"
        )
    export.write_text("from .client import Client\n", encoding="utf-8")
    (repo / "consumer.py").write_text(
        f"{imports}\ndef run():\n    {body}\n", encoding="utf-8"
    )
    (repo / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")
    inc = tmp_path / "inc"
    ArcGraphIndexer(repo, inc, roots).build()
    edits = [
        (export, "from .v2 import Client\n", "v2"),
        (repo / "unrelated.py", "VALUE = 2\n", "v2"),
        (repo / "pkg/v2.py", "class Client: pass\n", None),
        (repo / "pkg/v2.py", source, "v2"),
        (export, "if flag:\n    from .v2 import Client\n", None),
        (export, "from .client import Client\n", "client"),
        (
            export,
            "from .api import Client\n" if multihop else "from pkg import Client\n",
            None,
        ),
        (export, "from .v2 import Client\n", "v2"),
        (repo / "pkg/v2.py", None, None),
        (repo / "pkg/v2.py", source, "v2"),
        (repo / "pkg/v2.py", "class Client(", None),
        (repo / "pkg/v2.py", source, "v2"),
    ]

    def snapshot(output):
        reader = GraphStoreReader.from_current(output)
        edges = sorted(
            (e.target, e.kind, e.resolution.status, e.resolution.strategy)
            for e in reader.read_edges()
            if e.source == "fn:consumer.run"
            and e.kind in {"calls", "uses", "constructs", "dynamic_call"}
        )
        node = next(n for n in reader.read_nodes() if n.id == "fn:consumer.run")
        assert node.properties.get("imported_type_input") is True
        return edges, node.properties.get("type_refs", [])

    for step, (path, code, target) in enumerate(edits):
        if code is None:
            path.unlink()
        else:
            path.write_text(code, encoding="utf-8")
        ArcGraphReindexer(repo, inc, roots).reindex_changed()
        full = tmp_path / f"full-{step}"
        ArcGraphIndexer(repo, full, roots).build()
        assert snapshot(inc) == snapshot(full), (step, body, multihop)
        methods = {
            t
            for t, _, status, _ in snapshot(inc)[0]
            if status == "resolved" and t.startswith("method:pkg.")
        }
        assert methods == (
            {f"method:pkg.{target}.Client.send"} if target and resolves else set()
        ), (
            step,
            methods,
        )


def test_direct_import_constructor_recovers_missing_provider_method(tmp_path):
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer
    from arcgraph.core.graph_store import GraphStoreReader

    repo = tmp_path / "repo"
    repo.mkdir()
    roots = [SourceRoot(".")]
    (repo / "consumer.py").write_text(
        "from provider import Client\ndef run():\n    c = Client()\n    return c.send()\n",
        encoding="utf-8",
    )
    provider = repo / "provider.py"
    provider.write_text("class Client: pass\n", encoding="utf-8")
    inc = tmp_path / "inc"
    ArcGraphIndexer(repo, inc, roots).build()
    for step, code in enumerate(
        [
            "class Client:\n    def send(self): pass\n",
            "class Client: pass\n",
            "class Client:\n    def send(self): pass\n",
        ]
    ):
        provider.write_text(code, encoding="utf-8")
        ArcGraphReindexer(repo, inc, roots).reindex_changed()
        full = tmp_path / f"full-{step}"
        ArcGraphIndexer(repo, full, roots).build()

        def edges(out):
            return sorted(
                (e.target, e.kind, e.resolution.status, e.resolution.strategy)
                for e in GraphStoreReader.from_current(out).read_edges()
                if e.source == "fn:consumer.run"
            )

        assert edges(inc) == edges(full)
        assert ("method:provider.Client.send" in {e[0] for e in edges(inc)}) == (
            step != 1
        )
