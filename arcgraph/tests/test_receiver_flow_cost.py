"""Flow facts avoid repeated whole-function scans and unused type lookups."""

from types import SimpleNamespace

import pytest

from arcgraph.analyzers import types as type_analysis
from arcgraph.analyzers.calls.context import _CallResolutionContext
from arcgraph.tests.test_flow_attribute_chains import _calls


def test_guard_loop_discovery_walks_each_function_once(tmp_path, monkeypatch):
    active = False
    scans = []
    walk = type_analysis.ast.walk
    record = type_analysis.TypeRefAnalyzer._record_receiver_narrowings

    def counted_walk(node):
        if active and isinstance(node, type_analysis.ast.FunctionDef):
            scans.append(node.name)
        return walk(node)

    def measured_record(self, node, context):
        nonlocal active
        active = True
        try:
            return record(self, node, context)
        finally:
            active = False

    monkeypatch.setattr(type_analysis.ast, "walk", counted_walk)
    monkeypatch.setattr(
        type_analysis.TypeRefAnalyzer, "_record_receiver_narrowings", measured_record
    )
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path | None):\n"
        "    if p is not None:\n        p.parent.mkdir()\n"
        "    if p is not None:\n        p.parent.mkdir()\n"
        "    if p is not None:\n        p.parent.mkdir()\n",
    )
    assert calls["p.parent.mkdir()"] == {"extsym:pathlib.Path.mkdir"}
    assert scans.count("use") <= 1


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"name": "other", "range": [2, 0, 8, 99]}],
        [{"name": "value", "range": [2, 0, 3, 99]}],
    ],
)
def test_irrelevant_guard_never_reads_receiver_type(entries, monkeypatch):
    context = _CallResolutionContext([])
    source = SimpleNamespace(id="fn:use", properties={"receiver_narrowings": entries})
    context.callsite_position = (source.id, 6, 4)

    def forbidden_lookup(*args):
        assert False, "A nonapplicable guard requested a receiver type"

    monkeypatch.setattr(context, "type_ref_at", forbidden_lookup)
    assert context.narrowed_type_ref(source, "value") is None
