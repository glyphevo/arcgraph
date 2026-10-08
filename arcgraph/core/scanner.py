"""Repository file scanning for ArcGraph."""

from __future__ import annotations

import fnmatch
import hashlib
import mmap
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcgraph.core.schemas import BuildWarning, FileRecord


@dataclass(frozen=True)
class SourceRoot:
    path: str
    module_prefix: str = ""


@dataclass(frozen=True)
class SourceRootDetection:
    """Records which strategy produced the detected source roots."""

    strategy: str
    # "cli_explicit" / "legacy_roots" / "pyproject_arcgraph"
    # / "pyproject_hatch" / "pyproject_setuptools" / "pyproject_name"
    # / "setup_cfg" / "package_heuristic" / "repo_root_fallback"
    roots: tuple[str, ...] = ()
    non_package_dirs: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedSourceRoots:
    """Unified result from workspace discovery: roots + detection metadata."""

    roots: tuple[SourceRoot, ...]
    detection: SourceRootDetection


# -- Legacy source roots (deprecated) ----------------------------------------

LEGACY_SOURCE_ROOTS: tuple[SourceRoot, ...] = (
    SourceRoot("src"),
    SourceRoot("tests", "tests"),
    SourceRoot("scripts", "scripts"),
    SourceRoot("arcgraph"),
)

# Deprecated alias – callers should use detect_source_roots(repo_root) instead.
DEFAULT_SOURCE_ROOTS = LEGACY_SOURCE_ROOTS

# Shared native scan scope: freshness must retain a supported extension even
# when exclude temporarily removes every file with that suffix. The frontend
# re-exports these constants for its existing callers.
TYPESCRIPT_FRONTEND_NAME = "typescript-static"
TYPESCRIPT_SOURCE_EXTENSIONS = (
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)
TYPESCRIPT_FILE_EXTENSIONS = (*TYPESCRIPT_SOURCE_EXTENSIONS, ".vue")

# ArcGraph-owned safe defaults for common generated/build/cache directories.
# This intentionally does not read .gitignore: project-specific exclusions still
# belong in [tool.arcgraph].exclude. Names such as "generated" or "build" can be
# hand-written source in unusual repos, but the default policy favors avoiding
# generated artifacts unless a future explicit include override is added.
SAFE_GENERATED_DIR_NAMES: tuple[str, ...] = (
    ".cache",
    ".gradle",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".pnpm-store",
    ".svelte-kit",
    ".turbo",
    ".vite",
    ".yarn",
    "build",
    "coverage",
    "dist",
    "generated",
    "generated-client",
    "htmlcov",
    "target",
    "__generated__",
)

DEFAULT_IGNORE_RULES: tuple[str, ...] = (
    "*.pyc",
    ".claude/worktrees/**",
    "**/.claude/worktrees/**",
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.venv/**",
    "**/venv/**",
    "output/**",
    "output/arcgraph/**",
    *(f"{dirname}/**" for dirname in SAFE_GENERATED_DIR_NAMES),
    *(f"**/{dirname}/**" for dirname in SAFE_GENERATED_DIR_NAMES),
)

IGNORED_DIR_NAMES = {
    ".bzr",
    ".git",
    ".hg",
    ".jj",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "output",
    "venv",
    *SAFE_GENERATED_DIR_NAMES,
}

# Tool-managed nested worktrees contain complete repository copies. Match the
# layout rather than the leaf name: a normal source directory named
# ``worktrees`` remains indexable, as do non-worktree files under ``.claude``.
IGNORED_REPO_PATH_SEQUENCES: tuple[tuple[str, ...], ...] = ((".claude", "worktrees"),)

# Git pointer files are tiny (normally one path line). Keep metadata discovery
# bounded so a malformed checkout marker cannot turn scanning into an
# unbounded read.
MAX_GIT_METADATA_FILE_BYTES = 4096


def _read_bounded_git_metadata(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_GIT_METADATA_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_GIT_METADATA_FILE_BYTES:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text or "\n" in text or "\r" in text:
        return None
    return text


def _resolved_git_metadata_path(raw_path: str, *, relative_to: Path) -> Path | None:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = relative_to / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def _gitdir_from_pointer(path: Path) -> Path | None:
    contents = _read_bounded_git_metadata(path)
    if contents is None or not contents.startswith("gitdir:"):
        return None
    raw_gitdir = contents.removeprefix("gitdir:").strip()
    if not raw_gitdir:
        return None
    return _resolved_git_metadata_path(raw_gitdir, relative_to=path.parent)


def _repository_common_git_dir(repo_root: Path) -> Path | None:
    marker = repo_root / ".git"
    try:
        if marker.is_dir():
            return marker.resolve()
        if not marker.is_file():
            return None
    except OSError:
        return None

    git_dir = _gitdir_from_pointer(marker)
    if git_dir is None:
        return None
    common_dir_value = _read_bounded_git_metadata(git_dir / "commondir")
    if common_dir_value is None:
        # This also supports running ArcGraph from a submodule whose .git file
        # points directly at its own common Git directory.
        return git_dir
    return _resolved_git_metadata_path(common_dir_value, relative_to=git_dir)


def _same_repository_worktree_roots(repo_root: Path) -> tuple[Path, ...]:
    """Return linked worktrees of *repo_root* that are nested inside it.

    Git records each linked worktree twice: the common repository has
    ``worktrees/<id>/gitdir`` pointing to the worktree's ``.git`` file, and
    that file points back to the same administration directory. Requiring both
    pointers distinguishes same-repository worktrees from submodules, nested
    clones, vendored repositories, and unrelated ``.git`` marker files.
    """

    try:
        resolved_repo_root = repo_root.resolve()
    except OSError:
        return ()
    # ArcGraph is often rooted at a subdirectory of the checkout (a monorepo
    # package, say). The worktree registry lives at the checkout root, so search
    # upwards for it; roots are still filtered to what is under *repo_root*.
    common_git_dir = None
    for candidate in (resolved_repo_root, *resolved_repo_root.parents):
        common_git_dir = _repository_common_git_dir(candidate)
        if common_git_dir is not None:
            break
    if common_git_dir is None:
        return ()
    registry = common_git_dir / "worktrees"
    try:
        admin_dirs = sorted(registry.iterdir())
    except OSError:
        return ()

    roots: set[Path] = set()
    for admin_dir in admin_dirs:
        try:
            if not admin_dir.is_dir():
                continue
            resolved_admin_dir = admin_dir.resolve()
        except OSError:
            continue
        git_marker_value = _read_bounded_git_metadata(admin_dir / "gitdir")
        if git_marker_value is None:
            continue
        git_marker = _resolved_git_metadata_path(
            git_marker_value,
            relative_to=admin_dir,
        )
        if git_marker is None or git_marker.name != ".git":
            continue
        linked_root = git_marker.parent
        relative = _relative_path_under(linked_root, resolved_repo_root)
        if relative is None or relative == Path("."):
            continue
        if not _paths_are_same(_gitdir_from_pointer(git_marker), resolved_admin_dir):
            continue
        # Re-express the worktree using the caller's spelling of the root so the
        # textual comparisons downstream hold on a case-insensitive filesystem.
        roots.add(resolved_repo_root / relative)
    return tuple(sorted(roots))


# ``tsc`` writes its emit beside the source when no ``outDir`` is configured.
# A runtime-JS file and a TypeScript file with the same stem resolve to the same
# module id, and the JS one sorts first — so indexing both drops the real source
# and points every symbol at generated code.
TYPESCRIPT_EMIT_SOURCE_SUFFIXES: dict[str, tuple[str, ...]] = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx",),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}

# ``tsc --declaration`` emits these beside the source too, and a declaration
# shares the source's module name *and* its node ids while sorting first, so the
# generated stub would win dedupe and evict the real implementation.
TYPESCRIPT_DECLARATION_EMIT_SOURCES: dict[str, tuple[str, ...]] = {
    ".d.ts": (".ts", ".tsx"),
    ".d.mts": (".mts",),
    ".d.cts": (".cts",),
}

# TypeScript's normal, explicit-ESM, and explicit-CommonJS source lanes can
# intentionally coexist at one logical path (for example ``index.ts`` beside a
# hand-written ``index.mjs``). They are different modules even though the
# Python-style module-name projection below would otherwise collapse them.
# ``.jsx`` and ``.tsx`` are each their own lane: neither is an emit of ``.ts``
# or ``.js``, so a coexisting pair has to be separated rather than dropped.
# ``standard`` stays first so ``.ts`` keeps the historical unsuffixed name.
TYPESCRIPT_MODULE_VARIANT_LANES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("standard", (".d.ts", ".ts")),
    ("tsx", (".tsx",)),
    ("js", (".js",)),
    ("jsx", (".jsx",)),
    ("esm", (".d.mts", ".mts", ".mjs")),
    ("commonjs", (".d.cts", ".cts", ".cjs")),
    ("vue", (".vue",)),
)

_TYPESCRIPT_MODULE_VARIANT_LANE_NAMES = frozenset(
    name for name, _suffixes in TYPESCRIPT_MODULE_VARIANT_LANES
)

# ``index.*`` files that name their parent directory, the TypeScript analogue
# of ``__init__.py``. Module naming and package classification must agree on
# this set or an index file gets a package-style module name while
# ``is_package`` disagrees (or vice versa).
_TYPESCRIPT_PACKAGE_INDEX_FILENAMES = frozenset(
    {
        "index.ts",
        "index.tsx",
        "index.mts",
        "index.cts",
        "index.js",
        "index.jsx",
        "index.mjs",
        "index.cjs",
    }
)


def logical_module_name(module: str) -> str:
    """Return the operator-facing module name without an identity-only lane."""

    base, separator, variant = module.rpartition(".__arcgraph_variant_")
    if not separator or not base or not variant.endswith("__"):
        return module
    lane_and_digest = variant.removesuffix("__")
    lane, digest_separator, digest = lane_and_digest.rpartition("_")
    if (
        not digest_separator
        or lane not in _TYPESCRIPT_MODULE_VARIANT_LANE_NAMES
        or len(digest) != 8
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return module
    return base


def _typescript_module_variant_lane(path: str) -> str | None:
    for lane, suffixes in TYPESCRIPT_MODULE_VARIANT_LANES:
        if path.endswith(suffixes):
            return lane
    return None


def is_typescript_emit_artifact(path: Path) -> bool:
    """True when *path* is shadowed by a TypeScript source with the same stem."""

    name = path.name
    for suffix, source_suffixes in TYPESCRIPT_DECLARATION_EMIT_SOURCES.items():
        if not name.endswith(suffix):
            continue
        stem = name.removesuffix(suffix)
        try:
            return any(
                (path.parent / f"{stem}{source}").exists() for source in source_suffixes
            )
        except OSError:
            return False

    source_suffixes = TYPESCRIPT_EMIT_SOURCE_SUFFIXES.get(path.suffix)
    if source_suffixes is None:
        return False
    try:
        if not any(path.with_suffix(suffix).exists() for suffix in source_suffixes):
            return False
    except OSError:
        return False
    # A same-stem TypeScript source is necessary but not sufficient: a
    # hand-written CommonJS shim or config (``index.js`` beside ``index.ts``,
    # ``jest.config.js`` beside ``jest.config.ts``) legitimately coexists with
    # one. Dropping it on the stem alone hides real source from the index and,
    # via change-safety's shared vocabulary, from the working-tree manifest, so
    # editing it would leave the identity unchanged. Require positive evidence
    # that a compiler produced this file.
    return _has_generated_marker(path)


def _has_generated_marker(path: Path) -> bool:
    """True when *path* carries compiler-emitted provenance.

    Only evidence about *this* file counts: a sourcemap sibling or an in-file
    ``sourceMappingURL`` directive. A sibling declaration (``foo.d.ts``) is
    deliberately not accepted -- it proves the ``.ts`` was compiled, not that
    the runtime file was emitted. Under ``emitDeclarationOnly`` (types from
    ``tsc``, JavaScript from a bundler or by hand) the declaration coexists
    with exactly the hand-written runtime files this classification exists to
    keep.
    """

    try:
        if path.with_name(f"{path.name}.map").exists():
            return True
    except OSError:
        return False
    return _contains_source_mapping_marker(path)


_SOURCE_MAPPING_CHUNK_BYTES = 64 * 1024
# Enough to hold a line break, indentation, and the directive itself so a
# directive split across a chunk boundary is still matched by the fallback.
_SOURCE_MAPPING_OVERLAP_BYTES = 512
# The annotation is a *directive*: a line-leading comment, in the `//#` form
# tooling emits today or the legacy `//@` form, optionally block-style. Bare
# `sourceMappingURL=` also occurs in ordinary source -- a string constant, a
# sourcemap parser, prose in a comment -- and matching that deletes exactly the
# hand-written files this classification exists to keep.
_SOURCE_MAPPING_DIRECTIVE_BODY = rb"[ \t]*(?://[#@]|/\*[#@])[ \t]*sourceMappingURL="
_SOURCE_MAPPING_DIRECTIVE = re.compile(rb"(?:\A|\n)" + _SOURCE_MAPPING_DIRECTIVE_BODY)
# Past the first chunk, the start of the buffer is mid-file rather than start
# of file, so only a real line break may anchor the directive.
_SOURCE_MAPPING_DIRECTIVE_MID = re.compile(rb"\n" + _SOURCE_MAPPING_DIRECTIVE_BODY)

# The skip warning always reports the complete count; only the path listing is
# truncated to keep one build warning readable for dist-sized emit trees.
_EMIT_ARTIFACT_WARNING_SAMPLE_LIMIT = 5

# One path-less warning summarises every skipped emit artifact of a scan. Each
# scan is a full rescan, so the current warning is authoritative: incremental
# merges must replace the previous copy by this kind rather than retain it.
TYPESCRIPT_EMIT_SKIP_WARNING_KIND = "typescript_emit_artifact_skipped"


def _contains_source_mapping_marker(path: Path) -> bool:
    """True when *path* carries a source-mapping directive anywhere in it.

    ``tsc --inlineSourceMap`` writes the directive at the start of one very
    long base64 line, so its distance from EOF is the size of the embedded map
    and has no useful bound. Any scan window large enough to look safe still
    misses a legitimately larger map, and capping the scan saves nothing: a
    file judged to be source is read in full by ``_record_for`` right after.
    """

    try:
        with path.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                return False
            try:
                with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                    return _SOURCE_MAPPING_DIRECTIVE.search(data) is not None
            except (OSError, ValueError):
                handle.seek(0)
                return _chunked_contains(handle)
    except OSError:
        return False


def _chunked_contains(handle: Any) -> bool:
    """Search a binary stream for the directive without loading it whole."""

    carry = b""
    first = True
    while True:
        chunk = handle.read(_SOURCE_MAPPING_CHUNK_BYTES)
        if not chunk:
            return False
        window = carry + chunk
        pattern = _SOURCE_MAPPING_DIRECTIVE if first else _SOURCE_MAPPING_DIRECTIVE_MID
        if pattern.search(window) is not None:
            return True
        first = False
        # Keep the tail so a directive split across a chunk boundary still
        # matches, including the line break that anchors it.
        carry = window[-_SOURCE_MAPPING_OVERLAP_BYTES:]


def is_ignored_repo_relative_path(path: Path) -> bool:
    parts = path.parts
    if any(part in IGNORED_DIR_NAMES for part in parts):
        return True
    return any(
        parts[index : index + len(sequence)] == sequence
        for sequence in IGNORED_REPO_PATH_SEQUENCES
        for index in range(len(parts) - len(sequence) + 1)
    )


# ---------------------------------------------------------------------------
#  CLI explicit root inference (unchanged API, kept for --root flag)
# ---------------------------------------------------------------------------


def infer_source_roots(paths: list[str]) -> list[SourceRoot]:
    roots: list[SourceRoot] = []
    for raw_path in paths:
        normalized = raw_path.replace("\\", "/").rstrip("/")
        name = Path(normalized).name
        if normalized.endswith("backend/tests"):
            roots.append(SourceRoot(raw_path, "tests"))
        elif name == "scripts":
            roots.append(SourceRoot(raw_path, "scripts"))
        else:
            roots.append(SourceRoot(raw_path))
    return roots


# ---------------------------------------------------------------------------
#  Workspace discovery: detect_source_roots()
# ---------------------------------------------------------------------------


def detect_source_roots(repo_root: Path) -> ResolvedSourceRoots:
    """Auto-detect source roots using a priority strategy chain.

    Strategy chain (high → low):
      3. pyproject.toml [tool.ArcGraph] source_roots (explicit config)
      4. pyproject.toml packaging metadata (Hatchling → setuptools → name)
      5. setup.cfg [options.packages.find] where
      6. Package/import-root heuristic + non-package Python dirs
      7. Repo root fallback
    Strategies 1-2 (--root, --legacy-roots) are handled by CLI before this.
    """
    repo_root = repo_root.resolve()
    exclude = read_arcgraph_exclude(repo_root)
    linked_worktrees = _same_repository_worktree_roots(repo_root)
    for detector, fallback_strategy in _STRATEGY_CHAIN:
        result = detector(repo_root)
        if result is not None:
            roots, extra = result
            # extra is either a strategy name (str) or non_package_dirs (tuple)
            if isinstance(extra, tuple):
                strategy_name = fallback_strategy
                non_pkg = extra
            else:
                strategy_name = extra  # sub-strategy like "pyproject_ArcGraph"
                non_pkg = ()
            deduped = [
                root
                for root in _with_typescript_roots(repo_root, roots)
                if not _resolved_path_is_under_any(
                    repo_root / root.path,
                    linked_worktrees,
                )
            ]
            if not deduped:
                if strategy_name == "pyproject_arcgraph":
                    raise RuntimeError(
                        "Explicit [tool.arcgraph] source_roots resolve only to "
                        "linked worktrees of this repository. Choose source roots "
                        "inside the primary checkout instead of falling back to "
                        "automatic workspace discovery."
                    )
                continue
            return ResolvedSourceRoots(
                roots=tuple(deduped),
                detection=SourceRootDetection(
                    strategy=strategy_name,
                    roots=tuple(r.path for r in deduped),
                    non_package_dirs=non_pkg,
                    exclude=exclude,
                ),
            )
    # Should not reach here — _fallback_repo_root always succeeds.
    return _fallback_repo_root_resolved(repo_root)


def _with_typescript_roots(
    repo_root: Path,
    roots: list[SourceRoot],
) -> list[SourceRoot]:
    """Add conventional frontend TypeScript roots to Python discovery results.

    Python package discovery is intentionally conservative and stops once it
    finds import roots. In mixed Python + TypeScript monorepos that would hide
    the frontend from a multi-language build, so we supplement the detected
    roots with conventional TS roots when a package manifest is present.
    """
    if not (repo_root / "frontend" / "package.json").exists():
        return roots

    candidates = [
        SourceRoot("frontend/src"),
        SourceRoot("frontend/tests", "frontend_tests"),
    ]
    existing = {root.path for root in roots}
    extra = [
        root
        for root in candidates
        if root.path not in existing and (repo_root / root.path).is_dir()
    ]
    return [*roots, *extra]


# -- Strategy 3: pyproject.toml [tool.ArcGraph] ----------------------------


def _detect_from_pyproject(
    repo_root: Path,
) -> tuple[list[SourceRoot], str] | None:
    """Try pyproject.toml: ArcGraph config → hatch → setuptools → name."""
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return None

    try:
        import tomllib
    except ModuleNotFoundError:
        # Python 3.10 fallback
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return None

    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    # 3a. [tool.ArcGraph] source_roots — explicit override
    result = _try_arcgraph_config(repo_root, data)
    if result is not None:
        return result

    # 4a. Hatchling [tool.hatch.build.targets.wheel]
    result = _try_hatch_config(repo_root, data)
    if result is not None:
        return result

    # 4b. setuptools [tool.setuptools.packages.find]
    result = _try_setuptools_config(repo_root, data)
    if result is not None:
        return result

    # 4c. [project].name heuristic
    result = _try_project_name_heuristic(repo_root, data)
    if result is not None:
        return result

    return None


def _try_arcgraph_config(
    repo_root: Path,
    data: dict[str, Any],
) -> tuple[list[SourceRoot], str] | None:
    """[tool.arcgraph] source_roots = ["backend/src", ...]"""
    arcgraph_cfg = data.get("tool", {}).get("arcgraph", {})
    raw_roots = arcgraph_cfg.get("source_roots")
    if not raw_roots or not isinstance(raw_roots, list):
        return None

    roots: list[SourceRoot] = []
    for entry in raw_roots:
        if isinstance(entry, str):
            roots.append(SourceRoot(entry))
        elif isinstance(entry, dict):
            path = entry.get("path", "")
            prefix = entry.get("module_prefix", "")
            roots.append(SourceRoot(path, prefix))
    if roots:
        return roots, "pyproject_arcgraph"
    return None


def read_arcgraph_exclude(repo_root: Path) -> tuple[str, ...]:
    """Read ``[tool.arcgraph] exclude`` patterns from pyproject.toml.

    Returns an empty tuple when the key is absent or the file is missing.
    Patterns use the same fnmatch syntax as FileScanner ignore_rules,
    e.g. ``["**/generated/**", "legacy_app/**"]``.
    """
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return ()
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return ()
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except Exception:
        return ()
    raw = data.get("tool", {}).get("arcgraph", {}).get("exclude")
    if not raw or not isinstance(raw, list):
        return ()
    return tuple(str(e) for e in raw if isinstance(e, str))


def _try_hatch_config(
    repo_root: Path,
    data: dict[str, Any],
) -> tuple[list[SourceRoot], str] | None:
    """[tool.hatch.build.targets.wheel] packages / sources."""
    hatch = data.get("tool", {}).get("hatch", {})
    wheel = hatch.get("build", {}).get("targets", {}).get("wheel", {})
    packages = wheel.get("packages")
    if not packages or not isinstance(packages, list):
        return None

    # sources table rewrites prefixes, e.g. {"src": ""} means strip "src/"
    sources = wheel.get("sources", {})

    # Collect import roots by stripping source prefixes from package paths
    import_roots: set[str] = set()
    for pkg_path in packages:
        if not isinstance(pkg_path, str):
            continue
        pkg_path = pkg_path.replace("\\", "/").rstrip("/")

        # Check if any source rewrite applies
        resolved = False
        for src_prefix, _ in sorted(sources.items(), key=lambda x: -len(x[0])):
            src_prefix = src_prefix.replace("\\", "/").rstrip("/")
            if pkg_path.startswith(src_prefix + "/"):
                # The import root is the source prefix directory
                import_roots.add(src_prefix)
                resolved = True
                break

        if not resolved:
            # No source rewrite — the package itself is at repo root
            # e.g. packages=["arcgraph"] means SourceRoot(".", "")
            parent = str(Path(pkg_path).parent)
            if parent == ".":
                import_roots.add(".")
            else:
                import_roots.add(parent)

    if import_roots:
        roots = [SourceRoot(r, "") for r in sorted(import_roots)]
        return roots, "pyproject_hatch"
    return None


def _try_setuptools_config(
    repo_root: Path,
    data: dict[str, Any],
) -> tuple[list[SourceRoot], str] | None:
    """[tool.setuptools.packages.find] where = [...]"""
    setuptools = data.get("tool", {}).get("setuptools", {})
    find = setuptools.get("packages", {}).get("find", {})
    where = find.get("where")
    if not where or not isinstance(where, list):
        return None

    roots: list[SourceRoot] = []
    for path in where:
        if isinstance(path, str) and (repo_root / path).is_dir():
            roots.append(SourceRoot(path, ""))
    if roots:
        return roots, "pyproject_setuptools"
    return None


def _try_project_name_heuristic(
    repo_root: Path,
    data: dict[str, Any],
) -> tuple[list[SourceRoot], str] | None:
    """[project].name → normalize → check src/<normalized>."""
    project_name = data.get("project", {}).get("name")
    if not project_name or not isinstance(project_name, str):
        return None

    # PEP 503: normalize dashes to underscores
    normalized = project_name.replace("-", "_")
    candidate = repo_root / "src" / normalized
    if candidate.is_dir():
        return [SourceRoot("src", "")], "pyproject_name"
    return None


# -- Strategy 5: setup.cfg --------------------------------------------------


def _detect_from_setup_cfg(
    repo_root: Path,
) -> tuple[list[SourceRoot], str] | None:
    """[options.packages.find] where = ..."""
    cfg_path = repo_root / "setup.cfg"
    if not cfg_path.exists():
        return None

    import configparser

    parser = configparser.ConfigParser()
    try:
        parser.read(str(cfg_path), encoding="utf-8")
    except Exception:
        return None

    where = parser.get("options.packages.find", "where", fallback=None)
    if not where:
        return None

    roots: list[SourceRoot] = []
    for part in where.split(","):
        part = part.strip()
        if part and (repo_root / part).is_dir():
            roots.append(SourceRoot(part, ""))
    if roots:
        return roots, "setup_cfg"
    return None


# -- Strategy 6: Package heuristic + non-package dirs -----------------------


def _detect_from_packages_and_scripts(
    repo_root: Path,
) -> tuple[list[SourceRoot], tuple[str, ...]] | None:
    """Auto-detect via __init__.py (import root) and non-package .py dirs.

    Step A: Find dirs with __init__.py (depth 1-3), deduce import root.
    Step B: Find dirs with .py but no __init__.py (depth 1-2), set module_prefix.
    Conflict rule: same path from both steps → step A wins.
    """
    step_a_roots = _step_a_package_roots(repo_root)
    step_b_roots, _ = _step_b_non_package_roots(repo_root)

    # Dedup step B within itself: "scripts" covers "scripts/tests".
    # Step A roots are NOT deduped — different package trees can share
    # a parent directory (e.g. backend has evals/ and src/api/).
    resolved_b = _dedup_nested_roots(step_b_roots)

    # Conflict resolution: same path in both → step A (package) wins
    a_paths = {r.path for r in step_a_roots}
    resolved_b = [r for r in resolved_b if r.path not in a_paths]

    # Invariant: non-package roots must never swallow package roots.
    # Remove any step B root that is a *parent* of a step A root.
    def _is_parent_of_any_a_root(non_pkg: SourceRoot) -> bool:
        prefix = non_pkg.path.rstrip("/") + "/"
        return any(a_path.startswith(prefix) for a_path in a_paths)

    resolved_b = [r for r in resolved_b if not _is_parent_of_any_a_root(r)]

    combined = step_a_roots + resolved_b
    if not combined:
        return None

    # Invariant: non_package_dirs must only record final resolved non-package roots
    resolved_non_pkg_dirs = tuple(r.path for r in resolved_b)
    return combined, resolved_non_pkg_dirs


def _step_a_package_roots(repo_root: Path) -> list[SourceRoot]:
    """Find dirs with __init__.py, deduce import root by walking up."""
    import_roots: dict[str, SourceRoot] = {}

    linked_worktrees = _same_repository_worktree_roots(repo_root)
    for depth in range(1, 4):  # 1-3 layers
        for init_path in _iter_init_files_at_depth(
            repo_root, depth, linked_worktrees=linked_worktrees
        ):
            pkg_dir = init_path.parent
            root = _deduce_import_root(repo_root, pkg_dir)
            if root is not None:
                root_key = root.path
                if root_key not in import_roots:
                    import_roots[root_key] = root

    return list(import_roots.values())


def _iter_init_files_at_depth(
    repo_root: Path,
    depth: int,
    *,
    linked_worktrees: tuple[Path, ...] | None = None,
) -> list[Path]:
    """Find __init__.py files at exactly `depth` levels below repo_root."""
    # Use glob pattern with appropriate depth
    pattern = "/".join(["*"] * depth) + "/__init__.py"
    results = []
    if linked_worktrees is None:
        linked_worktrees = _same_repository_worktree_roots(repo_root)
    for p in repo_root.glob(pattern):
        rel = p.relative_to(repo_root)
        if is_ignored_repo_relative_path(rel):
            continue
        if any(_path_is_under(p, root) for root in linked_worktrees):
            continue
        results.append(p)
    return sorted(results)


def _deduce_import_root(repo_root: Path, pkg_dir: Path) -> SourceRoot | None:
    """Walk up from a package directory to find the import root.

    e.g. src/pkg/__init__.py → import root is "src"
         pkg/__init__.py     → import root is "."
         src/ns/sub/__init__.py → import root is "src" (ns is namespace pkg)
    """
    current = pkg_dir
    while True:
        parent = current.parent
        if parent == repo_root or parent == current:
            # Reached repo root — import root is the parent of the package
            rel = current.relative_to(repo_root).as_posix()
            parent_rel = (
                parent.relative_to(repo_root).as_posix() if parent != repo_root else "."
            )
            if parent == repo_root:
                return SourceRoot(".", "")
            return SourceRoot(parent_rel, "")

        if parent != repo_root and (parent / "pyproject.toml").exists():
            return SourceRoot(parent.relative_to(repo_root).as_posix(), "")

        # If parent still has __init__.py, it's also a package — walk up
        if (parent / "__init__.py").exists():
            current = parent
        elif _looks_like_namespace_package(parent):
            # PEP 420 implicit namespace package — keep walking up
            current = parent
        else:
            # Parent has no __init__.py — it's the import root
            rel = parent.relative_to(repo_root).as_posix()
            return SourceRoot(rel, "")


# Conventional source root directory names that should NOT be treated as
# namespace packages — they are import roots, not importable packages.
_SOURCE_ROOT_NAMES = frozenset(
    {
        "src",
        "lib",
        "source",
        "sources",
        "vendor",
        "third_party",
        "packages",
        "apps",
        "modules",
        "backend",
        "frontend",
    }
)


def _looks_like_namespace_package(directory: Path) -> bool:
    """Return True if *directory* looks like a PEP 420 implicit namespace package.

    A directory qualifies when it has a valid Python-identifier name (not a
    conventional source root name like ``src``) and contains at least one
    sub-package (directory with ``__init__.py``) or ``.py`` file.
    """
    name = directory.name
    if not name.isidentifier() or name in _SOURCE_ROOT_NAMES:
        return False
    # Quick check: does it contain any sub-package or .py file?
    for child in directory.iterdir():
        if child.is_dir() and (child / "__init__.py").exists():
            return True
        if child.is_file() and child.suffix == ".py":
            return True
    return False


def _step_b_non_package_roots(
    repo_root: Path,
) -> tuple[list[SourceRoot], tuple[str, ...]]:
    """Find dirs with .py files but no __init__.py (depth 1-2)."""
    non_pkg_dirs: list[str] = []
    roots: list[SourceRoot] = []
    seen_paths: set[str] = set()

    linked_worktrees = _same_repository_worktree_roots(repo_root)
    for depth in range(1, 3):  # 1-2 layers
        for d in _iter_python_dirs_at_depth(
            repo_root, depth, has_init=False, linked_worktrees=linked_worktrees
        ):
            rel = d.relative_to(repo_root).as_posix()
            if rel in seen_paths:
                continue
            seen_paths.add(rel)
            dir_name = d.name
            non_pkg_dirs.append(rel)
            roots.append(SourceRoot(rel, dir_name))

    return roots, tuple(non_pkg_dirs)


def _iter_python_dirs_at_depth(
    repo_root: Path,
    depth: int,
    *,
    has_init: bool,
    linked_worktrees: tuple[Path, ...] | None = None,
) -> list[Path]:
    """Find directories at `depth` that contain .py files.

    If has_init is False, only returns dirs without __init__.py.
    """
    # Build pattern for directories at the given depth
    pattern = "/".join(["*"] * depth)
    # Conventional non-package directory names that may have arbitrarily deep
    # .py files (tests/unit/api/test_x.py). These get recursive discovery.
    _CONVENTIONAL_NONPKG_NAMES = frozenset(
        {
            "tests",
            "test",
            "scripts",
            "benchmarks",
            "examples",
        }
    )
    results = []
    if linked_worktrees is None:
        linked_worktrees = _same_repository_worktree_roots(repo_root)
    for d in repo_root.glob(pattern):
        if not d.is_dir():
            continue
        rel = d.relative_to(repo_root)
        if is_ignored_repo_relative_path(rel):
            continue
        if any(_path_is_under(d, root) for root in linked_worktrees):
            continue
        has_init_py = (d / "__init__.py").exists()
        if has_init and not has_init_py:
            continue
        if not has_init and has_init_py:
            continue
        # Conventional dirs (tests/, scripts/) use recursive discovery
        # so arbitrarily deep .py files count.
        # Other dirs use bounded depth 2 to prevent namespace dirs
        # (e.g. "backend/") from matching via deep subdirectories.
        patterns = (
            ("**/*.py",) if d.name in _CONVENTIONAL_NONPKG_NAMES else ("*.py", "*/*.py")
        )
        has_py = any(
            not is_ignored_repo_relative_path(candidate.relative_to(repo_root))
            and not any(_path_is_under(candidate, root) for root in linked_worktrees)
            for pattern in patterns
            for candidate in d.glob(pattern)
        )
        if has_py:
            results.append(d)
    return sorted(results)


# -- Strategy 7: Repo root fallback -----------------------------------------


def _fallback_repo_root(
    repo_root: Path,
) -> tuple[list[SourceRoot], str] | None:
    """Last resort: use repo root itself as source root."""
    # Only if there are actually .py files at repo root
    if any(repo_root.glob("*.py")):
        return [SourceRoot(".", "")], "repo_root_fallback"
    # Even if no .py at root, we return it as the fallback
    return [SourceRoot(".", "")], "repo_root_fallback"


def _fallback_repo_root_resolved(repo_root: Path) -> ResolvedSourceRoots:
    """Construct a ResolvedSourceRoots for the repo root fallback."""
    return ResolvedSourceRoots(
        roots=(SourceRoot(".", ""),),
        detection=SourceRootDetection(
            strategy="repo_root_fallback",
            roots=(".",),
        ),
    )


def require_decodable_path(path: Path) -> None:
    """Fail clearly when a path name could not be decoded.

    Where Python's file-system encoding is not UTF-8 (on Linux, only when
    both locale coercion and UTF-8 mode are turned off under a C or POSIX
    locale), a non-ASCII name decodes to lone surrogates that cannot be
    hashed or written as UTF-8 later in the build.
    """

    text = str(path)
    if not any("\udc80" <= char <= "\udcff" for char in text):
        return
    shown = text.encode("utf-8", "backslashreplace").decode("utf-8")
    raise RuntimeError(
        f"Cannot index {shown}: the name is not valid in the file-system "
        f"encoding ({sys.getfilesystemencoding()}). Run ArcGraph under a "
        "UTF-8 locale or with PYTHONUTF8=1."
    )


# -- Parent-child dedup ------------------------------------------------------


def _path_is_under(path: Path, root: Path) -> bool:
    """Return True if *path* is under *root* (resolved paths)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _paths_are_same(left: Path | None, right: Path | None) -> bool:
    """Compare two paths by filesystem identity, tolerating case spellings."""

    if left is None or right is None:
        return False
    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def _relative_path_under(path: Path, root: Path) -> Path | None:
    """Return *path* relative to *root*, or None when it is not under it.

    ``Path.resolve()`` follows symlinks but does not canonicalise case, so on a
    case-insensitive filesystem two spellings of one directory compare unequal
    as text. Fall back to filesystem identity, which is authoritative.
    """

    try:
        return path.relative_to(root)
    except ValueError:
        pass
    names: list[str] = []
    current = path
    while True:
        try:
            if current.samefile(root):
                return Path(*reversed(names)) if names else Path(".")
        except OSError:
            pass
        if current.parent == current:
            return None
        names.append(current.name)
        current = current.parent


def _resolved_path_is_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return any(_path_is_under(resolved, root) for root in roots)


def _dedup_nested_roots(roots: list[SourceRoot]) -> list[SourceRoot]:
    """Remove child roots if a parent root already covers them.

    e.g. ["scripts", "scripts/tests"] → keep only "scripts".
    Only applies to auto-detected roots; --root explicit allows nesting.
    """
    if len(roots) <= 1:
        return roots

    # Normalize paths and sort by length (shortest first = parents first)
    sorted_roots = sorted(roots, key=lambda r: r.path)
    kept: list[SourceRoot] = []

    for candidate in sorted_roots:
        candidate_path = candidate.path.rstrip("/")
        is_nested = False
        for parent in kept:
            parent_path = parent.path.rstrip("/")
            # "." covers everything
            if parent_path == ".":
                is_nested = True
                break
            if parent_path == "packages" and candidate_path.startswith("packages/"):
                continue
            # Check if candidate is under parent
            if candidate_path.startswith(parent_path + "/"):
                is_nested = True
                break
        if not is_nested:
            kept.append(candidate)

    return kept


# -- Strategy chain ----------------------------------------------------------

_STRATEGY_CHAIN: list[
    tuple[
        # detector function
        Any,  # Callable[[Path], tuple[list[SourceRoot], str | tuple[str, ...]] | None]
        str,  # strategy name (for detection when _detect_from_pyproject bundles)
    ]
] = []


def _pyproject_wrapper(
    repo_root: Path,
) -> tuple[list[SourceRoot], str] | None:
    return _detect_from_pyproject(repo_root)


def _setup_cfg_wrapper(
    repo_root: Path,
) -> tuple[list[SourceRoot], str] | None:
    return _detect_from_setup_cfg(repo_root)


# The strategy chain is assembled here. The strategy name in the tuple is only
# used as a fallback; the detector may return its own strategy name.
_STRATEGY_CHAIN = [
    (_pyproject_wrapper, "pyproject"),
    (_setup_cfg_wrapper, "setup_cfg"),
    (_detect_from_packages_and_scripts, "package_heuristic"),
    (_fallback_repo_root, "repo_root_fallback"),
]


# ---------------------------------------------------------------------------
#  FileScanner (unchanged API)
# ---------------------------------------------------------------------------


class FileScanner:
    def __init__(
        self,
        repo_root: Path,
        source_roots: list[SourceRoot] | tuple[SourceRoot, ...] | None = None,
        ignore_rules: list[str] | tuple[str, ...] | None = None,
        file_extensions: tuple[str, ...] = (".py",),
    ) -> None:
        self.repo_root = repo_root.resolve()
        require_decodable_path(self.repo_root)
        self.source_roots = (
            tuple(source_roots)
            if source_roots
            else detect_source_roots(self.repo_root).roots
        )
        self.ignore_rules = tuple(ignore_rules or DEFAULT_IGNORE_RULES)
        self.exclude_rules: tuple[str, ...] = ()
        self.file_extensions = file_extensions or (".py",)
        self.warnings: list[BuildWarning] = []

    def scan(self) -> list[FileRecord]:
        # Every consumer (build, sync, freshness and precision) uses the same
        # current project exclusions, including when this scanner is reused.
        self.exclude_rules = read_arcgraph_exclude(self.repo_root)
        records: list[FileRecord] = []
        skipped_emit_artifacts: list[str] = []
        self.warnings = []
        linked_worktrees = _same_repository_worktree_roots(self.repo_root)

        # Pre-compute resolved paths for all roots (sorted longest-first
        # so we can do most-specific matching).
        root_abs: list[tuple[SourceRoot, Path]] = []
        for source_root in self.source_roots:
            rp = (self.repo_root / source_root.path).resolve()
            if any(_path_is_under(rp, root) for root in linked_worktrees):
                # Auto-detected roots never land here (detection already
                # excludes linked worktrees), so this root was configured
                # explicitly and skipping it silently would publish an empty
                # index with no diagnostic.
                self.warnings.append(
                    BuildWarning(
                        kind="source_root_in_linked_worktree",
                        message=(
                            "Source root lies inside a linked Git worktree "
                            f"and was not scanned: {source_root.path}"
                        ),
                        path=source_root.path,
                    )
                )
                continue
            if not rp.exists():
                self.warnings.append(
                    BuildWarning(
                        kind="missing_source_root",
                        message=f"Source root does not exist: {source_root.path}",
                        path=source_root.path,
                    )
                )
                continue
            root_abs.append((source_root, rp))

        # For each root, collect absolute paths of all more-specific child roots.
        # When scanning root R, skip files that fall under any child root.
        child_roots_for: dict[str, list[Path]] = {}
        for source_root, abs_path in root_abs:
            children: list[Path] = []
            for other_root, other_abs in root_abs:
                if other_root.path == source_root.path:
                    continue
                try:
                    other_abs.relative_to(abs_path)
                    children.append(other_abs)
                except ValueError:
                    pass
            child_roots_for[source_root.path] = children

        for source_root, root_path in root_abs:
            children = child_roots_for[source_root.path]
            extensions = set(self.file_extensions)
            for dirpath_raw, dirnames, filenames in os.walk(root_path):
                dirpath = Path(dirpath_raw)
                pruned_dirnames: list[str] = []
                for dirname in sorted(dirnames):
                    child_path = dirpath / dirname
                    if self._is_ignored(child_path):
                        continue
                    if any(
                        _path_is_under(child_path, root) for root in linked_worktrees
                    ):
                        continue
                    # Skip whole subtrees covered by a more specific child root.
                    if children and any(
                        _path_is_under(child_path, child) for child in children
                    ):
                        continue
                    pruned_dirnames.append(dirname)
                dirnames[:] = pruned_dirnames

                for filename in sorted(filenames):
                    path = dirpath / filename
                    if path.suffix not in extensions:
                        continue
                    if self._is_ignored(path):
                        continue
                    if is_typescript_emit_artifact(path):
                        # Declaration emits (.d.ts beside .ts) are the
                        # long-standing, intended drop -- their content lives
                        # in the source file. Only the narrowed runtime-file
                        # exclusion is disclosed.
                        if not path.name.endswith(
                            tuple(TYPESCRIPT_DECLARATION_EMIT_SOURCES)
                        ):
                            skipped_emit_artifacts.append(
                                self._repo_relative_display(path)
                            )
                        continue
                    records.append(self._record_for(path, root_path, source_root))

        if skipped_emit_artifacts:
            skipped_emit_artifacts.sort()
            sample = ", ".join(
                skipped_emit_artifacts[:_EMIT_ARTIFACT_WARNING_SAMPLE_LIMIT]
            )
            overflow = len(skipped_emit_artifacts) - _EMIT_ARTIFACT_WARNING_SAMPLE_LIMIT
            if overflow > 0:
                sample = f"{sample} (and {overflow} more)"
            self.warnings.append(
                BuildWarning(
                    kind=TYPESCRIPT_EMIT_SKIP_WARNING_KIND,
                    message=(
                        f"Skipped {len(skipped_emit_artifacts)} runtime file(s) "
                        "classified as TypeScript emit artifacts (same-stem "
                        f"TypeScript source plus compiler provenance): {sample}"
                    ),
                )
            )

        return self._disambiguate_typescript_module_variants(records)

    @staticmethod
    def _disambiguate_typescript_module_variants(
        records: list[FileRecord],
    ) -> list[FileRecord]:
        """Keep coexisting TS runtime lanes from sharing graph identities.

        Declaration/runtime companions within one lane intentionally retain one
        module identity. Only independently resolvable lanes are separated, and
        the standard lane keeps the historical module name when present.
        """

        by_module: dict[tuple[str, str], list[FileRecord]] = {}
        for record in records:
            if _typescript_module_variant_lane(record.path) is None:
                continue
            by_module.setdefault((record.source_root, record.module), []).append(record)

        replacements: dict[str, str] = {}
        lane_priority = {
            lane: index
            for index, (lane, _suffixes) in enumerate(TYPESCRIPT_MODULE_VARIANT_LANES)
        }
        for (_source_root, module), candidates in by_module.items():
            by_lane: dict[str, list[FileRecord]] = {}
            for candidate in candidates:
                lane = _typescript_module_variant_lane(candidate.path)
                if lane is not None:
                    by_lane.setdefault(lane, []).append(candidate)
            if len(by_lane) < 2:
                continue
            canonical_lane = min(by_lane, key=lane_priority.__getitem__)
            for lane, lane_records in by_lane.items():
                if lane == canonical_lane:
                    continue
                # A declaration/runtime companion may be added to this lane at
                # any time. Its membership must not participate in the graph
                # identity or unchanged nodes would churn on the next reindex.
                lane_seed = f"{module}|{lane}"
                discriminator = hashlib.sha256(lane_seed.encode("utf-8")).hexdigest()[
                    :8
                ]
                variant_module = f"{module}.__arcgraph_variant_{lane}_{discriminator}__"
                for record in lane_records:
                    replacements[record.path] = variant_module

        if not replacements:
            return records
        return [
            (
                record.model_copy(update={"module": replacements[record.path]})
                if record.path in replacements
                else record
            )
            for record in records
        ]

    def _is_ignored(self, path: Path) -> bool:
        rel = path.relative_to(self.repo_root)
        if is_ignored_repo_relative_path(rel):
            return True

        rel_path = rel.as_posix()
        for rule in (*self.ignore_rules, *self.exclude_rules):
            if fnmatch.fnmatch(rel_path, rule):
                return True
            if rule.endswith("/**"):
                base = rule[:-3]
                if rel_path == base or rel_path.startswith(f"{base}/"):
                    return True
        return False

    def _repo_relative_display(self, path: Path) -> str:
        try:
            return path.relative_to(self.repo_root).as_posix()
        except ValueError:
            return path.as_posix()

    def _record_for(
        self, path: Path, root_path: Path, source_root: SourceRoot
    ) -> FileRecord:
        require_decodable_path(path)
        content = path.read_bytes()
        text = content.decode("utf-8", errors="replace")
        module = self._module_name(path, root_path, source_root)

        return FileRecord(
            path=path.relative_to(self.repo_root).as_posix(),
            abs_path=str(path),
            source_root=root_path.relative_to(self.repo_root).as_posix(),
            module=module,
            file_hash=hashlib.sha256(content).hexdigest(),
            line_count=len(text.splitlines()),
            file_size=len(content),
            is_package=self._is_package_file(path, root_path),
        )

    @staticmethod
    def _module_name(path: Path, root_path: Path, source_root: SourceRoot) -> str:
        rel_path = path.relative_to(root_path)
        if rel_path.name == "__init__.py" or (
            rel_path.name in _TYPESCRIPT_PACKAGE_INDEX_FILENAMES
            and rel_path.parent != Path(".")
        ):
            parts = rel_path.parent.parts
        elif rel_path.name.endswith((".d.ts", ".d.mts", ".d.cts")):
            declaration_suffix = next(
                suffix
                for suffix in (".d.ts", ".d.mts", ".d.cts")
                if rel_path.name.endswith(suffix)
            )
            parts = (
                *rel_path.parent.parts,
                rel_path.name.removesuffix(declaration_suffix),
            )
        else:
            parts = rel_path.with_suffix("").parts

        module_parts = [part for part in (source_root.module_prefix, *parts) if part]
        return ".".join(module_parts)

    @staticmethod
    def _is_package_file(path: Path, root_path: Path) -> bool:
        rel_path = path.relative_to(root_path)
        return rel_path.name == "__init__.py" or (
            rel_path.name in _TYPESCRIPT_PACKAGE_INDEX_FILENAMES
            and rel_path.parent != Path(".")
        )
