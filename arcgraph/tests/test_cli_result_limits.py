"""Exercise CLI limit notices through real fixture queries and report rendering."""

import json
from pathlib import Path

import pytest

from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import build_parser, main
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"
TARGET = "pkg.service.MemoryService.create_memory"
COMMANDS = [
    (["callers", TARGET], 100),
    (["callees", TARGET], 100),
    (["impact", TARGET], 100),
    (["context", TARGET], 100),
    (["explain", TARGET], 100),
    (["similar", TARGET], 50),
    (["references", TARGET], 1000),
    (["ci"], 100),
    (["report", "ci"], 100),
    (["report", "pr"], 100),
    (["report", "html", TARGET], 50),
]


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    output = tmp_path_factory.mktemp("result-limits") / "index"
    ArcGraphIndexer(
        repo_root=FIXTURE,
        output_dir=output,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    return output


@pytest.mark.parametrize("human", [False, True], ids=["json", "human"])
@pytest.mark.parametrize(
    "command,limit",
    COMMANDS,
    ids=[" ".join(c[:2] if c[0] == "report" else c[:1]) for c, _ in COMMANDS],
)
def test_cli_reports_requested_and_applied_result_limit(
    index, capsys, command, limit, human
):
    prefix = ["--repo-root", str(FIXTURE), "--output-dir", str(index)]
    if human:
        prefix.append("--human")
    code = main([*prefix, *command, "--max-results", "10001"])
    captured = capsys.readouterr()
    assert code in (0, 1)  # CI may fail repository checks; the command must run.
    name = " ".join(command[:2] if command[0] == "report" else command[:1])
    assert (
        f"{name}: --max-results requested 10001; actual limit {limit}" in captured.err
    )
    if not human and command[0] != "report":
        payload = json.loads(captured.out)
        assert any(
            f"requested 10001; actual limit {limit}" in warning
            for warning in payload["warnings"]
        )
    if command[:2] == ["report", "html"]:
        assert "actual limit 100 (ci)" in captured.err


@pytest.mark.parametrize("kind", ["pr", "html"])
def test_report_file_receipts_keep_their_existing_keys(index, tmp_path, capsys, kind):
    output = tmp_path / f"report.{kind}"
    command = ["report", kind, *([TARGET] if kind == "html" else [])]
    assert (
        main(
            [
                "--repo-root",
                str(FIXTURE),
                "--output-dir",
                str(index),
                *command,
                "--max-results",
                "10001",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert set(receipt) == {"schema_version", "index_version", "status", "path"} | (
        {"changed_files"} if kind == "pr" else set()
    )
    assert f"report {kind}: --max-results requested 10001; actual limit" in captured.err
    assert output.stat().st_size > 0


def test_report_ci_defaults_do_not_require_optional_evidence_flags():
    args = build_parser().parse_args(["report", "ci"])
    for name in (
        "require_precision",
        "require_coverage",
        "require_runtime_trace",
        "python_full",
    ):
        assert getattr(args, name) is False


def test_cli_does_not_warn_when_requested_limit_is_supported(index, capsys):
    assert (
        main(
            [
                "--repo-root",
                str(FIXTURE),
                "--output-dir",
                str(index),
                "similar",
                TARGET,
                "--max-results",
                "50",
            ]
        )
        == 0
    )
    assert "--max-results requested" not in capsys.readouterr().err


def test_html_entrypoint_reports_each_component_ceiling(index, capsys):
    assert (
        main(
            [
                "--repo-root",
                str(FIXTURE),
                "--output-dir",
                str(index),
                "report",
                "html",
                "route:POST:/memories",
                "--max-results",
                "201",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    for component, limit in [("similar", 50), ("ci", 100), ("entrypoint_flow", 200)]:
        assert (
            f"report html: --max-results requested 201; actual limit {limit} ({component})"
            in captured.err
        )
    assert captured.out.lstrip().lower().startswith("<!doctype html>")
