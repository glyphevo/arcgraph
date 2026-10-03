from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from arcgraph.core.payload_policy import TargetRequestLimitError
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.schemas import (
    ContextRequest,
    ExplainResponse,
    READ_SCHEMA_VERSION,
    RiskReport,
    SCHEMA_VERSION,
    WhyReport,
)
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import main
from arcgraph.interfaces.agent_capabilities import (
    mcp_capability_names,
    mcp_capability_specs,
)
from arcgraph.interfaces.mcp_tools import (
    ArcGraphMCPConfig,
    ArcGraphMCPToolGroup,
    ArcGraphPermissionError,
    _entrypoint_query,
    _is_any_os_absolute,
    _looks_like_path,
    _sanitize_path_value,
    register_arcgraph_mcp_tools,
)
from arcgraph.providers.context_provider import ContextProvider

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
EXPLAIN_TARGET = "pkg.service.build_message"


def test_entrypoint_method_path_query_keeps_route_alias_expansion() -> None:
    assert (
        _entrypoint_query(entrypoint=None, method="post", path="gateway")
        == "POST /gateway"
    )
    assert _looks_like_path("GET /../../admin") is False
    # ANY (Django, Next.js) and ALL (Express app.all) are two stored
    # spellings of the same any-method claim: each wildcard query must also
    # match the other spelling.
    any_aliases = QueryEngine._entrypoint_aliases("ANY /gateway")
    all_aliases = QueryEngine._entrypoint_aliases("ALL /gateway")
    assert "route:ANY:/gateway" in any_aliases
    assert "route:ALL:/gateway" in any_aliases
    assert "route:ALL:/gateway" in all_aliases
    assert "route:ANY:/gateway" in all_aliases


def test_mcp_tool_group_smoke_context_risk_why_and_flow(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
            allowed_roots=[FIXTURE_ROOT.parent],
        )
    )

    status = tools.arcgraph_index_status(repo_id="sample")
    context = tools.arcgraph_get_context(
        repo_id="sample",
        task="change memory creation",
        targets=["pkg.service.MemoryService.create_memory"],
    )
    context_with_source_request = tools.arcgraph_get_context(
        repo_id="sample",
        targets=["pkg.service.MemoryService.create_memory"],
        include_source=True,
    )
    explain = tools.arcgraph_explain(
        repo_id="sample",
        task="review message building",
        targets=[EXPLAIN_TARGET],
        detail_level="detailed",
    )
    risk = tools.arcgraph_get_risk(
        repo_id="sample", targets=["pkg.service.MemoryService.create_memory"]
    )
    assert risk["max_results"] == 3
    flow = tools.arcgraph_entrypoint_flow(
        repo_id="sample", method="POST", path="/memories"
    )
    missing_flow = tools.arcgraph_entrypoint_flow(
        repo_id="sample", method="GET", path="../../admin"
    )
    why = tools.arcgraph_get_why(
        repo_id="sample", target="pkg.service.MemoryService.create_memory"
    )
    similar = tools.arcgraph_find_similar(
        repo_id="sample", target="pkg.service.MemoryService.create_memory"
    )
    learning = tools.arcgraph_record_learning(
        repo_id="sample",
        target="pkg.service.MemoryService.create_memory",
        observation="Memory creation changes should inspect route, worker, and repository impact.",
    )

    assert status["status"] == "available"
    assert context["status"] == "available"
    assert context["confidence_profile"] == "review_default"
    assert context["estimated_tokens"] > 0
    assert context["truncation"]["max_results"] == 30
    assert context["grouped_effects"]["confirmed"]
    assert context["unresolved_risks"] == []
    assert context["source_snippets"] == {"requested": False, "enabled": False}
    assert ExplainResponse.model_validate(explain)
    assert explain["repo_id"] == "sample"
    assert explain["read_only"] is True
    assert explain["status"] == "available"
    assert explain["detail_level"] == "detailed"
    assert explain["estimated_tokens"] < 8000
    assert context_with_source_request["source_snippets"] == {
        "requested": True,
        "enabled": False,
    }
    assert any(
        "Source snippets were requested but are disabled" in warning
        for warning in context_with_source_request["warnings"]
    )
    assert "route:POST:/memories" in {
        node["id"] for node in risk["blast_radius"]["entrypoints"]
    }
    assert risk["test_gaps"]
    assert any(item["kind"] == "test_gap" for item in risk["risk_factors"])
    assert context["test_gaps"]
    assert flow["entrypoints"][0]["id"] == "route:POST:/memories"
    assert missing_flow["entrypoints"] == []
    assert why["external_memory"]["status"] == "unavailable"
    assert why["structural_why"]
    assert RiskReport.model_validate(risk)
    assert WhyReport.model_validate(why)
    assert similar["status"] == "available"
    assert "similarity" not in similar["capabilities"]
    assert similar["capabilities_summary"]["omitted_value"] == "available"
    assert learning["mode"] == "proposal-only"
    assert learning["read_only"] is True
    assert not _contains_key(context, "snippet")
    assert not _contains_key(context, "source_snippet")
    assert not _contains_key(explain, "properties")

    for payload in (
        context,
        explain,
        risk,
        flow,
        missing_flow,
        why,
        similar,
        learning,
    ):
        assert payload["schema_version"] == READ_SCHEMA_VERSION
        assert payload["index_schema_version"] == SCHEMA_VERSION
        assert payload["capabilities_summary"]["reported"] == len(
            payload["capabilities"]
        )
        assert all(value != "available" for value in payload["capabilities"].values())
    assert status["schema_version"] == SCHEMA_VERSION
    assert "index_schema_version" not in status
    assert not _contains_key(explain, "snippet")
    assert not _contains_key(explain, "source_snippet")


def test_mcp_explain_matches_cli_core_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
            allowed_roots=[FIXTURE_ROOT.parent],
        )
    )

    cli_exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "explain",
            EXPLAIN_TARGET,
            "--task",
            "review message building",
            "--detail-level",
            "standard",
        ]
    )
    assert cli_exit_code == 0
    cli_payload = json.loads(capsys.readouterr().out)
    cli_explain = ExplainResponse.model_validate(cli_payload)

    mcp_payload = tools.arcgraph_explain(
        repo_id="sample",
        task="review message building",
        targets=[EXPLAIN_TARGET],
        detail_level="standard",
    )
    mcp_explain = ExplainResponse.model_validate(mcp_payload)
    incoming_edge = mcp_explain.explanations[0]["incoming_edges"][0]

    assert incoming_edge["evidence"]
    assert incoming_edge["resolution"]["strategy"]
    assert "confidence_sources" in incoming_edge
    assert "properties" not in incoming_edge
    assert mcp_explain.repo_id == "sample"
    assert mcp_explain.read_only is True
    assert mcp_explain.model_dump(exclude={"repo_id", "read_only"}) == (
        cli_explain.model_dump(exclude={"repo_id", "read_only"})
    )


def test_mcp_explain_source_policy_and_detail_fallback(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    default_tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
            allowed_roots=[FIXTURE_ROOT.parent],
        )
    )
    source_tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
            allowed_roots=[FIXTURE_ROOT.parent],
            expose_source_snippets=True,
        )
    )

    blocked = default_tools.arcgraph_explain(
        repo_id="sample",
        targets=[EXPLAIN_TARGET],
        include_source=True,
    )
    exposed = source_tools.arcgraph_explain(
        repo_id="sample",
        targets=[EXPLAIN_TARGET],
        include_source=True,
    )
    with pytest.raises(ValueError, match="detail_level"):
        default_tools.arcgraph_explain(
            repo_id="sample", targets=[EXPLAIN_TARGET], detail_level="full"
        )

    assert blocked["source_snippets"] == {"requested": True, "enabled": False}
    assert any(
        "Source snippets were requested but are disabled" in warning
        for warning in blocked["warnings"]
    )
    assert not _contains_key(blocked, "snippet")
    assert not _contains_key(blocked, "source_snippet")
    assert exposed["source_snippets"] == {"requested": True, "enabled": True}
    sanitized = source_tools._sanitize_payload(
        {
            "schema_version": "1.0.0",
            "status": "available",
            "targets": [EXPLAIN_TARGET],
            "explanations": [
                {
                    "incoming_edges": [
                        {
                            "evidence": [
                                {
                                    "path": "src/pkg/service.py",
                                    "snippet": "def build_message(value: str) -> str:",
                                }
                            ]
                        }
                    ]
                }
            ],
        },
        repo=source_tools._resolve_repo("sample"),
        repo_id="sample",
        include_source=True,
    )
    assert _contains_key(sanitized, "snippet")


def test_mcp_permission_rejects_unregistered_repo_and_path_escape(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
            allowed_roots=[FIXTURE_ROOT.parent],
        )
    )

    with pytest.raises(ArcGraphPermissionError):
        tools.arcgraph_index_status(repo_id="missing")

    with pytest.raises(ArcGraphPermissionError):
        tools.arcgraph_get_context(repo_id="sample", targets=["..\\outside\\secret.py"])

    with pytest.raises(ArcGraphPermissionError):
        tools.arcgraph_explain(repo_id="sample", targets=["..\\outside\\secret.py"])


def test_mcp_registration_uses_tool_group_handlers(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(FIXTURE_ROOT, output_dir=output_dir)
    )
    fake_mcp = _FakeMCP()

    register_arcgraph_mcp_tools(fake_mcp, tools)

    assert tuple(fake_mcp.handlers) == tuple(
        spec.name for spec in mcp_capability_specs()
    )
    assert tuple(fake_mcp.tool_metadata) == tuple(fake_mcp.handlers)
    for spec in mcp_capability_specs():
        metadata = fake_mcp.tool_metadata[spec.name]
        assert metadata["title"] == spec.title
        assert metadata["description"] == spec.description
        annotations = metadata["annotations"]
        assert annotations["readOnlyHint"] is spec.behavior.read_only
        assert annotations["destructiveHint"] is spec.behavior.destructive
        assert annotations["idempotentHint"] is spec.behavior.idempotent
        assert annotations["openWorldHint"] is spec.behavior.open_world

    payload = fake_mcp.handlers["arcgraph_get_context"](
        targets=["pkg.service.MemoryService.create_memory"]
    )
    assert payload["repo_id"] == "default"
    assert payload["status"] == "available"
    explain = fake_mcp.handlers["arcgraph_explain"](targets=[EXPLAIN_TARGET])
    assert explain["repo_id"] == "default"
    assert explain["status"] == "available"


def test_mcp_registration_does_not_import_the_optional_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = ArcGraphMCPToolGroup(ArcGraphMCPConfig.for_single_repo(tmp_path))
    fake_mcp = _FakeMCP()
    real_import = builtins.__import__

    def reject_mcp_import(
        name: str,
        globals: object = None,
        locals: object = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError("blocked optional MCP runtime", name=name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_mcp_import)

    register_arcgraph_mcp_tools(fake_mcp, tools)

    assert tuple(fake_mcp.handlers) == mcp_capability_names()


def test_mcp_facade_has_no_core_sql_query_logic() -> None:
    source = (
        Path(__file__)
        .parents[1]
        .joinpath("interfaces", "mcp_tools.py")
        .read_text(encoding="utf-8")
    )

    assert "SELECT " not in source
    assert ".connect(" not in source


class _FakeMCP:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.tool_metadata: dict[str, dict[str, object]] = {}

    def tool(
        self,
        *,
        name: str,
        title: str,
        description: str,
        annotations: object,
    ) -> object:
        assert title
        assert description
        self.tool_metadata[name] = {
            "title": title,
            "description": description,
            "annotations": annotations,
        }

        def decorator(func: object) -> object:
            self.handlers[name] = func
            return func

        return decorator


def test_is_any_os_absolute_detects_all_styles() -> None:
    # POSIX absolute
    assert _is_any_os_absolute("/home/alice/repo") is True
    assert _is_any_os_absolute("/etc/passwd") is True
    # Windows drive letter
    assert _is_any_os_absolute("C:\\Users\\alice\\repo") is True
    assert _is_any_os_absolute("D:/Projects/foo") is True
    # UNC
    assert _is_any_os_absolute("\\\\server\\share\\file") is True
    # Relative — should NOT be detected
    assert _is_any_os_absolute("src/foo.py") is False
    assert _is_any_os_absolute("backend\\models.py") is False
    assert _is_any_os_absolute("") is False


def test_sanitize_path_value_rejects_foreign_os_absolute(tmp_path: Path) -> None:
    """Cross-OS safety: a Windows path on POSIX (or vice versa) must be <external>.

    On POSIX, ``Path("C:\\\\Users\\\\x")`` is treated as a relative path.  Without
    the cross-OS guard, it would resolve *under* ``repo_root`` and leak through.
    This test verifies the guard regardless of the host OS.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    # Relative project paths are fine
    assert _sanitize_path_value("src/foo.py", repo_root) == "src/foo.py"
    assert _sanitize_path_value("backend/models.py", repo_root) == "backend/models.py"

    import os

    if os.name != "nt":
        # On POSIX: Windows-style absolute paths must be rejected
        assert (
            _sanitize_path_value("C:\\Users\\alice\\secret.py", repo_root)
            == "<external>"
        )
        assert _sanitize_path_value("D:\\Projects\\foo.py", repo_root) == "<external>"
        assert (
            _sanitize_path_value("\\\\server\\share\\file", repo_root) == "<external>"
        )
    else:
        # On Windows: POSIX absolute paths must be rejected
        assert _sanitize_path_value("/home/alice/secret.py", repo_root) == "<external>"
        assert _sanitize_path_value("/etc/passwd", repo_root) == "<external>"

    # Traversal via .. must be rejected
    assert _sanitize_path_value("../../../etc/passwd", repo_root) == "<external>"

    # Empty string is passed through
    assert _sanitize_path_value("", repo_root) == ""


def test_mcp_sanitizes_paths_embedded_in_real_provider_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    provider = ContextProvider(QueryEngine(output_dir))
    current = provider.query_engine.current()
    external_paths = (
        "/Users/alice/private/runtime-trace.json",
        r"C:\Build\private\runtime-trace.json",
        r"\\server\share\private\runtime-trace.json",
    )
    current["warnings"] = [
        {
            "kind": "runtime_trace_unavailable",
            "path": path,
            "message": f"Could not read runtime trace {path}",
        }
        for path in external_paths
    ]
    synthetic_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c2lnbmF0dXJl"
    current["warnings"].extend(
        [
            f"received {synthetic_token}",
            "Bearer synthetic-test-credential",
            "password=synthetic-test-password",
            r"runner failed at D:\Projects\private\trace.json",
            r"runner failed at \\server\share\private\trace.json",
        ]
    )
    monkeypatch.setattr(provider.query_engine, "current", lambda: current)
    provider_payload = provider.get_context(ContextRequest())

    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
            allowed_roots=[FIXTURE_ROOT.parent],
        )
    )
    repo = tools._resolve_repo("sample")
    sanitized = tools._sanitize_payload(
        provider_payload,
        repo=repo,
        repo_id="sample",
        include_source=False,
        target_scoped=True,
    )
    serialized = json.dumps(sanitized, sort_keys=True)

    for secret in (
        synthetic_token,
        "synthetic-test-credential",
        "synthetic-test-password",
    ):
        assert secret not in serialized
    for raw in external_paths:
        assert json.dumps(raw)[1:-1] not in serialized
    assert json.dumps(r"D:\Projects\private\trace.json")[1:-1] not in serialized
    assert json.dumps(r"\\server\share\private\trace.json")[1:-1] not in serialized
    assert "<external>" in serialized or "<redacted-user-path>" in serialized


def test_mcp_rejects_target_fanout_above_its_configured_limit(
    tmp_path: Path,
) -> None:
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            tmp_path,
            max_targets_limit=2,
        )
    )

    with pytest.raises(TargetRequestLimitError, match="at most 2"):
        tools.arcgraph_get_context(targets=["fn:one", "fn:two", "fn:three"])


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_mcp_qualified_caller_ids_remain_distinct_and_resolve(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    (repo / "registry.py").write_text(
        "def get_all_channels():\n    return []\n", encoding="utf-8"
    )
    callers = {
        "test_channels": ("TestChannelRegistry", "test_all_channels_registered"),
        "test_doctor": ("TestDoctor", "test_check_all_collects_channel_results"),
    }
    for module, (cls, method) in callers.items():
        (tests / f"{module}.py").write_text(
            f"from registry import get_all_channels\n\nclass {cls}:\n"
            f"    def {method}(self):\n        return get_all_channels()\n",
            encoding="utf-8",
        )
    output = tmp_path / "index"
    ArcGraphIndexer(
        repo_root=repo,
        output_dir=output,
        source_roots=[SourceRoot("."), SourceRoot("tests", "tests")],
    ).build()
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(repo, output_dir=output)
    )
    risk = tools.arcgraph_get_risk(
        targets=["registry.get_all_channels"], max_results=100
    )
    expected = {
        f"method:tests.{module}.{cls}.{method}"
        for module, (cls, method) in callers.items()
    }
    returned = {node["id"] for node in risk["blast_radius"]["affected_symbols"]}
    assert expected <= returned
    for node_id in expected:
        followup = tools.arcgraph_explain(targets=[node_id])
        assert followup["status"] == "available"
        assert followup["explanations"][0]["resolved_targets"] == [node_id]
