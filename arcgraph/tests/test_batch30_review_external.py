"""Real typeshed public calls, conservative overloads, and mandatory CI fixtures."""

from arcgraph.semantic.journal import load

from pathlib import Path

import pytest
import yaml

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.semantic.external_targets import definition
from arcgraph.semantic.snapshot import sha
from arcgraph.tests.test_batch30_build import configure, generation_directory
from arcgraph.tests.test_semantic_isolation import prepared as prepared


@pytest.mark.parametrize("builtin", [False, True])
@pytest.mark.parametrize("overload", [False, True])
def test_conditional_same_name_definitions_stay_unknown(tmp_path, builtin, overload):
    name = "get" if builtin else "ping"
    decorator = "        @overload\n" if overload else ""
    text = f"class {'dict' if builtin else 'Client'}:\n    if FLAG:\n{decorator}        def {name}(self): ...\n    else:\n{decorator}        def {name}(self): ...\n"
    line = next(i for i, value in enumerate(text.splitlines()) if "def " in value)
    module = "builtins" if builtin else "example"
    row = {
        "origin": "typeshed",
        "relative": f"stdlib/{module}.pyi",
        "module": module,
        "text": text,
        "text_digest": sha(text.encode()),
    }
    item = {
        "name": name,
        "selectionRange": {
            "start": {"line": line, "character": 12},
            "end": {"line": line, "character": 12 + len(name)},
        },
    }
    assert definition(row, item, "utf-16") is None


def external_project(prepared):
    project, environment, _, _ = prepared
    site = next(
        environment.glob("lib/python*/site-packages"), environment / "Lib/site-packages"
    )
    (site / "unique.py").write_text("def ping(): return 1\n", encoding="utf-8")
    (site / "ambiguous.py").write_text(
        "if FLAG:\n    def ping(): return 1\nelse:\n    def ping(): return 2\n",
        encoding="utf-8",
    )
    (project / "demo.py").write_text(
        "from collections.abc import Mapping\nimport unique, ambiguous\n"
        "def use_dict(d: dict[str, int]):\n    d.get('x')\n    d.copy()\n"
        "def use_map(m: Mapping[str, int]):\n    m.get('x')\n    m.keys()\n"
        "def use_unique():\n    unique.ping()\n"
        "def use_ambiguous():\n    ambiguous.ping()\n",
        encoding="utf-8",
    )
    return project


def test_real_typeshed_declarations_align_to_public_identity(prepared, tmp_path):
    project = external_project(prepared)
    configure(project)
    output = tmp_path / "out"
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    reader = GraphStoreReader.from_current(output)
    primary = load(generation_directory(directory) / "pyright.json")
    mapped = {row["target"] for row in primary["mapping"] if row["stage"] == "mapped"}
    targets = {
        "extsym:builtins.dict.get",
        "extsym:builtins.dict.copy",
        "extsym:collections.abc.Mapping.get",
        "extsym:collections.abc.Mapping.keys",
    }
    assert targets <= mapped
    assert not any(t and t.startswith("extsym:typing.Mapping.") for t in mapped)
    assert targets <= {node.id for node in reader.read_nodes()}
    for target in targets:
        assert any(
            edge.target == target
            and any(
                e.kind == "type_inference" and "declaration" in (e.detail or "")
                for e in edge.evidence
            )
            for edge in reader.read_edges()
        ), target
    candidates = [
        c
        for a in primary["answers"]
        for c in a["candidates"]
        if c["target"].startswith("extsym:collections.abc.Mapping.")
    ]
    assert candidates and all(c["semantics"] == "declaration" for c in candidates)
    assert any(a["outcome"] == "unknown" for a in primary["answers"])
    ambiguous = [
        e
        for e in reader.read_edges()
        if e.source == "fn:demo.use_ambiguous" and e.target == "extsym:ambiguous.ping"
    ]
    assert ambiguous and all(
        not any(x.kind == "type_inference" for x in e.evidence) for e in ambiguous
    )


def test_external_public_supports_merge_using_default_kind(prepared, tmp_path):
    project = external_project(prepared)
    default_output = tmp_path / "default"
    ArcGraphIndexer(project, default_output).build()
    default = GraphStoreReader.from_current(default_output)
    configure(project)
    output = tmp_path / "enabled"
    ArcGraphIndexer(project, output).build()
    reader = GraphStoreReader.from_current(output)
    for target in (
        "extsym:builtins.dict.get",
        "extsym:builtins.dict.copy",
        "extsym:unique.ping",
    ):
        baseline = [
            e
            for e in default.read_edges()
            if e.target == target and e.properties.get("callsite")
        ]
        edges = [
            e
            for e in reader.read_edges()
            if e.target == target and e.properties.get("callsite")
        ]
        assert len(baseline) == len(edges) == 1, [(e.kind, e.target) for e in edges]
        assert edges[0].kind == baseline[0].kind == "uses"
        assert {"type_inference", "ast_attribute_call"} <= {
            e.kind for e in edges[0].evidence
        }


@pytest.mark.parametrize(
    "missing", ["ARCGRAPH_TEST_PROJECT_PYTHON", "ARCGRAPH_TEST_PYRIGHT_TGZ"]
)
def test_ci_requires_semantic_dependencies_instead_of_skipping(
    tmp_path, monkeypatch, missing
):
    monkeypatch.setenv("ARCGRAPH_REQUIRE_SEMANTIC", "1")
    monkeypatch.setenv("ARCGRAPH_TEST_PROJECT_PYTHON", "/unused-project-python")
    monkeypatch.setenv("ARCGRAPH_TEST_PYRIGHT_TGZ", "/unused-pyright.tgz")
    monkeypatch.delenv(missing, raising=False)
    try:
        prepared.__wrapped__(tmp_path, monkeypatch, None)
    except BaseException as exc:
        assert isinstance(exc, pytest.fail.Exception), type(exc).__name__
        assert missing in str(exc)
    else:
        pytest.fail("Required semantic dependency was silently accepted")


def test_ci_requires_offline_semantic_archive(tmp_path, monkeypatch):
    from arcgraph.tests.test_semantic_install import tgz

    monkeypatch.setenv("ARCGRAPH_REQUIRE_SEMANTIC", "1")
    monkeypatch.delenv("ARCGRAPH_TEST_PYRIGHT_TGZ", raising=False)
    try:
        tgz.__wrapped__()
    except BaseException as exc:
        assert isinstance(exc, pytest.fail.Exception), type(exc).__name__
        assert "ARCGRAPH_TEST_PYRIGHT_TGZ" in str(exc)
    else:
        pytest.fail("Required pyright archive was silently accepted")


def test_local_semantic_fixture_may_skip(tmp_path, monkeypatch):
    monkeypatch.delenv("ARCGRAPH_REQUIRE_SEMANTIC", raising=False)
    monkeypatch.delenv("ARCGRAPH_TEST_PYRIGHT_TGZ", raising=False)
    with pytest.raises(pytest.skip.Exception, match="ARCGRAPH_TEST_PYRIGHT_TGZ"):
        prepared.__wrapped__(tmp_path, monkeypatch, None)


def test_unknown_primary_is_visible_without_changing_analyzer_resolution(
    prepared, tmp_path
):
    project = external_project(prepared)
    configure(project)
    output = tmp_path / "out"
    ArcGraphIndexer(project, output).build()
    edges = [
        e
        for e in GraphStoreReader.from_current(output).read_edges()
        if e.target == "extsym:ambiguous.ping" and e.properties.get("callsite")
    ]
    assert len(edges) == 1
    assert any(
        value.startswith("pyright:unknown:") for value in edges[0].resolution.fallbacks
    )
    assert "Primary pyright answer unknown" in edges[0].resolution.detail
    assert edges[0].resolution.status == "resolved"
    assert edges[0].resolution.strategy == "imported_module_attribute"
    assert not any(e.kind == "type_inference" for e in edges[0].evidence)


def test_ci_matrix_and_quality_require_real_semantic_tests():
    workflow = yaml.safe_load(
        (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
    )
    for job_name in ("quality", "test-matrix"):
        job = workflow["jobs"][job_name]
        step = next(s for s in job["steps"] if "pytest" in s.get("run", ""))
        assert step["env"].get("ARCGRAPH_REQUIRE_SEMANTIC") == "1"
        assert (
            step["env"]["ARCGRAPH_TEST_PROJECT_PYTHON"]
            == "${{ steps.semantic-project-python.outputs.python-path }}"
        )
        project_step = next(
            s for s in job["steps"] if s.get("id") == "semantic-project-python"
        )
        assert project_step["with"]["python-version"] == "3.14"
        legacy_step = next(
            s for s in job["steps"] if s.get("id") == "semantic-legacy-python"
        )
        assert legacy_step["with"]["python-version"] == "3.11"
        assert step["env"]["ARCGRAPH_TEST_LEGACY_PYTHON"] == (
            "${{ steps.semantic-legacy-python.outputs.python-path }}"
        )


def test_unproved_reexport_keeps_analyzer_fallback_without_semantic_claim(
    prepared, tmp_path, monkeypatch
):
    from arcgraph.semantic.external_targets import Capture

    project = external_project(prepared)
    configure(project)
    capture = Capture.item

    def missing_proof(self, *args):
        capture(self, *args)
        for uri in list(self.files):
            if self.files[uri]["module"] == "collections.abc":
                del self.files[uri]

    monkeypatch.setattr(Capture, "item", missing_proof)
    output = tmp_path / "out"
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    primary = load(generation_directory(directory) / "pyright.json")
    assert not any(
        row.get("target", "") and "Mapping." in row["target"]
        for row in primary["mapping"]
    )
    assert sum(a["outcome"] == "unknown" for a in primary["answers"]) >= 3
    edges = [
        e
        for e in GraphStoreReader.from_current(output).read_edges()
        if e.target.startswith("extsym:collections.abc.Mapping.")
        and e.properties.get("callsite")
    ]
    assert len(edges) == 2
    assert all(
        e.resolution.status == "resolved"
        and any(x.kind == "ast_attribute_call" for x in e.evidence)
        and not any(x.kind == "type_inference" for x in e.evidence)
        for e in edges
    )
    assert all(
        any(value.startswith("pyright:unknown:") for value in e.resolution.fallbacks)
        for e in edges
    )
