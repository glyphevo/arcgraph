from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph.change.contracts import (
    CodeIdentity,
    VerificationEvidence,
    VerificationRequirement,
)
from arcgraph.change.errors import TrustedRunnerError
from arcgraph.change.trusted_runners import TrustedRunnerRegistry


def _requirement() -> VerificationRequirement:
    return VerificationRequirement(
        repo_id="repo",
        requirement_id="requirement",
        category="test",
        level="required",
        subject="subject",
        reason="reason",
        acceptance_condition="pass",
        minimum_attestation_level="trusted_runner",
    )


def _evidence(*, producer_identity: str = "operator-runner") -> VerificationEvidence:
    return VerificationEvidence(
        repo_id="repo",
        evidence_id="evidence",
        verification_requirement_id="requirement",
        plan_id="plan",
        plan_revision=1,
        plan_content_digest="digest",
        result="pass",
        current_code_identity=CodeIdentity(
            repo_id="repo",
            working_tree_clean=True,
            working_tree_status_digest="clean",
            file_manifest_digest="manifest",
            index_version="index",
            build_identity_digest="build",
        ),
        attestation_level="trusted_runner",
        producer_identity=producer_identity,
        runner_metadata={"command": "pytest -q"},
        artifact_digest="artifact",
    )


def _registry_payload() -> dict[str, object]:
    return {
        "registry_version": "1.0",
        "runners": [
            {
                "runner_identity": "operator-runner",
                "active": True,
                "allowed_repo_ids": ["repo"],
                "allowed_requirement_ids": ["requirement"],
                "allowed_commands": ["pytest -q"],
            }
        ],
    }


def test_default_registry_rejects_claimed_trusted_runner(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(TrustedRunnerError):
        TrustedRunnerRegistry(None, repo_root=repo).validate(
            _evidence(), _requirement()
        )


def test_registry_must_be_external_and_exactly_match_allowlists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(TrustedRunnerError):
        TrustedRunnerRegistry(repo / "runner-registry.json", repo_root=repo)

    registry_path = tmp_path / "operator-registry.json"
    registry_path.write_text(
        json.dumps(_registry_payload()),
        encoding="utf-8",
    )
    registry = TrustedRunnerRegistry(registry_path, repo_root=repo)

    registry.validate(_evidence(), _requirement())
    with pytest.raises(TrustedRunnerError):
        registry.validate(_evidence(producer_identity="forged"), _requirement())


def test_corrupt_external_registry_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry_path = tmp_path / "operator-registry.json"
    registry_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(TrustedRunnerError):
        TrustedRunnerRegistry(registry_path, repo_root=repo).entries()
