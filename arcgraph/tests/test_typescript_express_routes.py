from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from arcgraph.tests.typescript_acceptance_support import skip_or_fail
from arcgraph.change.identities import stable_node_identity
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine, _route_query_aliases
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import _is_entrypoint_target, _normalized_report_target
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer


def _build_express_files(
    tmp_path: Path,
    files: dict[str, str],
) -> tuple[GraphStoreReader, QueryEngine]:
    if shutil.which("node") is None:
        skip_or_fail("Node.js is required for TypeScript Express route tests.")

    for relative_path, source_text in files.items():
        source = tmp_path / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(source_text.strip() + "\n", encoding="utf-8")
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
    return reader, QueryEngine(output_dir)


def _build_express_repo(tmp_path: Path) -> tuple[GraphStoreReader, QueryEngine]:
    return _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";

export function healthHandler(): string {
  return "ok";
}

async function dispatchRequest(): Promise<void> {}

export function createApp() {
  const app = express();
  const ordinaryClient = { get: (_path: string) => undefined };
  ordinaryClient.get("/not-a-route");

  app.get("/", healthHandler);
  app.all("/gateway", async (_req, _res) => {
    await dispatchRequest();
  });
  return app;
}
""",
        },
    )


def test_express_static_routes_create_entrypoints_and_handler_flow(
    tmp_path: Path,
) -> None:
    reader, engine = _build_express_repo(tmp_path)
    nodes = {node.id: node for node in reader.read_nodes()}
    edges = {
        (edge.source, edge.kind, edge.target): edge for edge in reader.read_edges()
    }

    assert nodes["route:GET:/"].properties["framework"] == "express"
    assert nodes["route:ALL:/gateway"].properties["declared_method"] == "ALL"
    assert "route:GET:/not-a-route" not in nodes
    assert (
        "route:GET:/",
        "invokes",
        "fn:server.healthHandler",
    ) in edges
    assert (
        "fn:server.createApp",
        "calls",
        "route:GET:/",
    ) not in edges

    handler_nodes = [
        node
        for node in nodes.values()
        if node.properties.get("framework") == "express"
        and node.properties.get("route_handler") is True
    ]
    assert len(handler_nodes) == 1
    handler = handler_nodes[0]
    assert ("route:ALL:/gateway", "invokes", handler.id) in edges
    assert (handler.id, "calls", "fn:server.dispatchRequest") in edges

    post_route = engine.route("POST", "/gateway")
    assert post_route["status"] == "available"
    assert post_route["route"]["id"] == "route:ALL:/gateway"
    assert {target["id"] for target in post_route["targets"]} == {handler.id}

    flow = engine.entrypoint_flow("POST /gateway")
    assert flow["status"] == "available"
    assert [node["id"] for node in flow["entrypoints"]] == ["route:ALL:/gateway"]
    assert {
        (edge["source"], edge["kind"], edge["target"]) for edge in flow["edges"]
    } >= {
        ("route:ALL:/gateway", "invokes", handler.id),
        (handler.id, "calls", "fn:server.dispatchRequest"),
    }

    report_target = _normalized_report_target("POST /gateway")
    assert report_target == "route:POST:/gateway"
    assert engine.impact(report_target)["resolved_targets"] == ["route:ALL:/gateway"]
    assert _is_entrypoint_target("ANY /gateway") is True
    # Prose without a slash-prefixed path must not be rewritten into a route id.
    assert _is_entrypoint_target("ALL gateway") is False
    assert _normalized_report_target("ALL gateway") == "ALL gateway"
    assert _normalized_report_target("get all users") == "get all users"


def test_route_query_reports_ordered_middleware_and_mount_context(
    tmp_path: Path,
) -> None:
    _reader, engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
function authGuard(): void {}
function auditRequest(): void {}
function listUsers(): void {}
app.get("/users", authGuard, auditRequest, listUsers);
""",
        },
    )

    result = engine.route("GET", "/users")

    assert result["mount_context"] == {
        "router": "app",
        "mounted": True,
        "router_local_path": "/users",
        "resolved_path": "/users",
    }
    chain = result["middleware_chain"]
    assert [item["target"]["name"] for item in chain] == [
        "authGuard",
        "auditRequest",
        "listUsers",
    ]
    assert [item["role"] for item in chain] == [
        "pre_handler_middleware",
        "pre_handler_middleware",
        "terminal_handler",
    ]
    assert chain[0]["authorization_hint"]["matches_name_heuristic"] is True
    assert chain[1]["authorization_hint"]["matches_name_heuristic"] is False
    assert result["evidence_boundary"]["chain_order"] == "available"


def test_route_chain_is_partial_when_an_unenumerated_bundle_was_dropped(
    tmp_path: Path,
) -> None:
    _reader, engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
function first(): void {}
function hidden(): void {}
let mutable = [hidden];
app.get("/users", first, mutable);
""",
        },
    )

    result = engine.route("GET", "/users")

    assert [item["target"]["name"] for item in result["middleware_chain"]] == ["first"]
    assert result["middleware_chain"][0]["role"] == "unknown"
    assert result["middleware_chains"][0]["complete"] is False
    assert result["dynamic_registration_gaps"]
    assert result["evidence_boundary"]["chain_order"] == "partial"


def test_duplicate_route_registrations_are_reported_as_separate_chains(
    tmp_path: Path,
) -> None:
    _reader, engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
function auth(): void {}
function first(): void {}
function audit(): void {}
function second(): void {}
app.get("/users", auth, first);
app.get("/users", audit, second);
""",
        },
    )

    result = engine.route("GET", "/users")

    assert [
        [item["target"]["name"] for item in chain["handlers"]]
        for chain in result["middleware_chains"]
    ] == [["auth", "first"], ["audit", "second"]]
    assert [item["chain_index"] for item in result["middleware_chain"]] == [
        0,
        0,
        1,
        1,
    ]
    assert result["evidence_boundary"]["registration_count"] == 2


def test_report_route_fallback_matches_entrypoint_priority_and_handles_bad_ids(
    tmp_path: Path,
) -> None:
    reader, engine = _build_express_files(
        tmp_path,
        {
            "src/pages/api/gateway.ts": """
export default function gateway(_request: unknown, response: any): void {
  response.end("next");
}
""",
            "src/server.ts": """
import express from "express";
const app = express();
app.all("/api/gateway", (_request, response) => response.send("express"));
""",
        },
    )
    route_ids = {node.id for node in reader.read_nodes() if node.kind == "route"}
    assert {
        "route:ANY:/api/gateway",
        "route:ALL:/api/gateway",
    } <= route_ids

    flow = engine.entrypoint_flow("GET /api/gateway")
    literal_flow = engine.entrypoint_flow("route:GET:/api/gateway")
    impact = engine.impact("route:GET:/api/gateway")
    assert [node["id"] for node in flow["entrypoints"]] == [
        "route:ANY:/api/gateway",
        "route:ALL:/api/gateway",
    ]
    assert [node["id"] for node in literal_flow["entrypoints"]] == [
        "route:ANY:/api/gateway",
        "route:ALL:/api/gateway",
    ]
    assert impact["resolved_targets"] == [
        "route:ANY:/api/gateway",
        "route:ALL:/api/gateway",
    ]

    for malformed in ("route:GET", "route:", "route:BOGUS:/api/gateway"):
        unresolved = engine.impact(malformed)
        assert unresolved["status"] == "partial"
        assert unresolved["resolved_targets"] == []


def test_express_inline_handler_ids_ignore_unrelated_line_shifts(
    tmp_path: Path,
) -> None:
    source = """
import express from "express";
const app = express();
app.get("/items", (_req, res) => res.send("first"));
app.get("/items", (_req, res) => res.send("second"));
app.get("/items/detail", (_req, res) => res.send("third"));
app.get("/items-detail", (_req, res) => res.send("fourth"));
"""
    reader, _engine = _build_express_files(tmp_path, {"src/server.ts": source})

    def handler_ids(current: GraphStoreReader) -> set[str]:
        return {
            node.id
            for node in current.read_nodes()
            if node.properties.get("route_handler") is True
        }

    before = handler_ids(reader)
    assert len(before) == 4

    server = tmp_path / "src" / "server.ts"
    server.write_text("\n" + source.strip() + "\n", encoding="utf-8")
    output_dir = tmp_path / "arcgraph-output"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()

    assert handler_ids(GraphStoreReader.from_current(output_dir)) == before


def test_express_handler_identity_survives_inserting_a_different_registration(
    tmp_path: Path,
) -> None:
    def source(lines: list[str]) -> str:
        return "\n".join(
            [
                'import express from "express";',
                "const app = express();",
                *[
                    f'app.get("/users", (_req, res) => res.send("{label}"));'
                    for label in lines
                ],
            ]
        )

    reader, _engine = _build_express_files(
        tmp_path,
        {"src/server.ts": source(["A", "B"])},
    )

    def identities(current: GraphStoreReader) -> dict[str, str]:
        return {
            node.properties["handler_body_sha256"]: stable_node_identity("repo", node)[
                "stable_identity"
            ]
            for node in current.read_nodes()
            if node.properties.get("route_handler") is True
        }

    before = identities(reader)
    (tmp_path / "src" / "server.ts").write_text(
        source(["NEW", "A", "B"]) + "\n",
        encoding="utf-8",
    )
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=tmp_path / "arcgraph-output",
        source_roots=[SourceRoot("src")],
    ).build()
    after = identities(GraphStoreReader.from_current(tmp_path / "arcgraph-output"))

    assert set(before) < set(after)
    assert all(after[body_hash] == identity for body_hash, identity in before.items())


def test_express_handler_identity_binds_identical_bodies_to_their_closures(
    tmp_path: Path,
) -> None:
    def source(function_names: list[str]) -> str:
        declarations = [f"""function {name}(dep: string) {{
  app.get("/users", (_req, res) => res.send(dep));
}}""" for name in function_names]
        return "\n".join(
            [
                'import express from "express";',
                "const app = express();",
                *declarations,
            ]
        )

    reader, _engine = _build_express_files(
        tmp_path,
        {"src/server.ts": source(["alpha", "beta"])},
    )

    def identities(current: GraphStoreReader) -> dict[str, tuple[str, str]]:
        return {
            node.properties["handler_enclosing_subject"]: (
                node.id,
                stable_node_identity("repo", node)["stable_identity"],
            )
            for node in current.read_nodes()
            if node.properties.get("route_handler") is True
        }

    before = identities(reader)
    assert set(before) == {"fn:server.alpha", "fn:server.beta"}

    (tmp_path / "src" / "server.ts").write_text(
        source(["newlyAdded", "alpha", "beta"]) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph-output"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    after = identities(GraphStoreReader.from_current(output_dir))

    assert set(after) == {
        "fn:server.newlyAdded",
        "fn:server.alpha",
        "fn:server.beta",
    }
    assert after["fn:server.alpha"] == before["fn:server.alpha"]
    assert after["fn:server.beta"] == before["fn:server.beta"]
    assert after["fn:server.newlyAdded"] not in before.values()


def test_express_handler_identity_binds_object_methods_to_their_bindings(
    tmp_path: Path,
) -> None:
    def source(binding_names: list[str]) -> str:
        declarations = [f"""const {name} = {{
  configure(dep: string) {{
    app.get("/users", (_req, res) => res.send(dep));
  }}
}};""" for name in binding_names]
        return "\n".join(
            [
                'import express from "express";',
                "const app = express();",
                *declarations,
            ]
        )

    output_dir = tmp_path / "arcgraph-output"
    reader, _engine = _build_express_files(
        tmp_path,
        {"src/server.ts": source(["alpha", "beta"])},
    )

    def identities(current: GraphStoreReader) -> dict[str, str]:
        return {
            node.properties["handler_enclosing_subject"]: stable_node_identity(
                "repo", node
            )["stable_identity"]
            for node in current.read_nodes()
            if node.properties.get("route_handler") is True
        }

    before = identities(reader)
    assert set(before) == {
        "mod:server::binding:alpha::method:configure",
        "mod:server::binding:beta::method:configure",
    }

    (tmp_path / "src" / "server.ts").write_text(
        source(["newlyAdded", "alpha", "beta"]) + "\n",
        encoding="utf-8",
    )
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    after = identities(GraphStoreReader.from_current(output_dir))

    assert set(before) < set(after)
    assert all(after[subject] == identity for subject, identity in before.items())


def test_express_handler_identity_marks_indistinguishable_duplicates(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
app.get("/users", (_req, res) => res.send("same"));
app.get("/users", (_req, res) => res.send("same"));
""",
        },
    )
    handlers = [
        node
        for node in reader.read_nodes()
        if node.properties.get("route_handler") is True
    ]

    assert len(handlers) == 2
    assert all(
        node.properties.get("stable_handler_ambiguous") is True for node in handlers
    )


def test_express_handler_identity_preserves_literal_whitespace(
    tmp_path: Path,
) -> None:
    def source(labels: list[str]) -> str:
        return "\n".join(
            [
                'import express from "express";',
                "const app = express();",
                *[
                    f'app.get("/users", (_req, res) => res.send("{label}"));'
                    for label in labels
                ],
            ]
        )

    reader, _engine = _build_express_files(
        tmp_path,
        {"src/server.ts": source(["A   B"])},
    )
    old_handler = next(
        node
        for node in reader.read_nodes()
        if node.properties.get("route_handler") is True
    )

    (tmp_path / "src" / "server.ts").write_text(
        source(["A B", "A   B"]) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph-output"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    handlers = [
        node
        for node in GraphStoreReader.from_current(output_dir).read_nodes()
        if node.properties.get("route_handler") is True
    ]

    assert len(handlers) == 2
    assert old_handler.id in {node.id for node in handlers}
    assert len({node.properties["handler_body_sha256"] for node in handlers}) == 2


def test_express_namespace_import_discovers_apps_routers_and_mounts(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import * as express from "express";

const app = express();
const router = express.Router();
function healthHandler(): string { return "ok"; }
function listUsers(): string[] { return []; }
app.get("/health", healthHandler);
router.get("/users", listUsers);
app.use("/api", router);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    invokes = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "invokes"
    }

    assert "route:GET:/health" in nodes
    assert "route:GET:/api/users" in nodes
    assert ("route:GET:/health", "fn:server.healthHandler") in invokes
    assert ("route:GET:/api/users", "fn:server.listUsers") in invokes
    assert not any(
        warning.kind == "typescript_express_route_unmounted"
        for warning in reader.read_warnings()
    )


def test_express_import_equals_and_export_equals_mount_cross_file_router(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/routes.ts": """
import express = require("express");
const router = express.Router();
export function listUsers(): string[] { return []; }
router.get("/users", listUsers);
export = router;
""",
            "src/app.ts": """
import express = require("express");
import router = require("./routes");
const app = express();
app.use("/api", router);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    edges = {
        (edge.source, edge.kind, edge.target): edge for edge in reader.read_edges()
    }

    assert nodes["route:GET:/api/users"].properties["route_mounted"] is True
    assert (
        "route:GET:/api/users",
        "invokes",
        "fn:routes.listUsers",
    ) in edges
    assert ("mod:app", "imports", "mod:routes") in edges
    assert ("mod:app", "imports", "ext:express") in edges
    assert (
        edges[("mod:app", "imports", "ext:express")].properties["import_kind"]
        == "commonjs"
    )


def test_express_mounts_are_order_independent_and_exact_methods_beat_wildcards(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/items-router.ts": """
import { Router } from "express";

const router = Router();
export function listItems(): string[] { return []; }
router.get("/list", listItems);
export default router;
""",
            "src/app.ts": """
import express from "express";
import itemsRouter from "./items-router.js";

const beforeClient = { get: (_path: string) => undefined };
export function callMountedRoute(): void {
  beforeClient.get("/api/list");
}

const app = express();
function wildcardHandler(): void {}
function exactHandler(): void {}
app.all("/wild", wildcardHandler);
app.get("/wild", exactHandler);
app.use("/api", itemsRouter);
app.use("/alternate", itemsRouter);

const exactClient = { get: (_path: string) => undefined };
export function callExactRoute(): void {
  exactClient.get("/wild");
}
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    call_edges = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }

    assert "route:GET:/api/list" in nodes
    assert nodes["route:GET:/api/list"].properties["route_mounted"] is True
    assert "route:GET:/alternate/list" in nodes
    assert (
        "route:GET:/alternate/list",
        "fn:items-router.listItems",
    ) in {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "invokes"
    }
    assert (
        "fn:app.callMountedRoute",
        "route:GET:/api/list",
    ) in call_edges
    assert ("fn:app.callExactRoute", "route:GET:/wild") in call_edges
    assert ("fn:app.callExactRoute", "route:ALL:/wild") not in call_edges


def test_express_router_mount_without_prefix_uses_application_root(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/users.ts": """
import { Router } from "express";
const router = Router();
export function getUser(): string { return "user"; }
router.get("/users/:id", getUser);
export default router;
""",
            "src/app.ts": """
import express from "express";
import router from "./users.js";
const app = express();
app.use(router);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}

    assert nodes["route:GET:/users/:id"].properties["route_mounted"] is True


def test_express_pathless_middleware_chain_keeps_router_mounts(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
const users = express.Router();
const teams = express.Router();
const api = express.Router();
function requireAuth(_req: any, _res: any, next: any) { next(); }
function tail(_req: any, _res: any, next: any) { next(); }
function cors() { return (_req: any, _res: any, next: any) => next(); }
app.use(requireAuth, users);
app.use(cors(), teams);
app.use(api, tail);
users.get("/users", (_req, res) => res.send("users"));
teams.get("/teams", (_req, res) => res.send("teams"));
api.get("/api", (_req, res) => res.send("api"));
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}

    assert nodes["route:GET:/users"].properties["route_mounted"] is True
    assert nodes["route:GET:/teams"].properties["route_mounted"] is True
    assert nodes["route:GET:/api"].properties["route_mounted"] is True


def test_express_pathless_middleware_chain_resolves_imported_routers(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/users.ts": """
import { Router } from "express";
const router = Router();
router.get("/users", (_req, res) => res.send("users"));
export default router;
""",
            "src/teams.ts": """
const express = require("express");
const router = express.Router();
router.get("/teams", (_req, res) => res.send("teams"));
module.exports = router;
""",
            "src/app.ts": """
import express from "express";
import users from "./users.js";
const teams = require("./teams.js");
const app = express();
function requireAuth(_req: any, _res: any, next: any) { next(); }
app.use(requireAuth, users);
app.use(requireAuth, teams);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}

    assert nodes["route:GET:/users"].properties["route_mounted"] is True
    assert nodes["route:GET:/teams"].properties["route_mounted"] is True


def test_express_pathless_middleware_chain_resolves_router_first_mount(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/users.ts": """
import { Router } from "express";
const router = Router();
router.get("/users", (_req, res) => res.send("users"));
export default router;
""",
            "src/app.ts": """
import express from "express";
import users from "./users.js";
const app = express();
function tail(_req: any, _res: any, next: any) { next(); }
app.use(users, tail);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}

    assert nodes["route:GET:/users"].properties["route_mounted"] is True
    assert not any(
        warning.kind == "typescript_express_route_unmounted"
        for warning in reader.read_warnings()
    )


def test_express_pathless_mount_does_not_reuse_shadowed_router_name(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
const api = express.Router();
function requireAuth(_req: any, _res: any, next: any) { next(); }
function configure(): void {
  const api = { notARouter: true };
  app.use(requireAuth, api);
}
api.get("/users", (_req, res) => res.send("users"));
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}

    assert "route:GET:/users" not in nodes
    assert nodes["route:GET:/users@server"].properties["route_mounted"] is False
    assert any(
        warning.kind == "typescript_express_route_unmounted"
        for warning in reader.read_warnings()
    )


def test_express_mount_expansion_is_bounded_and_disclosed(tmp_path: Path) -> None:
    depth = 10
    declarations = ["const app = express();"] + [
        f"const r{index} = express.Router();" for index in range(depth + 1)
    ]
    mounts = ["app.use('/a', r0);", "app.use('/b', r0);"]
    for index in range(depth):
        mounts.extend(
            [
                f"r{index}.use('/x{index}', r{index + 1});",
                f"r{index}.use('/y{index}', r{index + 1});",
            ]
        )
    source = "\n".join(
        [
            'import express from "express";',
            *declarations,
            *mounts,
            f"r{depth}.get('/leaf', (_req, res) => res.send('ok'));",
        ]
    )

    reader, _engine = _build_express_files(
        tmp_path,
        {"src/server.ts": source},
    )
    routes = [node for node in reader.read_nodes() if node.kind == "route"]

    assert len(routes) == 256
    warnings = [
        warning
        for warning in reader.read_warnings()
        if warning.kind == "typescript_express_mount_paths_capped"
    ]
    assert len(warnings) == 1


def test_express_mount_cap_prefers_app_reachable_prefixes(
    tmp_path: Path,
) -> None:
    """Unresolved fan-out must not crowd the sole app-mounted entrypoint out."""
    parents = 256
    declarations = [
        "const app = express();",
        "const leaf = express.Router();",
        *[f"const p{index} = express.Router();" for index in range(parents)],
    ]
    mounts = [
        *[f"p{index}.use('/u{index}', leaf);" for index in range(parents)],
        "app.use('/live', leaf);",
    ]
    source = "\n".join(
        [
            'import express from "express";',
            *declarations,
            *mounts,
            "leaf.get('/items', (_req, res) => res.send('ok'));",
        ]
    )

    reader, engine = _build_express_files(
        tmp_path,
        {"src/server.ts": source},
    )
    routes = [node for node in reader.read_nodes() if node.kind == "route"]
    mounted = [node for node in routes if node.properties.get("route_mounted") is True]

    assert "route:GET:/live/items" in {node.id for node in routes}
    assert any(
        node.id == "route:GET:/live/items" and node.properties["route_mounted"] is True
        for node in routes
    )
    assert len(mounted) >= 1
    assert len(routes) == 256
    assert any(
        warning.kind == "typescript_express_mount_paths_capped"
        for warning in reader.read_warnings()
    )

    flow = engine.entrypoint_flow("GET /live/items")
    assert flow["status"] == "available"
    assert [node["id"] for node in flow["entrypoints"]] == ["route:GET:/live/items"]


def test_express_mount_traversal_budget_prefers_indirect_app_reachable_prefix(
    tmp_path: Path,
) -> None:
    """Unresolved parents must not exhaust traversal before a nested app path."""
    parents = 4095
    declarations = [
        "const app = express();",
        "const leaf = express.Router();",
        "const mid = express.Router();",
        *[f"const a{index:04d} = express.Router();" for index in range(parents)],
    ]
    mounts = [
        *[f"a{index:04d}.use(leaf);" for index in range(parents)],
        "mid.use(leaf);",
        "app.use('/live', mid);",
    ]
    source = "\n".join(
        [
            'import express from "express";',
            *declarations,
            *mounts,
            "leaf.get('/items', (_req, res) => res.send('ok'));",
        ]
    )

    reader, engine = _build_express_files(
        tmp_path,
        {"src/server.ts": source},
    )
    routes = [node for node in reader.read_nodes() if node.kind == "route"]

    assert any(
        node.id == "route:GET:/live/items"
        and node.properties.get("route_mounted") is True
        for node in routes
    )
    assert len(routes) <= 256
    assert any(
        warning.kind == "typescript_express_mount_paths_capped"
        for warning in reader.read_warnings()
    )
    flow = engine.entrypoint_flow("GET /live/items")
    assert flow["status"] == "available"
    assert [node["id"] for node in flow["entrypoints"]] == ["route:GET:/live/items"]


def test_express_dynamic_mount_prefix_does_not_fall_back_to_root(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/users.ts": """
import { Router } from "express";
const router = Router();
export function getUser(): string { return "user"; }
router.get("/users/:id", getUser);
export default router;
""",
            "src/app.ts": """
import express from "express";
import router from "./users.js";
const app = express();
const mountPath = "/api";
app.use(mountPath, router);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}

    route_id = "route:GET:/users/:id@users"
    assert nodes[route_id].properties["route_mounted"] is False
    assert "route:GET:/users/:id" not in nodes
    assert "route:GET:/api/users/:id" not in nodes
    assert any(
        warning.kind == "typescript_express_route_unmounted"
        for warning in reader.read_warnings()
    )


def test_express_dynamic_call_mount_prefix_does_not_fall_back_to_root(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
const router = express.Router();
function getPrefix(): string { return "/api"; }
router.get("/users", (_req, res) => res.send("users"));
app.use(getPrefix(), router);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}

    route_id = "route:GET:/users@server"
    assert nodes[route_id].properties["route_mounted"] is False
    assert "route:GET:/users" not in nodes
    assert "route:GET:/api/users" not in nodes
    assert any(
        warning.kind == "typescript_express_route_unmounted"
        for warning in reader.read_warnings()
    )


def test_express_commonjs_default_export_mounts_across_files(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/users.ts": """
const express = require("express");
const router = express.Router();
export function getUser(): string { return "user"; }
router.get("/users/:id", getUser);
module.exports = router;
""",
            "src/app.ts": """
const express = require("express");
const router = require("./users.js");
const app = express();
app.use("/api", router);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}

    assert nodes["route:GET:/api/users/:id"].properties["route_mounted"] is True


def test_express_receiver_resolution_respects_shadowing_and_settings_getter(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function realHandler(): void {}
app.get("/real", realHandler);
app.get("env");

export function parameterShadow(app: { get: Function }): void {
  app.get("/parameter-shadow", realHandler);
}

export function blockShadow(): void {
  const app = { get: (_path: string, _handler: Function) => undefined };
  app.get("/block-shadow", realHandler);
}
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    warnings = reader.read_warnings()

    assert "route:GET:/real" in nodes
    assert "route:GET:/parameter-shadow" not in nodes
    assert "route:GET:/block-shadow" not in nodes
    assert not any(
        warning.kind == "typescript_express_route_dynamic"
        and "app.get" in warning.message
        for warning in warnings
    )


def test_unmounted_router_routes_do_not_match_client_urls(tmp_path: Path) -> None:
    reader, engine = _build_express_files(
        tmp_path,
        {
            "src/orphan.ts": """
import { Router } from "express";

const router = Router();
function localHandler(): void {}
router.get("/local", localHandler);
""",
            "src/client.ts": """
const client = { get: (_path: string) => undefined };
export function callLocalPath(): void {
  client.get("/local");
}
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    calls = [edge for edge in reader.read_edges() if edge.kind == "calls"]
    warnings = reader.read_warnings()

    route_id = "route:GET:/local@orphan"
    assert nodes[route_id].properties["route_mounted"] is False
    assert not any(edge.target == route_id for edge in calls)
    warning = next(
        warning
        for warning in warnings
        if warning.kind == "typescript_express_route_unmounted"
    )
    assert warning.frontend_name == "typescript-static"
    assert engine.entrypoint_flow("GET /local")["status"] == "unavailable"
    assert engine.entrypoint_flow(route_id)["status"] == "unavailable"


@pytest.mark.parametrize(
    "source_text",
    [
        """
import { type Request } from "express";
export function requestPath(request: Request): string {
  return request.path;
}
""",
        """
import type express = require("express");
export function requestPath(request: express.Request): string {
  return request.path;
}
""",
    ],
)
def test_type_only_express_import_does_not_disable_reindex(
    tmp_path: Path,
    source_text: str,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {"src/types.ts": source_text},
    )
    import_edge = next(edge for edge in reader.read_edges() if edge.kind == "imports")
    node_ids = {node.id for node in reader.read_nodes()}
    assert import_edge.target == "ext:express"
    assert import_edge.properties["import_kind"] == "type"
    assert "ext:express" in node_ids

    source = tmp_path / "src" / "types.ts"
    source.write_text(
        source.read_text(encoding="utf-8").replace("request.path", "request.url"),
        encoding="utf-8",
    )

    result = ArcGraphReindexer(
        repo_root=tmp_path,
        output_dir=tmp_path / "arcgraph-output",
        source_roots=[SourceRoot("src")],
    ).reindex_changed()

    assert result["status"] == "reindexed"


def test_runtime_express_import_dominates_earlier_type_import(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import type expressTypes = require("express/types");
import expressRuntime = require("express");
const app = expressRuntime();
export function health(): string { return "ok"; }
app.get("/health", health);
""",
        },
    )
    import_edges = [
        edge
        for edge in reader.read_edges()
        if edge.kind == "imports"
        and edge.source == "mod:server"
        and edge.target == "ext:express"
    ]
    assert len(import_edges) == 1
    assert import_edges[0].properties["import_kind"] == "commonjs"
    assert import_edges[0].properties["specifier"] == "express"

    source = tmp_path / "src" / "server.ts"
    source.write_text(
        source.read_text(encoding="utf-8").replace('return "ok";', 'return "ready";'),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Express mount context"):
        ArcGraphReindexer(
            repo_root=tmp_path,
            output_dir=tmp_path / "arcgraph-output",
            source_roots=[SourceRoot("src")],
        ).reindex_changed()


def test_express_repo_requires_full_build_before_typescript_reindex(
    tmp_path: Path,
) -> None:
    _reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/router.ts": """
import { Router } from "express";
const router = Router();
export function listItems(): string[] { return []; }
router.get("/list", listItems);
export default router;
""",
            "src/app.ts": """
import express from "express";
import router from "./router.js";
const app = express();
app.use("/api", router);
""",
        },
    )
    output_dir = tmp_path / "arcgraph-output"
    pointer_before = json.loads(
        (output_dir / "current.json").read_text(encoding="utf-8")
    )
    router_path = tmp_path / "src" / "router.ts"
    router_path.write_text(
        router_path.read_text(encoding="utf-8").replace(
            "return [];", 'return ["new"];'
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Express mount context"):
        ArcGraphReindexer(
            repo_root=tmp_path,
            output_dir=output_dir,
            source_roots=[SourceRoot("src")],
        ).reindex_changed()

    assert (
        json.loads((output_dir / "current.json").read_text(encoding="utf-8"))
        == pointer_before
    )


def test_express_repo_vue_only_change_also_requires_full_build(
    tmp_path: Path,
) -> None:
    _reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";
const app = express();
function health(): string { return "ok"; }
app.get("/health", health);
""",
            "src/Widget.vue": """
<template><main>before</main></template>
<script setup lang="ts">const label = "before";</script>
""",
        },
    )
    output_dir = tmp_path / "arcgraph-output"
    pointer_before = (output_dir / "current.json").read_text(encoding="utf-8")
    widget = tmp_path / "src" / "Widget.vue"
    widget.write_text(
        widget.read_text(encoding="utf-8").replace("before", "after"),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Express mount context"):
        ArcGraphReindexer(
            repo_root=tmp_path,
            output_dir=output_dir,
            source_roots=[SourceRoot("src")],
        ).reindex_changed()

    assert (output_dir / "current.json").read_text(encoding="utf-8") == pointer_before


def test_express_wildcard_query_bridges_any_and_all(tmp_path: Path) -> None:
    # ANY (Django, Next.js) and ALL (Express app.all) are two stored
    # spellings of the same any-method claim.
    assert _route_query_aliases("ANY /health") == [
        "route:ANY:/health",
        "route:ALL:/health",
    ]
    assert _route_query_aliases("route:ALL:/health") == [
        "route:ALL:/health",
        "route:ANY:/health",
    ]

    _reader, engine = _build_express_repo(tmp_path)
    result = engine.route("ANY", "/gateway")
    assert result["route"] is not None
    assert result["route"]["id"] == "route:ALL:/gateway"
    assert engine.entrypoint_flow("ANY /gateway")["status"] == "available"


def test_express_trailing_slash_mount_prefix_composes_single_slash(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/level2.ts": """
import { Router } from "express";

const router = Router();
export function listUsers(): string[] { return []; }
router.get("/users", listUsers);
export default router;
""",
            "src/level1.ts": """
import { Router } from "express";
import level2 from "./level2.js";

const router = Router();
router.use("/v1", level2);
export default router;
""",
            "src/app.ts": """
import express from "express";
import level1 from "./level1.js";

const app = express();
app.use("/api/", level1);
""",
        },
    )
    node_ids = {node.id for node in reader.read_nodes()}
    # Express serves GET /api/v1/users for a trailing-slash mount prefix
    # (verified against Express 5.2.1); the published path must match it.
    assert "route:GET:/api/v1/users" in node_ids
    assert not any(
        "//" in node_id for node_id in node_ids if node_id.startswith("route:")
    )


def test_express_empty_string_mount_is_root_mount(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/users.ts": """
import { Router } from "express";

const router = Router();
export function listUsers(): string[] { return []; }
router.get("/x", listUsers);
export default router;
""",
            "src/app.ts": """
import express from "express";
import users from "./users.js";

const app = express();
app.use("", users);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    # Express treats app.use("", router) as a root mount (verified against
    # Express 5.2.1).
    assert "route:GET:/x" in nodes
    assert nodes["route:GET:/x"].properties["route_mounted"] is True


def test_express_typed_parameter_receiver_is_not_an_http_client_call(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

export function realHandler(): string { return "app"; }

const app = express();
app.get("/users", realHandler);
export default app;
""",
            "src/routes.ts": """
import type { Express } from "express";

export function otherHandler(): string { return "routes"; }

export function register(app: Express): void {
  app.get("/users", otherHandler);
}
""",
        },
    )
    calls = [edge for edge in reader.read_edges() if edge.kind == "calls"]
    # The parameter-receiver registration must not be misread as an outbound
    # HTTP client call onto the same-path route registered in app.ts.
    assert not any(
        edge.source == "fn:routes.register" and edge.target == "route:GET:/users"
        for edge in calls
    )
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_deep_expression_chain_does_not_overflow_the_extractor(
    tmp_path: Path,
) -> None:
    # A long left-associative chain is routine in bundled or minified files;
    # one such file must not take down the whole extractor process.
    chain = " + ".join(["1"] * 30_000)
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": f"""
import express from "express";

export function bigChain(): number {{
  return {chain};
}}

const app = express();
export function handler(): string {{ return "ok"; }}
app.get("/deep", handler);
""",
        },
    )
    node_ids = {node.id for node in reader.read_nodes()}
    assert "route:GET:/deep" in node_ids
    assert "fn:app.bigChain" in node_ids


def test_local_router_lookalike_type_keeps_client_heuristics(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

export function realHandler(): string { return "app"; }

const app = express();
app.get("/users", realHandler);
export default app;
""",
            "src/client.ts": """
interface Router {
  get(path: string, callback?: () => void): void;
}

export function callUsers(client: Router): void {
  client.get("/users");
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # No trailing handler argument makes this call client-shaped, so the
    # ordinary HTTP-client heuristic keeps matching.
    assert ("fn:client.callUsers", "route:GET:/users") in calls
    assert not any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_express_settings_getter_on_typed_receiver_is_not_a_registration(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/settings.ts": """
import type { Express } from "express";

export function readEnvironment(app: Express): string {
  return app.get("env");
}
""",
        },
    )
    # Express's one-argument settings getter registers nothing; it must not
    # produce an unindexed-routes warning.
    assert not any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_local_type_alias_to_express_keeps_registration_guard(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

export function realHandler(): string { return "app"; }

const app = express();
app.get("/users", realHandler);
export default app;
""",
            "src/routes.ts": """
import type { Express } from "express";

type App = Express;

export function otherHandler(): string { return "routes"; }

export function register(app: App): void {
  app.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # A local alias of an express-imported type is still Express provenance:
    # the aliased registration must not become a client call edge.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_lookalike_package_application_type_keeps_client_heuristics(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "node_modules/expressive-client/package.json": """
{"name": "expressive-client", "version": "1.0.0", "types": "index.d.ts"}
""",
            "node_modules/expressive-client/index.d.ts": """
export interface Application {
  get(path: string, callback?: () => void): void;
}
""",
            "src/app.ts": """
import express from "express";

export function realHandler(): string { return "app"; }

const app = express();
app.get("/users", realHandler);
export default app;
""",
            "src/client.ts": """
import { Application } from "expressive-client";

export function callUsers(client: Application): void {
  client.get("/users");
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # A client-shaped call (no trailing handler) keeps the heuristic edge
    # no matter what package the receiver's type came from.
    assert ("fn:client.callUsers", "route:GET:/users") in calls
    assert not any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


_EXPRESS_APP_REGISTERING_USERS = """
import express from "express";

export function realHandler(): string { return "app"; }

const app = express();
app.get("/users", realHandler);
export default app;
"""


def test_express_typed_property_receiver_is_not_an_http_client_call(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/registrar.ts": """
import type { Express } from "express";

export function otherHandler(): string { return "routes"; }

export class Registrar {
  private app: Express;

  constructor(app: Express) {
    this.app = app;
  }

  register(): void {
    this.app.get("/users", otherHandler);
  }
}
""",
        },
    )
    # A registration through an Express-typed property is still a
    # registration, not an outbound HTTP client call.
    assert not any(
        edge.target == "route:GET:/users" and "registrar" in edge.source.lower()
        for edge in reader.read_edges()
        if edge.kind == "calls"
    )
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_extended_express_types_keep_registration_guard(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

type App = Express & { feature: string };

interface Extended extends Express {
  other: string;
}

export function otherHandler(): string { return "routes"; }

export function register(app: App): void {
  app.get("/users", otherHandler);
}

export function registerExtended(app: Extended): void {
  app.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # Intersection members and interface heritage keep Express provenance.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert ("fn:routes.registerExtended", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_type_parameter_shadowing_express_keeps_client_heuristics(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/client.ts": """
import type { Express } from "express";

interface Client {
  get(path: string, callback?: () => void): void;
}

export function forwardApp(app: Express): Express {
  return app;
}

export function callUsers<Express extends Client>(client: Express): void {
  client.get("/users");
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # A client-shaped call (no trailing handler) keeps the heuristic edge
    # regardless of how the receiver's type annotation resolves.
    assert ("fn:client.callUsers", "route:GET:/users") in calls
    assert not any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_cross_file_type_alias_to_express_keeps_registration_guard(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/types.ts": """
import type { Express } from "express";

export type App = Express;
""",
            "src/routes.ts": """
import type { App } from "./types.js";

export function otherHandler(): string { return "routes"; }

export function register(app: App): void {
  app.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # An alias declared in another source file still carries Express
    # provenance through the import alias chain.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_inferred_extended_express_type_keeps_registration_guard(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

interface Extended extends Express {
  other: string;
}

declare function getApp(): Extended;

export function otherHandler(): string { return "routes"; }

export function register(): void {
  const app = getApp();
  app.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # The receiver's type is inferred, so provenance must come from the
    # checker's type symbol and its heritage chain, not from an annotation.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_function_local_type_alias_shadowing_express_keeps_client_heuristics(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/client.ts": """
import type { Express } from "express";

interface Client {
  get(path: string, callback?: () => void): void;
}

export function forwardApp(app: Express): Express {
  return app;
}

export function callUsers(): void {
  type Express = Client;
  const client: Express = { get: (_path, _callback) => undefined };
  client.get("/users");
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # A client-shaped call (no trailing handler) keeps the heuristic edge
    # regardless of how the receiver's type annotation resolves.
    assert ("fn:client.callUsers", "route:GET:/users") in calls
    assert not any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_optional_express_receiver_keeps_registration_guard(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

export function otherHandler(): string { return "routes"; }

export function register(app: Express | undefined): void {
  app?.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # `Express | undefined` is Express whenever the optional call executes.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_mixed_union_receiver_keeps_client_heuristics(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/client.ts": """
import type { Express } from "express";

interface Client {
  get(path: string, callback?: () => void): void;
}

export function forwardApp(app: Express): Express {
  return app;
}

export function callUsers(receiver: Express | Client): void {
  receiver.get("/users");
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # A client-shaped call (no trailing handler) keeps the heuristic edge
    # regardless of the receiver's union type.
    assert ("fn:client.callUsers", "route:GET:/users") in calls
    assert not any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_all_express_union_receiver_keeps_registration_guard(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

type App = Express;

interface Extended extends Express {
  other: string;
}

export function otherHandler(): string { return "routes"; }

export function register(app: App | Extended | undefined): void {
  app?.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # Every substantive union member traces to Express -- including two
    # members whose provenance paths share the same import symbol.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_barrel_reexported_express_type_keeps_registration_guard(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/barrel.ts": """
export type { Express as App } from "express";
""",
            "src/routes.ts": """
import type { App } from "./barrel.js";

export function otherHandler(): string { return "routes"; }

export function register(app: App): void {
  app.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # A direct type re-export through a barrel still carries Express
    # provenance even with no express type package installed.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_inferred_intersection_express_type_keeps_registration_guard(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

declare function getApp(): Express & { feature: string };

export function otherHandler(): string { return "routes"; }

export function register(): void {
  const app = getApp();
  app.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # The inferred receiver's provenance comes from the callee's declared
    # intersection return type.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_generic_constraint_express_receiver_keeps_registration_guard(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

export function otherHandler(): string { return "routes"; }

export function register<T extends Express>(app: T): void {
  app.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # Every instantiation of `T extends Express` is assignable to Express.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_receiver_guard_covers_head_options_and_all_methods(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

export function otherHandler(): string { return "routes"; }

export function register(app: Express): void {
  app.head("/health", otherHandler);
  app.options("/health", otherHandler);
  app.all("/everything", otherHandler);
}
""",
        },
    )
    # Registrations through non-client verbs are equally unindexable and must
    # surface the same warning instead of vanishing silently.
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )
    assert not any(
        edge.source == "fn:routes.register"
        for edge in reader.read_edges()
        if edge.kind == "calls"
    )


def test_overload_selection_decides_express_provenance(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/mixed.ts": """
import type { Express } from "express";

interface Client {
  get(path: string, callback?: () => void): void;
}

declare function getReceiver(kind: "app"): Express;
declare function getReceiver(kind: "client"): Client;

export function otherHandler(): string { return "routes"; }

export function callUsers(): void {
  const client = getReceiver("client");
  client.get("/users");
}

export function register(): void {
  const app = getReceiver("app");
  app.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # Call shape splits the two: callUsers is client-shaped (no trailing
    # handler), register passes a handler and is registration-shaped.
    assert ("fn:mixed.callUsers", "route:GET:/users") in calls
    assert ("fn:mixed.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_conditional_union_of_express_intersections_keeps_guard(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

declare const appA: Express & { a: string };
declare const appB: Express & { b: string };

export function otherHandler(): string { return "routes"; }

export function register(flag: boolean): void {
  const app = flag ? appA : appB;
  app.get("/users", otherHandler);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # Both conditional branches trace to Express through the same shared
    # Express type; sibling branches must not read each other's visited sets
    # as refutation.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_local_dynamic_route_is_not_reported_as_external_receiver(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";

const app = express();
const dynamicPath = ["/a", "/b"][0];
function handler(): void {}
app.get(dynamicPath, handler);
""",
        },
    )
    warnings = reader.read_warnings()
    # The locally created receiver's dynamic registration already carries the
    # dynamic-route warning; the external-receiver warning must not fire for
    # it as well.
    assert any(
        warning.kind == "typescript_express_route_dynamic" for warning in warnings
    )
    assert not any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in warnings
    )


def test_element_access_express_registration_is_disclosed_as_unresolved(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";

const app = express();
function handler(): void {}
app["get"]("/users", handler);
""",
        },
    )

    assert "route:GET:/users" not in {node.id for node in reader.read_nodes()}
    warnings = [
        warning
        for warning in reader.read_warnings()
        if warning.kind == "typescript_route_registration_unresolved"
    ]
    assert len(warnings) == 1
    assert "element-access Express registrations" in warnings[0].message


def test_element_access_client_call_does_not_fabricate_route_edge(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/client.ts": """
declare const client: Record<string, (path: string) => void>;

export function callUsers(): void {
  client["get"]("/users");
}
""",
        },
    )

    assert ("fn:client.callUsers", "route:GET:/users") not in {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }


def test_uppercase_element_key_is_not_express_registration(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";

const app = express();
function handler(): void {}
app["GET"]("/users", handler);
""",
        },
    )

    assert not any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_computed_element_express_registration_is_disclosed(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";

const app = express();
const verb = Math.random() ? "get" : "post";
function handler(): void {}
app[verb]("/users", handler);
""",
        },
    )

    warnings = [
        warning
        for warning in reader.read_warnings()
        if warning.kind == "typescript_route_registration_unresolved"
    ]
    assert len(warnings) == 1
    assert "computed element-access Express registrations" in warnings[0].message


def test_config_object_second_argument_stays_client_shaped(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/client.ts": """
interface RequestOptions {
  headers: Record<string, string>;
}

interface Client {
  get(path: string, options?: RequestOptions): void;
}

declare const client: Client;

export function callUsers(): void {
  client.get("/users", { headers: {} });
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # A config object is not a handler: the call stays client-shaped.
    assert ("fn:client.callUsers", "route:GET:/users") in calls
    assert not any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_callback_style_client_is_suppressed_as_registration_shaped(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/legacy.ts": """
interface LegacyClient {
  get(path: string, callback: () => void): void;
}

declare const legacy: LegacyClient;

export function callUsers(): void {
  legacy.get("/users", () => undefined);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # The documented bounded loss of shape-based judgment: a callback-style
    # client call carries a trailing callable and is indistinguishable from a
    # registration, so its heuristic edge is suppressed by design.
    assert ("fn:legacy.callUsers", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_handler_array_marks_registration_shape(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

export function first(): void {}
export function second(): void {}

export function register(app: Express, handlers: (() => void)[]): void {
  app.get("/users", [first, second]);
  app.post("/users", handlers);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # Express accepts handler arrays -- literal or typed -- and both mark the
    # registration shape.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_array_and_regex_paths_keep_unresolved_warning(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/routes.ts": """
import type { Express } from "express";

export function otherHandler(): string { return "routes"; }

export function registerMany(app: Express): void {
  app.get(["/users", "/admins"], otherHandler);
}

export function registerPattern(app: Express): void {
  app.get(/^\\/reports$/, otherHandler);
}
""",
        },
    )
    # Array and regex paths are legal Express registrations; unresolved
    # receivers must stay visible for them too.
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )
    assert not any(
        edge.source in {"fn:routes.registerMany", "fn:routes.registerPattern"}
        for edge in reader.read_edges()
        if edge.kind == "calls"
    )


def test_tuple_handler_type_marks_registration_shape(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

export function first(): void {}
export function second(): void {}

const handlers: [() => void, () => void] = [first, second];

export function register(app: Express): void {
  app.get("/users", handlers);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # A tuple of callables is a handler bundle just like an array of them.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_mixed_string_and_regex_path_array_keeps_unresolved_warning(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/routes.ts": """
import type { Express } from "express";

export function otherHandler(): string { return "routes"; }

export function register(app: Express): void {
  app.get(["/users", /^\\/admins$/], otherHandler);
}
""",
        },
    )
    # Express allows path arrays mixing strings and regexes; the unresolved
    # registration must stay visible.
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_mixed_array_second_argument_stays_client_shaped(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/client.ts": """
interface Client {
  get(path: string, payload: unknown): void;
}

declare const client: Client;

function transform(): void {}

export function callUsers(): void {
  client.get("/users", [transform, { id: 1 }]);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # `[Function, object]` is not a legal Express handler array: one callable
    # member must not flip a client payload into a registration.
    assert ("fn:client.callUsers", "route:GET:/users") in calls
    assert not any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_generic_constrained_handler_array_marks_registration_shape(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": _EXPRESS_APP_REGISTERING_USERS,
            "src/routes.ts": """
import type { Express } from "express";

export function register<T extends (() => void)[]>(
  app: Express,
  handlers: T,
): void {
  app.get("/users", handlers);
}
""",
        },
    )
    calls = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "calls"
    }
    # `T extends Handler[]` proves every instantiation is a handler bundle.
    assert ("fn:routes.register", "route:GET:/users") not in calls
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )


def test_same_named_routers_in_different_scopes_keep_distinct_mounts(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express, { Router } from "express";

const app = express();

export function adminRoutes(): void {
  const r = Router();
  r.get("/users", (_req, _res) => undefined);
  app.use("/admin", r);
}

export function apiRoutes(): void {
  const r = Router();
  r.get("/items", (_req, _res) => undefined);
  app.use("/api", r);
}
""",
        },
    )
    route_ids = {node.id for node in reader.read_nodes() if node.kind == "route"}
    # Same-named routers in different scopes must not exchange mount
    # prefixes: the cross products are phantom routes.
    assert "route:GET:/admin/users" in route_ids
    assert "route:GET:/api/items" in route_ids
    assert "route:GET:/api/users" not in route_ids
    assert "route:GET:/admin/items" not in route_ids


def test_new_expression_router_factory_is_recognized(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
const router = new express.Router();
function handler(): void {}
router.get("/x", handler);
app.use("/api", router);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    # `new express.Router()` creates the same receiver as the call form.
    assert "route:GET:/api/x" in nodes
    assert nodes["route:GET:/api/x"].properties["route_mounted"] is True


def test_empty_string_route_path_is_static_mount_root(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express, { Router } from "express";

const app = express();
const router = Router();
function handler(): void {}
router.get("", handler);
app.use("/api", router);
""",
        },
    )
    route_ids = {node.id for node in reader.read_nodes() if node.kind == "route"}
    warnings = reader.read_warnings()
    # `router.get("", h)` is the static idiom for the bare mount root, not a
    # dynamic path.
    assert "route:GET:/api" in route_ids
    assert not any(
        warning.kind == "typescript_express_route_dynamic" for warning in warnings
    )


def test_route_chain_registrations_are_disclosed(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function listUsers(): void {}
function createUsers(): void {}
app.route("/users").get(listUsers).post(createUsers);
""",
        },
    )
    # Chained route(...) registrations are not modeled yet; they must be
    # disclosed instead of silently missing.
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )
    assert not any(
        edge.kind == "calls" and edge.target.startswith("route:")
        for edge in reader.read_edges()
    )


def test_aliased_named_export_router_mounts_across_files(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/routes.ts": """
import { Router } from "express";

const r = Router();
export function listUsers(): string[] { return []; }
r.get("/x", listUsers);
export { r as apiRouter };
""",
            "src/app.ts": """
import express from "express";
import { apiRouter } from "./routes.js";

const app = express();
app.use("/api", apiRouter);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    # The export alias must resolve back to the declaring module's local
    # name or the mount silently misses the registrations.
    assert "route:GET:/api/x" in nodes
    assert nodes["route:GET:/api/x"].properties["route_mounted"] is True


def test_named_default_export_router_mounts_across_files(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/routes.ts": """
import { Router } from "express";

const r = Router();
export function listUsers(): string[] { return []; }
r.get("/x", listUsers);
export { r as default };
""",
            "src/app.ts": """
import express from "express";
import routes from "./routes.js";

const app = express();
app.use("/api", routes);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    # `export { r as default }` is the named-export spelling of
    # `export default r`.
    assert "route:GET:/api/x" in nodes
    assert nodes["route:GET:/api/x"].properties["route_mounted"] is True


def test_route_command_refuses_router_local_ids(tmp_path: Path) -> None:
    _reader, engine = _build_express_files(
        tmp_path,
        {
            "src/orphan.ts": """
import { Router } from "express";

const router = Router();
function localHandler(): void {}
router.get("/local", localHandler);
""",
        },
    )
    # Target resolution and entrypoint lookup refuse router-local paths;
    # route() must not be the one resolver that disagrees.
    result = engine.route("GET", "/local@orphan")
    assert result["route"] is None
    assert result["status"] == "unavailable"


def test_for_scoped_router_does_not_collide_with_module_router(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express, { Router } from "express";

const app = express();
function topHandler(): void {}
function loopHandler(): void {}

const r = Router();
r.get("/top", topHandler);
app.use("/top-mount", r);

export function looped(): void {
  for (let r = Router(); ; ) {
    r.get("/loop", loopHandler);
    app.use("/loop-mount", r);
    break;
  }
}
""",
        },
    )
    route_ids = {node.id for node in reader.read_nodes() if node.kind == "route"}
    # `let r` in a for-head is block-scoped even though its nearest Block is
    # the SourceFile; it must not exchange mounts with the module-level `r`.
    assert "route:GET:/top-mount/top" in route_ids
    assert "route:GET:/loop-mount/loop" in route_ids
    assert "route:GET:/top-mount/loop" not in route_ids
    assert "route:GET:/loop-mount/top" not in route_ids


def test_var_in_block_router_keeps_module_key_for_export(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/routes.ts": """
import { Router } from "express";

export function listUsers(): string[] { return []; }

{
  var r = Router();
  r.get("/x", listUsers);
}

export { r };
""",
            "src/app.ts": """
import express from "express";
import { r } from "./routes.js";

const app = express();
app.use("/api", r);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    # `var` hoists to module scope: the block position must not leak into the
    # key or the importer's plain module key misses the registrations.
    assert "route:GET:/api/x" in nodes
    assert nodes["route:GET:/api/x"].properties["route_mounted"] is True


def test_terminal_route_chain_with_middleware_is_disclosed(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function auth(): void {}
function listUsers(): void {}
app.route("/users").get(auth, listUsers);
""",
        },
    )
    # A terminal chain also satisfies the generic registration shape; the
    # chain check must win so the disclosure warning still fires.
    assert any(
        warning.kind == "typescript_route_registration_unresolved"
        for warning in reader.read_warnings()
    )
    assert not any(
        edge.kind == "calls" and edge.target.startswith("route:")
        for edge in reader.read_edges()
    )


def test_inline_exported_router_mounts_across_files(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/routes.ts": """
import { Router } from "express";

export function listUsers(): string[] { return []; }

export const r = Router();
r.get("/x", listUsers);
""",
            "src/app.ts": """
import express from "express";
import { r } from "./routes.js";

const app = express();
app.use("/api", r);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    # `export const r` lives in the module's exports table, not locals; it is
    # still a module-level binding and must keep the plain key.
    assert "route:GET:/api/x" in nodes
    assert nodes["route:GET:/api/x"].properties["route_mounted"] is True


def test_barrel_reexported_router_mounts_across_files(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/routes.ts": """
import { Router } from "express";

export function listUsers(): string[] { return []; }

export const r = Router();
r.get("/x", listUsers);
""",
            "src/barrel.ts": """
export { r } from "./routes.js";
""",
            "src/app.ts": """
import express from "express";
import { r } from "./barrel.js";

const app = express();
app.use("/api", r);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    # A named value re-export through a barrel resolves through the alias
    # chain to the declaring binding.
    assert "route:GET:/api/x" in nodes
    assert nodes["route:GET:/api/x"].properties["route_mounted"] is True


def test_star_reexported_router_mounts_across_files(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/routes.ts": """
import { Router } from "express";

export function listUsers(): string[] { return []; }

export const r = Router();
r.get("/x", listUsers);
""",
            "src/barrel.ts": """
export * from "./routes.js";
""",
            "src/app.ts": """
import express from "express";
import { r } from "./barrel.js";

const app = express();
app.use("/api", r);
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    # `export *` forwarding is part of the same closed export-form family.
    assert "route:GET:/api/x" in nodes
    assert nodes["route:GET:/api/x"].properties["route_mounted"] is True


def test_extensionless_import_prefers_ts_over_tsx_variant(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/foo.ts": """
export function fromTs(): number { return 1; }
""",
            "src/foo.tsx": """
export function Component(): null { return null; }
""",
            "src/user.ts": """
import { fromTs } from "./foo";

export function use(): number { return fromTs(); }
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    ts_module = next(
        node.id
        for node in nodes.values()
        if node.kind == "module" and node.path == "src/foo.ts"
    )
    tsx_module = next(
        node.id
        for node in nodes.values()
        if node.kind == "module" and node.path == "src/foo.tsx"
    )
    import_targets = {
        edge.target
        for edge in reader.read_edges()
        if edge.kind == "imports" and edge.source.endswith("user")
    }
    # TypeScript resolves `./foo` with `.ts` before `.tsx`; the imports edge
    # must agree with what the checker resolves calls against.
    assert ts_module in import_targets
    assert tsx_module not in import_targets


def test_handler_array_members_get_invokes_edges(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function first(): void {}
function second(): void {}
function trailing(): void {}
app.get("/array", [first, second], trailing);
app.get("/inline", [(_req, _res) => undefined]);
""",
        },
    )
    invokes = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "invokes"
    }
    # Every statically enumerable member of a handler array links like a bare
    # handler, including inline callbacks (which get synthetic handler nodes).
    assert ("route:GET:/array", "fn:app.first") in invokes
    assert ("route:GET:/array", "fn:app.second") in invokes
    assert ("route:GET:/array", "fn:app.trailing") in invokes
    assert any(source == "route:GET:/inline" for source, _target in invokes)


def test_non_enumerable_handler_spread_is_disclosed(tmp_path: Path) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
declare const extras: (() => void)[];
function first(): void {}
app.get("/spread", first, ...extras);
""",
        },
    )
    invokes = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "invokes"
    }
    assert ("route:GET:/spread", "fn:app.first") in invokes
    assert any(
        warning.kind == "typescript_express_handlers_unresolved"
        for warning in reader.read_warnings()
    )


def test_directory_index_import_prefers_ts_over_tsx_variant(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/pkg/index.ts": """
export function fromTs(): number { return 1; }
""",
            "src/pkg/index.tsx": """
export function Component(): null { return null; }
""",
            "src/user.ts": """
import { fromTs } from "./pkg";

export function use(): number { return fromTs(); }
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    ts_module = next(
        node.id
        for node in nodes.values()
        if node.kind == "module" and node.path == "src/pkg/index.ts"
    )
    tsx_module = next(
        node.id
        for node in nodes.values()
        if node.kind == "module" and node.path == "src/pkg/index.tsx"
    )
    import_targets = {
        edge.target
        for edge in reader.read_edges()
        if edge.kind == "imports" and edge.source.endswith("user")
    }
    # Directory index resolution follows the same `.ts`-before-`.tsx`
    # priority as file stems; the single-valued directory alias is a last
    # resort only.
    assert ts_module in import_targets
    assert tsx_module not in import_targets


def test_wrapped_and_named_handler_arrays_link_or_disclose(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function first(): void {}
function second(): void {}
function third(): void {}
const bundle = [second, third];
let mutable = [first];
app.get("/wrapped", ([first, second]));
app.get("/named", bundle);
app.get("/mutable", mutable, first);
""",
        },
    )
    invokes = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "invokes"
    }
    # Parenthesized array literals and const array initializers enumerate
    # like bare arrays.
    assert ("route:GET:/wrapped", "fn:app.first") in invokes
    assert ("route:GET:/wrapped", "fn:app.second") in invokes
    assert ("route:GET:/named", "fn:app.second") in invokes
    assert ("route:GET:/named", "fn:app.third") in invokes
    # A mutable binding is not safely enumerable: its bundle is disclosed
    # while the enumerable trailing handler still links.
    assert ("route:GET:/mutable", "fn:app.first") in invokes
    assert any(
        warning.kind == "typescript_express_handlers_unresolved"
        for warning in reader.read_warnings()
    )


def test_const_callable_identifier_keeps_declared_identity(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function dep(): void {}
const handler = (): void => {
  dep();
};
app.get("/h", handler);
""",
        },
    )
    invokes = [
        edge
        for edge in reader.read_edges()
        if edge.kind == "invokes" and edge.source == "route:GET:/h"
    ]
    # The const callable resolves to its declared function; unwrapping must
    # not fork it into a route-specific synthetic copy.
    assert [edge.target for edge in invokes] == ["fn:app.handler"]
    assert any(
        edge.source == "fn:app.handler" and edge.target == "fn:app.dep"
        for edge in reader.read_edges()
        if edge.kind == "calls"
    )


def test_shared_bundle_callback_keeps_one_identity_across_routes(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function dep(): void {}
const shared = [
  (): void => {
    dep();
  },
];
app.get("/a", shared);
app.get("/b", shared);
""",
        },
    )
    invokes = {
        edge.source: edge.target
        for edge in reader.read_edges()
        if edge.kind == "invokes"
    }
    # Both routes invoke the SAME synthetic handler node, whose internal
    # calls are attributed exactly once.
    assert invokes["route:GET:/a"] == invokes["route:GET:/b"]
    shared_id = invokes["route:GET:/a"]
    assert any(
        edge.source == shared_id and edge.target == "fn:app.dep"
        for edge in reader.read_edges()
        if edge.kind == "calls"
    )


def test_mutated_const_bundle_is_disclosed_not_enumerated(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function first(): void {}
function second(): void {}
function third(): void {}
const arr = [first];
arr.push(second);
app.get("/m", arr, third);
""",
        },
    )
    invokes = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "invokes"
    }
    # A bundle mutated before registration cannot be enumerated safely: it is
    # disclosed while enumerable trailing handlers still link.
    assert ("route:GET:/m", "fn:app.third") in invokes
    assert ("route:GET:/m", "fn:app.first") not in invokes
    assert ("route:GET:/m", "fn:app.second") not in invokes
    assert any(
        warning.kind == "typescript_express_handlers_unresolved"
        for warning in reader.read_warnings()
    )


def test_all_transparent_wrappers_enumerate_handler_bundles(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function first(): void {}
function second(): void {}
app.get("/sat", [first, second] satisfies Array<() => void>);
app.get("/angle", <Array<() => void>>[first]);
app.get("/bang", ([second])!);
""",
        },
    )
    invokes = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "invokes"
    }
    assert ("route:GET:/sat", "fn:app.first") in invokes
    assert ("route:GET:/sat", "fn:app.second") in invokes
    assert ("route:GET:/angle", "fn:app.first") in invokes
    assert ("route:GET:/bang", "fn:app.second") in invokes
    assert not any(
        warning.kind == "typescript_express_handlers_unresolved"
        for warning in reader.read_warnings()
    )


def test_explicit_ts_extension_import_prefers_exact_file(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/foo.ts": """
export function fromFoo(): number { return 1; }
""",
            "src/foo.ts.ts": """
export function fromDouble(): number { return 2; }
""",
            "src/user.ts": """
import { fromFoo } from "./foo.ts";

export function use(): number { return fromFoo(); }
""",
        },
    )
    nodes = {node.id: node for node in reader.read_nodes()}
    exact_module = next(
        node.id
        for node in nodes.values()
        if node.kind == "module" and node.path == "src/foo.ts"
    )
    double_module = next(
        node.id
        for node in nodes.values()
        if node.kind == "module" and node.path == "src/foo.ts.ts"
    )
    import_targets = {
        edge.target
        for edge in reader.read_edges()
        if edge.kind == "imports" and edge.source.endswith("user")
    }
    # `import './foo.ts'` names an exact file; it must not resolve to the
    # derived candidate foo.ts.ts.
    assert exact_module in import_targets
    assert double_module not in import_targets


def test_shared_callback_identity_survives_route_insertion(
    tmp_path: Path,
) -> None:
    base_source = """
import express from "express";

const app = express();
function dep(): void {}
const shared = [
  (): void => {
    dep();
  },
];
__EXTRA__app.get("/a", shared);
app.get("/b", shared);
"""

    def shared_handler_identity(variant: str, extra: str) -> tuple[str, str]:
        reader, _engine = _build_express_files(
            tmp_path / variant,
            {"src/app.ts": base_source.replace("__EXTRA__", extra)},
        )
        invokes = {
            edge.source: edge.target
            for edge in reader.read_edges()
            if edge.kind == "invokes"
        }
        assert invokes["route:GET:/a"] == invokes["route:GET:/b"]
        handler_id = invokes["route:GET:/a"]
        handler = next(node for node in reader.read_nodes() if node.id == handler_id)
        assert (
            handler.properties["identity_profile"]
            == "typescript_express_shared_handler_v1"
        )
        return handler_id, stable_node_identity("repo", handler)["stable_identity"]

    without_insert = shared_handler_identity("without", "")
    with_insert = shared_handler_identity(
        "with", 'app.get("/inserted", (_req, _res) => undefined);\n'
    )
    # The shared callback's identity derives from its declaration alone:
    # inserting an unrelated earlier registration must not shift it.
    assert without_insert == with_insert


def test_transparent_wrappers_around_const_bundle_preserve_enumeration(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function first(): void {}
const bundle = [first];
app.get("/paren", (bundle));
app.get("/as", bundle as Array<() => void>);
app.get("/sat", bundle satisfies Array<() => void>);
app.get("/angle", <Array<() => void>>bundle);
app.get("/bang", bundle!);
app.get("/spread", ...(bundle as Array<() => void>));
""",
        },
    )
    invokes = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "invokes"
    }

    for path in ("paren", "as", "sat", "angle", "bang", "spread"):
        assert (f"route:GET:/{path}", "fn:app.first") in invokes
    assert not any(
        warning.kind == "typescript_express_handlers_unresolved"
        for warning in reader.read_warnings()
    )


def test_verb_named_call_on_untracked_receiver_disqualifies_bundle(
    tmp_path: Path,
) -> None:
    reader, _engine = _build_express_files(
        tmp_path,
        {
            "src/app.ts": """
import express from "express";

const app = express();
function first(): void {}
const bundle = [first];
const sink = { get: (_path: string, _handlers: unknown) => undefined };
sink.get("/elsewhere", bundle);
app.get("/m", bundle);
""",
        },
    )
    invokes = {
        (edge.source, edge.target)
        for edge in reader.read_edges()
        if edge.kind == "invokes"
    }
    # `sink.get` may mutate the bundle before registration; a verb-shaped
    # name on an untracked receiver must disqualify enumeration.
    assert ("route:GET:/m", "fn:app.first") not in invokes
    assert any(
        warning.kind == "typescript_express_handlers_unresolved"
        for warning in reader.read_warnings()
    )


def test_middleware_reused_across_registrations_stays_in_every_chain(
    tmp_path: Path,
) -> None:
    """Edge identity cannot carry the registration, so a handler serving the
    same route twice collapses to one edge. Every registration's facts must
    survive on it, or the later chain silently loses the handler."""

    _reader, engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
function auth(): void {}
function first(): void {}
function second(): void {}
app.get("/users", auth, first);
app.get("/users", auth, second);
""",
        },
    )

    result = engine.route("GET", "/users")

    assert [
        [item["target"]["name"] for item in chain["handlers"]]
        for chain in result["middleware_chains"]
    ] == [["auth", "first"], ["auth", "second"]]
    assert result["evidence_boundary"]["registration_count"] == 2
    assert result["evidence_boundary"]["chain_order"] == "available"


def test_reused_terminal_handler_keeps_both_registrations(tmp_path: Path) -> None:
    _reader, engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
function authA(): void {}
function authB(): void {}
function shared(): void {}
app.get("/users", authA, shared);
app.get("/users", authB, shared);
""",
        },
    )

    result = engine.route("GET", "/users")

    assert [
        [item["target"]["name"] for item in chain["handlers"]]
        for chain in result["middleware_chains"]
    ] == [["authA", "shared"], ["authB", "shared"]]
    assert result["evidence_boundary"]["chain_order"] == "available"


def test_identical_duplicate_registrations_are_both_reported(tmp_path: Path) -> None:
    """Two byte-identical registrations differ only by source position; both
    must appear, or the route under-reports how many times it is registered."""

    _reader, engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
function auth(): void {}
function handler(): void {}
app.get("/users", auth, handler);
app.get("/users", auth, handler);
""",
        },
    )

    result = engine.route("GET", "/users")

    assert result["evidence_boundary"]["registration_count"] == 2
    assert [
        [item["target"]["name"] for item in chain["handlers"]]
        for chain in result["middleware_chains"]
    ] == [["auth", "handler"], ["auth", "handler"]]


def test_handler_repeated_within_one_registration_keeps_every_position(
    tmp_path: Path,
) -> None:
    """A registration identifies a chain, not a position: the same handler
    listed twice in one registration occupies two positions."""

    _reader, engine = _build_express_files(
        tmp_path,
        {
            "src/server.ts": """
import express from "express";
const app = express();
function auth(): void {}
function handler(): void {}
app.get("/users", auth, auth, handler);
""",
        },
    )

    result = engine.route("GET", "/users")

    chain = result["middleware_chains"][0]
    assert [
        (item["position"], item["target"]["name"]) for item in chain["handlers"]
    ] == [
        (0, "auth"),
        (1, "auth"),
        (2, "handler"),
    ]
    assert result["evidence_boundary"]["chain_order"] == "available"


def test_route_registration_gaps_disclose_what_they_omit(tmp_path: Path) -> None:
    """A bounded gap list is a sample, and a caller cannot tell a sample from
    the whole set without being told."""

    from arcgraph.core import query_engine as query_engine_module
    from arcgraph.core.graph_store import GraphStoreWriter
    from arcgraph.core.query_engine import QueryEngine
    from arcgraph.core.schemas import BuildWarning, IndexMetadata, Node

    output_dir = tmp_path / "arcgraph"
    warnings = [
        BuildWarning(
            kind="typescript_route_registration_unresolved",
            message=f"dynamic registration {index}",
        )
        for index in range(30)
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="gap-disclosure",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[
            Node(
                id="route:GET:/users",
                kind="route",
                name="GET /users",
                qualname="GET /users",
                path="src/server.ts",
            )
        ],
        edges=[],
        warnings=warnings,
    )

    payload = QueryEngine(output_dir).route("GET", "/users")

    assert len(payload["dynamic_registration_gaps"]) == (
        query_engine_module._ROUTE_REGISTRATION_GAP_LIMIT
    )
    assert payload["dynamic_registration_gap_summary"] == {
        "total": 30,
        "returned": 25,
        "omitted": 5,
    }
    assert payload["evidence_boundary"]["chain_order"] == "partial"
