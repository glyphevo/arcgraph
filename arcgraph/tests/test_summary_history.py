"""Summary content and samples describe the input, not the incremental history."""

import json
import shutil

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer

NOISE = {
    "created_at",
    "index_version",
    "last_seen_index",
    "first_seen_index",
    "diagnostic_id",
    "phase_timings",
    "evidence_manifest",
    "generated_at",
}


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k not in NOISE}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def summary(output):
    return normalized(GraphStoreReader.from_current(output).metadata)


@pytest.mark.parametrize("field", ["adapter_metrics", "diagnostics"])
def test_deleted_pytest_file_summary_matches_same_history_full(tmp_path, field):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "conftest.py").write_text(
        "import pytest\n@pytest.fixture\ndef value():\n    return 1\n", encoding="utf-8"
    )
    for name in ("one", "two"):
        (tests / f"test_{name}.py").write_text(
            f"def test_{name}(value):\n    assert value == 1\n", encoding="utf-8"
        )
    (root / "widget.vue").write_text(
        '<script setup lang="ts">const title = "ok";</script>\n'
        "<template><p>{{ title }}</p></template>\n",
        encoding="utf-8",
    )
    output = tmp_path / "sync"
    reference = tmp_path / "reference"
    roots = [SourceRoot(".")]
    ArcGraphIndexer(root, output, roots).build()
    shutil.copytree(output, reference)
    (tests / "test_one.py").unlink()
    ArcGraphReindexer(root, output, roots).reindex_changed()
    ArcGraphIndexer(root, reference, roots).build()
    assert summary(output)[field] == summary(reference)[field]


def test_warning_file_edit_summary_order_and_samples_match_full(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name in ("a", "c"):
        (root / f"{name}.py").write_text(f"def broken_{name}(\n", encoding="utf-8")
    (root / "calls.py").write_text(
        "def exercise():\n" + "".join(f"    missing_{i:03d}()\n" for i in range(210)),
        encoding="utf-8",
    )
    output = tmp_path / "sync"
    reference = tmp_path / "reference"
    roots = [SourceRoot(".")]
    ArcGraphIndexer(root, output, roots).build()
    shutil.copytree(output, reference)
    (root / "a.py").write_text(
        "def broken_a(\n\n# edit with the same warning\n", encoding="utf-8"
    )
    ArcGraphReindexer(root, output, roots).reindex_changed()
    ArcGraphIndexer(root, reference, roots).build()
    actual = summary(output)
    expected = summary(reference)
    assert actual["summary_truncation"]["diagnostics"] > 0
    assert actual == expected


def test_workspace_resolve_merges_structured_analysis_warnings(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "same"\n', encoding="utf-8")
    (root / "broken.py").write_text("def incomplete(\n", encoding="utf-8")
    ArcGraphIndexer(root, root / "output/arcgraph", [SourceRoot(".")]).build()
    manifest = tmp_path / "arcgraph.workspace.toml"
    manifest.write_text(
        'schema_version = "1.0"\nname = "probe"\n[[repositories]]\nid = "same"\npath = "project"\n',
        encoding="utf-8",
    )
    result = main(["workspace", "resolve", "same", "--config", str(manifest)])
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "matched"
    assert any(
        w.get("kind") == "parse_error"
        for w in payload["matches"][0]["warnings"]
        if isinstance(w, dict)
    )
