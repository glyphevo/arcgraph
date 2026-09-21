from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph.change.service import ChangeSafetyService
from arcgraph.interfaces.cli import build_parser, main
from arcgraph.tests.change_safety_helpers import git, init_repo, write_build


def _change_args(repo: Path, output: Path, *command: str) -> list[str]:
    return [
        "--repo-root",
        str(repo),
        "--output-dir",
        str(output),
        "change",
        "--repo-id",
        "repo",
        *command,
    ]


def _build_clean_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = init_repo(tmp_path)
    output = tmp_path / "output"
    write_build(
        repo,
        output,
        index_version="index",
        commit_sha=git(repo, "rev-parse", "HEAD"),
    )
    return repo, output


def test_change_cli_exposes_required_command_tree() -> None:
    parser = build_parser()
    change = next(
        action
        for action in parser._actions
        if getattr(action, "dest", None) == "command"
    )
    change_parser = change.choices["change"]
    subcommands = next(
        action
        for action in change_parser._actions
        if getattr(action, "dest", None) == "change_command"
    ).choices

    assert {
        "plan",
        "show",
        "list",
        "approve",
        "reject",
        "abandon",
        "diff",
        "verify",
        "report",
        "archive",
        "reconcile-pins",
        "evidence",
        "audit",
    } <= set(subcommands)
    evidence = next(
        action
        for action in subcommands["evidence"]._actions
        if getattr(action, "dest", None) == "change_evidence_command"
    ).choices
    audit = next(
        action
        for action in subcommands["audit"]._actions
        if getattr(action, "dest", None) == "change_audit_command"
    ).choices
    assert {"add", "list", "show", "purge"} <= set(evidence)
    assert {"export"} <= set(audit)


def test_change_cli_lifecycle_evidence_export_and_exit_codes(
    tmp_path: Path,
    capsys,
) -> None:
    repo, output = _build_clean_repo(tmp_path)

    assert (
        main(
            _change_args(
                repo,
                output,
                "plan",
                "--task",
                "change the explicit function",
                "--target",
                "symbol:fn:app.f",
                "--plan-id",
                "cli-plan",
                "--pin-id",
                "pin-cli",
            )
        )
        == 0
    )
    planned = json.loads(capsys.readouterr().out)
    assert planned["status"] == "success"
    assert planned["verdict"] == "PLAN_READY"
    revision = planned["data"]["plan_revision"]
    digest = revision["plan_content_digest"]
    requirement = revision["verification_plan"]["requirements"][0]["requirement_id"]

    assert main(_change_args(repo, output, "show", "--plan-id", "cli-plan")) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["operation"] == "show"
    assert shown["data"]["plan_id"] == "cli-plan"

    assert (
        main(
            _change_args(
                repo,
                output,
                "approve",
                "--plan-id",
                "cli-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
                "--actor",
                "operator",
                "--reason",
                "reviewed",
            )
        )
        == 0
    )
    assert (
        json.loads(capsys.readouterr().out)["data"]["plan_decision"]["status"]
        == "approved"
    )

    assert (
        main(
            _change_args(
                repo,
                output,
                "evidence",
                "add",
                "--plan-id",
                "cli-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
                "--evidence-id",
                "evidence-cli",
                "--requirement-id",
                requirement,
                "--result",
                "pass",
                "--attestation-level",
                "self_reported",
                "--producer-identity",
                "local-operator",
                "--artifact-digest",
                "artifact-cli",
                "--runner-metadata-json",
                '{"token":"must-not-persist"}',
                "--material-json",
                '{"secret":"must-not-persist"}',
            )
        )
        == 0
    )
    evidence_added = json.loads(capsys.readouterr().out)
    assert evidence_added["data"]["redaction_status"] == "redacted"
    assert evidence_added["data"]["material_availability"] == "available"

    assert (
        main(
            _change_args(
                repo,
                output,
                "evidence",
                "show",
                "--plan-id",
                "cli-plan",
                "--evidence-id",
                "evidence-cli",
            )
        )
        == 0
    )
    evidence_shown = json.loads(capsys.readouterr().out)
    assert evidence_shown["operation"] == "evidence show"
    assert evidence_shown["data"]["evidence_id"] == "evidence-cli"
    assert "runner_metadata" not in evidence_shown["data"]

    assert (
        main(
            _change_args(
                repo,
                output,
                "evidence",
                "list",
                "--plan-id",
                "cli-plan",
            )
        )
        == 0
    )
    listed = json.loads(capsys.readouterr().out)
    serialized_list = json.dumps(listed)
    assert "runner_metadata" not in serialized_list
    assert "must-not-persist" not in serialized_list

    assert (
        main(
            _change_args(
                repo,
                output,
                "evidence",
                "purge",
                "--plan-id",
                "cli-plan",
                "--revision",
                "1",
                "--evidence-id",
                "evidence-cli",
                "--actor",
                "operator",
                "--reason",
                "retention-test",
                "--purge-mode",
                "replace_with_redacted",
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "purged"

    assert (
        main(
            _change_args(
                repo,
                output,
                "diff",
                "--plan-id",
                "cli-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["operation"] == "diff"

    assert (
        main(
            _change_args(
                repo,
                output,
                "verify",
                "--plan-id",
                "cli-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
            )
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["verdict"] == "SAFE_TO_PROCEED"

    assert (
        main(
            _change_args(
                repo,
                output,
                "report",
                "show",
                "--plan-id",
                "cli-plan",
                "--verification-id",
                verified["data"]["verification_id"],
            )
        )
        == 0
    )
    report_view = json.loads(capsys.readouterr().out)
    assert report_view["data"]["evidence_availability"]["evidence-cli"] == "redacted"

    assert (
        main(
            _change_args(
                repo,
                output,
                "audit",
                "export",
                "--export-id",
                "cli-audit",
            )
        )
        == 0
    )
    exported = json.loads(capsys.readouterr().out)
    export_path = Path(exported["data"]["export_path"])
    assert export_path.is_file()
    assert "must-not-persist" not in export_path.read_text(encoding="utf-8")

    assert (
        main(
            _change_args(
                repo,
                output,
                "archive",
                "--plan-id",
                "cli-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
                "--actor",
                "operator",
                "--reason",
                "complete",
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["data"]["plan_status"] == "archived"

    assert main(_change_args(repo, output, "reconcile-pins")) == 0
    assert json.loads(capsys.readouterr().out)["data"]["automatic_release"] is False

    assert (
        main(
            _change_args(
                repo,
                output,
                "plan",
                "--task",
                "unresolved target is a valid blocked payload",
                "--target",
                "symbol:fn:missing",
                "--plan-id",
                "blocked-plan",
                "--pin-id",
                "pin-blocked",
            )
        )
        == 2
    )
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "blocked"
    assert blocked["verdict"] == "PLAN_BLOCKED_UNRESOLVED_TARGET"

    assert main(_change_args(repo, output, "show", "--plan-id", "missing")) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "error"
    assert failed["error_code"] == "CHANGE_STORE_RECORD_NOT_FOUND"

    assert (
        main(
            _change_args(
                repo,
                output,
                "plan",
                "--task",
                "outside repository paths are rejected before plan activation",
                "--target",
                "path:../outside.py",
            )
        )
        == 1
    )
    path_error = json.loads(capsys.readouterr().out)
    assert path_error["error_code"] == "REPOSITORY_PATH_INVALID"


def test_change_cli_reject_abandon_archive_keep_exact_lifecycle_boundaries(
    tmp_path: Path,
    capsys,
) -> None:
    repo, output = _build_clean_repo(tmp_path)
    assert (
        main(
            _change_args(
                repo,
                output,
                "plan",
                "--task",
                "lifecycle boundary test",
                "--target",
                "symbol:fn:app.f",
                "--plan-id",
                "lifecycle-plan",
                "--pin-id",
                "pin-lifecycle",
            )
        )
        == 0
    )
    digest = json.loads(capsys.readouterr().out)["data"]["plan_revision"][
        "plan_content_digest"
    ]

    assert (
        main(
            _change_args(
                repo,
                output,
                "approve",
                "--plan-id",
                "lifecycle-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                "wrong-digest",
                "--actor",
                "reviewer",
                "--reason",
                "must bind exactly",
            )
        )
        == 1
    )
    assert (
        json.loads(capsys.readouterr().out)["error_code"] == "CHANGE_PLAN_STATE_INVALID"
    )

    assert (
        main(
            _change_args(
                repo,
                output,
                "reject",
                "--plan-id",
                "lifecycle-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
                "--actor",
                "reviewer",
                "--reason",
                "not ready",
            )
        )
        == 0
    )
    capsys.readouterr()
    service = ChangeSafetyService(repo, output, repo_id="repo")
    rejected = service.get_view("lifecycle-plan")
    assert rejected.plan_decision is not None
    assert rejected.plan_decision.status == "rejected"
    assert rejected.pin_projection["active"] is True

    assert (
        main(
            _change_args(
                repo,
                output,
                "abandon",
                "--plan-id",
                "lifecycle-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
                "--actor",
                "reviewer",
                "--reason",
                "stop work",
            )
        )
        == 0
    )
    capsys.readouterr()
    assert service.get_view("lifecycle-plan").pin_projection["active"] is True

    assert (
        main(
            _change_args(
                repo,
                output,
                "archive",
                "--plan-id",
                "lifecycle-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
                "--actor",
                "reviewer",
                "--reason",
                "archive after durable record",
            )
        )
        == 0
    )
    capsys.readouterr()
    assert service.get_view("lifecycle-plan").plan_status == "archived"
    assert service.get_view("lifecycle-plan").pin_projection["active"] is False

    assert main(["--human", *_change_args(repo, output, "list")]) == 0
    human = capsys.readouterr().out
    assert "change_contract_version 1.1.0" in human
    assert not human.lstrip().startswith("{")


@pytest.mark.parametrize(
    "command",
    [
        ("show", "--plan-id"),
        ("verify", "--plan-id", "plan", "--revision", "not-an-int"),
        (
            "evidence",
            "purge",
            "--plan-id",
            "plan",
            "--revision",
            "1",
            "--evidence-id",
            "evidence",
            "--actor",
            "operator",
            "--reason",
            "retention",
            "--purge-mode",
            "not-a-mode",
        ),
    ],
)
def test_change_argparse_errors_are_versioned_json_input_errors(
    tmp_path: Path,
    capsys,
    command: tuple[str, ...],
) -> None:
    repo, output = _build_clean_repo(tmp_path)

    exit_code = main(_change_args(repo, output, *command))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 1
    assert captured.err == ""
    assert payload["status"] == "error"
    assert payload["error_code"] == "CHANGE_CLI_INPUT_INVALID"
    assert payload["change_contract_version"] == "1.1.0"


def test_change_cli_requires_an_explicit_repository_id(
    tmp_path: Path,
    capsys,
) -> None:
    repo, output = _build_clean_repo(tmp_path)

    exit_code = main(
        [
            "--repo-root",
            str(repo),
            "--output-dir",
            str(output),
            "change",
            "list",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["repo_id"] == "unknown"
    assert payload["error_code"] == "CHANGE_CLI_INPUT_INVALID"
    assert "--repo-id" in payload["error"]["message"]


def test_change_cli_runtime_errors_redact_host_user_paths(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, output = _build_clean_repo(tmp_path)

    def fail_list(_self: ChangeSafetyService) -> list[object]:
        raise FileNotFoundError("missing C:\\Users\\alice\\private\\plans.json")

    monkeypatch.setattr(ChangeSafetyService, "list_views", fail_list)

    assert main(_change_args(repo, output, "list")) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "CHANGE_CLI_RUNTIME_ERROR"
    assert "alice" not in payload["error"]["message"]
    assert "<redacted-user-path>" in payload["error"]["message"]


@pytest.mark.parametrize("target", [":value", "path:", "path"])
def test_change_plan_rejects_malformed_target_syntax_as_structured_input_error(
    tmp_path: Path,
    capsys,
    target: str,
) -> None:
    repo, output = _build_clean_repo(tmp_path)

    exit_code = main(
        _change_args(
            repo,
            output,
            "plan",
            "--task",
            "invalid target",
            "--target",
            target,
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["error_code"] == "CHANGE_CLI_INPUT_INVALID"


def test_change_evidence_add_rejects_malformed_material_json(
    tmp_path: Path,
    capsys,
) -> None:
    repo, output = _build_clean_repo(tmp_path)

    exit_code = main(
        _change_args(
            repo,
            output,
            "evidence",
            "add",
            "--plan-id",
            "missing",
            "--revision",
            "1",
            "--plan-content-digest",
            "digest",
            "--requirement-id",
            "requirement",
            "--result",
            "pass",
            "--attestation-level",
            "self_reported",
            "--producer-identity",
            "operator",
            "--artifact-digest",
            "artifact",
            "--material-json",
            "{",
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["error_code"] == "CHANGE_CLI_INPUT_INVALID"
    assert "--material-json" in payload["error"]["message"]


def test_change_verify_blocking_verdict_uses_exit_code_two(
    tmp_path: Path,
    capsys,
) -> None:
    repo, output = _build_clean_repo(tmp_path)
    assert (
        main(
            _change_args(
                repo,
                output,
                "plan",
                "--task",
                "verify without evidence",
                "--target",
                "symbol:fn:app.f",
                "--plan-id",
                "no-evidence-plan",
            )
        )
        == 0
    )
    revision = json.loads(capsys.readouterr().out)["data"]["plan_revision"]
    digest = revision["plan_content_digest"]
    assert (
        main(
            _change_args(
                repo,
                output,
                "approve",
                "--plan-id",
                "no-evidence-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
                "--actor",
                "operator",
                "--reason",
                "reviewed",
            )
        )
        == 0
    )
    capsys.readouterr()

    exit_code = main(
        _change_args(
            repo,
            output,
            "verify",
            "--plan-id",
            "no-evidence-plan",
            "--revision",
            "1",
            "--plan-content-digest",
            digest,
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert payload["verdict"] == "INSUFFICIENT_EVIDENCE"


def test_change_verify_closes_the_revision_even_when_it_blocks(
    tmp_path: Path,
    capsys,
) -> None:
    """A blocked verify still ends the revision, so evidence must come first.

    ``docs/change-safety.md`` documents this ordering because
    the failure is late and quiet: the recovery attempt an operator would try
    next, ``evidence add``, still succeeds with exit 0, and only the following
    ``verify`` reports that the revision can no longer be verified.
    """

    repo, output = _build_clean_repo(tmp_path)
    assert (
        main(
            _change_args(
                repo,
                output,
                "plan",
                "--task",
                "verify before recording evidence",
                "--target",
                "symbol:fn:app.f",
                "--plan-id",
                "premature-verify-plan",
            )
        )
        == 0
    )
    revision = json.loads(capsys.readouterr().out)["data"]["plan_revision"]
    digest = revision["plan_content_digest"]
    requirement = revision["verification_plan"]["requirements"][0]["requirement_id"]
    assert (
        main(
            _change_args(
                repo,
                output,
                "approve",
                "--plan-id",
                "premature-verify-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
                "--actor",
                "operator",
                "--reason",
                "reviewed",
            )
        )
        == 0
    )
    capsys.readouterr()

    verify_args = _change_args(
        repo,
        output,
        "verify",
        "--plan-id",
        "premature-verify-plan",
        "--revision",
        "1",
        "--plan-content-digest",
        digest,
    )
    assert main(verify_args) == 2
    capsys.readouterr()

    # The obvious recovery still reports success, which is why the ordering has
    # to be documented rather than left for an operator to discover.
    assert (
        main(
            _change_args(
                repo,
                output,
                "evidence",
                "add",
                "--plan-id",
                "premature-verify-plan",
                "--revision",
                "1",
                "--plan-content-digest",
                digest,
                "--requirement-id",
                requirement,
                "--result",
                "pass",
                "--attestation-level",
                "self_reported",
                "--producer-identity",
                "operator",
                "--artifact-digest",
                "artifact-premature",
            )
        )
        == 0
    )
    capsys.readouterr()

    assert main(verify_args) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "error"
    assert payload["error_code"] == "CHANGE_PLAN_STATE_INVALID"
