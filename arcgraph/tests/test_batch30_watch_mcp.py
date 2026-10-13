"""A long-lived MCP reader follows watch publications and the project switch."""

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.schemas import ExplainResponse
from arcgraph.interfaces.ops import watch_index
from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def test_watch_and_existing_mcp_reader_follow_configuration_and_current_revision(
    prepared, tmp_path
):
    project, _, _, _ = prepared
    path = project / "demo.py"
    path.write_text(
        "def target(): return 1\ndef f(): return lambda: target()\n", encoding="utf-8"
    )
    configuration = project / "pyproject.toml"
    configuration.write_text(
        "[tool.arcgraph]\nsemantic_core = false\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    original, _ = ArcGraphIndexer(project, output).build()
    config = ArcGraphMCPConfig.for_single_repo(project, output_dir=output)
    config.max_results_limit = 1
    tools = ArcGraphMCPToolGroup(config)
    assert tools.arcgraph_index_status()["index_version"] == original.index_version
    for enabled in (True, False, True):
        configuration.write_text(
            "[tool.arcgraph]\nsemantic_core = " + str(enabled).lower() + "\n",
            encoding="utf-8",
        )
        assert tools.arcgraph_index_status()["freshness"]["stale"] is True
        watched = watch_index(
            repo_root=project,
            output_dir=output,
            poll_interval_seconds=0.05,
            debounce_seconds=0,
            max_cycles=1,
        )
        assert watched["events"][0]["published"] is True
        reader = GraphStoreReader.from_current(output)
        state = reader.metadata["toolchain_status"]["semantic_core"]
        assert state["status"] == ("active" if enabled else "disabled")
        status = tools.arcgraph_index_status()
        assert status["index_version"] == reader.metadata["index_version"]
        assert status["freshness"]["stale"] is False
        anonymous = [
            n for n in reader.read_nodes() if n.kind == "function" and "#" in n.id
        ]
        assert bool(anonymous) == enabled
        target = anonymous[0].id if enabled else "fn:demo.f"
        response = tools.arcgraph_explain(targets=[target], max_results=100)
        ExplainResponse.model_validate(response)
        assert response["max_results"] == 1
        assert response["index_version"] == reader.metadata["index_version"]
