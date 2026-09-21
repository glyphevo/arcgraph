"""Exact evidence ingress validation and dynamic material availability summaries."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
import re
from typing import Iterable

from arcgraph.change.contracts import (
    ChangePlanRevision,
    VerificationEvidence,
    VerificationRequirement,
    canonical_digest,
)
from arcgraph.change.errors import ChangeEvidenceError
from arcgraph.change.identities import (
    assert_code_identity_matches,
    assert_current_build_matches_code,
    capture_build_identity,
    capture_code_identity,
)
from arcgraph.change.trusted_runners import (
    TrustedRunnerRegistry,
    attestation_meets_requirement,
)
from arcgraph.core.graph_store import GraphStoreReader

_SENSITIVE_METADATA_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|secret|token|api[_-]?key|"
    r"private[_-]?key|access[_-]?key|connection[_-]?(?:string|uri))",
    re.IGNORECASE,
)
_SENSITIVE_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b", re.IGNORECASE),
    re.compile(
        r"\b[A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|"
        r"private[_-]?key)[A-Za-z0-9_-]*"
        r"\s*(?::|=|\bis\b|\bwas\b)\s*[^\s,;]+",
        re.IGNORECASE,
    ),
)
# Dotted identifiers are common graph identities, not evidence of a secret.
# Recognize compact tokens by their encoded JSON object header instead of length.
_COMPACT_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])(?=(?P<token>(?P<header>[A-Za-z0-9_-]+)\."
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*(?![A-Za-z0-9_-])))"
)
# Consume leading nonletters so punctuation/digit-prefixed URI text retains
# its prior redaction, without retrying every suffix of a long scheme token.
_URI_CREDENTIALS = re.compile(
    r"(?<![A-Za-z0-9+.-])(?P<prefix>[0-9+.-]*[A-Za-z][A-Za-z0-9+.-]*://)"
    r"[^/\s:@]+:[^/\s@]+@"
)
_WINDOWS_USER_PATH = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s\"']+(?:\\[^\s\"'<>]*)?")
_POSIX_USER_PATH = re.compile(r"/(?:home|Users)/[^/\s\"']+(?:/[^\s\"'<>]*)?")


class EvidenceIngressValidator:
    """Validate evidence against the exact revision and current code/build state."""

    def __init__(
        self,
        repo_root: Path,
        output_dir: Path,
        *,
        repo_id: str,
        trusted_runner_registry: TrustedRunnerRegistry | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.output_dir = output_dir.resolve()
        self.repo_id = repo_id
        self.trusted_runner_registry = trusted_runner_registry or TrustedRunnerRegistry(
            None,
            repo_root=self.repo_root,
        )

    def validate(
        self,
        evidence: VerificationEvidence,
        revision: ChangePlanRevision,
        *,
        material: dict[str, object] | None = None,
    ) -> VerificationEvidence:
        if evidence.repo_id != self.repo_id or revision.repo_id != self.repo_id:
            raise ChangeEvidenceError(
                "evidence and revision must use the configured repo_id",
                code="EVIDENCE_REPOSITORY_MISMATCH",
            )
        if (
            evidence.plan_id != revision.plan_id
            or evidence.plan_revision != revision.revision
            or evidence.plan_content_digest != revision.plan_content_digest
        ):
            raise ChangeEvidenceError(
                "evidence does not bind the exact plan revision digest",
                code="EVIDENCE_BINDING_MISMATCH",
            )
        requirement = _requirement_for(revision, evidence.verification_requirement_id)
        current_build = capture_build_identity(
            self.repo_root,
            self.output_dir,
            repo_id=self.repo_id,
        )
        source_paths = [
            record.path
            for record in GraphStoreReader.from_current(self.output_dir).iter_files()
        ]
        actual_code = capture_code_identity(
            self.repo_root,
            repo_id=self.repo_id,
            build_identity=current_build,
            source_paths=source_paths,
        )
        assert_current_build_matches_code(current_build, actual_code)
        assert_code_identity_matches(evidence.current_code_identity, actual_code)
        if not attestation_meets_requirement(evidence, requirement):
            raise ChangeEvidenceError(
                "evidence attestation level is below the requirement",
                code="EVIDENCE_ATTESTATION_INSUFFICIENT",
            )
        self.trusted_runner_registry.validate(evidence, requirement)
        if (
            not evidence.producer_identity.strip()
            or not evidence.artifact_digest.strip()
        ):
            raise ChangeEvidenceError(
                "evidence producer identity and artifact digest are required",
                code="EVIDENCE_IDENTITY_INCOMPLETE",
            )
        if evidence.attestation_level in {
            "artifact_backed",
            "trusted_runner",
        } and evidence.artifact_digest != canonical_digest(material or {}):
            raise ChangeEvidenceError(
                "artifact digest does not bind the submitted evidence material",
                code="EVIDENCE_ARTIFACT_DIGEST_MISMATCH",
            )
        return redact_evidence_metadata(evidence)


def evidence_summary(
    evidence: Iterable[VerificationEvidence],
    availability: dict[str, str],
) -> dict[str, object]:
    """Return a dynamic availability projection without mutating evidence records."""

    records = list(evidence)
    by_result: dict[str, int] = {}
    by_availability: dict[str, int] = {}
    for record in records:
        by_result[record.result] = by_result.get(record.result, 0) + 1
        state = availability.get(record.evidence_id, "missing")
        by_availability[state] = by_availability.get(state, 0) + 1
    return {
        "total": len(records),
        "by_result": dict(sorted(by_result.items())),
        "by_availability": dict(sorted(by_availability.items())),
    }


def redact_evidence_metadata(evidence: VerificationEvidence) -> VerificationEvidence:
    """Redact sensitive runner metadata before an immutable record is written."""

    metadata, changed = _redact_mapping(evidence.runner_metadata)
    if not changed:
        return evidence
    payload = evidence.model_dump(mode="python")
    payload["runner_metadata"] = metadata
    payload["redaction_status"] = "redacted"
    payload["payload_digest"] = ""
    return VerificationEvidence.model_validate(payload)


def redact_evidence_material(material: dict[str, object] | None) -> dict[str, object]:
    """Return an evidence-material projection safe for the local state store."""

    if material is None:
        return {}
    redacted, _changed = _redact_mapping(material)
    return redacted


def redact_sensitive_text(
    value: str,
    *,
    redact_paths: bool = True,
) -> tuple[str, bool]:
    """Redact well-known secret values and host-user paths in free text."""

    redacted = _redact_compact_tokens(value)
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    redacted = _URI_CREDENTIALS.sub(r"\g<prefix><redacted>@", redacted)
    if redact_paths:
        redacted = _WINDOWS_USER_PATH.sub("<redacted-user-path>", redacted)
        redacted = _POSIX_USER_PATH.sub("<redacted-user-path>", redacted)
    return redacted, redacted != value


def _redact_compact_tokens(value: str) -> str:
    # Lookahead permits overlapping candidates: an invalid `session.header.payload`
    # must not consume the start of the valid `header.payload.signature` token.
    spans: list[tuple[int, int]] = []
    for match in _COMPACT_TOKEN.finditer(value):
        if not _is_compact_header(match.group("header")):
            continue
        start, end = match.span("token")
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(end, spans[-1][1]))
        else:
            spans.append((start, end))
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.extend((value[cursor:start], "<redacted>"))
        cursor = end
    parts.append(value[cursor:])
    return "".join(parts)


def _is_compact_header(header: str) -> bool:
    try:
        decoded = base64.b64decode(
            header + "=" * (-len(header) % 4), altchars=b"-_", validate=True
        ).decode("utf-8")
        parsed = json.loads(decoded)
    except (ValueError, binascii.Error):
        return False
    # Redaction is conservative recognition, not signature or claim validation.
    return isinstance(parsed, dict)


def _requirement_for(
    revision: ChangePlanRevision,
    requirement_id: str,
) -> VerificationRequirement:
    for requirement in revision.verification_plan.requirements:
        if requirement.requirement_id == requirement_id:
            return requirement
    raise ChangeEvidenceError(
        "evidence requirement does not exist in the revision",
        code="EVIDENCE_REQUIREMENT_INVALID",
    )


def _redact_mapping(value: object) -> tuple[object, bool]:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        changed = False
        for key, child in value.items():
            if not isinstance(key, str):
                raise ChangeEvidenceError(
                    "runner metadata and material keys must be strings",
                    code="EVIDENCE_REDACTION_INVALID",
                )
            if _SENSITIVE_METADATA_KEY.search(key):
                result[key] = "<redacted>"
                changed = True
                continue
            redacted, child_changed = _redact_mapping(child)
            result[key] = redacted
            changed = changed or child_changed
        return result, changed
    if isinstance(value, list):
        result: list[object] = []
        changed = False
        for child in value:
            redacted, child_changed = _redact_mapping(child)
            result.append(redacted)
            changed = changed or child_changed
        return result, changed
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value, False
    raise ChangeEvidenceError(
        "runner metadata and material must contain JSON-compatible values",
        code="EVIDENCE_REDACTION_INVALID",
    )
