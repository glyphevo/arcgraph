"""Comprehension bindings follow Python evaluation scopes, not source line order."""

from pathlib import Path
import pytest
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer

HEADER = """class First:
    def act(self): pass
    def values(self) -> list[Second]: ...
class Second:
    def act(self): pass
"""


def analyze(tmp_path: Path, source: str):
    (tmp_path / "sample.py").write_text(HEADER + source, encoding="utf-8")
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    return [
        e
        for e in graph.edges
        if e.source == "fn:sample.use"
        and e.properties["callsite"].get("attribute") == "act"
    ]


def actual(edges):
    return [
        (
            v.start_line,
            v.column,
            e.target if e.resolution.status == "resolved" else None,
        )
        for e in edges
        for v in e.evidence
    ]


@pytest.mark.parametrize(
    "expression",
    [
        "[item.act() for item in {items}]",
        "{{item.act() for item in {items}}}",
        "{{item.act(): 1 for item in {items}}}",
        "(item.act() for item in {items})",
    ],
)
def test_independent_comprehensions_reusing_names(tmp_path: Path, expression: str):
    body = (
        "def use(first: list[First], second: list[Second]):\n    one = "
        + expression.format(items="first")
        + "\n    two = "
        + expression.format(items="second")
        + "\n"
    )
    assert sorted(actual(analyze(tmp_path, body))) == [
        (7, 11, "method:sample.First.act"),
        (8, 11, "method:sample.Second.act"),
    ]


@pytest.mark.parametrize(
    "body, expected",
    [
        (
            "    one = [item.act() for item in first]; two = [item.act() for item in second]\n",
            ["First", "Second"],
        ),
        (
            "    one = [\n        item.act()\n        for item in first\n    ]\n    two = [\n        item.act()\n        for item in second\n    ]\n",
            ["First", "Second"],
        ),
        (
            "    one = [item.act() for item in second]\n    two = [item.act() for item in first]\n",
            ["Second", "First"],
        ),
        (
            "    one = [(item.act(), [item.act() for item in second], item.act()) for item in first]\n",
            ["First", "Second", "First"],
        ),
        ("    one = [item.act() for row in rows for item in row]\n", ["Second"]),
        ("    one = [item.act() for item in rows for item in item]\n", ["Second"]),
        (
            "    one = [item.act() for item in item.values()]\n    item.act()\n",
            ["Second", "First"],
        ),
        (
            "    one = [item.act() for item in second]\n    alias = item\n    alias.act()\n    item.act()\n",
            ["Second", "First", "First"],
        ),
    ],
)
def test_nested_outer_and_evaluation_order(
    tmp_path: Path, body: str, expected: list[str]
):
    edges = analyze(
        tmp_path,
        "def use(first: list[First], second: list[Second], rows: list[list[Second]], item: First):\n"
        + body,
    )
    assert [r[2] for r in sorted(actual(edges))] == [
        "method:sample." + name + ".act" for name in expected
    ]


@pytest.mark.parametrize(
    "body, expected",
    [
        ("    one = [item.act() for item in first]\n    item.act()\n", ["First", None]),
        ("    one = [item.act() for item in unknown]\n", [None]),
        (
            "    one = [item.act() for row in rows if item.act() for item in second]\n",
            ["Second", None],
        ),
    ],
)
def test_missing_unbound_and_unknown_inputs_do_not_guess(
    tmp_path: Path, body: str, expected
):
    edges = analyze(
        tmp_path,
        "def use(first: list[First], second: list[Second], rows: list[list[Second]]):\n"
        + body,
    )
    assert [r[2] for r in sorted(actual(edges))] == [
        None if name is None else "method:sample." + name + ".act" for name in expected
    ]


def test_comprehension_walrus_does_not_reuse_outer_type(tmp_path: Path):
    edges = analyze(
        tmp_path,
        "def use(item: First, second: list[Second]):\n    [(alias := item) for item in second]\n    alias.act()\n",
    )
    assert [r[2] for r in actual(edges)] == [None]


def test_comprehension_in_nested_function(tmp_path: Path):
    source = (
        HEADER
        + "def outer():\n    def inner(first: list[First], second: list[Second]):\n        [item.act() for item in first]\n        [item.act() for item in second]\n"
    )
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    edges = [e for e in graph.edges if e.source == "fn:sample.outer.inner"]
    assert {e.target for e in edges} == {
        "method:sample.First.act",
        "method:sample.Second.act",
    }


def test_class_comprehension_does_not_capture_class_locals(tmp_path: Path):
    source = (
        HEADER
        + "class Container:\n    item: First\n    values: list[Second]\n    result = [item.act() for value in values]\n"
    )
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    assert not any(
        e.source == "class:sample.Container" and e.target == "method:sample.First.act"
        for e in graph.edges
    )


def test_comprehension_callable_shadows_module_function(tmp_path: Path):
    (tmp_path / "sample.py").write_text(
        "def operation(): ...\ndef use(operations):\n    return [operation() for operation in operations]\n",
        encoding="utf-8",
    )
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    edges = [e for e in graph.edges if e.source == "fn:sample.use"]
    assert not any(e.target == "fn:sample.operation" for e in edges)
    function = next(n for n in graph.nodes if n.id == "fn:sample.use")
    assert function.properties["callsites"][0]["name"] == "operation"


def test_provider_change_refreshes_comprehension_consumer(tmp_path: Path):
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer

    repo = tmp_path / "repo"
    repo.mkdir()
    model = repo / "model.py"
    model.write_text("class Item:\n    def act(self): ...\n", encoding="utf-8")
    consumer = repo / "consumer.py"
    consumer.write_text(
        "from model import Item\ndef use(items: list[Item]):\n    return [item.act() for item in items]\n",
        encoding="utf-8",
    )
    original = consumer.read_bytes()
    output = tmp_path / "index"
    roots = [SourceRoot(".")]
    ArcGraphIndexer(repo, output, roots).build()

    def state(path):
        return [
            (e.target, e.resolution.status)
            for e in GraphStoreReader.from_current(path).read_edges()
            if e.source == "fn:consumer.use"
        ]

    assert state(output) == [("method:model.Item.act", "resolved")]
    for i, body in enumerate(
        ["class Item: pass\n", "class Item:\n    def act(self): ...\n"]
    ):
        model.write_text(body, encoding="utf-8")
        ArcGraphReindexer(repo, output, roots).reindex_changed()
        full = tmp_path / f"full-{i}"
        ArcGraphIndexer(repo, full, roots).build()
        assert state(output) == state(full)
        assert consumer.read_bytes() == original
        assert state(output)[0][1] == ("unresolved" if i == 0 else "resolved")


def test_untyped_comprehension_keeps_only_explicit_generic_heuristics(tmp_path: Path):
    (tmp_path / "sample.py").write_text(
        'class Config:\n    def get(self, key): ...\ndef use(records):\n    return [config.get("key") for config in records]\n',
        encoding="utf-8",
    )
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    edges = [e for e in graph.edges if e.source == "fn:sample.use"]
    assert len(edges) == 1 and edges[0].target == "extsym:collections.abc.Mapping.get"
    assert edges[0].confidence == "heuristic"


@pytest.mark.parametrize("annotation", ["list[int]", "list[int | str]"])
def test_known_or_mixed_elements_cannot_use_string_heuristics(
    tmp_path: Path, annotation: str
):
    (tmp_path / "sample.py").write_text(
        f"def use(items: {annotation}):\n    return [item.strip() for item in items]\n",
        encoding="utf-8",
    )
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    assert not any(e.target == "extsym:builtins.str.strip" for e in graph.edges)


def test_shadowed_self_does_not_read_enclosing_class_field(tmp_path: Path):
    source = (
        HEADER
        + "class Container:\n    value: First\n    def use(self, others: list[object]):\n        return [self.value.act() for self in others]\n"
    )
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    assert not any(
        e.source == "method:sample.Container.use"
        and e.target == "method:sample.First.act"
        for e in graph.edges
    )


@pytest.mark.parametrize(
    "expression",
    [
        "[self.value.act() for self in others]",
        "[item.act() for self in others for item in self.values]",
    ],
)
def test_shadowed_self_uses_actual_element_fields(tmp_path: Path, expression: str):
    source = HEADER + """class Other:
    value: Second
    values: list[Second]
class Container:
    value: First
    values: list[First]
    def use(self, others: list[Other]):
        return """ + expression + "\n"
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    edges = [e for e in graph.edges if e.source == "method:sample.Container.use"]
    assert any(e.target == "method:sample.Second.act" for e in edges)
    assert not any(e.target == "method:sample.First.act" for e in edges)
