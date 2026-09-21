from __future__ import annotations

import json

from arcgraph.interfaces.agent_capabilities import mcp_capability_names
from arcgraph.interfaces.agent_help import (
    AGENT_HELP_SCHEMA_VERSION,
    HELP_TOPICS,
    AgentHelpResponse,
    build_agent_help,
    format_agent_help_human,
)
from arcgraph.interfaces.cli import build_parser, main
from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup
from arcgraph.interfaces.trial_feedback import TrialFeedbackStore


def test_agent_help_topics_are_bounded_structured_and_private() -> None:
    for topic in HELP_TOPICS:
        payload = build_agent_help(topic=topic, surface="mcp")
        encoded = json.dumps(payload, sort_keys=True)

        assert payload["schema_version"] == AGENT_HELP_SCHEMA_VERSION
        assert payload["status"] == "available"
        assert payload["topic"] == topic
        assert payload["read_only"] is True
        assert payload["source_snippets"] == {
            "requested": False,
            "enabled": False,
        }
        assert payload["inventory"]["mcp_tools"] == list(mcp_capability_names())
        assert payload["inventory"]["mcp_tool_count"] == len(mcp_capability_names())
        assert payload["inventory"]["cli_commands"] == []
        assert payload["feedback"]["enabled"] is False
        assert payload["feedback"]["cli_available"] is False
        assert payload["feedback"]["mcp_enabled"] is False
        assert payload["feedback"]["tool_registered"] is False
        assert "tool" in payload
        assert len(encoded.encode("utf-8")) < 32_000
        assert "/Users/" not in encoded
        assert "/private/" not in encoded
        assert "file://" not in encoded


def test_cli_help_topic_choices_match_the_structured_help_contract() -> None:
    parser = build_parser()
    commands = next(action for action in parser._actions if action.dest == "command")
    help_parser = commands.choices["help"]
    topic = next(action for action in help_parser._actions if action.dest == "topic")

    assert tuple(topic.choices) == HELP_TOPICS
    assert AgentHelpResponse.model_json_schema()["properties"]["topic"]["enum"] == list(
        HELP_TOPICS
    )


def test_agent_help_binds_tool_guidance_to_the_exact_registered_tool() -> None:
    payload = build_agent_help(
        topic="tool_selection",
        tool_name="arcgraph_get_context",
        surface="mcp",
    )

    assert payload["status"] == "available"
    assert payload["tool"]["name"] == "arcgraph_get_context"
    assert payload["tool"]["cost"] == "high"
    assert payload["tool"]["behavior"] == {
        "read_only": True,
        "destructive": False,
        "idempotent": True,
        "open_world": False,
    }
    assert [item["name"] for item in payload["tool_selection"]] == list(
        mcp_capability_names()
    )
    assert "cli_fallback" in payload["tool"]


def test_agent_help_does_not_echo_unknown_tool_inputs() -> None:
    secret = "/private/SECRET_TARGET_XYZ"
    payload = build_agent_help(
        topic="overview",
        tool_name=secret,
        surface="mcp",
    )
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "unavailable"
    assert payload["topic"] == "overview"
    assert secret not in encoded
    assert payload.get("tool") is None
    assert payload["warnings"] == ["The requested ArcGraph MCP tool is unknown."]


def test_mcp_help_does_not_require_an_index(tmp_path) -> None:
    tools = ArcGraphMCPToolGroup(ArcGraphMCPConfig.for_single_repo(tmp_path))

    payload = tools.arcgraph_help(
        topic="result_contract",
        tool_name="arcgraph_index_status",
    )

    assert payload["status"] == "available"
    assert payload["tool"]["name"] == "arcgraph_index_status"
    assert payload["inventory"]["mcp_tools"] == list(mcp_capability_names())
    assert payload["feedback"]["cli_available"] is True


def test_mcp_help_exposes_cli_feedback_when_the_mcp_tool_is_disabled(
    tmp_path,
) -> None:
    tools = ArcGraphMCPToolGroup(ArcGraphMCPConfig.for_single_repo(tmp_path))

    payload = tools.arcgraph_help(topic="feedback")

    assert payload["feedback"]["enabled"] is False
    assert payload["feedback"]["cli_available"] is True
    assert payload["feedback"]["mcp_enabled"] is False
    assert payload["feedback"]["tool_registered"] is False
    assert payload["feedback"]["persistence"] == "disabled"
    assert "arcgraph feedback record" in payload["feedback"]["guidance"]


def test_mcp_help_reports_the_feedback_tool_only_in_enabled_mode(tmp_path) -> None:
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            tmp_path,
            feedback_store=TrialFeedbackStore(tmp_path / "feedback.jsonl"),
        )
    )

    payload = tools.arcgraph_help(
        topic="feedback",
        tool_name="arcgraph_record_trial_feedback",
    )

    assert payload["status"] == "available"
    assert payload["feedback"]["enabled"] is True
    assert payload["feedback"]["cli_available"] is True
    assert payload["feedback"]["mcp_enabled"] is True
    assert payload["feedback"]["tool_registered"] is True
    assert payload["inventory"]["mcp_tools"] == list(
        mcp_capability_names(feedback_enabled=True)
    )
    assert payload["tool"]["behavior"]["read_only"] is False
    assert not (tmp_path / "feedback.jsonl").exists()


def test_known_feedback_tool_remains_discoverable_when_not_registered() -> None:
    payload = build_agent_help(
        topic="feedback",
        tool_name="arcgraph_record_trial_feedback",
        surface="mcp",
    )

    assert payload["status"] == "available"
    assert payload["tool"]["name"] == "arcgraph_record_trial_feedback"
    assert payload["feedback"]["tool_registered"] is False
    assert payload["feedback"]["mcp_enabled"] is False
    assert payload["warnings"] == [
        "The requested ArcGraph MCP tool is known but is not registered "
        "in this server mode."
    ]


def test_agent_help_preserves_explicit_missing_cli_fallback() -> None:
    payload = build_agent_help(
        topic="tool_selection",
        tool_name="arcgraph_get_why",
        surface="mcp",
    )

    assert "cli_fallback" in payload["tool"]
    assert payload["tool"]["cli_fallback"] is None


def test_cli_help_exposes_parser_commands_from_the_live_parser(
    capsys,
) -> None:
    parser = build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    expected_commands = list(subparsers.choices)

    assert main(["help", "--surface", "all"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["inventory"]["cli_commands"] == expected_commands
    assert payload["inventory"]["cli_command_count"] == len(expected_commands)
    assert payload["inventory"]["mcp_tools"] == list(mcp_capability_names())
    assert "help" in expected_commands
    assert "docs" in expected_commands
    assert payload["feedback"]["enabled"] is True
    assert payload["feedback"]["cli_available"] is True
    assert payload["feedback"]["mcp_enabled"] is False
    assert payload["feedback"]["tool_registered"] is False
    assert "arcgraph feedback record" in payload["feedback"]["guidance"]


def test_cli_mcp_help_reports_the_available_cli_feedback_fallback(capsys) -> None:
    assert main(["help", "--surface", "mcp"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["inventory"]["cli_commands"] == []
    assert payload["feedback"]["enabled"] is False
    assert payload["feedback"]["cli_available"] is True
    assert payload["feedback"]["mcp_enabled"] is False
    assert payload["feedback"]["persistence"] == "disabled"
    assert "arcgraph feedback record" in payload["feedback"]["guidance"]


def test_cli_only_tool_selection_does_not_list_unavailable_mcp_tools() -> None:
    parser = build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")

    payload = build_agent_help(
        topic="tool_selection",
        surface="cli",
        cli_commands=tuple(subparsers.choices),
    )

    assert payload["inventory"]["mcp_tools"] == []
    assert payload["tool_selection"] == []


def test_cli_unknown_tool_returns_structured_nonzero_result(capsys) -> None:
    assert main(["help", "--tool", "not_a_real_tool"]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "unavailable"
    assert payload["tool"] is None
    assert payload["warnings"] == ["The requested ArcGraph MCP tool is unknown."]


def test_cli_help_human_output_is_compact(capsys) -> None:
    assert (
        main(
            [
                "--human",
                "help",
                "--topic",
                "workflow",
                "--tool",
                "arcgraph_get_context",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out

    assert output.startswith("ArcGraph Agent Help — workflow\n")
    assert "ArcGraph Get Context (arcgraph_get_context)" in output
    assert "Feedback: CLI available with an explicit local log; MCP disabled" in output
    assert '"schema_version"' not in output


def test_human_formatter_revalidates_the_machine_contract() -> None:
    payload = build_agent_help(topic="overview", surface="mcp")

    output = format_agent_help_human(payload)

    assert "MCP tools:" in output
    assert "Check index status" in output
