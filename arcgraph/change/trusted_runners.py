"""Operator-controlled trusted-runner registry for evidence attestation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcgraph.change.contracts import VerificationEvidence, VerificationRequirement
from arcgraph.change.errors import TrustedRunnerError

_ATTESTATION_RANK = {
    "self_reported": 0,
    "artifact_backed": 1,
    "trusted_runner": 2,
}
_REQUIRED_ENTRY_FIELDS = frozenset(
    {
        "runner_identity",
        "active",
        "allowed_repo_ids",
        "allowed_requirement_ids",
        "allowed_commands",
    }
)


class TrustedRunnerRegistry:
    """Read a registry only from an operator-controlled path outside the repo."""

    def __init__(self, registry_path: Path | None, *, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.registry_path: Path | None = None
        if registry_path is None:
            return
        supplied = registry_path.absolute()
        if supplied.is_symlink() or _is_relative_to(supplied, self.repo_root):
            raise TrustedRunnerError(
                "a trusted-runner registry cannot be supplied from the analyzed repo"
            )
        resolved = supplied.resolve(strict=False)
        if _is_relative_to(resolved, self.repo_root):
            raise TrustedRunnerError(
                "a trusted-runner registry cannot resolve inside the analyzed repo"
            )
        self.registry_path = resolved

    def entries(self) -> list[dict[str, Any]]:
        """Return no entries by default; corrupt external input is fail-closed."""

        if self.registry_path is None or not self.registry_path.exists():
            return []
        if self.registry_path.is_symlink() or not self.registry_path.is_file():
            raise TrustedRunnerError("trusted-runner registry path is unsafe")
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TrustedRunnerError("trusted-runner registry cannot be read") from exc
        if not isinstance(payload, dict) or payload.get("registry_version") != "1.0":
            raise TrustedRunnerError("trusted-runner registry version is unsupported")
        entries = payload.get("runners")
        if not isinstance(entries, list) or not all(
            isinstance(item, dict) for item in entries
        ):
            raise TrustedRunnerError(
                "trusted-runner registry has invalid runner entries"
            )
        for entry in entries:
            _validate_entry(entry)
        return entries

    def validate(
        self,
        evidence: VerificationEvidence,
        requirement: VerificationRequirement,
    ) -> None:
        """Validate a claimed trusted runner against independent allowlists."""

        if evidence.attestation_level != "trusted_runner":
            return
        command = evidence.runner_metadata.get("command")
        if not isinstance(command, str) or not command.strip():
            raise TrustedRunnerError(
                "trusted-runner evidence requires a non-empty declared command"
            )
        for entry in self.entries():
            if entry.get("runner_identity") != evidence.producer_identity:
                continue
            if entry.get("active") is not True:
                continue
            if not _allowed(entry.get("allowed_repo_ids"), evidence.repo_id):
                continue
            if not _allowed(
                entry.get("allowed_requirement_ids"), requirement.requirement_id
            ):
                continue
            if not _allowed(entry.get("allowed_commands"), command):
                continue
            return
        raise TrustedRunnerError(
            "evidence claimed trusted_runner but no operator registry entry matched"
        )


def attestation_meets_requirement(
    evidence: VerificationEvidence,
    requirement: VerificationRequirement,
) -> bool:
    return (
        _ATTESTATION_RANK[evidence.attestation_level]
        >= _ATTESTATION_RANK[requirement.minimum_attestation_level]
    )


def _allowed(values: Any, value: Any) -> bool:
    if not isinstance(values, list) or not values:
        return False
    return "*" in values or value in values


def _validate_entry(entry: dict[str, Any]) -> None:
    if set(entry) != _REQUIRED_ENTRY_FIELDS:
        raise TrustedRunnerError(
            "trusted-runner registry entry has an unsupported schema"
        )
    if (
        not isinstance(entry["runner_identity"], str)
        or not entry["runner_identity"].strip()
    ):
        raise TrustedRunnerError("trusted-runner identity must be a non-empty string")
    if not isinstance(entry["active"], bool):
        raise TrustedRunnerError("trusted-runner active flag must be boolean")
    for field in (
        "allowed_repo_ids",
        "allowed_requirement_ids",
        "allowed_commands",
    ):
        values = entry[field]
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value for value in values)
        ):
            raise TrustedRunnerError(
                f"trusted-runner registry {field} must be a non-empty string list"
            )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
