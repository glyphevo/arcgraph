from __future__ import annotations

import ast
import json
import pytest

from arcgraph.analyzers.precise_references import PreciseReferenceAnalyzer
from arcgraph.analyzers.symbols import SymbolAnalyzer
from arcgraph.core.ids import callsite_id
from arcgraph.core.merge import EvidenceMergeEngine
from arcgraph.core.scanner import FileScanner, SourceRoot


def _import(tmp_path, source, occurrences, *, encoding=1, backend="scip"):
    (tmp_path / "demo.py").write_text(source, encoding="utf-8")
    file = FileScanner(tmp_path, [SourceRoot(".")]).scan()[0]
    nodes = SymbolAnalyzer().analyze(file, ast.parse(source)).nodes
    path = tmp_path / "precision.json"
    if backend == "scip":
        document = {"relative_path": "demo.py", "occurrences": occurrences}
        if encoding is not None:
            document["position_encoding"] = encoding
        payload = {"documents": [document]}
        analyzer = PreciseReferenceAnalyzer(tmp_path, path)
    else:
        payload = {"references": occurrences}
        analyzer = PreciseReferenceAnalyzer(tmp_path, pyright_export_path=path)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return analyzer.analyze(nodes), {node.name: node for node in nodes}


def _occurrence(line, start, end, *, target="f", **values):
    return {"symbol": f"fn:demo.{target}", "range": [line - 1, start, end], **values}


def _call_id(node, line, column, name="f"):
    return callsite_id(node.id, node.path, line, column, name)


@pytest.mark.parametrize("backend", ["scip", "pyright"])
@pytest.mark.parametrize("range_size", [3, 4])
def test_precision_second_call_on_same_line_keeps_its_callsite(
    tmp_path, backend, range_size
):
    source = "def f(): pass\ndef g():\n    f(); f()\n"
    records = [_occurrence(3, 4, 5), _occurrence(3, 9, 10)]
    if range_size == 4:
        for record in records:
            record["range"].insert(2, 2)
    if backend == "pyright":
        records = [dict(item, source="fn:demo.g", kind="call") for item in records]
    result, nodes = _import(tmp_path, source, records, backend=backend)
    assert [edge.resolution.callsite_id for edge in result.edges] == [
        _call_id(nodes["g"], 3, 4),
        _call_id(nodes["g"], 3, 9),
    ]
    merged = EvidenceMergeEngine.dedupe_edges(result.edges)
    assert len(merged) == 1
    assert [fact["callsite_id"] for fact in merged[0].properties["callsites"]] == [
        _call_id(nodes["g"], 3, 4),
        _call_id(nodes["g"], 3, 9),
    ]


def test_scip_three_element_range_has_same_end_line(tmp_path):
    result, _ = _import(
        tmp_path,
        "def f(): pass\ndef g():\n    f(); f()\n",
        [_occurrence(3, 9, 10)],
    )
    assert result.edges[0].evidence[0].end_line == 3


@pytest.mark.parametrize("backend", ["scip", "pyright"])
def test_precision_self_recursive_call_is_retained(tmp_path, backend):
    record = _occurrence(2, 11, 12)
    if backend == "pyright":
        record.update(source="fn:demo.f", kind="call")
    result, nodes = _import(
        tmp_path, "def f():\n    return f()\n", [record], backend=backend
    )
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge.source == edge.target == nodes["f"].id
    assert edge.kind == "calls"
    assert edge.resolution.callsite_id == _call_id(nodes["f"], 2, 11)
    # Retaining self references must not introduce self-defining relationships.
    definition = dict(record, role="definition", source="fn:demo.f")
    definitions, _ = _import(
        tmp_path, "def f():\n    return f()\n", [definition], backend=backend
    )
    assert definitions.edges == []


@pytest.mark.parametrize("callee", ["alias", "obj.alias", "obj.K", "obj.e\u0301"])
def test_scip_alias_call_uses_coordinates_instead_of_target_name(tmp_path, callee):
    source = f"def f(): pass\ndef g():\n    {callee}()\n"
    start = 4 + (4 if callee.startswith("obj.") else 0)
    end = start + len(callee.rsplit(".", 1)[-1].encode("utf-8"))
    result, nodes = _import(tmp_path, source, [_occurrence(3, start, end)])
    edge = result.edges[0]
    assert edge.kind == "calls"
    name = nodes["g"].properties["callsites"][0]["name"]
    assert edge.resolution.callsite_id == _call_id(nodes["g"], 3, 4, name)


@pytest.mark.parametrize("encoding,column", [(2, 14), (3, 13)])
@pytest.mark.parametrize("encoding_key", ["position_encoding", "positionEncoding"])
def test_scip_unicode_columns_are_converted_before_callsite_matching(
    tmp_path, encoding, column, encoding_key
):
    source = 'def f(): pass\ndef g():\n    é = "😀"; f()\n'
    result, nodes = _import(
        tmp_path, source, [_occurrence(3, column, column + 1)], encoding=encoding
    )
    # Also exercise protobuf JSON's camelCase field and symbolic enum value.
    if encoding_key == "positionEncoding":
        path = tmp_path / "precision.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        document = payload["documents"][0]
        document[encoding_key] = (
            "UTF16CodeUnitOffsetFromLineStart"
            if encoding == 2
            else "UTF32CodeUnitOffsetFromLineStart"
        )
        del document["position_encoding"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = PreciseReferenceAnalyzer(tmp_path, path).analyze(list(nodes.values()))
    edge = result.edges[0]
    assert edge.evidence[0].column == 17
    assert edge.resolution.callsite_id == _call_id(nodes["g"], 3, 17)
    # The UTF-8 spelling is the same position, and needs no unit conversion.
    direct, _ = _import(tmp_path, source, [_occurrence(3, 17, 18)], encoding=1)
    assert direct.edges[0].evidence[0].column == edge.evidence[0].column


@pytest.mark.parametrize("encoding", [None, 0, 99])
def test_scip_ambiguous_unicode_encoding_is_disclosed_and_not_imported(
    tmp_path, encoding
):
    result, _ = _import(
        tmp_path,
        'def f(): pass\ndef g():\n    é = "😀"; f()\n',
        [_occurrence(3, 13, 14)],
        encoding=encoding,
    )
    assert result.edges == []
    assert result.status == "partial"
    assert any(w.kind == "scip_position_unmappable" for w in result.warnings)
    assert result.metrics["scip_unresolved_occurrences"] == 1


def test_scip_zero_column_is_preserved_in_evidence(tmp_path):
    result, _ = _import(
        tmp_path,
        "def f(): pass\ndef g():\n    value = (\nf\n    )\n",
        [_occurrence(4, 0, 1)],
    )
    assert result.edges[0].source != result.edges[0].target
    assert result.edges[0].evidence[0].column == 0


def test_scip_does_not_promote_argument_reference_to_call(tmp_path):
    result, _ = _import(
        tmp_path,
        "def f(): pass\ndef g():\n    f(f)\n",
        [_occurrence(3, 6, 7)],
    )
    assert result.edges[0].kind == "references"
    assert result.edges[0].resolution.callsite_id is None


def test_scip_multiline_attribute_alias_matches_terminal_token(tmp_path):
    result, nodes = _import(
        tmp_path,
        "def f(): pass\ndef g():\n    (obj\n        .alias)()\n",
        [_occurrence(4, 9, 14)],
    )
    assert result.edges[0].kind == "calls"
    assert result.edges[0].resolution.callsite_id == _call_id(
        nodes["g"], 3, 4, "obj.alias"
    )


def test_precision_line_only_ambiguous_call_does_not_pick_first(tmp_path):
    result, _ = _import(
        tmp_path,
        "def f(): pass\ndef g():\n    f(); f()\n",
        [{"source": "fn:demo.g", "target": "fn:demo.f", "kind": "call", "line": 3}],
        backend="pyright",
    )
    assert result.edges[0].resolution.callsite_id is None
    assert any(w.kind == "pyright_callsite_unmatched" for w in result.warnings)
    assert result.status == "partial"


def test_scip_range_with_wrong_callee_end_stays_reference(tmp_path):
    source = "def f(): pass\ndef g():\n    f()\n"
    # The range includes '('; sharing the callee's start is insufficient.
    result, _ = _import(tmp_path, source, [_occurrence(3, 4, 6)])
    assert result.edges[0].kind == "references"
    assert result.edges[0].resolution.callsite_id is None
    exact, nodes = _import(tmp_path, source, [_occurrence(3, 4, 5)])
    assert exact.edges[0].kind == "calls"
    assert exact.edges[0].resolution.callsite_id == _call_id(nodes["g"], 3, 4)


def test_precision_column_without_line_does_not_match_unique_named_call(tmp_path):
    source = "def f(): pass\ndef g():\n    f()\n"
    record = {
        "source": "fn:demo.g",
        "target": "fn:demo.f",
        "kind": "call",
        "column": 4,
    }
    result, _ = _import(tmp_path, source, [record], backend="pyright")
    assert result.edges[0].resolution.callsite_id is None
    assert any(w.kind == "pyright_callsite_unmatched" for w in result.warnings)
    assert result.status == "partial"
    exact, nodes = _import(tmp_path, source, [dict(record, line=3)], backend="pyright")
    assert exact.edges[0].resolution.callsite_id == _call_id(nodes["g"], 3, 4)


@pytest.mark.parametrize(
    "encoding,range_value",
    [
        (1, [2, 4]),
        (1, [2, -1, 5]),
        (1, [2, 5, 4]),
        (1, [2, 5, 99]),
        (1, [2, 5, 6]),  # Inside the UTF-8 bytes of é.
        (2, [2, 10, 11]),  # Inside the UTF-16 surrogate pair for 😀.
        (3, [99, 0, 1]),
        ([], [2, 13, 14]),
    ],
)
def test_scip_invalid_positions_are_disclosed_without_confirmed_edges(
    tmp_path, encoding, range_value
):
    record = {"source": "fn:demo.g", "symbol": "fn:demo.f", "range": range_value}
    result, _ = _import(
        tmp_path,
        'def f(): pass\ndef g():\n    é = "😀"; f()\n',
        [record],
        encoding=encoding,
    )
    assert result.edges == []
    assert result.status == "partial"
    assert result.warnings[0].kind == "scip_position_unmappable"


def test_scip_unavailable_source_for_conversion_is_disclosed(tmp_path):
    source = 'def f(): pass\ndef g():\n    é = "😀"; f()\n'
    _, nodes = _import(tmp_path, source, [_occurrence(3, 14, 15)], encoding=2)
    (tmp_path / "demo.py").unlink()
    result = PreciseReferenceAnalyzer(tmp_path, tmp_path / "precision.json").analyze(
        list(nodes.values())
    )
    assert result.edges == []
    assert result.status == "partial"
    assert "unavailable" in result.warnings[0].message


def test_scip_unicode_line_separator_inside_string_is_not_a_source_newline(tmp_path):
    source = 'def f(): pass\ndef g():\n    s = "\u2028😀"; f()\n'
    result, nodes = _import(tmp_path, source, [_occurrence(3, 15, 16)], encoding=2)
    assert result.edges[0].evidence[0].column == 19
    assert result.edges[0].resolution.callsite_id == _call_id(nodes["g"], 3, 19)
