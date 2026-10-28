"""SCIP document paths are relative to metadata.project_root, not the repo."""

import ast
import json

import pytest

from arcgraph.analyzers.precise_references import PreciseReferenceAnalyzer
from arcgraph.analyzers.symbols import SymbolAnalyzer
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.interfaces import precision


def payload(root, *, camel=False, path="service.py"):
    return {
        "metadata": {"projectRoot" if camel else "project_root": root},
        "documents": [
            {
                "relativePath" if camel else "relative_path": path,
                "position_encoding": 1,
                "occurrences": [
                    {"range": [1, 21, 27], "symbol": "fn:pkg.service.target"}
                ],
            }
        ],
    }


def source(root):
    (root / "pkg").mkdir()
    (root / "pkg/service.py").write_text(
        "def target(): return 1\ndef caller(): return target()\n", encoding="utf-8"
    )
    file = FileScanner(root, [SourceRoot(".")]).scan()[0]
    return (
        SymbolAnalyzer()
        .analyze(file, ast.parse((root / file.path).read_text(encoding="utf-8")))
        .nodes
    )


@pytest.mark.parametrize("camel", [False, True])
def test_raw_target_only_scip_maps_callee_to_the_repo_file(tmp_path, camel):
    nodes = source(tmp_path)
    path = tmp_path / "scip.json"
    path.write_text(
        json.dumps(payload((tmp_path / "pkg").as_uri(), camel=camel)), encoding="utf-8"
    )
    result = PreciseReferenceAnalyzer(tmp_path, path).analyze(nodes)
    assert [(e.source, e.target) for e in result.edges] == [
        ("fn:pkg.service.caller", "fn:pkg.service.target")
    ]
    assert result.edges[0].evidence[0].path == "pkg/service.py"
    assert not result.warnings


def test_converter_rebases_before_merging_target_documents(tmp_path, monkeypatch):
    (tmp_path / "index.scip").write_bytes(b"placeholder")
    monkeypatch.setattr(
        precision,
        "_run_scip_print_json",
        lambda **kwargs: payload((tmp_path / "pkg").as_uri()),
    )
    precision.convert_scip_index_to_json(
        repo_root=tmp_path,
        scip_index_path="index.scip",
        output_path="index.json",
        commit_sha="test",
    )
    result = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert result["documents"][0]["relative_path"] == "pkg/service.py"
    assert result["metadata"]["project_root"] == tmp_path.as_uri()
    assert result["metadata"]["index_project_root"] == (tmp_path / "pkg").as_uri()


@pytest.mark.parametrize(
    "root,path",
    [
        ("outside", "service.py"),
        ("inside", "../../service.py"),
        ("remote", "service.py"),
        ("outside", "back-inside"),
    ],
)
def test_invalid_scip_root_or_document_is_reported_without_guessing(
    tmp_path, root, path
):
    nodes = source(tmp_path)
    if path == "back-inside":
        path = tmp_path.name + "/pkg/service.py"
    roots = {
        "outside": tmp_path.parent.as_uri(),
        "inside": (tmp_path / "pkg").as_uri(),
        "remote": "https://example.invalid/pkg",
    }
    index = tmp_path / "scip.json"
    index.write_text(json.dumps(payload(roots[root], path=path)), encoding="utf-8")
    result = PreciseReferenceAnalyzer(tmp_path, index).analyze(nodes)
    assert result.edges == []
    assert any(w.kind == "scip_path_unmappable" for w in result.warnings)
    assert result.status == "partial"


def test_multiple_targets_do_not_collapse_equal_relative_paths(tmp_path, monkeypatch):
    def index(**kwargs):
        (tmp_path / "index.scip").write_text(kwargs["target_only"][0], encoding="utf-8")

    def decode(**kwargs):
        target = kwargs["scip_index"].read_text(encoding="utf-8")
        return payload((tmp_path / target).as_uri())

    monkeypatch.setattr(precision, "_run_scip_python_index", index)
    monkeypatch.setattr(precision, "_run_scip_print_json", decode)
    result = precision.generate_scip_python_json(
        repo_root=tmp_path,
        output_path="index.json",
        target_only=["pkg", "scripts"],
        commit_sha="test",
    )
    data = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert result["documents"] == 2
    assert {d["relative_path"] for d in data["documents"]} == {
        "pkg/service.py",
        "scripts/service.py",
    }


def test_repository_internal_sibling_document_rebases(tmp_path):
    from arcgraph.analyzers.precision_positions import rebase_scip_documents

    index = payload((tmp_path / "pkg").as_uri(), path="../scripts/semantic_shadow.py")
    result = rebase_scip_documents(index, tmp_path)
    assert result["documents"][0]["relative_path"] == "scripts/semantic_shadow.py"
    assert result["metadata"]["project_root"] == tmp_path.as_uri()


def test_document_outside_repository_is_still_rejected(tmp_path):
    from arcgraph.analyzers.precision_positions import rebase_scip_documents

    index = payload((tmp_path / "pkg").as_uri(), path="../../outside.py")
    with pytest.raises(ValueError, match="SCIP document path escapes its project root"):
        rebase_scip_documents(index, tmp_path)


def test_multiple_targets_deduplicate_same_rebased_document(tmp_path, monkeypatch):
    def index(**kwargs):
        (tmp_path / "index.scip").write_text(kwargs["target_only"][0], encoding="utf-8")

    def decode(**kwargs):
        target = kwargs["scip_index"].read_text(encoding="utf-8")
        path = "../scripts/shared.py" if target == "pkg" else "shared.py"
        return payload((tmp_path / target).as_uri(), path=path)

    monkeypatch.setattr(precision, "_run_scip_python_index", index)
    monkeypatch.setattr(precision, "_run_scip_print_json", decode)
    result = precision.generate_scip_python_json(
        repo_root=tmp_path,
        output_path="index.json",
        target_only=["pkg", "scripts"],
        commit_sha="test",
    )
    data = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert result["documents"] == 1
    assert [doc["relative_path"] for doc in data["documents"]] == ["scripts/shared.py"]
