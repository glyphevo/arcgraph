from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

from arcgraph.change.contracts import BuildIdentity
from arcgraph.change.errors import (
    CurrentBuildCodeIdentityMismatch,
    StableIdentityCollision,
)
from arcgraph.change.identities import (
    _looks_like_source_path,
    assert_current_build_matches_code,
    capture_build_identity,
    capture_code_identity,
    stable_diagnostic_identity,
    stable_node_identity,
)
from arcgraph.core.graph_store import GraphStoreWriter
from arcgraph.core.schemas import FileRecord, IndexMetadata, Node, SemanticDiagnostic


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo.parent, "init", repo.name)
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "ArcGraph Tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _build_identity() -> BuildIdentity:
    return BuildIdentity(
        repo_id="repo",
        index_version="index",
        build_relative_path="builds/index",
        file_manifest_digest="not-used-by-capture",
        summary_digest="summary",
        index_sqlite_sha256="sqlite",
        index_sqlite_size=1,
    )


def test_code_identity_covers_untracked_source_content(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    build = _build_identity()
    initial = capture_code_identity(
        repo,
        repo_id="repo",
        build_identity=build,
        source_paths=["src/app.py"],
    )
    (repo / "src" / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
    changed = capture_code_identity(
        repo,
        repo_id="repo",
        build_identity=build,
        source_paths=["src/app.py", "src/new.py"],
    )

    assert initial.working_tree_clean
    assert not changed.working_tree_clean
    assert (
        "src/new.py" in changed.working_tree_status_digest
        or changed.code_identity_digest
    )
    assert initial.code_identity_digest != changed.code_identity_digest


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/module.mts", True),
        ("src/module.cjs", True),
        ("src/Component.vue", True),
        (".claude/worktrees/task/src/module.mts", False),
        (".venv/lib/module.py", False),
        ("frontend/.next/server/page.ts", False),
        # The emit-artifact branch runs only when repo_root is supplied, which
        # is what the production callers do. A hand-written sibling of a
        # TypeScript source stays source; a compiler emit does not.
        ("src/shim.js", True),
        ("src/built.js", False),
    ],
)
def test_source_path_detection_matches_scanner_vocabulary(
    tmp_path: Path,
    path: str,
    expected: bool,
) -> None:
    source = tmp_path / "src"
    source.mkdir(parents=True, exist_ok=True)
    (source / "shim.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (source / "shim.js").write_text(
        "module.exports = require('./dist');\n", encoding="utf-8"
    )
    (source / "built.ts").write_text("export const b = 1;\n", encoding="utf-8")
    (source / "built.js").write_text(
        "exports.b = 1;\n//# sourceMappingURL=built.js.map\n", encoding="utf-8"
    )

    # repo_root must be passed: omitting it silently skips the emit-artifact
    # filter, so the assertion would hold whether or not that filter works.
    assert _looks_like_source_path(path, tmp_path) is expected


def test_node_identity_does_not_use_historical_canonical_identity() -> None:
    first = Node(
        id="fn:src.app.f",
        kind="function",
        name="f",
        qualname="app.f",
        path="src/app.py",
        canonical_identity="project-a::app.f",
    )
    second = first.model_copy(update={"canonical_identity": "project-b::app.f"})

    first_identity = stable_node_identity("repo", first)
    second_identity = stable_node_identity("repo", second)

    assert first_identity == second_identity
    assert first_identity["identity_profile_version"] == "1.4"
    assert first_identity["stability_profile"]["identity_profile_version"] == "1.4"


def test_express_handler_identity_tracks_body_not_registration_ordinal() -> None:
    common_properties = {
        "frontend_name": "typescript-static",
        "identity_profile": "typescript_express_inline_handler_v2",
        "declaring_module": "server",
        "handler_enclosing_subject": "fn:server.configureRoutes",
        "route_receiver": "app",
        "route_method": "GET",
        "route_path": "/users",
        "route_handler_index": 0,
        "stable_handler_occurrence": 1,
    }
    original = Node(
        id="fn:server.__express_registration_0",
        kind="function",
        name="GET /users handler",
        qualname="server.__express_registration_0",
        path="src/server.ts",
        properties={
            **common_properties,
            "handler_body_sha256": "a" * 64,
        },
    )
    shifted = original.model_copy(
        deep=True,
        update={
            "id": "fn:server.__express_registration_1",
            "qualname": "server.__express_registration_1",
        },
    )
    replacement = original.model_copy(deep=True)
    replacement.properties["handler_body_sha256"] = "b" * 64

    assert stable_node_identity("repo", original) == stable_node_identity(
        "repo", shifted
    )
    assert (
        stable_node_identity("repo", original)["stable_identity"]
        != stable_node_identity("repo", replacement)["stable_identity"]
    )


def test_node_identity_rejects_ambiguous_express_handlers() -> None:
    handler = Node(
        id="fn:server.__express_get_users_handler_0",
        kind="function",
        name="GET /users handler",
        qualname="server.__express_get_users_handler_0",
        path="src/server.ts",
        properties={
            "frontend_name": "typescript-static",
            "identity_profile": "typescript_express_inline_handler_v2",
            "declaring_module": "server",
            "handler_enclosing_subject": "mod:server",
            "route_receiver": "app",
            "route_method": "GET",
            "route_path": "/users",
            "route_handler_index": 0,
            "handler_body_sha256": "a" * 64,
            "stable_handler_occurrence": 1,
            "stable_handler_ambiguous": True,
        },
    )

    with pytest.raises(StableIdentityCollision, match="ambiguous"):
        stable_node_identity("repo", handler)


def test_shared_express_handler_identity_uses_route_neutral_witnesses() -> None:
    handler = Node(
        id="fn:server.__express_shared_handler_1",
        kind="function",
        name="shared Express handler",
        qualname="server.__express_shared_handler_1",
        path="src/server.ts",
        properties={
            "frontend_name": "typescript-static",
            "identity_profile": "typescript_express_shared_handler_v1",
            "declaring_module": "server",
            "handler_enclosing_subject": "mod:server::binding:shared",
            "shared_route_handler": True,
            "handler_body_sha256": "a" * 64,
            "stable_handler_occurrence": 1,
            "stable_handler_ambiguous": False,
        },
    )
    shifted = handler.model_copy(
        update={
            "id": "fn:server.__express_shared_handler_2",
            "qualname": "server.__express_shared_handler_2",
        }
    )
    replacement = handler.model_copy(deep=True)
    replacement.properties["handler_body_sha256"] = "b" * 64
    legacy = handler.model_copy(deep=True)
    legacy.properties["identity_profile"] = "typescript_express_inline_handler_v2"

    identity = stable_node_identity("repo", handler)

    assert identity == stable_node_identity("repo", shifted)
    assert identity == stable_node_identity("repo", legacy)
    assert identity != stable_node_identity("repo", replacement)
    assert (
        identity["stability_profile"]["identity_source"]
        == "typescript_express_shared_handler_v1"
    )
    assert "route_path" not in identity["stability_profile"]


def test_shared_express_handler_identity_rejects_route_bound_witnesses() -> None:
    handler = Node(
        id="fn:server.__express_shared_handler_1",
        kind="function",
        name="shared Express handler",
        qualname="server.__express_shared_handler_1",
        path="src/server.ts",
        properties={
            "frontend_name": "typescript-static",
            "identity_profile": "typescript_express_shared_handler_v1",
            "declaring_module": "server",
            "handler_enclosing_subject": "mod:server::binding:shared",
            "shared_route_handler": True,
            "route_path": "/first",
            "handler_body_sha256": "a" * 64,
            "stable_handler_occurrence": 1,
            "stable_handler_ambiguous": False,
        },
    )

    with pytest.raises(StableIdentityCollision, match="route-bound"):
        stable_node_identity("repo", handler)


def test_node_identity_rejects_legacy_express_handler_profile() -> None:
    handler = Node(
        id="fn:server.__express_get_users_handler_0",
        kind="function",
        name="GET /users handler",
        qualname="server.__express_get_users_handler_0",
        path="src/server.ts",
        properties={
            "frontend_name": "typescript-static",
            "identity_profile": "typescript_express_inline_handler_v1",
            "declaring_module": "server",
            "route_receiver": "app",
            "route_method": "GET",
            "route_path": "/users",
            "route_handler_index": 0,
            "handler_body_sha256": "a" * 64,
            "stable_handler_occurrence": 1,
        },
    )

    with pytest.raises(StableIdentityCollision, match="enclosing lexical subject"):
        stable_node_identity("repo", handler)


def test_node_identity_rejects_an_unprofiled_identifier() -> None:
    """An arbitrary node.id cannot be declared cross-build stable by itself."""

    unprofiled = Node(
        id="foreign-project-v9::function:app.f",
        kind="function",
        name="f",
        qualname="app.f",
        path="src/app.py",
    )

    with pytest.raises(StableIdentityCollision, match="identity profile"):
        stable_node_identity("repo", unprofiled)


def test_node_identity_rejects_an_unwitnessed_position_derived_local_binding() -> None:
    """A raw local binding id is insufficient without its producer witnesses."""

    local = Node(
        id="local:binding:0123456789abcdef",
        kind="function",
        name="helper",
        qualname="app.outer.<locals>.helper",
        path="src/app.py",
        properties={"local_definition": True},
    )

    with pytest.raises(StableIdentityCollision, match="scope witness"):
        stable_node_identity("repo", local)


def test_node_identity_uses_local_definition_witnesses_instead_of_raw_id() -> None:
    common = {
        "kind": "function",
        "name": "helper",
        "qualname": "app.outer.<locals>.helper",
        "path": "src/app.py",
        "properties": {
            "local_definition": True,
            "scope": "fn:app.outer",
        },
    }
    first = Node(id="local:binding:first-position", **common)
    moved = Node(id="local:binding:second-position", start_line=30, **common)

    first_identity = stable_node_identity("repo", first)
    moved_identity = stable_node_identity("repo", moved)

    assert first_identity == moved_identity
    assert (
        first_identity["stability_profile"]["identity_source"]
        == "python_local_definition_v1"
    )


def test_node_identity_distinguishes_repeated_stable_callsites() -> None:
    subject = {
        "source_scope": "fn:app.run",
        "raw_expression": "client.send",
        "context": "function_body",
        "call_expression": "client.send()",
        "receiver_expression": "client",
        "attribute": "send",
    }
    common_properties = {
        "diagnostic_kind": "unresolved_dynamic_callsite",
        "reason": "dynamic_dispatch",
        "source_scope": "fn:app.run",
        "raw_expression": "client.send",
        "stable_callsite_subject": subject,
    }
    first = Node(
        id="unresolved:first-position",
        kind="diagnostic",
        name="client.send",
        path="src/app.py",
        properties={**common_properties, "stable_callsite_occurrence": 1},
    )
    second = first.model_copy(
        update={
            "id": "unresolved:second-position",
            "properties": {
                **common_properties,
                "stable_callsite_occurrence": 2,
            },
        }
    )

    assert (
        stable_node_identity("repo", first)["stable_identity"]
        != stable_node_identity("repo", second)["stable_identity"]
    )


def test_node_identity_uses_language_neutral_fallback_for_unknown_frontend() -> None:
    node = Node(
        id="fn:app.f",
        kind="function",
        name="f",
        qualname="app.f",
        path="src/app.py",
        properties={"frontend_name": "third-party-unprofiled"},
    )

    identity = stable_node_identity("repo", node)

    assert identity["stable_node_id"].startswith("semantic:")
    assert identity["stability_profile"]["frontend_name"] == "third-party-unprofiled"
    assert identity["stability_profile"]["language"] == "unknown"


@pytest.mark.parametrize(
    "frontend_name",
    [
        "c-semantic-external",
        "cpp-semantic-external",
        "csharp-semantic-external",
        "go-semantic-external",
        "java-semantic-external",
        "openapi-protocol",
        "rust-semantic-external",
        "scip-protocol",
        "swift-semantic-external",
    ],
)
def test_shipped_semantic_frontends_receive_stable_profiles(
    frontend_name: str,
) -> None:
    node = Node(
        id="fn:app.f",
        kind="function",
        name="f",
        qualname="app.f",
        path="src/app.ext",
        properties={"frontend_name": frontend_name},
    )

    identity = stable_node_identity("repo", node)

    assert identity["stable_node_id"].startswith("semantic:")
    assert (
        identity["stability_profile"]["identity_source"]
        == "language_neutral_semantic_node_v1"
    )


def test_diagnostic_identity_excludes_location_and_lifecycle_fields() -> None:
    common = {
        "repo_id": "repo",
        "index_version": "index",
        "diagnostic_kind": "unresolved_call",
        "message": "cannot resolve call",
        "severity": "warning",
        "frontend_name": "python",
        "fact_id": "fact:call",
        "properties": {"receiver": "x"},
    }
    first = SemanticDiagnostic(
        diagnostic_id="lifecycle-a",
        path="src/app.py",
        start_line=3,
        end_line=3,
        first_seen_index="one",
        last_seen_index="one",
        seen_count=1,
        **common,
    )
    second = SemanticDiagnostic(
        diagnostic_id="lifecycle-b",
        path="src/app.py",
        start_line=30,
        end_line=30,
        first_seen_index="one",
        last_seen_index="two",
        seen_count=2,
        **common,
    )

    assert stable_diagnostic_identity("repo", first) == stable_diagnostic_identity(
        "repo", second
    )


def test_diagnostic_identity_prefers_a_location_independent_callsite_subject() -> None:
    common_properties = {
        "source_scope": "fn:app.run",
        "raw_expression": "client.send",
        "failed_strategy": "dynamic_dispatch",
        "stable_callsite_subject": {
            "source_scope": "fn:app.run",
            "raw_expression": "client.send",
            "context": "function_body",
            "call_expression": "client.send()",
            "receiver_expression": "client",
            "attribute": "send",
        },
        "stable_callsite_occurrence": 1,
    }
    first = SemanticDiagnostic(
        diagnostic_id="diagnostic:first",
        repo_id="repo",
        index_version="first",
        diagnostic_kind="unresolved_callsite",
        message="Unresolved Python callsite 'client.send' in fn:app.run",
        frontend_name="python",
        path="src/app.py",
        start_line=10,
        properties={
            **common_properties,
            "callsite_id": "callsite:first-position",
            "path": "src/app.py",
            "line": 10,
            "column": 4,
        },
    )
    moved = first.model_copy(
        update={
            "diagnostic_id": "diagnostic:moved",
            "start_line": 30,
            "properties": {
                **common_properties,
                "callsite_id": "callsite:second-position",
                "path": "src/app.py",
                "line": 30,
                "column": 8,
            },
        }
    )

    assert stable_diagnostic_identity("repo", first) == stable_diagnostic_identity(
        "repo", moved
    )


def test_diagnostic_identity_uses_callsite_and_fallback_subjects() -> None:
    common = {
        "repo_id": "repo",
        "index_version": "index",
        "diagnostic_kind": "unresolved_call",
        "message": "cannot resolve call",
        "severity": "warning",
        "frontend_name": "python",
    }
    first_callsite = SemanticDiagnostic(
        diagnostic_id="diagnostic:first",
        properties={"callsite_id": "callsite:one"},
        **common,
    )
    second_callsite = first_callsite.model_copy(
        update={
            "diagnostic_id": "diagnostic:second",
            "properties": {"callsite_id": "callsite:two"},
        }
    )
    first_fallback = SemanticDiagnostic(
        diagnostic_id="diagnostic:fallback-one",
        properties={
            "source_scope": "fn:app.f",
            "failed_strategy": "static_lookup",
            "receiver": "client",
        },
        **common,
    )
    second_fallback = first_fallback.model_copy(
        update={
            "diagnostic_id": "diagnostic:fallback-two",
            "properties": {
                "source_scope": "fn:app.f",
                "failed_strategy": "dynamic_dispatch",
                "receiver": "client",
            },
        }
    )

    assert (
        stable_diagnostic_identity("repo", first_callsite)["stable_diagnostic_id"]
        != stable_diagnostic_identity("repo", second_callsite)["stable_diagnostic_id"]
    )
    assert (
        stable_diagnostic_identity("repo", first_fallback)["stable_diagnostic_id"]
        != stable_diagnostic_identity("repo", second_fallback)["stable_diagnostic_id"]
    )


def test_diagnostic_identity_fails_closed_without_a_semantic_subject() -> None:
    diagnostic = SemanticDiagnostic(
        diagnostic_id="diagnostic:unknown",
        repo_id="repo",
        index_version="index",
        diagnostic_kind="unresolved_call",
        message="cannot resolve call",
        severity="warning",
        frontend_name="python",
        properties={"receiver": "client"},
    )

    with pytest.raises(StableIdentityCollision, match="lacks fact_id"):
        stable_diagnostic_identity("repo", diagnostic)


def test_current_build_mismatch_is_detected_from_persisted_source_manifest(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    app = repo / "src" / "app.py"
    content = app.read_bytes()
    output = tmp_path / "output"
    GraphStoreWriter(output).write(
        IndexMetadata(
            index_version="indexed",
            repo_root=str(repo),
            source_roots=["src"],
            commit_sha=_git(repo, "rev-parse", "HEAD"),
        ),
        [
            FileRecord(
                path="src/app.py",
                abs_path=str(app),
                source_root="src",
                module="app",
                file_hash=hashlib.sha256(content).hexdigest(),
                line_count=2,
                file_size=len(content),
            )
        ],
        [],
        [],
        [],
    )
    build = capture_build_identity(repo, output, repo_id="repo")
    matching = capture_code_identity(
        repo,
        repo_id="repo",
        build_identity=build,
        source_paths=["src/app.py"],
    )
    assert_current_build_matches_code(build, matching)

    app.write_text("def f():\n    return 2\n", encoding="utf-8")
    changed = capture_code_identity(
        repo,
        repo_id="repo",
        build_identity=build,
        source_paths=["src/app.py"],
    )
    with pytest.raises(CurrentBuildCodeIdentityMismatch):
        assert_current_build_matches_code(build, changed)


def test_typescript_emit_artifacts_do_not_break_current_build_identity(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    source = repo / "src" / "util.ts"
    source.write_text("export const value: number = 1;\n", encoding="utf-8")
    _git(repo, "add", "src/util.ts")
    _git(repo, "commit", "-m", "add typescript source")
    content = source.read_bytes()
    output = tmp_path / "output"
    GraphStoreWriter(output).write(
        IndexMetadata(
            index_version="indexed",
            repo_root=str(repo),
            source_roots=["src"],
            commit_sha=_git(repo, "rev-parse", "HEAD"),
        ),
        [
            FileRecord(
                path="src/util.ts",
                abs_path=str(source),
                source_root="src",
                module="util",
                file_hash=hashlib.sha256(content).hexdigest(),
                line_count=1,
                file_size=len(content),
            )
        ],
        [],
        [],
        [],
    )
    build = capture_build_identity(repo, output, repo_id="repo")
    # The runtime emit carries its own compiler provenance: a declaration
    # companion alone no longer classifies a same-stem .js as emit.
    (repo / "src" / "util.js").write_text(
        "export const value = 1;\n//# sourceMappingURL=util.js.map\n",
        encoding="utf-8",
    )
    (repo / "src" / "util.d.ts").write_text(
        "export declare const value: number;\n", encoding="utf-8"
    )

    current = capture_code_identity(
        repo,
        repo_id="repo",
        build_identity=build,
        source_paths=["src/util.ts"],
    )
    assert_current_build_matches_code(build, current)

    (repo / "src" / "other.js").write_text(
        "export const other = 1;\n", encoding="utf-8"
    )
    changed = capture_code_identity(
        repo,
        repo_id="repo",
        build_identity=build,
        source_paths=["src/util.ts"],
    )
    with pytest.raises(CurrentBuildCodeIdentityMismatch):
        assert_current_build_matches_code(build, changed)


def test_current_build_mismatch_is_detected_after_a_new_commit(
    tmp_path: Path,
) -> None:
    """A clean new HEAD cannot reuse a graph built from the prior commit."""

    repo = _init_repo(tmp_path)
    app = repo / "src" / "app.py"
    content = app.read_bytes()
    output = tmp_path / "output"
    GraphStoreWriter(output).write(
        IndexMetadata(
            index_version="indexed",
            repo_root=str(repo),
            source_roots=["src"],
            commit_sha=_git(repo, "rev-parse", "HEAD"),
        ),
        [
            FileRecord(
                path="src/app.py",
                abs_path=str(app),
                source_root="src",
                module="app",
                file_hash=hashlib.sha256(content).hexdigest(),
                line_count=2,
                file_size=len(content),
            )
        ],
        [],
        [],
        [],
    )
    build = capture_build_identity(repo, output, repo_id="repo")

    (repo / "notes.txt").write_text("new committed state\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-m", "advance head without rebuilding")
    current = capture_code_identity(
        repo,
        repo_id="repo",
        build_identity=build,
        source_paths=["src/app.py"],
    )

    assert current.working_tree_clean
    assert current.file_manifest_digest == build.file_manifest_digest
    with pytest.raises(CurrentBuildCodeIdentityMismatch, match="Git HEAD"):
        assert_current_build_matches_code(build, current)
