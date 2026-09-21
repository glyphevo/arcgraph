from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from arcgraph.tests.typescript_acceptance_support import skip_or_fail
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer


def _build_typescript_repo(tmp_path: Path) -> GraphStoreReader:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript module-resolution tests.")

    files = {
        "src/io/index.ts": """
export async function readProtectedDocument(root: string, path: string): Promise<string> {
  return `${root}/${path}`;
}
""",
        "src/consumer/index.ts": """
import { readProtectedDocument } from "../io/index.js";

export async function loadWorkspaceDocument(root: string, path: string) {
  return readProtectedDocument(root, path);
}
""",
        "src/esm/index.mts": "export const esmValue = 1;\n",
        "src/use-esm.ts": """
import { esmValue } from "./esm/index.mjs";
export function useEsm() { return esmValue; }
""",
        "src/common/index.cts": "export const commonValue = 2;\n",
        "src/use-common.ts": """
import { commonValue } from "./common/index.cjs";
export function useCommon() { return commonValue; }
""",
        "src/components/View.tsx": """
export function View() { return <main />; }
""",
        "src/use-view.ts": """
import { View } from "./components/View.jsx";
export function useView() { return View(); }
""",
        "src/dual.ts": """
export function shared(): string { return "typescript"; }
""",
        "src/dual.mjs": """
export function shared() { return "runtime-esm"; }
""",
        "src/use-dual.ts": """
import { shared as fromTypeScript } from "./dual.js";
import { shared as fromRuntimeEsm } from "./dual.mjs";
export function callBoth() { return [fromTypeScript(), fromRuntimeEsm()]; }
""",
    }
    for relative_path, source in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source.strip() + "\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        """{
  "compilerOptions": {
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "jsx": "react-jsx"
  },
  "include": ["src/**/*"]
}
""",
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
    return reader


def test_nodenext_runtime_extensions_resolve_to_typescript_sources(
    tmp_path: Path,
) -> None:
    reader = _build_typescript_repo(tmp_path)
    files = {file.path: file for file in reader.read_files()}
    edges = {
        (edge.source, edge.kind, edge.target): edge for edge in reader.read_edges()
    }

    assert files["src/esm/index.mts"].module == "esm"
    assert files["src/common/index.cts"].module == "common"
    assert files["src/dual.ts"].module == "dual"
    assert files["src/dual.mjs"].module != files["src/dual.ts"].module
    for edge_key in (
        ("mod:consumer", "imports", "mod:io"),
        ("mod:use-esm", "imports", "mod:esm"),
        ("mod:use-common", "imports", "mod:common"),
        ("mod:use-view", "imports", "mod:components.View"),
    ):
        assert edge_key in edges
        assert edges[edge_key].confidence == "confirmed"

    cross_module_call = (
        "fn:consumer.loadWorkspaceDocument",
        "calls",
        "fn:io.readProtectedDocument",
    )
    assert cross_module_call in edges
    assert edges[cross_module_call].resolution.strategy in {
        None,
        "typescript_typechecker",
    }

    dual_targets = {
        edge.target
        for edge in edges.values()
        if edge.source == "fn:use-dual.callBoth" and edge.kind == "calls"
    }
    assert dual_targets == {
        f"fn:{files['src/dual.ts'].module}.shared",
        f"fn:{files['src/dual.mjs'].module}.shared",
    }
    shared_definitions = {
        node.path
        for node in reader.read_nodes()
        if node.name == "shared" and node.path in {"src/dual.ts", "src/dual.mjs"}
    }
    assert shared_definitions == {"src/dual.ts", "src/dual.mjs"}

    assert not any(
        edge.kind == "imports"
        and edge.target
        in {
            "unresolved:../io/index.js",
            "unresolved:./esm/index.mjs",
            "unresolved:./common/index.cjs",
            "unresolved:./components/View.jsx",
        }
        for edge in edges.values()
    )


def test_extensionless_imports_resolve_mjs_cjs_and_directory_indexes(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript module-resolution tests.")

    sources = {
        "src/pkg/index.mjs": "export function fromPackage() { return 'pkg'; }\n",
        "src/util.mjs": "export function fromUtility() { return 'util'; }\n",
        "src/common.cjs": "exports.commonValue = 1;\n",
        "src/main.mjs": """
import { fromPackage } from "./pkg";
import { fromUtility } from "./util";
import { commonValue } from "./common";
export function useAll() { return [fromPackage(), fromUtility(), commonValue]; }
""",
    }
    for relative_path, source in sources.items():
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

    edges = {(edge.source, edge.kind, edge.target) for edge in reader.read_edges()}
    assert {
        ("mod:main", "imports", "mod:pkg"),
        ("mod:main", "imports", "mod:util"),
        ("mod:main", "imports", "mod:common"),
        ("fn:main.useAll", "calls", "fn:pkg.fromPackage"),
        ("fn:main.useAll", "calls", "fn:util.fromUtility"),
    } <= edges
    assert not any(
        edge[0] == "mod:main"
        and edge[1] == "imports"
        and edge[2].startswith("unresolved:")
        for edge in edges
    )


def test_typescript_variant_module_identity_changes_require_full_build(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript module-resolution tests.")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    runtime_path = source_dir / "dual.mjs"
    runtime_path.write_text(
        'export function shared() { return "runtime-esm"; }\n',
        encoding="utf-8",
    )
    consumer_path = source_dir / "consumer.ts"
    consumer_path.write_text(
        'import { shared } from "./dual.mjs";\n'
        "export function useIt() { return shared(); }\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph-output"
    roots = [SourceRoot("src")]
    ArcGraphIndexer(tmp_path, output_dir, roots).build()
    initial = GraphStoreReader.from_current(output_dir)
    initial_modules = {file.path: file.module for file in initial.read_files()}
    assert initial_modules["src/dual.mjs"] == "dual"

    typescript_path = source_dir / "dual.ts"
    typescript_path.write_text(
        'export function shared(): string { return "typescript"; }\n',
        encoding="utf-8",
    )
    pointer_before_variant = (output_dir / "current.json").read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="module identity"):
        ArcGraphReindexer(tmp_path, output_dir, roots).reindex_changed()
    assert (output_dir / "current.json").read_text(
        encoding="utf-8"
    ) == pointer_before_variant

    ArcGraphIndexer(tmp_path, output_dir, roots).build()
    with_variant = GraphStoreReader.from_current(output_dir)
    modules = {file.path: file.module for file in with_variant.read_files()}
    assert modules["src/dual.ts"] == "dual"
    assert modules["src/dual.mjs"] != modules["src/dual.ts"]
    assert {
        node.path for node in with_variant.read_nodes() if node.name == "shared"
    } == {"src/dual.ts", "src/dual.mjs"}
    variant_edges = {
        (edge.source, edge.kind, edge.target) for edge in with_variant.read_edges()
    }
    assert (
        "mod:consumer",
        "imports",
        f"mod:{modules['src/dual.mjs']}",
    ) in variant_edges
    assert (
        "fn:consumer.useIt",
        "calls",
        f"fn:{modules['src/dual.mjs']}.shared",
    ) in variant_edges

    runtime_module = modules["src/dual.mjs"]
    runtime_node_id = f"fn:{runtime_module}.shared"
    declaration_path = source_dir / "dual.d.mts"
    declaration_path.write_text(
        "export declare const runtimeType: string;\n",
        encoding="utf-8",
    )
    declaration_added = ArcGraphReindexer(
        tmp_path,
        output_dir,
        roots,
    ).reindex_changed()
    assert declaration_added["changed"] == {
        "added": ["src/dual.d.mts"],
        "modified": [],
        "deleted": [],
    }
    with_declaration = GraphStoreReader.from_current(output_dir)
    declaration_modules = {
        file.path: file.module for file in with_declaration.read_files()
    }
    assert declaration_modules["src/dual.mjs"] == runtime_module
    assert declaration_modules["src/dual.d.mts"] == runtime_module
    assert any(
        node.id == runtime_node_id and node.path == "src/dual.mjs"
        for node in with_declaration.read_nodes()
    )

    declaration_path.unlink()
    declaration_deleted = ArcGraphReindexer(
        tmp_path,
        output_dir,
        roots,
    ).reindex_changed()
    assert declaration_deleted["changed"] == {
        "added": [],
        "modified": [],
        "deleted": ["src/dual.d.mts"],
    }
    after_declaration_delete = GraphStoreReader.from_current(output_dir)
    assert {file.path: file.module for file in after_declaration_delete.read_files()}[
        "src/dual.mjs"
    ] == runtime_module
    assert any(
        node.id == runtime_node_id and node.path == "src/dual.mjs"
        for node in after_declaration_delete.read_nodes()
    )

    typescript_path.unlink()
    pointer_before_collapse = (output_dir / "current.json").read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="module identity"):
        ArcGraphReindexer(tmp_path, output_dir, roots).reindex_changed()
    assert (output_dir / "current.json").read_text(
        encoding="utf-8"
    ) == pointer_before_collapse

    ArcGraphIndexer(tmp_path, output_dir, roots).build()
    after_delete = GraphStoreReader.from_current(output_dir)
    after_delete_modules = {
        file.path: file.module for file in after_delete.read_files()
    }
    assert after_delete_modules["src/dual.mjs"] == "dual"
    shared_nodes = [node for node in after_delete.read_nodes() if node.name == "shared"]
    assert [(node.id, node.path) for node in shared_nodes] == [
        ("fn:dual.shared", "src/dual.mjs")
    ]
    collapsed_edges = {
        (edge.source, edge.kind, edge.target) for edge in after_delete.read_edges()
    }
    assert ("mod:consumer", "imports", "mod:dual") in collapsed_edges
    assert ("fn:consumer.useIt", "calls", "fn:dual.shared") in collapsed_edges


def test_explicit_runtime_import_prefers_retained_js_beside_typescript(
    tmp_path: Path,
) -> None:
    """Hand-written shim.js must win `import \"./shim.js\"` over sibling shim.ts."""
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript module-resolution tests.")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "shim.ts").write_text(
        'export function fromTs(): string { return "ts"; }\n',
        encoding="utf-8",
    )
    (source_dir / "shim.js").write_text(
        'export function fromJs() { return "js"; }\n',
        encoding="utf-8",
    )
    (source_dir / "main.ts").write_text(
        'import { fromJs } from "./shim.js";\n'
        "export function use() { return fromJs(); }\n",
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

    files = {file.path: file for file in reader.read_files()}
    assert files["src/shim.js"].module.startswith("shim.__arcgraph_variant_js_")
    assert files["src/shim.ts"].module == "shim"
    js_module = files["src/shim.js"].module
    edges = {(edge.source, edge.kind, edge.target) for edge in reader.read_edges()}
    assert ("mod:main", "imports", f"mod:{js_module}") in edges
    assert ("fn:main.use", "calls", f"fn:{js_module}.fromJs") in edges
    assert ("mod:main", "imports", "mod:shim") not in edges


def test_omitted_emit_js_falls_back_to_typescript_before_jsx_variant(
    tmp_path: Path,
) -> None:
    """Dropped emit `.js` must prefer `.ts` over a sibling hand-written `.jsx`."""
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript module-resolution tests.")

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "shim.js").write_text(
        'export function fromJs() { return "js"; }\n'
        "//# sourceMappingURL=shim.js.map\n",
        encoding="utf-8",
    )
    (source_dir / "shim.ts").write_text(
        'export function fromTs(): string { return "ts"; }\n',
        encoding="utf-8",
    )
    (source_dir / "shim.jsx").write_text(
        "export function fromJsx() { return <span />;\n}\n",
        encoding="utf-8",
    )
    (source_dir / "main.ts").write_text(
        'import { fromTs } from "./shim.js";\n'
        "export function use() { return fromTs(); }\n",
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

    files = {file.path: file for file in reader.read_files()}
    assert "src/shim.js" not in files
    assert files["src/shim.ts"].module == "shim"
    assert files["src/shim.jsx"].module.startswith("shim.__arcgraph_variant_jsx_")
    edges = {(edge.source, edge.kind, edge.target) for edge in reader.read_edges()}
    assert ("mod:main", "imports", "mod:shim") in edges
    assert ("fn:main.use", "calls", "fn:shim.fromTs") in edges
    assert not any(
        target.startswith("mod:shim.__arcgraph_variant_jsx_")
        for _source, kind, target in edges
        if kind == "imports" and _source == "mod:main"
    )
