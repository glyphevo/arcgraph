from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest
from pydantic import ValidationError

from arcgraph.core.payload_policy import (
    MAX_TARGETS_PER_REQUEST,
    TARGET_SCOPED_CLI_COMMANDS,
    TargetRequestLimitError,
)
from arcgraph.core.schemas import ContextResponse
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.schemas import (
    ContextRequest,
    ExplainResponse,
    READ_SCHEMA_VERSION,
    RiskReport,
    SCHEMA_VERSION,
)
from arcgraph.interfaces.cli import main
from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.providers.context_provider import ContextProvider, _is_entrypoint_target
from arcgraph.providers.risk_provider import RiskProvider

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
TARGET = "pkg.service.MemoryService.create_memory"


@pytest.mark.parametrize(
    "target",
    ["GET /users", "ALL /gateway", "route:POST:/items"],
)
def test_context_provider_recognizes_entrypoint_aliases(target: str) -> None:
    assert _is_entrypoint_target(target) is True


@pytest.mark.parametrize(
    "target",
    ["get all users", "all handlers calling X", "GET", "any"],
)
def test_context_provider_keeps_prose_targets_out_of_entrypoints(target: str) -> None:
    assert _is_entrypoint_target(target) is False


def test_cli_context_returns_canonical_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "context",
            TARGET,
            "--task",
            "review memory creation",
            "--detail-level",
            "detailed",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    validated = ContextResponse.model_validate(payload)

    assert validated.status == "available"
    assert validated.task == "review memory creation"
    assert validated.targets == [TARGET]
    assert validated.detail_level == "detailed"
    assert validated.confidence_profile == "review_default"
    assert validated.source_snippets == {"requested": False, "enabled": False}
    assert validated.grouped_effects["confirmed"]
    assert validated.test_gaps
    assert validated.estimated_tokens > 0
    assert validated.estimated_tokens < 8000
    assert not _contains_key(payload["symbols"], "properties")
    assert not _contains_key(payload["impact"], "properties")
    assert not _contains_key(payload, "snippet")
    assert not _contains_key(payload, "source_snippet")


def test_cli_context_include_source_sets_snippet_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "context",
            TARGET,
            "--include-source",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    validated = ContextResponse.model_validate(payload)

    assert validated.source_snippets == {"requested": True, "enabled": True}


def test_cli_context_rejects_full_detail_level(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--repo-root",
                str(FIXTURE_ROOT),
                "--output-dir",
                str(output_dir),
                "context",
                TARGET,
                "--detail-level",
                "full",
            ]
        )

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_and_mcp_context_share_canonical_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    cli_exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "context",
            TARGET,
            "--task",
            "review memory creation",
            "--detail-level",
            "standard",
        ]
    )
    assert cli_exit_code == 0
    cli_payload = json.loads(capsys.readouterr().out)
    cli_context = ContextResponse.model_validate(cli_payload)

    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            FIXTURE_ROOT,
            repo_id="sample",
            output_dir=output_dir,
            allowed_roots=[FIXTURE_ROOT.parent],
        )
    )
    mcp_payload = tools.arcgraph_get_context(
        repo_id="sample",
        task="review memory creation",
        targets=[TARGET],
        detail_level="standard",
    )
    mcp_context = ContextResponse.model_validate(mcp_payload)

    assert mcp_context.repo_id == "sample"
    assert mcp_context.read_only is True
    assert mcp_context.model_dump(exclude={"repo_id", "read_only"}) == (
        cli_context.model_dump(exclude={"repo_id", "read_only"})
    )


def test_context_provider_preserves_structured_current_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = _build_fixture_index(tmp_path)
    provider = ContextProvider(QueryEngine(output_dir))
    current = provider.query_engine.current()
    current["warnings"] = [{"kind": "stale", "path": "src/pkg/service.py"}]
    monkeypatch.setattr(provider.query_engine, "current", lambda: current)

    request = ContextRequest(targets=[TARGET], detail_level="summary")
    context = ContextResponse.model_validate(provider.get_context(request))
    explain = ExplainResponse.model_validate(provider.explain(request))

    assert context.warnings
    assert explain.warnings
    assert any(isinstance(warning, dict) for warning in context.warnings)
    assert any(isinstance(warning, dict) for warning in explain.warnings)


def _build_fixture_index(tmp_path: Path) -> Path:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    return output_dir


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _warnings_text(warnings: list[object]) -> str:
    return "\n".join(
        (
            warning
            if isinstance(warning, str)
            else json.dumps(warning, sort_keys=True, ensure_ascii=False)
        )
        for warning in warnings
    )


UNRELATED_WARNING = {
    "kind": "typescript_import_unresolved",
    "message": "TypeScript import @scope/missing has no indexed file.",
    "path": "web/src/unrelated.ts",
}
PATHLESS_WARNING = (
    "Impact combines call, import, entrypoint, and resource facts; dynamic "
    "dispatch remains best-effort."
)


def _provider_with_index_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warnings: list[object]
) -> ContextProvider:
    output_dir = _build_fixture_index(tmp_path)
    provider = ContextProvider(QueryEngine(output_dir))
    current = provider.query_engine.current()
    current["warnings"] = warnings
    monkeypatch.setattr(provider.query_engine, "current", lambda: current)
    return provider


def test_index_warnings_unrelated_to_targets_are_folded_not_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warnings about files the answer never mentions must not be copied in.

    Before this, `current()["warnings"]` was seeded verbatim into every
    target-scoped payload: about 1000 tokens per call, byte-identical across
    unrelated targets, so it carried no information about the target.
    """
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [UNRELATED_WARNING] * 3
    )
    request = ContextRequest(targets=[TARGET], detail_level="summary")

    for payload in (provider.get_context(request), provider.explain(request)):
        joined = _warnings_text(payload["warnings"])
        assert "web/src/unrelated.ts" not in joined
        # Folded, not dropped: the count and kind stay recoverable.
        assert "index_warnings_omitted" in joined
        assert '"typescript_import_unresolved": 3' in joined


def test_index_warnings_touching_the_target_are_kept_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scoping filter must not swallow a warning that is actually relevant.

    This is the failure mode that matters: a filter is easy to write so that it
    drops everything and every assertion still passes.
    """
    relevant = {
        "kind": "stale_source",
        "message": "Source changed after the last build.",
        "path": "src/pkg/service.py",
    }
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [relevant, UNRELATED_WARNING]
    )
    request = ContextRequest(targets=[TARGET], detail_level="summary")

    for payload in (provider.get_context(request), provider.explain(request)):
        joined = _warnings_text(payload["warnings"])
        assert "src/pkg/service.py" in joined
        assert "Source changed after the last build." in joined
        assert "web/src/unrelated.ts" not in joined


def test_pathless_index_warnings_are_always_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capability caveats carry no path and qualify any answer, so they stay."""
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [PATHLESS_WARNING, UNRELATED_WARNING]
    )
    request = ContextRequest(targets=[TARGET], detail_level="summary")

    for payload in (provider.get_context(request), provider.explain(request)):
        assert PATHLESS_WARNING in payload["warnings"]


def test_index_status_still_reports_every_index_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoping is per-target only; the whole-index view stays complete."""
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [UNRELATED_WARNING, PATHLESS_WARNING]
    )

    status = provider.index_status()

    assert status["warnings"] == [UNRELATED_WARNING, PATHLESS_WARNING]


def test_relation_and_impact_payloads_scope_index_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same scoping applies to callers/callees/impact, not just context."""
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [UNRELATED_WARNING] * 2
    )
    request = ContextRequest(targets=[TARGET], detail_level="summary")

    payloads = [
        provider.compact_callers(TARGET, request),
        provider.compact_callees(TARGET, request),
        provider.compact_impact(TARGET, request),
    ]

    for payload in payloads:
        joined = _warnings_text(payload["warnings"])
        assert "web/src/unrelated.ts" not in joined
        assert '"typescript_import_unresolved": 2' in joined


def test_target_payloads_report_only_capabilities_that_qualify_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fully available capabilities place no caveat, so they are not repeated.

    On a minimal missing-symbol response, the complete table is roughly two
    thirds of the serialized payload while saying nothing an agent can act on.
    """
    output_dir = _build_fixture_index(tmp_path)
    provider = ContextProvider(QueryEngine(output_dir))
    current = provider.query_engine.current()
    current["capabilities"] = {
        "calls": "available",
        "imports": "available",
        "coverage": "unavailable",
        "test_recommendations": "heuristic",
    }
    monkeypatch.setattr(provider.query_engine, "current", lambda: current)
    request = ContextRequest(targets=[TARGET], detail_level="summary")

    for payload in (
        provider.get_context(request),
        provider.explain(request),
        provider.compact_callers(TARGET, request),
        provider.compact_callees(TARGET, request),
        provider.compact_impact(TARGET, request),
    ):
        capabilities = payload["capabilities"]
        # Degraded entries are the ones an agent must see.
        assert capabilities["coverage"] == "unavailable"
        assert capabilities["test_recommendations"] == "heuristic"
        # Fully available ones are omitted, and the omission is self-describing
        # so "absent" can never be read as "unknown".
        assert "calls" not in capabilities
        assert "imports" not in capabilities
        summary = payload["capabilities_summary"]
        assert summary["reported"] == len(capabilities)
        assert summary["total"] >= summary["reported"]
        assert summary["omitted_value"] == "available"


def test_index_status_still_reports_every_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoping is per-target; the whole-index capability table stays complete."""
    output_dir = _build_fixture_index(tmp_path)
    provider = ContextProvider(QueryEngine(output_dir))
    full = {"calls": "available", "coverage": "unavailable"}
    current = provider.query_engine.current()
    current["capabilities"] = full
    monkeypatch.setattr(provider.query_engine, "current", lambda: current)

    assert provider.index_status()["capabilities"] == full


def test_every_degraded_capability_value_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the exact string "available" may be treated as needing no caveat.

    ``basic``, ``heuristic``, ``partial`` and friends all describe a limit on
    the answer, so a filter that keyed off "anything truthy" would wrongly hide
    them.
    """
    output_dir = _build_fixture_index(tmp_path)
    provider = ContextProvider(QueryEngine(output_dir))
    degraded = {
        "architecture": "basic",
        "precise_references": "ast-fallback",
        "precision": "ast_fallback_only",
        "test_recommendations": "heuristic",
        "coverage": "partial",
        "runtime_trace": "unavailable",
    }
    current = provider.query_engine.current()
    current["capabilities"] = {**degraded, "calls": "available"}
    monkeypatch.setattr(provider.query_engine, "current", lambda: current)

    capabilities = provider.get_context(
        ContextRequest(targets=[TARGET], detail_level="summary")
    )["capabilities"]

    assert capabilities == degraded


# Every member of ``TARGET_SCOPED_CLI_COMMANDS`` gets a real invocation. An
# earlier version covered 8 of 15 and closed with a subset assertion, which
# only caught commands added to the set — deleting ``bindings`` from it still
# passed, and that command then silently shipped the full capability table
# again.
TARGET_SCOPED_CLI_CASES = [
    ("bindings", ["bindings", TARGET]),
    ("callees", ["callees", TARGET]),
    ("callers", ["callers", TARGET]),
    ("callsites", ["callsites", TARGET]),
    ("context", ["context", TARGET]),
    ("explain", ["explain", TARGET]),
    ("impact", ["impact", TARGET]),
    ("imports", ["imports", "pkg.service"]),
    ("route", ["route", "GET", "/memories"]),
    ("similar", ["similar", TARGET]),
    ("symbol", ["symbol", "MemoryService"]),
    ("tests", ["tests", TARGET]),
    ("types", ["types", TARGET]),
    ("unresolved", ["unresolved", TARGET]),
    ("worker", ["worker", TARGET]),
]
INDEX_LEVEL_CLI_CASES = [
    ("current", ["current"]),
    ("stats", ["stats"]),
    ("architecture", ["architecture"]),
]


@pytest.mark.parametrize("command,argv", TARGET_SCOPED_CLI_CASES, ids=lambda v: v)
def test_target_scoped_commands_scope_capabilities_whoever_built_the_payload(
    command: str,
    argv: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One rule, applied wherever the payload came from.

    ``callers``/``impact``/``context`` are shaped by ContextProvider;
    ``symbol``/``types``/``callsites``/``similar``/``tests`` go straight to
    QueryEngine. Both must land on the same contract, or the next command added
    on the QueryEngine path silently ships the full table again.
    """
    output_dir = _build_fixture_index(tmp_path)
    assert (
        main(["--repo-root", str(FIXTURE_ROOT), "--output-dir", str(output_dir), *argv])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    summary = payload["capabilities_summary"]
    assert payload["schema_version"] == READ_SCHEMA_VERSION
    assert payload["index_schema_version"] == SCHEMA_VERSION
    assert summary["omitted_value"] == "available"
    assert summary["reported"] == len(payload["capabilities"])
    # Idempotence: a provider-scoped payload must not be scoped twice, which
    # would report `total` as the already-reduced count.
    assert summary["total"] > summary["reported"]
    assert all(value != "available" for value in payload["capabilities"].values())


@pytest.mark.parametrize("command,argv", INDEX_LEVEL_CLI_CASES, ids=lambda v: v)
def test_index_level_commands_keep_the_complete_capability_table(
    command: str,
    argv: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Whole-index views exist to report capability, so they report all of it."""
    output_dir = _build_fixture_index(tmp_path)
    assert (
        main(["--repo-root", str(FIXTURE_ROOT), "--output-dir", str(output_dir), *argv])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert "capabilities_summary" not in payload
    assert any(value == "available" for value in payload["capabilities"].values())


def test_target_scoped_command_set_matches_its_behaviour_coverage() -> None:
    """Equality, not subset: removing a command must fail as loudly as adding one.

    A ``<=`` assertion is one-way. It catches a name the CLI does not have,
    but a command *dropped* from the set silently reverts to emitting the full
    capability table, and nothing in the suite noticed.
    """
    from arcgraph.interfaces.cli import build_parser
    import argparse as _argparse

    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, _argparse._SubParsersAction)
    )
    covered = {command for command, _ in TARGET_SCOPED_CLI_CASES}

    # Every scoped command is a real CLI command...
    assert TARGET_SCOPED_CLI_COMMANDS <= set(subparsers.choices)
    # ...and every one of them is exercised above, by identity.
    assert covered == TARGET_SCOPED_CLI_COMMANDS


def test_scoping_never_hides_a_failure_for_the_requested_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target that fails to parse must still say why.

    The relevant-path set is built from nodes that were *found*. A file that
    fails to parse contributes none, so folding by that set discarded the very
    ``parse_error`` explaining the missing target: callers saw "No graph node
    resolved" with the cause removed.
    """
    provider = _provider_with_index_warnings(
        tmp_path,
        monkeypatch,
        [
            {
                "kind": "parse_error",
                "message": "SyntaxError at line 3",
                "path": "src/pkg/broken.py",
            }
        ],
    )
    request = ContextRequest(targets=["src/pkg/broken.py"], detail_level="summary")

    for payload in (
        provider.get_context(request),
        provider.explain(request),
        provider.compact_callers("src/pkg/broken.py", request),
        provider.compact_impact("src/pkg/broken.py", request),
    ):
        assert "SyntaxError at line 3" in _warnings_text(payload["warnings"])


def test_index_level_context_keeps_every_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no targets there is nothing to scope against, so nothing is folded."""
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [UNRELATED_WARNING, PATHLESS_WARNING]
    )

    warnings = provider.get_context(ContextRequest(targets=[], detail_level="summary"))[
        "warnings"
    ]

    joined = _warnings_text(warnings)
    assert "web/src/unrelated.ts" in joined
    assert "index_warnings_omitted" not in joined


def test_folding_still_applies_when_the_target_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety rule must not disable the optimisation for healthy targets."""
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [UNRELATED_WARNING] * 3
    )

    warnings = provider.get_context(
        ContextRequest(targets=[TARGET], detail_level="summary")
    )["warnings"]

    joined = _warnings_text(warnings)
    assert "web/src/unrelated.ts" not in joined
    assert '"typescript_import_unresolved": 3' in joined


@pytest.mark.parametrize("command", ["callers", "callees", "impact"])
def test_raw_returns_the_query_engine_payload_byte_for_byte(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--raw`` must reproduce ``QueryEngine`` exactly, including capabilities.

    Capability scoping was applied at the CLI dispatch point without excluding
    ``--raw``, so the escape hatch returned 6 capability keys plus a
    ``capabilities_summary`` the QueryEngine never emits — contradicting both
    the flag's purpose and the migration note telling old scripts to add it.
    """
    output_dir = _build_fixture_index(tmp_path)
    engine = QueryEngine(output_dir)
    expected = getattr(engine, command)(TARGET)

    assert (
        main(
            [
                "--repo-root",
                str(FIXTURE_ROOT),
                "--output-dir",
                str(output_dir),
                command,
                TARGET,
                "--raw",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == expected


def _risk_provider_with_index_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warnings: list[object]
) -> RiskProvider:
    output_dir = _build_fixture_index(tmp_path)
    provider = RiskProvider(QueryEngine(output_dir))
    current = provider.query_engine.current()
    current["warnings"] = warnings
    monkeypatch.setattr(provider.query_engine, "current", lambda: current)
    return provider


def test_risk_reports_scope_index_warnings_like_context_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The risk path is agent-facing too, and had no test at all.

    ``arcgraph_get_risk`` copied the whole index warning list verbatim while
    ``context`` and ``explain`` had already stopped doing so.
    """
    provider = _risk_provider_with_index_warnings(
        tmp_path, monkeypatch, [UNRELATED_WARNING] * 4
    )

    warnings = provider.get_risk([TARGET])["warnings"]

    joined = _warnings_text(warnings)
    assert "web/src/unrelated.ts" not in joined
    assert warnings == [
        {
            "kind": "index_warnings_omitted",
            "message": (
                "4 index-level warning(s) unrelated to the requested targets "
                "were omitted (typescript_import_unresolved=4). Run `arcgraph "
                "current` for the full list."
            ),
            "counts_by_kind": {"typescript_import_unresolved": 4},
        }
    ]


def test_risk_reports_keep_warnings_when_a_target_does_not_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same safety rule as context: an unresolved target disables folding."""
    provider = _risk_provider_with_index_warnings(
        tmp_path,
        monkeypatch,
        [
            {
                "kind": "parse_error",
                "message": "SyntaxError at line 3",
                "path": "src/pkg/broken.py",
            }
        ],
    )

    for targets in (["src/pkg/broken.py"], []):
        warnings = provider.get_risk(targets)["warnings"]
        assert "SyntaxError at line 3" in _warnings_text(warnings)


def test_risk_summarizes_many_unscoped_warnings_without_misattributing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _risk_provider_with_index_warnings(
        tmp_path,
        monkeypatch,
        [
            {
                "kind": "parse_error" if index < 3 else "dynamic_import",
                "message": f"diagnostic {index}",
                "path": f"src/generated_{index}.py",
            }
            for index in range(5)
        ],
    )

    payload = provider.get_risk(["missingShortName"], max_results=3)

    details = [
        warning
        for warning in payload["warnings"]
        if isinstance(warning, dict) and warning.get("path")
    ]
    summary = next(
        warning
        for warning in payload["warnings"]
        if isinstance(warning, dict)
        and warning.get("kind") == "index_warnings_unscoped_summary"
    )
    assert len(details) == 3
    assert all(warning["kind"] == "parse_error" for warning in details)
    assert summary["total"] == 2
    assert summary["counts_by_kind"] == {"dynamic_import": 2}
    assert payload["truncation"]["truncated_counts"]["warnings.details"] == 2
    assert payload["assurance"]["limits"]["response_truncated"] is True


def test_unresolved_context_and_risk_are_partial(tmp_path: Path) -> None:
    output_dir = _build_fixture_index(tmp_path)
    engine = QueryEngine(output_dir)

    context = ContextProvider(engine).get_context(
        ContextRequest(targets=["missingShortName"])
    )
    risk = RiskProvider(engine).get_risk(["missingShortName"])

    assert context["status"] == "partial"
    assert risk["status"] == "partial"
    assert context["assurance"]["posture"] == "do_not_rely"
    assert risk["assurance"]["posture"] == "do_not_rely"
    assert risk["target_reports"][0]["target_resolution"]["status"] == "unresolved"


def test_unresolved_risk_preserves_suggestions_and_recommends_their_paths(
    tmp_path: Path,
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    risk = RiskProvider(QueryEngine(output_dir)).get_risk(["MemoryServic"])

    resolution = risk["target_reports"][0]["target_resolution"]
    assert resolution["status"] == "unresolved"
    assert resolution["suggestions"]
    suggestion_paths = {item["path"] for item in resolution["suggestions"]}
    suggested_reads = {
        item["path"]
        for item in risk["recommended_next_reads"]
        if item["reason"] == "unresolved target suggestion"
    }
    assert suggested_reads
    assert suggested_reads <= suggestion_paths


def test_risk_reports_response_truncation_separately_from_analysis_limits(
    tmp_path: Path,
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    report = RiskProvider(QueryEngine(output_dir)).get_risk([TARGET], max_results=1)

    assert report["truncation"]["scope"] == "response_presentation"
    assert report["truncation"]["truncated"] is True
    assert report["assurance"]["limits"]["response_truncated"] is True
    assert "blast_radius.affected_symbols" in report["truncation"]["truncated_counts"]
    assert (
        report["target_reports"][0]["analysis_limits"]["traversal"]["truncated"]
        is False
    )


def test_risk_reports_scope_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Risk payloads follow the same capability contract as context payloads."""
    provider = _risk_provider_with_index_warnings(tmp_path, monkeypatch, [])
    current = provider.query_engine.current()
    current["capabilities"] = {"calls": "available", "coverage": "unavailable"}

    report = provider.get_risk([TARGET])

    assert report["capabilities"] == {"coverage": "unavailable"}
    assert report["capabilities_summary"]["omitted_value"] == "available"


TARGET_DEFINITION_FILE = "src/pkg/service.py"
STALE_TARGET_WARNING = {
    "kind": "stale_source",
    "message": "Source changed after the last build.",
    "path": TARGET_DEFINITION_FILE,
}


def test_every_payload_keeps_warnings_about_the_target_definition_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warning about the file under review must survive in every payload.

    Relation and impact payloads list callers, callees, and blast radius —
    never the target's own definition — so scoping by the nodes they happen to
    name folded away a ``stale_source`` on the exact file being asked about.
    ``context`` and ``explain`` were unaffected only because they add the
    target's definition to ``recommended_reads``; that does not generalise.
    """
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [STALE_TARGET_WARNING]
    )
    risk = _risk_provider_with_index_warnings(
        tmp_path, monkeypatch, [STALE_TARGET_WARNING]
    )
    request = ContextRequest(targets=[TARGET], detail_level="summary")

    payloads = {
        "context": provider.get_context(request),
        "explain": provider.explain(request),
        "callers": provider.compact_callers(TARGET, request),
        "callees": provider.compact_callees(TARGET, request),
        "impact": provider.compact_impact(TARGET, request),
        "risk": risk.get_risk([TARGET]),
    }

    for name, payload in payloads.items():
        assert TARGET_DEFINITION_FILE in _warnings_text(payload["warnings"]), name


def test_unmatched_entrypoint_target_disables_folding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entrypoint fast path returns early and must flag its own failure.

    ``_is_entrypoint_target`` short-circuits before the impact query that sets
    the unresolved flag, so an unmatched route folded the index warnings while
    reporting nothing unresolved — contradicting the documented rule.
    """
    provider = _provider_with_index_warnings(
        tmp_path,
        monkeypatch,
        [
            {
                "kind": "parse_error",
                "message": "SyntaxError at line 3",
                "path": "src/pkg/broken.py",
            }
        ],
    )

    warnings = provider.get_context(
        ContextRequest(
            targets=["route:GET:/definitely-missing"], detail_level="summary"
        )
    )["warnings"]

    joined = _warnings_text(warnings)
    assert "SyntaxError at line 3" in joined
    assert "index_warnings_omitted" not in joined


def test_folding_survives_the_target_definition_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding the definition path must not disable folding for everything else."""
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [UNRELATED_WARNING] * 5
    )
    risk = _risk_provider_with_index_warnings(
        tmp_path, monkeypatch, [UNRELATED_WARNING] * 5
    )
    request = ContextRequest(targets=[TARGET], detail_level="summary")

    for name, payload in (
        ("callers", provider.compact_callers(TARGET, request)),
        ("impact", provider.compact_impact(TARGET, request)),
        ("risk", risk.get_risk([TARGET])),
    ):
        joined = _warnings_text(payload["warnings"])
        assert "web/src/unrelated.ts" not in joined
        if name == "risk":
            assert any(
                warning.get("counts_by_kind", {}).get("typescript_import_unresolved")
                == 5
                for warning in payload["warnings"]
                if isinstance(warning, dict)
            )
        else:
            assert "typescript_import_unresolved=5" in joined


# The four target shapes callers actually use. ``symbol()`` only matches the
# first: a path target resolves through a different code path and returned zero
# matches, so deriving definition paths by re-resolving the query string hid
# the changed file's own warning — including for ``changed_files``, which is
# the shape risk reports are normally driven by.
TARGET_SHAPES = [
    ("symbol", TARGET),
    ("relative_path", TARGET_DEFINITION_FILE),
    ("absolute_path", str((FIXTURE_ROOT / TARGET_DEFINITION_FILE).resolve())),
]


@pytest.mark.parametrize("shape,target", TARGET_SHAPES, ids=lambda v: v)
def test_target_definition_warning_survives_every_target_shape(
    shape: str, target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [STALE_TARGET_WARNING]
    )
    risk = _risk_provider_with_index_warnings(
        tmp_path, monkeypatch, [STALE_TARGET_WARNING]
    )
    request = ContextRequest(targets=[target], detail_level="summary")

    payloads = {
        "context": provider.get_context(request),
        "explain": provider.explain(request),
        "callers": provider.compact_callers(target, request),
        "callees": provider.compact_callees(target, request),
        "impact": provider.compact_impact(target, request),
        "risk": risk.get_risk([target]),
    }

    for name, payload in payloads.items():
        assert TARGET_DEFINITION_FILE in _warnings_text(
            payload["warnings"]
        ), f"{shape}/{name}"


def test_changed_files_keep_their_own_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``changed_files`` is how risk reports are normally driven.

    It is always a path, never a symbol, so the ``symbol()``-based lookup
    resolved nothing and every changed file's own warning was folded away.
    """
    risk = _risk_provider_with_index_warnings(
        tmp_path, monkeypatch, [STALE_TARGET_WARNING]
    )

    warnings = risk.get_risk(changed_files=[TARGET_DEFINITION_FILE])["warnings"]

    assert TARGET_DEFINITION_FILE in _warnings_text(warnings)


def test_path_target_resolves_to_nodes_the_lookup_must_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the asymmetry the bug rested on, so the fix cannot silently regress.

    ``symbol(path)`` is empty while the same query resolves to many nodes.
    A definition-path lookup keyed off the query string therefore had nothing
    to work with; one keyed off ``resolved_targets`` does.
    """
    output_dir = _build_fixture_index(tmp_path)
    engine = QueryEngine(output_dir)

    assert engine.symbol(TARGET_DEFINITION_FILE).get("matches") == []
    resolved = engine.callers(TARGET_DEFINITION_FILE).get("resolved_targets", [])
    assert len(resolved) > 1
    paths = {node["path"] for node in engine.nodes_by_ids(resolved) if node.get("path")}
    assert TARGET_DEFINITION_FILE in paths


def _failing_store_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory: Any
) -> Any:
    """A provider whose node lookup raises the way a broken store would."""
    import sqlite3

    output_dir = _build_fixture_index(tmp_path)
    provider = factory(QueryEngine(output_dir))
    current = provider.query_engine.current()
    current["warnings"] = [STALE_TARGET_WARNING, UNRELATED_WARNING]
    monkeypatch.setattr(provider.query_engine, "current", lambda: current)

    def raise_operational_error(node_ids: list[str]) -> list[dict[str, Any]]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(provider.query_engine, "nodes_by_ids", raise_operational_error)
    return provider


def test_store_failure_disables_folding_instead_of_hiding_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable store must not be reported as "nothing was relevant".

    Parameterised SQL does not raise on a malformed id — it returns no rows —
    so an exception from ``nodes_by_ids`` is a storage or connection fault.
    Swallowing it into an empty path set left folding enabled, which silently
    reclassified the target's own warnings as unrelated and dropped them along
    with any sign that the read had failed.
    """
    provider = _failing_store_provider(tmp_path, monkeypatch, ContextProvider)
    risk = _failing_store_provider(tmp_path, monkeypatch, RiskProvider)
    request = ContextRequest(targets=[TARGET_DEFINITION_FILE], detail_level="summary")

    payloads = {
        "callers": provider.compact_callers(TARGET_DEFINITION_FILE, request),
        "callees": provider.compact_callees(TARGET_DEFINITION_FILE, request),
        "impact": provider.compact_impact(TARGET_DEFINITION_FILE, request),
        "risk": risk.get_risk(changed_files=[TARGET_DEFINITION_FILE]),
    }

    for name, payload in payloads.items():
        joined = _warnings_text(payload["warnings"])
        # The target's own warning survives...
        assert TARGET_DEFINITION_FILE in joined, name
        # ...the failure is stated rather than inferred...
        assert "warning_scope_unavailable" in joined, name
        # ...and nothing was folded on a scope the payload could not compute.
        assert "index_warnings_omitted" not in joined, name


def test_store_failure_for_one_risk_target_does_not_hide_behind_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A risk report covers many targets; one unreadable scope poisons them all.

    Accumulating paths across targets meant a successful target could supply
    enough paths to look like scoping had worked, while a failed one silently
    contributed nothing.
    """
    risk = _failing_store_provider(tmp_path, monkeypatch, RiskProvider)

    warnings = risk.get_risk([TARGET], changed_files=[TARGET_DEFINITION_FILE])[
        "warnings"
    ]

    joined = _warnings_text(warnings)
    assert "warning_scope_unavailable" in joined
    assert "index_warnings_omitted" not in joined


def test_unknown_warning_scope_bounds_path_details_but_keeps_failure_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    risk = _failing_store_provider(tmp_path, monkeypatch, RiskProvider)
    current = risk.query_engine.current()
    current["warnings"] = [
        {
            "kind": "parse_error",
            "message": f"parse failure {index}",
            "path": f"src/generated_{index}.py",
        }
        for index in range(4_000)
    ]

    payload = risk.get_risk(changed_files=[TARGET_DEFINITION_FILE], max_results=3)

    warnings = payload["warnings"]
    assert any(
        isinstance(warning, dict) and warning.get("kind") == "warning_scope_unavailable"
        for warning in warnings
    )
    assert len(warnings) == 4  # sentinel, two details, and one omission summary
    assert payload["truncation"]["truncated_counts"]["warnings.details"] == 3_998


def test_unknown_warning_scope_never_drops_pathless_capability_caveats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability_warning = {
        "kind": "typescript_frontend_unavailable",
        "message": "TypeScript compiler API is unavailable.",
        "reason": "compiler_not_found",
    }
    provider = _risk_provider_with_index_warnings(
        tmp_path,
        monkeypatch,
        [
            capability_warning,
            *[
                {
                    "kind": "parse_error",
                    "message": f"failure {index}",
                    "path": f"src/generated_{index}.ts",
                }
                for index in range(4)
            ],
        ],
    )

    payload = provider.get_risk(["missingShortName"], max_results=1)

    assert capability_warning in payload["warnings"]
    assert any(
        isinstance(warning, dict)
        and warning.get("kind") == "index_warnings_unscoped_summary"
        for warning in payload["warnings"]
    )


def test_unresolved_path_prioritizes_its_own_warning_in_the_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = "src/pkg/broken.py"
    target_warning = {
        "kind": "parse_error",
        "message": "This syntax error prevented target resolution.",
        "path": requested,
    }
    provider = _risk_provider_with_index_warnings(
        tmp_path,
        monkeypatch,
        [
            *[
                {
                    "kind": "parse_error",
                    "message": f"unrelated failure {index}",
                    "path": f"src/generated_{index}.py",
                }
                for index in range(10)
            ],
            target_warning,
        ],
    )

    payload = provider.get_risk([requested], max_results=1)

    assert target_warning in payload["warnings"]


def test_healthy_store_still_folds_and_reports_no_scope_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degradation must not fire on the normal path."""
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [STALE_TARGET_WARNING, *[UNRELATED_WARNING] * 3]
    )
    risk = _risk_provider_with_index_warnings(
        tmp_path, monkeypatch, [STALE_TARGET_WARNING, *[UNRELATED_WARNING] * 3]
    )
    request = ContextRequest(targets=[TARGET_DEFINITION_FILE], detail_level="summary")

    for name, payload in (
        ("callers", provider.compact_callers(TARGET_DEFINITION_FILE, request)),
        ("impact", provider.compact_impact(TARGET_DEFINITION_FILE, request)),
        ("risk", risk.get_risk(changed_files=[TARGET_DEFINITION_FILE])),
    ):
        joined = _warnings_text(payload["warnings"])
        assert "warning_scope_unavailable" not in joined
        assert TARGET_DEFINITION_FILE in joined
        if name == "risk":
            assert any(
                warning.get("counts_by_kind", {}).get("typescript_import_unresolved")
                == 3
                for warning in payload["warnings"]
                if isinstance(warning, dict)
            )
        else:
            assert "typescript_import_unresolved=3" in joined


NodeLookup = Callable[[list[str]], list[dict[str, Any]]]


def _provider_for_node_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[QueryEngine], Any],
) -> Any:
    """Provider with index warnings seeded; the caller patches ``nodes_by_ids``.

    The patch is applied by each test rather than through a wrapper callback:
    calling a function held in a parameter is exactly the shape ArcGraph
    reports as an unresolved static candidate, and this suite should not ship
    the pattern its own release gate flags.
    """
    output_dir = _build_fixture_index(tmp_path)
    provider = factory(QueryEngine(output_dir))
    current = provider.query_engine.current()
    current["warnings"] = [STALE_TARGET_WARNING, UNRELATED_WARNING, UNRELATED_WARNING]
    monkeypatch.setattr(provider.query_engine, "current", lambda: current)
    return provider


@pytest.mark.parametrize("mode", ["empty", "short"])
def test_incomplete_node_read_is_treated_as_unknown_scope(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short read is as blind as a failed one, and must degrade the same way.

    Only exceptions were treated as "scope unknown". A lookup that returned
    successfully with no rows, or with the target's own node missing, produced
    an ordinary path set, folding stayed on, and the target's warning was
    reclassified as unrelated — with no sentinel to show anything went wrong.
    """
    payloads: dict[str, dict[str, Any]] = {}
    request = ContextRequest(targets=[TARGET_DEFINITION_FILE], detail_level="summary")

    for name, factory in (("context", ContextProvider), ("risk", RiskProvider)):
        provider = _provider_for_node_read(tmp_path, monkeypatch, factory)
        real: NodeLookup = provider.query_engine.nodes_by_ids
        lookup: NodeLookup = (
            (lambda node_ids: [])
            if mode == "empty"
            else (lambda node_ids: real(node_ids)[:-1])
        )
        monkeypatch.setattr(provider.query_engine, "nodes_by_ids", lookup)
        if name == "context":
            payloads["callers"] = provider.compact_callers(
                TARGET_DEFINITION_FILE, request
            )
            payloads["impact"] = provider.compact_impact(
                TARGET_DEFINITION_FILE, request
            )
        else:
            payloads["risk"] = provider.get_risk(changed_files=[TARGET_DEFINITION_FILE])

    for name, payload in payloads.items():
        joined = _warnings_text(payload["warnings"])
        assert "warning_scope_unavailable" in joined, f"{mode}/{name}"
        assert TARGET_DEFINITION_FILE in joined, f"{mode}/{name}"
        assert "index_warnings_omitted" not in joined, f"{mode}/{name}"


def test_risk_batches_definition_path_lookup_across_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One report performs one complete definition-path read, not one per target.

    The preflight presentation can bound ``resolved_targets`` while disclosing
    the omitted count, but warning scoping still has to inspect every resolved
    definition behind the report.
    """
    risk = _provider_for_node_read(tmp_path, monkeypatch, RiskProvider)
    expected_ids = {
        node_id
        for target in (TARGET, TARGET_DEFINITION_FILE)
        for node_id in risk.query_engine.impact(target)["resolved_targets"]
    }
    real: NodeLookup = risk.query_engine.nodes_by_ids
    calls: list[list[str]] = []

    def lookup(node_ids: list[str]) -> list[dict[str, Any]]:
        calls.append(node_ids)
        return real(node_ids)

    monkeypatch.setattr(risk.query_engine, "nodes_by_ids", lookup)

    payload = risk.get_risk([TARGET], changed_files=[TARGET_DEFINITION_FILE])

    assert len(calls) == 1
    assert set(calls[0]) == expected_ids
    assert {
        node_id
        for report in payload["target_reports"]
        for node_id in report["resolved_targets"]
    } <= expected_ids


def test_risk_deduplicates_query_work_across_input_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = _build_fixture_index(tmp_path)
    provider = RiskProvider(QueryEngine(output_dir))
    real_impact = provider.query_engine.impact
    calls: list[str] = []

    def impact(target: str, *, max_depth: int) -> dict[str, Any]:
        calls.append(target)
        return real_impact(target, max_depth=max_depth)

    monkeypatch.setattr(provider.query_engine, "impact", impact)

    payload = provider.get_risk(
        [TARGET, TARGET],
        changed_files=[TARGET_DEFINITION_FILE, TARGET_DEFINITION_FILE, TARGET],
    )

    assert calls == [TARGET, TARGET_DEFINITION_FILE]
    assert payload["targets"] == [TARGET]
    assert payload["changed_files"] == [TARGET_DEFINITION_FILE, TARGET]


def test_target_scoped_providers_reject_unbounded_input_before_query_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = _build_fixture_index(tmp_path)
    engine = QueryEngine(output_dir)
    calls: list[str] = []

    def impact(target: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(target)
        raise AssertionError("query work should not start")

    monkeypatch.setattr(engine, "impact", impact)
    too_many = [
        f"fn:pkg.target_{index}" for index in range(MAX_TARGETS_PER_REQUEST + 1)
    ]

    with pytest.raises(TargetRequestLimitError, match="at most"):
        ContextProvider(engine).get_context(ContextRequest(targets=too_many))
    with pytest.raises(TargetRequestLimitError, match="at most"):
        RiskProvider(engine).get_risk(too_many)

    assert calls == []


@pytest.mark.parametrize("provider_kind", ["context", "risk"])
def test_programming_errors_in_definition_path_lookup_propagate(
    provider_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = ContextProvider if provider_kind == "context" else RiskProvider
    provider = _provider_for_node_read(tmp_path, monkeypatch, factory)

    def fail_loudly(_node_ids: list[str]) -> list[dict[str, Any]]:
        raise AssertionError("programming defect")

    monkeypatch.setattr(provider.query_engine, "nodes_by_ids", fail_loudly)

    with pytest.raises(AssertionError, match="programming defect"):
        if provider_kind == "context":
            provider.compact_impact(
                TARGET,
                ContextRequest(targets=[TARGET], detail_level="summary"),
            )
        else:
            provider.get_risk([TARGET])


def test_read_payload_version_is_independent_from_the_index_schema(
    tmp_path: Path,
) -> None:
    output_dir = _build_fixture_index(tmp_path)
    engine = QueryEngine(output_dir)
    payload = ContextProvider(engine).get_context(ContextRequest(targets=[TARGET]))

    assert engine.symbol(TARGET)["schema_version"] == SCHEMA_VERSION
    assert payload["schema_version"] == READ_SCHEMA_VERSION
    assert payload["index_schema_version"] == SCHEMA_VERSION
    with pytest.raises(ValidationError):
        ContextResponse.model_validate({**payload, "schema_version": SCHEMA_VERSION})


def test_risk_report_schema_matches_the_real_provider_payload(tmp_path: Path) -> None:
    output_dir = _build_fixture_index(tmp_path)
    payload = RiskProvider(QueryEngine(output_dir)).get_risk([TARGET])

    validated = RiskReport.model_validate(payload)

    assert isinstance(validated.blast_radius, dict)
    assert validated.test_candidates
    assert validated.estimated_tokens > 0
    assert validated.estimated_tokens < 1500
    assert validated.unknowns
    assert validated.recommended_next_reads
    with pytest.raises(ValidationError):
        RiskReport.model_validate({**payload, "unexpected": True})


def test_risk_bounds_high_cardinality_unresolved_diagnostics_by_max_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = _build_fixture_index(tmp_path)
    engine = QueryEngine(output_dir)
    provider = RiskProvider(engine)
    impact = engine.impact(TARGET)
    impact["unresolved_risks"] = {
        "summary": {"total": 200, "returned": 200, "limit": 200},
        "items": [
            {
                "diagnostic_id": f"diagnostic:{index}",
                "diagnostic_kind": "unresolved_callsite",
                "severity": "info",
                "path": f"src/pkg/generated_{index}.py",
                "start_line": index + 1,
                "message": "verbose diagnostic " + ("x" * 1000),
                "properties": {
                    "raw_expression": f"receiver_{index}.call",
                    "failed_strategy": "dynamic_dispatch",
                    "stable_callsite_subject": {"source": "y" * 1000},
                },
            }
            for index in range(200)
        ],
    }
    monkeypatch.setattr(engine, "impact", lambda *_args, **_kwargs: impact)

    payload = provider.get_risk([TARGET], max_results=3)

    assert payload["estimated_tokens"] < 2500
    assert payload["truncation"]["reason"] == "max_results"
    assert (
        payload["truncation"]["truncated_counts"]["target_reports.unresolved_risks"]
        == 197
    )
    samples = payload["target_reports"][0]["unresolved_risks"]
    assert len(samples) == 3
    assert samples[0]["message"]
    assert samples[0]["message_truncated"] is True
    assert samples[0]["category"]
    assert samples[0]["suggested_next_step"]


def test_risk_keeps_all_safety_factor_kinds_beyond_result_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = _build_fixture_index(tmp_path)
    engine = QueryEngine(output_dir)
    provider = RiskProvider(engine)
    impact = engine.impact(TARGET)
    impact["analysis_limits"]["traversal"] = {
        "truncated": True,
        "reasons": ["synthetic"],
    }
    impact["resource_impact"]["edges"] = [
        {"kind": "writes", "source": "a", "target": "b"}
    ]
    impact["test_candidates"] = []
    monkeypatch.setattr(engine, "impact", lambda *_args, **_kwargs: impact)

    payload = provider.get_risk([TARGET], max_results=1)

    assert {item["kind"] for item in payload["risk_factors"]} >= {
        "analysis_truncated",
        "entrypoint_impact",
        "resource_write",
        "missing_test_candidate",
    }


def test_risk_honors_requested_limit_for_target_reports_and_blast_radius(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = _build_fixture_index(tmp_path)
    engine = QueryEngine(output_dir)
    provider = RiskProvider(engine)
    base = engine.impact(TARGET)
    targets = [f"synthetic_{index}" for index in range(5)]

    def impact(target: str, **_kwargs: Any) -> dict[str, Any]:
        report = dict(base)
        report["query"] = target
        report["entrypoint_impact"] = {
            **base["entrypoint_impact"],
            "entrypoints": [
                {
                    **base["entrypoint_impact"]["entrypoints"][0],
                    "id": f"route:GET:/{target}",
                }
            ],
        }
        report["test_candidates"] = [
            {"path": f"tests/test_{target}.py", "reason": "synthetic"}
        ]
        return report

    monkeypatch.setattr(engine, "impact", impact)

    payload = provider.get_risk(targets, max_results=5)

    assert len(payload["target_reports"]) == 5
    assert len(payload["blast_radius"]["entrypoints"]) == 5
    assert len(payload["test_candidates"]) == 5


def test_index_status_exposes_runtime_build_identity(tmp_path: Path) -> None:
    output_dir = _build_fixture_index(tmp_path)

    payload = ContextProvider(QueryEngine(output_dir)).index_status()

    identity = payload["build_identity"]
    assert identity["display_version"]
    assert len(identity["runtime_fingerprint"]["sha256"]) == 64
    assert payload["storage"]["status_is_read_only"] is True
    assert payload["storage"]["safe_prune"]["mode"] == "dry_run"


def test_sentinel_fires_only_for_a_failed_definition_path_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three cases stop folding; only one of them is a failure.

    Documenting all three as emitting ``warning_scope_unavailable`` overstated
    the contract: no-target and unresolved-target are ordinary states already
    visible in the payload, and a consumer treating the sentinel as "the store
    is broken" would have misread both.
    """
    provider = _provider_with_index_warnings(
        tmp_path, monkeypatch, [UNRELATED_WARNING] * 2
    )

    no_targets = provider.get_context(
        ContextRequest(targets=[], detail_level="summary")
    )["warnings"]
    unresolved = provider.get_context(
        ContextRequest(targets=["does.not.exist.anywhere"], detail_level="summary")
    )["warnings"]

    for warnings in (no_targets, unresolved):
        joined = _warnings_text(warnings)
        # Folding is off — every warning is present, unfolded...
        assert "web/src/unrelated.ts" in joined
        assert "index_warnings_omitted" not in joined
        # ...but nothing failed, so no sentinel.
        assert "warning_scope_unavailable" not in joined


def test_index_status_reuses_build_identity_instead_of_rehashing(
    tmp_path: Path, monkeypatch
) -> None:
    """index_status is the cheapest documented check, so repeated calls must
    not re-fingerprint the package or re-walk the output tree every time."""

    import arcgraph.version_info as version_info_module
    from arcgraph.core import cleanup as cleanup_module

    monkeypatch.setattr(version_info_module, "_VERSION_INFO_CACHE", None)
    monkeypatch.setattr(cleanup_module, "_STORAGE_STATUS_CACHE", {})

    fingerprints = {"count": 0}
    real_fingerprint = version_info_module._runtime_fingerprint

    def counting_fingerprint(package_dir):
        fingerprints["count"] += 1
        return real_fingerprint(package_dir)

    monkeypatch.setattr(
        version_info_module, "_runtime_fingerprint", counting_fingerprint
    )

    walks = {"count": 0}
    real_payload = cleanup_module._storage_status_payload

    def counting_payload(output_dir, *, keep_builds):
        walks["count"] += 1
        return real_payload(output_dir, keep_builds=keep_builds)

    monkeypatch.setattr(cleanup_module, "_storage_status_payload", counting_payload)

    first = version_info_module.version_info()
    for _ in range(4):
        assert version_info_module.version_info() == first
    assert fingerprints["count"] == 1

    output_dir = tmp_path / "arcgraph"
    output_dir.mkdir()
    status = cleanup_module.arcgraph_output_storage_status(output_dir)
    for _ in range(4):
        assert cleanup_module.arcgraph_output_storage_status(output_dir) == status
    assert walks["count"] == 1

    # The identity command must still observe live state.
    version_info_module.version_info(refresh=True)
    assert fingerprints["count"] == 2
    cleanup_module.arcgraph_output_storage_status(output_dir, refresh=True)
    assert walks["count"] == 2
