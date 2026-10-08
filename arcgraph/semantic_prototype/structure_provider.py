"""Independent target-CPython structural generations and explicit availability.

No interpreter discovery, installation, project imports, or product graph writes.
Run with ``python -m arcgraph.semantic_prototype.structure_provider --help``.
"""

from __future__ import annotations

import argparse
import tempfile
import json
from pathlib import Path
import subprocess
import sys
from typing import Literal
import uuid

from . import structure_facts
from .contract import Envelope, Model, Snapshot, Span, digest, write_generation
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
    status: Literal["parsed", "parse_error"]
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


def analyze(
    snapshot: Snapshot,
    root: Path,
    interpreters: dict[str, str],
    *,
    timeout: float = 60,
    environment: dict[str, str] | None = None,
    access_facts: bool = False,
) -> StructuralGeneration:
    """Choose only an explicitly registered interpreter of the requested minor."""
    verify(snapshot, root)
    target = snapshot.target_python
    worker = Path(structure_facts.__file__).resolve()
    producer = StructuralProducer(
        worker_digest=sha(worker.read_bytes()),
        wrapper_digest=sha(Path(__file__).read_bytes()),
        interpreter=(("requested", target),),
        capabilities=CAPABILITIES
        + (("accesses:attribute-load",) if access_facts else ()),
    )
    generation_id = str(uuid.uuid4())
    initial = StructuralGeneration(
        id=generation_id,
        snapshot=snapshot,
        producer=producer,
        availability="unavailable",
    )
    if target not in SUPPORTED:
        return initial.model_copy(update={"reasons": ("target_python_unsupported",)})
    interpreter = interpreters.get(target)
    if not interpreter:
        return initial.model_copy(update={"reasons": ("target_interpreter_missing",)})
    paths = sorted(f.path for f in snapshot.files if f.path.endswith(".py"))
    request = {
        "root": str(root.resolve()),
        "paths": paths,
        "target": target,
        "access_facts": access_facts,
        "format": "jsonl/1",
    }
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as wire:
        try:
            process = subprocess.run(
                [interpreter, "-I", "-S", "-B", "-X", "utf8", str(worker)],
                input=json.dumps(request),
                text=True,
                encoding="utf-8",
                stdout=wire,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=True,
                env=environment,
            )
            legacy = getattr(process, "stdout", None)
            if legacy is not None:
                wire.write(legacy)
            wire.seek(0)
            first = wire.readline()
            if first.startswith('{"wire_format":"jsonl/1",'):
                data = json.loads(first)
                data["records"] = (json.loads(line) for line in wire)
            else:
                # Existing recorded worker replies remain valid for profile 0.1.
                wire.seek(0)
                data = json.load(wire)
        except subprocess.TimeoutExpired:
            return initial.model_copy(update={"reasons": ("timeout",)})
        except (OSError, subprocess.CalledProcessError, ValueError):
            return initial.model_copy(
                update={"reasons": ("tool_error_or_invalid_output",)}
            )
        verify(snapshot, root)
        if not isinstance(data, dict) or not isinstance(data.get("interpreter"), dict):
            return initial.model_copy(
                update={"reasons": ("tool_error_or_invalid_output",)}
            )
        version_text = data["interpreter"].get("version", "")
        actual_version = version_text.split() if isinstance(version_text, str) else []
        actual_minor = (
            ".".join(actual_version[0].split(".")[:2]) if actual_version else ""
        )
        if (
            data.get("profile") != structure_facts.PROFILE
            or data.get("interpreter", {}).get("target") != target
            or data["interpreter"].get("implementation") != "cpython"
            or actual_minor != target
        ):
            return initial.model_copy(
                update={"reasons": ("interpreter_identity_mismatch",)}
            )
        producer = producer.model_copy(
            update={"interpreter": tuple(sorted(data["interpreter"].items()))}
        )
        producer_id, snapshot_id = producer.id, snapshot.id
        records = []
        try:
            files = tuple(StructuralFile.model_validate(f) for f in data["files"])
            for row in data["records"]:
                row = dict(row)
                raw_span = row.pop("span")
                row["span"] = Span(start=tuple(raw_span[:2]), end=tuple(raw_span[2:]))
                row["envelope"] = Envelope(
                    generation=generation_id,
                    producer=producer_id,
                    snapshot=snapshot_id,
                    record_id=digest((generation_id, row["id"])),
                    provenance=(structure_facts.PROFILE, "target_cpython_parse_only"),
                )
                records.append(StructuralRecord.model_validate(row))
            if data.get("record_count", len(records)) != len(records):
                raise ValueError("truncated structural wire output")
            wire.close()
            bundle = StructuralGeneration(
                id=generation_id,
                snapshot=snapshot,
                producer=producer,
                availability=(
                    "partial"
                    if any(f.status != "parsed" for f in files)
                    else "available"
                ),
                files=files,
                records=tuple(records),
            )
            # The wire representation and its parsed dictionaries are no longer
            # needed once every record has been validated into the owned corpus.
            del data, process
            bundle = bundle.seal()
            lines = {
                f.path: canonical((root / f.path).read_bytes()).split("\n")
                for f in files
                if f.status == "parsed"
            }
            for record in bundle.records:
                for s in [record.span, *spans(record.payload)]:
                    for line, col in (s.start, s.end):
                        if line >= len(lines[record.path]):
                            raise ValueError("structural line outside source")
                        PrecisionPositions._byte_column(
                            lines[record.path][line], col, 1
                        )
            return bundle
        except (KeyError, TypeError, ValueError):
            return initial.model_copy(
                update={"reasons": ("invalid_structural_records",)}
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--interpreter", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    snapshot = Snapshot.model_validate_json(args.snapshot.read_bytes())
    bundle = analyze(snapshot, args.root, {snapshot.target_python: args.interpreter})
    if not bundle.complete:
        print(
            json.dumps(
                {"availability": bundle.availability, "reasons": bundle.reasons}
            ),
            file=sys.stderr,
        )
        return 2
    write_generation(bundle, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
