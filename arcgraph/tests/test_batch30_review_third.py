"""Review D: proven .py declarations merge support without becoming execution."""

import json

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests.test_batch30_build import configure, generation_directory
from arcgraph.tests.test_semantic_isolation import prepared as prepared


@pytest.mark.parametrize(
    "module,bases,body",
    [
        (
            "plain",
            "",
            "    @overload\n    def ping(self, x: int) -> int: ...\n"
            "    @overload\n    def ping(self, x: str) -> str: ...\n",
        ),
        (
            "proto",
            "(Protocol)",
            "    @overload\n    def ping(self, x: int) -> int: ...\n"
            "    @overload\n    def ping(self, x: str) -> str: ...\n",
        ),
        ("single_proto", "(Protocol)", "    def ping(self, x: int) -> int: ...\n"),
    ],
)
def test_real_py_declaration_support_merges_into_public_edge(
    prepared, tmp_path, module, bases, body
):
    project, environment, _, _ = prepared
    site = next(
        environment.glob("lib/python*/site-packages"), environment / "Lib/site-packages"
    )
    (site / f"{module}.py").write_text(
        "from typing import Protocol, overload\n" + f"class Client{bases}:\n" + body,
        encoding="utf-8",
    )
    (project / "demo.py").write_text(
        f"from {module} import Client\ndef use(c: Client):\n    return c.ping(1)\n",
        encoding="utf-8",
    )
    configure(project)
    output = tmp_path / "out"
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    generation = generation_directory(directory)

    def read(name):
        return json.loads((generation / name).read_text(encoding="utf-8"))

    target = f"extsym:{module}.Client.ping"
    primary = read("pyright.json")
    assert primary["raw_count"] == len(primary["mapping"])
    candidates = [
        c for a in primary["answers"] for c in a["candidates"] if c["target"] == target
    ]
    assert candidates and all(c["semantics"] == "declaration" for c in candidates)
    projection = read("projection.json")
    relations = [r for r in projection["edges"] if r["key"][1] == target]
    assert relations and all(
        r["relation"] == "declaration_dependency" for r in relations
    )
    reader = GraphStoreReader.from_current(output)
    edges = [
        e
        for e in reader.read_edges()
        if e.source == "fn:demo.use" and e.target == target
    ]
    assert len(edges) == 1
    edge = edges[0]
    assert edge.kind == "uses"
    assert {e.kind for e in edge.evidence} >= {"ast_attribute_call", "type_inference"}
    assert any(
        e.kind == "type_inference" and "declaration source" in (e.detail or "")
        for e in edge.evidence
    )
    assert edge.resolution.status == "resolved"
    assert "pyright_call_hierarchy" in edge.confidence_sources["inferred"]
    assert edge.confidence == "inferred"
    assert len([n for n in reader.read_nodes() if n.id == target]) == 1
    assert any(key[1] == target for key in projection["projected_calls"])
    assert read("bridge.json")["graph_losses"] == []
