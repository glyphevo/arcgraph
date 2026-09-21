"""Union annotations must not choose a sole receiver by member order."""

from pathlib import Path
from typing import Any

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.contracts import FrontendGraphFragment
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer
from arcgraph.pipeline.reindexer import ArcGraphReindexer

HEADER = """from typing import Any, Union, Optional, Annotated
class Config:
    def fetch(self): return self
    def get(self, key): return self
class Other:
    def fetch(self): return self
    def get(self, key): return self
"""


def analyze(tmp_path: Path, body: str) -> FrontendGraphFragment:
    (tmp_path / "sample.py").write_text(HEADER + body)
    return PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )


@pytest.mark.parametrize(
    "annotation",
    [
        "Config | Other",
        "Other | Config",
        "Union[Config, Other]",
        "Config | Other | None",
        "None | Other | Config",
        "Optional[Union[Other, Config]]",
        'Annotated[Config | Other, "metadata"]',
        "Config | Missing",
        "Missing | Config",
        "Config | Any",
        "Any | Config",
        "Config | dict[str, Any]",
        "list[Config] | list[Other]",
    ],
)
def test_union_does_not_publish_one_member_as_receiver(
    tmp_path: Path, annotation: str
) -> None:
    graph = analyze(
        tmp_path, f'def use(config: {annotation}):\n    return config.get("key")\n'
    )
    node = next(n for n in graph.nodes if n.id == "fn:sample.use")
    ref = next(t for t in node.properties["type_refs"] if t["name"] == "config")
    assert "type_id" not in ref
    calls = [e for e in graph.edges if e.source == node.id]
    assert len(calls) == 1
    assert calls[0].resolution.status == "unresolved"
    assert (
        calls[0].properties["callsite"]["semantic_reason"] == "ambiguous_union_receiver"
    )
    diagnostic = next(n for n in graph.nodes if n.id == calls[0].target)
    assert len(diagnostic.properties["receiver_type_options"]) >= 2


@pytest.mark.parametrize(
    "annotation",
    [
        "Config | None",
        "None | Config",
        "Optional[Config]",
        "Union[Config, Config]",
        'Annotated[Config | None, "metadata"]',
    ],
)
def test_single_non_none_receiver_is_preserved(tmp_path: Path, annotation: str) -> None:
    graph = analyze(
        tmp_path, f"def use(config: {annotation}):\n    return config.fetch()\n"
    )
    calls = [e for e in graph.edges if e.source == "fn:sample.use"]
    assert len(calls) == 1
    assert calls[0].target == "method:sample.Config.fetch"
    assert calls[0].resolution.status == "resolved"


@pytest.mark.parametrize(
    "body",
    [
        "def use(config: Config | Other):\n    alias = config\n    return alias.fetch()\n",
        "def use(config: Config | Other):\n    return config.fetch().fetch()\n",
        "def provider() -> Config | Other: ...\ndef use():\n    return provider().fetch()\n",
        "def provider() -> Config | Other: ...\ndef use():\n    alias = provider()\n    return alias.fetch()\n",
        "def use(config: list[Config | Other]):\n    return config[0].fetch()\n",
        "class Holder:\n    config: Config | Other\ndef use(holder: Holder):\n    return holder.config.fetch()\n",
        "class Holder:\n    @property\n    def config(self) -> Config | Other: ...\ndef use(holder: Holder):\n    return holder.config.fetch()\n",
    ],
)
def test_union_survives_receiver_propagation(tmp_path: Path, body: str) -> None:
    graph = analyze(tmp_path, body)
    calls = [
        e
        for e in graph.edges
        if e.source == "fn:sample.use"
        and e.properties["callsite"].get("attribute") == "fetch"
    ]
    assert calls
    assert all(e.resolution.status == "unresolved" for e in calls)


@pytest.mark.parametrize(
    "body",
    [
        "def use(config: Config | None):\n    config = unknown()\n    return config.fetch()\n",
        "def use():\n    config.fetch()\n    config: Config | None = None\n",
        "def use(config: Config | None):\n    del config\n    return config.fetch()\n",
        "def use(config: Config | None):\n    config = unknown()\n    alias = config\n    return alias.fetch()\n",
    ],
)
def test_nullable_receiver_still_requires_stable_available_binding(
    tmp_path: Path, body: str
) -> None:
    graph = analyze(tmp_path, body)
    assert not [
        e
        for e in graph.edges
        if e.source == "fn:sample.use" and e.target == "method:sample.Config.fetch"
    ]


@pytest.mark.parametrize(
    "imports, annotation, expected",
    [
        ("from typing import Union as U\n", "U[Config, Other]", False),
        ("from typing import Optional as O\n", "O[Config]", True),
        ("Optional = unknown()\n", "Optional[Config]", False),
        ("from typing import Optional as O\nO = unknown()\n", "O[Config]", False),
        ("import typing as t\n", "t.Union[Config, Other]", False),
        ("from sample import Config as C\n", "C | None", True),
        ("from external import Config as C\n", "C | None", False),
        ("C = unknown()\n", "C | None", False),
        ("from sample import Config as C\nC = unknown()\n", "C | None", False),
    ],
)
def test_typing_and_class_alias_identity(
    tmp_path: Path, imports: str, annotation: str, expected: bool
) -> None:
    graph = analyze(
        tmp_path,
        imports + f"def use(config: {annotation}):\n    return config.fetch()\n",
    )
    assert (
        any(
            e.source == "fn:sample.use" and e.target == "method:sample.Config.fetch"
            for e in graph.edges
        )
        == expected
    )


def test_union_provider_change_refreshes_consumer(tmp_path: Path) -> None:
    repo: Path = tmp_path / "repo"
    repo.mkdir()
    model: Path = repo / "model.py"
    consumer: Path = repo / "consumer.py"
    model.write_text("class Config:\n    def fetch(self): ...\n")
    consumer.write_text(
        "from model import Config, Other\ndef use(config: Config | Other):\n    return config.fetch()\n"
    )
    output: Path = tmp_path / "index"
    roots = [SourceRoot(".")]
    ArcGraphIndexer(repo, output, roots).build()

    def state(folder: Path) -> dict[str, Any]:
        reader = GraphStoreReader.from_current(folder)
        node = next(n for n in reader.read_nodes() if n.id == "fn:consumer.use")
        refs = [t for t in node.properties["type_refs"] if t["name"] == "config"]
        return {
            "refs": refs,
            "calls": sorted(
                (e.target, e.resolution.status)
                for e in reader.read_edges()
                if e.source == node.id
            ),
        }

    for addition in ("class Other:\n    def fetch(self): ...\n", ""):
        model.write_text("class Config:\n    def fetch(self): ...\n" + addition)
        ArcGraphReindexer(repo, output, roots).reindex_changed()
        full: Path = tmp_path / ("full-added" if addition else "full-removed")
        ArcGraphIndexer(repo, full, roots).build()
        assert state(output) == state(full)
        assert all(status == "unresolved" for _, status in state(output)["calls"])


def test_unresolved_query_exposes_union_reason(tmp_path: Path) -> None:
    from arcgraph.core.query_engine import QueryEngine

    repo: Path = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text(
        HEADER + 'def use(config: Config | Other):\n    return config.get("key")\n'
    )
    index: Path = tmp_path / "index"
    ArcGraphIndexer(repo, index, [SourceRoot(".")]).build()
    result: dict[str, Any] = QueryEngine(index).unresolved("fn:sample.use")
    records = result["unresolved"]
    assert len(records) == 1
    assert records[0]["properties"]["semantic_reason"] == "ambiguous_union_receiver"
    assert records[0]["properties"]["failed_strategy"] == "union_receiver"
    assert result["classification"]["category_counts"]["true_dynamic_call"] == 1
