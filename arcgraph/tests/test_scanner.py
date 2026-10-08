from __future__ import annotations

import io
from pathlib import Path

import pytest

from arcgraph.core.scanner import (
    DEFAULT_IGNORE_RULES,
    LEGACY_SOURCE_ROOTS,
    FileScanner,
    SourceRoot,
    _SOURCE_MAPPING_CHUNK_BYTES,
    _chunked_contains,
    _iter_init_files_at_depth,
    _paths_are_same,
    detect_source_roots,
    is_typescript_emit_artifact,
    read_arcgraph_exclude,
)


def _register_linked_worktree(
    repo_root: Path,
    worktree_root: Path,
    *,
    name: str,
) -> None:
    """Create the two Git metadata pointers used by a linked worktree."""

    admin_dir = repo_root / ".git" / "worktrees" / name
    admin_dir.mkdir(parents=True, exist_ok=True)
    worktree_root.mkdir(parents=True, exist_ok=True)
    git_marker = worktree_root / ".git"
    git_marker.write_text(f"gitdir: {admin_dir}\n", encoding="utf-8")
    (admin_dir / "gitdir").write_text(f"{git_marker}\n", encoding="utf-8")


def test_legacy_source_roots_include_arcgraph_package() -> None:
    assert SourceRoot("arcgraph") in LEGACY_SOURCE_ROOTS


def test_scanner_ignores_only_repo_relative_directory_names(tmp_path: Path) -> None:
    repo_root = tmp_path / "venv" / "sample_repo"
    source_root = repo_root / "src" / "pkg"
    source_root.mkdir(parents=True)
    (source_root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    ignored_dir = repo_root / "src" / "pkg" / "node_modules"
    ignored_dir.mkdir()
    (ignored_dir / "ignored.py").write_text("VALUE = 2\n", encoding="utf-8")

    scanner = FileScanner(repo_root=repo_root, source_roots=[SourceRoot("src")])
    records = scanner.scan()

    assert [record.path for record in records] == ["src/pkg/module.py"]


def test_scanner_prunes_ignored_directories_before_descending(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    package_dir = source_root / "pkg"
    ignored_dir = source_root / "node_modules"
    package_dir.mkdir(parents=True)
    ignored_dir.mkdir()
    (package_dir / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (ignored_dir / "ignored.py").write_text("VALUE = 2\n", encoding="utf-8")
    descended: list[str] = []

    def fake_walk(root: Path):
        dirnames = ["node_modules", "pkg"]
        yield str(root), dirnames, []
        for dirname in dirnames:
            descended.append(dirname)
            if dirname == "pkg":
                yield str(root / dirname), [], ["module.py"]
            elif dirname == "node_modules":
                yield str(root / dirname), [], ["ignored.py"]

    monkeypatch.setattr("arcgraph.core.scanner.os.walk", fake_walk)

    scanner = FileScanner(repo_root=repo_root, source_roots=[SourceRoot("src")])
    records = scanner.scan()

    assert descended == ["pkg"]
    assert [record.path for record in records] == ["src/pkg/module.py"]


def test_scanner_prunes_output_arcgraph_for_repo_root_source(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "output" / "arcgraph").mkdir(parents=True)
    (repo_root / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo_root / "output" / "arcgraph" / "generated.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )
    descended: list[str] = []

    def fake_walk(root: Path):
        root_dirs = ["output", "src"]
        yield str(root), root_dirs, []
        for dirname in root_dirs:
            descended.append(dirname)
            if dirname == "src":
                yield str(root / dirname), [], ["module.py"]
            elif dirname == "output":
                output_dirs = ["arcgraph"]
                yield str(root / dirname), output_dirs, []
                for child in output_dirs:
                    descended.append(f"{dirname}/{child}")
                    yield str(root / dirname / child), [], ["generated.py"]

    monkeypatch.setattr("arcgraph.core.scanner.os.walk", fake_walk)

    scanner = FileScanner(repo_root=repo_root, source_roots=[SourceRoot(".")])
    records = scanner.scan()

    assert "output/arcgraph" not in descended
    assert [record.path for record in records] == ["src/module.py"]


def test_scanner_default_ignores_common_python_build_generated_dirs(
    tmp_path: Path,
) -> None:
    """Default ignores favor generated/build artifacts over scanning them."""
    repo_root = tmp_path / "repo"
    package_dir = repo_root / "src" / "pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    for dirname in ("build", "dist", "generated", "generated-client"):
        ignored_dir = repo_root / "src" / dirname
        ignored_dir.mkdir(parents=True)
        (ignored_dir / "ignored.py").write_text("VALUE = 2\n", encoding="utf-8")

    scanner = FileScanner(repo_root=repo_root, source_roots=[SourceRoot("src")])
    records = scanner.scan()

    assert [record.path for record in records] == ["src/pkg/module.py"]


def test_scanner_default_ignores_typescript_frontend_build_dirs(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    src_dir = repo_root / "frontend" / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "App.tsx").write_text(
        "export function App() { return null; }\n",
        encoding="utf-8",
    )

    for dirname in (".next", "dist", "build", "generated"):
        ignored_dir = repo_root / "frontend" / dirname
        ignored_dir.mkdir(parents=True)
        (ignored_dir / "ignored.ts").write_text(
            "export const ignored = true;\n",
            encoding="utf-8",
        )

    scanner = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("frontend")],
        file_extensions=(".ts", ".tsx"),
    )
    records = scanner.scan()

    assert [record.path for record in records] == ["frontend/src/App.tsx"]


def test_scanner_keeps_existing_node_modules_output_cache_defaults(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    package_dir = repo_root / "pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    for dirname in ("node_modules", "output", ".cache"):
        ignored_dir = repo_root / dirname
        ignored_dir.mkdir(parents=True)
        (ignored_dir / "ignored.py").write_text("VALUE = 2\n", encoding="utf-8")

    scanner = FileScanner(repo_root=repo_root, source_roots=[SourceRoot(".")])
    records = scanner.scan()

    assert [record.path for record in records] == ["pkg/module.py"]


def test_scanner_ignores_only_claude_managed_worktree_subtree(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    for relative_path in (
        ".claude/hooks/keep.py",
        ".claude/worktrees/task/src/copied.py",
        "packages/service/.claude/worktrees/task/src/copied.py",
        "fixtures/worktrees/keep.py",
        "src/pkg/module.py",
    ):
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")

    records = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot(".")],
    ).scan()

    assert [record.path for record in records] == [
        ".claude/hooks/keep.py",
        "fixtures/worktrees/keep.py",
        "src/pkg/module.py",
    ]


def test_source_root_detection_ignores_claude_managed_worktree_copy(
    tmp_path: Path,
) -> None:
    for relative_path in (
        ".claude/worktrees/task/src/copied/__init__.py",
        "nested/.claude/worktrees/__init__.py",
    ):
        copied_package = tmp_path / relative_path
        copied_package.parent.mkdir(parents=True, exist_ok=True)
        copied_package.write_text("", encoding="utf-8")

    detected = detect_source_roots(tmp_path)

    assert detected.roots == (SourceRoot("."),)
    assert detected.detection.strategy == "repo_root_fallback"


def test_deep_package_probe_excludes_nested_claude_worktree_copy(
    tmp_path: Path,
) -> None:
    ignored = (
        tmp_path
        / "packages"
        / "service"
        / ".claude"
        / "worktrees"
        / "task"
        / "mypkg"
        / "__init__.py"
    )
    kept = (
        tmp_path
        / "packages"
        / "server"
        / "src"
        / "company"
        / "service"
        / "mypkg"
        / "__init__.py"
    )
    for path in (ignored, kept):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    assert _iter_init_files_at_depth(tmp_path, 6) == [kept]


def test_scanner_merges_arcgraph_exclude_with_default_ignores(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "src" / "pkg").mkdir(parents=True)
    (repo_root / "src" / "pkg" / "module.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "legacy_app").mkdir(parents=True)
    (repo_root / "src" / "legacy_app" / "old.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )
    (repo_root / "pyproject.toml").write_text(
        '[tool.arcgraph]\nexclude = ["src/legacy_app/**"]\n',
        encoding="utf-8",
    )

    scanner = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("src")],
        ignore_rules=[*DEFAULT_IGNORE_RULES, *read_arcgraph_exclude(repo_root)],
    )
    records = scanner.scan()

    assert [record.path for record in records] == ["src/pkg/module.py"]


def test_scanner_treats_nested_typescript_index_as_package(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    component_dir = repo_root / "src" / "components"
    component_dir.mkdir(parents=True)
    (repo_root / "src" / "index.ts").write_text("export {}\n", encoding="utf-8")
    (component_dir / "index.ts").write_text("export {}\n", encoding="utf-8")

    scanner = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("src")],
        file_extensions=(".ts",),
    )
    records = scanner.scan()
    by_path = {record.path: record for record in records}

    assert by_path["src/index.ts"].module == "index"
    assert by_path["src/index.ts"].is_package is False
    assert by_path["src/components/index.ts"].module == "components"
    assert by_path["src/components/index.ts"].is_package is True


def test_scanner_keeps_submodules_and_vendored_checkout_sources(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git" / "modules" / "submodule").mkdir(parents=True)
    paths = {
        "src/keep.py": "VALUE = 1\n",
        "src/clone/.git/HEAD": "ref: refs/heads/main\n",
        "src/clone/copied.py": "VALUE = 2\n",
        "src/jjcopy/.jj/repo/type": "git\n",
        "src/jjcopy/copied.py": "VALUE = 5\n",
        "src/submodule/.git": "gitdir: ../../.git/modules/submodule\n",
        "src/submodule/copied.py": "VALUE = 3\n",
        "src/not-a-checkout/.gitignore": "*.pyc\n",
        "src/not-a-checkout/keep.py": "VALUE = 4\n",
    }
    for relative_path, content in paths.items():
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    records = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("src")],
    ).scan()

    assert [record.path for record in records] == [
        "src/keep.py",
        "src/clone/copied.py",
        "src/jjcopy/copied.py",
        "src/not-a-checkout/keep.py",
        "src/submodule/copied.py",
    ]


def test_scanner_prunes_same_repository_linked_worktrees_at_any_path(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    for relative_path in (
        "src/keep.py",
        "fixtures/worktrees/keep.py",
        "wt/src/copied.py",
        ".cursor/worktrees/task/src/copied.py",
    ):
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")
    (repo_root / ".git").mkdir(exist_ok=True)
    _register_linked_worktree(repo_root, repo_root / "wt", name="wt")
    _register_linked_worktree(
        repo_root,
        repo_root / ".cursor" / "worktrees" / "task",
        name="cursor-task",
    )

    records = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot(".")],
    ).scan()

    assert [record.path for record in records] == [
        "fixtures/worktrees/keep.py",
        "src/keep.py",
    ]


def test_worktree_admin_paths_compare_by_filesystem_identity(tmp_path: Path) -> None:
    admin_dir = tmp_path / "repo" / ".git" / "worktrees" / "task"
    admin_dir.mkdir(parents=True)
    alias = tmp_path / "admin-alias"
    alias.symlink_to(admin_dir, target_is_directory=True)

    assert alias != admin_dir
    assert _paths_are_same(alias, admin_dir)


def test_scanner_finds_worktree_registry_above_subdirectory_root(
    tmp_path: Path,
) -> None:
    checkout_root = tmp_path / "repo"
    repo_root = checkout_root / "packages" / "service"
    (checkout_root / ".git").mkdir(parents=True)
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "keep.py").write_text("VALUE = 1\n", encoding="utf-8")
    linked_root = repo_root / "wt"
    (linked_root / "src").mkdir(parents=True)
    (linked_root / "src" / "copied.py").write_text("VALUE = 2\n", encoding="utf-8")
    _register_linked_worktree(checkout_root, linked_root, name="service-wt")

    records = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot(".")],
    ).scan()

    assert [record.path for record in records] == ["src/keep.py"]


def test_scanner_finds_nested_worktree_when_repo_root_is_linked_worktree(
    tmp_path: Path,
) -> None:
    common_git_dir = tmp_path / "main" / ".git"
    repo_root = tmp_path / "current"
    current_admin = common_git_dir / "worktrees" / "current"
    nested_admin = common_git_dir / "worktrees" / "nested"
    current_admin.mkdir(parents=True)
    nested_admin.mkdir(parents=True)
    repo_root.mkdir()

    current_marker = repo_root / ".git"
    current_marker.write_text(f"gitdir: {current_admin}\n", encoding="utf-8")
    (current_admin / "commondir").write_text("../..\n", encoding="utf-8")
    (current_admin / "gitdir").write_text(
        f"{current_marker}\n",
        encoding="utf-8",
    )

    nested_root = repo_root / "nested"
    nested_root.mkdir()
    nested_marker = nested_root / ".git"
    nested_marker.write_text(f"gitdir: {nested_admin}\n", encoding="utf-8")
    (nested_admin / "gitdir").write_text(
        f"{nested_marker}\n",
        encoding="utf-8",
    )

    (repo_root / "src").mkdir()
    (repo_root / "src" / "keep.py").write_text("VALUE = 1\n", encoding="utf-8")
    (nested_root / "copied.py").write_text("VALUE = 2\n", encoding="utf-8")

    detected = detect_source_roots(repo_root)
    records = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot(".")],
    ).scan()

    assert detected.roots == (SourceRoot("src", "src"),)
    assert [record.path for record in records] == ["src/keep.py"]


def test_scanner_prefers_corresponding_typescript_source_over_runtime_emit(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    # A same-stem TypeScript source is necessary but not sufficient to call a
    # runtime file build output, so the emits below carry the provenance a
    # compiler leaves behind. `src/shim.js` deliberately does not: a
    # hand-written CommonJS shim beside a .ts source is real source and must
    # stay both indexed and visible to change safety.
    emitted = "//# sourceMappingURL=out.map\n"
    sources = {
        "src/plain.ts": "export const plain = 1;\n",
        "src/plain.js": f"export const plain = 1;\n{emitted}",
        "src/component.tsx": "export const Component = () => null;\n",
        "src/component.jsx": f"export const Component = () => null;\n{emitted}",
        "src/module.mts": "export const moduleValue = 1;\n",
        "src/module.mjs": f"export const moduleValue = 1;\n{emitted}",
        "src/common.cts": "export const common = 1;\n",
        "src/common.cjs": f"exports.common = 1;\n{emitted}",
        "src/runtime.js": "export const runtime = 1;\n",
        "src/shim.ts": "export const shim = 1;\n",
        "src/shim.js": "module.exports = require('./dist');\n",
        # A .ts source does not emit .mjs; both are intentional source variants.
        "src/dual.ts": "export const typed = 1;\n",
        "src/dual.mjs": "export const runtime = 1;\n",
    }
    for relative_path, content in sources.items():
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    records = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("src")],
        file_extensions=(".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
    ).scan()

    assert [record.path for record in records] == [
        "src/common.cts",
        "src/component.tsx",
        "src/dual.mjs",
        "src/dual.ts",
        "src/module.mts",
        "src/plain.ts",
        "src/runtime.js",
        "src/shim.js",
        "src/shim.ts",
    ]
    by_path = {record.path: record for record in records}
    # The surviving hand-written runtime file is an independent module, so it
    # must not share graph identities with the .ts source beside it.
    assert by_path["src/shim.ts"].module == "shim"
    assert by_path["src/shim.js"].module.startswith("shim.__arcgraph_variant_js_")
    assert by_path["src/dual.ts"].module == "dual"
    assert by_path["src/dual.mjs"].module.startswith("dual.__arcgraph_variant_esm_")
    assert by_path["src/dual.mjs"].module != by_path["src/dual.ts"].module


def test_inline_source_map_marker_is_found_far_from_end_of_file(
    tmp_path: Path,
) -> None:
    """``tsc --inlineSourceMap`` puts the marker before a long base64 payload.

    The marker then sits tens of kilobytes from EOF, so a small fixed tail
    window misses it and the emit is mistaken for hand-written source.
    """

    source = tmp_path / "src"
    source.mkdir(parents=True)
    (source / "big.ts").write_text("export const value = 1;\n", encoding="utf-8")
    payload = "A" * 40_000
    (source / "big.js").write_text(
        "exports.value = 1;\n"
        f"//# sourceMappingURL=data:application/json;base64,{payload}\n",
        encoding="utf-8",
    )
    # Hand-written sibling of the same size class, with no marker anywhere.
    (source / "shim.ts").write_text("export const shim = 1;\n", encoding="utf-8")
    (source / "shim.js").write_text(
        "module.exports = require('./dist');\n" + ("// filler\n" * 5_000),
        encoding="utf-8",
    )

    emit = source / "big.js"
    marker_distance = len(emit.read_bytes()) - emit.read_bytes().rfind(
        b"sourceMappingURL="
    )
    assert marker_distance > 2048
    assert is_typescript_emit_artifact(emit) is True
    assert is_typescript_emit_artifact(source / "shim.js") is False

    # An embedded map has no useful size bound, so the scan must not be capped:
    # any fixed window is just a larger version of the same defect.
    (source / "huge.ts").write_text("export const huge = 1;\n", encoding="utf-8")
    huge = source / "huge.js"
    huge.write_text(
        "exports.huge = 1;\n"
        "//# sourceMappingURL=data:application/json;base64,"
        + ("A" * (8 * 1024 * 1024 + 1024))
        + "\n",
        encoding="utf-8",
    )
    assert len(huge.read_bytes()) > 8 * 1024 * 1024
    assert is_typescript_emit_artifact(huge) is True

    # An empty runtime file must not raise through the memory-mapped path.
    (source / "empty.ts").write_text("export const empty = 1;\n", encoding="utf-8")
    (source / "empty.js").write_text("", encoding="utf-8")
    assert is_typescript_emit_artifact(source / "empty.js") is False


def test_source_mapping_directive_survives_a_chunk_boundary_split() -> None:
    directive = b"\n//# sourceMappingURL=data:x\n"
    for offset in (
        _SOURCE_MAPPING_CHUNK_BYTES - len(directive),
        _SOURCE_MAPPING_CHUNK_BYTES - 1,
        _SOURCE_MAPPING_CHUNK_BYTES,
    ):
        stream = io.BytesIO(b"x" * offset + directive + b"y" * 64)
        assert _chunked_contains(stream) is True
    assert _chunked_contains(io.BytesIO(b"z" * 4096)) is False
    # Past the first chunk the buffer start is mid-file, so a directive-shaped
    # run that is not line-leading must not be anchored by it.
    mid_line = io.BytesIO(
        b"x" * (_SOURCE_MAPPING_CHUNK_BYTES - 21) + b"//# sourceMappingURL=" + b"y" * 64
    )
    assert _chunked_contains(mid_line) is False


@pytest.mark.parametrize(
    ("body", "is_emit"),
    [
        ("exports.v = 1;\n//# sourceMappingURL=out.js.map\n", True),
        ("exports.v = 1;\n//@ sourceMappingURL=out.js.map\n", True),
        ("exports.v = 1;\n/*# sourceMappingURL=out.js.map */\n", True),
        ("exports.v = 1;\n  //# sourceMappingURL=out.js.map\n", True),
        # Ordinary source that merely names the field is not build output.
        ('const FIELD = "sourceMappingURL=";\nmodule.exports = { FIELD };\n', False),
        ('exports.parse = (s) => s.indexOf("sourceMappingURL=");\n', False),
        ("const x = 1; // mentions sourceMappingURL= mid-line\n", False),
    ],
)
def test_only_a_line_leading_directive_marks_generated_output(
    tmp_path: Path,
    body: str,
    is_emit: bool,
) -> None:
    source = tmp_path / "src"
    source.mkdir(parents=True)
    (source / "mod.ts").write_text("export const v = 1;\n", encoding="utf-8")
    runtime = source / "mod.js"
    runtime.write_text(body, encoding="utf-8")

    assert is_typescript_emit_artifact(runtime) is is_emit


def test_scanner_drops_declaration_emits_but_keeps_standalone_declarations(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    sources = {
        "src/foo.ts": "export const foo = 1;\n",
        "src/foo.d.ts": "export declare const foo: number;\n",
        "src/module.mts": "export const moduleValue = 1;\n",
        "src/module.d.mts": "export declare const moduleValue: number;\n",
        "src/types.d.ts": "export interface Options { enabled: boolean }\n",
    }
    for relative_path, content in sources.items():
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    records = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("src")],
        file_extensions=(".ts", ".mts"),
    ).scan()

    assert [record.path for record in records] == [
        "src/foo.ts",
        "src/module.mts",
        "src/types.d.ts",
    ]


def test_scanner_gives_coexisting_jsx_its_own_module_identity(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "widget.ts").write_text(
        "export const widget = 'typed';\n",
        encoding="utf-8",
    )
    (source_root / "widget.jsx").write_text(
        "export const widget = 'jsx';\n",
        encoding="utf-8",
    )

    records = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("src")],
        file_extensions=(".ts", ".jsx"),
    ).scan()
    by_path = {record.path: record.module for record in records}

    assert by_path["src/widget.ts"] == "widget"
    assert by_path["src/widget.jsx"].startswith("widget.__arcgraph_variant_jsx_")
    assert by_path["src/widget.jsx"] != by_path["src/widget.ts"]


def test_typescript_variant_module_identity_ignores_lane_membership(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    source_root = repo_root / "src"
    source_root.mkdir(parents=True)
    (source_root / "dual.ts").write_text(
        "export const typed = 1;\n",
        encoding="utf-8",
    )
    (source_root / "dual.mjs").write_text(
        "export const runtime = 1;\n",
        encoding="utf-8",
    )
    scanner = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("src")],
        file_extensions=(".ts", ".mts", ".mjs"),
    )

    before = {record.path: record.module for record in scanner.scan()}
    (source_root / "dual.d.mts").write_text(
        "export declare const runtime: number;\n",
        encoding="utf-8",
    )
    after = {record.path: record.module for record in scanner.scan()}

    assert after["src/dual.mjs"] == before["src/dual.mjs"]
    assert after["src/dual.d.mts"] == before["src/dual.mjs"]


def test_typescript_variant_grouping_is_scoped_to_each_source_root(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    sources = {
        "frontend/lib/util.ts": "export const frontend = 1;\n",
        "backend/lib/util.mjs": "export const backend = 1;\n",
    }
    for relative_path, content in sources.items():
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    records = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("frontend"), SourceRoot("backend")],
        file_extensions=(".ts", ".mjs"),
    ).scan()

    assert {record.path: record.module for record in records} == {
        "backend/lib/util.mjs": "lib.util",
        "frontend/lib/util.ts": "lib.util",
    }


def test_source_root_detection_counts_python_from_submodule(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git" / "modules" / "vendor-copy").mkdir(parents=True)
    checkout = tmp_path / "scripts" / "vendor-copy"
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text(
        "gitdir: ../../.git/modules/vendor-copy\n",
        encoding="utf-8",
    )
    (checkout / "copied.py").write_text("VALUE = 1\n", encoding="utf-8")

    detected = detect_source_roots(tmp_path)

    assert detected.roots == (SourceRoot("scripts", "scripts"),)
    assert detected.detection.strategy == "package_heuristic"


def test_source_root_detection_excludes_same_repository_linked_worktree(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    linked_root = repo_root / "wt"
    linked_root.mkdir()
    (linked_root / "copied.py").write_text("VALUE = 2\n", encoding="utf-8")
    _register_linked_worktree(repo_root, linked_root, name="wt")

    detected = detect_source_roots(repo_root)

    assert detected.roots == (SourceRoot("src", "src"),)
    assert detected.detection.non_package_dirs == ("src",)


def test_explicit_source_root_does_not_fall_back_when_it_is_a_linked_worktree(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    (repo_root / "src").mkdir()
    (repo_root / "src" / "keep.py").write_text("VALUE = 1\n", encoding="utf-8")
    linked_root = repo_root / "wt"
    linked_root.mkdir()
    (linked_root / "only.py").write_text("VALUE = 2\n", encoding="utf-8")
    _register_linked_worktree(repo_root, linked_root, name="wt")
    (repo_root / "pyproject.toml").write_text(
        '[tool.arcgraph]\nsource_roots = ["wt"]\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Explicit.*source_roots"):
        detect_source_roots(repo_root)


def test_source_root_detection_does_not_add_typescript_root_from_worktree(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts" / "keep.py").write_text("VALUE = 1\n", encoding="utf-8")
    linked_root = repo_root / "frontend"
    (linked_root / "src").mkdir(parents=True)
    (linked_root / "package.json").write_text("{}\n", encoding="utf-8")
    (linked_root / "src" / "copied.ts").write_text(
        "export const copied = true;\n",
        encoding="utf-8",
    )
    _register_linked_worktree(repo_root, linked_root, name="frontend")

    detected = detect_source_roots(repo_root)

    assert detected.roots == (SourceRoot("scripts", "scripts"),)


def test_declaration_companion_alone_does_not_mark_runtime_emit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    source.mkdir(parents=True)
    (source / "shim.ts").write_text("export const value = 1;\n", encoding="utf-8")
    (source / "shim.d.ts").write_text(
        "export declare const value: number;\n", encoding="utf-8"
    )
    (source / "shim.js").write_text("exports.value = 1;\n", encoding="utf-8")

    # Under emitDeclarationOnly the declaration proves the .ts was compiled,
    # not that the runtime file was emitted: the same-stem .js stays source.
    assert is_typescript_emit_artifact(source / "shim.js") is False

    (source / "shim.js.map").write_text("{}\n", encoding="utf-8")
    assert is_typescript_emit_artifact(source / "shim.js") is True


def test_scan_reports_skipped_emit_artifacts_in_one_warning(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    source = repo_root / "src"
    source.mkdir(parents=True)
    (source / "kept.ts").write_text("export const kept = 1;\n", encoding="utf-8")
    (source / "built.ts").write_text("export const built = 1;\n", encoding="utf-8")
    (source / "built.js").write_text(
        "exports.built = 1;\n//# sourceMappingURL=built.js.map\n", encoding="utf-8"
    )

    scanner = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("src")],
        file_extensions=(".ts", ".js"),
    )
    records = scanner.scan()

    assert "src/built.js" not in [record.path for record in records]
    skip_warnings = [
        warning
        for warning in scanner.warnings
        if warning.kind == "typescript_emit_artifact_skipped"
    ]
    assert len(skip_warnings) == 1
    assert "Skipped 1 runtime file(s)" in skip_warnings[0].message
    assert "src/built.js" in skip_warnings[0].message


def test_configured_root_inside_linked_worktree_warns(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    worktree = repo_root / ".claude" / "worktrees" / "feat"
    source = worktree / "src"
    source.mkdir(parents=True)
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _register_linked_worktree(repo_root, worktree, name="feat")

    scanner = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot(".claude/worktrees/feat/src")],
    )
    records = scanner.scan()

    # The explicitly configured root is excluded, but never silently: an
    # empty index without a diagnostic is undebuggable.
    assert records == []
    assert any(
        warning.kind == "source_root_in_linked_worktree" for warning in scanner.warnings
    )


def test_declaration_emits_are_not_counted_in_skip_warning(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    source = repo_root / "src"
    source.mkdir(parents=True)
    (source / "mod.ts").write_text("export const value = 1;\n", encoding="utf-8")
    (source / "mod.d.ts").write_text(
        "export declare const value: number;\n", encoding="utf-8"
    )

    scanner = FileScanner(
        repo_root=repo_root,
        source_roots=[SourceRoot("src")],
        file_extensions=(".ts",),
    )
    records = scanner.scan()

    # The declaration emit is dropped as always, but it is not a runtime file
    # and must not inflate the runtime skip disclosure.
    assert [record.path for record in records] == ["src/mod.ts"]
    assert not any(
        warning.kind == "typescript_emit_artifact_skipped"
        for warning in scanner.warnings
    )


def test_self_index_excludes_only_the_unshipped_semantic_experiment() -> None:
    import fnmatch
    import subprocess

    root = Path(__file__).resolve().parents[2]
    tracked = subprocess.check_output(
        ["git", "ls-files"], cwd=root, text=True, encoding="utf-8"
    ).splitlines()
    expected = {
        path
        for path in tracked
        if path.startswith(
            (
                "arcgraph/semantic_prototype/",
                "arcgraph/tests/fixtures/semantic_prototype/",
            )
        )
        or path
        in {
            "arcgraph/tests/test_semantic_prototype.py",
            "arcgraph/tests/test_semantic_pipeline.py",
            "arcgraph/tests/test_semantic_packaging.py",
            "arcgraph/tests/test_structure_provider.py",
        }
    }
    patterns = read_arcgraph_exclude(root)
    matched = {
        path for path in tracked if any(fnmatch.fnmatch(path, p) for p in patterns)
    }
    assert expected
    assert matched == expected
    assert "arcgraph/tests/test_scanner.py" not in matched


def test_self_index_configuration_keeps_product_and_general_tests(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "pyproject.toml").write_bytes((root / "pyproject.toml").read_bytes())
    kept = {"arcgraph/core/scanner.py", "arcgraph/tests/test_scanner.py"}
    skipped = {
        "arcgraph/semantic_prototype/future_module.py",
        "arcgraph/tests/test_semantic_prototype.py",
        "arcgraph/tests/test_semantic_pipeline.py",
        "arcgraph/tests/test_semantic_packaging.py",
        "arcgraph/tests/test_structure_provider.py",
        "arcgraph/tests/fixtures/semantic_prototype/nested/example.py",
    }
    for rel in kept | skipped:
        file = tmp_path / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("VALUE = 1\n", encoding="utf-8")
    resolved = detect_source_roots(tmp_path)
    scanner = FileScanner(
        tmp_path,
        resolved.roots,
        [*DEFAULT_IGNORE_RULES, *resolved.detection.exclude],
    )
    assert {file.path for file in scanner.scan()} == kept
