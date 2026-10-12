"""Validated semantic records derived from the product StructureView."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from . import structure_facts
from .contract import Envelope, Model, Snapshot, Span, digest
from arcgraph.analyzers.precision_positions import PrecisionPositions
from .snapshot import canonical, sha, verify
from .json_stream import content_digest

SUPPORTED = ("3.11", "3.12", "3.13", "3.14")
CAPABILITIES = (
    "definitions",
    "callsites:explicit",
    "owners:syntactic/lexical/execution",
    "decorators",
    "bases",
    "parameters",
    "type_parameters",
    "imports",
    "bindings:syntax",
    "exports:literal-or-dynamic",
    "annotations:phase",
    "control:syntax",
    "declaration_syntax:stub/typing_guard",
)


class StructuralProducer(Model):
    name: Literal["cpython-structure"] = "cpython-structure"
    profile: Literal["structure-facts/0.1"] = "structure-facts/0.1"
    worker_digest: str
    wrapper_digest: str
    interpreter: tuple[tuple[str, str], ...]
    capabilities: tuple[str, ...] = CAPABILITIES

    @property
    def id(self):
        return digest(self.model_dump(mode="json"))


class StructuralFile(Model):
    path: str
    raw_digest: str
    status: Literal["parsed", "parse_error", "host_fallback"]
    diagnostic: dict | None = None


class StructuralRecord(Model):
    envelope: Envelope
    id: str
    kind: Literal[
        "definition",
        "callsite",
        "scope",
        "annotation",
        "import",
        "binding",
        "export",
        "control",
        "access",
    ]
    path: str
    span: Span
    syntactic_owner: str
    lexical_scope: str
    execution_owner: str | None
    phase: str
    payload: dict


def spans(value):
    """Visit all nested span fields in the structural payload."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "span" or key.endswith("_span"):
                if item is not None:
                    if not isinstance(item, list) or len(item) != 4:
                        raise ValueError("four-coordinate structural span required")
                    yield Span(start=tuple(item[:2]), end=tuple(item[2:]))
            else:
                yield from spans(item)
    elif isinstance(value, list):
        for item in value:
            yield from spans(item)


class StructuralGeneration(Model):
    schema_version: Literal["structural-generation/0.1"] = "structural-generation/0.1"
    id: str
    snapshot: Snapshot
    producer: StructuralProducer
    availability: Literal["available", "partial", "unavailable"]
    reasons: tuple[str, ...] = ()
    files: tuple[StructuralFile, ...] = ()
    records: tuple[StructuralRecord, ...] = ()
    complete: bool = False
    count: int = 0
    checksum: str = ""

    def content_digest(self):
        return content_digest(self)

    def checked(self):
        # Validate one record at a time: nested mutable payloads and model_copy
        # remain checked, without copying the complete structural corpus.
        StructuralGeneration.model_validate(
            {
                **self.model_dump(mode="json", exclude={"records", "files"}),
                "records": (),
                "files": (),
            }
        )
        for file in self.files:
            StructuralFile.model_validate(file.model_dump(mode="json"))
        for record in self.records:
            StructuralRecord.model_validate(record.model_dump(mode="json"))
        result = self
        if (
            not result.complete
            or result.availability == "unavailable"
            or result.count != len(result.records)
            or result.checksum != result.content_digest()
        ):
            raise ValueError("incomplete or corrupt structural generation")
        manifest = {
            f.path: f.raw_digest
            for f in result.snapshot.files
            if f.path.endswith(".py")
        }
        if {f.path: f.raw_digest for f in result.files} != manifest or len(
            result.files
        ) != len(manifest):
            raise ValueError("structural file coverage mismatch")
        parsed = {f.path for f in result.files if f.status == "parsed"}
        if (result.availability == "available") != (len(parsed) == len(manifest)):
            raise ValueError("structural availability mismatch")
        for f in result.files:
            if (f.status == "parsed") != (f.diagnostic is None):
                raise ValueError("structural diagnostic mismatch")
        ids = {r.id for r in result.records}
        if len(ids) != len(result.records) or len(
            {r.envelope.record_id for r in result.records}
        ) != len(ids):
            raise ValueError("duplicate structural identity")
        owners = {
            r.id: r.path for r in result.records if r.kind in ("definition", "scope")
        }
        expected_envelope = (result.id, result.producer.id, result.snapshot.id)
        for r in result.records:
            e = r.envelope
            if (
                (e.generation, e.producer, e.snapshot) != expected_envelope
                or e.mapping != "exact"
                or e.native_unit != "utf-8"
            ):
                raise ValueError("structural envelope mismatch")
            if r.path not in parsed:
                raise ValueError("facts from unparsed or unfrozen file")
            for owner in (r.syntactic_owner, r.lexical_scope, r.execution_owner):
                if (
                    owner is not None
                    and owners.get(owner) != r.path
                    and owner != "module:" + r.path
                ):
                    raise ValueError("unmapped structural owner")
            required = {
                "definition": (
                    "name",
                    "qualname",
                    "symbol_kind",
                    "name_span",
                    "parameters",
                    "decorators",
                    "bases",
                    "type_parameters",
                ),
                "callsite": (
                    "callee_span",
                    "token_span",
                    "callee",
                    "expression",
                    "execution",
                ),
                "access": (
                    "callee_span",
                    "token_span",
                    "callee",
                    "expression",
                    "execution",
                ),
                "scope": ("parent", "scope_kind"),
                "annotation": ("role", "expression"),
                "import": ("module", "level", "aliases", "star"),
                "binding": ("role",),
                "export": ("status", "names", "operation"),
                "control": ("syntax",),
            }[r.kind]
            if any(key not in r.payload for key in required):
                raise ValueError("missing structural payload field")
            payload = r.payload
            if r.kind == "definition":
                # ast's definition span excludes decorators; every other field
                # must be contained in the definition, including its name.
                payload = {
                    key: value for key, value in payload.items() if key != "decorators"
                }
                if any(s.end > r.span.start for s in spans(r.payload["decorators"])):
                    raise ValueError("decorator outside definition prefix")
            spans_ = list(spans(payload))
            if any(
                not (r.span.start <= s.start and s.end <= r.span.end) for s in spans_
            ):
                raise ValueError("structural payload span outside record")
            if r.kind == "scope":
                parent = r.payload["parent"]
                if owners.get(parent) != r.path and parent != "module:" + r.path:
                    raise ValueError("unmapped structural scope parent")
            if r.kind in ("callsite", "access") and r.payload["token_span"] is not None:
                callee = r.payload["callee_span"]
                token = r.payload["token_span"]
                if not (
                    tuple(callee[:2]) <= tuple(token[:2])
                    and tuple(token[2:]) <= tuple(callee[2:])
                ):
                    raise ValueError("call token outside callee")
        return result

    def seal(self):
        return self.model_copy(
            update={
                "complete": True,
                "count": len(self.records),
                "checksum": self.content_digest(),
            }
        ).checked()


def admit_structure(
    bundle: StructuralGeneration, expected: Snapshot
) -> tuple[bool, str]:
    """Pure admission for this structural profile; partial files remain explicit."""
    try:
        bundle.checked()
    except ValueError as exc:
        return False, str(exc)
    identity = dict(bundle.producer.interpreter)
    if identity.get("parser_status") == "host_fallback" or (
        bundle.files and all(f.status == "host_fallback" for f in bundle.files)
    ):
        return False, "host_fallback_not_semantic_evidence"
    if bundle.snapshot.id != expected.id:
        return False, "snapshot_identity_mismatch"
    if expected.target_python not in SUPPORTED:
        return False, "target_python_unsupported"
    if (
        identity.get("target") != expected.target_python
        or identity.get("implementation") != "cpython"
    ):
        return False, "interpreter_identity_mismatch"
    return True, (
        "experimental_partial"
        if bundle.availability == "partial"
        else "experimental_admitted"
    )


def project_read(parser, paths):
    """Observe host reads and keep D2's project attempts across semantic blocks."""
    hosted = set()
    original = getattr(parser, "_read_host", None)
    if original is None:
        trees, notices = parser.read(paths)
        if parser.status()["status"] == "host_fallback":
            hosted.update(paths)
        return trees, notices, hosted
    had_override = "_read_host" in parser.__dict__
    previous = parser.__dict__.get("_read_host")

    def read_host(host_paths):
        hosted.update(host_paths)
        return original(host_paths)

    parser._read_host = read_host
    try:
        trees, notices = parser.read(paths, continue_project=True)
    finally:
        if had_override:
            parser._read_host = previous
        else:
            del parser._read_host
    return trees, notices, hosted


def analyze(
    snapshot, root, *, structure_view=None, environment=None, access_facts=True
):
    """Convert D2's ASTs, releasing each block; never start a second parser."""
    import ast
    from arcgraph.core.structure import StructureView
    from .frozen import interpreter_identity

    verify(snapshot, root)
    parser = structure_view or StructureView(root, environment)
    status = parser.status()
    interpreter = interpreter_identity(root, executable=status["interpreter"])
    expected_interpreter = {
        key: value for key, value in snapshot.environment if key in interpreter
    }
    if (
        status["status"] != "host_fallback"
        and expected_interpreter
        and expected_interpreter != interpreter
    ):
        raise ValueError("interpreter_identity_mismatch")
    target = ".".join(status["version"].split(".")[:2])
    if target != snapshot.target_python:
        raise ValueError("interpreter_identity_mismatch")
    producer = StructuralProducer(
        worker_digest=sha(Path(structure_facts.__file__).read_bytes()),
        wrapper_digest=sha(Path(__file__).read_bytes()),
        interpreter=tuple(
            sorted(
                {
                    "implementation": "cpython",
                    "version": status["version"],
                    "target": target,
                    "parser_status": status["status"],
                    **interpreter,
                }.items()
            )
        ),
        capabilities=CAPABILITIES
        + (("accesses:attribute-load",) if access_facts else ()),
    )
    generation_id = digest((snapshot.id, producer.id))
    producer_id, snapshot_id = producer.id, snapshot.id
    records, files = [], []
    modules = dict(snapshot.module_names)
    manifest = [f for f in snapshot.files if f.path.endswith(".py")]
    hosted_files = set()
    for offset in range(0, len(manifest), 64):
        block = manifest[offset : offset + 64]
        trees, notices, hosted = project_read(
            parser, {f.path: root / f.path for f in block}
        )
        hosted_files.update(hosted)
        errors = {w.path: w.message for w in notices}
        for file in block:
            tree = trees.get(file.path) if file.path not in hosted else None
            files.append(
                StructuralFile(
                    path=file.path,
                    raw_digest=file.raw_digest,
                    status=(
                        "host_fallback"
                        if file.path in hosted
                        else "parsed" if tree is not None else "parse_error"
                    ),
                    diagnostic=(
                        None
                        if tree is not None
                        else {
                            "reason": (
                                "host_fallback"
                                if file.path in hosted
                                else "parse_error"
                            ),
                            "message": (
                                parser.status().get("reason")
                                if file.path in hosted
                                else errors.get(file.path)
                            ),
                        }
                    ),
                )
            )
            if tree is None:
                continue
            text = canonical((root / file.path).read_bytes())
            visitor = structure_facts.Facts(
                file.path,
                text,
                tuple(map(int, target.split("."))),
                access_facts,
                module_name=modules.get(file.path),
            )
            visitor.future = any(
                isinstance(n, ast.ImportFrom)
                and n.module == "__future__"
                and any(a.name == "annotations" for a in n.names)
                for n in tree.body
            )
            visitor.visit(tree)
            for raw in visitor.records:
                span = raw.pop("span")
                records.append(
                    StructuralRecord(
                        **raw,
                        span=Span(start=tuple(span[:2]), end=tuple(span[2:])),
                        envelope=Envelope(
                            generation=generation_id,
                            producer=producer_id,
                            snapshot=snapshot_id,
                            record_id=digest((generation_id, raw["id"])),
                            provenance=(
                                structure_facts.PROFILE,
                                "shared_structure_view",
                            ),
                        ),
                    )
                )
            del visitor, tree
        del trees
    verify(snapshot, root)
    if interpreter_identity(root, executable=status["interpreter"]) != interpreter:
        raise ValueError("input_changed_during_run: selected interpreter changed")
    if hosted_files:
        effective = (
            "host_fallback"
            if len(hosted_files) == len(manifest)
            else "partial_fallback"
        )
        producer = producer.model_copy(
            update={
                "interpreter": tuple(
                    sorted(
                        {
                            **dict(producer.interpreter),
                            "parser_status": effective,
                            "d2_parser_status": parser.status()["status"],
                        }.items()
                    )
                )
            }
        )
        generation_id = digest((snapshot.id, producer.id))
        for index, record in enumerate(records):
            records[index] = record.model_copy(
                update={
                    "envelope": record.envelope.model_copy(
                        update={
                            "generation": generation_id,
                            "producer": producer.id,
                            "record_id": digest((generation_id, record.id)),
                        }
                    )
                }
            )
    bundle = StructuralGeneration(
        id=generation_id,
        snapshot=snapshot,
        producer=producer,
        reasons=("host_fallback_files_uncovered",) if hosted_files else (),
        availability=(
            "partial" if any(f.status != "parsed" for f in files) else "available"
        ),
        files=tuple(files),
        records=tuple(records),
    ).seal()
    lines = {
        f.path: canonical((root / f.path).read_bytes()).split("\n")
        for f in files
        if f.status == "parsed"
    }
    for record in bundle.records:
        for span in (record.span, *spans(record.payload)):
            for line, col in (span.start, span.end):
                if line >= len(lines[record.path]):
                    raise ValueError("structural line outside source")
                PrecisionPositions._byte_column(lines[record.path][line], col, 1)
    return bundle
