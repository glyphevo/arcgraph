"""Lightweight, sequential process isolation for the standalone pipeline.

Executed as a file so the coordinator and LSP collector need not import graph
schemas. Semantic validation/mapping still runs through the original pipeline.
Intermediate journals are complete, checked, and streamed; no candidates or
structural fields are discarded. There is no Node heap limit by default.
"""

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tokenize

SESSION_DEFINITIONS = 512


def dump(value, path):
    pending = path.with_suffix(path.suffix + ".part")
    with pending.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)


def load(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def wire_line(handle, value):
    line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    handle.write(line)
    return line.encode()


def write_structure_cache(bundle, path):
    pending = path.with_suffix(".part")
    with pending.open("w", encoding="utf-8") as handle:
        wire_line(handle, bundle.model_dump(mode="json", exclude={"records"}))
        for record in bundle.records:
            wire_line(handle, record.model_dump(mode="json"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)


def intern_keys(value):
    """Share immutable strings across records; retain every key and value."""
    if isinstance(value, dict):
        keys = {sys.intern(key): item for key, item in value.items()}
        value.clear()
        value.update(keys)
        for key in value:
            value[key] = intern_keys(value[key])
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = intern_keys(item)
        return value
    if isinstance(value, str):
        return sys.intern(value)
    return value


def read_structure_cache(path):
    from arcgraph.semantic_prototype.structure_provider import (
        StructuralGeneration,
        StructuralRecord,
    )

    with path.open(encoding="utf-8") as handle:
        header = json.loads(next(handle))
        rows = [
            StructuralRecord.model_validate(intern_keys(json.loads(line)))
            for line in handle
        ]
    return StructuralGeneration.model_validate({**header, "records": rows}).checked()


def write_graph_cache(graph, path):
    pending = path.with_suffix(".part")
    checksum = hashlib.sha256()
    with pending.open("w", encoding="utf-8") as handle:
        wire_line(handle, {"counts": {key: len(rows) for key, rows in graph.items()}})
        for key, rows in graph.items():
            for row in rows:
                checksum.update(wire_line(handle, {"section": key, "row": row}))
        wire_line(handle, {"complete": True, "checksum": checksum.hexdigest()})
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)


def read_graph_cache(path):
    checksum = hashlib.sha256()
    with path.open(encoding="utf-8") as handle:
        counts = json.loads(next(handle))["counts"]
        graph = {key: [] for key in counts}
        for line in handle:
            row = intern_keys(json.loads(line))
            if "complete" in row:
                if row != {
                    "complete": True,
                    "checksum": checksum.hexdigest(),
                } or handle.read(1):
                    raise ValueError("corrupt graph journal")
                break
            checksum.update(line.encode())
            graph[row["section"]].append(row["row"])
        else:
            raise ValueError("incomplete graph journal")
    if {key: len(rows) for key, rows in graph.items()} != counts:
        raise ValueError("graph journal count mismatch")
    return graph


def heavy_context(args):
    from arcgraph.semantic_prototype.contract import Snapshot
    from arcgraph.semantic_prototype.snapshot import verify

    snapshot = Snapshot.model_validate_json(args.snapshot.read_bytes())
    verify(snapshot, args.root)
    return snapshot


def structure_stage(args, cache):
    from arcgraph.semantic_prototype.anchors import View
    from arcgraph.semantic_prototype.contract import write_generation
    from arcgraph.semantic_prototype.snapshot import native_position, canonical
    from arcgraph.semantic_prototype.structure_provider import analyze, admit_structure

    snapshot = heavy_context(args)
    bundle = analyze(
        snapshot,
        args.root,
        {snapshot.target_python: args.interpreter},
        access_facts=True,
    )
    allowed, reason = admit_structure(bundle, snapshot)
    if not allowed:
        raise ValueError(reason)
    view = View(bundle)
    texts = {
        f.path: canonical((args.root / f.path).read_bytes()) for f in snapshot.files
    }
    plan = {
        "snapshot": bundle.snapshot.model_dump(mode="json"),
        "snapshot_id": snapshot.id,
        "definitions": [
            {
                "path": d.path,
                "positions": {
                    unit: native_position(texts[d.path], d.name_span.start, unit)
                    for unit in ("utf-8", "utf-16")
                },
            }
            for d in view.definitions
            if d.symbol_kind in ("class", "function")
        ],
    }
    write_generation(bundle, args.output / "structure.json")
    write_structure_cache(bundle, cache / "structure.jsonl")
    dump(plan, cache / "plan.json")


def initialize_params(root):
    return {
        "processId": None,
        "rootUri": root.as_uri(),
        "workspaceFolders": [{"uri": root.as_uri(), "name": "frozen"}],
        "capabilities": {
            "general": {"positionEncodings": ["utf-8", "utf-16"]},
            "textDocument": {"callHierarchy": {"dynamicRegistration": False}},
        },
    }


def verify_sources(plan, root):
    for file in plan["snapshot"]["files"]:
        if (
            hashlib.sha256((root / file["path"]).read_bytes()).hexdigest()
            != file["raw_digest"]
        ):
            raise ValueError("frozen source changed during collection")


def transport_stage(args, cache, client_class=None):
    # Loading this stdlib-only module directly avoids arcgraph.__init__/pydantic.
    spec = importlib.util.spec_from_file_location(
        "staged_lsp", Path(__file__).with_name("lsp.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = load(cache / "plan.json")
    verify_sources(plan, args.root)
    config = load(args.root / "pyrightconfig.json")
    config["prototype_node_heap_mib"] = args.pyright_heap_mib or "runtime-default"
    config["prototype_transport"] = (
        f"restart/{SESSION_DEFINITIONS}-definitions/lazy-open-frozen-workspace"
    )
    config["prototype_node_options_digest"] = hashlib.sha256(
        os.environ.get("NODE_OPTIONS", "").encode()
    ).hexdigest()
    dump(config, cache / "config.json")

    class Transcript:
        def __init__(self, handle):
            self.handle = handle

        def append(self, row):
            wire_line(self.handle, row)

    pending = cache / "responses.part"
    responses = cache / "responses.jsonl"
    checksum = hashlib.sha256()
    count = 0
    sessions = []
    errors = []
    started = time.perf_counter()
    with (
        (args.output / "lsp.jsonl").open("w", encoding="utf-8") as transcript,
        pending.open("w", encoding="utf-8") as journal,
    ):
        wire_line(journal, {"snapshot": plan["snapshot_id"]})
        peer = (client_class or module.Client)(
            [args.pyright, "--stdio"],
            args.root,
            dict(os.environ),
            {"python": {"analysis": config}},
            transcript=Transcript(transcript),
        )

        def request(method, params):
            nonlocal count
            row = {"method": method, "params": params}
            try:
                result = peer.request(method, params)
                row["result"] = result
            except (TimeoutError, module.LspError, OSError) as exc:
                row["error"] = {
                    "kind": (
                        "timeout" if isinstance(exc, TimeoutError) else "tool_error"
                    ),
                    "message": str(exc),
                }
                raise
            finally:
                checksum.update(wire_line(journal, row))
                count += 1
            return result

        opened = set()

        def open_document(path):
            if path in opened:
                return
            raw = (args.root / path).read_bytes()
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            text = raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
            peer.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": (args.root / path).as_uri(),
                        "languageId": "python",
                        "version": 1,
                        "text": text,
                    }
                },
            )
            opened.add(path)

        try:
            init = request("initialize", initialize_params(args.root))
            caps = (init or {}).get("capabilities", {})
            unit = caps.get("positionEncoding", "utf-16")
            supported = unit in ("utf-8", "utf-16") and caps.get(
                "callHierarchyProvider"
            )
            if supported:
                peer.notify("initialized", {})
            sessions.append({"first_definition": 0, "initialize": init})
            setup = time.perf_counter() - started
            query_started = time.perf_counter()
            for index, definition in enumerate(
                plan["definitions"] if supported else ()
            ):
                if index and index % SESSION_DEFINITIONS == 0:
                    peer.close()
                    errors.extend(peer.stderr)
                    verify_sources(plan, args.root)
                    peer = (client_class or module.Client)(
                        [args.pyright, "--stdio"],
                        args.root,
                        dict(os.environ),
                        {"python": {"analysis": config}},
                        transcript=Transcript(transcript),
                    )
                    # The entire frozen workspace remains present and configured.
                    # Open each queried file lazily; do not restrict queries or targets.
                    next_init = peer.request("initialize", initialize_params(args.root))
                    next_caps = (next_init or {}).get("capabilities", {})
                    if next_caps.get(
                        "positionEncoding", "utf-16"
                    ) != unit or not next_caps.get("callHierarchyProvider"):
                        raise ValueError(
                            "capabilities changed across transport sessions"
                        )
                    peer.notify("initialized", {})
                    opened.clear()
                    sessions.append(
                        {"first_definition": index, "initialize": next_init}
                    )
                open_document(definition["path"])
                try:
                    prepared = request(
                        "textDocument/prepareCallHierarchy",
                        {
                            "textDocument": {
                                "uri": (args.root / definition["path"]).as_uri()
                            },
                            "position": definition["positions"][unit],
                        },
                    )
                    for item in prepared or []:
                        request("callHierarchy/outgoingCalls", {"item": item})
                except (TimeoutError, module.LspError, OSError):
                    continue
            query = time.perf_counter() - query_started
        except (TimeoutError, module.LspError, OSError):
            setup, query = time.perf_counter() - started, 0
        finally:
            peer.close()
            errors.extend(peer.stderr)
        verify_sources(plan, args.root)
        wire_line(
            journal,
            {"complete": True, "count": count, "checksum": checksum.hexdigest()},
        )
        journal.flush()
        os.fsync(journal.fileno())
        (args.output / "pyright-stderr.txt").write_bytes(b"".join(errors))
    os.replace(pending, responses)
    dump(
        {"setup_seconds": setup, "query_seconds": query, "physical_sessions": sessions},
        cache / "transport.json",
    )


class JournalReplay:
    def __init__(self, path, snapshot_id):
        self.handle = path.open(encoding="utf-8")
        header = json.loads(next(self.handle))
        if header != {"snapshot": snapshot_id}:
            self.handle.close()
            raise ValueError("journal snapshot mismatch")
        self.checksum = hashlib.sha256()
        self.count = 0

    def notify(self, method, params):
        # Notifications already ran on the same verified frozen documents.
        pass

    def request(self, method, params, timeout=30):
        from arcgraph.semantic_prototype.lsp import LspError

        line = next(self.handle)
        row = json.loads(line)
        if row.get("method") != method or row.get("params") != params:
            raise ValueError("journal request mismatch or incomplete run")
        self.checksum.update(line.encode())
        self.count += 1
        if "error" in row:
            if row["error"]["kind"] == "timeout":
                raise TimeoutError(row["error"]["message"])
            raise LspError(row["error"]["message"])
        return row["result"]

    def close(self):
        try:
            footer = json.loads(next(self.handle))
            if footer != {
                "complete": True,
                "count": self.count,
                "checksum": self.checksum.hexdigest(),
            } or self.handle.read(1):
                raise ValueError("journal truncated, corrupt or has unused responses")
        finally:
            self.handle.close()


def graph_stage(args, cache):
    from arcgraph.semantic_prototype.pipeline import existing_graph

    snapshot = heavy_context(args)
    raw = existing_graph(snapshot, args.root)
    dump(raw, args.output / "existing-graph.json")
    write_graph_cache(raw, cache / "graph.jsonl")
    heavy_context(args)


def map_stage(args, cache):
    from arcgraph.semantic_prototype.contract import write_generation
    from arcgraph.semantic_prototype.pipeline import run

    snapshot = heavy_context(args)
    from arcgraph.semantic_prototype.disk_structure import read

    bundle = read(cache / "structure.jsonl")
    peer = JournalReplay(cache / "responses.jsonl", snapshot.id)
    try:
        _, primary, fallback, shadow, meta, _ = run(
            snapshot,
            args.root,
            args.interpreter,
            peer,
            pyright_version=args.pyright_version,
            configuration=load(cache / "config.json"),
            structure=bundle,
            graph=read_graph_cache(cache / "graph.jsonl"),
        )
    finally:
        peer.handle.close()
    meta["pyright"].update(load(cache / "transport.json"))
    for name, value in (("pyright", primary), ("arcgraph", fallback)):
        write_generation(value, args.output / (name + ".json"))
    dump(shadow, args.output / "projection.json")
    dump(meta, cache / "mapping.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "root", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("interpreter", "pyright", "pyright-version"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--pyright-heap-mib", type=int)
    parser.add_argument("--stage", choices=("structure", "transport", "graph", "map"))
    args = parser.parse_args(argv)
    if args.pyright_heap_mib is not None:
        if args.pyright_heap_mib < 128:
            raise ValueError("pyright heap must be at least 128 MiB")
        os.environ["NODE_OPTIONS"] = f"--max-old-space-size={args.pyright_heap_mib}"
    cache = args.output / ".stage-cache"
    if args.stage:
        if args.stage != "transport":
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        globals()[args.stage + "_stage"](args, cache)
        return 0
    args.output.mkdir(parents=True, exist_ok=False)
    cache.mkdir()
    times = {}
    for stage in ("structure", "transport", "graph", "map"):
        dump({"phase": stage, "started": time.time()}, cache / "phase.json")
        started = time.perf_counter()
        subprocess.run(
            [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                *(sys.argv[1:] if argv is None else argv),
                "--stage",
                stage,
            ],
            check=True,
        )
        times[stage] = time.perf_counter() - started
    meta = load(cache / "mapping.json")
    meta["process_stages_seconds"] = times
    meta["staged_complete"] = True
    dump(meta, args.output / "run.json")
    dump({"phase": "complete", "started": time.time()}, cache / "phase.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
