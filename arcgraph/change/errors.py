"""Typed, fail-closed errors for Surgical Change Safety."""

from __future__ import annotations


class ChangeSafetyError(RuntimeError):
    """Base error with a stable machine-readable failure code."""

    code = "CHANGE_SAFETY_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")

    def to_payload(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self).split(": ", 1)[-1]}


class ChangeContractVersionUnsupported(ChangeSafetyError):
    code = "CHANGE_CONTRACT_VERSION_UNSUPPORTED"


class ChangeContractSchemaUnsupported(ChangeSafetyError):
    code = "CHANGE_SCHEMA_VERSION_UNSUPPORTED"


class ChangeContractDigestMismatch(ChangeSafetyError):
    code = "PLAN_CONTENT_DIGEST_MISMATCH"


class RepositoryPathError(ChangeSafetyError):
    code = "REPOSITORY_PATH_INVALID"


class OutputContainmentError(ChangeSafetyError):
    code = "OUTPUT_PATH_OUTSIDE_CONTAINMENT"


class ChangeStoreCorrupt(ChangeSafetyError):
    code = "CHANGE_STORE_CORRUPT"


class ChangeStoreNotFound(ChangeSafetyError):
    code = "CHANGE_STORE_RECORD_NOT_FOUND"


class BaselineIntegrityMismatch(ChangeSafetyError):
    code = "BASELINE_INDEX_INTEGRITY_MISMATCH"


class BaselineSourceUnavailable(ChangeSafetyError):
    code = "BASELINE_SOURCE_UNAVAILABLE"


class BaselineGitObjectMissing(ChangeSafetyError):
    code = "BASELINE_GIT_OBJECT_MISSING"


class BaselineSourceIdentityMismatch(ChangeSafetyError):
    code = "BASELINE_SOURCE_IDENTITY_MISMATCH"


class CurrentCodeIdentityMismatch(ChangeSafetyError):
    code = "CURRENT_CODE_IDENTITY_MISMATCH"


class CurrentBuildCodeIdentityMismatch(ChangeSafetyError):
    code = "CURRENT_BUILD_CODE_IDENTITY_MISMATCH"


class ChangePlanStateError(ChangeSafetyError):
    code = "CHANGE_PLAN_STATE_INVALID"


class ChangeEvidenceError(ChangeSafetyError):
    code = "CHANGE_EVIDENCE_INVALID"


class TrustedRunnerError(ChangeSafetyError):
    code = "TRUSTED_RUNNER_UNAVAILABLE"


class GraphDeltaIncomplete(ChangeSafetyError):
    code = "GRAPH_DELTA_INCOMPLETE"


class StableIdentityCollision(ChangeSafetyError):
    code = "STABLE_IDENTITY_COLLISION"
