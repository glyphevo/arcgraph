"""Two record producers, independent of publishing or graph storage."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import unquote, urlparse
import uuid

from . import structure
from .contract import (
    Answer,
    Callsite,
    Candidate,
    Definition,
    Envelope,
    Generation,
    MappingRow,
    Producer,
    Snapshot,
    Span,
    digest,
)
from .lsp import LspError, Peer
from .snapshot import canonical, native_position, native_span, verify


def oracle(snapshot: Snapshot, root: Path, interpreter: str | None = None) -> dict:
    verify(snapshot, root)
    paths = [f.path for f in snapshot.files if f.path.endswith(".py")]
    if interpreter:
        result = subprocess.run(
            [interpreter, "-I", "-X", "utf8", str(Path(structure.__file__).resolve())],
            input=json.dumps({"root": str(root), "paths": paths}),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
            check=True,
        )
        return json.loads(result.stdout)
    return structure.scan(root, paths)


def producer(
    name: str, version: str, target_versions: tuple[str, ...], configuration: dict
) -> Producer:
    return Producer(
        name=name,
        version=version,
        target_versions=target_versions,
        configuration=tuple(
            sorted((k, json.dumps(v, sort_keys=True)) for k, v in configuration.items())
        ),
    )


class Records:
    def __init__(self, snapshot: Snapshot, root: Path, backend: Producer, data: dict):
        self.snapshot = snapshot
        self.root = root.resolve()
        self.backend = backend
        self.generation_id = str(uuid.uuid4())
        self.snapshot_id = snapshot.id
        self.producer_id = backend.id
        self.data = data
        self.definitions = []
        self.callsites = []
        self.candidates = defaultdict(list)
        self.reasons = defaultdict(set)
        self.mapping = []
        self.by_definition = defaultdict(list)
        self.by_token = defaultdict(list)
        self.by_start = defaultdict(list)
        self.texts = {
            f.path: canonical((root / f.path).read_bytes()) for f in snapshot.files
        }
        for d in data["definitions"]:
            definition = Definition(
                envelope=self.envelope("definition:" + d["id"]),
                id=d["id"],
                path=d["path"],
                span=self.span(d["span"]),
                name_span=self.span(d["name_span"]),
                name=d["name"],
                qualname=d["qualname"],
                symbol_kind=d["kind"],
                owner=d["owner"],
                decorators=tuple(d["decorators"]),
                bases=tuple(d["bases"]),
                declaration_only=d["declaration_only"],
            )
            self.definitions.append(definition)
            self.by_definition[
                (d["path"], definition.name_span.start, d["name"])
            ].append(definition)
        self.defs = {d.id: d for d in self.definitions}
        for c in data["callsites"]:
            site = Callsite(
                envelope=self.envelope("callsite:" + c["id"]),
                id=c["id"],
                path=c["path"],
                span=self.span(c["span"]),
                token_span=self.span(c["token_span"]) if c["token_span"] else None,
                owner=c["owner"],
                syntactic_owner=c["syntactic_owner"],
                phase=c["phase"],
                expression=c["expression"],
                receiver_annotation=c.get("receiver_annotation"),
            )
            self.callsites.append(site)
            if site.token_span:
                self.by_token[
                    (site.path, site.token_span.start, site.token_span.end)
                ].append(site)
            self.by_start[(site.path, site.span.start)].append(site)
        self.sites = {c.id: c for c in self.callsites}

    def envelope(
        self,
        key: str,
        unit: str = "utf-8",
        provenance: tuple[str, ...] = ("structure",),
    ) -> Envelope:
        return Envelope(
            generation=self.generation_id,
            producer=self.producer_id,
            snapshot=self.snapshot_id,
            record_id=digest((self.generation_id, key)),
            native_unit=unit,
            provenance=provenance,
        )

    @staticmethod
    def span(value) -> Span:
        return Span(start=tuple(value[:2]), end=tuple(value[2:]))

    def path(self, uri: str) -> str | None:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            return None
        path = Path(unquote(parsed.path)).resolve()
        return (
            path.relative_to(self.root).as_posix()
            if path.is_relative_to(self.root)
            else None
        )

    def lookup(self, item: dict, unit: str) -> Definition | None:
        path = self.path(item["uri"])
        if path not in self.texts:
            return None
        span = self.range(path, item["selectionRange"], unit)
        choices = self.by_definition[(path, span.start, item["name"])]
        return choices[0] if len(choices) == 1 else None

    def owner_matches(self, source, site):
        return source is not None and source.id == site.owner

    def range(self, path, value, unit):
        return native_span(self.texts[path], value, unit)

    def candidate(
        self,
        site: Callsite,
        target: Definition,
        method: str,
        raw_id: str,
        confidence: str = "uncalibrated",
    ) -> None:
        semantics = (
            "declaration"
            if target.declaration_only
            else (
                "descriptor_value"
                if any(
                    x == "property" or x.endswith((".setter", ".getter"))
                    for x in target.decorators
                )
                else "logical_body" if target.decorators else "implementation"
            )
        )
        annotation = site.receiver_annotation
        evidence = [raw_id, *("decorator:" + x for x in target.decorators)]
        if semantics == "logical_body":
            evidence.append("wrapper_execution_unknown")
        relation = "not_applicable"
        if annotation:
            evidence.append("receiver_annotation:" + annotation)
            relation = "backend_inferred" if method != "name_guess" else "unverified"
            owner = self.defs.get(target.owner)
            if (
                method == "name_guess"
                and owner is not None
                and (
                    annotation == owner.qualname
                    or (
                        annotation == owner.name
                        and owner.path == site.path
                        and sum(
                            d.name == owner.name
                            and d.path == site.path
                            and d.symbol_kind == "class"
                            for d in self.definitions
                        )
                        == 1
                    )
                )
            ):
                relation = "nominal_match"
        value = Candidate(
            target=target.id,
            dispatch="construct" if target.symbol_kind == "class" else "invoke",
            semantics=semantics,
            method=method,
            evidence=tuple(evidence),
            receiver_relation=relation,
            confidence=confidence,
        )
        self.candidates[site.id].append(value)

    def mapped(self, raw_id, reason, stage, site=None, target=None):
        self.mapping.append(
            MappingRow(
                raw_id=raw_id,
                first_reason=reason,
                stage=stage,
                subject=site.id if site else None,
                target=target.id if target else None,
            )
        )
        if site and reason != "mapped_success":
            self.reasons[site.id].add(reason)

    def finish(
        self, *, unit="utf-8", failure=None, availability="available"
    ) -> Generation:
        verify(self.snapshot, self.root)
        answers = []
        for site in self.callsites:
            values = self.candidates[site.id]
            answers.append(
                Answer(
                    envelope=self.envelope(
                        "answer:" + site.id, unit, (self.backend.name,)
                    ),
                    subject=site.id,
                    outcome="candidates" if values else "unknown",
                    candidates=tuple(values),
                    reasons=(
                        tuple(sorted(self.reasons[site.id]))
                        if values
                        else tuple(
                            sorted(self.reasons[site.id] or {failure or "no_answer"})
                        )
                    ),
                    completeness="open" if values else "unknown",
                )
            )
        return Generation(
            id=self.generation_id,
            snapshot=self.snapshot,
            producer=self.backend,
            availability=availability,
            definitions=tuple(self.definitions),
            callsites=tuple(self.callsites),
            answers=tuple(answers),
            mapping=tuple(self.mapping),
            raw_count=len(self.mapping),
            parse_failures=tuple(tuple(x) for x in self.data["parse_failures"]),
        ).seal()


def pyright_records(
    snapshot: Snapshot,
    root: Path,
    backend: Producer,
    data: dict,
    peer: Peer,
    *,
    timeout: float = 30,
    initialize_result: dict | None = None,
    record_factory=Records,
) -> tuple[Generation, dict]:
    started = time.perf_counter()
    verify(snapshot, root)
    records = record_factory(snapshot, root, backend, data)
    requests = {
        "initialize": 0,
        "prepare": 0,
        "outgoing": 0,
        "error": 0,
        "timeout": 0,
        "empty": 0,
    }
    try:
        requests["initialize"] += int(initialize_result is None)
        init = (
            initialize_result
            if initialize_result is not None
            else peer.request(
                "initialize",
                {
                    "processId": None,
                    "rootUri": root.as_uri(),
                    "workspaceFolders": [{"uri": root.as_uri(), "name": "frozen"}],
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-8", "utf-16"]},
                        "textDocument": {
                            "callHierarchy": {"dynamicRegistration": False}
                        },
                    },
                },
                timeout,
            )
        )
        caps = (init or {}).get("capabilities", {})
        # LSP 3.17: omission means UTF-16, regardless of client offer order.
        unit = caps.get("positionEncoding", "utf-16")
        if unit not in ("utf-8", "utf-16") or not caps.get("callHierarchyProvider"):
            return records.finish(
                failure="capability_unsupported", availability="unavailable"
            ), {"requests": requests, "initialize": init, "encoding": unit}
        if initialize_result is None:
            peer.notify("initialized", {})
        for path, text in records.texts.items() if initialize_result is None else ():
            if path.endswith(".py"):
                peer.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": (root / path).as_uri(),
                            "languageId": "python",
                            "version": 1,
                            "text": text,
                        }
                    },
                )
    except (TimeoutError, LspError, OSError) as exc:
        reason = "timeout" if isinstance(exc, TimeoutError) else "tool_error"
        requests["timeout" if reason == "timeout" else "error"] += 1
        return records.finish(failure=reason, availability="unavailable"), {
            "requests": requests,
            "encoding": None,
        }
    setup_seconds = time.perf_counter() - started
    query_start = time.perf_counter()
    for definition in records.definitions:
        if definition.symbol_kind not in ("class", "function"):
            continue
        try:
            requests["prepare"] += 1
            prepared = peer.request(
                "textDocument/prepareCallHierarchy",
                {
                    "textDocument": {"uri": (root / definition.path).as_uri()},
                    "position": native_position(
                        records.texts[definition.path], definition.name_span.start, unit
                    ),
                },
                timeout,
            )
            if not prepared:
                requests["empty"] += 1
            for item in prepared or []:
                requests["outgoing"] += 1
                source = records.lookup(item, unit)
                outgoing = peer.request(
                    "callHierarchy/outgoingCalls", {"item": item}, timeout
                )
                for ti, entry in enumerate(outgoing or []):
                    for fi, native in enumerate(entry["fromRanges"]):
                        raw_id = f"{definition.id}:{requests['outgoing']}:{ti}:{fi}"
                        site = target = None
                        try:
                            path = records.path(item["uri"])
                            span = records.range(path, native, unit)
                            matches = records.by_token[(path, span.start, span.end)]
                            if len(matches) != 1:
                                reason, stage = (
                                    "coordinate_no_unique_callsite",
                                    "coordinate",
                                )
                            else:
                                site = matches[0]
                                if not records.owner_matches(source, site):
                                    reason, stage = "owner_mismatch", "owner"
                                else:
                                    target = records.lookup(entry["to"], unit)
                                    reason, stage = (
                                        ("mapped_success", "mapped")
                                        if target
                                        else ("symbol_missing_or_external", "symbol")
                                    )
                        except (KeyError, ValueError, TypeError):
                            reason, stage = "coordinate_unconvertible", "coordinate"
                        records.mapped(raw_id, reason, stage, site, target)
                        if stage == "mapped":
                            records.candidate(site, target, "type_inference", raw_id)
        except (
            TimeoutError,
            LspError,
            OSError,
            KeyError,
            ValueError,
            TypeError,
        ) as exc:
            reason = "timeout" if isinstance(exc, TimeoutError) else "tool_error"
            requests["timeout" if reason == "timeout" else "error"] += 1
            for c in records.callsites:
                if records.owner_matches(definition, c):
                    records.reasons[c.id].add(reason)
    partial = bool(
        requests["error"] or requests["timeout"] or records.data["parse_failures"]
    )
    query_seconds = time.perf_counter() - query_start
    return records.finish(
        unit=unit, availability="partial" if partial else "available"
    ), {
        "requests": requests,
        "initialize": init,
        "encoding": unit,
        "setup_seconds": setup_seconds,
        "query_seconds": query_seconds,
    }


def arcgraph_records(
    snapshot: Snapshot, root: Path, backend: Producer, data: dict, content=None
) -> tuple[Generation, dict]:
    """Wrap an in-memory graph; never invoke build() or persist a graph."""
    verify(snapshot, root)
    if content is None:
        from arcgraph.core.scanner import FileScanner, SourceRoot
        from arcgraph.pipeline.indexer import ArcGraphIndexer

        files = FileScanner(root, [SourceRoot(".")], file_extensions={".py"}).scan()
        paths = {f.path for f in snapshot.files}
        content = ArcGraphIndexer(root, source_roots=[SourceRoot(".")]).analyze_files(
            [f for f in files if f.path in paths], []
        )
    raw = {
        "nodes": [n.model_dump(mode="json") for n in content.nodes],
        "edges": [e.model_dump(mode="json") for e in content.edges],
        "warnings": [w.model_dump(mode="json") for w in content.warnings],
    }
    return wrap_graph(snapshot, root, backend, data, raw), raw


def wrap_graph(
    snapshot: Snapshot,
    root: Path,
    backend: Producer,
    data: dict,
    raw: dict,
    *,
    record_factory=Records,
) -> Generation:
    records = record_factory(snapshot, root, backend, data)
    failures = [
        (w["path"], w["message"])
        for w in raw.get("warnings", [])
        if w.get("kind") == "parse_error" and w.get("path")
    ]
    for site in records.callsites:
        if site.path in {path for path, _ in failures}:
            records.reasons[site.id].add("structural_parser_unsupported")
    nodes = {n["id"]: n for n in raw["nodes"]}
    by_node = {}
    for n in nodes.values():
        if n["kind"] == "module":
            by_node[n["id"]] = "module:" + n["path"]
            continue
        choices = [
            d
            for d in records.data["definitions"]
            if d["path"] == n.get("path")
            and d["qualname"] == n.get("qualname")
            and n.get("start_line") in (d["line"], d["first_line"])
        ]
        if len(choices) == 1:
            by_node[n["id"]] = choices[0]["id"]
    for ei, edge in enumerate(raw["edges"]):
        if edge["kind"] not in ("calls", "constructs", "dynamic_call"):
            continue
        properties = edge.get("properties", {})
        facts = [properties.get("callsite", {}), *properties.get("callsites", [])]
        facts = list(
            {
                digest(
                    (
                        f.get("callsite_id"),
                        f.get("line"),
                        f.get("column"),
                        f.get("call_expression"),
                    )
                ): f
                for f in facts
                if f
            }.values()
        ) or [{}]
        for fi, fact in enumerate(facts):
            rid = f"edge:{ei}:{fi}"
            site = target = None
            path = nodes.get(edge["source"], {}).get("path")
            line, col = fact.get("line"), fact.get("column")
            matches = (
                records.by_start[(path, (line - 1, col))]
                if line is not None and col is not None
                else []
            )
            if len(matches) > 1:
                matches = [
                    s for s in matches if s.expression == fact.get("call_expression")
                ]
            if len(matches) != 1:
                reason, stage = "coordinate_no_unique_callsite", "coordinate"
            else:
                site = matches[0]
                target = records.defs.get(by_node.get(edge["target"]))
                if (
                    not records.owner_matches(
                        records.defs.get(by_node.get(edge["source"])), site
                    )
                    and by_node.get(edge["source"]) != site.owner
                ):
                    reason, stage = "owner_mismatch", "owner"
                else:
                    reason, stage = (
                        ("mapped_success", "mapped")
                        if target
                        else ("symbol_missing_or_external", "symbol")
                    )
            records.mapped(rid, reason, stage, site, target)
            if stage == "mapped":
                strategy = (
                    fact.get("resolution_strategy")
                    or edge.get("resolution", {}).get("strategy")
                    or "unknown_strategy"
                )
                method = (
                    "name_guess"
                    if strategy
                    in (
                        "unique_method_fallback",
                        "unique_short_name_fallback",
                        "receiver_name_boundary_method",
                    )
                    else strategy
                )
                records.candidate(site, target, method, rid, edge["confidence"])
    generation = records.finish(
        availability=(
            "partial" if records.data["parse_failures"] or failures else "available"
        )
    )
    return generation.model_copy(
        update={
            "parse_failures": tuple(
                sorted(set((*generation.parse_failures, *failures)))
            )
        }
    ).seal()
