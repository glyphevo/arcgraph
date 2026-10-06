"""Git/read-only and graph-build identities used by Change Safety.

This module invokes only fixed, read-only Git subcommands.  It never accepts a
command string from a request and never writes Git state.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from arcgraph.change.contracts import (
    BuildIdentity,
    CodeIdentity,
    WorkingTreeIdentity,
    build_identity_digest,
    canonical_digest,
    stable_projection,
)
from arcgraph.change.errors import (
    BaselineIntegrityMismatch,
    BaselineSourceUnavailable,
    CurrentBuildCodeIdentityMismatch,
    CurrentCodeIdentityMismatch,
    RepositoryPathError,
    StableIdentityCollision,
)
from arcgraph.change.paths import normalize_repository_path
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import (
    is_ignored_repo_relative_path,
    is_typescript_emit_artifact,
)
from arcgraph.core.schemas import (
    Edge,
    FileRecord,
    Node,
    SCHEMA_VERSION,
    SemanticDiagnostic,
)

_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".vue",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".sql",
        ".graphql",
        ".gql",
        ".proto",
    }
)
NODE_IDENTITY_PROFILE_VERSION = "1.4"
DIAGNOSTIC_IDENTITY_PROFILE_VERSION = "1.1"
_NODE_ID_PREFIXES_BY_KIND: dict[str, frozenset[str]] = {
    "class": frozenset({"class"}),
    "cli_command": frozenset({"cli"}),
    "cli_option": frozenset({"cli"}),
    "component": frozenset({"component"}),
    "config": frozenset({"config"}),
    "coverage_run": frozenset({"coverage"}),
    "diagnostic": frozenset(),
    "enum": frozenset({"enum"}),
    "external_package": frozenset({"ext"}),
    "external_symbol": frozenset({"extsym"}),
    "function": frozenset({"fn", "function"}),
    "interface": frozenset({"interface"}),
    "log_sink": frozenset({"log"}),
    "mcp_tool": frozenset({"mcp_tool"}),
    "method": frozenset({"method"}),
    "model_field": frozenset({"field"}),
    "module": frozenset({"mod"}),
    "package": frozenset({"package"}),
    "pydantic_model": frozenset({"schema"}),
    "protocol_symbol": frozenset({"protocol"}),
    "pytest_fixture": frozenset({"fixture"}),
    "queue": frozenset({"queue"}),
    "route": frozenset({"route"}),
    "source_root": frozenset({"source_root"}),
    "table": frozenset({"table"}),
    "test_case": frozenset({"test"}),
    "type_alias": frozenset({"type_alias"}),
    "vue_emit": frozenset({"vue_emit"}),
    "vue_prop": frozenset({"vue_prop"}),
    "worker_task": frozenset({"worker"}),
}
_POSITION_DERIVED_NODE_ID_PREFIXES = frozenset({"decl", "local", "unresolved"})
_PROFILED_NODE_FRONTENDS = frozenset({"typescript-static"})
_SEMANTIC_NODE_FRONTENDS: dict[str, str] = {
    "c-semantic-external": "c",
    "cpp-semantic-external": "cpp",
    "csharp-semantic-external": "csharp",
    "go-semantic-external": "go",
    "java-semantic-external": "java",
    "openapi-protocol": "openapi",
    "rust-semantic-external": "rust",
    "scip-protocol": "scip",
    "swift-semantic-external": "swift",
}
_DIAGNOSTIC_LIFECYCLE_PROPERTY_KEYS = frozenset(
    {
        "diagnostic_lifecycle_status",
        "first_seen_index",
        "last_seen_index",
        "seen_count",
        "index_version",
        "lifecycle_status",
    }
)
_DIAGNOSTIC_LOCATION_PROPERTY_KEYS = frozenset(
    {
        "callsite_id",
        "column",
        "conflict_id",
        "conflict_scope",
        "line",
        "path",
        "start_line",
    }
)
_STABLE_CALLSITE_SUBJECT_KEYS = (
    "source_scope",
    "raw_expression",
    "context",
    "call_expression",
    "receiver_expression",
    "attribute",
)


def sha256_file(path: Path) -> str:
    """Hash a regular file without loading it all at once."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head_sha(repo_root: Path) -> str | None:
    return _git_optional(repo_root, ["rev-parse", "HEAD"])


def git_tree_sha(repo_root: Path, commit: str = "HEAD") -> str | None:
    return _git_optional(repo_root, ["rev-parse", f"{commit}^{{tree}}"])


def capture_working_tree_identity(
    repo_root: Path,
    *,
    repo_id: str,
) -> WorkingTreeIdentity:
    """Capture all staged, unstaged, rename/copy, and untracked status data."""

    root = repo_root.resolve()
    raw_status = _git_required(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    records = _parse_porcelain_v1_z(raw_status, root)
    projection = [
        {
            "status": record["status"],
            "path": record["path"],
            **({"orig_path": record["orig_path"]} if "orig_path" in record else {}),
        }
        for record in records
    ]
    changed_paths = sorted(
        {
            path
            for record in records
            for path in (record["path"], record.get("orig_path"))
            if path is not None
        }
    )
    untracked_paths = sorted(
        record["path"] for record in records if record["status"] == "??"
    )
    return WorkingTreeIdentity(
        repo_id=repo_id,
        head_sha=git_head_sha(root),
        branch=_git_optional(root, ["symbolic-ref", "--quiet", "--short", "HEAD"]),
        clean=not records,
        status_digest=canonical_digest(projection),
        changed_paths=changed_paths,
        untracked_paths=untracked_paths,
    )


def source_file_manifest(
    repo_root: Path,
    paths: Iterable[str | Path],
) -> list[dict[str, Any]]:
    """Build a stable manifest for the exact source files under analysis."""

    root = repo_root.resolve()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in paths:
        normalized = normalize_repository_path(raw_path, root)
        if normalized.comparison_key in seen:
            continue
        seen.add(normalized.comparison_key)
        path = root / Path(*normalized.display_path.split("/"))
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RepositoryPathError("source path escaped the repository") from exc
        if path.is_symlink() or not path.is_file():
            raise RepositoryPathError(
                f"analyzed source file is missing or unsafe: {normalized.display_path}"
            )
        records.append(
            {
                "path": normalized.display_path,
                "comparison_key": normalized.comparison_key,
                "file_type": "regular_file",
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    return sorted(
        records, key=lambda record: (record["comparison_key"], record["path"])
    )


def source_file_manifest_digest(repo_root: Path, paths: Iterable[str | Path]) -> str:
    return canonical_digest(source_file_manifest(repo_root, paths))


def indexed_source_file_manifest(
    repo_root: Path,
    records: Iterable[FileRecord],
) -> list[dict[str, Any]]:
    """Project a persisted scanner manifest without consulting live source files."""

    root = repo_root.resolve()
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        normalized = normalize_repository_path(record.path, root)
        if normalized.comparison_key in seen:
            raise BaselineIntegrityMismatch(
                "indexed file manifest has a path collision"
            )
        seen.add(normalized.comparison_key)
        if record.file_size < 0:
            raise BaselineIntegrityMismatch("indexed file manifest has an invalid size")
        result.append(
            {
                "path": normalized.display_path,
                "comparison_key": normalized.comparison_key,
                "file_type": "regular_file",
                "sha256": record.file_hash,
                "size": record.file_size,
            }
        )
    return sorted(result, key=lambda item: (item["comparison_key"], item["path"]))


def indexed_source_file_manifest_digest(
    repo_root: Path,
    records: Iterable[FileRecord],
) -> str:
    return canonical_digest(indexed_source_file_manifest(repo_root, records))


def discover_source_paths(repo_root: Path) -> list[str]:
    """Discover tracked plus untracked source candidates without scanning arbitrary roots."""

    root = repo_root.resolve()
    tracked = _split_nul(_git_required(root, ["ls-files", "-z"]))
    status = _parse_porcelain_v1_z(
        _git_required(
            root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        ),
        root,
    )
    untracked = [record["path"] for record in status if record["status"] == "??"]
    values = set(tracked) | set(untracked)
    return sorted(path for path in values if _looks_like_source_path(path, root))


def capture_build_identity(
    repo_root: Path,
    output_dir: Path,
    *,
    repo_id: str,
    index_version: str | None = None,
) -> BuildIdentity:
    """Capture the immutable identity of a published ArcGraph graph build."""

    root = repo_root.resolve()
    output = output_dir.resolve()
    reader = (
        GraphStoreReader.from_current(output)
        if index_version is None
        else GraphStoreReader.from_build(output, index_version)
    )
    metadata = reader.metadata
    actual_index_version = str(metadata.get("index_version") or "")
    if not actual_index_version:
        raise BaselineIntegrityMismatch("build summary is missing index_version")
    if index_version is not None and actual_index_version != index_version:
        raise BaselineIntegrityMismatch("requested build does not match its summary")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise BaselineIntegrityMismatch("build schema version is unsupported")
    build_dir = reader.sqlite_path.parent
    try:
        build_relative_path = build_dir.relative_to(output).as_posix()
    except ValueError as exc:  # pragma: no cover - GraphStoreReader guard
        raise BaselineIntegrityMismatch(
            "build path is outside ArcGraph output"
        ) from exc
    sqlite_path = reader.sqlite_path
    if sqlite_path.is_symlink() or not sqlite_path.is_file():
        raise BaselineIntegrityMismatch("build index.sqlite is missing or unsafe")
    summary_path = build_dir / "summary.json"
    if summary_path.is_symlink() or not summary_path.is_file():
        raise BaselineIntegrityMismatch("build summary.json is missing or unsafe")
    try:
        manifest_digest = indexed_source_file_manifest_digest(root, reader.iter_files())
    except Exception as exc:
        raise BaselineIntegrityMismatch(
            "build does not provide the required persisted source manifest"
        ) from exc
    # ``output/evidence/manifest.json`` is a live, mutable operator view.  A
    # baseline must bind the immutable manifest snapshot persisted in this
    # build's summary instead; otherwise a later valid reindex would make an
    # older pinned build look tampered with simply because the live manifest
    # was refreshed.
    evidence_manifest = metadata.get("evidence_manifest")
    evidence_digest = (
        canonical_digest(evidence_manifest)
        if isinstance(evidence_manifest, dict)
        else None
    )
    payload: dict[str, Any] = {
        "repo_id": repo_id,
        "index_version": actual_index_version,
        "build_relative_path": build_relative_path,
        "commit_sha": _string_or_none(metadata.get("commit_sha")),
        "git_tree_sha": (
            git_tree_sha(root, str(metadata["commit_sha"]))
            if metadata.get("commit_sha")
            else None
        ),
        "file_manifest_digest": manifest_digest,
        "summary_digest": sha256_file(summary_path),
        "evidence_manifest_digest": evidence_digest,
        "index_sqlite_sha256": sha256_file(sqlite_path),
        "index_sqlite_size": sqlite_path.stat().st_size,
        "freshness": {"status": "unknown", "source": "build-summary"},
    }
    created_at = _datetime_or_none(metadata.get("created_at"))
    if created_at is not None:
        payload["created_at"] = created_at
    return BuildIdentity(**payload)


def verify_build_identity(output_dir: Path, identity: BuildIdentity) -> None:
    """Revalidate build containment, size, hashes, and immutable metadata."""

    output = output_dir.resolve()
    parts = Path(identity.build_relative_path).parts
    if (
        len(parts) != 2
        or parts[0] != "builds"
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise BaselineIntegrityMismatch("build identity has an unsafe build path")
    build_dir = (output / Path(*parts)).resolve(strict=False)
    try:
        build_dir.relative_to((output / "builds").resolve(strict=False))
    except ValueError as exc:
        raise BaselineIntegrityMismatch(
            "build identity escapes the builds root"
        ) from exc
    sqlite_path = build_dir / "index.sqlite"
    summary_path = build_dir / "summary.json"
    if (
        sqlite_path.is_symlink()
        or summary_path.is_symlink()
        or not sqlite_path.is_file()
        or not summary_path.is_file()
    ):
        raise BaselineIntegrityMismatch("baseline build files are missing or unsafe")
    if sqlite_path.stat().st_size != identity.index_sqlite_size:
        raise BaselineIntegrityMismatch("baseline index sqlite size changed")
    if sha256_file(sqlite_path) != identity.index_sqlite_sha256:
        raise BaselineIntegrityMismatch("baseline index sqlite digest changed")
    if sha256_file(summary_path) != identity.summary_digest:
        raise BaselineIntegrityMismatch("baseline summary digest changed")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineIntegrityMismatch("baseline summary cannot be read") from exc
    if (
        summary.get("index_version") != identity.index_version
        or summary.get("schema_version") != identity.schema_version
    ):
        raise BaselineIntegrityMismatch("baseline summary identity does not match")
    if identity.evidence_manifest_digest is not None:
        evidence_manifest = summary.get("evidence_manifest")
        if not isinstance(evidence_manifest, dict):
            raise BaselineIntegrityMismatch(
                "baseline evidence manifest snapshot is missing"
            )
        if canonical_digest(evidence_manifest) != identity.evidence_manifest_digest:
            raise BaselineIntegrityMismatch(
                "baseline evidence manifest snapshot digest changed"
            )


def capture_code_identity(
    repo_root: Path,
    *,
    repo_id: str,
    build_identity: BuildIdentity,
    source_paths: Sequence[str | Path] | None = None,
) -> CodeIdentity:
    """Capture a current-code identity that includes working-tree and manifest state."""

    root = repo_root.resolve()
    working_tree = capture_working_tree_identity(root, repo_id=repo_id)
    paths = (
        list(source_paths) if source_paths is not None else discover_source_paths(root)
    )
    paths.extend(
        path
        for path in working_tree.untracked_paths
        if _looks_like_source_path(path, root)
    )
    return CodeIdentity(
        repo_id=repo_id,
        head_sha=working_tree.head_sha,
        git_tree_sha=git_tree_sha(root),
        working_tree_clean=working_tree.clean,
        working_tree_status_digest=working_tree.status_digest,
        file_manifest_digest=source_file_manifest_digest(root, paths),
        index_version=build_identity.index_version,
        build_identity_digest=build_identity_digest(build_identity),
    )


def assert_code_identity_matches(
    expected: CodeIdentity,
    actual: CodeIdentity,
) -> None:
    if expected.repo_id != actual.repo_id:
        raise CurrentCodeIdentityMismatch("evidence and current code repo_id differ")
    if expected.code_identity_digest != actual.code_identity_digest:
        raise CurrentCodeIdentityMismatch(
            "current source, Git status, or manifest no longer matches recorded identity"
        )


def assert_current_build_matches_code(
    build_identity: BuildIdentity,
    code_identity: CodeIdentity,
) -> None:
    """Fail closed when a graph build does not represent the current source."""

    if code_identity.repo_id != build_identity.repo_id:
        raise CurrentBuildCodeIdentityMismatch("build and current code repo_id differ")
    if code_identity.index_version != build_identity.index_version:
        raise CurrentBuildCodeIdentityMismatch("current code references another index")
    if code_identity.build_identity_digest != build_identity_digest(build_identity):
        raise CurrentBuildCodeIdentityMismatch("current code references another build")
    if (
        build_identity.commit_sha is not None
        and code_identity.head_sha != build_identity.commit_sha
    ):
        raise CurrentBuildCodeIdentityMismatch(
            "current Git HEAD does not match the graph build commit"
        )
    if (
        build_identity.git_tree_sha is not None
        and code_identity.git_tree_sha != build_identity.git_tree_sha
    ):
        raise CurrentBuildCodeIdentityMismatch(
            "current Git tree does not match the graph build tree"
        )
    if code_identity.file_manifest_digest != build_identity.file_manifest_digest:
        raise CurrentBuildCodeIdentityMismatch(
            "current source manifest does not match the current graph build"
        )


def stable_node_identity(repo_id: str, node: Node) -> dict[str, Any]:
    """Create the Change-Safety identity without using canonical_identity.

    ArcGraph's historical ``canonical_identity`` can include project metadata;
    it is therefore intentionally not a Change Stable Identity input.  The
    stable node id comes from an eligibility-checked ArcGraph identifier.  A
    raw value is never self-authenticating: its producer shape must be known
    for the Node kind, and unavailable or position-derived profiles fail
    closed rather than silently joining cross-build records.
    """

    prefix = _typed_node_id_prefix(node)
    frontend_name = node.properties.get("frontend_name")
    if frontend_name is not None and not isinstance(frontend_name, str):
        raise StableIdentityCollision(
            "node identity profile is inapplicable to the declared frontend"
        )
    express_handler_profile = node.properties.get("identity_profile")
    if (
        node.kind == "function"
        and frontend_name == "typescript-static"
        and express_handler_profile == "typescript_express_inline_handler_v1"
    ):
        raise StableIdentityCollision(
            "legacy Express handler identity lacks an enclosing lexical subject"
        )
    if (
        node.kind == "function"
        and frontend_name == "typescript-static"
        and express_handler_profile == "typescript_express_inline_handler_v2"
    ):
        # An earlier extractor revision emitted route-neutral shared handlers
        # under the inline-v2 label. Their explicit shared witness and
        # route-free shape are sufficient to normalize immutable snapshots to
        # the corrected shared-v1 profile instead of blocking every comparison.
        profile = (
            _typescript_express_shared_handler_profile(node, frontend_name)
            if node.properties.get("shared_route_handler") is True
            else _typescript_express_handler_profile(node, frontend_name)
        )
        stable_node_id = f"profile:{canonical_digest(profile)}"
        return {
            "repo_id": repo_id,
            "stable_node_id": stable_node_id,
            "stable_identity": f"{repo_id}:{stable_node_id}",
            "identity_profile_version": NODE_IDENTITY_PROFILE_VERSION,
            "stability_profile": profile,
        }
    if (
        node.kind == "function"
        and frontend_name == "typescript-static"
        and express_handler_profile == "typescript_express_shared_handler_v1"
    ):
        profile = _typescript_express_shared_handler_profile(node, frontend_name)
        stable_node_id = f"profile:{canonical_digest(profile)}"
        return {
            "repo_id": repo_id,
            "stable_node_id": stable_node_id,
            "stable_identity": f"{repo_id}:{stable_node_id}",
            "identity_profile_version": NODE_IDENTITY_PROFILE_VERSION,
            "stability_profile": profile,
        }
    if frontend_name is not None and frontend_name not in _PROFILED_NODE_FRONTENDS:
        return _semantic_frontend_node_identity(
            repo_id,
            node,
            prefix=prefix,
            frontend_name=frontend_name,
        )
    if prefix in _POSITION_DERIVED_NODE_ID_PREFIXES:
        profile = _position_derived_node_profile(node, prefix, frontend_name)
        stable_node_id = f"profile:{canonical_digest(profile)}"
        return {
            "repo_id": repo_id,
            "stable_node_id": stable_node_id,
            "stable_identity": f"{repo_id}:{stable_node_id}",
            "identity_profile_version": NODE_IDENTITY_PROFILE_VERSION,
            "stability_profile": profile,
        }
    _validate_node_id_kind(node, prefix)
    profile = {
        "identity_profile_version": NODE_IDENTITY_PROFILE_VERSION,
        "identity_source": "arcgraph_node_id_v1",
        "id_prefix": prefix,
        "kind": node.kind,
        "frontend_name": frontend_name,
        "path": node.path,
        "qualname": node.qualname,
    }
    return {
        "repo_id": repo_id,
        "stable_node_id": node.id,
        "stable_identity": f"{repo_id}:{node.id}",
        "identity_profile_version": NODE_IDENTITY_PROFILE_VERSION,
        "stability_profile": profile,
    }


def _semantic_frontend_node_identity(
    repo_id: str,
    node: Node,
    *,
    prefix: str,
    frontend_name: str,
) -> dict[str, Any]:
    """Bind shipped protocol/external nodes to language-neutral witnesses."""

    if not node.path and not node.qualname:
        raise StableIdentityCollision(
            "semantic frontend identity requires a path or qualname witness"
        )
    declared_language = node.properties.get("language")
    language = (
        declared_language
        if isinstance(declared_language, str) and declared_language.strip()
        else _SEMANTIC_NODE_FRONTENDS.get(frontend_name, "unknown")
    )
    profile = {
        "identity_profile_version": NODE_IDENTITY_PROFILE_VERSION,
        "identity_source": "language_neutral_semantic_node_v1",
        "frontend_name": frontend_name,
        "language": language,
        "id_prefix": prefix,
        "raw_node_id": node.id,
        "kind": node.kind,
        "path": node.path,
        "qualname": node.qualname,
        "name": node.name,
    }
    stable_node_id = f"semantic:{canonical_digest(profile)}"
    return {
        "repo_id": repo_id,
        "stable_node_id": stable_node_id,
        "stable_identity": f"{repo_id}:{stable_node_id}",
        "identity_profile_version": NODE_IDENTITY_PROFILE_VERSION,
        "stability_profile": profile,
    }


def stable_edge_identity(
    repo_id: str,
    edge: Edge,
    *,
    nodes: Mapping[str, Mapping[str, Any]] | None = None,
    source_identity: str | None = None,
    target_identity: str | None = None,
) -> dict[str, Any]:
    """Build an edge identity from Source/Target Change Stable Identities."""

    if source_identity is None or target_identity is None:
        if nodes is None:
            raise StableIdentityCollision(
                "edge identity requires validated source and target node identities"
            )
        source_identity = nodes.get(edge.source, {}).get("stable_identity")
        target_identity = nodes.get(edge.target, {}).get("stable_identity")
    source = source_identity
    target = target_identity
    if not isinstance(source, str) or not isinstance(target, str):
        raise StableIdentityCollision(
            "edge identity references a node without a validated stable identity"
        )
    expected_prefix = f"{repo_id}:"
    if not source.startswith(expected_prefix) or not target.startswith(expected_prefix):
        raise StableIdentityCollision(
            "edge identity endpoints do not belong to the requested repository"
        )
    projection = {
        "repo_id": repo_id,
        "source": source,
        "target": target,
        "kind": edge.kind,
        "semantic_role": edge.semantic_role,
    }
    return {
        **projection,
        "stable_edge_id": canonical_digest(projection),
    }


def _typed_node_id_prefix(node: Node) -> str:
    if not node.id or ":" not in node.id:
        raise StableIdentityCollision("node identity profile requires a typed node.id")
    prefix, payload = node.id.split(":", 1)
    if not prefix or not payload:
        raise StableIdentityCollision(
            "node identity profile requires a populated typed node.id"
        )
    return prefix


def _validate_node_id_kind(node: Node, prefix: str) -> None:
    """Accept only a known ArcGraph node-id family for the declared kind."""

    allowed = _NODE_ID_PREFIXES_BY_KIND.get(node.kind, frozenset())
    if prefix not in allowed:
        raise StableIdentityCollision(
            "node identity profile is inapplicable to the node id and kind"
        )


def _position_derived_node_profile(
    node: Node,
    prefix: str,
    frontend_name: Any,
) -> dict[str, Any]:
    """Validate producer-specific witnesses for position-derived raw ids."""

    properties = node.properties
    common = {
        "identity_profile_version": NODE_IDENTITY_PROFILE_VERSION,
        "id_prefix": prefix,
        "kind": node.kind,
        "frontend_name": frontend_name,
    }
    if (
        prefix == "local"
        and node.kind in {"class", "function"}
        and properties.get("local_definition") is True
    ):
        scope = _required_non_empty_property(properties, "scope")
        if not node.qualname or not node.path:
            raise StableIdentityCollision(
                "local definition identity requires path and qualname witnesses"
            )
        return {
            **common,
            "identity_source": "python_local_definition_v1",
            "scope": scope,
            "name": node.name,
            "qualname": node.qualname,
            "path": node.path,
        }
    if (
        prefix == "decl"
        and node.kind == "declaration"
        and properties.get("reason") == "class_body_declaration"
    ):
        return {
            **common,
            "identity_source": "python_declaration_callsite_v1",
            **_stable_callsite_profile(properties),
        }
    if (
        prefix == "unresolved"
        and node.kind == "diagnostic"
        and properties.get("diagnostic_kind") == "unresolved_dynamic_callsite"
    ):
        return {
            **common,
            "identity_source": "python_unresolved_callsite_v1",
            **_stable_callsite_profile(properties),
        }
    if (
        prefix == "unresolved"
        and node.kind == "diagnostic"
        and frontend_name == "typescript-static"
        and properties.get("identity_profile") == "typescript_unresolved_reference_v1"
    ):
        specifier = _required_non_empty_property(properties, "specifier")
        if node.id != f"unresolved:{specifier}":
            raise StableIdentityCollision(
                "TypeScript unresolved-reference id does not match its witness"
            )
        return {
            **common,
            "identity_source": "typescript_unresolved_reference_v1",
            "specifier": specifier,
        }
    raise StableIdentityCollision(
        "node identity profile is inapplicable to a position-derived node id"
    )


def _typescript_express_handler_profile(
    node: Node,
    frontend_name: str,
) -> dict[str, Any]:
    properties = node.properties
    ambiguous = properties.get("stable_handler_ambiguous")
    if ambiguous is not None and not isinstance(ambiguous, bool):
        raise StableIdentityCollision(
            "Express handler ambiguity witness must be boolean"
        )
    if ambiguous:
        raise StableIdentityCollision(
            "Express handler identity is ambiguous within its lexical subject"
        )
    body_hash = _required_non_empty_property(properties, "handler_body_sha256")
    if len(body_hash) != 64 or any(
        character not in "0123456789abcdef" for character in body_hash
    ):
        raise StableIdentityCollision(
            "Express handler identity requires a SHA-256 body witness"
        )
    handler_index = properties.get("route_handler_index")
    occurrence = properties.get("stable_handler_occurrence")
    if (
        isinstance(handler_index, bool)
        or not isinstance(handler_index, int)
        or handler_index < 0
        or isinstance(occurrence, bool)
        or not isinstance(occurrence, int)
        or occurrence < 1
    ):
        raise StableIdentityCollision(
            "Express handler identity requires valid handler and occurrence ordinals"
        )
    if not node.path:
        raise StableIdentityCollision(
            "Express handler identity requires a source path witness"
        )
    return {
        "identity_profile_version": NODE_IDENTITY_PROFILE_VERSION,
        "identity_source": "typescript_express_inline_handler_v2",
        "kind": node.kind,
        "frontend_name": frontend_name,
        "path": node.path,
        "declaring_module": _required_non_empty_property(
            properties, "declaring_module"
        ),
        "handler_enclosing_subject": _required_non_empty_property(
            properties, "handler_enclosing_subject"
        ),
        "route_receiver": _required_non_empty_property(properties, "route_receiver"),
        "route_method": _required_non_empty_property(properties, "route_method"),
        "route_path": _required_non_empty_property(properties, "route_path"),
        "route_handler_index": handler_index,
        "handler_body_sha256": body_hash,
        "stable_handler_occurrence": occurrence,
    }


def _typescript_express_shared_handler_profile(
    node: Node,
    frontend_name: str,
) -> dict[str, Any]:
    properties = node.properties
    ambiguous = properties.get("stable_handler_ambiguous")
    if ambiguous is not None and not isinstance(ambiguous, bool):
        raise StableIdentityCollision(
            "Express handler ambiguity witness must be boolean"
        )
    if ambiguous:
        raise StableIdentityCollision(
            "Express handler identity is ambiguous within its lexical subject"
        )
    if properties.get("shared_route_handler") is not True:
        raise StableIdentityCollision(
            "Shared Express handler identity requires a shared-handler witness"
        )
    route_fields = (
        "route_receiver",
        "route_method",
        "route_path",
        "route_handler_index",
    )
    if any(field in properties for field in route_fields):
        raise StableIdentityCollision(
            "Shared Express handler identity must not contain route-bound witnesses"
        )
    body_hash = _required_non_empty_property(properties, "handler_body_sha256")
    if len(body_hash) != 64 or any(
        character not in "0123456789abcdef" for character in body_hash
    ):
        raise StableIdentityCollision(
            "Express handler identity requires a SHA-256 body witness"
        )
    occurrence = properties.get("stable_handler_occurrence")
    if (
        isinstance(occurrence, bool)
        or not isinstance(occurrence, int)
        or occurrence < 1
    ):
        raise StableIdentityCollision(
            "Shared Express handler identity requires a valid occurrence ordinal"
        )
    if not node.path:
        raise StableIdentityCollision(
            "Express handler identity requires a source path witness"
        )
    return {
        "identity_profile_version": NODE_IDENTITY_PROFILE_VERSION,
        "identity_source": "typescript_express_shared_handler_v1",
        "kind": node.kind,
        "frontend_name": frontend_name,
        "path": node.path,
        "declaring_module": _required_non_empty_property(
            properties, "declaring_module"
        ),
        "handler_enclosing_subject": _required_non_empty_property(
            properties, "handler_enclosing_subject"
        ),
        "shared_route_handler": True,
        "handler_body_sha256": body_hash,
        "stable_handler_occurrence": occurrence,
    }


def _stable_callsite_profile(properties: Mapping[str, Any]) -> dict[str, Any]:
    subject = properties.get("stable_callsite_subject")
    occurrence = properties.get("stable_callsite_occurrence")
    if not isinstance(subject, Mapping):
        raise StableIdentityCollision(
            "position-derived node lacks a stable callsite subject"
        )
    normalized_subject = {
        key: (
            _non_empty_string(subject.get(key))
            if subject.get(key) is not None
            else None
        )
        for key in _STABLE_CALLSITE_SUBJECT_KEYS
    }
    if (
        not normalized_subject["source_scope"]
        or not normalized_subject["raw_expression"]
    ):
        raise StableIdentityCollision(
            "stable callsite subject lacks source_scope or raw_expression"
        )
    if normalized_subject["source_scope"] != _required_non_empty_property(
        properties, "source_scope"
    ) or normalized_subject["raw_expression"] != _required_non_empty_property(
        properties, "raw_expression"
    ):
        raise StableIdentityCollision(
            "stable callsite subject conflicts with its node witnesses"
        )
    if (
        isinstance(occurrence, bool)
        or not isinstance(occurrence, int)
        or occurrence < 1
    ):
        raise StableIdentityCollision(
            "stable callsite occurrence must be a positive integer"
        )
    return {
        "stable_callsite_subject": normalized_subject,
        "stable_callsite_occurrence": occurrence,
    }


def _required_non_empty_property(properties: Mapping[str, Any], key: str) -> str:
    value = _non_empty_string(properties.get(key))
    if value is None:
        raise StableIdentityCollision(
            f"node identity profile requires a non-empty {key} witness"
        )
    return value


def stable_diagnostic_identity(
    repo_id: str,
    diagnostic: SemanticDiagnostic,
) -> dict[str, Any]:
    """Produce a versioned, location-independent diagnostic identity.

    The graph diagnostic_id and lifecycle fields are deliberately excluded. A
    diagnostic must instead identify a semantic subject: a fact, a callsite,
    or the documented source-scope fallback. Missing all of those
    discriminators is unsafe because unrelated diagnostics could otherwise be
    silently merged by Graph Delta.
    """

    subject = _diagnostic_subject(diagnostic)
    projection = {
        "repo_id": repo_id,
        "identity_profile_version": DIAGNOSTIC_IDENTITY_PROFILE_VERSION,
        "diagnostic_kind": diagnostic.diagnostic_kind,
        "frontend_name": diagnostic.frontend_name,
        "subject": subject,
    }
    return {
        "repo_id": repo_id,
        "stable_diagnostic_id": canonical_digest(projection),
        "identity_profile_version": DIAGNOSTIC_IDENTITY_PROFILE_VERSION,
        "stability_profile": projection,
        "preimage_digest": canonical_digest(projection),
    }


def stable_diagnostic_index(
    repo_id: str,
    diagnostics: Iterable[SemanticDiagnostic],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for diagnostic in diagnostics:
        identity = stable_diagnostic_identity(repo_id, diagnostic)
        key = identity["stable_diagnostic_id"]
        existing = result.get(key)
        if existing and existing["stability_profile"] != identity["stability_profile"]:
            raise StableIdentityCollision(
                f"diagnostic stable id {key!r} has a projection collision"
            )
        result[key] = identity
    return result


def diagnostic_semantic_projection(diagnostic: SemanticDiagnostic) -> dict[str, Any]:
    """Project diagnostic semantics separately from identity and lifecycle."""

    message_template = _normalized_message_template(diagnostic.message)
    if _diagnostic_stable_conflict_subject(diagnostic.properties) is not None:
        message_template = "Confirmed facts disagree"
    return {
        "diagnostic_kind": diagnostic.diagnostic_kind,
        "frontend_name": diagnostic.frontend_name,
        "message_template": message_template,
        "severity": diagnostic.severity,
        "properties": _diagnostic_properties_projection(diagnostic.properties),
    }


def diagnostic_lifecycle_projection(diagnostic: SemanticDiagnostic) -> dict[str, Any]:
    """Project only lifecycle metadata and location for audit visibility."""

    return {
        "index_version": diagnostic.index_version,
        "first_seen_index": diagnostic.first_seen_index,
        "last_seen_index": diagnostic.last_seen_index,
        "seen_count": diagnostic.seen_count,
        "lifecycle_status": diagnostic.properties.get("diagnostic_lifecycle_status"),
        "lifecycle_properties": {
            key: stable_projection(value)
            for key, value in sorted(diagnostic.properties.items())
            if key in _DIAGNOSTIC_LIFECYCLE_PROPERTY_KEYS
        },
        "path": diagnostic.path,
        "start_line": diagnostic.start_line,
        "end_line": diagnostic.end_line,
        "location_properties": {
            key: stable_projection(value)
            for key, value in sorted(diagnostic.properties.items())
            if key in _DIAGNOSTIC_LOCATION_PROPERTY_KEYS
        },
    }


def _diagnostic_subject(diagnostic: SemanticDiagnostic) -> dict[str, Any]:
    if diagnostic.fact_id and diagnostic.fact_id.strip():
        return {"kind": "fact_id", "value": diagnostic.fact_id.strip()}
    properties = diagnostic.properties
    stable_conflict = _diagnostic_stable_conflict_subject(properties)
    if stable_conflict is not None:
        return stable_conflict
    warning_subject = _diagnostic_warning_subject(diagnostic)
    if warning_subject is not None:
        return warning_subject
    stable_callsite = _diagnostic_stable_callsite_subject(properties)
    if stable_callsite is not None:
        return stable_callsite
    callsite_id = _non_empty_string(properties.get("callsite_id"))
    if callsite_id:
        return {"kind": "callsite_id", "value": callsite_id}
    source_scope = _non_empty_string(properties.get("source_scope"))
    failed_strategy = _non_empty_string(properties.get("failed_strategy"))
    if not source_scope or not failed_strategy:
        raise StableIdentityCollision(
            "diagnostic lacks fact_id, callsite_id, or a complete fallback subject"
        )
    projected_properties = _diagnostic_properties_projection(properties)
    return {
        "kind": "source_scope_fallback",
        "source_scope": source_scope,
        "failed_strategy": failed_strategy,
        "properties_digest": canonical_digest(projected_properties),
        "message_template_digest": canonical_digest(
            _normalized_message_template(diagnostic.message)
        ),
    }


def _diagnostic_properties_projection(properties: dict[str, Any]) -> dict[str, Any]:
    excluded = set(_DIAGNOSTIC_LIFECYCLE_PROPERTY_KEYS)
    if (
        _diagnostic_stable_callsite_subject(properties) is not None
        or _diagnostic_stable_conflict_subject(properties) is not None
    ):
        excluded.update(_DIAGNOSTIC_LOCATION_PROPERTY_KEYS)
    return {
        key: stable_projection(value)
        for key, value in sorted(properties.items())
        if key not in excluded
    }


def _diagnostic_stable_callsite_subject(
    properties: Mapping[str, Any],
) -> dict[str, Any] | None:
    subject = properties.get("stable_callsite_subject")
    occurrence = properties.get("stable_callsite_occurrence")
    if subject is None and occurrence is None:
        return None
    profile = _stable_callsite_profile(properties)
    return {
        "kind": "stable_callsite_v1",
        **profile,
    }


def _diagnostic_stable_conflict_subject(
    properties: Mapping[str, Any],
) -> dict[str, Any] | None:
    subject = properties.get("stable_conflict_subject")
    occurrence = properties.get("stable_conflict_occurrence")
    if subject is None and occurrence is None:
        return None
    if not isinstance(subject, Mapping):
        raise StableIdentityCollision("merge conflict lacks a stable subject")
    source = _non_empty_string(subject.get("source"))
    edge_kind = _non_empty_string(subject.get("edge_kind"))
    targets = subject.get("targets")
    semantic_role = subject.get("semantic_role")
    if (
        source is None
        or edge_kind is None
        or not isinstance(targets, list)
        or not targets
        or any(_non_empty_string(target) is None for target in targets)
        or (semantic_role is not None and not isinstance(semantic_role, str))
    ):
        raise StableIdentityCollision("merge conflict has an invalid stable subject")
    normalized_targets = sorted(str(target).strip() for target in targets)
    if (
        source != _non_empty_string(properties.get("source"))
        or edge_kind != _non_empty_string(properties.get("edge_kind"))
        or semantic_role != properties.get("semantic_role")
        or normalized_targets != sorted(properties.get("targets") or [])
    ):
        raise StableIdentityCollision(
            "merge conflict stable subject conflicts with its witnesses"
        )
    if (
        isinstance(occurrence, bool)
        or not isinstance(occurrence, int)
        or occurrence < 1
    ):
        raise StableIdentityCollision(
            "merge conflict occurrence must be a positive integer"
        )
    return {
        "kind": "stable_merge_conflict_v1",
        "source": source,
        "edge_kind": edge_kind,
        "semantic_role": semantic_role,
        "targets": normalized_targets,
        "occurrence": occurrence,
    }


def _diagnostic_warning_subject(
    diagnostic: SemanticDiagnostic,
) -> dict[str, Any] | None:
    subject = diagnostic.properties.get("warning_subject")
    if subject is None:
        return None
    if not isinstance(subject, Mapping):
        raise StableIdentityCollision("build warning lacks a stable subject")
    path = subject.get("path")
    message = _non_empty_string(subject.get("message"))
    if (
        (path is not None and not isinstance(path, str))
        or path != diagnostic.path
        or message != _normalized_message_template(diagnostic.message)
    ):
        raise StableIdentityCollision(
            "build warning stable subject conflicts with its witnesses"
        )
    return {
        "kind": "build_warning_v1",
        "path": path,
        "message": message,
    }


def _normalized_message_template(value: str) -> str:
    return " ".join(value.split())


def _non_empty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _git_required(repo_root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise BaselineSourceUnavailable(detail or "Git repository state is unavailable")
    return result.stdout.decode("utf-8", errors="surrogateescape")


def _git_optional(repo_root: Path, args: list[str]) -> str | None:
    try:
        value = _git_required(repo_root, args).strip()
    except BaselineSourceUnavailable:
        return None
    return value or None


def _parse_porcelain_v1_z(raw: str, repo_root: Path) -> list[dict[str, str]]:
    tokens = _split_nul(raw)
    records: list[dict[str, str]] = []
    position = 0
    while position < len(tokens):
        item = tokens[position]
        position += 1
        if len(item) < 4:
            raise BaselineSourceUnavailable("Git status output is malformed")
        status = item[:2]
        if item[2] != " ":
            raise BaselineSourceUnavailable("Git status output lacks path separator")
        path = normalize_repository_path(item[3:], repo_root).display_path
        record: dict[str, str] = {"status": status, "path": path}
        if "R" in status or "C" in status:
            if position >= len(tokens):
                raise BaselineSourceUnavailable("Git rename/copy status is incomplete")
            record["orig_path"] = normalize_repository_path(
                tokens[position], repo_root
            ).display_path
            position += 1
        records.append(record)
    return sorted(
        records,
        key=lambda record: (
            record["status"],
            record["path"],
            record.get("orig_path", ""),
        ),
    )


def _split_nul(raw: str) -> list[str]:
    return [item for item in raw.split("\0") if item]


def _looks_like_source_path(path: str, repo_root: Path | None = None) -> bool:
    normalized = path.replace("\\", "/")
    relative = Path(normalized)
    # Share the scanner's built-in exclusion vocabulary so generated/tooling
    # paths do not register as changed source when the scanner omits them.
    if is_ignored_repo_relative_path(relative):
        return False
    if repo_root is not None and is_typescript_emit_artifact(repo_root / relative):
        return False
    return relative.suffix.lower() in _SOURCE_SUFFIXES


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
