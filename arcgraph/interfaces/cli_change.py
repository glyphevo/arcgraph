"""CLI handlers for the ``arcgraph change`` command group.

Split out of ``cli.py`` because the group is a clean leaf: it depends on no
other handler in that module, and only ``build_parser`` and ``main`` reach
back into it. Keeping it here mirrors the ``arcgraph/change`` subsystem
boundary the commands actually wrap.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from arcgraph.change.contracts import (
    CHANGE_CONTRACT_VERSION,
    ChangeIntent,
    ChangeTarget,
    EvidencePurgeEvent,
    VerificationEvidence,
    canonical_digest,
)
from arcgraph.change.errors import ChangeSafetyError
from arcgraph.change.evidence import redact_sensitive_text
from arcgraph.change.service import ChangeSafetyService
from arcgraph.change.trusted_runners import TrustedRunnerRegistry
from arcgraph.change.verdicts import verdict_is_blocking
from arcgraph.core.schemas import SCHEMA_VERSION


def handle_change_plan(args: argparse.Namespace) -> dict[str, Any]:
    repo_id = _change_repo_id(args)
    task = args.task.strip()
    if not task:
        raise ValueError("--task must not be empty")
    intent = ChangeIntent(
        repo_id=repo_id,
        intent_id=args.intent_id or f"intent-{uuid.uuid4().hex}",
        task=task,
        targets=[
            _change_target(
                target,
                repo_id=repo_id,
                resolution_policy=args.target_policy,
            )
            for target in args.target
        ],
        acceptance_criteria=list(args.acceptance_criterion),
        constraints=list(args.constraint),
        allowed_surface_changes=list(args.allow_surface_change),
        forbidden_surface_changes=list(args.forbid_surface_change),
        user_declared_protected_surfaces=list(args.protect),
    )
    view = _change_service(args).plan(
        intent,
        plan_id=args.plan_id,
        revision=args.revision,
        pin_id=args.pin_id or f"pin-{uuid.uuid4().hex}",
        openapi_input=args.openapi_input,
    )
    return _change_payload(
        args,
        operation="plan",
        data=view.model_dump(mode="json"),
        verdict=view.plan_revision.planning_verdict,
    )


def handle_change_show(args: argparse.Namespace) -> dict[str, Any]:
    view = _change_service(args).get_view(args.plan_id)
    return _change_payload(
        args,
        operation="show",
        data=view.model_dump(mode="json"),
        verdict=view.plan_revision.planning_verdict,
    )


def handle_change_list(args: argparse.Namespace) -> dict[str, Any]:
    views = _change_service(args).list_views()
    return _change_payload(
        args,
        operation="list",
        data={"plans": [view.model_dump(mode="json") for view in views]},
    )


def handle_change_approve(args: argparse.Namespace) -> dict[str, Any]:
    view = _change_service(args).approve(
        args.plan_id,
        args.revision,
        args.plan_content_digest,
        actor=args.actor,
        reason=args.reason,
    )
    return _change_payload(
        args,
        operation="approve",
        data=view.model_dump(mode="json"),
        verdict=view.plan_revision.planning_verdict,
    )


def handle_change_reject(args: argparse.Namespace) -> dict[str, Any]:
    view = _change_service(args).reject(
        args.plan_id,
        args.revision,
        args.plan_content_digest,
        actor=args.actor,
        reason=args.reason,
    )
    return _change_payload(
        args,
        operation="reject",
        data=view.model_dump(mode="json"),
        verdict=view.plan_revision.planning_verdict,
    )


def handle_change_abandon(args: argparse.Namespace) -> dict[str, Any]:
    view = _change_service(args).abandon(
        args.plan_id,
        args.revision,
        args.plan_content_digest,
        actor=args.actor,
        reason=args.reason,
    )
    return _change_payload(
        args,
        operation="abandon",
        data=view.model_dump(mode="json"),
        verdict=view.plan_revision.planning_verdict,
    )


def handle_change_diff(args: argparse.Namespace) -> dict[str, Any]:
    delta = _change_service(args).diff(
        args.plan_id,
        args.revision,
        args.plan_content_digest,
    )
    return _change_payload(
        args,
        operation="diff",
        data=delta.model_dump(mode="json"),
    )


def handle_change_verify(args: argparse.Namespace) -> dict[str, Any]:
    report = _change_service(args).verify(
        args.plan_id,
        args.revision,
        args.plan_content_digest,
    )
    return _change_payload(
        args,
        operation="verify",
        data=report.model_dump(mode="json"),
        verdict=report.verdict,
    )


def handle_change_report_show(args: argparse.Namespace) -> dict[str, Any]:
    view = _change_service(args).get_verification_report_view(
        args.plan_id,
        args.verification_id,
    )
    return _change_payload(
        args,
        operation="report show",
        data=view.model_dump(mode="json"),
        verdict=view.report.verdict,
    )


def handle_change_archive(args: argparse.Namespace) -> dict[str, Any]:
    view = _change_service(args).archive(
        args.plan_id,
        args.revision,
        args.plan_content_digest,
        actor=args.actor,
        reason=args.reason,
    )
    return _change_payload(
        args,
        operation="archive",
        data=view.model_dump(mode="json"),
    )


def handle_change_reconcile_pins(args: argparse.Namespace) -> dict[str, Any]:
    return _change_payload(
        args,
        operation="reconcile-pins",
        data=_change_service(args).reconcile_pins(),
    )


def handle_change_evidence_add(args: argparse.Namespace) -> dict[str, Any]:
    service = _change_service(args)
    recorded = service.record_evidence_submission(
        evidence_id=args.evidence_id or f"evidence-{uuid.uuid4().hex}",
        verification_requirement_id=args.requirement_id,
        plan_id=args.plan_id,
        plan_revision=args.revision,
        plan_content_digest=args.plan_content_digest,
        result=args.result,
        attestation_level=args.attestation_level,
        producer_identity=args.producer_identity,
        stdout_digest=args.stdout_digest,
        stderr_digest=args.stderr_digest,
        runner_metadata=_change_json_object(
            args.runner_metadata_json,
            argument_name="--runner-metadata-json",
        ),
        artifact_digest=args.artifact_digest,
        material=_change_json_object(
            args.material_json,
            argument_name="--material-json",
        ),
    )
    return _change_payload(
        args,
        operation="evidence add",
        data=_evidence_summary_payload(
            recorded,
            service.evidence_material_availability(
                recorded.plan_id, recorded.evidence_id
            ),
        ),
    )


def handle_change_evidence_list(args: argparse.Namespace) -> dict[str, Any]:
    service = _change_service(args)
    records = service.list_evidence(args.plan_id)
    return _change_payload(
        args,
        operation="evidence list",
        data={
            "evidence": [
                _evidence_summary_payload(
                    record,
                    service.evidence_material_availability(
                        record.plan_id, record.evidence_id
                    ),
                )
                for record in records
            ]
        },
    )


def handle_change_evidence_show(args: argparse.Namespace) -> dict[str, Any]:
    service = _change_service(args)
    record = service.get_evidence(args.plan_id, args.evidence_id)
    return _change_payload(
        args,
        operation="evidence show",
        data=_evidence_summary_payload(
            record,
            service.evidence_material_availability(record.plan_id, record.evidence_id),
        ),
    )


def handle_change_evidence_purge(args: argparse.Namespace) -> dict[str, Any]:
    service = _change_service(args)
    record = service.get_evidence(args.plan_id, args.evidence_id)
    if record.plan_revision != args.revision:
        raise ValueError("--revision does not match the immutable evidence record")
    event = EvidencePurgeEvent(
        repo_id=service.repo_id,
        purge_id=f"purge-{uuid.uuid4().hex}",
        plan_id=args.plan_id,
        plan_revision=args.revision,
        evidence_id=args.evidence_id,
        requirement_id=record.verification_requirement_id,
        actor=args.actor,
        reason=args.reason,
        purge_mode=args.purge_mode,
        artifact_digest=record.artifact_digest,
        redacted_material_digest=(
            canonical_digest({"redacted": True})
            if args.purge_mode == "replace_with_redacted"
            else None
        ),
    )
    return _change_payload(
        args,
        operation="evidence purge",
        data=service.purge_evidence(event),
    )


def handle_change_audit_export(args: argparse.Namespace) -> dict[str, Any]:
    path = _change_service(args).audit_export(args.export_id)
    export_payload = json.loads(path.read_text(encoding="utf-8"))
    return _change_payload(
        args,
        operation="audit export",
        data={
            "export_path": str(path),
            "non_authoritative": True,
            "sensitive_material_included": bool(
                export_payload.get("sensitive_material_included")
            ),
            "redaction_assurance": export_payload.get("redaction_assurance"),
        },
    )


def _change_repo_id(args: argparse.Namespace) -> str:
    value = getattr(args, "change_repo_id", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("--repo-id is required and must be a non-empty logical id")
    return value.strip()


def _change_repo_root(args: argparse.Namespace) -> Path:
    return Path(args.repo_root).resolve()


def _change_output_dir(args: argparse.Namespace) -> Path:
    output = Path(args.output_dir)
    return (
        output.resolve()
        if output.is_absolute()
        else (_change_repo_root(args) / output).resolve()
    )


def _change_service(args: argparse.Namespace) -> ChangeSafetyService:
    repo_root = _change_repo_root(args)
    registry_value = getattr(args, "trusted_runner_registry", None)
    registry = (
        TrustedRunnerRegistry(Path(registry_value), repo_root=repo_root)
        if registry_value
        else None
    )
    return ChangeSafetyService(
        repo_root,
        _change_output_dir(args),
        repo_id=_change_repo_id(args),
        trusted_runner_registry=registry,
    )


def _change_target(
    value: str,
    *,
    repo_id: str,
    resolution_policy: str,
) -> ChangeTarget:
    kind, separator, target_value = value.partition(":")
    if not separator or kind not in {"symbol", "path", "route", "resource", "contract"}:
        raise ValueError(
            "--target must use KIND:VALUE where KIND is symbol, path, route, resource, or contract"
        )
    if not target_value.strip():
        raise ValueError("--target value must not be empty")
    return ChangeTarget(
        repo_id=repo_id,
        kind=kind,
        value=target_value,
        resolution_policy=resolution_policy,
    )


def _change_json_object(value: str, *, argument_name: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{argument_name} must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{argument_name} must be a JSON object")
    return payload


def _evidence_summary_payload(
    record: VerificationEvidence,
    availability: str,
) -> dict[str, Any]:
    """Keep list/show output out of controlled Evidence Material."""

    return {
        "schema_version": record.schema_version,
        "change_contract_version": record.change_contract_version,
        "repo_id": record.repo_id,
        "evidence_id": record.evidence_id,
        "verification_requirement_id": record.verification_requirement_id,
        "plan_id": record.plan_id,
        "plan_revision": record.plan_revision,
        "plan_content_digest": record.plan_content_digest,
        "result": record.result,
        "attestation_level": record.attestation_level,
        "producer_identity": record.producer_identity,
        "redaction_status": record.redaction_status,
        "artifact_digest": record.artifact_digest,
        "captured_at": record.captured_at.isoformat(),
        "material_availability": availability,
    }


def _change_payload(
    args: argparse.Namespace,
    *,
    operation: str,
    data: dict[str, Any],
    verdict: str | None = None,
) -> dict[str, Any]:
    blocked = verdict is not None and verdict_is_blocking(verdict)  # type: ignore[arg-type]
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "change_contract_version": CHANGE_CONTRACT_VERSION,
        "repo_id": _change_repo_id(args),
        "operation": operation,
        "status": "blocked" if blocked else "success",
        "data": data,
    }
    if verdict is not None:
        payload["verdict"] = verdict
    if blocked:
        payload["_exit_code"] = 2
    return payload


def _change_repo_id_from_argv(argv: list[str]) -> str:
    for index, argument in enumerate(argv):
        if argument == "--repo-id" and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith("--repo-id="):
            return argument.partition("=")[2]
    return "unknown"


def _change_error_payload(
    args: argparse.Namespace,
    exc: Exception,
) -> dict[str, Any]:
    if isinstance(exc, ChangeSafetyError):
        detail = exc.to_payload()
        error_code = detail["code"]
        message = detail["message"]
    elif isinstance(exc, ValueError):
        error_code = "CHANGE_CLI_INPUT_INVALID"
        message = str(exc)
    else:
        error_code = "CHANGE_CLI_RUNTIME_ERROR"
        message = str(exc)
    message = redact_sensitive_text(message, redact_paths=True)[0]
    repo_id = getattr(args, "change_repo_id", "unknown")
    return {
        "schema_version": SCHEMA_VERSION,
        "change_contract_version": CHANGE_CONTRACT_VERSION,
        "repo_id": repo_id,
        "status": "error",
        "error_code": error_code,
        "error": {"code": error_code, "message": message},
    }


def _add_change_revision_binding(sub: argparse.ArgumentParser) -> None:
    """Bind a command to one exact plan revision, digest included."""
    sub.add_argument("--plan-id", required=True)
    sub.add_argument("--revision", type=int, required=True)
    sub.add_argument("--plan-content-digest", required=True)


def _add_change_actor_reason(sub: argparse.ArgumentParser) -> None:
    """Require an accountable actor and a stated reason on any decision."""
    sub.add_argument("--actor", required=True)
    sub.add_argument("--reason", required=True)


def add_change_parser(subparsers: Any) -> None:
    """Register the ``arcgraph change`` command tree.

    Lives beside the handlers it wires up: the group is 218 lines of
    argparse setup that referenced nothing else in ``build_parser``.
    """
    change = subparsers.add_parser(
        "change",
        help="Plan, verify, and audit one fail-closed surgical code change.",
    )
    change.add_argument(
        "--repo-id",
        dest="change_repo_id",
        required=True,
        help=(
            "Required logical repository id bound into Surgical Change Safety "
            "records; no cross-repository default is inferred."
        ),
    )
    change.add_argument(
        "--trusted-runner-registry",
        help=(
            "Operator-controlled JSON trusted-runner registry outside the analyzed "
            "repository. Omit for the fail-closed default of no trusted runners."
        ),
    )
    change_subparsers = change.add_subparsers(dest="change_command")
    change.set_defaults(handler=lambda args, parser=change: parser.print_help())
    change_plan = change_subparsers.add_parser(
        "plan", help="Create and activate a plan from explicit targets."
    )
    change_plan.add_argument("--task", required=True)
    change_plan.add_argument(
        "--target",
        action="append",
        default=[],
        metavar="KIND:VALUE",
        help="Explicit target: symbol, path, route, resource, or contract. Repeatable.",
    )
    change_plan.add_argument(
        "--target-policy",
        default="must_resolve",
        choices=["must_resolve", "allow_manual_review", "informational"],
        help="Resolution policy applied to every supplied target.",
    )
    change_plan.add_argument("--intent-id")
    change_plan.add_argument("--plan-id")
    change_plan.add_argument("--revision", type=int, default=1)
    change_plan.add_argument("--pin-id")
    change_plan.add_argument("--acceptance-criterion", action="append", default=[])
    change_plan.add_argument("--constraint", action="append", default=[])
    change_plan.add_argument("--allow-surface-change", action="append", default=[])
    change_plan.add_argument("--forbid-surface-change", action="append", default=[])
    change_plan.add_argument("--protect", action="append", default=[])
    change_plan.add_argument("--openapi-input")
    change_plan.set_defaults(handler=handle_change_plan)

    change_show = change_subparsers.add_parser(
        "show", help="Read the current view for an activated plan."
    )
    change_show.add_argument("--plan-id", required=True)
    change_show.set_defaults(handler=handle_change_show)

    change_list = change_subparsers.add_parser(
        "list", help="List current plan views without mutating state."
    )
    change_list.set_defaults(handler=handle_change_list)

    for command_name, command_help, handler in (
        (
            "approve",
            "Approve one exact technically ready revision.",
            handle_change_approve,
        ),
        (
            "reject",
            "Reject one exact pending revision without releasing its pin.",
            handle_change_reject,
        ),
        (
            "abandon",
            "Abandon one eligible revision without releasing its pin.",
            handle_change_abandon,
        ),
        (
            "archive",
            "Archive one terminal revision and then release its pin safely.",
            handle_change_archive,
        ),
    ):
        command = change_subparsers.add_parser(command_name, help=command_help)
        _add_change_revision_binding(command)
        _add_change_actor_reason(command)
        command.set_defaults(handler=handler)

    change_diff = change_subparsers.add_parser(
        "diff", help="Compute a current graph delta for one exact revision."
    )
    _add_change_revision_binding(change_diff)
    change_diff.set_defaults(handler=handle_change_diff)

    change_verify = change_subparsers.add_parser(
        "verify", help="Persist verification for one exact approved revision."
    )
    _add_change_revision_binding(change_verify)
    change_verify.set_defaults(handler=handle_change_verify)

    change_report = change_subparsers.add_parser(
        "report",
        help="Read an immutable verification report with current evidence availability.",
    )
    report_subparsers = change_report.add_subparsers(dest="change_report_command")
    change_report.set_defaults(
        handler=lambda args, parser=change_report: parser.print_help()
    )
    report_show = report_subparsers.add_parser(
        "show",
        help="Project current Purged/Redacted evidence availability onto one report.",
    )
    report_show.add_argument("--plan-id", required=True)
    report_show.add_argument("--verification-id", required=True)
    report_show.set_defaults(handler=handle_change_report_show)

    change_reconcile = change_subparsers.add_parser(
        "reconcile-pins", help="Report pin/store divergence without auto-release."
    )
    change_reconcile.set_defaults(handler=handle_change_reconcile_pins)

    change_evidence = change_subparsers.add_parser(
        "evidence", help="Record or inspect persisted verification evidence."
    )
    evidence_subparsers = change_evidence.add_subparsers(dest="change_evidence_command")
    change_evidence.set_defaults(
        handler=lambda args, parser=change_evidence: parser.print_help()
    )
    evidence_add = evidence_subparsers.add_parser(
        "add", help="Record exact evidence through the ChangeSafetyService ingress."
    )
    _add_change_revision_binding(evidence_add)
    evidence_add.add_argument("--evidence-id")
    evidence_add.add_argument("--requirement-id", required=True)
    evidence_add.add_argument(
        "--result",
        required=True,
        choices=["pass", "fail", "unknown", "not_run"],
    )
    evidence_add.add_argument(
        "--attestation-level",
        required=True,
        choices=["self_reported", "artifact_backed", "trusted_runner"],
    )
    evidence_add.add_argument("--producer-identity", required=True)
    evidence_add.add_argument(
        "--artifact-digest",
        required=True,
        help=(
            "Artifact SHA-256 digest. For artifact_backed/trusted_runner evidence "
            "this must equal the canonical digest of --material-json."
        ),
    )
    evidence_add.add_argument("--stdout-digest")
    evidence_add.add_argument("--stderr-digest")
    evidence_add.add_argument(
        "--runner-metadata-json",
        default="{}",
        help="JSON object retained only after sensitive-field redaction.",
    )
    evidence_add.add_argument(
        "--material-json",
        default="{}",
        help="JSON object retained only after sensitive-field redaction.",
    )
    evidence_add.set_defaults(handler=handle_change_evidence_add)

    evidence_list = evidence_subparsers.add_parser(
        "list", help="List redacted summaries for a plan's persisted evidence."
    )
    evidence_list.add_argument("--plan-id", required=True)
    evidence_list.set_defaults(handler=handle_change_evidence_list)

    evidence_show = evidence_subparsers.add_parser(
        "show", help="Read a redacted summary for one evidence record."
    )
    evidence_show.add_argument("--plan-id", required=True)
    evidence_show.add_argument("--evidence-id", required=True)
    evidence_show.set_defaults(handler=handle_change_evidence_show)

    evidence_purge = evidence_subparsers.add_parser(
        "purge", help="Purge only controlled material while retaining evidence records."
    )
    evidence_purge.add_argument("--plan-id", required=True)
    evidence_purge.add_argument("--revision", type=int, required=True)
    evidence_purge.add_argument("--evidence-id", required=True)
    _add_change_actor_reason(evidence_purge)
    evidence_purge.add_argument(
        "--purge-mode",
        required=True,
        choices=["remove_material", "replace_with_redacted"],
    )
    evidence_purge.set_defaults(handler=handle_change_evidence_purge)

    change_audit = change_subparsers.add_parser(
        "audit", help="Create a default-redacted audit export under the output root."
    )
    audit_subparsers = change_audit.add_subparsers(dest="change_audit_command")
    change_audit.set_defaults(
        handler=lambda args, parser=change_audit: parser.print_help()
    )
    audit_export = audit_subparsers.add_parser(
        "export", help="Export immutable audit records without sensitive material."
    )
    audit_export.add_argument("--export-id", required=True)
    audit_export.set_defaults(handler=handle_change_audit_export)
