"""Typing-only imports establish annotation identity, not executable bindings."""

from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer
from arcgraph.pipeline.reindexer import ArcGraphReindexer

PROVIDER = "class Cache:\n def get(self): pass\n def get_batch(self): pass\n"
HEADER = "from __future__ import annotations\nfrom typing import TYPE_CHECKING, Optional, Union\n"
IMPORT = "if TYPE_CHECKING:\n from provider import Cache\n"


def consumer(annotation="Cache | None", imports=IMPORT):
    return HEADER + imports + f"""class Service:
 def __init__(self, cache: {annotation} = None):
  from provider import Cache as RuntimeCache
  if cache is not None:
   self._cache = cache
  else:
   self._cache = RuntimeCache()
 def run(self): return self._cache.get()
 def batch(self): return self._cache.get_batch()
"""


def analyze(tmp_path, source, provider=PROVIDER):
    (tmp_path / "provider.py").write_text(provider)
    (tmp_path / "other.py").write_text(PROVIDER)
    (tmp_path / "consumer.py").write_text(source)
    return PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )


def calls(graph):
    return {
        (e.source, e.target)
        for e in graph.edges
        if e.source in {"method:consumer.Service.run", "method:consumer.Service.batch"}
        and e.resolution.status == "resolved"
        and e.kind == "calls"
    }


@pytest.mark.parametrize(
    "annotation,imports",
    [
        ("Cache | None", IMPORT),
        ("Optional[Cache]", IMPORT),
        ("Union[None, Cache]", IMPORT),
        ("'Cache | None'", IMPORT),
        (
            "Cache | None",
            "import typing\nif typing.TYPE_CHECKING:\n from provider import Cache\n",
        ),
        ("Public | None", "if TYPE_CHECKING:\n from provider import Cache as Public\n"),
        ("provider.Cache | None", "if TYPE_CHECKING:\n import provider\n"),
    ],
)
def test_typing_imported_optional_receiver_keeps_provider_identity(
    tmp_path, annotation, imports
):
    graph = analyze(tmp_path, consumer(annotation, imports))
    assert calls(graph) == {
        ("method:consumer.Service.run", "method:provider.Cache.get"),
        ("method:consumer.Service.batch", "method:provider.Cache.get_batch"),
    }


@pytest.mark.parametrize(
    "annotation,imports,provider",
    [
        ("Cache | Unknown | None", IMPORT, PROVIDER),
        ("Cache | int | None", IMPORT, PROVIDER),
        (
            "Cache | Other | None",
            IMPORT + "from other import Cache as Other\n",
            PROVIDER,
        ),
        ("Cache | None", "if flag:\n from provider import Cache\n", PROVIDER),
        ("Cache | None", IMPORT + "Cache = unknown()\n", PROVIDER),
        ("Cache | None", "TYPE_CHECKING = True\n" + IMPORT, PROVIDER),
        (
            "Cache | None",
            "import custom\nif custom.TYPE_CHECKING:\n from provider import Cache\n",
            PROVIDER,
        ),
        (
            "Cache | None",
            "if TYPE_CHECKING:\n if flag:\n  from provider import Cache\n",
            PROVIDER,
        ),
        ("Cache | None", IMPORT + "else:\n from other import Cache\n", PROVIDER),
        ("Cache | None", IMPORT, "class Missing: pass\n"),
    ],
)
def test_unknown_mixed_rebound_or_conditional_imports_stay_unresolved(
    tmp_path, annotation, imports, provider
):
    assert not calls(analyze(tmp_path, consumer(annotation, imports), provider))


def test_typing_import_does_not_create_runtime_constructor_edge(tmp_path):
    graph = analyze(tmp_path, HEADER + IMPORT + "def create(): return Cache()\n")
    assert not any(
        e.source == "fn:consumer.create"
        and e.target == "class:provider.Cache"
        and e.kind == "constructs"
        for e in graph.edges
    )


def snapshot(index):
    store = GraphStoreReader.from_current(index)
    with store.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT source,target,kind,resolution_json FROM edges WHERE kind = 'calls' AND source IN "
                "('method:consumer.Service.run','method:consumer.Service.batch') ORDER BY source,target,kind"
            )
        ]


@pytest.mark.parametrize(
    "annotation", ["Cache | None", "Optional[Cache]", "'Cache | None'"]
)
def test_typing_receiver_incremental_tracks_reexport_and_provider_changes(
    tmp_path: Path, annotation
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "consumer.py").write_text(consumer(annotation))
    (repo / "v1.py").write_text(PROVIDER)
    (repo / "v2.py").write_text(PROVIDER)
    (repo / "provider.py").write_text("from v1 import Cache\n")
    index = tmp_path / "incremental"
    roots = [SourceRoot(".")]
    ArcGraphIndexer(repo, index, roots).build()
    edits = [
        ("unrelated.py", "# comment\n", "v1"),
        ("provider.py", "from v2 import Cache\n", "v2"),
        ("v2.py", "class Cache: pass\n", None),
        ("v2.py", PROVIDER, "v2"),
        ("provider.py", "class Cache(\n", None),
        ("provider.py", "from v1 import Cache\n", "v1"),
        ("unrelated.py", "# another comment\n", "v1"),
    ]
    for step, (name, source, target) in enumerate(edits):
        (repo / name).write_text(source)
        ArcGraphReindexer(repo, index, roots).reindex_changed()
        full = tmp_path / f"full-{step}"
        ArcGraphIndexer(repo, full, roots).build()
        actual = snapshot(index)
        assert actual == snapshot(full), (step, actual, snapshot(full))
        resolved = {row[1] for row in actual if row[1].startswith("method:")}
        assert resolved == (
            {f"method:{target}.Cache.get", f"method:{target}.Cache.get_batch"}
            if target
            else set()
        )


@pytest.mark.parametrize(
    "old,new",
    [
        (
            "from provider import Cache as RuntimeCache",
            "from other import Cache as RuntimeCache",
        ),
        ("if cache is not None:", "if cache is None:"),
        ("if cache is not None:", "if flag:"),
        ("self._cache = RuntimeCache()", "self._cache = unknown()"),
        ("self._cache = RuntimeCache()", "self._cache = RuntimeCache.factory()"),
        ("self._cache = RuntimeCache()", "self._cache = None"),
        (" def run(self):", "  self._cache = unknown()\n def run(self):"),
        (
            " def run(self):",
            " def replace(self, value): self._cache = value\n def run(self):",
        ),
        ("  if cache is not None:", "  cache = unknown()\n  if cache is not None:"),
        ("  if cache is not None:", "  self = unknown()\n  if cache is not None:"),
        ("= None):", "= unknown()):"),
    ],
)
def test_recovery_does_not_claim_a_mixed_or_mutated_instance_attribute(
    tmp_path, old, new
):
    graph = analyze(tmp_path, consumer().replace(old, new))
    assert not calls(graph)
