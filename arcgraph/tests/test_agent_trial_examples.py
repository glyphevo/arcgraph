"""Execute the trial guide's MCP examples against a bounded caller fixture."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import re

from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup
from arcgraph.pipeline.indexer import ArcGraphIndexer

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "agent-reading-guide.md"


def _examples() -> dict[str, dict[str, object]]:
    blocks = [
        json.loads(block)
        for block in re.findall(r"```json\n(.*?)\n```", GUIDE.read_text(), re.DOTALL)
    ]
    # The guide also holds manual client-config templates in json fences; only
    # blocks that name a tool are executable tool-call examples.
    examples = [block for block in blocks if "tool" in block]
    assert examples, "The executable MCP examples must remain in the guide."
    return {example["tool"]: example["arguments"] for example in examples}


def test_trial_examples_use_supported_mcp_parameters() -> None:
    examples = _examples()
    assert {"arcgraph_get_risk", "arcgraph_explain"} <= examples.keys()
    for name, arguments in examples.items():
        # Bind to the public handlers used by MCP registration. Unknown keywords
        # (such as get_risk(detail_level=...)) must fail before a user copies them.
        handler = getattr(ArcGraphMCPToolGroup, name)
        inspect.signature(handler).bind(None, **arguments)


def test_trial_explain_example_recovers_callers_hidden_by_summary(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = "def target_function():\n    return 1\n\n"
    source += "\n".join(
        f"def caller_{i}():\n    return target_function()\n" for i in range(8)
    )
    (repo / "your_package.py").write_text(source, encoding="utf-8")
    output = tmp_path / "index"
    ArcGraphIndexer(
        repo_root=repo, output_dir=output, source_roots=[SourceRoot(".")]
    ).build()
    group = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(repo, output_dir=output)
    )
    examples = _examples()
    for name, arguments in examples.items():
        assert getattr(group, name)(**arguments)["status"] == "available"

    arguments = examples["arcgraph_explain"]
    summary = group.arcgraph_explain(**{**arguments, "detail_level": "summary"})
    detailed = group.arcgraph_explain(**arguments)
    assert summary["truncation"]["truncated"]
    assert summary["truncation"]["context_limit"] == 6
    assert len(summary["explanations"][0]["callers"]) == 6
    assert not detailed["truncation"]["truncated"]
    assert {node["id"] for node in detailed["explanations"][0]["callers"]} == {
        f"fn:your_package.caller_{i}" for i in range(8)
    }
