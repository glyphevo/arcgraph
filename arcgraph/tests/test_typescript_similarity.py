from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from arcgraph.tests.typescript_acceptance_support import skip_or_fail
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.pipeline.typescript_frontend import (
    TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM,
    TypeScriptGraphAnalyzer,
)


def _command_source(
    command_name: str,
    function_name: str,
    *,
    extra_body: str = "",
) -> str:
    return f"""
import {{ checkRule, finishCommand }} from "../support.js";

export function {function_name}(registry: any, dependencies: any): void {{
  registry.addCommand(
    "{command_name}",
    {{ annotations: {{ readOnlyHint: true }} }},
    async () => {{
      const decision = checkRule("{command_name}");
      return finishCommand(dependencies, decision);
    }}
  );
{extra_body}
}}
"""


def _build_similarity_repo(tmp_path: Path) -> tuple[Path, Path]:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    files = {
        "src/support.ts": """
export function checkRule(name: string) { return name; }
export function finishCommand(dependencies: any, decision: string) {
  return { dependencies, decision };
}
""",
        "src/commands/alpha.ts": _command_source(
            "alpha.inspect",
            "configureAlphaCommands",
            extra_body="""
  if (dependencies.enabled) {
    for (const candidate of dependencies.candidates ?? []) {
      registry.addCommand(candidate.name, candidate.schema, async () => {
        const result = await candidate.run();
        return { content: [{ type: "text", text: String(result) }] };
      });
    }
  }
  try {
    dependencies.observe("alpha.inspect");
  } catch (error) {
    dependencies.report(error);
  }
""",
        ),
        "src/commands/beta.ts": _command_source("beta.list", "configureBetaCommands"),
        "src/commands/gamma.ts": _command_source(
            "gamma.read", "configureGammaCommands"
        ),
        "src/elsewhere/delta.ts": _command_source(
            "delta.list",
            "configureDeltaCommands",
        ),
        "src/commands/unrelated.ts": """
export function calculateRange(start: number, end: number): number {
  let total = 0;
  for (let index = start; index < end; index += 1) total += index;
  return total;
}
""",
    }
    for relative_path, source in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source.strip() + "\n", encoding="utf-8")

    output_dir = tmp_path / "arcgraph-output"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    if any(
        warning.kind == "typescript_frontend_unavailable"
        for warning in reader.read_warnings()
    ):
        skip_or_fail("TypeScript compiler API is unavailable in this environment.")
    return tmp_path, output_dir


def test_typescript_similarity_profiles_and_edges_are_queryable(tmp_path: Path) -> None:
    _repo_root, output_dir = _build_similarity_repo(tmp_path)
    reader = GraphStoreReader.from_current(output_dir)
    nodes = {node.id: node for node in reader.read_nodes()}
    edges = reader.read_edges()

    profile = nodes["fn:commands.alpha.configureAlphaCommands"].properties["similarity"]
    assert profile["algorithm"] == TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM
    assert (
        reader.metadata["extractor_metadata"]["typescript-static"][
            "similarity_profile_algorithm"
        ]
        == TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM
    )
    assert profile["ngram_hashes"]
    assert all(isinstance(value, int) for value in profile["ngram_hashes"])
    similar_pairs = {
        frozenset((edge.source, edge.target))
        for edge in edges
        if edge.kind == "similar_to"
    }
    assert (
        frozenset(
            (
                "fn:commands.alpha.configureAlphaCommands",
                "fn:commands.beta.configureBetaCommands",
            )
        )
        in similar_pairs
    )
    assert not any(
        "fn:commands.unrelated.calculateRange" in pair for pair in similar_pairs
    )

    result = QueryEngine(output_dir).similar("commands.alpha.configureAlphaCommands")
    similar_ids = {item["node"]["id"] for item in result["similar"]}
    assert {
        "fn:commands.beta.configureBetaCommands",
        "fn:commands.gamma.configureGammaCommands",
    } <= similar_ids
    assert "fn:elsewhere.delta.configureDeltaCommands" not in similar_ids
    assert all(
        "similarity" not in item["node"]["properties"] for item in result["similar"]
    )
    family = result["pattern_family"]
    assert family["member_count"] >= 3
    assert family["modified_member_count"] == 0
    assert family["only_one_member_modified"] is False
    assert all("low_information" in member for member in family["members"])
    assert all("information_quality" in item for item in result["similar"])

    beta = next(
        item
        for item in result["similar"]
        if item["node"]["id"] == "fn:commands.beta.configureBetaCommands"
    )
    reasons = beta["edge"]["properties"]["reasons"]
    jaccard_reason = next(
        reason for reason in reasons if reason.startswith("token_jaccard=")
    )
    jaccard = float(jaccard_reason.split("=", 1)[1])
    assert jaccard < 0.82
    assert any(reason.startswith("token_containment=") for reason in reasons)

    gamma_path = _repo_root / "src" / "commands" / "gamma.ts"
    gamma_path.write_text(
        gamma_path.read_text(encoding="utf-8") + "// pending change\n",
        encoding="utf-8",
    )
    changed_family = QueryEngine(output_dir).similar(
        "commands.alpha.configureAlphaCommands"
    )["pattern_family"]
    assert changed_family["modified_member_count"] == 1
    assert changed_family["only_one_member_modified"] is True
    assert changed_family["modified_members"][0]["path"] == "src/commands/gamma.ts"


def test_typescript_incremental_reindex_reuses_unchanged_similarity_profiles(
    tmp_path: Path,
) -> None:
    repo_root, output_dir = _build_similarity_repo(tmp_path)
    beta_id = "fn:commands.beta.configureBetaCommands"
    before_nodes = {
        node.id: node for node in GraphStoreReader.from_current(output_dir).read_nodes()
    }
    before_targets = before_nodes[beta_id].properties["similarity"]["call_targets"]
    assert before_targets == [
        "fn:support.checkRule",
        "fn:support.finishCommand",
    ]
    beta_path = repo_root / "src" / "commands" / "beta.ts"
    beta_path.write_text(
        beta_path.read_text(encoding="utf-8").replace(
            "beta.list",
            "beta.list_runs",
        ),
        encoding="utf-8",
    )

    result = ArcGraphReindexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).reindex_changed()

    assert result["status"] == "reindexed"
    after_nodes = {
        node.id: node for node in GraphStoreReader.from_current(output_dir).read_nodes()
    }
    assert (
        after_nodes[beta_id].properties["similarity"]["call_targets"] == before_targets
    )
    similar = QueryEngine(output_dir).similar("commands.beta.configureBetaCommands")
    assert "fn:commands.alpha.configureAlphaCommands" in {
        item["node"]["id"] for item in similar["similar"]
    }


def test_typescript_incremental_reindex_rewrites_dead_profile_targets(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    support = tmp_path / "src" / "util" / "support.ts"
    support.parent.mkdir(parents=True)
    support.write_text(
        """
export function checkRule(value: string): string { return value; }
export function otherRule(value: string): string { return value; }
""".strip() + "\n",
        encoding="utf-8",
    )
    core = tmp_path / "src" / "core"
    core.mkdir()
    (core / "p.ts").write_text(
        """
import { checkRule, otherRule } from "../util/support.js";
export function inspectP(value: string): string {
  return otherRule(checkRule(value));
}
""".strip() + "\n",
        encoding="utf-8",
    )
    (core / "q.ts").write_text(
        """
import { checkRule } from "../util/support.js";
export function inspectQ(value: string): string {
  return checkRule(value);
}
""".strip() + "\n",
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(tmp_path, incremental_output, roots).build()

    support.write_text(
        """
export function checkPolicy(value: string): string { return value; }
export function otherPolicy(value: string): string { return value; }
""".strip() + "\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(tmp_path, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(tmp_path, full_output, roots).build()

    def profiles(output: Path) -> dict[str, dict[str, object]]:
        return {
            node.id: node.properties["similarity"]
            for node in GraphStoreReader.from_current(output).read_nodes()
            if node.id in {"fn:core.p.inspectP", "fn:core.q.inspectQ"}
        }

    incremental_profiles = profiles(incremental_output)
    assert incremental_profiles == profiles(full_output)
    assert incremental_profiles["fn:core.p.inspectP"]["call_targets"] == [
        "mod:util.support"
    ]
    assert incremental_profiles["fn:core.q.inspectQ"]["call_targets"] == [
        "mod:util.support"
    ]

    def similar_edges(output: Path) -> list[tuple[str, str, object]]:
        return [
            (edge.source, edge.target, edge.properties.get("score"))
            for edge in GraphStoreReader.from_current(output).read_edges()
            if edge.kind == "similar_to"
            and {edge.source, edge.target}
            == {"fn:core.p.inspectP", "fn:core.q.inspectQ"}
        ]

    assert similar_edges(incremental_output) == similar_edges(full_output)


def test_typescript_incremental_reindex_does_not_fallback_to_parent_module(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    source_dir = tmp_path / "src"
    support = source_dir / "util" / "support.ts"
    support.parent.mkdir(parents=True)
    support.write_text(
        "export function checkRule(value: string): string { return value; }\n",
        encoding="utf-8",
    )
    (source_dir / "util" / "support.py").write_text(
        "def python_only() -> str:\n    return 'python'\n",
        encoding="utf-8",
    )
    (source_dir / "util.ts").write_text(
        "export function parentOnly(): string { return 'parent'; }\n",
        encoding="utf-8",
    )
    core_dir = source_dir / "core"
    core_dir.mkdir()
    caller = core_dir / "caller.ts"
    caller.write_text(
        'import { checkRule } from "../util/support.js";\n'
        "export function inspect(value: string): string { return checkRule(value); }\n",
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(tmp_path, incremental_output, roots).build()

    support.unlink()
    ArcGraphReindexer(tmp_path, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(tmp_path, full_output, roots).build()

    def call_targets(output: Path) -> list[str]:
        node = next(
            node
            for node in GraphStoreReader.from_current(output).read_nodes()
            if node.id == "fn:core.caller.inspect"
        )
        return node.properties["similarity"]["call_targets"]

    assert call_targets(incremental_output) == []
    assert call_targets(incremental_output) == call_targets(full_output)


def test_vue_module_context_does_not_change_incremental_import_resolution(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "Widget.vue").write_text(
        "<template><main>Widget</main></template>\n",
        encoding="utf-8",
    )
    main = source_dir / "main.ts"
    main.write_text(
        'import { widgetHelper } from "./Widget.vue";\n'
        "export function inspect() { return widgetHelper(); }\n",
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(tmp_path, incremental_output, roots).build()
    main.write_text(
        'import { widgetHelper } from "./Widget.vue";\n'
        "export function inspect() { return widgetHelper(); }\n"
        "// changed\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(tmp_path, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(tmp_path, full_output, roots).build()

    def relevant_edges(output: Path) -> set[tuple[str, str, str]]:
        return {
            (edge.source, edge.kind, edge.target)
            for edge in GraphStoreReader.from_current(output).read_edges()
            if edge.source in {"mod:main", "fn:main.inspect"}
        }

    assert relevant_edges(incremental_output) == relevant_edges(full_output)
    assert not any(
        target == "mod:Widget"
        for _source, _kind, target in relevant_edges(incremental_output)
    )


def test_typescript_incremental_reindex_does_not_restore_removed_features(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    source = tmp_path / "src" / "features.ts"
    source.parent.mkdir()
    source.write_text(
        """
function helper(): void {}
export function inspect(enabled: boolean): string {
  helper();
  return localStorage.getItem("token") || String(enabled);
}
""".strip() + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph-output"
    roots = [SourceRoot("src")]
    ArcGraphIndexer(tmp_path, output_dir, roots).build()
    node_id = "fn:features.inspect"
    before = {
        node.id: node for node in GraphStoreReader.from_current(output_dir).read_nodes()
    }[node_id].properties["similarity"]
    assert before["call_targets"] == ["fn:features.helper"]
    assert before["resource_targets"] == ["config:browser_storage:localStorage:token"]

    source.write_text(
        """
function helper(): void {}
export function inspect(enabled: boolean): string {
  return String(enabled);
}
""".strip() + "\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(tmp_path, output_dir, roots).reindex_changed()

    after = {
        node.id: node for node in GraphStoreReader.from_current(output_dir).read_nodes()
    }[node_id].properties["similarity"]
    assert after["call_targets"] == []
    assert after["resource_targets"] == []


@pytest.mark.parametrize(
    ("support_source", "import_line"),
    [
        (
            "export function remote(): string { return 'remote'; }",
            'import { remote } from "./support.js";',
        ),
        (
            "export function remote(): string { return 'remote'; }",
            'const { remote } = require("./support.js");',
        ),
        (
            "const remote = (): string => 'remote'; export default remote;",
            'import remote from "./support.js";',
        ),
        (
            """
export function remote(): string { return 'remote'; }
export class Service { remote(): string { return 'method'; } }
""",
            'import { remote } from "@/support";',
        ),
    ],
)
def test_typescript_incremental_reindex_reconciles_unresolved_import_calls(
    tmp_path: Path,
    support_source: str,
    import_line: str,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    (tmp_path / "vite.config.ts").write_text(
        'export default { resolve: { alias: { "@": "./src" } } };\n',
        encoding="utf-8",
    )
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "support.ts").write_text(
        support_source + "\n",
        encoding="utf-8",
    )
    main = source_dir / "main.ts"
    main.write_text(
        f"""
{import_line}
export function inspect(enabled: boolean): string {{
  remote();
  return String(enabled);
}}
""".strip() + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph-output"
    roots = [SourceRoot("src")]
    ArcGraphIndexer(tmp_path, output_dir, roots).build()
    node_id = "fn:main.inspect"
    before = {
        node.id: node for node in GraphStoreReader.from_current(output_dir).read_nodes()
    }[node_id].properties["similarity"]
    assert before["call_targets"] == ["fn:support.remote"]

    main.write_text(
        f"""
{import_line}
export function inspect(enabled: boolean): string {{
  remote();
  return enabled ? "yes" : "no";
}}
""".strip() + "\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(tmp_path, output_dir, roots).reindex_changed()
    retained_reader = GraphStoreReader.from_current(output_dir)
    retained = {node.id: node for node in retained_reader.read_nodes()}[
        node_id
    ].properties["similarity"]
    assert retained["call_targets"] == ["fn:support.remote"]
    assert any(
        edge.kind == "imports"
        and edge.source == "mod:main"
        and edge.target == "mod:support"
        for edge in retained_reader.read_edges()
    )

    main.write_text(
        f"""
{import_line}
export function inspect(enabled: boolean): string {{
  return String(enabled);
}}
""".strip() + "\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(tmp_path, output_dir, roots).reindex_changed()

    reader = GraphStoreReader.from_current(output_dir)
    after = {node.id: node for node in reader.read_nodes()}[node_id].properties[
        "similarity"
    ]
    assert after["call_targets"] == []
    assert not any(
        edge.kind == "calls"
        and edge.source == node_id
        and edge.target == "fn:support.remote"
        for edge in reader.read_edges()
    )


def test_typescript_overloads_do_not_emit_self_similarity_edges(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    source = tmp_path / "src" / "parse.ts"
    source.parent.mkdir()
    source.write_text(
        """
export function parse(value: string): string;
export function parse(value: number): number;
export function parse(value: string | number): string | number {
  return value;
}
""".strip() + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph-output"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    if any(
        warning.kind == "typescript_frontend_unavailable"
        for warning in reader.read_warnings()
    ):
        skip_or_fail("TypeScript compiler API is unavailable in this environment.")

    assert not any(
        edge.kind == "similar_to" and edge.source == edge.target
        for edge in reader.read_edges()
    )


def test_typescript_reindex_rejects_incompatible_similarity_profiles(
    tmp_path: Path,
) -> None:
    repo_root, output_dir = _build_similarity_repo(tmp_path)
    pointer_before = json.loads(
        (output_dir / "current.json").read_text(encoding="utf-8")
    )
    sqlite_path = output_dir / pointer_before["build_dir"] / "index.sqlite"
    with sqlite3.connect(sqlite_path) as conn:
        rows = conn.execute("SELECT id, properties_json FROM nodes").fetchall()
        for node_id, properties_json in rows:
            properties = json.loads(properties_json)
            profile = properties.get("similarity")
            if not isinstance(profile, dict):
                continue
            profile["algorithm"] = "typescript_syntax_kind_ngrams_v2"
            conn.execute(
                "UPDATE nodes SET properties_json = ? WHERE id = ?",
                (json.dumps(properties, sort_keys=True), node_id),
            )

    beta_path = repo_root / "src" / "commands" / "beta.ts"
    beta_path.write_text(
        beta_path.read_text(encoding="utf-8").replace(
            "beta.list",
            "beta.list_runs",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="full ArcGraph build"):
        ArcGraphReindexer(
            repo_root=repo_root,
            output_dir=output_dir,
            source_roots=[SourceRoot("src")],
        ).reindex_changed()

    assert (
        json.loads((output_dir / "current.json").read_text(encoding="utf-8"))
        == pointer_before
    )


@pytest.mark.parametrize("change_kind", ["modify", "delete"])
def test_typescript_reindex_restores_missing_similarity_metadata(
    tmp_path: Path,
    change_kind: str,
) -> None:
    repo_root, output_dir = _build_similarity_repo(tmp_path)
    pointer_before = json.loads(
        (output_dir / "current.json").read_text(encoding="utf-8")
    )
    summary_path = output_dir / pointer_before["summary_path"]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.get("extractor_metadata", {}).pop("typescript-static", None)
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    beta_path = repo_root / "src" / "commands" / "beta.ts"
    if change_kind == "delete":
        beta_path.unlink()
    else:
        beta_path.write_text(
            beta_path.read_text(encoding="utf-8").replace(
                "beta.list",
                "beta.list_runs",
            ),
            encoding="utf-8",
        )

    result = ArcGraphReindexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).reindex_changed()

    assert result["status"] == "reindexed"
    assert (
        json.loads((output_dir / "current.json").read_text(encoding="utf-8"))
        != pointer_before
    )
    recovered = GraphStoreReader.from_current(output_dir)
    assert (
        recovered.metadata["extractor_metadata"]["typescript-static"][
            "similarity_profile_algorithm"
        ]
        == TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM
    )


def test_typescript_reindex_rejects_pre_similarity_index(tmp_path: Path) -> None:
    repo_root, output_dir = _build_similarity_repo(tmp_path)
    pointer_before = json.loads(
        (output_dir / "current.json").read_text(encoding="utf-8")
    )
    summary_path = output_dir / pointer_before["summary_path"]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.get("extractor_metadata", {}).pop("typescript-static", None)
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    sqlite_path = output_dir / pointer_before["build_dir"] / "index.sqlite"
    removed_profiles = 0
    with sqlite3.connect(sqlite_path) as conn:
        rows = conn.execute("SELECT id, kind, properties_json FROM nodes").fetchall()
        for node_id, kind, properties_json in rows:
            if kind not in {"function", "method"}:
                continue
            properties = json.loads(properties_json)
            if properties.get("frontend_name") != "typescript-static":
                continue
            if properties.pop("similarity", None) is None:
                continue
            removed_profiles += 1
            conn.execute(
                "UPDATE nodes SET properties_json = ? WHERE id = ?",
                (json.dumps(properties, sort_keys=True), node_id),
            )
    assert removed_profiles > 0

    beta_path = repo_root / "src" / "commands" / "beta.ts"
    beta_path.write_text(
        beta_path.read_text(encoding="utf-8").replace("beta.list", "beta.list_runs"),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="cannot verify the previous similarity"):
        ArcGraphReindexer(
            repo_root=repo_root,
            output_dir=output_dir,
            source_roots=[SourceRoot("src")],
        ).reindex_changed()

    assert (
        json.loads((output_dir / "current.json").read_text(encoding="utf-8"))
        == pointer_before
    )


def test_reindex_keeps_profile_count_whole_index_and_still_fails_on_strip(
    tmp_path: Path,
) -> None:
    """The recorded count must describe the index, not the changed batch.

    A change touching no profiled file must not record zero, or the guard that
    rejects a stripped index is silently disarmed on the following run.
    """

    repo_root, output_dir = _build_similarity_repo(tmp_path)

    def recorded_profile_count() -> int:
        store = GraphStoreReader.from_current(output_dir)
        metadata = store.metadata.get("extractor_metadata", {})
        return metadata.get("typescript-static", {}).get("similarity_profile_count")

    def live_profile_count() -> int:
        store = GraphStoreReader.from_current(output_dir)
        return sum(
            1
            for node in store.read_nodes()
            if node.kind in {"function", "method"}
            and node.properties.get("frontend_name") == "typescript-static"
            and isinstance(node.properties.get("similarity"), dict)
        )

    built_count = recorded_profile_count()
    assert built_count > 0
    assert built_count == live_profile_count()

    # A file with no similarity profile of its own.
    unprofiled = repo_root / "src" / "trivial.ts"
    unprofiled.write_text("export function trivial() {}\n", encoding="utf-8")
    ArcGraphReindexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).reindex_changed()
    unprofiled.write_text("export function trivial() {}\n// touch\n", encoding="utf-8")
    ArcGraphReindexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).reindex_changed()

    assert recorded_profile_count() == live_profile_count()
    assert recorded_profile_count() > 0

    # With the count intact, stripping the profiles must still fail closed.
    pointer_before = json.loads(
        (output_dir / "current.json").read_text(encoding="utf-8")
    )
    sqlite_path = output_dir / pointer_before["build_dir"] / "index.sqlite"
    removed = 0
    with sqlite3.connect(sqlite_path) as conn:
        for node_id, kind, properties_json in conn.execute(
            "SELECT id, kind, properties_json FROM nodes"
        ).fetchall():
            if kind not in {"function", "method"}:
                continue
            properties = json.loads(properties_json)
            if properties.get("frontend_name") != "typescript-static":
                continue
            if properties.pop("similarity", None) is None:
                continue
            removed += 1
            conn.execute(
                "UPDATE nodes SET properties_json = ? WHERE id = ?",
                (json.dumps(properties, sort_keys=True), node_id),
            )
    assert removed > 0

    beta_path = repo_root / "src" / "commands" / "beta.ts"
    beta_path.write_text(
        beta_path.read_text(encoding="utf-8").replace("beta.list", "beta.list_runs"),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="cannot verify the previous similarity"):
        ArcGraphReindexer(
            repo_root=repo_root,
            output_dir=output_dir,
            source_roots=[SourceRoot("src")],
        ).reindex_changed()
    assert (
        json.loads((output_dir / "current.json").read_text(encoding="utf-8"))
        == pointer_before
    )


def test_reindex_rejects_partial_similarity_profile_strip(tmp_path: Path) -> None:
    """A recorded profile count must match live profiles exactly, not merely be non-zero."""
    repo_root, output_dir = _build_similarity_repo(tmp_path)
    pointer_before = json.loads(
        (output_dir / "current.json").read_text(encoding="utf-8")
    )
    store = GraphStoreReader.from_current(output_dir)
    recorded = store.metadata["extractor_metadata"]["typescript-static"][
        "similarity_profile_count"
    ]
    assert isinstance(recorded, int) and recorded > 1

    sqlite_path = output_dir / pointer_before["build_dir"] / "index.sqlite"
    removed = 0
    with sqlite3.connect(sqlite_path) as conn:
        for node_id, kind, properties_json in conn.execute(
            "SELECT id, kind, properties_json FROM nodes"
        ).fetchall():
            if kind not in {"function", "method"}:
                continue
            properties = json.loads(properties_json)
            if properties.get("frontend_name") != "typescript-static":
                continue
            if properties.pop("similarity", None) is None:
                continue
            removed += 1
            conn.execute(
                "UPDATE nodes SET properties_json = ? WHERE id = ?",
                (json.dumps(properties, sort_keys=True), node_id),
            )
            break
    assert removed == 1

    beta_path = repo_root / "src" / "commands" / "beta.ts"
    beta_path.write_text(
        beta_path.read_text(encoding="utf-8").replace("beta.list", "beta.list_runs"),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="cannot verify the previous similarity"):
        ArcGraphReindexer(
            repo_root=repo_root,
            output_dir=output_dir,
            source_roots=[SourceRoot("src")],
        ).reindex_changed()
    assert (
        json.loads((output_dir / "current.json").read_text(encoding="utf-8"))
        == pointer_before
    )


def test_full_build_records_similarity_profile_count_after_cross_frontend_dedupe(
    tmp_path: Path,
) -> None:
    """Colliding Python/TS callables must not inflate the published profile count."""
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "service.py").write_text(
        "def process(value):\n"
        "    if value > 10:\n"
        "        return value + 1\n"
        "    return value + 2\n",
        encoding="utf-8",
    )
    (source_root / "service.ts").write_text(
        "export function process(value: number): number {\n"
        "  if (value > 10) {\n"
        "    return value + 1;\n"
        "  }\n"
        "  return value + 2;\n"
        "}\n",
        encoding="utf-8",
    )
    (source_root / "other.ts").write_text(
        "export function other(value: number): number {\n"
        "  if (value > 10) {\n"
        "    return value + 1;\n"
        "  }\n"
        "  return value + 2;\n"
        "}\n",
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(repo_root, output_dir, roots).build()

    store = GraphStoreReader.from_current(output_dir)
    recorded = store.metadata["extractor_metadata"]["typescript-static"][
        "similarity_profile_count"
    ]
    live = sum(
        1
        for node in store.read_nodes()
        if node.kind in {"function", "method"}
        and node.properties.get("frontend_name") == "typescript-static"
        and isinstance(node.properties.get("similarity"), dict)
    )
    assert recorded == live == 1

    (source_root / "other.ts").write_text(
        "export function other(value: number): number {\n"
        "  if (value > 10) {\n"
        "    return value + 1;\n"
        "  }\n"
        "  return value + 3;\n"
        "}\n",
        encoding="utf-8",
    )
    result = ArcGraphReindexer(repo_root, output_dir, roots).reindex_changed()
    assert result["status"] == "reindexed"


def test_typescript_similarity_caps_dense_duplicate_buckets(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    duplicate_count = 30
    for index in range(duplicate_count):
        path = tmp_path / "src" / "duplicates" / f"tool_{index}.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"""
export function configureDuplicate{index}(registry: any, dependencies: any): void {{
  registry.addCommand("duplicate.{index}", {{ readOnly: true }}, async () => {{
    const value = await dependencies.load("duplicate.{index}");
    dependencies.observe(value);
    return {{ content: [{{ type: "text", text: String(value) }}] }};
  }});
}}
""".strip() + "\n",
            encoding="utf-8",
        )

    output_dir = tmp_path / "arcgraph-output"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    if any(
        warning.kind == "typescript_frontend_unavailable"
        for warning in reader.read_warnings()
    ):
        skip_or_fail("TypeScript compiler API is unavailable in this environment.")

    similar_edges = [edge for edge in reader.read_edges() if edge.kind == "similar_to"]
    complete_graph_edges = duplicate_count * (duplicate_count - 1) // 2
    assert 0 < len(similar_edges) < complete_graph_edges
    assert len(similar_edges) <= duplicate_count * 10
    warning = next(
        warning
        for warning in reader.read_warnings()
        if warning.kind == "similarity_edges_capped"
    )
    assert warning.frontend_name == "typescript-static"
    assert warning.path is None


def test_typescript_similarity_approximates_oversized_near_match_bucket(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    function_count = 211
    source = "\n".join(
        f"export function f{index}(): boolean {{ return {'!' * index}true; }}"
        for index in range(1, function_count + 1)
    )
    path = tmp_path / "src" / "functions.ts"
    path.parent.mkdir(parents=True)
    path.write_text(source + "\n", encoding="utf-8")

    output_dir = tmp_path / "arcgraph-output"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    if any(
        warning.kind == "typescript_frontend_unavailable"
        for warning in reader.read_warnings()
    ):
        skip_or_fail("TypeScript compiler API is unavailable in this environment.")

    profiles = [
        node
        for node in reader.read_nodes()
        if isinstance(node.properties.get("similarity"), dict)
    ]
    similar_edges = [edge for edge in reader.read_edges() if edge.kind == "similar_to"]
    assert len(profiles) == function_count
    assert 0 < len(similar_edges) <= function_count * 10
    warning = next(
        warning
        for warning in reader.read_warnings()
        if warning.kind == "similarity_bucket_approximated"
    )
    assert f"{function_count} candidates" in warning.message
    assert warning.frontend_name == "typescript-static"
    assert warning.path is None


def test_typescript_similarity_reindex_replaces_large_bucket_state(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    for index in range(211):
        (source_dir / f"f{index:03d}.ts").write_text(
            f"export function f{index:03d}(): boolean {{ return true; }}\n",
            encoding="utf-8",
        )

    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental-output"
    ArcGraphIndexer(tmp_path, incremental_output, roots).build()
    added_path = source_dir / "f105a.ts"
    added_path.write_text(
        "export function f105a(): boolean { return true; }\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(tmp_path, incremental_output, roots).reindex_changed()

    full_output = tmp_path / "full-output"
    ArcGraphIndexer(tmp_path, full_output, roots).build()

    def similarity_edges(output_dir: Path) -> set[tuple[str, str]]:
        return {
            (edge.source, edge.target)
            for edge in GraphStoreReader.from_current(output_dir).read_edges()
            if edge.kind == "similar_to"
        }

    assert similarity_edges(incremental_output) == similarity_edges(full_output)

    warning_kinds = {
        "similarity_bucket_approximated",
        "similarity_edges_capped",
    }

    def derived_warnings(output_dir: Path) -> list[tuple[str, str | None, str]]:
        return [
            (warning.kind, warning.path, warning.message)
            for warning in GraphStoreReader.from_current(output_dir).read_warnings()
            if warning.kind in warning_kinds
        ]

    incremental_warnings = derived_warnings(incremental_output)
    assert incremental_warnings == derived_warnings(full_output)
    assert len(incremental_warnings) == len(set(incremental_warnings))

    added_path.write_text(
        "export function f105a(): boolean { return !true; }\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(tmp_path, incremental_output, roots).reindex_changed()
    ArcGraphIndexer(tmp_path, full_output, roots).build()
    refreshed_warnings = derived_warnings(incremental_output)
    assert len(refreshed_warnings) == len(set(refreshed_warnings))
    assert refreshed_warnings == derived_warnings(full_output)
    assert similarity_edges(incremental_output) == similarity_edges(full_output)

    added_path.unlink()
    ArcGraphReindexer(tmp_path, incremental_output, roots).reindex_changed()
    ArcGraphIndexer(tmp_path, full_output, roots).build()
    assert similarity_edges(incremental_output) == similarity_edges(full_output)
    assert derived_warnings(incremental_output) == derived_warnings(full_output)


def test_typescript_reindex_extractor_failure_does_not_publish_empty_similarity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    for index in range(15):
        (source_dir / f"f{index:02d}.ts").write_text(
            f"export function f{index:02d}(): boolean {{ return true; }}\n",
            encoding="utf-8",
        )
    output_dir = tmp_path / "arcgraph-output"
    roots = [SourceRoot("src")]
    ArcGraphIndexer(tmp_path, output_dir, roots).build()
    before_reader = GraphStoreReader.from_current(output_dir)
    before_edges = [
        edge for edge in before_reader.read_edges() if edge.kind == "similar_to"
    ]
    if not before_edges:
        skip_or_fail("TypeScript compiler API is unavailable in this environment.")
    before_warnings = before_reader.read_warnings()
    pointer_before = (output_dir / "current.json").read_text(encoding="utf-8")
    changed = source_dir / "f00.ts"
    changed.write_text(
        "export function f00(): boolean { return !false; }\n",
        encoding="utf-8",
    )

    def fail_extractor(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("forced extractor failure")

    monkeypatch.setattr(TypeScriptGraphAnalyzer, "_run_extractor", fail_extractor)
    with pytest.raises(RuntimeError, match="extractor was unavailable"):
        ArcGraphReindexer(tmp_path, output_dir, roots).reindex_changed()

    assert (output_dir / "current.json").read_text(encoding="utf-8") == pointer_before
    after_reader = GraphStoreReader.from_current(output_dir)
    assert [
        edge for edge in after_reader.read_edges() if edge.kind == "similar_to"
    ] == before_edges
    assert after_reader.read_warnings() == before_warnings


def test_typescript_reindex_recovers_index_without_extractor_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    source_path = source_dir / "service.ts"
    source_path.write_text(
        "export function service(): boolean { return true; }\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph-output"
    roots = [SourceRoot("src")]

    def fail_extractor(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("forced extractor failure")

    with monkeypatch.context() as context:
        context.setattr(TypeScriptGraphAnalyzer, "_run_extractor", fail_extractor)
        ArcGraphIndexer(tmp_path, output_dir, roots).build()

    unavailable = GraphStoreReader.from_current(output_dir)
    assert "typescript-static" not in unavailable.metadata.get("extractor_metadata", {})
    assert any(
        warning.kind == "typescript_frontend_unavailable"
        for warning in unavailable.read_warnings()
    )
    source_path.write_text(
        "export function service(): boolean { return !false; }\n",
        encoding="utf-8",
    )

    result = ArcGraphReindexer(tmp_path, output_dir, roots).reindex_changed()

    assert result["status"] == "reindexed"
    recovered = GraphStoreReader.from_current(output_dir)
    assert (
        recovered.metadata["extractor_metadata"]["typescript-static"][
            "similarity_profile_algorithm"
        ]
        == TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM
    )


def test_typescript_reindex_preserves_python_similarity_disclosure(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "dense.py").write_text(
        "\n\n".join(
            f"def f{index:03d}(value):\n    return value" for index in range(201)
        )
        + "\n",
        encoding="utf-8",
    )
    typescript_path = source_dir / "service.ts"
    typescript_path.write_text(
        "export function service(): boolean { return true; }\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph-output"
    roots = [SourceRoot("src")]
    ArcGraphIndexer(tmp_path, output_dir, roots).build()

    def skipped_warnings() -> list[tuple[str, str | None]]:
        return [
            (warning.kind, warning.frontend_name)
            for warning in GraphStoreReader.from_current(output_dir).read_warnings()
            if warning.kind == "similarity_bucket_skipped"
        ]

    assert skipped_warnings() == [
        ("similarity_bucket_skipped", "python-v1-compat-shim")
    ]
    typescript_path.write_text(
        "export function service(): boolean { return !false; }\n",
        encoding="utf-8",
    )

    ArcGraphReindexer(tmp_path, output_dir, roots).reindex_changed()

    assert skipped_warnings() == [
        ("similarity_bucket_skipped", "python-v1-compat-shim")
    ]


def test_vue_only_reindex_replaces_typescript_similarity_warnings(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    for index in range(211):
        (source_dir / f"f{index:03d}.ts").write_text(
            f"export function f{index:03d}(): boolean {{ return true; }}\n",
            encoding="utf-8",
        )
    widget = source_dir / "Widget.vue"
    widget.write_text(
        "<template><main>0</main></template>\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph-output"
    roots = [SourceRoot("src")]
    ArcGraphIndexer(tmp_path, output_dir, roots).build()

    warning_kinds = {
        "similarity_bucket_approximated",
        "similarity_edges_capped",
    }

    def derived_warnings() -> list[tuple[str, str | None, str]]:
        return [
            (warning.kind, warning.frontend_name, warning.message)
            for warning in GraphStoreReader.from_current(output_dir).read_warnings()
            if warning.kind in warning_kinds
        ]

    baseline = derived_warnings()
    assert baseline
    assert {kind for kind, _frontend, _message in baseline} <= warning_kinds
    assert any(kind == "similarity_bucket_approximated" for kind, *_rest in baseline)
    for revision in range(1, 4):
        widget.write_text(
            f"<template><main>{revision}</main></template>\n",
            encoding="utf-8",
        )
        ArcGraphReindexer(tmp_path, output_dir, roots).reindex_changed()
        refreshed = derived_warnings()
        assert refreshed == baseline
        assert len(refreshed) == len(set(refreshed))


def test_bottom_k_sketch_preserves_containment_after_truncation() -> None:
    node = shutil.which("node")
    if node is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    module_path = (
        Path(__file__).parents[1]
        / "pipeline"
        / "typescript_extractor"
        / "similarity.mjs"
    )
    script = f"""
import {{ scoreNormalizedTokenSequences }} from {json.dumps(module_path.as_uri())};
const contained = Array.from({{ length: 110 }}, (_, index) => `contained-${{index}}`);
const prefix = Array.from({{ length: 220 }}, (_, index) => `prefix-${{index}}`);
const suffix = Array.from({{ length: 50 }}, (_, index) => `suffix-${{index}}`);
const result = scoreNormalizedTokenSequences(contained, [...prefix, ...contained, ...suffix]);
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["left_ngram_count"] < 128
    assert result["right_ngram_count"] > 128
    assert result["right_sketch_count"] == 128
    assert result["score"] == 1
    assert "token_containment=1.00" in result["reasons"]
    assert any(
        reason.startswith("token_containment_sample=") for reason in result["reasons"]
    )


def test_bottom_k_sketch_does_not_claim_containment_from_tiny_sample() -> None:
    node = shutil.which("node")
    if node is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    module_path = (
        Path(__file__).parents[1]
        / "pipeline"
        / "typescript_extractor"
        / "similarity.mjs"
    )
    script = f"""
import {{ scoreNormalizedTokenSequences }} from {json.dumps(module_path.as_uri())};
const shared = ['SharedA316300', 'SharedB316300', 'SharedC316300'];
function stream(prefix, length, seed) {{
  const values = Array.from({{ length }}, (_, index) => `${{prefix}}-${{seed}}-${{index}}`);
  values.splice(Math.floor(length / 2), 3, ...shared);
  return values;
}}
let maximum = 0;
let containmentClaims = 0;
for (let index = 0; index < 200; index += 1) {{
  const result = scoreNormalizedTokenSequences(
    stream('large', 9000, index),
    stream('small', 60, index)
  );
  maximum = Math.max(maximum, result.score);
  if (result.reasons.some((reason) => reason.startsWith('token_containment='))) {{
    containmentClaims += 1;
  }}
}}
process.stdout.write(JSON.stringify({{ maximum, containmentClaims }}));
"""
    completed = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["maximum"] < 0.82
    assert result["containmentClaims"] == 0


def test_similarity_runner_omitted_allowlist_means_no_filtering() -> None:
    node = shutil.which("node")
    if node is None:
        skip_or_fail("Node.js is required for TypeScript similarity tests.")

    runner_path = (
        Path(__file__).parents[1]
        / "pipeline"
        / "typescript_extractor"
        / "similarity_runner.mjs"
    )
    row = {
        "id": "fn:src.alpha",
        "path": "src/alpha.ts",
        "start_line": 1,
        "end_line": 5,
        "profile": {
            "algorithm": TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM,
            "bucket": "src|function|0||sync",
            "structure_hash": "structure-1",
            "ngram_hashes": [1, 2, 3],
            "ngram_count": 3,
            "call_targets": ["fn:src.helper"],
            "call_target_modules": {"fn:src.helper": "helper"},
            "resource_targets": [],
        },
    }

    def run_runner(payload: dict) -> dict:
        completed = subprocess.run(
            [node, str(runner_path)],
            input=json.dumps(payload),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(completed.stdout)

    # An omitted allowlist is the documented no-filtering default, not an
    # empty allowlist that strips every repo-local target.
    absent = run_runner({"profiles": [row]})
    assert absent["profiles"][0]["profile"]["call_targets"] == ["fn:src.helper"]

    empty = run_runner({"profiles": [row], "validTargetIds": []})
    assert empty["profiles"][0]["profile"]["call_targets"] == []


def test_similarity_family_traversal_is_bounded_and_disclosed(
    tmp_path: Path, monkeypatch
) -> None:
    """A similarity component is unbounded in principle, so the walk stops at
    a stated limit and the payload says the traversal was truncated."""

    from arcgraph.core import query_engine as query_engine_module
    from arcgraph.core.graph_store import GraphStoreWriter
    from arcgraph.core.query_engine import QueryEngine
    from arcgraph.core.schemas import Edge, IndexMetadata, Node

    monkeypatch.setattr(query_engine_module, "SIMILARITY_FAMILY_NODE_LIMIT", 4)

    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id=f"fn:pkg.handler_{index}",
            kind="function",
            name=f"handler_{index}",
            qualname=f"pkg.handler_{index}",
            path=f"src/handler_{index}.py",
            properties={"similarity": {"algorithm": "minhash", "buckets": ["b"]}},
        )
        for index in range(12)
    ]
    # One chain: every node links to the next, so the component is the whole set.
    edges = [
        Edge(
            source=nodes[index].id,
            target=nodes[index + 1].id,
            kind="similar_to",
            properties={"score": 0.9, "reasons": ["structure"]},
        )
        for index in range(len(nodes) - 1)
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="bounded-family",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=nodes,
        edges=edges,
        warnings=[],
    )

    payload = QueryEngine(output_dir).similar("pkg.handler_0")
    family = payload["pattern_family"]

    assert family["family_traversal_truncated"] is True
    assert family["family_traversal_limit"] == 4
    assert family["member_count"] <= 4
    # A universal claim cannot be made over a partial family.
    assert family["only_one_member_modified"] is False


def test_similarity_family_seed_counts_against_the_limit(tmp_path: Path) -> None:
    """One file target resolves to every symbol the file declares, so the
    family can exceed the limit before an edge is walked. Bounding only the
    expansion left member_count above the stated limit while claiming
    nothing was cut."""

    from arcgraph.core import query_engine as query_engine_module
    from arcgraph.core.graph_store import GraphStoreWriter
    from arcgraph.core.query_engine import QueryEngine
    from arcgraph.core.schemas import IndexMetadata, Node

    limit = query_engine_module.SIMILARITY_FAMILY_NODE_LIMIT
    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id=f"fn:big.s{index}",
            kind="function",
            name=f"s{index}",
            qualname=f"big.s{index}",
            path="src/big.py",
            properties={"similarity": {"algorithm": "minhash", "buckets": ["b"]}},
        )
        for index in range(limit + 20)
    ]
    nodes.append(
        Node(id="mod:big", kind="module", name="big", qualname="big", path="src/big.py")
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="seed-limit",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=nodes,
        edges=[],
        warnings=[],
    )

    family = QueryEngine(output_dir).similar("src/big.py")["pattern_family"]

    assert family["member_count"] <= limit
    assert family["family_traversal_truncated"] is True
    # A universal claim cannot be made over a family that was cut.
    assert family["only_one_member_modified"] is False


def test_similar_keeps_the_full_target_set_for_membership(tmp_path: Path) -> None:
    """Bounding the seed before analysis made a symbol past the limit report
    as similar to its own file, and dropped implementations linked only to a
    dropped target. Only the family walk is bounded."""

    from arcgraph.core import query_engine as query_engine_module
    from arcgraph.core.graph_store import GraphStoreWriter
    from arcgraph.core.query_engine import QueryEngine
    from arcgraph.core.schemas import Edge, IndexMetadata, Node

    limit = query_engine_module.SIMILARITY_FAMILY_NODE_LIMIT
    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id=f"fn:wide.s{index}",
            kind="function",
            name=f"s{index}",
            qualname=f"wide.s{index}",
            path="src/wide.py",
            properties={"similarity": {"algorithm": "minhash", "buckets": ["b"]}},
        )
        for index in range(limit + 1)
    ]
    outsider = Node(
        id="fn:other.impl",
        kind="function",
        name="impl",
        qualname="other.impl",
        path="src/other.py",
        properties={"similarity": {"algorithm": "minhash", "buckets": ["b"]}},
    )
    nodes.append(outsider)
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="seed-membership",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=nodes,
        edges=[
            # The last symbol of the file is a member, not a match.
            Edge(
                source="fn:wide.s0",
                target=f"fn:wide.s{limit}",
                kind="similar_to",
                properties={"score": 0.99, "reasons": ["structure"]},
            ),
            # An outside implementation reachable only from a late member.
            Edge(
                source=f"fn:wide.s{limit}",
                target="fn:other.impl",
                kind="similar_to",
                properties={"score": 0.97, "reasons": ["structure"]},
            ),
        ],
        warnings=[],
    )

    payload = QueryEngine(output_dir).similar("src/wide.py")
    similar_ids = {item["node"]["id"] for item in payload["similar"]}

    assert f"fn:wide.s{limit}" not in similar_ids
    assert "fn:other.impl" in similar_ids
