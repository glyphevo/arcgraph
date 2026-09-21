from __future__ import annotations

from arcgraph.core.recovery import STALE_RECOVERY_COMMAND
from arcgraph.interfaces.agent_capabilities import (
    AgentCapabilitySpec,
    ALL_MCP_CAPABILITY_NAMES,
    MCP_CAPABILITY_SPECS,
    capability_by_name,
    mcp_capability_names,
    mcp_capability_specs,
    mcp_tool_descriptions,
)
from arcgraph.interfaces import mcp_tools
from arcgraph.interfaces.mcp_tools import ArcGraphMCPToolGroup


def test_capability_registry_exactly_covers_the_mcp_tool_group() -> None:
    handler_names = {
        name
        for name in dir(ArcGraphMCPToolGroup)
        if name.startswith("arcgraph_")
        and callable(getattr(ArcGraphMCPToolGroup, name))
    }

    assert set(mcp_capability_names(feedback_enabled=True)) == handler_names
    assert ALL_MCP_CAPABILITY_NAMES == handler_names
    assert set(mcp_capability_names()) == handler_names - {
        "arcgraph_record_trial_feedback"
    }
    assert tuple(mcp_tool_descriptions()) == mcp_capability_names()
    assert "handler_name" not in AgentCapabilitySpec.__dataclass_fields__
    assert not hasattr(mcp_tools, "TOOL_DESCRIPTIONS")


def test_capability_registry_has_complete_agent_guidance() -> None:
    assert mcp_capability_specs(feedback_enabled=True) == MCP_CAPABILITY_SPECS

    for spec in MCP_CAPABILITY_SPECS:
        assert capability_by_name(spec.name, feedback_enabled=True) is spec
        assert spec.title.startswith("ArcGraph ")
        assert spec.description.endswith(".")
        assert spec.when_to_use
        assert spec.avoid_when
        assert spec.prerequisites
        assert spec.cost in {"low", "medium", "high", "variable"}
        assert spec.result_signals
        assert spec.recovery
        assert spec.behavior.destructive is False
        assert spec.behavior.idempotent is True

    assert capability_by_name("arcgraph_get_why").behavior.open_world is True
    assert {spec.name for spec in MCP_CAPABILITY_SPECS if spec.behavior.open_world} == {
        "arcgraph_get_why"
    }
    feedback = capability_by_name(
        "arcgraph_record_trial_feedback",
        feedback_enabled=True,
    )
    assert feedback is not None
    assert feedback.availability == "feedback_enabled"
    assert feedback.behavior.read_only is False
    assert feedback.behavior.destructive is False
    assert feedback.behavior.idempotent is True
    assert feedback.behavior.open_world is False
    assert {
        spec.name for spec in MCP_CAPABILITY_SPECS if not spec.behavior.read_only
    } == {"arcgraph_record_trial_feedback"}
    assert capability_by_name("missing") is None


def test_feedback_capability_is_present_only_when_explicitly_enabled() -> None:
    assert mcp_capability_specs(feedback_enabled=True) == MCP_CAPABILITY_SPECS
    assert "arcgraph_record_trial_feedback" not in mcp_capability_names()
    assert (
        mcp_capability_names(feedback_enabled=True)[-1]
        == "arcgraph_record_trial_feedback"
    )
    assert capability_by_name("arcgraph_record_trial_feedback") is None


def test_index_status_recovery_uses_the_machine_readable_stale_command() -> None:
    spec = capability_by_name("arcgraph_index_status")

    assert spec is not None
    assert any(f"`{STALE_RECOVERY_COMMAND}`" in item for item in spec.recovery)
    assert all("arcgraph reindex --changed" not in item for item in spec.recovery)
