from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import ContextRequest
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.providers.context_provider import _compact_relation_payload

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
INCOMING_TARGET = "pkg.service.build_message"
OUTGOING_TARGET = "pkg.api.hello"


def test_callers_compact_matches_explain_incoming_edges(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    callers = _run_cli_json(
        capsys,
        "--repo-root",
        str(FIXTURE_ROOT),
        "--output-dir",
        str(output_dir),
        "callers",
        INCOMING_TARGET,
        "--compact",
        "--detail-level",
        "detailed",
    )
    explain = _run_cli_json(
        capsys,
        "--repo-root",
        str(FIXTURE_ROOT),
        "--output-dir",
        str(output_dir),
        "explain",
        INCOMING_TARGET,
        "--detail-level",
        "detailed",
    )

    incoming_edge = next(
        edge
        for edge in callers["edges"]
        if edge["source"] == "fn:pkg.api.hello"
        and edge["target"] == "fn:pkg.service.build_message"
    )

    assert callers["status"] == "available"
    assert callers["relation"] == "callers"
    assert callers["source_snippets"] == {"requested": False, "enabled": False}
    assert callers["edges"] == explain["explanations"][0]["incoming_edges"]
    assert incoming_edge["evidence"]
    assert incoming_edge["resolution"]["strategy"]
    assert "fallbacks" in incoming_edge["resolution"]
    assert "confidence_sources" in incoming_edge
    assert "properties" not in incoming_edge
    assert callers["estimated_tokens"] < 8000
    assert not _contains_key(callers, "properties")
    assert not _contains_key(callers, "snippet")
    assert not _contains_key(callers, "source_snippet")


def test_callees_compact_matches_explain_outgoing_edges(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    callees = _run_cli_json(
        capsys,
        "--repo-root",
        str(FIXTURE_ROOT),
        "--output-dir",
        str(output_dir),
        "callees",
        OUTGOING_TARGET,
        "--compact",
        "--detail-level",
        "standard",
    )
    explain = _run_cli_json(
        capsys,
        "--repo-root",
        str(FIXTURE_ROOT),
        "--output-dir",
        str(output_dir),
        "explain",
        OUTGOING_TARGET,
        "--detail-level",
        "standard",
    )

    assert callees["status"] == "available"
    assert callees["relation"] == "callees"
    assert callees["edges"] == explain["explanations"][0]["outgoing_edges"]
    assert callees["edges"]
    assert all("properties" not in edge for edge in callees["edges"])
    assert all("confidence_sources" in edge for edge in callees["edges"])
    assert callees["estimated_tokens"] < 8000


def test_impact_compact_keeps_provenance_without_raw_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    payload = _run_cli_json(
        capsys,
        "--repo-root",
        str(FIXTURE_ROOT),
        "--output-dir",
        str(output_dir),
        "impact",
        INCOMING_TARGET,
        "--compact",
        "--detail-level",
        "detailed",
    )
    call_edges = payload["call_impact"]["edges"]

    assert payload["status"] == "available"
    assert call_edges
    assert call_edges[0]["evidence"]
    assert "resolution" in call_edges[0]
    assert "confidence_sources" in call_edges[0]
    assert "properties" not in call_edges[0]
    assert payload["provenance_summary"]["resolution"]["edge_count"] >= len(call_edges)
    assert payload["estimated_tokens"] < 8000
    assert not _contains_key(payload, "properties")
    assert not _contains_key(payload, "snippet")
    assert not _contains_key(payload, "source_snippet")


def test_raw_callers_remains_debug_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = _build_fixture_index(tmp_path)

    payload = _run_cli_json(
        capsys,
        "--repo-root",
        str(FIXTURE_ROOT),
        "--output-dir",
        str(output_dir),
        "callers",
        INCOMING_TARGET,
        "--raw",
    )

    assert payload["edges"]
    assert "properties" in payload["edges"][0]


def test_compact_relation_caps_evidence_and_strips_snippets() -> None:
    payload = _compact_relation_payload(
        current={
            "index_version": "idx",
            "freshness": {"status": "fresh", "stale": False},
            "capabilities": {},
            "warnings": [],
        },
        request=ContextRequest(
            targets=["fn:target"],
            max_results=30,
            detail_level="summary",
            include_source=False,
        ),
        query="fn:target",
        relation="callers",
        node_key="callers",
        result={
            "schema_version": "1.0.0",
            "status": "available",
            "warnings": [],
            "resolved_targets": ["fn:target"],
            "callers": [],
            "edges": [
                {
                    "source": "fn:source",
                    "target": "fn:target",
                    "kind": "calls",
                    "confidence": "confirmed",
                    "semantic_role": "call",
                    "resolution": {"status": "resolved", "fallbacks": []},
                    "confidence_sources": {"ast": ["call"]},
                    "evidence": [
                        {"kind": "one", "path": "src/a.py", "snippet": "one()"},
                        {"kind": "two", "path": "src/a.py", "snippet": "two()"},
                        {"kind": "three", "path": "src/a.py", "snippet": "three()"},
                    ],
                    "properties": {"large": "payload"},
                }
            ],
        },
    )

    edge = payload["edges"][0]
    assert len(edge["evidence"]) == 2
    assert edge["evidence_total"] == 3
    assert edge["evidence_truncated"] == 1
    assert payload["truncation"]["truncated_counts"]["evidence_items"] == 1
    assert "properties" not in edge
    assert not _contains_key(payload, "snippet")


def test_compact_detail_level_rejects_full(
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
                "callers",
                INCOMING_TARGET,
                "--compact",
                "--detail-level",
                "full",
            ]
        )

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def _build_fixture_index(tmp_path: Path) -> Path:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    return output_dir


def _run_cli_json(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    exit_code = main(list(args))
    assert exit_code == 0
    return json.loads(capsys.readouterr().out)


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


@pytest.mark.parametrize("command", ["callers", "callees", "impact"])
def test_relation_commands_default_to_the_bounded_agent_payload(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No flags must mean the bounded payload, not the debug dump.

    The unbounded payload carries every node property, which on this repository
    reaches 3 MB for ``callers`` and 10 MB for ``impact``. A default that large
    is unusable for the agents the payload exists to serve, so ``--raw`` has to
    be opt-in.
    """
    output_dir = _build_fixture_index(tmp_path)
    target = OUTGOING_TARGET if command == "callees" else INCOMING_TARGET

    default = _run_cli_json(
        capsys,
        "--repo-root",
        str(FIXTURE_ROOT),
        "--output-dir",
        str(output_dir),
        command,
        target,
    )

    # ``truncation`` is only ever emitted by the bounded payload, and node
    # ``properties`` only ever by the raw one. Checking both directions keeps
    # the test from passing on a payload that is merely small.
    assert "truncation" in default
    for node in default.get(command, []):
        assert "properties" not in node


@pytest.mark.parametrize("command", ["callers", "callees", "impact"])
def test_compact_flag_is_accepted_and_matches_the_default(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--compact`` stays accepted so existing agent scripts keep working."""
    output_dir = _build_fixture_index(tmp_path)
    target = OUTGOING_TARGET if command == "callees" else INCOMING_TARGET
    base = [
        "--repo-root",
        str(FIXTURE_ROOT),
        "--output-dir",
        str(output_dir),
        command,
        target,
    ]

    assert _run_cli_json(capsys, *base) == _run_cli_json(capsys, *base, "--compact")


@pytest.mark.parametrize("command", ["callers", "callees", "impact"])
def test_default_relation_commands_preserve_stale_recovery_action(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = tmp_path / "repo"
    source = repo_root / "src" / "pkg" / "service.py"
    source.parent.mkdir(parents=True)
    (source.parent / "__init__.py").write_text("", encoding="utf-8")
    source.write_text(
        "def target():\n    return 1\n\n" "def caller():\n    return target()\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(repo_root, output_dir, [SourceRoot("src")]).build()
    source.write_text(
        "def target():\n    return 2\n\n" "def caller():\n    return target()\n",
        encoding="utf-8",
    )

    payload = _run_cli_json(
        capsys,
        "--repo-root",
        str(repo_root),
        "--output-dir",
        str(output_dir),
        command,
        "pkg.service.target",
    )

    assert payload["freshness"]["stale"] is True
    assert payload["recovery_action"] == {
        "kind": "refresh_index",
        "command": "arcgraph sync --if-stale",
        "automatic": False,
        "reader_remains_read_only": True,
    }


@pytest.mark.parametrize("command", ["callers", "callees", "impact"])
@pytest.mark.parametrize(
    "option",
    [["--max-results", "5"], ["--detail-level", "detailed"], ["--include-source"]],
)
def test_raw_rejects_shaping_options_it_cannot_apply(
    command: str, option: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--raw`` must refuse shaping rather than discard it silently.

    Before this contract, ``callers TARGET --max-results 5`` returned all 132
    callers: argparse accepted the option and the raw path never read it.
    """
    output_dir = _build_fixture_index(tmp_path)
    target = OUTGOING_TARGET if command == "callees" else INCOMING_TARGET

    exit_code = main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            command,
            target,
            "--raw",
            *option,
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert option[0] in captured.err
    # Nothing may reach stdout: a caller piping JSON must not receive a
    # partial or unshaped payload alongside the refusal.
    assert captured.out == ""


@pytest.mark.parametrize("command", ["callers", "callees", "impact"])
def test_max_results_bounds_the_default_payload(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--max-results`` must change the returned count, not just be parsed."""
    output_dir = _build_fixture_index(tmp_path)
    target = OUTGOING_TARGET if command == "callees" else INCOMING_TARGET

    payload = _run_cli_json(
        capsys,
        "--repo-root",
        str(FIXTURE_ROOT),
        "--output-dir",
        str(output_dir),
        command,
        target,
        "--max-results",
        "1",
        "--detail-level",
        "detailed",
    )

    assert payload["truncation"]["max_results"] == 1
    assert len(payload.get(command, [])) <= 1
