"""Validated, replayable records for the isolated semantic prototype."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, model_validator


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Span(Model):
    start: tuple[StrictInt, StrictInt]
    end: tuple[StrictInt, StrictInt]

    @model_validator(mode="after")
    def ordered(self):
        if min(*self.start, *self.end) < 0 or self.start >= self.end:
            raise ValueError("nonempty zero-based half-open span required")
        return self


class Source(Model):
    path: str
    raw_digest: str
    text_digest: str

    @model_validator(mode="after")
    def relative(self):
        p = PurePosixPath(self.path)
        if (
            p.is_absolute()
            or ".." in p.parts
            or p.as_posix() != self.path
            or "\\" in self.path
        ):
            raise ValueError("canonical relative path required")
        return self


class Snapshot(Model):
    files: tuple[Source, ...]
    target_python: str
    platform: str
    environment: tuple[tuple[str, str], ...] = (("dependencies", "unknown"),)
    text_profile: Literal["pep263-lf/1"] = "pep263-lf/1"

    @property
    def id(self) -> str:
        return digest(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def unique(self):
        if (
            len({f.path for f in self.files}) != len(self.files)
            or tuple(sorted(self.files, key=lambda f: f.path)) != self.files
        ):
            raise ValueError("sorted unique file manifest required")
        return self


class Producer(Model):
    name: Literal["pyright", "arcgraph"]
    version: str
    configuration: tuple[tuple[str, str], ...]
    target_versions: tuple[str, ...]
    capabilities: tuple[str, ...] = (
        "definitions:structure",
        "callsites:structure",
        "call_targets:open",
    )

    @property
    def id(self) -> str:
        return digest(self.model_dump(mode="json"))


class Envelope(Model):
    generation: str
    producer: str
    snapshot: str
    record_id: str
    mapping: Literal["exact", "failed"] = "exact"
    native_unit: Literal["utf-8", "utf-16", "unicode_scalar"] = "utf-8"
    provenance: tuple[str, ...] = ("structure",)


class Definition(Model):
    envelope: Envelope
    kind: Literal["definition"] = "definition"
    id: str
    path: str
    span: Span
    name_span: Span
    name: str
    qualname: str
    symbol_kind: Literal["function", "class", "lambda", "generator"]
    owner: str
    decorators: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    declaration_only: bool = False


class Callsite(Model):
    envelope: Envelope
    kind: Literal["callsite"] = "callsite"
    id: str
    path: str
    span: Span
    token_span: Span | None
    owner: str
    syntactic_owner: str
    phase: str
    expression: str
    receiver_annotation: str | None = None


class Candidate(Model):
    target: str
    dispatch: Literal["invoke", "construct"]
    semantics: Literal[
        "implementation", "logical_body", "declaration", "descriptor_value"
    ]
    method: str
    evidence: tuple[str, ...] = ()
    receiver_relation: Literal[
        "unverified", "backend_inferred", "nominal_match", "not_applicable"
    ] = "not_applicable"
    confidence: str = "uncalibrated"


class Answer(Model):
    envelope: Envelope
    kind: Literal["call_resolution"] = "call_resolution"
    subject: str
    outcome: Literal["candidates", "negative", "unknown"]
    candidates: tuple[Candidate, ...] = ()
    reasons: tuple[str, ...] = ()
    completeness: Literal["open", "unknown"] = "unknown"
    proposition: Literal["possible_call_targets"] = "possible_call_targets"

    @model_validator(mode="after")
    def algebra(self):
        if self.outcome == "candidates":
            if not self.candidates or self.completeness != "open":
                raise ValueError("nonempty open candidate set required")
        elif self.candidates or not self.reasons or self.completeness != "unknown":
            raise ValueError("unknown/negative requires reasons and no candidates")
        if self.outcome == "negative" and self.reasons != ("capability_unsupported",):
            raise ValueError("no closed-negative capability in this prototype")
        return self


class MappingRow(Model):
    raw_id: str
    first_reason: str
    stage: Literal["coordinate", "owner", "symbol", "mapped"]
    subject: str | None = None
    target: str | None = None


class Generation(Model):
    schema_version: Literal["semantic-prototype/0.1"] = "semantic-prototype/0.1"
    id: str
    snapshot: Snapshot
    producer: Producer
    availability: Literal["available", "partial", "unavailable"]
    definitions: tuple[Definition, ...]
    callsites: tuple[Callsite, ...]
    answers: tuple[Answer, ...]
    mapping: tuple[MappingRow, ...] = ()
    raw_count: int = 0
    parse_failures: tuple[tuple[str, str], ...] = ()
    complete: bool = False
    count: int = 0
    checksum: str = ""

    @property
    def records(self):
        return (*self.definitions, *self.callsites, *self.answers)

    def content_digest(self):
        return digest(
            self.model_dump(mode="json", exclude={"complete", "count", "checksum"})
        )

    def checked(self):
        # Revalidation also catches model_copy/update and mutable nested payloads.
        result = Generation.model_validate(self.model_dump(mode="json"))
        if (
            not result.complete
            or result.count != len(result.records)
            or result.checksum != result.content_digest()
            or result.raw_count != len(result.mapping)
        ):
            raise ValueError("incomplete or corrupt generation")
        defs = {d.id for d in result.definitions}
        sites = {s.id for s in result.callsites}
        paths = {f.path for f in result.snapshot.files}
        expected_envelope = (result.id, result.producer.id, result.snapshot.id)
        ids = [r.envelope.record_id for r in result.records]
        if (
            len(set(ids)) != len(ids)
            or len(defs) != len(result.definitions)
            or len(sites) != len(result.callsites)
        ):
            raise ValueError("duplicate record identity")
        if {a.subject for a in result.answers} != sites or len(result.answers) != len(
            sites
        ):
            raise ValueError("each callsite requires exactly one answer")
        for r in result.records:
            e = r.envelope
            if (e.generation, e.producer, e.snapshot) != expected_envelope:
                raise ValueError("envelope identity mismatch")
            if isinstance(r, Answer):
                if e.mapping == "failed" and r.candidates:
                    raise ValueError("unmapped coordinate cannot support candidates")
                if any(c.target not in defs for c in r.candidates):
                    raise ValueError("unmapped target reference")
            else:
                if r.path not in paths:
                    raise ValueError("record outside source manifest")
                if r.owner not in defs and r.owner != "module:" + r.path:
                    raise ValueError("unmapped owner reference")
        if len({m.raw_id for m in result.mapping}) != len(result.mapping):
            raise ValueError("duplicate raw identity")
        return result

    def seal(self):
        return self.model_copy(
            update={
                "complete": True,
                "count": len(self.records),
                "checksum": self.content_digest(),
            }
        ).checked()


def admit(
    generation: Generation, expected: Snapshot, producer: Producer
) -> tuple[bool, str]:
    """Pure experimental admission; unknown environment never implies verified."""
    try:
        generation.checked()
    except ValueError as exc:
        return False, str(exc)
    if generation.snapshot.id != expected.id or generation.producer.id != producer.id:
        return False, "identity_mismatch"
    if expected.target_python not in producer.target_versions:
        return False, "target_python_unsupported"
    if generation.availability == "unavailable":
        return False, "backend_unavailable"
    return True, (
        "experimental_environment_unknown"
        if any(v == "unknown" for _, v in expected.environment)
        else "experimental_admitted"
    )


def write_generation(generation: Generation, path: Path) -> None:
    """Publish one complete artifact atomically; interrupted temporary is harmless."""
    import tempfile

    generation.checked()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".generation-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            from .json_stream import chunks

            f.writelines(chunks(generation))
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_generation(path: Path) -> Generation:
    return Generation.model_validate_json(path.read_bytes()).checked()
