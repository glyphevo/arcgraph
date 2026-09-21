"""Local read-only HTTP server for ArcGraph Explorer."""

from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse

from arcgraph.core.force_graph_export import (
    ForceGraphExportOptions,
    build_focus_view,
    build_force_graph_export,
)
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.utils import strip_source_snippets
from arcgraph.interfaces.workbench import (
    WORKBENCH_ASSET_PACKAGE,
    WORKBENCH_AUDIT_SCHEMA,
    WORKBENCH_AUDIT_VERSION,
    WorkbenchAuditOptions,
    attach_browser_open_result,
    attach_workbench_payload_budget,
    build_workbench_audit_index,
    build_workbench_status,
)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class VisualWorkbenchApp:
    """Request router and cached graph state for the local workbench server."""

    def __init__(
        self,
        query_engine: QueryEngine,
        *,
        graph_options: ForceGraphExportOptions | None = None,
    ) -> None:
        self.query_engine = query_engine
        self.graph_options = graph_options or ForceGraphExportOptions()
        self.graph_payload = build_force_graph_export(query_engine, self.graph_options)
        self.focus_index = self.graph_payload.get("focus_index", {})

    def status_payload(self) -> dict[str, Any]:
        return build_workbench_status(self.query_engine)

    def graph_overview_payload(self) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in self.graph_payload.items()
            if key not in {"focus_index", "audit_index", "payload_budget"}
        }
        payload["api"] = {"mode": "remote", "base_path": "/api"}
        payload["focus_index"] = {
            "mode": "remote",
            "nodes": [],
            "edges": [],
            "adjacency": {},
            "counts": dict(self.focus_index.get("counts", {})),
        }
        payload["audit_index"] = _remote_audit_index()
        attach_workbench_payload_budget(payload)
        return payload

    def search(self, query: str, *, limit: int) -> dict[str, Any]:
        payload = self.query_engine.search_nodes(query=query, limit=limit)
        focus_ids = {
            node.get("id")
            for node in self.focus_index.get("nodes", [])
            if isinstance(node, dict)
        }
        payload["items"] = [
            {**item, "focusable": item.get("id") in focus_ids}
            for item in payload.get("items", [])
        ]
        return payload

    def focus(
        self, targets: list[str], *, depth: int, direction: str
    ) -> dict[str, Any]:
        return build_focus_view(
            self.focus_index,
            targets=targets,
            depth=depth,
            direction=direction,
        )

    def node_detail(self, node_id: str) -> dict[str, Any]:
        payload = self.query_engine.node_detail(node_id)
        return _strip_raw_properties(strip_source_snippets(payload))

    def audit(self, node_id: str, *, detail_level: str) -> dict[str, Any]:
        node = self._focus_node(node_id)
        if node is None:
            return {
                "status": "unavailable",
                "audit": None,
                "warnings": [f"No focusable ArcGraph node found for {node_id!r}."],
            }
        audit_payload = build_workbench_audit_index(
            self.query_engine,
            {
                "focus": {"resolved_seed_ids": [node_id]},
                "focus_index": {"nodes": [node], "counts": {"nodes": 1}},
            },
            WorkbenchAuditOptions(
                include=True,
                max_nodes=0,
                detail_level=_audit_detail_level(detail_level),
            ),
        )
        audit = audit_payload.get("nodes", {}).get(node_id)
        return {
            "status": "available" if audit else "unavailable",
            "audit": audit,
            "warnings": audit_payload.get("warnings", []),
        }

    def asset(self, path: str) -> tuple[bytes, str] | None:
        clean_path = PurePosixPath(path.lstrip("/") or "index.html")
        if any(part in {"", ".", ".."} for part in clean_path.parts):
            return None
        asset_root = resources.files(WORKBENCH_ASSET_PACKAGE)
        asset = asset_root.joinpath(*clean_path.parts)
        if not asset.is_file():
            return None
        content_type = (
            mimetypes.guess_type(clean_path.name)[0] or "application/octet-stream"
        )
        return asset.read_bytes(), content_type

    def _focus_node(self, node_id: str) -> dict[str, Any] | None:
        for node in self.focus_index.get("nodes", []):
            if isinstance(node, dict) and node.get("id") == node_id:
                return node
        return None


def create_visual_workbench_server(
    query_engine: QueryEngine,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    graph_options: ForceGraphExportOptions | None = None,
) -> ThreadingHTTPServer:
    """Create a loopback-only HTTP server for ArcGraph Explorer."""

    if host not in LOOPBACK_HOSTS:
        raise RuntimeError(
            "ArcGraph visual serve only accepts loopback hosts: "
            "127.0.0.1, localhost, or ::1."
        )
    app = VisualWorkbenchApp(query_engine, graph_options=graph_options)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            _handle_get(self, app)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve_visual_workbench(
    query_engine: QueryEngine,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    graph_options: ForceGraphExportOptions | None = None,
    open_browser: bool = False,
) -> dict[str, Any]:
    server = create_visual_workbench_server(
        query_engine,
        host=host,
        port=port,
        graph_options=graph_options,
    )
    bound_host, bound_port = server.server_address[:2]
    url = f"http://{bound_host}:{bound_port}/"
    result: dict[str, Any] = {
        "schema": "ArcGraphVisualServe",
        "status": "stopped",
        "url": url,
        "warnings": [],
    }
    if open_browser:
        attach_browser_open_result(result, url)
    print(f"ArcGraph visual serve: {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return result


def _handle_get(handler: BaseHTTPRequestHandler, app: VisualWorkbenchApp) -> None:
    parsed = urlparse(handler.path)
    query = parse_qs(parsed.query)
    try:
        if parsed.path in {"/api/status", "/status_data.json"}:
            _write_json(handler, app.status_payload())
        elif parsed.path in {"/api/graph", "/graph_data.json"}:
            _write_json(handler, app.graph_overview_payload())
        elif parsed.path == "/api/search":
            _write_json(
                handler,
                app.search(
                    _query_value(query, "q"),
                    limit=_query_int(
                        query, "limit", default=20, minimum=1, maximum=100
                    ),
                ),
            )
        elif parsed.path == "/api/focus":
            targets = query.get("target", [])
            _write_json(
                handler,
                app.focus(
                    targets,
                    depth=_query_int(query, "depth", default=1, minimum=1, maximum=2),
                    direction=_query_value(query, "direction", default="both"),
                ),
            )
        elif parsed.path == "/api/node":
            _write_json(handler, app.node_detail(_query_value(query, "id")))
        elif parsed.path == "/api/audit":
            _write_json(
                handler,
                app.audit(
                    _query_value(query, "id"),
                    detail_level=_query_value(query, "detail_level", default="summary"),
                ),
            )
        elif parsed.path in {"/", "/index.html"}:
            _write_asset(handler, app, "index.html")
        elif parsed.path == "/favicon.ico":
            handler.send_response(HTTPStatus.NO_CONTENT)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        elif parsed.path.startswith("/vendor/"):
            _write_asset(handler, app, parsed.path.lstrip("/"))
        else:
            _write_json(
                handler,
                {"status": "not_found", "path": parsed.path},
                status=HTTPStatus.NOT_FOUND,
            )
    except (RuntimeError, ValueError, TypeError, KeyError) as exc:
        _write_json(
            handler,
            {"status": "error", "message": str(exc)},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def _write_asset(
    handler: BaseHTTPRequestHandler,
    app: VisualWorkbenchApp,
    path: str,
) -> None:
    asset = app.asset(path)
    if asset is None:
        _write_json(
            handler,
            {"status": "not_found", "path": path},
            status=HTTPStatus.NOT_FOUND,
        )
        return
    body, content_type = asset
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _write_json(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any],
    *,
    status: HTTPStatus = HTTPStatus.OK,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _query_value(query: dict[str, list[str]], key: str, *, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def _query_int(
    query: dict[str, list[str]],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(_query_value(query, key, default=str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _audit_detail_level(value: str) -> str:
    return value if value in {"summary", "standard"} else "summary"


def _remote_audit_index() -> dict[str, Any]:
    return {
        "schema": WORKBENCH_AUDIT_SCHEMA,
        "version": WORKBENCH_AUDIT_VERSION,
        "mode": "remote",
        "enabled": True,
        "detail_level": "summary",
        "max_nodes": 0,
        "counts": {
            "focus_nodes": 0,
            "nodes": 0,
            "truncated": 0,
            "initial_focus_nodes": 0,
            "estimated_bytes": 0,
        },
        "estimated_bytes": 0,
        "nodes": {},
        "warnings": [],
    }


def _strip_raw_properties(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_raw_properties(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_raw_properties(item)
            for key, item in value.items()
            if key not in {"properties", "snippet", "source_snippet"}
        }
    return value
