from __future__ import annotations

import anyio
import asyncio
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
import uuid

import pytest

from arcgraph import __version__
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.agent_help import HELP_TOPICS
from arcgraph.interfaces.trial_feedback import (
    FEEDBACK_WARNING_KINDS,
    MAX_WARNING_KINDS,
)
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests import mcp_runtime_probe
from arcgraph.tests.mcp_runtime_probe import (
    CANCEL_STARTED,
    CONCURRENT_MIXED,
    CONCURRENT_SAME,
    TIMEOUT_STARTED,
    probe_release_path,
    replace_state_file,
)
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError
from mcp_types import REQUEST_TIMEOUT

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
_COPIED_FIXTURE_IGNORE = shutil.ignore_patterns(
    "output",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)
EXPECTED_PROTOCOLS = {
    "auto": "2026-07-28",
    "legacy": "2025-11-25",
}
EXPECTED_TOOL_DESCRIPTIONS = {
    "arcgraph_index_status": (
        "Return ArcGraph index freshness, counts, and capabilities for a "
        "registered repository."
    ),
    "arcgraph_get_context": (
        "Return compact structural context for code targets before editing or review."
    ),
    "arcgraph_explain": (
        "Explain target resolution, direct edge evidence, resolution fallbacks, "
        "and confidence sources. Resource/queue reads, writes, enqueues and "
        "consumes appear separately in explanations.relations."
    ),
    "arcgraph_get_risk": (
        "Run a bounded Change Preflight with resolution, impact, tests, similar "
        "implementations, entrypoints, assurance, and next reads."
    ),
    "arcgraph_entrypoint_flow": (
        "Trace a route, MCP tool, worker, or queue entrypoint through indexed flow edges."
    ),
    "arcgraph_get_why": (
        "Explain structural reasons and, when configured, external historical "
        "memory around a target."
    ),
    "arcgraph_find_similar": (
        "Find indexed implementations with similar normalized AST structure and "
        "supporting evidence."
    ),
    "arcgraph_record_learning": (
        "Create a read-only external memory candidate; does not persist memory."
    ),
    "arcgraph_preview_change_plan": (
        "Compute a fail-closed surgical-change plan without persisting a plan or pin."
    ),
    "arcgraph_get_change_plan": (
        "Read a current surgical-change plan view and redacted evidence summary."
    ),
    "arcgraph_list_change_plans": (
        "List current surgical-change plan views without mutation."
    ),
    "arcgraph_get_graph_delta": (
        "Compute a current graph delta for one exact surgical-change revision "
        "without persisting a report."
    ),
    "arcgraph_verify_change": (
        "Compute a surgical-change verification result without writing a report "
        "or lifecycle event."
    ),
    "arcgraph_help": (
        "Return bounded Agent guidance for tool selection, result interpretation, "
        "recovery, and trial feedback."
    ),
}
EXPECTED_TOOL_TITLES = {
    "arcgraph_index_status": "ArcGraph Index Status",
    "arcgraph_get_context": "ArcGraph Get Context",
    "arcgraph_explain": "ArcGraph Explain",
    "arcgraph_get_risk": "ArcGraph Get Risk",
    "arcgraph_entrypoint_flow": "ArcGraph Entrypoint Flow",
    "arcgraph_get_why": "ArcGraph Get Why",
    "arcgraph_find_similar": "ArcGraph Find Similar",
    "arcgraph_record_learning": "ArcGraph Record Learning Candidate",
    "arcgraph_preview_change_plan": "ArcGraph Preview Change Plan",
    "arcgraph_get_change_plan": "ArcGraph Get Change Plan",
    "arcgraph_list_change_plans": "ArcGraph List Change Plans",
    "arcgraph_get_graph_delta": "ArcGraph Get Graph Delta",
    "arcgraph_verify_change": "ArcGraph Verify Change",
    "arcgraph_help": "ArcGraph Help",
}
EXPECTED_TOOL_PROPERTIES = {
    "arcgraph_index_status": {"repo_id"},
    "arcgraph_get_context": {
        "repo_id",
        "task",
        "targets",
        "max_results",
        "detail_level",
        "profile",
        "include_source",
    },
    "arcgraph_explain": {
        "repo_id",
        "task",
        "targets",
        "max_results",
        "detail_level",
        "profile",
        "include_source",
    },
    "arcgraph_get_risk": {
        "repo_id",
        "targets",
        "changed_files",
        "max_depth",
        "max_results",
        "include_source",
        "verify_references",
    },
    "arcgraph_entrypoint_flow": {
        "repo_id",
        "entrypoint",
        "method",
        "path",
        "max_depth",
        "max_results",
        "include_source",
    },
    "arcgraph_get_why": {
        "repo_id",
        "target",
        "task",
        "max_results",
        "include_source",
    },
    "arcgraph_find_similar": {
        "repo_id",
        "target",
        "max_results",
        "detail_level",
        "include_source",
    },
    "arcgraph_record_learning": {
        "repo_id",
        "target",
        "observation",
        "memory_type",
        "title",
        "include_source",
    },
    "arcgraph_preview_change_plan": {
        "repo_id",
        "task",
        "targets",
        "constraints",
        "acceptance_criteria",
        "protected_surfaces",
        "openapi_input",
    },
    "arcgraph_get_change_plan": {"repo_id", "plan_id"},
    "arcgraph_list_change_plans": {"repo_id"},
    "arcgraph_get_graph_delta": {
        "repo_id",
        "plan_id",
        "revision",
        "plan_content_digest",
    },
    "arcgraph_verify_change": {
        "repo_id",
        "plan_id",
        "revision",
        "plan_content_digest",
    },
    "arcgraph_help": {"topic", "tool_name"},
}
TOOL_CALLS = {
    "arcgraph_index_status": {"repo_id": "sample"},
    "arcgraph_get_context": {
        "repo_id": "sample",
        "targets": ["pkg.service.build_message"],
    },
    "arcgraph_explain": {
        "repo_id": "sample",
        "targets": ["pkg.service.build_message"],
    },
    "arcgraph_get_risk": {
        "repo_id": "sample",
        "targets": ["pkg.service.MemoryService.create_memory"],
    },
    "arcgraph_entrypoint_flow": {
        "repo_id": "sample",
        "method": "POST",
        "path": "/memories",
    },
    "arcgraph_get_why": {
        "repo_id": "sample",
        "target": "pkg.service.MemoryService.create_memory",
    },
    "arcgraph_find_similar": {
        "repo_id": "sample",
        "target": "pkg.service.MemoryService.create_memory",
    },
    "arcgraph_record_learning": {
        "repo_id": "sample",
        "target": "pkg.service.MemoryService.create_memory",
        "observation": "Review structural impact.",
    },
    "arcgraph_preview_change_plan": {
        "repo_id": "sample",
        "task": "Review message construction",
        "targets": [{"kind": "symbol", "value": "pkg.service.build_message"}],
    },
    "arcgraph_get_change_plan": {
        "repo_id": "sample",
        "plan_id": "missing-plan",
    },
    "arcgraph_list_change_plans": {"repo_id": "sample"},
    "arcgraph_get_graph_delta": {
        "repo_id": "sample",
        "plan_id": "missing-plan",
        "revision": 1,
        "plan_content_digest": "0" * 64,
    },
    "arcgraph_verify_change": {
        "repo_id": "sample",
        "plan_id": "missing-plan",
        "revision": 1,
        "plan_content_digest": "0" * 64,
    },
    "arcgraph_help": {"topic": "workflow"},
}
EXPECTED_CONTRACT_ERRORS = {
    "arcgraph_get_change_plan": "CHANGE_STORE_RECORD_NOT_FOUND",
    "arcgraph_get_graph_delta": "CHANGE_STORE_RECORD_NOT_FOUND",
    "arcgraph_verify_change": "CHANGE_STORE_RECORD_NOT_FOUND",
}
EXPECTED_FEEDBACK_EVENT_PROPERTIES = {
    "client_event_id",
    "surface",
    "tool_name",
    "outcome",
    "issue_kind",
    "stage",
    "fallback",
    "result_status",
    "freshness_status",
    "warning_kinds",
}
EXPECTED_FEEDBACK_REQUIRED = {
    "client_event_id",
    "surface",
    "tool_name",
    "outcome",
    "issue_kind",
    "stage",
    "fallback",
}


@pytest.fixture(scope="module")
def indexed_sample(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    workspace = tmp_path_factory.mktemp("mcp-protocol")
    repo_root = workspace / "sample_project"
    shutil.copytree(FIXTURE_ROOT, repo_root, ignore=_COPIED_FIXTURE_IGNORE)
    _initialize_git_repository(repo_root, commit=True)
    output_dir = workspace / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    connection = sqlite3.connect(reader.sqlite_path)
    try:
        connection.executemany(
            "INSERT INTO warnings(kind, path, message) VALUES (?, ?, ?)",
            [
                (
                    "synthetic_external_path",
                    "/etc/passwd",
                    "Synthetic warning references /etc/passwd.",
                ),
                (
                    "synthetic_user_path",
                    "/Users/alice/.ssh/id_rsa",
                    "Synthetic warning references /Users/alice/.ssh/id_rsa.",
                ),
                (
                    "synthetic_secret",
                    "src/pkg/service.py",
                    "Synthetic credential SECRET123 must not cross MCP.",
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return repo_root.resolve(), output_dir


@pytest.fixture
def indexed_sample_without_baseline(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "sample_project"
    shutil.copytree(FIXTURE_ROOT, repo_root, ignore=_COPIED_FIXTURE_IGNORE)
    _initialize_git_repository(repo_root, commit=False)
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    return repo_root.resolve(), output_dir


def test_mcp_stdio_modern_and_legacy_contract(
    indexed_sample: tuple[Path, Path],
) -> None:
    modern = anyio.run(_exercise_stdio_contract, "auto", indexed_sample)
    legacy = anyio.run(_exercise_stdio_contract, "legacy", indexed_sample)

    assert modern["tool_contract"] == legacy["tool_contract"]
    assert modern["result_contract"] == legacy["result_contract"]


def test_mcp_stdio_reports_unavailable_baseline_without_git_head(
    indexed_sample_without_baseline: tuple[Path, Path],
) -> None:
    anyio.run(
        _exercise_baseline_unavailable,
        indexed_sample_without_baseline,
    )


def test_mcp_stdio_proves_worker_thread_concurrency_and_sqlite_safety(
    indexed_sample: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    anyio.run(
        _exercise_worker_thread_concurrency,
        indexed_sample,
        tmp_path / "concurrency-state.json",
    )


def test_mcp_stdio_cancels_started_requests_and_recovers(
    indexed_sample: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    anyio.run(
        _exercise_cancellation_and_timeout,
        indexed_sample,
        tmp_path / "cancellation-state.json",
    )


def _deny_first_reads(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    denials: int,
) -> list[int]:
    attempts = [0]
    original = Path.read_text

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            attempts[0] += 1
            if attempts[0] <= denials:
                raise PermissionError(13, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    return attempts


def test_probe_state_poll_retries_windows_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path: Path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"rounds": {CANCEL_STARTED: {"cancel_seen": 1}}}),
        encoding="utf-8",
    )
    attempts = _deny_first_reads(monkeypatch, state_path, denials=3)

    state = asyncio.run(
        _wait_for_probe_round(
            state_path, CANCEL_STARTED, field="cancel_seen", minimum=1
        )
    )

    assert state == {"cancel_seen": 1}
    assert attempts[0] == 4


def test_probe_state_poll_names_a_persistent_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path: Path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    _deny_first_reads(monkeypatch, state_path, denials=10**6)

    with pytest.raises(AssertionError, match="last read error: PermissionError"):
        asyncio.run(
            _wait_for_probe_round(
                state_path,
                CANCEL_STARTED,
                field="cancel_seen",
                minimum=1,
                timeout=0.1,
            )
        )


def test_probe_state_replace_retries_windows_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path: Path = tmp_path / "state.json"
    temporary: Path = tmp_path / "state.json.tmp"
    state_path.write_text("old\n", encoding="utf-8")
    temporary.write_text("new\n", encoding="utf-8")
    attempts = [0]
    original = Path.replace

    def replace(self: Path, target: Any) -> Path:
        attempts[0] += 1
        if attempts[0] <= 2:
            raise PermissionError(13, "Permission denied", str(target))
        return original(self, target)

    monkeypatch.setattr(Path, "replace", replace)

    replace_state_file(temporary, state_path)

    assert attempts[0] == 3
    assert state_path.read_text(encoding="utf-8") == "new\n"
    assert not temporary.exists()


def test_probe_state_replace_gives_up_on_a_persistent_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path: Path = tmp_path / "state.json"
    temporary: Path = tmp_path / "state.json.tmp"
    temporary.write_text("new\n", encoding="utf-8")

    def replace(self: Path, target: Any) -> Path:
        raise PermissionError(13, "Permission denied", str(target))

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(mcp_runtime_probe, "_PROBE_WAIT_SECONDS", 0.05)

    with pytest.raises(PermissionError):
        replace_state_file(temporary, state_path)


def test_mcp_stdio_enforces_runtime_security_boundaries(
    indexed_sample: tuple[Path, Path],
) -> None:
    anyio.run(_exercise_runtime_security_boundaries, indexed_sample)


def test_mcp_stdio_metrics_are_per_call_private_and_protocol_clean(
    indexed_sample: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    anyio.run(_exercise_stdio_metrics, indexed_sample, tmp_path / "mcp.jsonl")


def test_mcp_stdio_feedback_is_opt_in_private_durable_and_idempotent(
    indexed_sample: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    anyio.run(
        _exercise_stdio_feedback,
        indexed_sample,
        tmp_path / "private" / "feedback.jsonl",
        tmp_path / "private" / "metrics.jsonl",
    )


async def _exercise_stdio_contract(
    mode: str,
    indexed_sample: tuple[Path, Path],
) -> dict[str, Any]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with Client(
            stdio_client(_server_parameters(indexed_sample), errlog=errlog),
            mode=mode,
            raise_exceptions=True,
            read_timeout_seconds=10,
        ) as client:
            assert client.protocol_version == EXPECTED_PROTOCOLS[mode]
            assert client.server_info is not None
            assert client.server_info.name == "ArcGraph"
            assert client.server_info.version == __version__

            listing = await client.list_tools()
            tool_contract: dict[str, Any] = {}
            for tool in listing.tools:
                tool_contract[tool.name] = {
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "annotations": (
                        tool.annotations.model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_none=True,
                        )
                        if tool.annotations is not None
                        else None
                    ),
                }

            assert list(tool_contract) == list(EXPECTED_TOOL_DESCRIPTIONS)
            for name, expected_description in EXPECTED_TOOL_DESCRIPTIONS.items():
                contract = tool_contract[name]
                assert contract["description"] == expected_description
                schema = contract["input_schema"]
                assert schema["type"] == "object"
                assert set(schema["properties"]) == EXPECTED_TOOL_PROPERTIES[name]
                assert schema.get("required", []) == []
                if "detail_level" in schema["properties"]:
                    assert schema["properties"]["detail_level"]["enum"] == [
                        "summary",
                        "standard",
                        "detailed",
                    ]
                assert contract["annotations"] == {
                    "title": EXPECTED_TOOL_TITLES[name],
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": name == "arcgraph_get_why",
                }

            result_contract: dict[str, Any] = {}
            for name, arguments in TOOL_CALLS.items():
                result = await client.call_tool(name, arguments)
                assert result.is_error is False
                assert result.content
                assert isinstance(result.structured_content, dict)
                payload = result.structured_content
                assert payload["read_only"] is True
                assert payload["source_snippets"]["enabled"] is False
                if name in EXPECTED_CONTRACT_ERRORS:
                    assert payload["status"] == "error"
                    assert payload["error_code"] == EXPECTED_CONTRACT_ERRORS[name]
                elif name == "arcgraph_preview_change_plan":
                    assert payload["status"] == "available"
                    assert payload["verdict"] == "PLAN_READY"
                else:
                    assert payload["status"] in {"available", "partial"}
                result_contract[name] = {
                    "status": payload["status"],
                    "error_code": payload.get("error_code"),
                    "keys": sorted(payload),
                }

        assert _read_errlog(errlog) == ""
    return {
        "tool_contract": tool_contract,
        "result_contract": result_contract,
    }


async def _exercise_baseline_unavailable(
    indexed_sample: tuple[Path, Path],
) -> None:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with Client(
            stdio_client(_server_parameters(indexed_sample), errlog=errlog),
            mode="auto",
            raise_exceptions=True,
            read_timeout_seconds=10,
        ) as client:
            result = await client.call_tool(
                "arcgraph_preview_change_plan",
                TOOL_CALLS["arcgraph_preview_change_plan"],
            )
            assert result.is_error is False
            assert isinstance(result.structured_content, dict)
            payload = result.structured_content
            assert payload["status"] == "error"
            assert payload["error_code"] == "BASELINE_SOURCE_UNAVAILABLE"
            assert payload["read_only"] is True
            assert payload["source_snippets"] == {
                "requested": False,
                "enabled": False,
            }

        assert _read_errlog(errlog) == ""


async def _exercise_worker_thread_concurrency(
    indexed_sample: tuple[Path, Path],
    state_path: Path,
) -> None:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with Client(
            stdio_client(
                _probe_server_parameters(indexed_sample, state_path),
                errlog=errlog,
            ),
            mode="auto",
            raise_exceptions=True,
            read_timeout_seconds=10,
        ) as client:
            context_arguments = {
                **TOOL_CALLS["arcgraph_get_context"],
                "task": CONCURRENT_SAME,
            }
            same_tool_results = await asyncio.gather(
                client.call_tool("arcgraph_get_context", context_arguments),
                client.call_tool("arcgraph_get_context", context_arguments),
            )
            assert all(result.is_error is False for result in same_tool_results)
            assert all(
                isinstance(result.structured_content, dict)
                for result in same_tool_results
            )
            same_state = await _wait_for_probe_round(
                state_path,
                CONCURRENT_SAME,
                field="completed",
                minimum=2,
            )
            _assert_concurrent_round(same_state)

            mixed_context = {
                **TOOL_CALLS["arcgraph_get_context"],
                "task": CONCURRENT_MIXED,
            }
            mixed_explain = {
                **TOOL_CALLS["arcgraph_explain"],
                "task": CONCURRENT_MIXED,
            }
            mixed_tool_results = await asyncio.gather(
                client.call_tool("arcgraph_get_context", mixed_context),
                client.call_tool("arcgraph_explain", mixed_explain),
            )
            assert all(result.is_error is False for result in mixed_tool_results)
            assert all(
                isinstance(result.structured_content, dict)
                for result in mixed_tool_results
            )
            mixed_state = await _wait_for_probe_round(
                state_path,
                CONCURRENT_MIXED,
                field="completed",
                minimum=2,
            )
            _assert_concurrent_round(mixed_state)

        assert _read_errlog(errlog) == ""


async def _exercise_cancellation_and_timeout(
    indexed_sample: tuple[Path, Path],
    state_path: Path,
) -> None:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with Client(
            stdio_client(
                _probe_server_parameters(indexed_sample, state_path),
                errlog=errlog,
            ),
            mode="auto",
            raise_exceptions=True,
            read_timeout_seconds=10,
        ) as client:
            cancel_arguments = {
                **TOOL_CALLS["arcgraph_get_context"],
                "task": CANCEL_STARTED,
            }
            cancelled_call = asyncio.create_task(
                client.call_tool("arcgraph_get_context", cancel_arguments)
            )
            cancel_started_state = await _wait_for_probe_round(
                state_path,
                CANCEL_STARTED,
                field="started",
                minimum=1,
            )
            assert cancel_started_state.get("cancel_seen", 0) == 0
            assert cancelled_call.cancel() is True
            await _wait_for_probe_round(
                state_path,
                CANCEL_STARTED,
                field="cancel_seen",
                minimum=1,
            )
            try:
                await _assert_server_responsive(client)
            finally:
                _release_probe(state_path, CANCEL_STARTED)
            try:
                await cancelled_call
            except asyncio.CancelledError:
                pass
            cancel_state = await _wait_for_probe_round(
                state_path,
                CANCEL_STARTED,
                field="completed",
                minimum=1,
            )
            assert cancel_state["active"] == 0
            assert cancel_state["cancel_seen"] == 1

            timeout_arguments = {
                **TOOL_CALLS["arcgraph_get_context"],
                "task": TIMEOUT_STARTED,
            }
            timed_call = asyncio.create_task(
                client.call_tool(
                    "arcgraph_get_context",
                    timeout_arguments,
                    read_timeout_seconds=1,
                )
            )
            timeout_started_state = await _wait_for_probe_round(
                state_path,
                TIMEOUT_STARTED,
                field="started",
                minimum=1,
            )
            assert timeout_started_state.get("cancel_seen", 0) == 0
            with pytest.raises(MCPError) as timeout_error:
                await timed_call
            assert timeout_error.value.error.code == REQUEST_TIMEOUT
            await _wait_for_probe_round(
                state_path,
                TIMEOUT_STARTED,
                field="cancel_seen",
                minimum=1,
            )
            try:
                await _assert_server_responsive(client)
            finally:
                _release_probe(state_path, TIMEOUT_STARTED)
            timeout_state = await _wait_for_probe_round(
                state_path,
                TIMEOUT_STARTED,
                field="completed",
                minimum=1,
            )
            assert timeout_state["active"] == 0
            assert timeout_state["cancel_seen"] == 1

        assert _read_errlog(errlog) == ""


async def _exercise_runtime_security_boundaries(
    indexed_sample: tuple[Path, Path],
) -> None:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with Client(
            stdio_client(_server_parameters(indexed_sample), errlog=errlog),
            mode="auto",
            raise_exceptions=True,
            read_timeout_seconds=10,
        ) as client:
            status = await client.call_tool(
                "arcgraph_index_status",
                TOOL_CALLS["arcgraph_index_status"],
            )
            assert status.is_error is False
            assert status.structured_content["status"] == "available"
            serialized_status = json.dumps(status.structured_content, sort_keys=True)
            for sensitive in (
                "/etc/passwd",
                "/Users/alice/.ssh/id_rsa",
                "SECRET123",
            ):
                assert sensitive not in serialized_status

            blocked_source = await client.call_tool(
                "arcgraph_get_context",
                {
                    "repo_id": "sample",
                    "targets": ["pkg.service.build_message"],
                    "include_source": True,
                },
            )
            assert blocked_source.is_error is False
            assert blocked_source.structured_content["source_snippets"] == {
                "requested": True,
                "enabled": False,
            }
            assert not _contains_key(blocked_source.structured_content, "snippet")
            assert not _contains_key(
                blocked_source.structured_content,
                "source_snippet",
            )

            invalid_detail = await client.call_tool(
                "arcgraph_explain",
                {
                    "repo_id": "sample",
                    "targets": ["pkg.service.build_message"],
                    "detail_level": "full",
                },
            )
            assert invalid_detail.is_error is True
            assert invalid_detail.structured_content is None

            escaped = await client.call_tool(
                "arcgraph_get_context",
                {"repo_id": "sample", "targets": ["/etc/passwd"]},
            )
            assert escaped.is_error is True
            assert escaped.structured_content is None

            unknown_repo = await client.call_tool(
                "arcgraph_get_context",
                {"repo_id": "unregistered"},
            )
            assert unknown_repo.is_error is True
            assert unknown_repo.structured_content is None

        assert _read_errlog(errlog) == ""


async def _exercise_stdio_metrics(
    indexed_sample: tuple[Path, Path],
    metrics_log: Path,
) -> None:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with Client(
            stdio_client(
                _server_parameters(indexed_sample, metrics_log=metrics_log),
                errlog=errlog,
            ),
            mode="auto",
            raise_exceptions=True,
            read_timeout_seconds=10,
        ) as client:
            listing = await client.list_tools()
            assert [tool.name for tool in listing.tools] == list(
                EXPECTED_TOOL_DESCRIPTIONS
            )

            calls = [
                (
                    "arcgraph_index_status",
                    TOOL_CALLS["arcgraph_index_status"],
                )
                for _ in range(8)
            ]
            calls.extend(
                [
                    (
                        "arcgraph_get_context",
                        TOOL_CALLS["arcgraph_get_context"],
                    )
                    for _ in range(4)
                ]
            )
            results = await asyncio.gather(
                *(client.call_tool(name, arguments) for name, arguments in calls)
            )
            assert all(result.is_error is False for result in results)

            contract_error = await client.call_tool(
                "arcgraph_get_change_plan",
                TOOL_CALLS["arcgraph_get_change_plan"],
            )
            assert contract_error.is_error is False
            assert contract_error.structured_content["status"] == "error"

            permission_error = await client.call_tool(
                "arcgraph_get_context",
                {"repo_id": "sample", "targets": ["/etc/passwd"]},
            )
            assert permission_error.is_error is True

            unknown_tool = await client.call_tool(
                "SECRET_TOOL_/etc/passwd",
                {"target": "SECRET_TARGET"},
            )
            assert unknown_tool.is_error is True

        assert _read_errlog(errlog) == ""

    raw_metrics = metrics_log.read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw_metrics.splitlines() if line.strip()]
    expected_keys = {
        "timestamp",
        "tool_name",
        "status",
        "duration_ms",
        "payload_bytes",
        "estimated_tokens",
        "truncated",
        "freshness_status",
    }
    assert len(events) == 15
    assert all(set(event) == expected_keys for event in events)
    assert sum(event["status"] == "success" for event in events) == 12
    assert sum(event["status"] == "error" for event in events) == 3
    assert all(event["duration_ms"] >= 0 for event in events)
    assert all(event["payload_bytes"] >= 0 for event in events)
    assert all(event["estimated_tokens"] >= 0 for event in events)
    assert all(isinstance(event["truncated"], bool) for event in events)
    assert all(
        event["freshness_status"] in {"fresh", "stale", "unknown", "not_reported"}
        for event in events
    )
    assert all(
        event["freshness_status"] == "fresh"
        for event in events
        if event["tool_name"] in {"arcgraph_index_status", "arcgraph_get_context"}
        and event["status"] == "success"
    )
    assert all(
        event["payload_bytes"] > 0 and event["estimated_tokens"] > 0
        for event in events
        if event["status"] == "success"
    )
    contract_error_events = [
        event for event in events if event["tool_name"] == "arcgraph_get_change_plan"
    ]
    assert len(contract_error_events) == 1
    assert contract_error_events[0]["status"] == "error"
    assert contract_error_events[0]["payload_bytes"] > 0
    assert (
        len(
            [
                event
                for event in events
                if event["status"] == "error" and event["payload_bytes"] == 0
            ]
        )
        == 2
    )
    assert len([event for event in events if event["tool_name"] == "unknown_tool"]) == 1
    for sensitive in (
        "sample",
        "pkg.service.build_message",
        "/etc/passwd",
        "SECRET123",
        "SECRET_TOOL_",
        "SECRET_TARGET",
        str(indexed_sample[0]),
        str(indexed_sample[1]),
        "repo_id",
        "targets",
    ):
        assert sensitive not in raw_metrics


async def _exercise_stdio_feedback(
    indexed_sample: tuple[Path, Path],
    feedback_log: Path,
    metrics_log: Path,
) -> None:
    event_id = str(uuid.uuid4())
    arguments = {
        "client_event_id": event_id,
        "surface": "mcp",
        "tool_name": "arcgraph_help",
        "outcome": "partial",
        "issue_kind": "discoverability",
        "stage": "preflight",
        "fallback": "none",
        "result_status": "available",
        "freshness_status": "fresh",
        "warning_kinds": ["stale_index"],
    }
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with Client(
            stdio_client(
                _server_parameters(
                    indexed_sample,
                    metrics_log=metrics_log,
                    feedback_log=feedback_log,
                ),
                errlog=errlog,
            ),
            mode="auto",
            raise_exceptions=True,
            read_timeout_seconds=10,
        ) as client:
            listing = await client.list_tools()
            names = [tool.name for tool in listing.tools]
            assert names == [
                *EXPECTED_TOOL_DESCRIPTIONS,
                "arcgraph_record_trial_feedback",
            ]
            help_tool = next(
                tool for tool in listing.tools if tool.name == "arcgraph_help"
            )
            assert help_tool.input_schema["properties"]["topic"]["enum"] == list(
                HELP_TOPICS
            )
            feedback_tool = next(
                tool
                for tool in listing.tools
                if tool.name == "arcgraph_record_trial_feedback"
            )
            assert feedback_tool.description == (
                "Record one privacy-bounded Agent trial feedback event in an "
                "explicitly enabled local append-only log."
            )
            assert set(feedback_tool.input_schema["properties"]) == {"feedback"}
            assert feedback_tool.input_schema["required"] == ["feedback"]
            feedback_schema = feedback_tool.input_schema["properties"]["feedback"]
            assert feedback_schema["additionalProperties"] is False
            assert set(feedback_schema["properties"]) == (
                EXPECTED_FEEDBACK_EVENT_PROPERTIES
            )
            assert set(feedback_schema["required"]) == (EXPECTED_FEEDBACK_REQUIRED)
            assert feedback_schema["properties"]["warning_kinds"]["items"][
                "enum"
            ] == list(FEEDBACK_WARNING_KINDS)
            assert (
                feedback_schema["properties"]["warning_kinds"]["maxItems"]
                == MAX_WARNING_KINDS
                == len(FEEDBACK_WARNING_KINDS)
            )
            assert feedback_tool.annotations is not None
            assert feedback_tool.annotations.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ) == {
                "title": "ArcGraph Record Trial Feedback",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            }
            assert not feedback_log.exists()

            status_before = await client.call_tool(
                "arcgraph_index_status",
                TOOL_CALLS["arcgraph_index_status"],
            )
            help_result = await client.call_tool(
                "arcgraph_help",
                {"topic": "feedback"},
            )
            assert help_result.structured_content["feedback"]["enabled"] is True
            assert help_result.structured_content["feedback"]["cli_available"] is True
            assert help_result.structured_content["inventory"]["mcp_tool_count"] == 15

            rejected = await client.call_tool(
                "arcgraph_record_trial_feedback",
                {"feedback": {**arguments, "message": "/private/SECRET_TARGET"}},
            )
            assert rejected.is_error is False
            assert rejected.structured_content["status"] == "error"
            assert (
                rejected.structured_content["error_code"]
                == "TRIAL_FEEDBACK_INPUT_INVALID"
            )
            assert "/private/SECRET_TARGET" not in rejected.model_dump_json()
            assert not feedback_log.exists()

            recorded = await client.call_tool(
                "arcgraph_record_trial_feedback",
                {"feedback": arguments},
            )
            duplicate = await client.call_tool(
                "arcgraph_record_trial_feedback",
                {"feedback": arguments},
            )
            conflicting = await client.call_tool(
                "arcgraph_record_trial_feedback",
                {
                    "feedback": {
                        **arguments,
                        "issue_kind": "incorrect_result",
                    }
                },
            )

            assert recorded.is_error is False
            assert recorded.structured_content["status"] == "recorded"
            assert recorded.structured_content["duplicate"] is False
            assert duplicate.is_error is False
            assert duplicate.structured_content["duplicate"] is True
            assert (
                duplicate.structured_content["feedback_id"]
                == recorded.structured_content["feedback_id"]
            )
            assert conflicting.is_error is False
            assert conflicting.structured_content["status"] == "error"
            assert (
                conflicting.structured_content["error_code"]
                == "TRIAL_FEEDBACK_ID_CONFLICT"
            )
            status_after = await client.call_tool(
                "arcgraph_index_status",
                TOOL_CALLS["arcgraph_index_status"],
            )
            assert status_after.structured_content == status_before.structured_content

        assert _read_errlog(errlog) == ""

    raw_feedback = feedback_log.read_text(encoding="utf-8")
    raw_metrics = metrics_log.read_text(encoding="utf-8")
    assert len(raw_feedback.splitlines()) == 1
    for forbidden in (
        "/private/SECRET_TARGET",
        str(indexed_sample[0]),
        str(indexed_sample[1]),
        "repo_id",
        "targets",
        "message",
    ):
        assert forbidden not in raw_feedback
    assert event_id not in raw_metrics
    assert "feedback_id" not in raw_metrics
    assert '"duration_ms"' not in raw_feedback
    assert '"estimated_tokens"' not in raw_feedback


def _server_parameters(
    indexed_sample: tuple[Path, Path],
    *,
    metrics_log: Path | None = None,
    feedback_log: Path | None = None,
) -> StdioServerParameters:
    repo_root, output_dir = indexed_sample
    args = [
        "-m",
        "arcgraph.interfaces.mcp_server",
        "--repo-root",
        str(repo_root),
        "--output-dir",
        str(output_dir),
        "--repo-id",
        "sample",
    ]
    if metrics_log is not None:
        args.extend(["--metrics-log", str(metrics_log)])
    if feedback_log is not None:
        args.extend(["--feedback-log", str(feedback_log)])
    return StdioServerParameters(
        command=sys.executable,
        args=args,
        cwd=output_dir.parent,
    )


def _probe_server_parameters(
    indexed_sample: tuple[Path, Path],
    state_path: Path,
) -> StdioServerParameters:
    repo_root, output_dir = indexed_sample
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "arcgraph.tests.mcp_runtime_probe",
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(output_dir),
            "--repo-id",
            "sample",
            "--state-path",
            str(state_path),
        ],
        cwd=output_dir.parent,
    )


async def _wait_for_probe_round(
    state_path: Path,
    marker: str,
    *,
    field: str,
    minimum: int,
    timeout: float = 5,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    last_state: dict[str, Any] = {}
    last_error: OSError | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            await asyncio.sleep(0.01)
            continue
        except PermissionError as exc:
            # Windows denies opening the file while the probe server replaces it.
            last_error = exc
            await asyncio.sleep(0.01)
            continue
        rounds = state.get("rounds")
        if isinstance(rounds, dict):
            round_state = rounds.get(marker)
            if isinstance(round_state, dict):
                last_state = round_state
                value = round_state.get(field, 0)
                if isinstance(value, int) and value >= minimum:
                    return round_state
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"MCP runtime probe {marker!r} did not reach {field}>={minimum}: "
        f"{last_state!r}; last read error: {last_error!r}"
    )


def _assert_concurrent_round(state: dict[str, Any]) -> None:
    assert state["started"] == 2
    assert state["completed"] == 2
    assert state["active"] == 0
    assert state["max_active"] == 2
    assert len(state["thread_ids"]) == 2
    assert state.get("barrier_failed") is not True


async def _assert_server_responsive(client: Client) -> None:
    status = await asyncio.wait_for(
        client.call_tool(
            "arcgraph_index_status",
            TOOL_CALLS["arcgraph_index_status"],
        ),
        timeout=2,
    )
    assert status.is_error is False
    assert status.structured_content["status"] == "available"


def _release_probe(state_path: Path, marker: str) -> None:
    probe_release_path(state_path, marker).write_text(
        "release\n",
        encoding="utf-8",
    )


def _read_errlog(errlog: Any) -> str:
    errlog.seek(0)
    return str(errlog.read())


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _initialize_git_repository(repo_root: Path, *, commit: bool) -> None:
    _git(repo_root, "init")
    _git(repo_root, "config", "core.autocrlf", "false")
    _git(repo_root, "config", "core.hooksPath", ".git/hooks-disabled")
    if not commit:
        return
    _git(repo_root, "config", "user.email", "mcp-protocol@example.invalid")
    _git(repo_root, "config", "user.name", "ArcGraph MCP Protocol Tests")
    _git(repo_root, "config", "commit.gpgsign", "false")
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", "baseline")


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
