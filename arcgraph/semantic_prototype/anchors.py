"""Backend generations refer to one structural generation, without copying U."""

from collections import defaultdict
from pathlib import Path
import re
import uuid
from typing import Literal

from arcgraph.analyzers.precision_positions import PrecisionPositions
from .backends import Records
from .contract import (
    Answer,
    Callsite,
    Definition,
    MappingRow,
    Model,
    Producer,
    Snapshot,
    Span,
)
from .json_stream import content_digest
from .snapshot import canonical, verify
from .structure_provider import StructuralGeneration, admit_structure


class BackendRun(Model):
    schema_version: Literal["anchored-backend/0.1"] = "anchored-backend/0.1"
    id: str
    snapshot: Snapshot
    producer: Producer
    structure: str
    structure_checksum: str
    availability: Literal["available", "partial", "unavailable"]
    answers: tuple[Answer, ...]
    mapping: tuple[MappingRow, ...] = ()
    raw_count: int = 0
    parse_failures: tuple[tuple[str, str], ...] = ()
    complete: bool = False
    count: int = 0
    checksum: str = ""

    def content_digest(self):
        return content_digest(self)

    def checked(self):
        BackendRun.model_validate(
            {
                **self.model_dump(mode="json", exclude={"answers", "mapping"}),
                "answers": (),
                "mapping": (),
            }
        )
        for answer in self.answers:
            Answer.model_validate(answer.model_dump(mode="json"))
        for row in self.mapping:
            MappingRow.model_validate(row.model_dump(mode="json"))
        result = self
        if (
            not result.complete
            or result.count != len(result.answers)
            or result.raw_count != len(result.mapping)
            or result.checksum != result.content_digest()
        ):
            raise ValueError("incomplete or corrupt backend generation")
        identity = (result.id, result.producer.id, result.snapshot.id)
        if len({a.subject for a in result.answers}) != len(result.answers) or len(
            {a.envelope.record_id for a in result.answers}
        ) != len(result.answers):
            raise ValueError("duplicate backend answer")
        if any(
            (a.envelope.generation, a.envelope.producer, a.envelope.snapshot)
            != identity
            or a.envelope.mapping != "exact"
            for a in result.answers
        ):
            raise ValueError("backend envelope mismatch")
        if len({m.raw_id for m in result.mapping}) != len(result.mapping):
            raise ValueError("duplicate raw mapping identity")
        return result

    def seal(self):
        return self.model_copy(
            update={
                "complete": True,
                "count": len(self.answers),
                "checksum": self.content_digest(),
            }
        ).checked()


class View:
    """Derived indexes only. All identities and ranges come from structural facts."""

    def __init__(self, bundle: StructuralGeneration):
        self.bundle = bundle
        self.records = {r.id: r for r in bundle.records}
        self.definitions = []
        self.callsites = []
        self.annotations = {}
        for r in bundle.records:
            if r.kind == "definition":
                for p in r.payload["parameters"]:
                    if p["annotation"]:
                        self.annotations[(r.id, p["name"])] = p["annotation"]["source"]
            if r.kind == "binding" and r.payload.get("annotation"):
                for target in r.payload["targets"]:
                    self.annotations[(r.syntactic_owner, target["source"])] = r.payload[
                        "annotation"
                    ]["source"]
        for r in bundle.records:
            p = r.payload
            if r.kind == "definition" and p["symbol_kind"] in (
                "function",
                "class",
                "lambda",
                "generator",
            ):
                parent = self.records.get(r.syntactic_owner)
                decorators = tuple(d["source"] for d in p["decorators"])
                protocol = (
                    parent is not None
                    and parent.kind == "definition"
                    and any(
                        b["source"].split("[")[0].split(".")[-1] == "Protocol"
                        for b in parent.payload["bases"]
                    )
                )
                self.definitions.append(
                    Definition(
                        envelope=r.envelope,
                        id=r.id,
                        path=r.path,
                        span=r.span,
                        name_span=Records.span(p["name_span"]),
                        name=p["name"],
                        qualname=p["qualname"],
                        symbol_kind=p["symbol_kind"],
                        owner=r.syntactic_owner,
                        decorators=decorators,
                        bases=tuple(b["source"] for b in p["bases"]),
                        declaration_only=protocol
                        or any(
                            d.split(".")[-1] == "abstractmethod" for d in decorators
                        ),
                    )
                )
            if r.kind in ("callsite", "access"):
                expression = p["normalized_expression"]
                match = re.match(r"([\w]+)\.", p["callee"]["source"])
                annotation = (
                    self.annotation(r.syntactic_owner, match[1]) if match else None
                )
                self.callsites.append(
                    Callsite(
                        envelope=r.envelope,
                        id=r.id,
                        path=r.path,
                        span=r.span,
                        token_span=(
                            Records.span(p["token_span"]) if p["token_span"] else None
                        ),
                        owner=r.execution_owner or r.syntactic_owner,
                        syntactic_owner=r.syntactic_owner,
                        phase=r.phase,
                        expression=expression,
                        receiver_annotation=annotation,
                    )
                )
        self.defs = {d.id: d for d in self.definitions}
        self.sites = {s.id: s for s in self.callsites}
        self.data = {
            "parse_failures": [
                (f.path, str(f.diagnostic))
                for f in bundle.files
                if f.status != "parsed"
            ],
            "definitions": [
                {
                    "id": d.id,
                    "path": d.path,
                    "qualname": d.qualname,
                    "line": d.span.start[0] + 1,
                    "first_line": min(
                        [
                            d.span.start[0] + 1,
                            *[
                                x["span"][0] + 1
                                for x in self.records[d.id].payload["decorators"]
                            ],
                        ]
                    ),
                }
                for d in self.definitions
            ],
        }

    def annotation(self, owner, name):
        seen = set()
        while owner not in seen:
            seen.add(owner)
            if (owner, name) in self.annotations:
                return self.annotations[(owner, name)]
            record = self.records.get(owner)
            if record is None:
                return None
            owner = record.syntactic_owner
        return None

    def owner_matches(self, source, site):
        if source is None:
            return False
        r = self.records[site.id]
        if source.id == r.execution_owner:
            return True
        # LSP attributes defaults/decorators to their definition. Synthetic
        # annotation/comprehension scopes are absent from callHierarchy.
        if (
            r.phase
            in (
                "definition_default",
                "definition_base",
                "decorator",
                "annotation_lazy",
                "type_alias_lazy",
            )
            and source.id == r.syntactic_owner
        ):
            return True
        owner = self.records.get(r.execution_owner)
        while owner is not None and (
            owner.kind == "scope"
            or owner.kind == "definition"
            and owner.payload["symbol_kind"] in ("lambda", "generator")
        ):
            parent = (
                owner.payload["parent"]
                if owner.kind == "scope"
                else owner.syntactic_owner
            )
            if source.id == parent:
                return True
            owner = self.records.get(parent)
        return False


def admit(run: BackendRun, view: View, expected: Snapshot, producer: Producer):
    """Pure, shared admission before any policy decision or edge projection."""
    try:
        run.checked()
        allowed, reason = admit_structure(view.bundle, expected)
        if not allowed:
            return False, reason
        if run.snapshot.id != expected.id or run.producer.id != producer.id:
            return False, "backend_identity_mismatch"
        if expected.target_python not in run.producer.target_versions:
            return False, "target_python_unsupported"
        if (
            run.structure != view.bundle.id
            or run.structure_checksum != view.bundle.checksum
        ):
            return False, "structure_identity_mismatch"
        if {a.subject for a in run.answers} != set(view.sites):
            return False, "structure_subject_mismatch"
        if any(c.target not in view.defs for a in run.answers for c in a.candidates):
            return False, "unmapped_candidate"
        for m in run.mapping:
            if (
                m.subject is not None
                and m.subject not in view.sites
                or m.target is not None
                and m.target not in view.defs
            ):
                return False, "unmapped_mapping_identity"
        if run.availability == "unavailable":
            return False, "backend_unavailable"
        return True, (
            "experimental_partial"
            if run.availability == "partial"
            else "experimental_admitted"
        )
    except ValueError as exc:
        return False, str(exc)


class AnchoredRecords(Records):
    def __init__(self, snapshot: Snapshot, root: Path, backend: Producer, view: View):
        self.view = view
        self.snapshot, self.root, self.backend = snapshot, root.resolve(), backend
        self.generation_id = str(uuid.uuid4())
        self.snapshot_id, self.producer_id = snapshot.id, backend.id
        self.data = view.data
        self.definitions, self.callsites = view.definitions, view.callsites
        self.defs, self.sites = view.defs, view.sites
        self.candidates, self.reasons = defaultdict(list), defaultdict(set)
        self.mapping = []
        self.by_definition, self.by_token, self.by_start = (
            defaultdict(list),
            defaultdict(list),
            defaultdict(list),
        )
        self.texts = {
            f.path: canonical((root / f.path).read_bytes()) for f in snapshot.files
        }
        self.lines = {p: text.split("\n") for p, text in self.texts.items()}
        for d in self.definitions:
            self.by_definition[(d.path, d.name_span.start, d.name)].append(d)
        for c in self.callsites:
            if c.token_span:
                self.by_token[(c.path, c.token_span.start, c.token_span.end)].append(c)
            if self.view.records[c.id].kind == "callsite":
                self.by_start[(c.path, c.span.start)].append(c)

    def owner_matches(self, source, site):
        return self.view.owner_matches(source, site)

    def candidate(self, site, target, method, raw_id, confidence="uncalibrated"):
        super().candidate(site, target, method, raw_id, confidence)
        if (
            method == "name_guess"
            and site.receiver_annotation is None
            and self.view.records[site.id].payload["callee"]["syntax"] == "Attribute"
        ):
            self.candidates[site.id][-1] = self.candidates[site.id][-1].model_copy(
                update={"receiver_relation": "unverified"}
            )

    def finish(self, *, unit="utf-8", failure=None, availability="available"):
        verify(self.snapshot, self.root)
        answers = tuple(
            Answer(
                envelope=self.envelope(
                    "answer:" + s.id, unit, (self.backend.name, self.view.bundle.id)
                ),
                subject=s.id,
                outcome="candidates" if self.candidates[s.id] else "unknown",
                candidates=tuple(self.candidates[s.id]),
                reasons=tuple(
                    sorted(
                        self.reasons[s.id]
                        or (() if self.candidates[s.id] else (failure or "no_answer",))
                    )
                ),
                completeness="open" if self.candidates[s.id] else "unknown",
            )
            for s in self.callsites
        )
        return BackendRun(
            id=self.generation_id,
            snapshot=self.snapshot,
            producer=self.backend,
            structure=self.view.bundle.id,
            structure_checksum=self.view.bundle.checksum,
            availability=availability,
            answers=answers,
            mapping=tuple(self.mapping),
            raw_count=len(self.mapping),
            parse_failures=tuple(self.data["parse_failures"]),
        ).seal()

    def range(self, path, value, unit):
        def convert(pos):
            line, col = pos["line"], pos["character"]
            if (
                type(line) is not int
                or type(col) is not int
                or min(line, col) < 0
                or line >= len(self.lines[path])
            ):
                raise ValueError("unconvertible position")
            return line, PrecisionPositions._byte_column(
                self.lines[path][line],
                col,
                {"utf-8": 1, "utf-16": 2, "unicode_scalar": 3}[unit],
            )

        return Span(start=convert(value["start"]), end=convert(value["end"]))
