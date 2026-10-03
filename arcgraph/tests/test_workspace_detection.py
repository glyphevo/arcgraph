"""Tests for workspace discovery (Phase 1)."""

from __future__ import annotations

from pathlib import Path

from arcgraph.core.scanner import (
    LEGACY_SOURCE_ROOTS,
    ResolvedSourceRoots,
    SourceRoot,
    _dedup_nested_roots,
    _detect_from_packages_and_scripts,
    _detect_from_pyproject,
    _detect_from_setup_cfg,
    detect_source_roots,
)

# ---------------------------------------------------------------------------
#  pyproject.toml — [tool.arcgraph] explicit config
# ---------------------------------------------------------------------------


def test_arcgraph_explicit_config(tmp_path: Path) -> None:
    """[tool.arcgraph] source_roots overrides everything."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.arcgraph]\nsource_roots = ["backend/src", "scripts"]',
        encoding="utf-8",
    )
    (tmp_path / "backend" / "src").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    result = _detect_from_pyproject(tmp_path)
    assert result is not None
    roots, strategy = result
    assert strategy == "pyproject_arcgraph"
    assert SourceRoot("backend/src") in roots
    assert SourceRoot("scripts") in roots


def test_arcgraph_explicit_config_with_prefix(tmp_path: Path) -> None:
    """[tool.arcgraph] source_roots supports dict entries with module_prefix."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.arcgraph]\n"
        'source_roots = [{path = "scripts", module_prefix = "scripts"}]',
        encoding="utf-8",
    )
    result = _detect_from_pyproject(tmp_path)
    assert result is not None
    roots, strategy = result
    assert strategy == "pyproject_arcgraph"
    assert SourceRoot("scripts", "scripts") in roots


def test_detect_source_roots_adds_conventional_typescript_frontend(
    tmp_path: Path,
) -> None:
    """Mixed monorepos should expose frontend/src to the TS frontend."""
    (tmp_path / "backend" / "src" / "api").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "api" / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "tests").mkdir(parents=True)
    (tmp_path / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.tsx").write_text(
        "export function App() { return <main />; }\n",
        encoding="utf-8",
    )

    result = detect_source_roots(tmp_path)

    assert result.detection.strategy == "package_heuristic"
    assert SourceRoot("backend/src") in result.roots
    assert SourceRoot("frontend/src") in result.roots
    assert SourceRoot("frontend/tests", "frontend_tests") in result.roots


def test_mixed_python_typescript_discovery_skips_generated_roots(
    tmp_path: Path,
) -> None:
    """Generated/build roots must not make mixed monorepos fall back to repo root."""
    (tmp_path / "backend" / "src" / "api").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "api" / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "App.tsx").write_text(
        "export function App() { return null; }\n",
        encoding="utf-8",
    )

    for dirname in ("build", "dist", "generated", ".next"):
        generated_pkg = tmp_path / dirname / "pkg"
        generated_pkg.mkdir(parents=True)
        (generated_pkg / "__init__.py").write_text("", encoding="utf-8")
        (generated_pkg / "artifact.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = detect_source_roots(tmp_path)
    paths = {root.path for root in result.roots}

    assert result.detection.strategy == "package_heuristic"
    assert "backend/src" in paths
    assert "frontend/src" in paths
    assert "." not in paths
    assert not {"build", "dist", "generated", ".next"} & paths


# ---------------------------------------------------------------------------
#  pyproject.toml — Hatchling
# ---------------------------------------------------------------------------


def test_hatch_flat_package(tmp_path: Path) -> None:
    """packages=["arcgraph"] → SourceRoot(".", "")"""
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\n\n'
        '[project]\nname = "arcgraph"\nversion = "0.1.0"\n\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["arcgraph"]\n',
        encoding="utf-8",
    )
    (tmp_path / "arcgraph").mkdir()
    result = _detect_from_pyproject(tmp_path)
    assert result is not None
    roots, strategy = result
    assert strategy == "pyproject_hatch"
    assert SourceRoot(".", "") in roots


def test_hatch_src_rewrite(tmp_path: Path) -> None:
    """packages=["src/api", "src/services"], sources={"src": ""} → SourceRoot("src", "")"""
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\n\n'
        '[project]\nname = "sampleapp"\nversion = "0.5.0"\n\n'
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["src/api", "src/services"]\n\n'
        "[tool.hatch.build.targets.wheel.sources]\n"
        '"src" = ""\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "services").mkdir(parents=True)
    result = _detect_from_pyproject(tmp_path)
    assert result is not None
    roots, strategy = result
    assert strategy == "pyproject_hatch"
    # All packages under src/ should deduce to one import root: "src"
    assert len(roots) == 1
    assert roots[0] == SourceRoot("src", "")


# ---------------------------------------------------------------------------
#  pyproject.toml — setuptools
# ---------------------------------------------------------------------------


def test_setuptools_find_where(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    result = _detect_from_pyproject(tmp_path)
    assert result is not None
    roots, strategy = result
    assert strategy == "pyproject_setuptools"
    assert SourceRoot("src", "") in roots


# ---------------------------------------------------------------------------
#  pyproject.toml — [project].name heuristic
# ---------------------------------------------------------------------------


def test_project_name_heuristic_src_layout(tmp_path: Path) -> None:
    """[project].name with dashes → normalize → check src/<normalized>."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "my-cool-lib"\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "my_cool_lib").mkdir(parents=True)
    result = _detect_from_pyproject(tmp_path)
    assert result is not None
    roots, strategy = result
    assert strategy == "pyproject_name"
    assert SourceRoot("src", "") in roots


def test_project_name_nonexistent_skipped(tmp_path: Path) -> None:
    """[project].name → src/<name> does not exist → returns None."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "nonexistent-pkg"\n',
        encoding="utf-8",
    )
    result = _detect_from_pyproject(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
#  setup.cfg
# ---------------------------------------------------------------------------


def test_setup_cfg_where(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text(
        "[options.packages.find]\nwhere = src\n",
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    result = _detect_from_setup_cfg(tmp_path)
    assert result is not None
    roots, strategy = result
    assert strategy == "setup_cfg"
    assert SourceRoot("src", "") in roots


# ---------------------------------------------------------------------------
#  Package heuristic + non-package dirs
# ---------------------------------------------------------------------------


def test_package_import_root_deduction(tmp_path: Path) -> None:
    """src/pkg/__init__.py → SourceRoot("src", "")"""
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    result = _detect_from_packages_and_scripts(tmp_path)
    assert result is not None
    roots, _ = result
    assert SourceRoot("src", "") in roots


def test_flat_layout_package(tmp_path: Path) -> None:
    """pkg/__init__.py at repo root → SourceRoot(".", "")"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    result = _detect_from_packages_and_scripts(tmp_path)
    assert result is not None
    roots, _ = result
    assert SourceRoot(".", "") in roots


def test_non_package_dir_with_prefix(tmp_path: Path) -> None:
    """scripts/foo.py (no __init__.py) → SourceRoot("scripts", "scripts")"""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "foo.py").write_text("# script", encoding="utf-8")
    result = _detect_from_packages_and_scripts(tmp_path)
    assert result is not None
    roots, non_pkg_dirs = result
    assert SourceRoot("scripts", "scripts") in roots
    assert "scripts" in non_pkg_dirs


def test_same_path_conflict_package_wins(tmp_path: Path) -> None:
    """Same path from both step A (package) and step B (non-package) → A wins."""
    # Create a dir that is both an import root (has a subdirectory with __init__.py)
    # and has standalone .py files
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "pkg").mkdir()
    (tmp_path / "lib" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "lib" / "main.py").write_text("# standalone", encoding="utf-8")
    result = _detect_from_packages_and_scripts(tmp_path)
    assert result is not None
    roots, _ = result
    # "lib" should appear with empty prefix (package import-root), not "lib" prefix
    lib_roots = [r for r in roots if r.path == "lib"]
    assert len(lib_roots) == 1
    assert lib_roots[0].module_prefix == ""


# ---------------------------------------------------------------------------
#  Parent-child dedup
# ---------------------------------------------------------------------------


def test_dedup_nested_roots() -> None:
    """scripts/ + scripts/tests/ → only scripts/"""
    roots = [
        SourceRoot("scripts", "scripts"),
        SourceRoot("scripts/tests", "tests"),
    ]
    deduped = _dedup_nested_roots(roots)
    assert len(deduped) == 1
    assert deduped[0] == SourceRoot("scripts", "scripts")


def test_dedup_dot_root_covers_all() -> None:
    """'.' root covers everything else."""
    roots = [
        SourceRoot(".", ""),
        SourceRoot("src", ""),
        SourceRoot("scripts", "scripts"),
    ]
    deduped = _dedup_nested_roots(roots)
    assert len(deduped) == 1
    assert deduped[0].path == "."


def test_dedup_no_false_positive() -> None:
    """Unrelated roots should all be kept."""
    roots = [
        SourceRoot("backend/src", ""),
        SourceRoot("arcgraph", ""),
        SourceRoot("scripts", "scripts"),
    ]
    deduped = _dedup_nested_roots(roots)
    assert len(deduped) == 3


# ---------------------------------------------------------------------------
#  detect_source_roots() integration
# ---------------------------------------------------------------------------


def test_detect_empty_project(tmp_path: Path) -> None:
    """Empty project → repo root fallback, no crash."""
    result = detect_source_roots(tmp_path)
    assert isinstance(result, ResolvedSourceRoots)
    assert result.detection.strategy == "repo_root_fallback"
    assert len(result.roots) >= 1


def test_resolved_source_roots_detection_info(tmp_path: Path) -> None:
    """ResolvedSourceRoots always has detection info."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.arcgraph]\nsource_roots = ["src"]',
        encoding="utf-8",
    )
    result = detect_source_roots(tmp_path)
    assert result.detection is not None
    assert result.detection.strategy == "pyproject_arcgraph"
    assert "src" in result.detection.roots


def test_source_root_specs_persistence(tmp_path: Path) -> None:
    """source_root_specs should preserve module_prefix for round-trip."""
    roots = (SourceRoot("scripts", "scripts"), SourceRoot("backend/src", ""))
    specs = [{"path": root.path, "module_prefix": root.module_prefix} for root in roots]
    # Round-trip: convert specs back to SourceRoot
    recovered = tuple(
        SourceRoot(path=s["path"], module_prefix=s["module_prefix"]) for s in specs
    )
    assert recovered == roots


# ---------------------------------------------------------------------------
#  Legacy alias
# ---------------------------------------------------------------------------


def test_legacy_alias_available() -> None:
    """DEFAULT_SOURCE_ROOTS should still work as alias."""
    from arcgraph.core.scanner import DEFAULT_SOURCE_ROOTS

    assert DEFAULT_SOURCE_ROOTS is LEGACY_SOURCE_ROOTS


def test_non_package_root_detects_nested_test_dir(tmp_path: Path) -> None:
    """backend/tests/unit/test_x.py must be detected even if tests/ has no direct .py."""
    (tmp_path / "backend" / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "unit" / "test_x.py").write_text(
        "def test_hello(): pass\n", encoding="utf-8"
    )
    result = _detect_from_packages_and_scripts(tmp_path)
    assert result is not None
    roots, non_pkg_dirs = result
    paths = {r.path for r in roots}
    assert "backend/tests" in paths
    # backend itself must NOT be a root (it is just a namespace directory)
    assert "backend" not in paths


def test_non_package_root_detects_deep_test_tree(tmp_path: Path) -> None:
    """Depth 3+ test tree: backend/tests/unit/api/test_x.py must still detect backend/tests.

    Invariant: conventional dirs (tests/test/scripts) use recursive discovery,
    so arbitrarily deep .py files are found. Non-conventional dirs stay bounded.
    """
    (tmp_path / "backend" / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "pkg" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "backend" / "tests" / "unit" / "api").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "unit" / "api" / "test_x.py").write_text(
        "def test_hello(): pass\n", encoding="utf-8"
    )

    result = detect_source_roots(tmp_path)
    paths = {r.path for r in result.roots}
    non_pkg = result.detection.non_package_dirs

    assert "backend/src" in paths, f"Must find package root; got {paths}"
    assert "backend/tests" in paths, f"Must find deep test root; got {paths}"
    assert "backend" not in paths, f"Namespace dir must not be a root; got {paths}"
    assert "backend/tests" in non_pkg, f"Must record test root; got {non_pkg}"


def test_non_package_root_does_not_swallow_package_root(tmp_path: Path) -> None:
    """End-to-end invariant: precise package roots must not be swallowed by parent dirs.

    Given:
      backend/src/pkg/__init__.py  →  step A finds SourceRoot("backend/src", "")
      backend/tests/unit/test_x.py →  step B finds SourceRoot("backend/tests", "tests")

    Expected: detect_source_roots returns backend/src AND backend/tests.
    Must NOT return SourceRoot("backend", "backend") that covers both.
    """
    (tmp_path / "backend" / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "pkg" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "backend" / "src" / "pkg" / "main.py").write_text(
        "X = 1\n", encoding="utf-8"
    )
    (tmp_path / "backend" / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "unit" / "test_x.py").write_text(
        "def test_hello(): pass\n", encoding="utf-8"
    )

    result = detect_source_roots(tmp_path)
    paths = {r.path for r in result.roots}

    assert "backend/src" in paths, f"Must find package import root; got {paths}"
    assert "backend/tests" in paths, f"Must find non-package test root; got {paths}"
    assert "backend" not in paths, f"Parent dir must not swallow children; got {paths}"


def test_nested_pyproject_package_root_does_not_promote_packages_parent(
    tmp_path: Path,
) -> None:
    """A package under packages/<name> should use that project directory as root."""
    project_root = tmp_path / "packages" / "arcgraph"
    package_dir = project_root / "arcgraph"
    package_dir.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text(
        '[project]\nname = "arcgraph"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "cli.py").write_text("def main(): pass\n", encoding="utf-8")

    result = detect_source_roots(tmp_path)
    paths = {r.path for r in result.roots}

    assert "packages/arcgraph" in paths
    assert "packages" not in paths


def test_output_install_artifacts_do_not_become_source_roots(tmp_path: Path) -> None:
    """Generated install-check packages under output/ must not affect discovery."""
    package_dir = tmp_path / "packages" / "arcgraph" / "arcgraph"
    package_dir.mkdir(parents=True)
    (tmp_path / "packages" / "arcgraph" / "pyproject.toml").write_text(
        '[project]\nname = "arcgraph"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    installed_dir = tmp_path / "output" / "install-check" / "arcgraph"
    installed_dir.mkdir(parents=True)
    (installed_dir / "__init__.py").write_text("", encoding="utf-8")

    result = detect_source_roots(tmp_path)
    paths = {r.path for r in result.roots}

    assert "packages/arcgraph" in paths
    assert "output/install-check" not in paths


def test_non_package_dirs_metadata_only_contains_resolved(tmp_path: Path) -> None:
    """Invariant: non_package_dirs must only record final resolved non-package roots.

    Given:
      backend/src/pkg/__init__.py  → package root (step A)
      backend/tests/unit/test_x.py → non-package root (step B)

    non_package_dirs must contain only "backend/tests", not "backend".
    """
    (tmp_path / "backend" / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "pkg" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "backend" / "src" / "pkg" / "main.py").write_text(
        "X = 1\n", encoding="utf-8"
    )
    (tmp_path / "backend" / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "unit" / "test_x.py").write_text(
        "def test_hello(): pass\n", encoding="utf-8"
    )

    result = detect_source_roots(tmp_path)
    non_pkg = result.detection.non_package_dirs

    assert (
        "backend/tests" in non_pkg
    ), f"Must record kept non-package root; got {non_pkg}"
    assert "backend" not in non_pkg, f"Filtered parent must not appear; got {non_pkg}"
    assert "backend/src" not in non_pkg, f"Package root must not appear; got {non_pkg}"


def test_step_a_nested_package_roots_both_survive(tmp_path: Path) -> None:
    """Step A import roots from different package trees must both survive.

    Real-repo scenario:
      backend/evals/__init__.py → step A finds SourceRoot("backend", "")
      backend/src/api/__init__.py → step A finds SourceRoot("backend/src", "")

    Both are valid independent import roots. Dedup must NOT collapse
    backend/src into backend.
    """
    (tmp_path / "backend" / "evals").mkdir(parents=True)
    (tmp_path / "backend" / "evals" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "backend" / "evals" / "eval_main.py").write_text(
        "X = 1\n", encoding="utf-8"
    )
    (tmp_path / "backend" / "src" / "api").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "api" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "backend" / "src" / "api" / "routes.py").write_text(
        "Y = 2\n", encoding="utf-8"
    )

    result = detect_source_roots(tmp_path)
    paths = {r.path for r in result.roots}

    assert "backend" in paths, f"Must find backend (import root for evals); got {paths}"
    assert (
        "backend/src" in paths
    ), f"Must find backend/src (import root for api); got {paths}"


def test_step_b_nested_non_package_dedup_consistent(tmp_path: Path) -> None:
    """Step B dedup (scripts covers scripts/tests) must sync with non_package_dirs.

    After dedup, both roots and non_package_dirs should agree:
    only "scripts" remains, not "scripts/tests".
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "scripts" / "tests").mkdir()
    (tmp_path / "scripts" / "tests" / "test_run.py").write_text(
        "pass\n", encoding="utf-8"
    )

    result = detect_source_roots(tmp_path)
    paths = {r.path for r in result.roots}
    non_pkg = result.detection.non_package_dirs

    assert "scripts" in paths
    assert "scripts/tests" not in paths, f"Child must be deduped; got {paths}"
    assert (
        "scripts/tests" not in non_pkg
    ), f"non_package_dirs must agree with roots; got {non_pkg}"


def test_explicit_arcgraph_config_not_deduped(tmp_path: Path) -> None:
    """Explicit [tool.arcgraph] source_roots must not be deduped.

    If a user intentionally configures nested roots with different prefixes,
    both must survive.
    """
    (tmp_path / "app" / "core").mkdir(parents=True)
    (tmp_path / "app" / "core" / "main.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "app" / "plugins" / "auth").mkdir(parents=True)
    (tmp_path / "app" / "plugins" / "auth" / "handler.py").write_text(
        "pass\n", encoding="utf-8"
    )
    pyproject_toml = """\
[tool.arcgraph]
source_roots = [
    {path = "app", module_prefix = ""},
    {path = "app/plugins", module_prefix = "plugins"},
]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_toml, encoding="utf-8")

    result = detect_source_roots(tmp_path)
    paths = {r.path for r in result.roots}

    assert "app" in paths, f"Parent root must survive; got {paths}"
    assert "app/plugins" in paths, f"Child root must NOT be deduped; got {paths}"


# ---------------------------------------------------------------------------
#  _safe_rel_path regression tests
# ---------------------------------------------------------------------------


def test_safe_rel_path_windows_drive_letter() -> None:
    """Windows C:\\ paths must be detected as absolute and not leaked."""
    from arcgraph.interfaces.reports import _safe_rel_path

    result = _safe_rel_path(r"C:\Users\alice\repo\output\metrics.jsonl")
    assert result == "metrics.jsonl"
    assert "Users" not in result
    assert "alice" not in result


def test_safe_rel_path_windows_drive_with_repo_root() -> None:
    """Windows path inside repo → proper relative path."""
    from arcgraph.interfaces.reports import _safe_rel_path

    result = _safe_rel_path(
        r"C:\Users\alice\repo\output\metrics.jsonl",
        repo_root=r"C:\Users\alice\repo",
    )
    assert result == "output/metrics.jsonl"


def test_safe_rel_path_cross_drive_no_leak() -> None:
    """Different Windows drives must NOT match: C:\\repo\\secret vs D:\\repo."""
    from arcgraph.interfaces.reports import _safe_rel_path

    result = _safe_rel_path(
        r"C:\repo\secret\metrics.jsonl",
        repo_root=r"D:\repo",
    )
    # Must return basename, not a relative path that leaks "secret"
    assert result == "metrics.jsonl"
    assert "secret" not in result


def test_safe_rel_path_posix_absolute() -> None:
    """POSIX absolute paths are still handled correctly."""
    from arcgraph.interfaces.reports import _safe_rel_path

    result = _safe_rel_path("/home/alice/repo/output/report.html")
    assert result == "report.html"

    result = _safe_rel_path(
        "/home/alice/repo/output/report.html",
        repo_root="/home/alice/repo",
    )
    assert result == "output/report.html"


def test_safe_rel_path_relative_passthrough() -> None:
    """Relative paths pass through unchanged."""
    from arcgraph.interfaces.reports import _safe_rel_path

    assert _safe_rel_path("output/report.html") == "output/report.html"
    assert _safe_rel_path("metrics.jsonl") == "metrics.jsonl"


def test_safe_rel_path_unc_path() -> None:
    """UNC paths (\\\\server\\share) are treated as absolute."""
    from arcgraph.interfaces.reports import _safe_rel_path

    result = _safe_rel_path(r"\\server\share\data\metrics.jsonl")
    assert result == "metrics.jsonl"
    assert "server" not in result


# ---------------------------------------------------------------------------
#  FileScanner nested-root dedup
# ---------------------------------------------------------------------------


def test_file_scanner_nested_roots_no_duplicate_paths(tmp_path: Path) -> None:
    """Invariant: each physical file appears exactly once in scan results.

    When roots are nested (backend, backend/src, backend/tests),
    FileScanner must assign each file to the most specific root.
    backend/src/api/routes.py → root backend/src, module api.routes (not src.api.routes).
    """
    from arcgraph.core.scanner import FileScanner, SourceRoot

    (tmp_path / "backend" / "evals").mkdir(parents=True)
    (tmp_path / "backend" / "evals" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "backend" / "evals" / "eval_main.py").write_text(
        "X = 1\n", encoding="utf-8"
    )
    (tmp_path / "backend" / "src" / "api").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "api" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "backend" / "src" / "api" / "routes.py").write_text(
        "Y = 2\n", encoding="utf-8"
    )
    (tmp_path / "backend" / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "unit" / "test_x.py").write_text(
        "def test_hello(): pass\n", encoding="utf-8"
    )

    scanner = FileScanner(
        repo_root=tmp_path,
        source_roots=[
            SourceRoot("backend", ""),
            SourceRoot("backend/src", ""),
            SourceRoot("backend/tests", "tests"),
        ],
    )
    records = scanner.scan()
    paths = [r.path for r in records]

    # No duplicate paths
    assert len(paths) == len(set(paths)), f"Duplicate paths found: {paths}"

    # Module for routes.py must use most-specific root (backend/src → api.routes)
    routes_records = [r for r in records if r.path.endswith("routes.py")]
    assert len(routes_records) == 1
    assert (
        routes_records[0].module == "api.routes"
    ), f"Expected api.routes, got {routes_records[0].module}"
    assert routes_records[0].source_root == "backend/src"

    # evals/eval_main.py must use backend root → evals.eval_main
    eval_records = [r for r in records if r.path.endswith("eval_main.py")]
    assert len(eval_records) == 1
    assert eval_records[0].module == "evals.eval_main"
    assert eval_records[0].source_root == "backend"


# ---------------------------------------------------------------------------
#  MCP _sanitize_path_value cross-platform
# ---------------------------------------------------------------------------


def test_mcp_sanitize_posix_absolute_on_any_os() -> None:
    """POSIX absolute paths must be detected on any OS, not just POSIX."""
    from arcgraph.interfaces.mcp_tools import _sanitize_path_value
    from pathlib import Path as P

    # /home/alice/secret.py is a POSIX absolute path — must not leak
    result = _sanitize_path_value("/home/alice/secret.py", P("C:/repo"))
    assert result == "<external>", f"POSIX absolute path leaked: {result}"

    # Extensionless POSIX absolute paths must also be caught
    result = _sanitize_path_value("/tmp/secret", P("C:/repo"))
    assert result == "<external>", f"Extensionless POSIX path leaked: {result}"

    result = _sanitize_path_value("/usr/bin/python", P("C:/repo"))
    assert result == "<external>", f"Extensionless POSIX path leaked: {result}"

    # Relative project paths are fine
    result = _sanitize_path_value("backend/src/foo.py", P("C:/repo"))
    assert result == "backend/src/foo.py"


# ---------------------------------------------------------------------------
#  Structural hierarchy nested-root assignment
# ---------------------------------------------------------------------------


def test_structural_module_uses_most_specific_root() -> None:
    """Module under nested root must be linked to the most specific root.

    Given roots backend, backend/src:
    - backend/src/main.py (source_root=backend/src) must be linked to
      source_root:backend/src, NOT source_root:backend.
    """
    from arcgraph.core.structural import generate_structural_hierarchy
    from arcgraph.core.schemas import Node
    from arcgraph.core.scanner import SourceRoot

    roots = [
        SourceRoot("backend", ""),
        SourceRoot("backend/src", ""),
    ]
    existing_nodes = [
        Node(
            id="mod:main",
            kind="module",
            name="main",
            path="backend/src/main.py",
            qualname="main",
        ),
        Node(
            id="mod:evals.runner",
            kind="module",
            name="runner",
            path="backend/evals/runner.py",
            qualname="evals.runner",
        ),
    ]
    _, edges = generate_structural_hierarchy(
        roots,
        existing_nodes=existing_nodes,
    )
    contains_edges = [e for e in edges if e.kind == "contains"]

    # main.py must be under source_root:backend/src
    main_parents = [e.source for e in contains_edges if e.target == "mod:main"]
    assert (
        "source_root:backend/src" in main_parents
    ), f"main.py should be under backend/src; got parents {main_parents}"
    assert (
        "source_root:backend" not in main_parents
    ), f"main.py must NOT be under backend; got parents {main_parents}"

    # runner.py must be under source_root:backend
    runner_parents = [
        e.source for e in contains_edges if e.target == "mod:evals.runner"
    ]
    assert any(
        "backend" in p and "src" not in p for p in runner_parents
    ), f"runner.py should be under backend; got parents {runner_parents}"


def test_mcp_sanitize_route_path_not_treated_as_filesystem() -> None:
    """Route paths in properties.path must be preserved by _contain_paths.

    Context-aware: top-level 'path' fields are sanitized, but
    'properties.path' contains domain values (route paths) and must
    pass through unchanged.
    """
    from arcgraph.interfaces.mcp_tools import _contain_paths
    from pathlib import Path as P

    repo = P("C:/repo")
    payload = {
        "nodes": [
            {
                "kind": "route",
                "path": "/home/alice/secret.py",  # top-level: must sanitize
                "properties": {
                    "path": "/api/v1/memories",  # domain: must preserve
                },
            },
            {
                "kind": "route",
                "path": "backend/src/routes.py",  # relative: must preserve
                "properties": {
                    "path": "/health",  # domain: must preserve
                },
            },
            {
                "kind": "diagnostic",
                "properties": {
                    "path": "/tmp/secret",
                    "source_path": "/home/alice/source.py",
                    "reanalysis_recommended_paths": [
                        "backend/src/routes.py",
                        "/home/alice/private.py",
                    ],
                },
            },
        ],
    }
    result = _contain_paths(payload, repo)
    nodes = result["nodes"]

    # Top-level absolute path must be sanitized
    assert nodes[0]["path"] == "<external>"
    # properties.path (route) must be preserved
    assert nodes[0]["properties"]["path"] == "/api/v1/memories"

    # Relative project path passes through
    assert nodes[1]["path"] == "backend/src/routes.py"
    # properties.path (route) passes through
    assert nodes[1]["properties"]["path"] == "/health"

    # Non-route properties path fields are filesystem evidence and must sanitize.
    assert nodes[2]["properties"]["path"] == "<external>"
    assert nodes[2]["properties"]["source_path"] == "<external>"
    assert nodes[2]["properties"]["reanalysis_recommended_paths"] == [
        "backend/src/routes.py",
        "<external>",
    ]


# ---------------------------------------------------------------------------
#  PEP 420 implicit namespace packages
# ---------------------------------------------------------------------------


def test_namespace_package_root_deduction(tmp_path: Path) -> None:
    """Directories without __init__.py that contain sub-packages are
    treated as implicit namespace packages — the import root is their
    parent, not the namespace directory itself."""
    src = tmp_path / "src"
    ns = src / "ns_pkg"
    sub = ns / "sub"
    sub.mkdir(parents=True)
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "foo.py").write_text("x = 1\n", encoding="utf-8")
    # ns_pkg has NO __init__.py — it's a namespace package

    resolved = detect_source_roots(tmp_path)
    root_paths = {r.path for r in resolved.roots}
    # The import root should be "src", not "src/ns_pkg"
    assert "src" in root_paths

    from arcgraph.core.scanner import FileScanner

    scanner = FileScanner(tmp_path, source_roots=resolved.roots)
    records = scanner.scan()
    modules = {r.module for r in records}
    # Module names must include the namespace package prefix
    assert "ns_pkg.sub" in modules
    assert "ns_pkg.sub.foo" in modules


def test_namespace_package_with_py_files(tmp_path: Path) -> None:
    """A namespace package can also contain .py files directly."""
    ns = tmp_path / "ns_pkg"
    ns.mkdir()
    (ns / "standalone.py").write_text("y = 2\n", encoding="utf-8")
    sub = ns / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "bar.py").write_text("z = 3\n", encoding="utf-8")

    resolved = detect_source_roots(tmp_path)
    root_paths = {r.path for r in resolved.roots}
    assert "." in root_paths

    from arcgraph.core.scanner import FileScanner

    scanner = FileScanner(tmp_path, source_roots=resolved.roots)
    records = scanner.scan()
    modules = {r.module for r in records}
    assert "ns_pkg.standalone" in modules
    assert "ns_pkg.sub" in modules
    assert "ns_pkg.sub.bar" in modules


def test_src_dir_not_treated_as_namespace_package(tmp_path: Path) -> None:
    """Conventional source root names (src, lib) should NOT be treated as
    namespace packages — they should remain import roots."""
    src = tmp_path / "src"
    pkg = src / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "app.py").write_text("x = 1\n", encoding="utf-8")

    resolved = detect_source_roots(tmp_path)
    root_paths = {r.path for r in resolved.roots}
    # src should be the root, NOT "."
    assert "src" in root_paths
    assert "." not in root_paths

    from arcgraph.core.scanner import FileScanner

    scanner = FileScanner(tmp_path, source_roots=resolved.roots)
    records = scanner.scan()
    modules = {r.module for r in records}
    # Module should be mypkg.app, NOT src.mypkg.app
    assert "mypkg" in modules
    assert "mypkg.app" in modules
