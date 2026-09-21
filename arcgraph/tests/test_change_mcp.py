from __future__ import annotations

import pytest

from arcgraph.interfaces.agent_capabilities import mcp_capability_names
from arcgraph.interfaces.mcp_tools import (
    ArcGraphMCPConfig,
    ArcGraphMCPToolGroup,
    ArcGraphPermissionError,
    _change_mcp_error,
)
from arcgraph.tests.change_safety_helpers import activated_approved_plan


def test_change_mcp_tools_are_read_only_compute_and_preview(tmp_path) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            repo,
            repo_id="repo",
            output_dir=output,
            allowed_roots=[repo.parent],
        )
    )

    before_plans = service.store.list_plan_ids()
    before_reports = service.store.list_verification_reports(plan.plan_id)
    preview = tools.arcgraph_preview_change_plan(
        repo_id="repo",
        task="preview exact function change",
        targets=[{"kind": "symbol", "value": "fn:app.f"}],
    )
    assert preview["read_only"] is True
    assert preview["status"] == "available"
    assert service.store.list_plan_ids() == before_plans

    shown = tools.arcgraph_get_change_plan(repo_id="repo", plan_id=plan.plan_id)
    assert shown["read_only"] is True
    assert shown["data"]["plan_id"] == plan.plan_id
    listed = tools.arcgraph_list_change_plans(repo_id="repo")
    assert [item["plan_id"] for item in listed["data"]["plans"]] == [plan.plan_id]

    delta = tools.arcgraph_get_graph_delta(
        repo_id="repo",
        plan_id=plan.plan_id,
        revision=plan.revision,
        plan_content_digest=plan.plan_content_digest,
    )
    assert delta["status"] == "available"
    assert delta["operation"] == "get_graph_delta"

    verification = tools.arcgraph_verify_change(
        repo_id="repo",
        plan_id=plan.plan_id,
        revision=plan.revision,
        plan_content_digest=plan.plan_content_digest,
    )
    assert verification["read_only"] is True
    assert verification["status"] == "blocked"
    assert verification["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert service.store.list_verification_reports(plan.plan_id) == before_reports


def test_change_mcp_rejects_path_escape_and_has_no_write_tools(tmp_path) -> None:
    repo, output, _service, _plan = activated_approved_plan(tmp_path)
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            repo,
            repo_id="repo",
            output_dir=output,
            allowed_roots=[repo.parent],
        )
    )

    with pytest.raises(ArcGraphPermissionError):
        tools.arcgraph_preview_change_plan(
            repo_id="repo",
            task="invalid external target",
            targets=[{"kind": "path", "value": "../outside.py"}],
        )
    with pytest.raises(ArcGraphPermissionError):
        tools.arcgraph_preview_change_plan(
            repo_id="repo",
            task="invalid external OpenAPI input",
            targets=[{"kind": "symbol", "value": "fn:app.f"}],
            openapi_input="../outside.yaml",
        )

    required = {
        "arcgraph_preview_change_plan",
        "arcgraph_get_change_plan",
        "arcgraph_list_change_plans",
        "arcgraph_get_graph_delta",
        "arcgraph_verify_change",
    }
    tool_names = mcp_capability_names()
    assert required <= set(tool_names)
    forbidden_fragments = (
        "approve",
        "reject",
        "abandon",
        "archive",
        "purge",
        "audit",
        "persist",
    )
    assert not any(
        fragment in tool_name
        for tool_name in tool_names
        for fragment in forbidden_fragments
    )


def test_change_mcp_distinguishes_a_missing_plan_from_store_corruption(
    tmp_path,
) -> None:
    repo, output, _service, _plan = activated_approved_plan(tmp_path)
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            repo,
            repo_id="repo",
            output_dir=output,
            allowed_roots=[repo.parent],
        )
    )

    payload = tools.arcgraph_get_change_plan(repo_id="repo", plan_id="missing")

    assert payload["status"] == "error"
    assert payload["error_code"] == "CHANGE_STORE_RECORD_NOT_FOUND"


def test_change_mcp_generic_errors_redact_host_user_paths() -> None:
    payload = _change_mcp_error(
        "repo",
        FileNotFoundError("missing C:\\Users\\alice\\private\\index.sqlite"),
    )

    assert "alice" not in payload["error"]["message"]
    assert "<redacted-user-path>" in payload["error"]["message"]


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        (
            "arcgraph_preview_change_plan",
            {
                "task": "preview",
                "targets": [{"kind": "symbol", "value": "fn:app.f"}],
            },
        ),
        ("arcgraph_get_change_plan", {"plan_id": "plan"}),
        ("arcgraph_list_change_plans", {}),
        (
            "arcgraph_get_graph_delta",
            {
                "plan_id": "plan",
                "revision": 1,
                "plan_content_digest": "digest",
            },
        ),
        (
            "arcgraph_verify_change",
            {
                "plan_id": "plan",
                "revision": 1,
                "plan_content_digest": "digest",
            },
        ),
    ],
)
def test_each_change_mcp_tool_returns_a_redacted_runtime_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    kwargs: dict[str, object],
) -> None:
    repo, output, _service, _plan = activated_approved_plan(tmp_path)
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            repo,
            repo_id="repo",
            output_dir=output,
            allowed_roots=[repo.parent],
        )
    )

    class FailingService:
        def __getattr__(self, _name: str):
            def fail(*_args: object, **_kwargs: object) -> None:
                raise FileNotFoundError(
                    "missing C:\\Users\\alice\\private\\change-store.json"
                )

            return fail

    monkeypatch.setattr(tools, "_change_service", lambda _repo: FailingService())

    payload = getattr(tools, tool_name)(repo_id="repo", **kwargs)

    assert payload["status"] == "error"
    assert payload["error_code"] == "CHANGE_MCP_RUNTIME_ERROR"
    assert "alice" not in payload["error"]["message"]
    assert "<redacted-user-path>" in payload["error"]["message"]
