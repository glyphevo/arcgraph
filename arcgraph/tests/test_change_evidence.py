from __future__ import annotations

import base64
import json

import pytest

from arcgraph.change.contracts import (
    EvidencePurgeEvent,
    VerificationEvidence,
    canonical_digest,
)
from arcgraph.change.errors import (
    ChangeEvidenceError,
    ChangeStoreCorrupt,
    CurrentBuildCodeIdentityMismatch,
)
from arcgraph.change.evidence import redact_sensitive_text
from arcgraph.tests.change_safety_helpers import (
    activated_approved_plan,
    current_code_identity,
)


def _evidence(plan, code, *, evidence_id: str = "evidence") -> VerificationEvidence:
    requirement = plan.verification_plan.requirements[0]
    return VerificationEvidence(
        repo_id="repo",
        evidence_id=evidence_id,
        verification_requirement_id=requirement.requirement_id,
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        plan_content_digest=plan.plan_content_digest,
        result="pass",
        current_code_identity=code,
        attestation_level="self_reported",
        producer_identity="local-test",
        runner_metadata={"command": "pytest -q", "token": "do-not-store"},
        artifact_digest="artifact-digest",
    )


def test_evidence_ingress_binds_exact_revision_and_redacts_material(
    tmp_path,
) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    evidence = _evidence(plan, current_code_identity(repo, output))

    persisted = service.record_evidence(
        evidence,
        material={"token": "do-not-store", "summary": "passed"},
    )

    assert persisted.redaction_status == "redacted"
    assert persisted.runner_metadata["token"] == "<redacted>"
    assert (
        service.get_evidence(plan.plan_id, evidence.evidence_id).runner_metadata[
            "token"
        ]
        == "<redacted>"
    )
    assert (
        service.store.evidence_material_projection(plan.plan_id, evidence.evidence_id)[
            "availability"
        ]
        == "available"
    )


def test_evidence_redaction_scans_values_not_only_key_names(tmp_path) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    evidence = _evidence(plan, current_code_identity(repo, output)).model_copy(
        update={
            "runner_metadata": {
                "note": "Bearer secret-token-value",
                "private_key_path": "C:\\Users\\alice\\.ssh\\id_ed25519",
                "database": "postgres://alice:password@example.invalid/db",
            }
        }
    )

    persisted = service.record_evidence(
        evidence,
        material={"note": "sk-abcdefghijklmnopqrstuv"},
    )
    serialized = str(persisted.runner_metadata)

    assert "secret-token-value" not in serialized
    assert "alice" not in serialized
    assert "password@" not in serialized


@pytest.mark.parametrize(
    ("value", "forbidden"),
    [
        ("uppercase key SK-ABCDEFGHIJKLMNOP", "SK-ABCDEFGHIJKLMNOP"),
        ("mixed GitHub GhP_ABCDEFGHIJKLMNOPQRSTUVWX", "GhP_"),
        ("lower AWS akia1234567890abcdef", "akia1234567890abcdef"),
        ("the PaSsWoRd is swordfish", "swordfish"),
        ("clientSecretValue=joined-secret", "joined-secret"),
    ],
)
def test_sensitive_text_redaction_covers_case_language_and_joined_names(
    value: str,
    forbidden: str,
) -> None:
    redacted, changed = redact_sensitive_text(value)

    assert changed
    assert forbidden not in redacted


@pytest.mark.parametrize(
    "value",
    [
        "method:tests.test_channels.TestChannelRegistry.test_all_channels_registered",
        "method:tests.test_doctor.TestDoctor.test_check_all_collects_channel_results",
        "long_module_name.LongClassName.long_method_name",
        "abcdefghijk.abcdefghijk.abcdefghijk",
        "W10.e30.signature",  # JSON array is not a token header.
        "////.e30.signature",
    ],
)
def test_sensitive_text_preserves_ordinary_qualified_names(value: str) -> None:
    assert redact_sensitive_text(value) == (value, False)


@pytest.mark.parametrize("header", [{"alg": "HS256", "typ": "JWT"}, {"alg": "none"}])
@pytest.mark.parametrize("signature", ["c2lnbmF0dXJl", ""])
@pytest.mark.parametrize("payload", ["e30", "eyJzdWIiOiJzeW50aGV0aWMtdGVzdCJ9"])
def test_sensitive_text_redacts_structured_tokens_with_short_segments(
    header: dict[str, str], signature: str, payload: str
) -> None:
    # Whitespace in JSON and short payloads must not evade detection.
    encoded = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    token = f"{encoded}.{payload}.{signature}"
    value = f"received ({token}); retry"
    assert redact_sensitive_text(value) == ("received (<redacted>); retry", True)


def test_artifact_backed_evidence_digest_must_bind_material(tmp_path) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    material = {"summary": "passed", "count": 3}
    evidence = _evidence(plan, current_code_identity(repo, output)).model_copy(
        update={
            "attestation_level": "artifact_backed",
            "artifact_digest": canonical_digest(material),
        }
    )

    persisted = service.record_evidence(evidence, material=material)

    assert persisted.artifact_digest == canonical_digest(material)


def test_artifact_backed_evidence_rejects_a_self_reported_digest(tmp_path) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    evidence = _evidence(plan, current_code_identity(repo, output)).model_copy(
        update={
            "attestation_level": "artifact_backed",
            "artifact_digest": "not-the-material-digest",
        }
    )

    with pytest.raises(ChangeEvidenceError, match="artifact digest"):
        service.record_evidence(evidence, material={"summary": "passed"})

    assert service.list_evidence(plan.plan_id) == []


def test_evidence_ingress_rejects_mismatched_revision_without_visible_record(
    tmp_path,
) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    evidence = _evidence(plan, current_code_identity(repo, output)).model_copy(
        update={"verification_requirement_id": "not-a-plan-requirement"}
    )

    with pytest.raises(ChangeEvidenceError):
        service.record_evidence(evidence)

    assert service.list_evidence(plan.plan_id) == []


def test_evidence_ingress_rejects_current_build_source_mismatch(tmp_path) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    old_code = current_code_identity(repo, output)
    (repo / "src" / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")

    with pytest.raises(CurrentBuildCodeIdentityMismatch):
        service.record_evidence(_evidence(plan, old_code))

    assert service.list_evidence(plan.plan_id) == []


def test_evidence_purge_preserves_record_and_marks_dynamic_availability(
    tmp_path,
) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    evidence = service.record_evidence(
        _evidence(plan, current_code_identity(repo, output))
    )
    event = EvidencePurgeEvent(
        repo_id="repo",
        purge_id="purge",
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        evidence_id=evidence.evidence_id,
        requirement_id=evidence.verification_requirement_id,
        actor="test",
        reason="redact retained material",
        purge_mode="replace_with_redacted",
        artifact_digest=evidence.artifact_digest,
        redacted_material_digest=canonical_digest({"redacted": True}),
    )

    result = service.purge_evidence(event)

    assert result["status"] == "purged"
    assert service.get_evidence(plan.plan_id, evidence.evidence_id).payload_digest
    assert (
        service.store.evidence_material_projection(plan.plan_id, evidence.evidence_id)[
            "availability"
        ]
        == "redacted"
    )
    assert service.store.list_purge_events(plan.plan_id)[0].redacted_material_digest


def test_evidence_purge_rejects_a_digest_not_bound_to_redacted_material(
    tmp_path,
) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    evidence = service.record_evidence(
        _evidence(plan, current_code_identity(repo, output))
    )
    event = EvidencePurgeEvent(
        repo_id="repo",
        purge_id="purge-bad-digest",
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        evidence_id=evidence.evidence_id,
        requirement_id=evidence.verification_requirement_id,
        actor="test",
        reason="bad digest must not become authoritative",
        purge_mode="replace_with_redacted",
        artifact_digest=evidence.artifact_digest,
        redacted_material_digest="not-the-retained-projection",
    )

    with pytest.raises(ChangeStoreCorrupt, match="redacted material digest"):
        service.purge_evidence(event)


def test_evidence_purge_is_monotonic_and_cannot_restore_material(tmp_path) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    evidence = service.record_evidence(
        _evidence(plan, current_code_identity(repo, output))
    )

    def event(purge_id: str, purge_mode: str) -> EvidencePurgeEvent:
        return EvidencePurgeEvent(
            repo_id="repo",
            purge_id=purge_id,
            plan_id=plan.plan_id,
            plan_revision=plan.revision,
            evidence_id=evidence.evidence_id,
            requirement_id=evidence.verification_requirement_id,
            actor="test",
            reason="retention",
            purge_mode=purge_mode,  # type: ignore[arg-type]
            artifact_digest=evidence.artifact_digest,
            redacted_material_digest=(
                canonical_digest({"redacted": True})
                if purge_mode == "replace_with_redacted"
                else None
            ),
        )

    service.purge_evidence(event("redact", "replace_with_redacted"))
    with pytest.raises(ChangeStoreCorrupt, match="already redacted"):
        service.purge_evidence(event("redact-again", "replace_with_redacted"))

    service.purge_evidence(event("remove", "remove_material"))
    with pytest.raises(ChangeStoreCorrupt, match="cannot be restored"):
        service.purge_evidence(event("restore", "replace_with_redacted"))


@pytest.mark.parametrize("prefix", ["session.", "auth.header.", "word." * 30])
def test_dotted_prefix_does_not_consume_valid_token(prefix):
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2ln"
    value = f"{prefix}{token} and {prefix}{token}"
    assert redact_sensitive_text(value) == (
        f"{prefix}<redacted> and {prefix}<redacted>",
        True,
    )
    from arcgraph.change.evidence import redact_evidence_material

    assert redact_evidence_material({"note": value}) == {
        "note": f"{prefix}<redacted> and {prefix}<redacted>"
    }


def test_overlapping_valid_compact_tokens_redact_union_of_spans():
    header = "eyJhbGciOiJIUzI1NiJ9"
    assert redact_sensitive_text(f"{header}.{header}.e30.c2ln") == ("<redacted>", True)


def test_dotted_token_is_detected_and_redacted_at_shared_output_boundaries(tmp_path):
    from arcgraph.change.store import _contains_detectable_sensitive_material
    from arcgraph.interfaces.mcp_tools import _sanitize_free_text

    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2ln"
    value = "session." + token
    assert _contains_detectable_sensitive_material({"note": value})
    result = _sanitize_free_text(value, tmp_path, ())
    assert token not in result
    assert not _contains_detectable_sensitive_material({"note": result})


@pytest.mark.parametrize(
    "uri",
    [
        "postgres://alice:pass@example.invalid/db",
        "https://alice:pass@example.invalid/a",
        "git+ssh://alice:pass@example.invalid/repo",
        "x.https://alice:pass@example.invalid/a",
        "(https://alice:pass@example.invalid/a)",
        ".https://alice:pass@example.invalid/a",
        "1.https://alice:pass@example.invalid/a",
        "-https://alice:pass@example.invalid/a",
        "+https://alice:pass@example.invalid/a",
        "123https://alice:pass@example.invalid/a",
    ],
)
def test_uri_boundary_preserves_credential_redaction(uri):
    result, changed = redact_sensitive_text(uri)
    assert changed
    assert result == uri.replace("alice:pass@", "<redacted>@")


def test_long_dotted_text_redaction_has_bounded_runtime():
    # The old URI regex retries the entire suffix at every character. Bound
    # this regression in a subprocess, so a recurrence cannot hang pytest.
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    code = 'import sys; sys.path.insert(0, sys.argv[1]); from arcgraph.change.evidence import redact_sensitive_text; texts=("abcdefghijk."*200000, "1234567890-."*200000); assert all(redact_sensitive_text(text)==(text,False) for text in texts)'
    subprocess.run(
        [sys.executable, "-I", "-c", code, str(root)], check=True, timeout=10
    )
