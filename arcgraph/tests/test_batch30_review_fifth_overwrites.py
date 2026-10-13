"""Class writes and deletes invalidate an earlier external method proof."""

from arcgraph.semantic.journal import load


import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests.test_batch30_build import configure, generation_directory
from arcgraph.tests.test_batch30_review_second import lookup
from arcgraph.tests.test_semantic_isolation import prepared as prepared

WRITES = [
    "ping = None",
    "ping = other",
    "ping: object = None",
    "ping: object",
    "ping += other",
    "del ping",
    "ping, marker = None, 1",
    "marker = ping = None",
]


@pytest.mark.parametrize("write", WRITES)
def test_later_class_write_or_delete_stays_unknown(write):
    text = (
        "class Client:\n    def ping(self): return 1\n"
        "    def other(self): return 2\n    " + write + "\n"
    )
    assert lookup(text, 1) is None, write


@pytest.mark.parametrize("write", ["ping = None", "del ping", "ping: object"])
def test_a_write_before_the_method_does_not_hide_it(write):
    assert (
        lookup("class Client:\n    " + write + "\n    def ping(self): return 1\n", 2)
        is not None
    )


def test_nested_function_or_attribute_writes_do_not_rebind_the_class_method():
    text = (
        "class Client:\n    def ping(self): return 1\n"
        "    def other(self):\n        ping = None\n        del ping\n"
        "    other.ping = None\n"
    )
    assert lookup(text, 1) is not None


def test_real_overwritten_methods_keep_only_analyzer_evidence(prepared, tmp_path):
    project, environment, _, _ = prepared
    site = next(
        environment.glob("lib/python*/site-packages"), environment / "Lib/site-packages"
    )
    classes = {f"Write{i}": write for i, write in enumerate(WRITES)}
    (site / "example.py").write_text(
        "\n".join(
            "class " + name + ":\n    def ping(self): return 1\n"
            "    def other(self): return 2\n    " + write + "\n"
            for name, write in classes.items()
        ),
        encoding="utf-8",
    )
    (project / "demo.py").write_text(
        "from example import "
        + ", ".join(classes)
        + "\n"
        + "\n".join(
            f"def use{i}(c: {name}): return c.ping()" for i, name in enumerate(classes)
        ),
        encoding="utf-8",
    )
    configure(project)
    output = tmp_path / "out"
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    primary = load(generation_directory(directory) / "pyright.json")
    edges = list(GraphStoreReader.from_current(output).read_edges())
    for i, name in enumerate(classes):
        target = f"extsym:example.{name}.ping"
        matches = [
            e for e in edges if e.source == f"fn:demo.use{i}" and e.target == target
        ]
        assert matches, (name, classes[name])
        assert all(
            {v.kind for v in e.evidence} == {"ast_attribute_call"} for e in matches
        ), (name, classes[name])
        assert not any(m.get("target") == target for m in primary["mapping"])
        assert not any(
            c["target"] in (target, f"extsym:example.{name}.other")
            for a in primary["answers"]
            for c in a["candidates"]
        )
