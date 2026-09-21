from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from arcgraph.core.force_graph_export import build_focus_view, build_force_graph_export
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces import cli_visual as cli_visual_interface
from arcgraph.interfaces.cli import main
from arcgraph.interfaces.visual_server import create_visual_workbench_server
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def test_visual_server_rejects_non_loopback_host(tmp_path: Path) -> None:
    engine = _build_engine(tmp_path)

    with pytest.raises(RuntimeError, match="loopback"):
        create_visual_workbench_server(engine, host="0.0.0.0", port=0)


def test_visual_serve_cli_rejects_non_loopback_host_without_traceback(
    tmp_path: Path,
    capsys,
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "visual",
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "0",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "ArcGraph visual serve only accepts loopback hosts" in captured.err
    assert "Traceback" not in captured.err
    # The refusal is also machine-readable: stdout carries the error envelope,
    # which is what an agent parsing default JSON output reads.
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["command"] == "visual"
    assert "loopback" in payload["error"]["message"]


def test_visual_serve_cli_passes_open_flag_to_loopback_server(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    output_dir = tmp_path / "arcgraph"
    captured: dict[str, object] = {}
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    def fake_serve(*args, **kwargs):
        captured.update(kwargs)
        return {
            "schema": "ArcGraphVisualServe",
            "status": "stopped",
            "url": "http://127.0.0.1:8765/",
            "open": {"requested": True, "status": "opened"},
        }

    monkeypatch.setattr(cli_visual_interface, "serve_visual_workbench", fake_serve)

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "visual",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--open",
        ]
    )

    assert exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert captured["open_browser"] is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["open"]["status"] == "opened"


def test_visual_server_api_payloads_are_read_only_and_compact(tmp_path: Path) -> None:
    engine = _build_engine(tmp_path)
    server, base_url, thread = _start_server(engine)
    try:
        status = _get_json(base_url, "/api/status")
        assert status["schema"] == "ArcGraphWorkbenchStatus"

        graph = _get_json(base_url, "/api/graph")
        assert graph["focus_index"]["mode"] == "remote"
        assert graph["focus_index"]["nodes"] == []
        assert graph["focus_index"]["edges"] == []
        assert graph["audit_index"]["mode"] == "remote"
        assert graph["audit_index"]["nodes"] == {}
        assert graph["payload_budget"]["graph_data_bytes"] > 0

        search = _get_json(base_url, "/api/search?q=hello&limit=5")
        assert search["status"] == "available"
        assert any(item["id"] == "fn:pkg.api.hello" for item in search["items"])

        focus = _get_json(
            base_url,
            "/api/focus?target=pkg.api.hello&depth=1&direction=outgoing",
        )
        assert focus["status"] == "available"
        assert "fn:pkg.api.hello" in {node["id"] for node in focus["nodes"]}
        assert focus["counts"]["hidden_nodes"] >= 0

        node = _get_json(base_url, "/api/node?id=fn:pkg.api.hello")
        assert node["status"] == "available"
        assert not _contains_key(node, "properties")

        audit = _get_json(base_url, "/api/audit?id=fn:pkg.api.hello")
        assert audit["status"] == "available"
        assert audit["audit"]
        assert not _contains_key(audit, "properties")
        assert not _contains_key(audit, "snippet")
        assert not _contains_key(audit, "source_snippet")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_visual_server_focus_uses_static_focus_helper(tmp_path: Path) -> None:
    engine = _build_engine(tmp_path)
    static_graph = build_force_graph_export(engine)
    expected = build_focus_view(
        static_graph["focus_index"],
        targets=["pkg.api.hello"],
        depth=1,
        direction="outgoing",
    )
    server, base_url, thread = _start_server(engine)
    try:
        focus = _get_json(
            base_url,
            "/api/focus?target=pkg.api.hello&depth=1&direction=outgoing",
        )
        assert (
            focus["focus"]["visible_node_ids"] == expected["focus"]["visible_node_ids"]
        )
        assert (
            focus["focus"]["visible_edge_ids"] == expected["focus"]["visible_edge_ids"]
        )

        missing = _get_json(base_url, "/api/focus?target=pkg.missing.nope")
        assert missing["status"] == "partial"
        assert missing["warnings"][0]["kind"] == "focus_target_not_found"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _build_engine(tmp_path: Path) -> QueryEngine:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    return QueryEngine(output_dir)


def _start_server(
    engine: QueryEngine,
) -> tuple[object, str, threading.Thread]:
    server = create_visual_workbench_server(engine, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}", thread


def _get_json(base_url: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    return False
