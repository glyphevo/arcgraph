from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

import anyio
import pytest

from arcgraph.interfaces.agent_capabilities import mcp_capability_names
from scripts.arcgraph_multi_project_trial_probe import (
    EXPECTED_FEEDBACK_TOOL_NAMES,
    _call_tool,
    _project_layout,
    _server_parameters,
    _validate_foreign_path_rejected,
    _validate_separate_feedback,
    _validate_separate_metrics,
    _validate_tool_contract,
)
from scripts.arcgraph_package_readiness_smoke import (
    _validate_multi_project_trial_payload,
    build_multi_project_trial_command,
)


class _ForeignResult:
    def __init__(self, *, is_error: bool, payload: dict[str, object]) -> None:
        self.is_error = is_error
        self.structured_content = payload

    def model_dump_json(self) -> str:
        return json.dumps(
            {
                "isError": self.is_error,
                "structuredContent": self.structured_content,
            }
        )


def test_black_box_feedback_tool_contract_matches_capability_registry() -> None:
    assert EXPECTED_FEEDBACK_TOOL_NAMES == mcp_capability_names(feedback_enabled=True)


def test_server_parameters_keep_default_repo_and_separate_state(tmp_path: Path) -> None:
    python = tmp_path / "venv" / "bin" / "python"
    project_a = _project_layout(tmp_path, "project-a")
    project_b = _project_layout(tmp_path, "project-b")

    params_a = _server_parameters(python, project_a, name="Project A")
    params_b = _server_parameters(python, project_b, name="Project B")

    assert "--repo-id" not in params_a.args
    assert "--repo-id" not in params_b.args
    assert params_a.args[params_a.args.index("--name") + 1] == "Project A"
    assert params_b.args[params_b.args.index("--name") + 1] == "Project B"
    for option in ("--repo-root", "--output-dir", "--metrics-log", "--feedback-log"):
        assert (
            params_a.args[params_a.args.index(option) + 1]
            != params_b.args[params_b.args.index(option) + 1]
        )


def test_foreign_path_requires_an_error_or_unresolved_bounded_result() -> None:
    _validate_foreign_path_rejected(
        _ForeignResult(is_error=True, payload={"message": "outside allowed roots"})
    )
    _validate_foreign_path_rejected(
        _ForeignResult(
            is_error=False,
            payload={
                "status": "unavailable",
                "resolved_targets": [],
                "warnings": [],
            },
        )
    )

    with pytest.raises(RuntimeError, match="not rejected"):
        _validate_foreign_path_rejected(
            _ForeignResult(
                is_error=False,
                payload={
                    "status": "available",
                    "resolved_targets": ["fn:other"],
                },
            )
        )


def test_feedback_validator_rejects_cross_project_event_ids() -> None:
    records_a = [{"client_event_id": "event-a"}]
    records_b = [{"client_event_id": "event-a"}]

    with pytest.raises(RuntimeError, match="Project A feedback crossed"):
        _validate_separate_feedback(
            records_a,
            records_b,
            event_ids_a=("event-a",),
            event_ids_b=("event-b",),
        )


def test_metrics_validator_rejects_cross_project_leakage() -> None:
    expected_a = Counter(
        {
            "arcgraph_index_status": 1,
            "arcgraph_get_context": 2,
            "arcgraph_record_trial_feedback": 1,
        }
    )
    expected_b = Counter(
        {
            "arcgraph_index_status": 2,
            "arcgraph_get_context": 1,
            "arcgraph_help": 1,
            "arcgraph_record_trial_feedback": 1,
        }
    )
    records_a = [
        {"tool_name": "arcgraph_index_status"},
        {"tool_name": "arcgraph_get_context"},
        {"tool_name": "arcgraph_get_context"},
        {"tool_name": "arcgraph_record_trial_feedback"},
    ]
    records_b = [
        {"tool_name": "arcgraph_index_status"},
        {"tool_name": "arcgraph_index_status"},
        {"tool_name": "arcgraph_get_context"},
        {"tool_name": "arcgraph_help"},
        {"tool_name": "arcgraph_record_trial_feedback"},
    ]

    _validate_separate_metrics(
        records_a,
        records_b,
        expected_a=expected_a,
        expected_b=expected_b,
    )

    with pytest.raises(RuntimeError, match="Project B metrics contained unexpected"):
        _validate_separate_metrics(
            records_a,
            [*records_b, {"tool_name": "arcgraph_get_context"}],
            expected_a=expected_a,
            expected_b=expected_b,
        )
    with pytest.raises(RuntimeError, match="Project A metrics contained unexpected"):
        _validate_separate_metrics(
            [*records_a, {"tool_name": "arcgraph_help"}],
            records_b,
            expected_a=expected_a,
            expected_b=expected_b,
        )
    with pytest.raises(RuntimeError, match="Project A best-effort metrics evidence"):
        _validate_separate_metrics(
            records_a[:-1],
            records_b,
            expected_a=expected_a,
            expected_b=expected_b,
        )


def test_tool_call_records_the_expected_metric_next_to_the_call() -> None:
    class FakeClient:
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, object],
        ) -> dict[str, object]:
            return {"name": name, "arguments": arguments}

    async def exercise() -> tuple[dict[str, object], Counter[str]]:
        expected: Counter[str] = Counter()
        result = await _call_tool(
            FakeClient(),
            "arcgraph_help",
            {"topic": "workflow"},
            expected_metrics=expected,
        )
        return result, expected

    result, expected = anyio.run(exercise)

    assert result["name"] == "arcgraph_help"
    assert expected == Counter({"arcgraph_help": 1})


def test_feedback_tool_contract_rejects_equal_count_substitution() -> None:
    class Tool:
        def __init__(self, name: str) -> None:
            self.name = name

    tools = [Tool(name) for name in EXPECTED_FEEDBACK_TOOL_NAMES]
    assert _validate_tool_contract(tools) == EXPECTED_FEEDBACK_TOOL_NAMES

    tools[-2] = Tool("arcgraph_unexpected_replacement")
    with pytest.raises(RuntimeError, match="exact trial tool contract"):
        _validate_tool_contract(tools)


def test_package_plan_runs_probe_with_one_installed_environment(
    tmp_path: Path,
) -> None:
    spec = build_multi_project_trial_command(
        repo_root=tmp_path / "source",
        server_python=tmp_path / "venv" / "bin" / "python",
        arcgraph=tmp_path / "venv" / "bin" / "arcgraph",
        workspace=tmp_path / "trial",
        package_version="0.1.0rc6",
        timeout_seconds=120,
    )

    assert spec.name == "installed-wheel-multi-project-trial"
    assert spec.command[0] == str(tmp_path / "venv" / "bin" / "python")
    assert spec.command[spec.command.index("--server-python") + 1] == spec.command[0]
    assert "--repo-id" not in spec.command


def test_package_validator_requires_every_isolation_dimension() -> None:
    payload = {
        "schema_version": "1.0",
        "status": "pass",
        "project_count": 2,
        "repo_ids": ["default", "default"],
        "feedback_tool_names": list(EXPECTED_FEEDBACK_TOOL_NAMES),
        "one_installed_environment": True,
        "installed_cli_feedback": True,
        "server_names_distinct": True,
        "output_directories_distinct": True,
        "path_authorization_fail_closed": True,
        "current_and_graph_state_isolated": True,
        "metrics_isolated": True,
        "feedback_isolated": True,
        "peer_survived_other_server_shutdown": True,
        "installation_state_unchanged": True,
        "client_configuration_unchanged": True,
        "permission_model": "posix_private_modes_verified",
        "state_alias_policy": "posix_parent_symlinks_rejected",
    }
    if __import__("os").name != "posix":
        payload["permission_model"] = "windows_acl_not_asserted"
        payload["state_alias_policy"] = "windows_reparse_not_asserted"

    assert _validate_multi_project_trial_payload(payload) == payload
    payload["metrics_isolated"] = False
    with pytest.raises(RuntimeError, match="omitted an isolation guarantee"):
        _validate_multi_project_trial_payload(payload)

    payload["metrics_isolated"] = True
    payload["feedback_tool_names"][0] = "arcgraph_unexpected_replacement"
    with pytest.raises(RuntimeError, match="exact feedback-enabled tool contract"):
        _validate_multi_project_trial_payload(payload)

    payload["feedback_tool_names"] = list(EXPECTED_FEEDBACK_TOOL_NAMES)
    payload["state_alias_policy"] = "not_verified"
    with pytest.raises(RuntimeError, match="state-alias policy"):
        _validate_multi_project_trial_payload(payload)
