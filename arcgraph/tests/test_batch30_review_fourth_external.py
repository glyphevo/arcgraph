"""Review E: placeholder siblings must not hide a proved implementation."""

import ast
import json

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests.test_batch30_build import configure, generation_directory
from arcgraph.tests.test_batch30_review_second import lookup
from arcgraph.tests.test_semantic_isolation import prepared as prepared


@pytest.mark.parametrize(
    "body", ["...", "pass", "raise NotImplementedError", '"doc"; ...']
)
@pytest.mark.parametrize("shape", ["branches", "class", "async"])
def test_placeholder_selections_bind_the_unique_real_sibling(body, shape):
    keyword = "async def" if shape == "async" else "def"
    if shape == "branches":
        text = (
            "import sys\nclass Client:\n    if sys.version_info >= (3, 99):\n"
            f"        def ping(self): {body}\n"
            "    else:\n        def ping(self): return 1\n"
        )
    else:
        text = (
            f"class Client:\n    {keyword} ping(self): {body}\n"
            f"    {keyword} ping(self): return 1\n"
        )
    functions = [
        n
        for n in ast.walk(ast.parse(text))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    real = next(n for n in functions if isinstance(n.body[0], ast.Return))
    for selected in functions:
        result = lookup(text, selected.lineno - 1)
        assert result is not None, (shape, body, selected.lineno)
        assert result["declaration_only"] is False
        assert result["span"].start[0] == real.lineno - 1
        assert result["name_span"].start[0] == real.lineno - 1


@pytest.mark.parametrize("body", ["...", "pass", "raise NotImplementedError"])
def test_later_class_placeholder_overwrites_an_earlier_implementation(body):
    text = f"class Client:\n    def ping(self): return 1\n    def ping(self): {body}\n"
    assert lookup(text, 1) is None
    assert lookup(text, 2) is None


@pytest.mark.parametrize("branches", [False, True])
def test_multiple_real_implementations_stay_unknown(branches):
    text = (
        "class Client:\n    if FLAG:\n        def ping(self): return 1\n"
        "    else:\n        def ping(self): return 2\n"
        if branches
        else "class Client:\n    def ping(self): return 1\n    def ping(self): return 2\n"
    )
    for line, value in enumerate(text.splitlines()):
        if "def ping" in value:
            assert lookup(text, line) is None


def test_real_placeholder_siblings_gain_execution_support(prepared, tmp_path):
    project, environment, _, _ = prepared
    site = next(
        environment.glob("lib/python*/site-packages"), environment / "Lib/site-packages"
    )
    (site / "example.py").write_text(
        "import sys\nclass VersionEmpty:\n    if sys.version_info >= (3, 99):\n"
        "        def ping(self): ...\n    else:\n        def ping(self): return 1\n"
        "class Shadow:\n    def ping(self): ...\n    def ping(self): return 1\n"
        "class OnlyReal:\n    def ping(self): return 1\n"
        "class Overwritten:\n    def ping(self): return 1\n    def ping(self): pass\n",
        encoding="utf-8",
    )
    (project / "demo.py").write_text(
        "from example import VersionEmpty, Shadow, OnlyReal, Overwritten\n"
        "def version(c: VersionEmpty): return c.ping()\n"
        "def shadow(c: Shadow): return c.ping()\n"
        "def real(c: OnlyReal): return c.ping()\n"
        "def overwritten(c: Overwritten): return c.ping()\n",
        encoding="utf-8",
    )
    configure(project)
    output = tmp_path / "out"
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    generation = generation_directory(directory)
    primary = json.loads((generation / "pyright.json").read_text(encoding="utf-8"))
    edges = list(GraphStoreReader.from_current(output).read_edges())
    assert primary["raw_count"] == len(primary["mapping"])
    for name in ("VersionEmpty", "Shadow", "OnlyReal"):
        target = f"extsym:example.{name}.ping"
        assert any(
            m.get("target") == target and m["stage"] == "mapped"
            for m in primary["mapping"]
        ), target
        assert any(
            c["target"] == target and c["semantics"] == "implementation"
            for a in primary["answers"]
            for c in a["candidates"]
        ), target
        assert any(
            e.target == target and any(v.kind == "type_inference" for v in e.evidence)
            for e in edges
        ), target
    target = "extsym:example.Overwritten.ping"
    assert not any(m.get("target") == target for m in primary["mapping"])
    assert not any(
        e.target == target and any(v.kind == "type_inference" for v in e.evidence)
        for e in edges
    )
