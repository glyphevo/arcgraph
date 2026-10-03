"""Unchanged providers must remain visible while refreshing Python TypeRefs."""

from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer
from arcgraph.pipeline.reindexer import ArcGraphReindexer

PROVIDER = "class Animal:\n    def speak(self): pass\nclass Pet(Animal): pass\n"
CONSUMERS = {
    "inherited_comprehension": "from provider import Pet\ndef chorus(crowd: list[Pet]):\n    return [buddy.speak() for buddy in crowd]\n",
    "direct_comprehension": "from provider import Animal\ndef chorus(crowd: list[Animal]):\n    return [buddy.speak() for buddy in crowd]\n",
    "ordinary_inherited": "from provider import Pet\ndef chorus(buddy: Pet):\n    return buddy.speak()\n",
    "nullable_inherited": "from provider import Pet\ndef chorus(buddy: Pet | None):\n    return buddy.speak()\n",
    "quoted_inherited": "from provider import Pet\ndef chorus(buddy: 'Pet'):\n    return buddy.speak()\n",
    "qualified_inherited": "import provider\ndef chorus(buddy: provider.Pet):\n    return buddy.speak()\n",
}


def snapshot(output: Path):
    reader = GraphStoreReader.from_current(output)
    fn = next(n for n in reader.read_nodes() if n.id == "fn:consumer.chorus")
    calls = sorted(
        (e.target, e.resolution.status, e.resolution.strategy)
        for e in reader.read_edges()
        if e.source == fn.id and e.kind in {"calls", "uses", "dynamic_call"}
    )
    return calls, fn.properties.get("type_refs", [])


@pytest.mark.parametrize("case", CONSUMERS)
def test_repeated_partial_edits_match_full_build(tmp_path: Path, case: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    provider = repo / "provider.py"
    consumer = repo / "consumer.py"
    unrelated = repo / "unrelated.py"
    provider.write_text(PROVIDER, encoding="utf-8")
    consumer.write_text(CONSUMERS[case], encoding="utf-8")
    unrelated.write_text("VALUE = 1\n", encoding="utf-8")
    roots = [SourceRoot(".")]
    index = tmp_path / "index"
    ArcGraphIndexer(repo, index, roots).build()
    assert snapshot(index)[0][0][0] == "method:provider.Animal.speak"
    edits = [
        (unrelated, "VALUE = 1\n# unrelated\n", True),
        (consumer, CONSUMERS[case] + "# consumer only\n", True),
        (provider, "class Animal: pass\nclass Pet(Animal): pass\n", False),
        (provider, PROVIDER, True),
        (provider, "class Renamed:\n    def speak(self): pass\n", False),
        (provider, PROVIDER, True),
        (provider, None, False),
        (provider, PROVIDER, True),
        (unrelated, "VALUE = 2\n", True),
    ]
    for step, (path, source, resolved) in enumerate(edits):
        if source is None:
            path.unlink()
        else:
            path.write_text(source, encoding="utf-8")
        ArcGraphReindexer(repo, index, roots).reindex_changed()
        full = tmp_path / f"full-{step}"
        ArcGraphIndexer(repo, full, roots).build()
        actual = snapshot(index)
        assert actual == snapshot(full), (case, step)
        assert (
            any(
                target == "method:provider.Animal.speak" and status == "resolved"
                for target, status, _ in actual[0]
            )
            is resolved
        ), (case, step, actual[0])


def test_context_nodes_do_not_resurrect_replaced_provider(tmp_path: Path):
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    provider.write_text(PROVIDER, encoding="utf-8")
    consumer.write_text(CONSUMERS["inherited_comprehension"], encoding="utf-8")
    roots = [SourceRoot(".")]
    old = PythonGraphAnalyzer().analyze(FileScanner(tmp_path, roots).scan())
    provider.write_text("class Pet: pass\n", encoding="utf-8")
    files = FileScanner(tmp_path, roots).scan()
    # Even an API caller supplying stale context cannot override current files.
    current = PythonGraphAnalyzer().analyze(files, call_context_nodes=old.nodes)
    assert not any(e.target == "method:provider.Animal.speak" for e in current.edges)


def test_explicit_import_does_not_select_unrelated_same_named_class(tmp_path: Path):
    (tmp_path / "provider.py").write_text("class Other: pass\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text(
        "class Pet:\n    def speak(self): pass\n", encoding="utf-8"
    )
    (tmp_path / "consumer.py").write_text(
        CONSUMERS["inherited_comprehension"], encoding="utf-8"
    )
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    assert not any(
        e.source == "fn:consumer.chorus" and e.target == "method:unrelated.Pet.speak"
        for e in graph.edges
    )


def test_provider_rename_with_same_named_unrelated_class(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "provider.py").write_text(PROVIDER, encoding="utf-8")
    (repo / "consumer.py").write_text(
        CONSUMERS["inherited_comprehension"], encoding="utf-8"
    )
    (repo / "other.py").write_text(
        "class Pet:\n    def speak(self): pass\n", encoding="utf-8"
    )
    roots = [SourceRoot(".")]
    index = tmp_path / "index"
    ArcGraphIndexer(repo, index, roots).build()
    for step, source in enumerate(["class Renamed: pass\n", PROVIDER]):
        (repo / "provider.py").write_text(source, encoding="utf-8")
        ArcGraphReindexer(repo, index, roots).reindex_changed()
        full = tmp_path / f"full-{step}"
        ArcGraphIndexer(repo, full, roots).build()
        assert snapshot(index) == snapshot(full)
        assert not any(
            target == "method:other.Pet.speak" for target, _, _ in snapshot(index)[0]
        )
        assert any(
            target == "method:provider.Animal.speak"
            for target, _, _ in snapshot(index)[0]
        ) is (step == 1)


def test_parse_error_does_not_reuse_old_context(tmp_path: Path):
    (tmp_path / "provider.py").write_text(PROVIDER, encoding="utf-8")
    (tmp_path / "consumer.py").write_text(
        CONSUMERS["inherited_comprehension"], encoding="utf-8"
    )
    roots = [SourceRoot(".")]
    old = PythonGraphAnalyzer().analyze(FileScanner(tmp_path, roots).scan())
    (tmp_path / "provider.py").write_text("class Pet(\n", encoding="utf-8")
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, roots).scan(), call_context_nodes=old.nodes
    )
    assert any(w.kind == "parse_error" for w in graph.warnings)
    assert not any(e.target == "method:provider.Animal.speak" for e in graph.edges)
