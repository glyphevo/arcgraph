"""Convert offline OpenTelemetry span dumps into ArcGraph runtime trace payloads."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class OtelSpanConversion:
    payload: dict[str, Any]
    metrics: dict[str, Any]
    status: str


@dataclass
class OtelSpanConverter:
    repo_root: Path
    span_path: str | Path
    commit_sha: str | None = None
    index_version: str | None = None
    max_spans: int = 50_000
    max_file_bytes: int = 10 * 1024 * 1024
    max_seconds: float = 120.0
    _diagnostics: list[dict[str, str]] = field(default_factory=list, init=False)
    _redacted_attributes: int = 0

    SENSITIVE_ATTRIBUTE_TOKENS = (
        "arg",
        "authorization",
        "body",
        "cookie",
        "header",
        "headers",
        "jwt",
        "password",
        "payload",
        "request",
        "response",
        "secret",
        "token",
    )
    SOURCE_KEYS = (
        "arcgraph.source",
        "arcgraph.source_id",
        "arcgraph.caller",
        "arcgraph.caller_id",
    )
    TARGET_KEYS = (
        "arcgraph.target",
        "arcgraph.target_id",
        "arcgraph.callee",
        "arcgraph.callee_id",
    )

    def convert(self) -> OtelSpanConversion:
        started = time.monotonic()
        path = self._absolute_path(Path(self.span_path))
        spans: list[dict[str, Any]] = []
        parse_errors = 0
        missing = 0
        too_large = 0
        if not path.exists():
            missing = 1
            self._diagnostic(
                "runtime_trace_otel_missing",
                f"OpenTelemetry span file does not exist: {path}",
            )
        elif path.stat().st_size > self.max_file_bytes:
            too_large = 1
            self._diagnostic(
                "runtime_trace_otel_too_large",
                (
                    "OpenTelemetry span file exceeds max_file_bytes="
                    f"{self.max_file_bytes}: {path}"
                ),
            )
        else:
            try:
                spans = self._load_spans(path)
            except (OSError, json.JSONDecodeError) as exc:
                parse_errors = 1
                self._diagnostic("runtime_trace_otel_parse_error", str(exc))

        max_spans = max(1, self.max_spans)
        limited = max(0, len(spans) - max_spans)
        if limited:
            self._diagnostic(
                "runtime_trace_otel_span_limit_exceeded",
                f"OpenTelemetry dump has {len(spans)} span(s); imported first {max_spans}.",
            )

        events: list[dict[str, Any]] = []
        processed = 0
        unmapped = 0
        wall_time_limited = False
        for span in spans[:max_spans]:
            if self._wall_time_exceeded(started):
                wall_time_limited = True
                self._diagnostic(
                    "runtime_trace_otel_wall_time_limit_exceeded",
                    f"OpenTelemetry import exceeded max_seconds={self.max_seconds}.",
                )
                break
            processed += 1
            event = self._event_from_span(span)
            if event is None:
                unmapped += 1
                continue
            events.append(event)

        if self._redacted_attributes:
            self._diagnostic(
                "runtime_trace_otel_redacted_attributes",
                (
                    "Ignored "
                    f"{self._redacted_attributes} sensitive OpenTelemetry attribute(s)."
                ),
            )

        status = (
            "partial"
            if (
                self._diagnostics
                or unmapped
                or limited
                or parse_errors
                or missing
                or too_large
                or wall_time_limited
            )
            else "available"
        )
        trace_run = {
            "trace_run_id": f"otel-{int(time.time() * 1000)}",
            "source": "opentelemetry",
            "commit_sha": self.commit_sha,
            "index_version": self.index_version,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "frontend_version": "ArcGraph-otel-span-import-v1",
        }
        metrics = {
            "otel_spans_total": len(spans),
            "otel_spans_processed": processed,
            "otel_spans_converted": len(events),
            "otel_spans_unmapped": unmapped,
            "otel_spans_limited": limited,
            "otel_parse_errors": parse_errors,
            "otel_missing": missing,
            "otel_too_large": too_large,
            "otel_redacted_attributes": self._redacted_attributes,
            "otel_wall_time_limited": wall_time_limited,
        }
        return OtelSpanConversion(
            payload={
                "trace_run": trace_run,
                "events": events,
                "diagnostics": self._diagnostics,
            },
            metrics=metrics,
            status=status,
        )

    def _load_spans(self, path: Path) -> list[dict[str, Any]]:
        text = path.read_text(encoding="utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return self._load_jsonl_spans(text)
        return self._extract_spans(payload)

    def _load_jsonl_spans(self, text: str) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) <= 1:
            raise json.JSONDecodeError("Invalid OTLP JSON or JSONL span dump", text, 0)
        for line in lines:
            spans.extend(self._extract_spans(json.loads(line)))
        return spans

    def _extract_spans(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            spans: list[dict[str, Any]] = []
            for item in payload:
                spans.extend(self._extract_spans(item))
            return spans
        if not isinstance(payload, dict):
            return []
        if isinstance(payload.get("spans"), list):
            return [span for span in payload["spans"] if isinstance(span, dict)]
        if isinstance(payload.get("resourceSpans"), list):
            return self._extract_resource_spans(payload["resourceSpans"])
        if "spanId" in payload or "attributes" in payload:
            return [payload]
        return []

    def _extract_resource_spans(
        self, resource_spans: list[Any]
    ) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        for resource_span in resource_spans:
            if not isinstance(resource_span, dict):
                continue
            for scope_key in ("scopeSpans", "instrumentationLibrarySpans"):
                for scope_span in resource_span.get(scope_key, []):
                    if isinstance(scope_span, dict):
                        spans.extend(self._extract_spans(scope_span))
        return spans

    def _event_from_span(self, span: dict[str, Any]) -> dict[str, Any] | None:
        attributes = self._span_attributes(span)
        self._redacted_attributes += sum(
            1 for key in attributes if self._is_sensitive_attribute(key)
        )
        source = self._first_string(attributes, self.SOURCE_KEYS)
        target = self._first_string(attributes, self.TARGET_KEYS)
        if source is None or target is None:
            self._diagnostic(
                "runtime_trace_otel_span_unmapped",
                (
                    "OpenTelemetry span does not include explicit "
                    "arcgraph.source and arcgraph.target attributes."
                ),
            )
            return None
        event: dict[str, Any] = {
            "kind": self._string_or_default(
                attributes.get("arcgraph.event_kind")
                or attributes.get("arcgraph.kind"),
                "call",
            ),
            "source": source,
            "target": target,
            "detail": self._span_detail(span),
        }
        path = self._first_string(
            attributes,
            ("arcgraph.path", "code.filepath", "code.file.path", "code.path"),
        )
        if path is not None:
            event["path"] = path
        line = self._int_or_none(
            attributes.get("arcgraph.line")
            or attributes.get("code.lineno")
            or attributes.get("code.line")
        )
        if line is not None:
            event["line"] = line
        column = self._int_or_none(
            attributes.get("arcgraph.column") or attributes.get("code.column")
        )
        if column is not None:
            event["column"] = column
        test_id = self._first_string(attributes, ("arcgraph.test_id", "test.id"))
        if test_id is not None:
            event["test_id"] = test_id
        return event

    def _span_attributes(self, span: dict[str, Any]) -> dict[str, Any]:
        attributes = span.get("attributes")
        if isinstance(attributes, dict):
            return dict(attributes)
        if not isinstance(attributes, list):
            return {}
        result: dict[str, Any] = {}
        for item in attributes:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not isinstance(key, str) or not key:
                continue
            result[key] = self._attribute_value(item.get("value"))
        return result

    def _attribute_value(self, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in value:
                return value[key]
        if "arrayValue" in value and isinstance(value["arrayValue"], dict):
            return [
                self._attribute_value(item)
                for item in value["arrayValue"].get("values", [])
            ]
        if "kvlistValue" in value and isinstance(value["kvlistValue"], dict):
            return {
                item.get("key"): self._attribute_value(item.get("value"))
                for item in value["kvlistValue"].get("values", [])
                if isinstance(item, dict)
            }
        return None

    def _diagnostic(self, kind: str, message: str) -> None:
        self._diagnostics.append(
            {
                "kind": kind,
                "path": self._display_path(self._absolute_path(Path(self.span_path))),
                "message": message,
            }
        )

    def _wall_time_exceeded(self, started: float) -> bool:
        return (
            self.max_seconds >= 0 and (time.monotonic() - started) >= self.max_seconds
        )

    def _absolute_path(self, path: Path) -> Path:
        return path if path.is_absolute() else self.repo_root / path

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)

    @classmethod
    def _is_sensitive_attribute(cls, key: str) -> bool:
        lowered = key.lower()
        return any(token in lowered for token in cls.SENSITIVE_ATTRIBUTE_TOKENS)

    @staticmethod
    def _span_detail(span: dict[str, Any]) -> str | None:
        for key in ("spanId", "span_id", "name"):
            value = span.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _first_string(attributes: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = attributes.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _string_or_default(value: Any, default: str) -> str:
        return str(value) if value else default

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
