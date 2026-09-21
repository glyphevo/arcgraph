from __future__ import annotations


import json
from importlib import resources
from pathlib import Path

from arcgraph.interfaces import workbench as workbench_interface
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import main
from arcgraph.interfaces.workbench import (
    AUDIT_DEFAULT_MAX_NODES,
    attach_workbench_payload_budget,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def test_visual_workbench_cli_writes_local_assets_and_status(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    workbench_dir = tmp_path / "workbench"
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
            "workbench",
            "--output-dir",
            str(workbench_dir),
            "--min-symbol-degree",
            "1",
        ]
    )

    assert exit_code == 0
    index_html = workbench_dir / "index.html"
    graph_json = workbench_dir / "graph_data.json"
    status_json = workbench_dir / "status_data.json"
    vendor_d3 = workbench_dir / "vendor" / "d3.v7.min.js"
    assert index_html.exists()
    assert graph_json.exists()
    assert status_json.exists()
    assert vendor_d3.exists()

    html = index_html.read_text(encoding="utf-8")
    assert "https://" not in html
    assert "fonts.googleapis" not in html
    assert "d3js.org" not in html
    assert "vendor/d3.v7.min.js" in html

    graph = json.loads(graph_json.read_text(encoding="utf-8"))
    assert graph["schema"] == "ArcGraphForceGraph"
    assert graph["meta"]["visual_contract"]["default_view"] == "system_map"
    assert all("semantic_tier" in node for node in graph["module_graph"]["nodes"])
    assert graph["audit_index"]["enabled"] is True
    assert graph["audit_index"]["max_nodes"] == AUDIT_DEFAULT_MAX_NODES
    assert graph["audit_index"]["counts"]["nodes"] > 0
    sample_audit = next(iter(graph["audit_index"]["nodes"].values()))
    assert "provenance" in sample_audit
    assert "impact" in sample_audit
    assert "tests" in sample_audit
    assert "unresolved" in sample_audit
    assert "recommended_next_reads" in sample_audit
    _assert_no_forbidden_audit_payload(sample_audit)
    assert graph["payload_budget"]["graph_data_bytes"] > 0
    assert graph["payload_budget"]["sections"]["focus_index"] > 0
    assert graph["payload_budget"]["sections"]["audit_index"] > 0
    assert graph["payload_budget"]["largest_section"]["name"]
    assert graph["payload_budget"]["warnings"] == []

    status = json.loads(status_json.read_text(encoding="utf-8"))
    assert status["schema"] == "ArcGraphWorkbenchStatus"
    assert status["visual_contract"]["default_view"] == "system_map"
    assert "freshness" in status["summary"]
    assert "ci" in status["summary"]
    assert "semantic_quality" in status["summary"]


def test_visual_workbench_open_uses_browser_without_changing_exit_code(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    output_dir = tmp_path / "arcgraph"
    workbench_dir = tmp_path / "workbench"
    opened: list[str] = []
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    monkeypatch.setattr(
        workbench_interface.webbrowser,
        "open",
        lambda target: opened.append(target) or True,
    )

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "visual",
            "workbench",
            "--output-dir",
            str(workbench_dir),
            "--open",
            "--no-audit-index",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["open"]["status"] == "opened"
    assert payload["open"]["target"].startswith("file:")
    assert opened == [payload["open"]["target"]]


def test_visual_workbench_open_failure_is_warning_not_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    output_dir = tmp_path / "arcgraph"
    workbench_dir = tmp_path / "workbench"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    monkeypatch.setattr(workbench_interface.webbrowser, "open", lambda target: False)

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "visual",
            "workbench",
            "--output-dir",
            str(workbench_dir),
            "--open",
            "--no-audit-index",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["open"]["status"] == "warn"
    assert payload["warnings"][0]["kind"] == "browser_open_failed"


def test_visual_force_focus_index_survives_overview_symbol_caps(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    graph_path = tmp_path / "focus.json"
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
            "force",
            "--output",
            str(graph_path),
            "--initial-focus",
            "pkg.api.hello",
            "--focus-depth",
            "1",
            "--focus-direction",
            "outgoing",
            "--max-symbol-nodes",
            "1",
            "--min-symbol-degree",
            "999",
            "--audit-max-nodes",
            "0",
        ]
    )

    assert exit_code == 0
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert graph["symbol_graph"]["nodes"] == []
    assert graph["focus_index"]["counts"]["nodes"] > 0
    assert graph["focus"]["resolved_seed_ids"] == ["fn:pkg.api.hello"]
    assert "fn:pkg.api.hello" in graph["focus"]["visible_node_ids"]
    assert "fn:pkg.service.build_message" in graph["focus"]["visible_node_ids"]
    assert graph["focus"]["direction"] == "outgoing"
    assert graph["focus"]["depth"] == 1
    assert graph["audit_index"]["counts"]["nodes"] == 1
    assert "fn:pkg.api.hello" in graph["audit_index"]["nodes"]
    assert graph["audit_index"]["counts"]["truncated"] > 0
    assert graph["payload_budget"]["graph_data_bytes"] > 0


def test_visual_force_focus_missing_target_warns_without_failing(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    graph_path = tmp_path / "missing-focus.json"
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
            "force",
            "--output",
            str(graph_path),
            "--initial-focus",
            "pkg.missing.nope",
        ]
    )

    assert exit_code == 0
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert graph["focus"]["mode"] == "overview"
    assert graph["focus"]["resolved_seed_ids"] == []
    assert graph["focus"]["unresolved"][0]["reason"] == "not_found"
    assert graph["warnings"][-1]["kind"] == "focus_target_not_found"


def test_visual_workbench_can_disable_static_audit_index(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    workbench_dir = tmp_path / "workbench"
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
            "workbench",
            "--output-dir",
            str(workbench_dir),
            "--no-audit-index",
        ]
    )

    assert exit_code == 0
    graph = json.loads((workbench_dir / "graph_data.json").read_text(encoding="utf-8"))
    assert graph["focus_index"]["counts"]["nodes"] > 0
    assert graph["audit_index"]["enabled"] is False
    assert graph["audit_index"]["nodes"] == {}
    assert graph["payload_budget"]["sections"]["audit_index"] > 0


def test_payload_budget_warnings_are_non_fatal_with_synthetic_payload() -> None:
    payload = {
        "schema": "ArcGraphForceGraph",
        "version": 1,
        "status": "available",
        "module_graph": {"nodes": [], "edges": []},
        "symbol_graph": {"nodes": [], "edges": []},
        "focus_index": {
            "nodes": [{"id": f"fn:pkg.n{i}", "label": "x" * 20} for i in range(6)],
            "edges": [],
        },
        "audit_index": {
            "enabled": True,
            "nodes": {"fn:pkg.n0": {"recommended_next_reads": [{"path": "x" * 40}]}},
        },
        "meta": {"filters": {}},
    }

    attach_workbench_payload_budget(
        payload,
        thresholds={"graph_data": 100, "focus_index": 100, "audit_index": 50},
    )

    warnings = payload["payload_budget"]["warnings"]
    assert {warning["section"] for warning in warnings} == {
        "graph_data",
        "focus_index",
        "audit_index",
    }
    assert all(warning["kind"] == "payload_budget_exceeded" for warning in warnings)


def test_workbench_template_uses_backend_contract_not_name_regexes() -> None:
    template = (
        resources.files("arcgraph.assets.workbench")
        .joinpath("index.html")
        .read_text(encoding="utf-8")
    )

    assert "semantic_tier" in template
    assert "visualContract()" in template
    assert "/route/.test" not in template
    assert "subPkg.endsWith('/api')" not in template
    assert "speculative" not in template
    assert "fonts.googleapis" not in template
    assert "d3js.org" not in template
    assert "focus_index" in template
    assert "btn-clear-focus" in template
    assert "enterFocus" in template
    assert "audit_index" in template
    assert "audit-container" in template
    assert "renderAuditDrawer" in template
    assert "payload-warning-strip" in template
    assert "status-mode" in template
    assert "status-payload" in template
    assert "status-rendering" in template
    assert "updateRenderingStatusChip" in template
    assert "renderPayloadWarnings" in template
    assert "remoteMode" in template
    assert "enterRemoteFocus" in template
    assert "runRemoteSearch" in template
    assert "loadRemoteAudit" in template
    assert "/api/focus" in template
    assert "/api/search" in template
    assert "/api/audit" in template
    assert "renderer-group" in template
    assert 'data-renderer="auto"' in template
    assert 'data-renderer="svg"' in template
    assert 'data-renderer="canvas"' in template
    assert "density-group" in template
    assert 'data-density="auto"' in template
    assert 'data-density="full"' in template
    assert 'data-density="balanced"' in template
    assert 'data-density="sparse"' in template
    assert "labels-group" in template
    assert 'data-labels="auto"' in template
    assert 'data-labels="key"' in template
    assert 'data-labels="all"' in template
    assert 'data-labels="none"' in template
    assert "RENDERER_AUTO_NODE_THRESHOLD = 600" in template
    assert "RENDERER_AUTO_EDGE_THRESHOLD = 1200" in template
    assert "DENSITY_AUTO_BALANCED_EDGE_THRESHOLD = 350" in template
    assert "DENSITY_AUTO_SPARSE_EDGE_THRESHOLD = 900" in template
    assert "resolveRenderer" in template
    assert "prepareRenderableGraph" in template
    assert "resolveDensityMode" in template
    assert "aggregateVisualEdges" in template
    assert "shouldDrawLabel" in template
    assert "renderCanvasGraph" in template
    assert "drawCanvas" in template
    assert "body.drawer-open .zoom-controls" in template
    assert "innerHTML" not in template


def test_workbench_vendor_assets_are_packaged() -> None:
    assets = resources.files("arcgraph.assets.workbench")

    assert assets.joinpath("index.html").is_file()
    assert assets.joinpath("vendor", "d3.v7.min.js").is_file()
    assert assets.joinpath("vendor", "LICENSE.d3.txt").is_file()


def test_legacy_viz_index_is_not_a_second_workbench_template() -> None:
    legacy_index = Path("viz/index.html").read_text(encoding="utf-8")

    assert "arcgraph visual workbench" in legacy_index
    assert "d3.forceSimulation" not in legacy_index
    assert "fetchJson('graph_data.json')" not in legacy_index
    assert "semanticTier(node)" not in legacy_index


def test_legacy_viz_script_delegates_instead_of_extracting() -> None:
    """The demo script must stay a caller, never a second extraction path.

    `viz/index.html` already has this guard; the script beside it did not. If
    this file ever grows its own node/edge shaping, the repository has two
    extraction contracts and a reader cannot tell which one is current.
    """

    source = Path("viz/extract_graph.py").read_text(encoding="utf-8")

    # It delegates to the one packaged export path, and names the CLI command
    # that supersedes it for normal use.
    assert "from arcgraph.core.force_graph_export import" in source
    assert "build_force_graph_export" in source
    assert "arcgraph visual force" in source
    # It does not shape the graph itself.
    for reimplementation in ("def _build_nodes", "def _build_edges", "semanticTier"):
        assert reimplementation not in source
    assert len(source.splitlines()) < 200, (
        "the demo wrapper has grown; check it is still delegating rather than "
        "carrying its own export logic"
    )


def _assert_no_forbidden_audit_payload(value: object) -> None:
    if isinstance(value, dict):
        forbidden = {"properties", "snippet", "source_snippet"}
        assert not forbidden.intersection(value)
        for item in value.values():
            _assert_no_forbidden_audit_payload(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_audit_payload(item)
