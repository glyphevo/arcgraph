"""A fallback constructor is not evidence about an untyped incoming value."""

from pathlib import Path
from typing import Any

import pytest

from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.core.schemas import Edge
from arcgraph.pipeline.contracts import FrontendGraphFragment
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer

HEADER = "class Config:\n    def get(self, key): return key\nclass Other:\n    def get(self, key): return key\n"


def analyze(tmp_path: Path, body: str) -> FrontendGraphFragment:
    (tmp_path / "sample.py").write_text(HEADER + body)
    files = FileScanner(tmp_path, [SourceRoot(".")]).scan()
    return PythonGraphAnalyzer().analyze(files)


def get_calls(fragment: FrontendGraphFragment) -> list[Edge]:
    return [
        e
        for e in fragment.edges
        if e.source == "fn:sample.use"
        and e.target == "method:sample.Config.get"
        and e.kind == "calls"
    ]


@pytest.mark.parametrize(
    "signature,expression",
    [
        ("flag", "Config() if flag else Config()"),
        ("config: Config, flag", "config if flag else Config()"),
        ("config: Config | None = None", "config if config is not None else Config()"),
        ("config: Config | None = None", "Config() if config is None else config"),
        ("config: Config | None = None", "config if None is not config else Config()"),
        ("config: Config = None", "config if config is not None else Config()"),
    ],
)
def test_all_reachable_branches_have_the_same_receiver_type(
    tmp_path: Path, signature: str, expression: str
) -> None:
    result = analyze(
        tmp_path,
        f"def use({signature}):\n    cfg = {expression}\n    return cfg.get('key')\n",
    )
    assert len(get_calls(result)) == 1
    node = next(n for n in result.nodes if n.id == "fn:sample.use")
    ref = next(r for r in node.properties["type_refs"] if r["name"] == "cfg")
    assert ref["strategy"] == "conditional_join"
    assert ref["type_id"] == "class:sample.Config"


@pytest.mark.parametrize(
    "body",
    [
        "def use(config=None):\n    cfg = config if config is not None else Config()\n    return cfg.get('key')\n",
        "def use(config=None):\n    return config.get('key')\n",
        "def use(flag):\n    cfg = Config() if flag else Other()\n    return cfg.get('key')\n",
        "def use(flag):\n    cfg = Config() if flag else {}\n    return cfg.get('key')\n",
        "def use(config: Config | Other, flag):\n    cfg = config if flag else Config()\n    return cfg.get('key')\n",
        "def use(config: Config | None, flag):\n    cfg = config if flag else Config()\n    return cfg.get('key')\n",
        "def use(config: Config | None):\n    cfg = config if config is None else Config()\n    return cfg.get('key')\n",
        "def use(config=None):\n    cfg = config or Config()\n    return cfg.get('key')\n",
        "def use(Config, flag):\n    cfg = Config() if flag else Config()\n    return cfg.get('key')\n",
        "def use(config: Config, flag):\n    config = unknown()\n    cfg = config if flag else Config()\n    return cfg.get('key')\n",
        "def use(flag):\n    cfg = Config() if flag else Config()\n    cfg = unknown()\n    return cfg.get('key')\n",
        "def use(flag):\n    cfg.get('key')\n    cfg = Config() if flag else Config()\n",
        "def use(flag):\n    if flag:\n        cfg = Config() if flag else Config()\n    return cfg.get('key')\n",
        "def use(config: Config, flag):\n    cfg = config if (config := unknown()) else Config()\n    return cfg.get('key')\n",
        "def use(config: Config | Other | None):\n    cfg = config if config is not None else Config()\n    return cfg.get('key')\n",
        "def use(config: Config | None, other):\n    cfg = config if other is not None else Config()\n    return cfg.get('key')\n",
        "def use(config: Config | None):\n    cfg = config if config != None else Config()\n    return cfg.get('key')\n",
        "def use(config: Config | None):\n    cfg = config if config else Config()\n    return cfg.get('key')\n",
        "def use(flag, config: Config = None):\n    cfg = config if flag else Config()\n    return cfg.get('key')\n",
        "def use(config: Config = unknown()):\n    cfg = config if config is not None else Config()\n    return cfg.get('key')\n",
        "def use(flag):\n    cfg = Config() if flag else Config()\n    del cfg\n    return cfg.get('key')\n",
        "def use(flag):\n    cfg = Config() if flag else Config()\n    def mutate():\n        nonlocal cfg\n        cfg = unknown()\n    return cfg.get('key')\n",
    ],
)
def test_unknown_mixed_shadowed_or_unavailable_values_do_not_invent_calls(
    tmp_path: Path, body: str
) -> None:
    result = analyze(tmp_path, body)
    assert not get_calls(result)
    node = next(n for n in result.nodes if n.id == "fn:sample.use")
    assert any(c.get("name", "").endswith(".get") for c in node.properties["callsites"])


def test_imported_constructor_alias_and_optional_parameter(tmp_path: Path) -> None:
    (tmp_path / "model.py").write_text(HEADER)
    (tmp_path / "consumer.py").write_text(
        "from typing import Optional\nfrom model import Config as C\n"
        "def use(config: Optional[C] = None):\n"
        "    cfg = config if config is not None else C()\n"
        "    alias = cfg\n    return alias.get('key')\n"
    )
    result = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    edge = next(
        e
        for e in result.edges
        if e.source == "fn:consumer.use" and e.target == "method:model.Config.get"
    )
    assert edge.resolution.strategy == "receiver_type"
    assert edge.properties["callsite"]["receiver_type"] == "class:model.Config"


def test_conditional_join_does_not_reuse_shadowed_import_or_local_type(
    tmp_path: Path,
) -> None:
    (tmp_path / "model.py").write_text(HEADER)
    (tmp_path / "consumer.py").write_text(
        "from model import Config\nConfig = unknown()\n"
        "def use(flag):\n    cfg = Config() if flag else Config()\n    return cfg.get('key')\n"
    )
    result = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    assert not any(
        e.source == "fn:consumer.use" and e.target == "method:model.Config.get"
        for e in result.edges
    )


@pytest.mark.parametrize(
    "imports,annotation",
    [
        ("from external_library import Config\n", "Config"),
        ("", "Config"),
        ("from model import Config\nConfig = unknown()\n", "Config"),
        ("if flag:\n    from model import Config\n", "Config"),
    ],
)
def test_parameter_type_needs_exact_stable_import_identity(
    tmp_path: Path, imports: str, annotation: str
) -> None:
    (tmp_path / "model.py").write_text(HEADER)
    (tmp_path / "consumer.py").write_text(
        "from model import Config as Local\n"
        + imports
        + f"def use(config: {annotation}, flag):\n"
        "    cfg = config if flag else Local()\n    return cfg.get('key')\n"
    )
    result = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    assert not any(
        e.source == "fn:consumer.use" and e.target == "method:model.Config.get"
        for e in result.edges
    )


def test_conditional_join_incremental_replacement_matches_full_build(
    tmp_path: Path,
) -> None:
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer

    repo: Path = tmp_path / "repo"
    repo.mkdir()
    path: Path = repo / "sample.py"
    roots = [SourceRoot(".")]
    output = tmp_path / "index"
    path.write_text(
        HEADER
        + "def use(flag):\n    cfg = Config() if flag else Config()\n    return cfg.get('key')\n"
    )
    ArcGraphIndexer(repo, output, roots).build()

    def state(folder: Path) -> tuple[dict[str, Any], list[tuple]]:
        reader = GraphStoreReader.from_current(folder)
        node = next(n for n in reader.read_nodes() if n.id == "fn:sample.use")
        return node.properties, sorted(
            (e.target, e.kind, e.confidence)
            for e in reader.read_edges()
            if e.source == node.id
        )

    assert state(output)[0]["conditional_type_inference"] is True
    for i, expr in enumerate(
        ["config if config is not None else Config()", "Config() if flag else Config()"]
    ):
        path.write_text(
            HEADER
            + f"def use(flag, config=None):\n    cfg = {expr}\n    return cfg.get('key')\n"
        )
        ArcGraphReindexer(repo, output, roots).reindex_changed()
        full = tmp_path / f"full-{i}"
        ArcGraphIndexer(repo, full, roots).build()
        assert state(output) == state(full)
        assert bool(state(output)[0].get("conditional_type_inference")) == bool(i)


def test_provider_changes_refresh_unchanged_conditional_consumers(
    tmp_path: Path,
) -> None:
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer

    repo: Path = tmp_path / "repo"
    repo.mkdir()
    model: Path = repo / "model.py"
    model.write_text("# Class is initially unavailable.\n")
    (repo / "consumer.py").write_text(
        "from model import Config\ndef use(flag):\n"
        "    cfg = Config() if flag else Config()\n    return cfg.get('key')\n"
    )
    roots = [SourceRoot(".")]
    output = tmp_path / "index"
    ArcGraphIndexer(repo, output, roots).build()

    def state(folder: Path) -> tuple[dict[str, Any], list[tuple]]:
        reader = GraphStoreReader.from_current(folder)
        node = next(n for n in reader.read_nodes() if n.id == "fn:consumer.use")
        return node.properties, sorted(
            (e.target, e.kind) for e in reader.read_edges() if e.source == node.id
        )

    assert not state(output)[0].get("conditional_type_inference")
    for i, text in enumerate(
        [HEADER, "class Config:\n    def other(self): pass\n", "# Removed\n"]
    ):
        model.write_text(text)
        ArcGraphReindexer(repo, output, roots).reindex_changed()
        full = tmp_path / f"full-{i}"
        ArcGraphIndexer(repo, full, roots).build()
        assert state(output) == state(full)
        assert (("method:model.Config.get", "calls") in state(output)[1]) == (i == 0)
