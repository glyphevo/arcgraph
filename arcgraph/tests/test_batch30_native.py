"""Concrete builtin identities are proven without treating protocols as bodies."""

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.semantic.external_targets import native_identity
from arcgraph.semantic.journal import load
from arcgraph.tests.test_batch30_build import configure, generation_directory
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def test_only_pinned_concrete_builtin_declarations_have_native_identity():
    get = {"path": "typeshed/stdlib/builtins.pyi", "qualname": "builtins.dict.get"}
    assert native_identity(get, "3.11")
    assert not native_identity(dict(get, path="dependency/builtins.pyi"), "3.14")
    assert not native_identity(
        dict(get, qualname="collections.abc.Mapping.get"), "3.14"
    )
    assert not native_identity(dict(get, qualname="builtins.dict.missing"), "3.14")
    modern = dict(get, qualname="builtins.bytearray.resize")
    assert not native_identity(modern, "3.11")
    assert native_identity(modern, "3.14")


def test_real_dict_stub_maps_to_native_runtime_without_protocol_aliasing(
    prepared, tmp_path
):
    project, _, _, _ = prepared
    configure(project)
    (project / "demo.py").write_text(
        "from collections.abc import Mapping\ndef native(value: dict[str, str]):\n    return value.get('key')\ndef declared(value: Mapping[str, str]):\n    return value.get('key')\n",
        encoding="utf-8",
    )
    metadata, directory = ArcGraphIndexer(project, tmp_path / "out").build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    generation = generation_directory(directory)
    primary = load(generation / "pyright.json")
    candidates = [
        (a["subject"], c) for a in primary["answers"] for c in a["candidates"]
    ]
    native = [c for _, c in candidates if c["target"] == "extsym:builtins.dict.get"]
    assert native and all(c["semantics"] == "implementation" for c in native)
    assert all(
        "native_runtime_identity:extsym:builtins.dict.get" in c["evidence"]
        for c in native
    )
    declarations = [
        c
        for _, c in candidates
        if c["target"]
        in ("extsym:collections.abc.Mapping.get", "extsym:typing.Mapping.get")
    ]
    assert declarations and all(c["semantics"] == "declaration" for c in declarations)
    reader = GraphStoreReader.from_current(tmp_path / "out")
    edges = reader.read_edges()
    assert any(
        e.source == "fn:demo.native"
        and e.target == "extsym:builtins.dict.get"
        and any(x.kind == "type_inference" for x in e.evidence)
        for e in edges
    )
    assert not any(
        e.source == "fn:demo.declared" and e.target == "extsym:builtins.dict.get"
        for e in edges
    )
    assert primary["raw_count"] == len(primary["mapping"])


def test_external_execution_requires_its_native_identity_proof(
    prepared, tmp_path, monkeypatch
):
    from arcgraph.semantic.external_targets import Registry

    original = Registry.admitted

    def remove_proof(self, run, view):
        answers = tuple(
            a.model_copy(
                update={
                    "candidates": tuple(
                        c.model_copy(
                            update={
                                "evidence": tuple(
                                    e
                                    for e in c.evidence
                                    if not e.startswith("native_runtime_identity:")
                                )
                            }
                        )
                        for c in a.candidates
                    )
                }
            )
            for a in run.answers
        )
        return original(self, run.model_copy(update={"answers": answers}).seal(), view)

    monkeypatch.setattr(Registry, "admitted", remove_proof)
    project, _, _, _ = prepared
    configure(project)
    (project / "demo.py").write_text(
        "def f(value: dict[str, str]): return value.get('key')\n", encoding="utf-8"
    )
    metadata, _ = ArcGraphIndexer(project, tmp_path / "out").build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "unavailable"
    assert (
        "external_definition_unverified"
        in metadata.toolchain_status["semantic_core"]["reason"]
    )
