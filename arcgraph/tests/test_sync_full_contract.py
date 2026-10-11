"""C09: incremental publication equals a full build, field by field."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.tests.typescript_acceptance_support import skip_or_fail

SOURCES = {
    "provider.py": "class Plain: pass\nclass Base:\n    def run(self): return 1\nclass Child(Base): pass\n",
    "exports.py": "from provider import Child as Public\n",
    "test_consumer.py": "from provider import Plain\nfrom exports import Public\ndef test_guard(value):\n    if isinstance(value, Plain):\n        return value\ndef consume(value: Public):\n    return value.run()\n",
    "unrelated.py": "VALUE = 1\n",
    "frontend/provider.ts": "export function run() { return 1; }\n",
    "frontend/exports.ts": 'export { run } from "./provider";\n',
    "frontend/consumer.ts": 'import { run } from "./exports";\nimport { idle, keep } from "external-kit";\nexport function consume() { return run(); }\n',
    "frontend/shared.ts": 'import { keep } from "external-kit";\nexport function shared() { return keep(); }\n',
    "frontend/remove.ts": 'import { unused } from "delete-kit";\nexport const value = 1;\n',
}
EDITS = {
    "py": ("unrelated.py", "VALUE = 2\n"),
    "pyadd": ("added.py", "from provider import Plain\ndef added(): return Plain()\n"),
    "pydel": ("unrelated.py", None),
    "export": ("exports.py", "from provider import Base as Public\n"),
    "base": ("provider.py", SOURCES["provider.py"].replace("Child(Base)", "Child")),
    "ts": ("frontend/consumer.ts", SOURCES["frontend/consumer.ts"] + "// changed\n"),
    "tsadd": (
        "frontend/added.ts",
        'import { extra } from "add-kit";\nexport const added = 1;\n',
    ),
    "tsdel": ("frontend/remove.ts", None),
    "tsimport": (
        "frontend/consumer.ts",
        SOURCES["frontend/consumer.ts"].replace("idle, keep", "keep"),
    ),
    "tsexport": (
        "frontend/exports.ts",
        'export { run as renamed } from "./provider";\n',
    ),
}


def _strip_volatile(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key not in {"created_at", "index_version"}
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _snapshot(output: Path) -> dict[str, list[object]]:
    current = json.loads((output / "current.json").read_text(encoding="utf-8"))
    build = output / current["build_dir"]
    return {
        table: sorted(
            (
                _strip_volatile(json.loads(line))
                for line in (build / f"{table}.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ),
            key=lambda row: json.dumps(row, sort_keys=True),
        )
        for table in ("nodes", "edges", "semantic_facts")
    }


@pytest.mark.parametrize("kind", EDITS)
def test_sync_matches_full_field_by_field(tmp_path: Path, kind: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, source in SOURCES.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    roots = [SourceRoot(".")]
    incremental = tmp_path / "incremental"
    ArcGraphIndexer(repo, incremental, roots).build()
    reader = GraphStoreReader.from_current(incremental)
    if any(w.kind == "typescript_frontend_unavailable" for w in reader.read_warnings()):
        skip_or_fail("C09 requires the TypeScript compiler API.")
    assert any(n.id == "extsym:external-kit.idle" for n in reader.read_nodes())
    assert any(n.properties.get("receiver_narrowings") for n in reader.read_nodes())

    name, source = EDITS[kind]
    path = repo / name
    if source is None:
        path.unlink()
    else:
        path.write_text(source, encoding="utf-8")
    result = ArcGraphReindexer(repo, incremental, roots).reindex_changed()
    assert result["status"] == "reindexed"
    full = tmp_path / "full"
    ArcGraphIndexer(repo, full, roots).build()

    actual, expected = _snapshot(incremental), _snapshot(full)
    for table in expected:
        assert actual[table] == expected[table], (kind, table)
