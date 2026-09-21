from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer


def test_typescript_call_edges_preserve_how_each_callsite_invokes_target(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for TypeScript call fact tests.")
    source = tmp_path / "src" / "service.ts"
    source.parent.mkdir()
    source.write_text(
        """
export async function target(...values: number[]): Promise<number> {
  return values.length;
}

export async function caller(values: number[]): Promise<number> {
  target();
  const assigned = target(1);
  await target(1, 2);
  await target(...values);
  return target(assigned);
}
""".strip() + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    ArcGraphIndexer(tmp_path, output, [SourceRoot("src")]).build()
    reader = GraphStoreReader.from_current(output)
    if any(
        warning.kind == "typescript_frontend_unavailable"
        for warning in reader.read_warnings()
    ):
        pytest.skip("TypeScript compiler API is unavailable.")

    edge = next(
        item
        for item in reader.read_edges()
        if item.source == "fn:service.caller"
        and item.target == "fn:service.target"
        and item.kind == "calls"
    )
    facts = edge.properties["callsites"]

    assert len(facts) == 5
    assert [fact["argument_count"] for fact in facts] == [0, 1, 2, None, 1]
    assert [fact["awaited"] for fact in facts] == [False, False, True, True, False]
    assert [fact["return_value_usage"] for fact in facts] == [
        "discarded",
        "assigned",
        "discarded",
        "discarded",
        "returned",
    ]
    assert facts[3]["has_spread_argument"] is True
    assert facts[3]["argument_count_known"] is False
    assert all(fact["path"] == "src/service.ts" and fact["line"] for fact in facts)

    payload = QueryEngine(output).callsites("service.caller")
    typescript_calls = [
        item for item in payload["callsites"] if item["context"] == "typescript_call"
    ]
    assert len(typescript_calls) == 5
    assert {item["argument_count"] for item in typescript_calls} == {None, 0, 1, 2}


def test_typescript_call_facts_bound_expressions_and_classify_parent_role(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for TypeScript call fact tests.")
    long_argument = "x" * 800
    source = tmp_path / "src" / "usage.ts"
    source.parent.mkdir()
    source.write_text(
        f"""
export function target(value: unknown): number {{ return 1; }}
function consume(value: unknown): void {{ void value; }}
export function caller(flag: boolean): number {{
  const arithmetic = 1 + target(1);
  const branch = flag ? target(2) : 0;
  consume(target(3));
  target({long_argument!r});
  return arithmetic + branch;
}}
""".strip() + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    ArcGraphIndexer(tmp_path, output, [SourceRoot("src")]).build()
    reader = GraphStoreReader.from_current(output)
    if any(
        warning.kind == "typescript_frontend_unavailable"
        for warning in reader.read_warnings()
    ):
        pytest.skip("TypeScript compiler API is unavailable.")

    edge = next(
        item
        for item in reader.read_edges()
        if item.source == "fn:usage.caller"
        and item.target == "fn:usage.target"
        and item.kind == "calls"
    )
    facts = edge.properties["callsites"]

    assert [fact["return_value_usage"] for fact in facts] == [
        "used",
        "used",
        "passed_as_argument",
        "discarded",
    ]
    assert max(len(fact["raw_expression"]) for fact in facts) == 512
    assert facts[-1]["raw_expression_truncated"] is True
    assert all(len(fact["callee_expression"]) <= 512 for fact in facts)


def test_truncated_call_expression_stays_utf8_encodable(tmp_path: Path) -> None:
    """A 512-char truncation must never split a surrogate pair: the stored
    expression has to survive UTF-8 encoding on every read surface."""

    if shutil.which("node") is None:
        pytest.skip("Node.js is required for TypeScript call-fact tests.")
    padding = "a" * 500
    source = tmp_path / "src" / "wide.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        "export function sink(value: string): string { return value; }\n"
        "export function caller(): string {\n"
        f'  return sink("{padding}\U0001f600{padding}");\n'
        "}\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    ArcGraphIndexer(tmp_path, output, [SourceRoot("src")]).build()
    if any(
        warning.kind == "typescript_frontend_unavailable"
        for warning in GraphStoreReader.from_current(output).read_warnings()
    ):
        pytest.skip("TypeScript compiler API is unavailable.")

    payload = QueryEngine(output).callsites("wide.caller")
    for record in payload["callsites"]:
        expression = record.get("raw_expression") or ""
        # Round-trips through UTF-8 exactly when no lone surrogate survived.
        assert expression.encode("utf-8").decode("utf-8") == expression
    json.dumps(payload, ensure_ascii=False)
