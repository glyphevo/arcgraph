from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from pydantic import ValidationError

from arcgraph.semantic_prototype.backends import (
    Records,
    oracle,
    producer,
    pyright_records,
    wrap_graph,
    arcgraph_records,
)
from arcgraph.semantic_prototype.contract import (
    Answer,
    Candidate,
    MappingRow,
    Snapshot,
    Source,
    Span,
    admit,
    read_generation,
    write_generation,
)
from arcgraph.semantic_prototype.snapshot import (
    capture,
    canonical,
    column,
    native_span,
    native_position,
    verify,
)
from arcgraph.semantic_prototype.shadow import project

FIXTURE = Path(__file__).parent / "fixtures/semantic_prototype"


def view(tmp_path, source=None):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "demo.py").write_text(
        source or (FIXTURE / "source.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    root = tmp_path / "frozen"
    snapshot = capture(
        source_root, ["demo.py"], root, target_python="3.14", platform="Darwin"
    )
    data = oracle(snapshot, root)
    return snapshot, root, data


def backend(name="pyright"):
    return producer(name, "1.1.414" if name == "pyright" else "test", ("3.14",), {})


class Replay:
    def __init__(self, root):
        self.root = root
        self.messages = json.loads(
            (FIXTURE / "recorded-lsp.json").read_text(encoding="utf-8")
        )
        self.requests = [
            r["message"]
            for r in self.messages
            if r["direction"] == "send"
            and "method" in r["message"]
            and "id" in r["message"]
            and r["message"]["method"] != "shutdown"
        ]
        self.index = 0
        self.notifications = []

    def request(self, method, params, timeout=30):
        expected = self.requests[self.index]
        self.index += 1
        normalized = json.loads(
            json.dumps(params).replace(self.root.as_uri(), "file:///FROZEN")
        )
        assert (method, normalized) == (expected["method"], expected["params"])
        response = next(
            r["message"]
            for r in self.messages
            if r["direction"] == "receive"
            and r["message"].get("id") == expected["id"]
            and "method" not in r["message"]
        )
        return json.loads(
            json.dumps(response.get("result")).replace(
                "file:///FROZEN", self.root.as_uri()
            )
        )

    def notify(self, method, params):
        self.notifications.append((method, params))


def test_recorded_pyright_replay_uses_default_utf16_and_exact_tokens(tmp_path):
    snapshot, root, data = view(tmp_path)
    peer = Replay(root)
    generation, meta = pyright_records(snapshot, root, backend(), data, peer)
    assert meta["encoding"] == "utf-16"
    assert meta["requests"]["error"] == 0
    calls = [a for a in generation.answers if a.outcome == "candidates"]
    assert len(calls) == 3
    assert len({a.subject for a in calls}) == 3
    assert {c.semantics for a in calls for c in a.candidates} == {
        "implementation",
        "logical_body",
    }
    assert peer.notifications[1][1]["textDocument"]["version"] == 1
    assert "😀" in peer.notifications[1][1]["textDocument"]["text"]
    assert sum(r.first_reason == "mapped_success" for r in generation.mapping) == 3
    assert admit(generation, snapshot, backend()) == (
        True,
        "experimental_environment_unknown",
    )
    out = tmp_path / "generation.json"
    write_generation(generation, out)
    assert read_generation(out) == generation
    assert not list(tmp_path.glob(".generation-*"))


@pytest.mark.parametrize(
    "unit,col", [("utf-8", 8), ("utf-16", 5), ("unicode_scalar", 4)]
)
def test_coordinate_conversion_and_roundtrip(unit, col):
    text = "é😀 f()\n"
    assert column(text, 0, col, unit) == 8
    assert native_position(text, (0, 8), unit) == {"line": 0, "character": col}
    assert native_span(
        text,
        {
            "start": {"line": 0, "character": col},
            "end": {"line": 0, "character": col + 1},
        },
        unit,
    ) == Span(start=(0, 8), end=(0, 9))
    assert canonical(b"\xef\xbb\xbfabc\r\nx\r") == "abc\nx\n"
    assert canonical(b"# coding: latin-1\n\xe9") == "# coding: latin-1\né"


@pytest.mark.parametrize(
    "line,col,unit",
    [
        (-1, 0, "utf-8"),
        (2, 0, "utf-8"),
        (0, -1, "utf-8"),
        (0, 1, "utf-8"),
        (0, 2, "utf-16"),
        (0, 90, "unicode_scalar"),
        (0, 0, "unknown"),
        (True, 0, "utf-8"),
    ],
)
def test_unconvertible_positions_do_not_guess(line, col, unit):
    with pytest.raises(ValueError):
        column("é😀", line, col, unit)


@pytest.mark.parametrize(
    "path", ["/x.py", "../x.py", "a/../x.py", "a//x.py", "a\\x.py"]
)
def test_snapshot_rejects_noncanonical_paths(path):
    with pytest.raises(ValueError):
        Source(path=path, raw_digest="", text_digest="")


def test_snapshot_is_frozen_dirty_and_checks_symlinks(tmp_path):
    snapshot, root, data = view(tmp_path)
    (tmp_path / "source/demo.py").write_text("changed live content", encoding="utf-8")
    verify(snapshot, root)
    with pytest.raises(ValueError, match="new"):
        capture(
            tmp_path / "source",
            ["demo.py"],
            root,
            target_python="3.14",
            platform="Darwin",
        )
    (root / "demo.py").write_text("changed frozen content", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        verify(snapshot, root)
    (tmp_path / "source/link.py").symlink_to(tmp_path / "source/demo.py")
    with pytest.raises(ValueError, match="symlink"):
        capture(
            tmp_path / "source",
            ["link.py"],
            tmp_path / "other",
            target_python="3.14",
            platform="Darwin",
        )
    with pytest.raises(ValueError):
        Snapshot(
            files=(snapshot.files[0], snapshot.files[0]),
            target_python="3.14",
            platform="Darwin",
        )


def test_structure_preserves_scope_phase_declarations_and_parse_gaps(tmp_path):
    source = "from typing import Protocol\nfrom abc import abstractmethod\ndef outer(x=f()):\n    a=lambda: f()\n    b=(f() for x in f())\n    async def inner(): f()\nclass Interface(Protocol):\n    def t(self): ...\nclass Abstract:\n    @abstractmethod\n    def t(self): return 1\n"
    snapshot, root, data = view(tmp_path, source)
    assert {
        "definition_default",
        "lambda_body",
        "deferred_generator",
        "function_body",
    } <= {s["phase"] for s in data["callsites"]}
    assert len([d for d in data["definitions"] if d["declaration_only"]]) == 2
    records = Records(snapshot, root, backend(), data).finish()
    assert records.count == len(records.definitions) + 2 * len(records.callsites)
    other = tmp_path / "bad.py"
    other.write_text("def :", encoding="utf-8")
    from arcgraph.semantic_prototype.structure import scan

    assert scan(tmp_path, ["bad.py"])["parse_failures"]
    import sys

    assert oracle(snapshot, root, sys.executable)["callsites"] == data["callsites"]


def test_answer_algebra_requires_explicit_unknown_and_open_candidates(tmp_path):
    snapshot, root, data = view(tmp_path)
    rec = Records(snapshot, root, backend(), data)
    envelope = rec.envelope("answer")
    args = {"envelope": envelope, "subject": rec.callsites[0].id}
    with pytest.raises(ValidationError):
        Answer(**args, outcome="candidates")
    with pytest.raises(ValidationError):
        Answer(**args, outcome="unknown")
    with pytest.raises(ValidationError):
        Answer(**args, outcome="negative", reasons=("not_callable",))
    target = rec.definitions[0].id
    candidate = Candidate(
        target=target,
        dispatch="invoke",
        semantics="implementation",
        method="lexical_binding",
    )
    with pytest.raises(ValidationError):
        Answer(
            **args,
            outcome="negative",
            reasons=("capability_unsupported",),
            candidates=(candidate,),
        )
    assert (
        Answer(
            **args, outcome="negative", reasons=("capability_unsupported",)
        ).completeness
        == "unknown"
    )


def test_admission_rejects_corruption_wrong_identity_and_unsupported_target(tmp_path):
    snapshot, root, data = view(tmp_path)
    rec = Records(snapshot, root, backend(), data)
    good = rec.finish()
    for bad in [
        good.model_copy(update={"complete": False}),
        good.model_copy(update={"count": 0}),
        good.model_copy(update={"checksum": "bad"}),
    ]:
        assert not admit(bad, snapshot, backend())[0]
        with pytest.raises(ValueError):
            write_generation(bad, tmp_path / "bad.json")
    assert (
        admit(good, snapshot.model_copy(update={"platform": "Windows"}), backend())[1]
        == "identity_mismatch"
    )
    assert admit(good, snapshot, backend("arcgraph"))[1] == "identity_mismatch"
    unsupported = good.model_copy(
        update={"producer": backend().model_copy(update={"target_versions": ("3.12",)})}
    )
    # A correctly sealed unsupported producer still cannot pass.
    env = {**rec.envelope("answer").model_dump(), "producer": unsupported.producer.id}
    unsupported = unsupported.model_copy(
        update={
            "definitions": tuple(
                d.model_copy(
                    update={
                        "envelope": d.envelope.model_copy(
                            update={"producer": unsupported.producer.id}
                        )
                    }
                )
                for d in good.definitions
            ),
            "callsites": tuple(
                d.model_copy(
                    update={
                        "envelope": d.envelope.model_copy(
                            update={"producer": unsupported.producer.id}
                        )
                    }
                )
                for d in good.callsites
            ),
            "answers": tuple(
                d.model_copy(
                    update={
                        "envelope": d.envelope.model_copy(
                            update={"producer": unsupported.producer.id}
                        )
                    }
                )
                for d in good.answers
            ),
        }
    )
    unsupported = unsupported.seal()
    assert (
        admit(unsupported, snapshot, unsupported.producer)[1]
        == "target_python_unsupported"
    )
    unavailable = good.model_copy(update={"availability": "unavailable"}).seal()
    assert admit(unavailable, snapshot, backend())[1] == "backend_unavailable"
    known = snapshot.model_copy(update={"environment": (("dependencies", "none"),)})
    good_known = Records(known, root, backend(), data).finish()
    assert admit(good_known, known, backend())[1] == "experimental_admitted"
    assert env["producer"] == unsupported.producer.id


@pytest.mark.parametrize(
    "mutation", ["envelope", "duplicate", "coverage", "target", "owner", "path", "raw"]
)
def test_generation_cross_record_invariants(tmp_path, mutation):
    snapshot, root, data = view(tmp_path)
    rec = Records(snapshot, root, backend(), data)
    g = rec.finish()
    if mutation == "envelope":
        g = g.model_copy(
            update={
                "answers": (
                    g.answers[0].model_copy(
                        update={
                            "envelope": g.answers[0].envelope.model_copy(
                                update={"snapshot": "different"}
                            )
                        }
                    ),
                    *g.answers[1:],
                )
            }
        )
    elif mutation == "duplicate":
        g = g.model_copy(update={"definitions": (g.definitions[0], *g.definitions)})
    elif mutation == "coverage":
        g = g.model_copy(update={"answers": g.answers[1:]})
    elif mutation == "target":
        g = g.model_copy(
            update={
                "answers": (
                    g.answers[0].model_copy(
                        update={
                            "outcome": "candidates",
                            "completeness": "open",
                            "candidates": (
                                Candidate(
                                    target="missing",
                                    dispatch="invoke",
                                    semantics="implementation",
                                    method="name_guess",
                                ),
                            ),
                        }
                    ),
                    *g.answers[1:],
                )
            }
        )
    elif mutation in ("owner", "path"):
        g = g.model_copy(
            update={
                "definitions": (
                    g.definitions[0].model_copy(update={mutation: "missing"}),
                    *g.definitions[1:],
                )
            }
        )
    else:
        row = MappingRow(raw_id="x", first_reason="bad", stage="coordinate")
        g = g.model_copy(update={"mapping": (row, row)})
    with pytest.raises(ValueError):
        g.seal()


class FailingPeer:
    def __init__(self, result=None, failure=None, at="initialize"):
        self.result, self.failure, self.at = result, failure, at

    def request(self, method, params, timeout=30):
        if method == self.at and self.failure:
            raise self.failure
        if method == "initialize":
            return (
                self.result
                if self.result is not None
                else {"capabilities": {"callHierarchyProvider": True}}
            )
        return []

    def notify(self, method, params):
        pass


@pytest.mark.parametrize(
    "mode",
    ["timeout", "error", "empty", "unsupported", "bad_encoding", "prepare_error"],
)
def test_lsp_unknown_and_unavailable_are_not_closed_negative(tmp_path, mode):
    from arcgraph.semantic_prototype.lsp import LspError

    snapshot, root, data = view(tmp_path)
    peer = FailingPeer(
        failure=(
            TimeoutError()
            if mode == "timeout"
            else LspError("error") if mode in ("error", "prepare_error") else None
        ),
        at=(
            "textDocument/prepareCallHierarchy"
            if mode == "prepare_error"
            else "initialize"
        ),
        result=(
            {"capabilities": {"callHierarchyProvider": False}}
            if mode == "unsupported"
            else (
                {
                    "capabilities": {
                        "callHierarchyProvider": True,
                        "positionEncoding": "utf-32",
                    }
                }
                if mode == "bad_encoding"
                else None
            )
        ),
    )
    generation, meta = pyright_records(snapshot, root, backend(), data, peer)
    assert {a.outcome for a in generation.answers} == {"unknown"}
    assert generation.complete
    assert generation.availability == (
        "partial"
        if mode == "prepare_error"
        else "available" if mode == "empty" else "unavailable"
    )
    if mode == "timeout":
        assert all(a.reasons == ("timeout",) for a in generation.answers)
    if mode == "empty":
        assert meta["requests"]["empty"] == 3


def policy_fixture(tmp_path):
    source = "from functools import lru_cache\nfrom typing import Protocol\nclass Interface(Protocol):\n    def t(self): ...\nclass UploadFile:\n    def seek(self): pass\n@lru_cache()\ndef cached(): return 1\ndef f(): pass\ndef h(): pass\ndef g(file: anyio.AsyncFile):\n    cached(); file.seek(); f(); f()\n"
    snapshot, root, data = view(tmp_path, source)
    primary = Records(snapshot, root, backend(), data)
    fallback = Records(snapshot, root, backend("arcgraph"), data)
    defs = {d.qualname: d for d in primary.definitions}
    sites = primary.callsites[-4:]
    return primary, fallback, defs, sites


def test_decorator_protocol_and_receiver_annotation_policy_fixture(tmp_path):
    primary, fallback, defs, sites = policy_fixture(tmp_path)
    primary.candidate(sites[0], defs["demo.cached"], "type_inference", "wrapped")
    primary.candidate(sites[2], defs["demo.Interface.t"], "type_inference", "abstract")
    fallback.candidate(sites[1], defs["demo.UploadFile.seek"], "name_guess", "receiver")
    p, f = primary.finish(), fallback.finish()
    assert p.answers[-4].candidates[0].semantics == "logical_body"
    assert "wrapper_execution_unknown" in p.answers[-4].candidates[0].evidence
    assert p.answers[-2].candidates[0].semantics == "declaration"
    assert f.answers[-3].candidates[0].receiver_relation == "unverified"
    report = project(p, f)
    assert len(report["edges"]) == 1
    assert {r["reason"] for r in report["suppressed"]} == {
        "declaration_not_execution_target",
        "receiver_relation_unverified",
    }


def test_primary_positive_blocks_guess_and_dedupes_supports_without_polymorphic_conflict(
    tmp_path,
):
    primary, fallback, defs, sites = policy_fixture(tmp_path)
    for target in ("demo.f", "demo.h"):
        primary.candidate(sites[2], defs[target], "type_inference", target)
    fallback.candidate(sites[2], defs["demo.cached"], "name_guess", "guessed")
    fallback.candidate(sites[2], defs["demo.f"], "lexical_binding", "direct")
    fallback.candidate(sites[3], defs["demo.f"], "lexical_binding", "second")
    report = project(primary.finish(), fallback.finish())
    assert len(report["edges"]) == 3
    shared = next(
        e for e in report["edges"] if e["key"][:2] == (sites[2].id, defs["demo.f"].id)
    )
    assert len(shared["supports"]) == 2
    assert report["suppressed"][0]["reason"] == "primary_positive_blocks_name_guess"
    assert report["disagreements"][0]["classification"] == "open_set_disagreement"
    assert len(report["removed_calls"]) == 1
    assert len(report["added_calls"]) == 1
    bad = fallback.finish().model_copy(update={"complete": False})
    with pytest.raises(ValueError):
        project(primary.finish(), bad)
    with pytest.raises(ValueError, match="structure_subject"):
        project(
            primary.finish(),
            fallback.finish()
            .model_copy(update={"callsites": (), "answers": ()})
            .seal(),
        )


def test_wrap_current_graph_preserves_repeated_and_none_call_roles(tmp_path):
    snapshot, root, data = view(tmp_path, "def f(): pass\ndef g(): f(); f()\n")
    raw = {
        "nodes": [
            {
                "id": "f",
                "kind": "function",
                "path": "demo.py",
                "qualname": "demo.f",
                "start_line": 1,
            },
            {
                "id": "g",
                "kind": "function",
                "path": "demo.py",
                "qualname": "demo.g",
                "start_line": 2,
            },
        ],
        "edges": [],
    }
    for role in (None, "call"):
        raw["edges"].append(
            {
                "source": "g",
                "target": "f",
                "kind": "calls",
                "confidence": "confirmed",
                "semantic_role": role,
                "resolution": {"strategy": "lexical_binding"},
                "properties": {
                    "callsites": [{"line": 2, "column": 9}, {"line": 2, "column": 14}]
                },
            }
        )
    f = wrap_graph(snapshot, root, backend("arcgraph"), data, raw)
    p = Records(snapshot, root, backend(), data).finish()
    report = project(p, f)
    assert len(report["edges"]) == 2
    assert len(f.mapping) == 4
    assert len(report["edges"][0]["supports"]) == 1
    generation, graph = arcgraph_records(snapshot, root, backend("arcgraph"), data)
    assert graph["edges"]
    assert len([a for a in generation.answers if a.outcome == "candidates"]) == 2
    assert not (root / "output").exists()


def test_stdio_recorded_replay_exercises_configuration_and_shutdown(tmp_path):
    import os
    import sys
    from arcgraph.semantic_prototype.lsp import Client

    snapshot, root, data = view(tmp_path)
    with Client(
        [sys.executable, str(FIXTURE / "replay-stdio.txt"), "replay"],
        root,
        dict(os.environ),
        {},
    ) as peer:
        generation, meta = pyright_records(snapshot, root, backend(), data, peer)
    assert meta["requests"]["error"] == 0
    assert sum(len(a.candidates) for a in generation.answers) == 3
    assert peer.process.returncode == 0
    assert any(r["message"].get("method") == "shutdown" for r in peer.transcript)


@pytest.mark.parametrize(
    "mode",
    [
        "config",
        "error",
        "bad_length",
        "missing_length",
        "truncated",
        "malformed",
        "eof",
        "timeout",
        "stubborn",
    ],
)
def test_stdio_transport_bounds_and_errors(tmp_path, mode):
    import os
    import sys
    from arcgraph.semantic_prototype.lsp import Client, LspError

    with Client(
        [sys.executable, str(FIXTURE / "replay-stdio.txt"), mode],
        tmp_path,
        dict(os.environ),
        {"python": {"analysis": {"x": 1}}},
    ) as peer:
        if mode == "config":
            assert peer.request("probe", {}, timeout=2) == "ok"
        else:
            with pytest.raises(
                TimeoutError if mode in ("timeout", "stubborn") else LspError
            ):
                peer.request("probe", {}, timeout=0.1)
    assert peer.process.poll() is not None


def test_mapping_failures_and_descriptor_candidates_are_preserved(tmp_path):
    snapshot, root, data = view(
        tmp_path,
        "class C:\n    @property\n    def value(self): return lambda: 1\n    def g(self): self.value()\n",
    )
    rec = Records(snapshot, root, backend(), data)
    defs = {d.name: d for d in rec.definitions}
    rec.candidate(rec.callsites[-1], defs["value"], "type_inference", "descriptor")
    assert rec.finish().answers[-1].candidates[0].semantics == "descriptor_value"
    assert rec.path("https://example.com/x.py") is None
    assert rec.path("file://host/x.py") is None
    assert (
        rec.lookup(
            {"uri": (root / "missing.py").as_uri(), "name": "x", "selectionRange": {}},
            "utf-16",
        )
        is None
    )
    primary = rec.finish()
    fallback = Records(snapshot, root, backend("arcgraph"), data).finish()
    assert (
        project(primary, fallback)["suppressed"][0]["reason"]
        == "descriptor_value_not_execution_target"
    )


@pytest.mark.skipif(
    not __import__("os").environ.get("ARCGRAPH_TEST_PYRIGHT_COMMAND"),
    reason="explicit pyright command required",
)
def test_opt_in_pyright_integration(tmp_path):
    import os
    import shlex
    from arcgraph.semantic_prototype.lsp import Client

    snapshot, root, data = view(tmp_path)
    command = shlex.split(os.environ["ARCGRAPH_TEST_PYRIGHT_COMMAND"])
    with Client(command, root, dict(os.environ), {}) as peer:
        generation, meta = pyright_records(snapshot, root, backend(), data, peer)
    assert meta["encoding"] in ("utf-8", "utf-16")
    assert len([a for a in generation.answers if a.outcome == "candidates"]) == 3


@pytest.mark.parametrize("mutation", ["end", "owner", "external", "source_coordinate"])
def test_lsp_mapping_accounting_reports_first_loss(tmp_path, mutation):
    snapshot, root, data = view(tmp_path)
    peer = Replay(root)
    original = peer.request

    def changed(method, params, timeout=30):
        result = original(method, params, timeout)
        if method == "callHierarchy/outgoingCalls" and result:
            for entry in result:
                if mutation == "end":
                    for r in entry["fromRanges"]:
                        r["end"]["character"] += 1
                elif mutation == "owner":
                    # The return item claims an unrelated owner; only matching ranges survive coordinate mapping.
                    params["item"]["selectionRange"]["start"]["character"] += 1
                elif mutation == "external":
                    entry["to"]["uri"] = "file:///outside/module.py"
                elif mutation == "source_coordinate":
                    for r in entry["fromRanges"]:
                        r["start"]["line"] = 99999
        return result

    peer.request = changed
    if mutation == "owner":
        # Change prepare items before the wrapper derives source identity.
        def changed_owner(method, params, timeout=30):
            result = original(method, params, timeout)
            if method == "textDocument/prepareCallHierarchy" and result:
                result[0]["name"] = "not_owner"
            return result

        # Replay outgoing parameter comparison is intentionally bypassed for forged native owner.
        def owner_replay(method, params, timeout=30):
            if method == "callHierarchy/outgoingCalls":
                expected = peer.requests[peer.index]
                params = json.loads(
                    json.dumps(expected["params"]).replace(
                        "file:///FROZEN", root.as_uri()
                    )
                )
            return changed_owner(method, params, timeout)

        peer.request = owner_replay
    gen, _ = pyright_records(snapshot, root, backend(), data, peer)
    assert len(gen.mapping) == 4
    assert sum(1 for row in gen.mapping) == len(gen.mapping)
    wanted = {
        "end": "coordinate_no_unique_callsite",
        "owner": "owner_mismatch",
        "external": "symbol_missing_or_external",
        "source_coordinate": "coordinate_unconvertible",
    }[mutation]
    assert sum(row.first_reason == wanted for row in gen.mapping) >= 3
    assert not any(a.candidates for a in gen.answers)


@pytest.mark.parametrize(
    "start,end",
    [((0, 0), (0, 0)), ((0, 2), (0, 1)), ((-1, 0), (0, 2)), ((True, 0), (1, 2))],
)
def test_span_requires_nonempty_strict_integer_half_open_range(start, end):
    with pytest.raises(ValueError):
        Span(start=start, end=end)


def test_atomic_publish_failure_preserves_previous_artifact(tmp_path, monkeypatch):
    import os

    snapshot, root, data = view(tmp_path)
    gen = Records(snapshot, root, backend(), data).finish()
    path = tmp_path / "generation.json"
    write_generation(gen, path)
    old = path.read_bytes()

    def fail(*args):
        raise OSError("interrupted publication")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_generation(gen, path)
    assert path.read_bytes() == old
    assert not list(tmp_path.glob(".generation-*"))


def test_failed_mapping_and_missing_raw_unit_cannot_be_sealed(tmp_path):
    snapshot, root, data = view(tmp_path)
    rec = Records(snapshot, root, backend(), data)
    rec.candidate(rec.callsites[-1], rec.definitions[0], "type_inference", "native")
    gen = rec.finish()
    answers = list(gen.answers)
    answers[-1] = answers[-1].model_copy(
        update={
            "envelope": answers[-1].envelope.model_copy(update={"mapping": "failed"})
        }
    )
    with pytest.raises(ValueError, match="coordinate"):
        gen.model_copy(update={"answers": tuple(answers)}).seal()
    with pytest.raises(ValueError, match="corrupt"):
        gen.model_copy(update={"raw_count": 99}).seal()


def test_module_entry_produces_artifacts_without_graph_build(tmp_path, monkeypatch):
    from arcgraph.semantic_prototype import __main__ as entry

    source = tmp_path / "input"
    source.mkdir()
    (source / "demo.py").write_bytes((FIXTURE / "source.txt").read_bytes())
    files = tmp_path / "files.json"
    files.write_text('["demo.py"]', encoding="utf-8")
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text('{"version":"1.1.414"}', encoding="utf-8")

    class Peer(Replay):
        def __init__(self, command, root, env, configuration):
            super().__init__(root)
            self.transcript = []
            assert env["HOME"] != __import__("os").environ.get("HOME")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(entry, "Client", Peer)
    output = tmp_path / "result"
    args = [
        "--source",
        str(source),
        "--files",
        str(files),
        "--output",
        str(output),
        "--pyright-package",
        str(package),
        "--node",
        "node",
    ]
    assert entry.main(args) == 0
    assert read_generation(output / "pyright.json").complete
    assert json.loads((output / "shadow.json").read_text(encoding="utf-8"))["edges"]
    assert not (source / "output").exists()
    with pytest.raises(SystemExit):
        entry.main(args)
    (package / "package.json").write_text('{"version":"1.1.1"}', encoding="utf-8")
    with pytest.raises(SystemExit):
        entry.main(args)


def test_protocol_declaration_and_same_class_annotation_are_distinct(tmp_path):
    source = "from typing import Protocol\nclass Translator(Protocol):\n    def t(self): ...\nclass UploadFile:\n    def seek(self): pass\ndef use(translator: Translator, local: UploadFile, other: anyio.AsyncFile):\n    translator.t(); local.seek(); other.seek()\n"
    snapshot, root, data = view(tmp_path, source)
    primary = Records(snapshot, root, backend(), data)
    fallback = Records(snapshot, root, backend("arcgraph"), data)
    defs = {d.qualname: d for d in primary.definitions}
    primary.candidate(
        primary.callsites[0], defs["demo.Translator.t"], "type_inference", "protocol"
    )
    fallback.candidate(
        primary.callsites[1], defs["demo.UploadFile.seek"], "name_guess", "nominal"
    )
    fallback.candidate(
        primary.callsites[2], defs["demo.UploadFile.seek"], "name_guess", "unrelated"
    )
    gen = fallback.finish()
    assert gen.answers[1].candidates[0].receiver_relation == "nominal_match"
    assert gen.answers[2].candidates[0].receiver_relation == "unverified"
    report = project(primary.finish(), gen)
    assert len(report["edges"]) == 1
    assert report["edges"][0]["key"][0] == primary.callsites[1].id
    assert {x["reason"] for x in report["suppressed"]} == {
        "declaration_not_execution_target",
        "receiver_relation_unverified",
    }


def test_hot_session_does_not_reinitialize_or_reopen_documents(tmp_path):
    snapshot, root, data = view(tmp_path)
    peer = FailingPeer()
    initialized = {
        "capabilities": {"callHierarchyProvider": True, "positionEncoding": "utf-8"}
    }
    gen, meta = pyright_records(
        snapshot, root, backend(), data, peer, initialize_result=initialized
    )
    assert meta["requests"]["initialize"] == 0
    assert meta["encoding"] == "utf-8"
    assert gen.complete


def test_wrapped_parse_failure_is_partial_with_per_point_reason(tmp_path):
    snapshot, root, data = view(tmp_path)
    raw = {
        "nodes": [],
        "edges": [],
        "warnings": [
            {
                "kind": "parse_error",
                "path": "demo.py",
                "message": "host syntax unsupported",
            }
        ],
    }
    g = wrap_graph(snapshot, root, backend("arcgraph"), data, raw)
    assert g.availability == "partial"
    assert g.parse_failures == (("demo.py", "host syntax unsupported"),)
    assert all(a.reasons == ("structural_parser_unsupported",) for a in g.answers)


def test_snapshot_raw_newline_change_rejects_same_canonical_text(tmp_path):
    snapshot, root, _ = view(tmp_path, "def f(): pass\n")
    path = root / "demo.py"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError, match="changed"):
        verify(snapshot, root)


def test_wrap_chain_calls_with_shared_start_uses_expression_and_all_facts(tmp_path):
    snapshot, root, data = view(
        tmp_path, "def make(): pass\ndef run(): pass\ndef g(): make().run()\n"
    )
    inner = {
        "line": 3,
        "column": 9,
        "call_expression": "make()",
        "resolution_strategy": "lexical_binding",
    }
    outer = {
        "line": 3,
        "column": 9,
        "call_expression": "make().run()",
        "resolution_strategy": "unique_method_fallback",
    }
    raw = {
        "nodes": [
            {
                "id": n,
                "kind": "function",
                "path": "demo.py",
                "qualname": "demo." + n,
                "start_line": i + 1,
            }
            for i, n in enumerate(["make", "run", "g"])
        ],
        "edges": [
            {
                "source": "g",
                "target": "make",
                "kind": "calls",
                "confidence": "confirmed",
                "properties": {"callsite": inner, "callsites": [inner]},
            },
            {
                "source": "g",
                "target": "run",
                "kind": "calls",
                "confidence": "inferred",
                "properties": {"callsite": outer, "callsites": []},
            },
        ],
    }
    g = wrap_graph(snapshot, root, backend("arcgraph"), data, raw)
    assert len(g.mapping) == 2
    assert all(r.first_reason == "mapped_success" for r in g.mapping)
    assert {a.candidates[0].method for a in g.answers} == {
        "lexical_binding",
        "name_guess",
    }


@pytest.mark.parametrize(
    "root,uri,expected",
    [
        (PureWindowsPath("C:/frozen"), "file:///C:/frozen/demo.py", "demo.py"),
        (PureWindowsPath("C:/frozen"), "file://localhost/C:/frozen/demo.py", "demo.py"),
        (PureWindowsPath("C:/frozen"), "file:///c:/FROZEN/demo.py", "demo.py"),
        (
            PureWindowsPath("C:/frozen"),
            "file:///C:/frozen/%C3%A9%20%23%25.py",
            "é #%.py",
        ),
        (
            PureWindowsPath("//server/share/frozen"),
            "file://server/share/frozen/demo.py",
            "demo.py",
        ),
        (
            PureWindowsPath("//server/share/frozen"),
            "file:////server/share/frozen/demo.py",
            "demo.py",
        ),
        (PurePosixPath("/frozen"), "file:///frozen/%C3%A9%20%23%25.py", "é #%.py"),
        (PurePosixPath("/frozen"), "file://localhost/frozen/demo.py", "demo.py"),
        (PureWindowsPath("C:/frozen"), "file:///D:/frozen/demo.py", None),
        (PureWindowsPath("C:/frozen"), "file:///C:/frozen/../outside.py", None),
        (PureWindowsPath("C:/frozen"), "file:C:demo.py", None),
        (PureWindowsPath("C:/frozen"), "file:///C:/frozen/demo.py?x=1", None),
        (PureWindowsPath("C:/frozen"), "file:///C:/frozen/demo.py#other", None),
        (PureWindowsPath("C:/frozen"), "file://server/share/demo.py", None),
        (PurePosixPath("/frozen"), "file://server/frozen/demo.py", None),
        (PurePosixPath("/frozen"), "https://localhost/frozen/demo.py", None),
        (PurePosixPath("/frozen"), "file:demo.py", None),
    ],
)
def test_file_uri_mapping_uses_snapshot_path_flavour_on_every_host(root, uri, expected):
    records = object.__new__(Records)
    records.root = root
    assert records.path(uri) == expected


@pytest.mark.parametrize(
    "root",
    [
        PureWindowsPath("C:/frozen"),
        PureWindowsPath("//server/share/frozen"),
        PurePosixPath("/frozen"),
    ],
)
def test_file_uri_roundtrip_keeps_percent_unicode_and_space(root):
    records = object.__new__(Records)
    records.root = root
    assert records.path((root / "é #%.py").as_uri()) == "é #%.py"


def test_file_uri_mapping_resolves_native_symlinks_before_containment(tmp_path):
    root = tmp_path / "frozen"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"pass\n")
    (root / "escape.py").symlink_to(outside)
    records = object.__new__(Records)
    records.root = root.resolve()
    assert records.path((root / "escape.py").as_uri()) is None
