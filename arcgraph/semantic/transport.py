"""Complete LSP collection into a checked journal before graph objects load."""

import hashlib
from contextlib import ExitStack
import os
import time

from .journal import dump, load, wire_line
from .lsp import LspError
from .runtime import SessionPool, capabilities, initialize_params, session
from .external_targets import Capture
from .contract import digest
from .reuse import ReuseRejected

SESSION_DEFINITIONS = 512


def collect(plan_path, root, config, output, *, reuse=None):
    plan = load(plan_path)
    started = time.perf_counter()
    checksum = hashlib.sha256()
    count = 0
    pending = output / "responses.part"
    counters = {"initialize": 0, "prepare": 0, "outgoing": 0, "error": 0, "timeout": 0}
    durations = {"initialize": 0.0, "prepare": 0.0, "outgoing": 0.0}
    launch_seconds = 0.0
    external = Capture(root, config)
    first_prepare_seconds = 0.0
    restart_initialize_seconds = 0.0
    with (
        (output / "lsp.jsonl").open("w", encoding="utf-8") as transcript,
        pending.open("w", encoding="utf-8") as journal,
    ):

        class Transcript:
            def append(self, row):
                wire_line(transcript, {"seconds": time.perf_counter() - started, **row})

        wire_line(journal, {"snapshot": plan["snapshot_id"]})
        with ExitStack() as lifetime, ExitStack() as peers:
            pool = lifetime.enter_context(SessionPool().lifetime())
            launch_started = time.perf_counter()
            peer, detail = peers.enter_context(
                session(root, config, transcript=Transcript(), pool=pool)
            )
            launch_seconds += time.perf_counter() - launch_started
            sessions = [dict(first_definition=0, **detail)]
            errors = []

            cached = None
            reused_definitions = 0
            queried_definitions = 0

            def request(method, params, label):
                nonlocal count
                if cached is None:
                    counters[label] += 1
                tick = time.perf_counter()
                row = {"method": method, "params": params}
                try:
                    if cached is None:
                        result = peer.request(method, params)
                    else:
                        previous = next(cached)
                        if previous["method"] != method or previous["params"] != params:
                            raise ReuseRejected(
                                "Reused native request does not match current plan"
                            )
                        result = previous["result"]
                    if method == "callHierarchy/outgoingCalls":
                        if cached is not None:
                            recapture = Capture(root, config)
                            for entry in result or ():
                                recapture.item(entry["to"], current_detail)
                            reuse.verify_capture(recapture.files)
                        for entry in result or ():
                            external.item(entry["to"], current_detail)
                    row["result"] = result
                    return result
                except (LspError, OSError, TimeoutError) as exc:
                    kind = "timeout" if isinstance(exc, TimeoutError) else "error"
                    counters[kind] += 1
                    row["error"] = {
                        "kind": "timeout" if kind == "timeout" else "tool_error",
                        "message": str(exc),
                    }
                    raise
                finally:
                    durations[label] += time.perf_counter() - tick
                    checksum.update(wire_line(journal, row))
                    count += 1

            initialize = request("initialize", initialize_params(root), "initialize")
            current_detail = detail
            unit = capabilities(initialize)
            if reuse is not None and reuse.encoding != unit:
                raise ReuseRejected("Reused journal position encoding changed")
            peer.notify("initialized", {})
            startup = time.perf_counter() - started
            opened = set()
            for index, definition in enumerate(plan["definitions"]):
                cached = (
                    reuse.rows(definition, root, current_detail)
                    if reuse is not None
                    else None
                )
                if (
                    cached is None
                    and queried_definitions
                    and queried_definitions % SESSION_DEFINITIONS == 0
                ):
                    errors.extend(peer.stderr)
                    peers.close()
                    launch_started = time.perf_counter()
                    peer, next_detail = peers.enter_context(
                        session(root, config, transcript=Transcript(), pool=pool)
                    )
                    launch_seconds += time.perf_counter() - launch_started
                    current_detail = next_detail
                    # No cached rows are consumed by a physical handshake.
                    tick = time.perf_counter()
                    next_initialize = request(
                        "initialize", initialize_params(root), "initialize"
                    )
                    restart_initialize_seconds += time.perf_counter() - tick
                    if capabilities(next_initialize) != unit:
                        raise ValueError(
                            "Position encoding changed across pyright sessions"
                        )
                    peer.notify("initialized", {})
                    sessions.append(dict(first_definition=index, **next_detail))
                    opened.clear()
                if cached is None:
                    queried_definitions += 1
                else:
                    reused_definitions += 1
                path = definition["path"]
                if cached is None and path not in opened:
                    from .snapshot import canonical

                    peer.notify(
                        "textDocument/didOpen",
                        {
                            "textDocument": {
                                "uri": (root / path).as_uri(),
                                "languageId": "python",
                                "version": 1,
                                "text": canonical((root / path).read_bytes()),
                            }
                        },
                    )
                    opened.add(path)
                try:
                    prepare_started = time.perf_counter()
                    items = request(
                        "textDocument/prepareCallHierarchy",
                        {
                            "textDocument": {"uri": (root / path).as_uri()},
                            "position": definition["positions"][unit],
                        },
                        "prepare",
                    )
                    if index % SESSION_DEFINITIONS == 0:
                        first_prepare_seconds += time.perf_counter() - prepare_started
                    for item in items or ():
                        request(
                            "callHierarchy/outgoingCalls", {"item": item}, "outgoing"
                        )
                    if cached is not None and next(cached, None) is not None:
                        raise ReuseRejected("Unused reused native responses")
                except (LspError, OSError, TimeoutError):
                    continue
            errors.extend(peer.stderr)
            stderr = b"".join(errors)
        wire_line(
            journal,
            {"complete": True, "count": count, "checksum": checksum.hexdigest()},
        )
        journal.flush()
        os.fsync(journal.fileno())
    os.replace(pending, output / "responses.jsonl")
    (output / "pyright-stderr.txt").write_bytes(stderr)
    dump(external.files, output / "external-targets.json")
    detail.update(
        startup_seconds=startup,
        launch_seconds=launch_seconds,
        first_prepare_seconds=first_prepare_seconds,
        restart_initialize_seconds=restart_initialize_seconds,
        indexing_measurement="Indexing is lazy and coupled to requests; first prepare per session is a latency proxy, not independent total indexing time.",
        request_seconds=durations,
        requests=counters,
        encoding=unit,
        sessions=sessions,
        total_seconds=time.perf_counter() - started,
        external_capture=digest(external.files),
    )
    if reuse is not None:
        detail["reuse"] = {
            "definitions": reused_definitions,
            "queried": queried_definitions,
            "affected_paths": sorted(reuse.affected),
        }
    dump(detail, output / "transport.json")
    return detail
