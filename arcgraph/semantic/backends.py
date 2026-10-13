"""Two record producers, independent of publishing or graph storage."""

from __future__ import annotations

from collections import defaultdict
import json
import ntpath
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import time
from urllib.parse import unquote, urlsplit

from .contract import (
    Callsite,
    Candidate,
    Definition,
    Envelope,
    MappingRow,
    Producer,
    Snapshot,
    Span,
    digest,
)
from .lsp import LspError, Peer
from .snapshot import native_position, native_span, verify


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
        """Decode using the snapshot's path flavour, then check containment.

        Pure paths allow Windows drive/UNC cases to be checked on any host;
        native paths additionally resolve symlinks before mapping a symbol.
        """
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.query or parsed.fragment:
            return None
        value = unquote(parsed.path)
        if "\x00" in value:
            return None
        if isinstance(self.root, PureWindowsPath):
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                value = "//" + unquote(parsed.netloc) + value
            elif (
                len(value) >= 4
                and value[0] == "/"
                and value[1].isalpha()
                and value[2:4] == ":/"
            ):
                value = value[1:]
            value = ntpath.normpath(value)
        else:
            if parsed.netloc.lower() not in ("", "localhost"):
                return None
            value = posixpath.normpath(value)
        path = (
            PureWindowsPath(value)
            if isinstance(self.root, PureWindowsPath)
            else Path(value) if isinstance(self.root, Path) else PurePosixPath(value)
        )
        if not path.is_absolute():
            return None
        if isinstance(path, Path):
            path = path.resolve()
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
        choices = [definition for definition in choices if definition.name_span == span]
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


def pyright_records(
    snapshot: Snapshot,
    root: Path,
    backend: Producer,
    data: dict,
    peer: Peer,
    *,
    timeout: float = 30,
    initialize_result: dict | None = None,
    record_factory=None,
) -> tuple[object, dict]:
    started = time.perf_counter()
    verify(snapshot, root)
    if record_factory is None:
        from .anchors import AnchoredRecords

        record_factory = AnchoredRecords
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
        provider = caps.get("callHierarchyProvider")
        if unit not in ("utf-8", "utf-16") or not (
            provider is True or isinstance(provider, dict)
        ):
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
    if hasattr(peer, "initialize_count"):
        requests["initialize"] = peer.initialize_count
    return records.finish(
        unit=unit, availability="partial" if partial else "available"
    ), {
        "requests": requests,
        "initialize": init,
        "encoding": unit,
        "setup_seconds": setup_seconds,
        "query_seconds": query_seconds,
    }


def wrap_graph(
    snapshot: Snapshot,
    root: Path,
    backend: Producer,
    data: dict,
    raw: dict,
    *,
    record_factory=None,
):
    if record_factory is None:
        from .anchors import AnchoredRecords

        record_factory = AnchoredRecords
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
    by_location = defaultdict(list)
    for definition in records.data["definitions"]:
        for line in {definition["line"], definition["first_line"]}:
            by_location[(definition["path"], definition["qualname"], line)].append(
                definition
            )
    for n in nodes.values():
        if n["kind"] == "module":
            by_node[n["id"]] = "module:" + n["path"]
            continue
        choices = by_location[(n.get("path"), n.get("qualname"), n.get("start_line"))]
        if len(choices) == 1:
            by_node[n["id"]] = choices[0]["id"]
    for ei, edge in enumerate(raw["edges"]):
        if edge["kind"] not in ("calls", "constructs", "dynamic_call", "initializes"):
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
