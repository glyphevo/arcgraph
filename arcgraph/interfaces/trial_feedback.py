"""Privacy-bounded local feedback for ArcGraph Agent trials."""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Iterable, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    WithJsonSchema,
    field_validator,
)

from arcgraph import __version__
from arcgraph.interfaces.local_state import (
    LocalStateDirectoryInspectionError,
    LocalStateDirectorySyncError,
    PrivateLocalStateError,
    append_bytes_with_rollback,
    ensure_private_parent_directories,
    exclusive_file_lock,
    open_private_append,
    open_private_read,
    private_parent_directory,
    read_all,
    require_private_regular_file,
    sync_created_file_path,
)

TRIAL_FEEDBACK_SCHEMA_VERSION = "1.0.0"
MAX_FEEDBACK_RECORD_BYTES = 4096
MAX_FEEDBACK_FILE_BYTES = 10 * 1024 * 1024
DISTINCT_METRICS_FEEDBACK_LOGS_MESSAGE = (
    "Metrics and feedback logs must use distinct local paths."
)

FEEDBACK_SURFACES = ("cli", "mcp")
FEEDBACK_OUTCOMES = ("success", "partial", "failed", "not_used")
FEEDBACK_ISSUE_KINDS = (
    "discoverability",
    "incorrect_result",
    "stale_index",
    "unresolved_target",
    "truncated",
    "missing_capability",
    "latency",
    "integration",
    "recovery",
    "other",
)
FEEDBACK_STAGES = ("preflight", "query", "interpretation", "recovery")
FEEDBACK_FALLBACKS = (
    "none",
    "grep",
    "direct_read",
    "shell_cli",
    "human",
    "abandoned",
)
FEEDBACK_RESULT_STATUSES = (
    "available",
    "partial",
    "unavailable",
    "blocked",
    "error",
    "failed",
    "unknown",
)
FEEDBACK_FRESHNESS_STATUSES = ("fresh", "stale", "unknown", "not_reported")
FEEDBACK_WARNING_KINDS = (
    "focus_target_not_found",
    "index_warnings_omitted",
    "missing_test_candidate",
    "other",
    "parse_error",
    "payload_budget_exceeded",
    "query_unavailable",
    "receiver_resolution_unknown",
    "runtime_trace_wall_time_limit_exceeded",
    "source_scope_fallback",
    "stale_index",
    "test_gap",
    "typescript_frontend_unavailable",
    "unresolved_target",
    "wall_time_limit_exceeded",
    "warning_scope_unavailable",
)
# A warning kind may stop being emitted or accepted by the current request
# schema, but removing it from the reader would make an existing append-only
# v1 log unreadable. Keep this compatibility vocabulary append-only for the
# lifetime of schema 1.0.0; a breaking record-shape change requires a new
# schema version and a separate log.
FEEDBACK_V1_HISTORICAL_WARNING_KINDS = ("runtime_trace_unavailable",)
FEEDBACK_V1_READ_WARNING_KINDS = tuple(
    dict.fromkeys((*FEEDBACK_WARNING_KINDS, *FEEDBACK_V1_HISTORICAL_WARNING_KINDS))
)
MAX_WARNING_KINDS = len(FEEDBACK_WARNING_KINDS)
MAX_V1_READ_WARNING_KINDS = len(FEEDBACK_V1_READ_WARNING_KINDS)
FeedbackSurface = Literal[*FEEDBACK_SURFACES]
FeedbackOutcome = Literal[*FEEDBACK_OUTCOMES]
FeedbackIssueKind = Literal[*FEEDBACK_ISSUE_KINDS]
FeedbackStage = Literal[*FEEDBACK_STAGES]
FeedbackFallback = Literal[*FEEDBACK_FALLBACKS]
FeedbackResultStatus = Literal[*FEEDBACK_RESULT_STATUSES]
FeedbackFreshnessStatus = Literal[*FEEDBACK_FRESHNESS_STATUSES]
FeedbackWarningKind = Literal[*FEEDBACK_WARNING_KINDS]
FeedbackV1ReadWarningKind = Literal[*FEEDBACK_V1_READ_WARNING_KINDS]
SafeIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$",
    ),
]


class TrialFeedbackError(RuntimeError):
    """Base class for safe trial-feedback failures."""

    error_code = "TRIAL_FEEDBACK_ERROR"


class TrialFeedbackInputError(TrialFeedbackError):
    error_code = "TRIAL_FEEDBACK_INPUT_INVALID"


class TrialFeedbackConflictError(TrialFeedbackError):
    error_code = "TRIAL_FEEDBACK_ID_CONFLICT"


class TrialFeedbackStorageError(TrialFeedbackError):
    error_code = "TRIAL_FEEDBACK_STORAGE_UNAVAILABLE"


class TrialFeedbackLimitError(TrialFeedbackError):
    error_code = "TRIAL_FEEDBACK_LIMIT_EXCEEDED"


class TrialFeedbackDisabledError(TrialFeedbackError):
    error_code = "TRIAL_FEEDBACK_DISABLED"


class _StrictFeedbackModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TrialFeedbackRequest(_StrictFeedbackModel):
    client_event_id: uuid.UUID
    surface: FeedbackSurface
    tool_name: SafeIdentifier
    outcome: FeedbackOutcome
    issue_kind: FeedbackIssueKind
    stage: FeedbackStage
    fallback: FeedbackFallback
    result_status: FeedbackResultStatus | None = None
    freshness_status: FeedbackFreshnessStatus | None = None
    warning_kinds: list[FeedbackWarningKind] = Field(
        default_factory=list,
        max_length=MAX_WARNING_KINDS,
    )

    @field_validator("warning_kinds")
    @classmethod
    def _warning_kinds_are_unique(
        cls,
        values: list[str],
    ) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("warning kinds must be unique")
        return sorted(values)


class TrialFeedbackRecord(TrialFeedbackRequest):
    schema_version: Literal["1.0.0"] = TRIAL_FEEDBACK_SCHEMA_VERSION
    warning_kinds: list[FeedbackV1ReadWarningKind] = Field(
        default_factory=list,
        max_length=MAX_V1_READ_WARNING_KINDS,
    )
    feedback_id: str
    product_version: str
    timestamp: str


TrialFeedbackPayload = Annotated[
    dict[str, Any],
    WithJsonSchema(TrialFeedbackRequest.model_json_schema()),
]


def validate_feedback_request(
    payload: dict[str, Any],
    *,
    allowed_tool_names_by_surface: Mapping[str, Iterable[str]],
) -> TrialFeedbackRequest:
    """Validate without returning input-bearing Pydantic errors."""

    try:
        request = TrialFeedbackRequest.model_validate(payload)
    except ValidationError as exc:
        raise TrialFeedbackInputError(
            "Feedback must use the documented bounded fields and enum values."
        ) from exc
    allowed = frozenset(
        allowed_tool_names_by_surface[request.surface]
        if request.surface in allowed_tool_names_by_surface
        else ()
    )
    if request.tool_name != "unknown" and request.tool_name not in allowed:
        raise TrialFeedbackInputError(
            "Feedback tool_name must identify a supported ArcGraph operation "
            "for the selected surface or use 'unknown'."
        )
    return request


class TrialFeedbackStore:
    """Append-only, idempotent JSONL store for one project/client trial."""

    def __init__(
        self,
        path: Path,
        *,
        max_file_bytes: int = MAX_FEEDBACK_FILE_BYTES,
    ) -> None:
        if not path.is_absolute():
            raise TrialFeedbackStorageError("Feedback log path must be absolute.")
        self.path = path
        self.max_file_bytes = max_file_bytes
        self._lock = threading.Lock()

    def record(
        self,
        request: TrialFeedbackRequest,
    ) -> tuple[TrialFeedbackRecord, bool]:
        with self._lock:
            try:
                created_directories = ensure_private_parent_directories(
                    self.path.parent
                )
                with private_parent_directory(self.path.parent) as parent_fd:
                    fd, created = open_private_append(
                        self.path,
                        parent_fd=parent_fd,
                    )
                    try:
                        with exclusive_file_lock(fd):
                            require_private_regular_file(fd)
                            records, current_size = self._read_records_from_fd(fd)
                            for record in records:
                                if record.client_event_id != request.client_event_id:
                                    continue
                                if _request_payload(record) != request.model_dump(
                                    mode="json"
                                ):
                                    raise TrialFeedbackConflictError(
                                        "The client event id is already bound to "
                                        "different feedback."
                                    )
                                sync_created_file_path(parent_fd=parent_fd)
                                return record, True

                            record = TrialFeedbackRecord(
                                **request.model_dump(),
                                feedback_id=f"feedback-{uuid.uuid4().hex}",
                                product_version=__version__,
                                timestamp=datetime.now(timezone.utc).isoformat(),
                            )
                            encoded = _encode_record(record)
                            if current_size + len(encoded) > self.max_file_bytes:
                                raise TrialFeedbackLimitError(
                                    "Feedback log reached its configured local "
                                    "size limit."
                                )
                            append_bytes_with_rollback(
                                fd,
                                encoded,
                                original_size=current_size,
                                durable=True,
                            )
                            if created or created_directories:
                                sync_created_file_path(parent_fd=parent_fd)
                            return record, False
                    finally:
                        os.close(fd)
            except TrialFeedbackError:
                raise
            except LocalStateDirectoryInspectionError as exc:
                raise TrialFeedbackStorageError(
                    "Feedback reached the local log but its parent directories "
                    "could not be inspected, so durability is unproven. Re-check "
                    "that every component of the feedback log path is still a "
                    "readable local directory."
                ) from exc
            except LocalStateDirectorySyncError as exc:
                raise TrialFeedbackStorageError(
                    "Feedback reached the local log but its parent directory "
                    "entry could not be made durable. The log path and its "
                    "permissions are not the problem; select a feedback log on "
                    "a filesystem whose directories support fsync."
                ) from exc
            except (OSError, PrivateLocalStateError) as exc:
                raise TrialFeedbackStorageError(
                    "Feedback could not be written to the selected local log. "
                    "Use a new canonical private path; active POSIX logs require "
                    "parent mode 0700 and file mode 0600."
                ) from exc

    def summarize(self) -> dict[str, Any]:
        with self._lock:
            if not os.path.lexists(self.path):
                return _empty_summary(status="unavailable")
            try:
                with private_parent_directory(self.path.parent) as parent_fd:
                    fd = open_private_read(
                        self.path,
                        parent_fd=parent_fd,
                    )
                    try:
                        with exclusive_file_lock(fd):
                            require_private_regular_file(fd)
                            records, _current_size = self._read_records_from_fd(fd)
                    finally:
                        os.close(fd)
            except TrialFeedbackError:
                raise
            except (OSError, PrivateLocalStateError) as exc:
                raise TrialFeedbackStorageError(
                    "Feedback log could not be read safely. Use a canonical private "
                    "path and keep incompatible or invalid logs separate."
                ) from exc
        if not records:
            return _empty_summary(status="empty")
        return {
            "schema_version": TRIAL_FEEDBACK_SCHEMA_VERSION,
            "status": "available",
            "event_count": len(records),
            "by_surface": _counts(record.surface for record in records),
            "by_tool": _counts(record.tool_name for record in records),
            "by_outcome": _counts(record.outcome for record in records),
            "by_issue_kind": _counts(record.issue_kind for record in records),
            "by_stage": _counts(record.stage for record in records),
            "by_fallback": _counts(record.fallback for record in records),
            "by_result_status": _counts(
                record.result_status or "not_reported" for record in records
            ),
            "by_freshness_status": _counts(
                record.freshness_status or "not_reported" for record in records
            ),
            "by_warning_kind": _counts(
                warning for record in records for warning in record.warning_kinds
            ),
            "privacy": _summary_privacy(),
            "warnings": [],
        }

    def _read_records_from_fd(
        self,
        fd: int,
    ) -> tuple[list[TrialFeedbackRecord], int]:
        current_size = os.fstat(fd).st_size
        if current_size > self.max_file_bytes:
            raise TrialFeedbackLimitError(
                "Feedback log exceeds its configured local size limit."
            )
        try:
            raw = read_all(fd)
        except OSError as exc:
            raise TrialFeedbackStorageError(
                "Feedback log could not be read safely."
            ) from exc
        if raw and not raw.endswith(b"\n"):
            raise TrialFeedbackStorageError(
                "Feedback log contains an incomplete record."
            )
        records: list[TrialFeedbackRecord] = []
        for raw_line in raw.splitlines():
            if not raw_line.strip():
                continue
            if len(raw_line) + 1 > MAX_FEEDBACK_RECORD_BYTES:
                raise TrialFeedbackLimitError(
                    "Feedback log contains an oversized record."
                )
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise TrialFeedbackStorageError(
                    "Feedback log contains an invalid record."
                ) from exc
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != TRIAL_FEEDBACK_SCHEMA_VERSION
            ):
                raise TrialFeedbackStorageError(
                    "Feedback log uses an unsupported record schema version. "
                    "Preserve it for diagnosis and use a separate log for the "
                    "current schema."
                )
            try:
                records.append(TrialFeedbackRecord.model_validate(payload))
            except ValidationError as exc:
                raise TrialFeedbackStorageError(
                    "Feedback log contains an invalid record."
                ) from exc
        return records, current_size


def feedback_success_payload(
    record: TrialFeedbackRecord,
    *,
    duplicate: bool,
) -> dict[str, Any]:
    return {
        "schema_version": TRIAL_FEEDBACK_SCHEMA_VERSION,
        "status": "recorded",
        "feedback_id": record.feedback_id,
        "duplicate": duplicate,
        "read_only": False,
        "destructive": False,
        "network_access": False,
        "persistence": "local_append_only",
        "source_snippets": {"requested": False, "enabled": False},
        "warnings": [],
    }


def feedback_error_payload(exc: TrialFeedbackError) -> dict[str, Any]:
    return {
        "schema_version": TRIAL_FEEDBACK_SCHEMA_VERSION,
        "status": "error",
        "error_code": exc.error_code,
        "message": str(exc),
        "read_only": False,
        "destructive": False,
        "network_access": False,
        "persistence": "local_append_only",
        "source_snippets": {"requested": False, "enabled": False},
        "warnings": [],
    }


def _request_payload(record: TrialFeedbackRecord) -> dict[str, Any]:
    return {
        field: value
        for field, value in record.model_dump(mode="json").items()
        if field
        not in {
            "schema_version",
            "feedback_id",
            "product_version",
            "timestamp",
        }
    }


def _encode_record(record: TrialFeedbackRecord) -> bytes:
    encoded = (
        json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_FEEDBACK_RECORD_BYTES:
        raise TrialFeedbackLimitError("Feedback record exceeds the local size limit.")
    return encoded


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _empty_summary(*, status: Literal["unavailable", "empty"]) -> dict[str, Any]:
    return {
        "schema_version": TRIAL_FEEDBACK_SCHEMA_VERSION,
        "status": status,
        "event_count": 0,
        "by_surface": {},
        "by_tool": {},
        "by_outcome": {},
        "by_issue_kind": {},
        "by_stage": {},
        "by_fallback": {},
        "by_result_status": {},
        "by_freshness_status": {},
        "by_warning_kind": {},
        "privacy": _summary_privacy(),
        "warnings": [],
    }


def _summary_privacy() -> dict[str, Any]:
    return {
        "shareable": True,
        "omitted": [
            "feedback_ids",
            "client_event_ids",
            "timestamps",
            "input_path",
            "repository_ids",
            "source_paths",
            "source_text",
            "targets",
            "prompts",
            "raw_exceptions",
            "free_form_narrative",
        ],
    }
