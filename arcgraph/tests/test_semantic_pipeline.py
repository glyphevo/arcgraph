"""Unified structure anchors, independent generations, and shadow policy rules."""

import json
from pathlib import Path
import sys

import pytest

from arcgraph.semantic_prototype import pipeline, structure_provider
from arcgraph.semantic_prototype.anchors import AnchoredRecords, BackendRun, View, admit
from arcgraph.semantic_prototype.backends import producer, pyright_records
from arcgraph.semantic_prototype.contract import (
    Candidate,
    Span,
    digest,
    write_generation,
)
from arcgraph.semantic_prototype.json_stream import content_digest, chunks
from arcgraph.semantic_prototype.projection import project
from arcgraph.semantic_prototype.snapshot import capture
from arcgraph.tests.test_semantic_prototype import Replay

FIXTURE = Path(__file__).parent / "fixtures/semantic_prototype"
TARGET = f"{sys.version_info.major}.{sys.version_info.minor}"


def make_view(tmp_path, filename="policy-v2.txt", source=None):
    src = tmp_path / "source"
    src.mkdir()
    (src / "demo.py").write_text(
        source or (FIXTURE / filename).read_text(encoding="utf-8"), encoding="utf-8"
    )
    root = tmp_path / "frozen"
    (src / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    snapshot = capture(
        src,
        ["demo.py", "pyrightconfig.json"],
        root,
        target_python=TARGET,
        platform="Darwin",
    )
    bundle = structure_provider.analyze(
        snapshot, root, {TARGET: sys.executable}, access_facts=True
    )
    assert bundle.complete, bundle.reasons
    return snapshot, root, View(bundle)


def make_records(snapshot, root, view, name="pyright"):
    backend = producer(name, "test", (TARGET,), {})
    return AnchoredRecords(snapshot, root, backend, view)


def find(view, name):
    return next(d for d in view.definitions if d.qualname == "demo." + name)


def site(view, expression):
    return next(s for s in view.callsites if s.expression == expression)


def add(records, expression, name, method="type_inference"):
    s, d = site(records.view, expression), find(records.view, name)
    rid = str(len(records.mapping))
    records.mapped(rid, "mapped_success", "mapped", s, d)
    records.candidate(s, d, method, rid)


def project_records(main, fallback):
    return project(
        main.view, main.finish(), fallback.finish(), (main.backend, fallback.backend)
    )


def test_shared_structure_is_the_only_callsite_universe_and_accesses_are_separate(
    tmp_path,
):
    snapshot, root, view = make_view(tmp_path)
    main, fallback = make_records(snapshot, root, view), make_records(
        snapshot, root, view, "arcgraph"
    )
    assert main.callsites is fallback.callsites is view.callsites
    assert main.definitions is fallback.definitions is view.definitions
    assert {s.id for s in view.callsites} == {
        r.id for r in view.bundle.records if r.kind in ("callsite", "access")
    }
    assert view.records[site(view, "x.value").id].kind == "access"
    assert view.records[site(view, "x.run()").id].kind == "callsite"
    generation = main.finish()
    assert "callsites" not in generation.model_dump()
    assert generation.structure == view.bundle.id
    assert len(generation.answers) == len(view.callsites)
    assert all(
        a.outcome == "unknown" and a.reasons == ("no_answer",)
        for a in generation.answers
    )
    assert admit(generation, view, snapshot, main.backend)[0]


def test_gradual_receiver_candidates_are_retained_as_unverified_open_targets(tmp_path):
    snapshot, root, view = make_view(tmp_path)
    main, fallback = make_records(snapshot, root, view), make_records(
        snapshot, root, view, "arcgraph"
    )
    for expression in ("x.run()", "y.run()", "known.run()"):
        add(fallback, expression, "Concrete.run", "name_guess")
    report = project_records(main, fallback)
    assert {view.sites[k[0]].expression for k in report["projected_calls"]} == {
        "x.run()",
        "y.run()",
    }
    kept = [d for d in report["decisions"] if d["accepted"]]
    assert all(
        d["decision"] == "retain_gradual_receiver_unverified"
        and d["receiver_relation"] == "unverified"
        for d in kept
    )
    assert any(
        d["decision"] == "nominal_receiver_relation_unverified" and not d["accepted"]
        for d in report["decisions"]
    )


def test_primary_execution_answer_blocks_only_fallback_name_guesses(tmp_path):
    snapshot, root, view = make_view(tmp_path)
    main, fallback = make_records(snapshot, root, view), make_records(
        snapshot, root, view, "arcgraph"
    )
    add(main, "x.run()", "Concrete.run")
    add(fallback, "x.run()", "Concrete.second", "name_guess")
    add(fallback, "x.run()", "Concrete.wrapped", "lexical")
    add(fallback, "y.run()", "Concrete.run", "name_guess")
    add(main, "y.run()", "Contract.run")
    report = project_records(main, fallback)
    assert {
        (view.sites[s].expression, view.defs[t].name)
        for s, t, _, _ in report["projected_calls"]
    } == {("x.run()", "run"), ("x.run()", "wrapped"), ("y.run()", "run")}
    assert report["removed_calls"] == [
        (
            site(view, "x.run()").id,
            find(view, "Concrete.second").id,
            "invoke",
            "function_body",
        )
    ]
    assert any(
        d["decision"] == "primary_execution_blocks_name_guess"
        for d in report["decisions"]
    )


def test_declarations_properties_and_decorated_logical_bodies_retain_provenance(
    tmp_path,
):
    snapshot, root, view = make_view(tmp_path)
    main, fallback = make_records(snapshot, root, view), make_records(
        snapshot, root, view, "arcgraph"
    )
    for expression, target in (
        ("Contract.run(x)", "Contract.run"),
        ("Base.abstract(x)", "Base.abstract"),
        ("x.value", "Concrete.value"),
        ("x.wrapped()", "Concrete.wrapped"),
    ):
        add(main, expression, target)
        add(fallback, expression, target, "lexical")
    report = project_records(main, fallback)
    assert {e["relation"] for e in report["edges"]} == {
        "execution",
        "declaration_dependency",
        "property_access",
    }
    assert len(report["projected_calls"]) == 1
    assert view.sites[report["projected_calls"][0][0]].expression == "x.wrapped()"
    assert all(len(e["supports"]) == 2 for e in report["edges"])
    for e in report["edges"]:
        for support in e["supports"]:
            assert (
                support["generation"]
                and support["answer"]
                and support["producer"]
                and support["policy"]
            )
    wrapped = next(
        d
        for d in report["decisions"]
        if view.sites[d["key"][0]].expression == "x.wrapped()"
    )
    assert "wrapper_execution_unknown" in wrapped["evidence"]


def test_syntax_only_and_plain_member_references_do_not_become_execution_edges(
    tmp_path,
):
    snapshot, root, view = make_view(
        tmp_path,
        source="from __future__ import annotations\ndef g(): pass\ndef f(x: g()):\n    return x.g\n",
    )
    main, fallback = make_records(snapshot, root, view), make_records(
        snapshot, root, view, "arcgraph"
    )
    add(main, "g()", "g")
    add(main, "x.g", "g")
    report = project_records(main, fallback)
    assert not report["edges"]
    assert all(
        d["decision"] == "non_execution_syntax_or_access" for d in report["decisions"]
    )


def test_recorded_lsp_is_anchored_without_legacy_oracle(tmp_path, monkeypatch):
    snapshot, root, view = make_view(tmp_path, filename="source.txt")
    records = make_records(snapshot, root, view)
    result, meta = pyright_records(
        snapshot,
        root,
        records.backend,
        view,
        Replay(root),
        record_factory=AnchoredRecords,
    )
    assert meta["encoding"] == "utf-16"
    assert meta["requests"]["error"] == 0
    assert len([a for a in result.answers if a.candidates]) == 3
    assert admit(result, view, snapshot, records.backend)[0]
    out = tmp_path / "backend.json"
    write_generation(result, out)
    assert BackendRun.model_validate_json(out.read_bytes()).checked() == result


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("structure", "wrong", "structure_identity_mismatch"),
        ("structure_checksum", "wrong", "structure_identity_mismatch"),
        ("availability", "unavailable", "backend_unavailable"),
    ],
)
def test_backend_admission_rejects_identity_and_unavailability(
    tmp_path, field, value, reason
):
    snapshot, root, view = make_view(tmp_path)
    records = make_records(snapshot, root, view)
    result = records.finish().model_copy(update={field: value}).seal()
    assert admit(result, view, snapshot, records.backend) == (False, reason)


def test_backend_integrity_coordinate_bounds_and_stream_hash(tmp_path):
    snapshot, root, view = make_view(tmp_path)
    records = make_records(snapshot, root, view)
    result = records.finish()
    assert content_digest(view.bundle) == digest(
        view.bundle.model_dump(mode="json", exclude={"complete", "count", "checksum"})
    )
    assert json.loads("".join(chunks(view.bundle))) == view.bundle.model_dump(
        mode="json"
    )
    assert not admit(
        result.model_copy(update={"count": result.count + 1}),
        view,
        snapshot,
        records.backend,
    )[0]
    other = snapshot.model_copy(update={"platform": "other"})
    assert not admit(result, view, other, records.backend)[0]
    backend = records.backend.model_copy(update={"version": "changed"})
    assert not admit(result, view, snapshot, backend)[0]
    unsupported = records.backend.model_copy(update={"target_versions": ()})
    unsupported_records = AnchoredRecords(snapshot, root, unsupported, view)
    assert (
        admit(unsupported_records.finish(), view, snapshot, unsupported)[1]
        == "target_python_unsupported"
    )
    r = records.range(
        "demo.py",
        {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
        "utf-16",
    )
    assert r == Span(start=(0, 0), end=(0, 4))
    for line, col in ((-1, 0), (0, -1), (999, 0), (0, True)):
        with pytest.raises(ValueError):
            records.range(
                "demo.py",
                {
                    "start": {"line": line, "character": col},
                    "end": {"line": 0, "character": 4},
                },
                "utf-16",
            )


def test_owner_bridge_defaults_and_generator_scopes_are_explicit(tmp_path):
    snapshot, root, view = make_view(
        tmp_path,
        source="def g(): return 1\ndef f(x=g()):\n    return (g() for i in range(2))\n",
    )
    f, g = find(view, "f"), find(view, "g")
    defaults = next(s for s in view.callsites if s.phase == "definition_default")
    generator = next(s for s in view.callsites if s.phase == "deferred_generator")
    assert view.owner_matches(f, defaults)
    assert view.owner_matches(f, generator)
    assert not view.owner_matches(g, generator)
    assert not view.owner_matches(None, defaults)


def test_lsp_scope_failure_marks_bridged_defaults_and_generator_calls(tmp_path):
    snapshot, root, view = make_view(
        tmp_path,
        source="def g(): return 1\ndef f(x=g()):\n    return (g() for i in range(2))\n",
    )

    class Peer:
        def request(self, method, params, timeout=30):
            if method == "initialize":
                return {"capabilities": {"callHierarchyProvider": True}}
            raise TimeoutError("scope unavailable")

        def notify(self, method, params):
            pass

    backend = producer("pyright", "test", (TARGET,), {})
    run, meta = pyright_records(
        snapshot, root, backend, view, Peer(), record_factory=AnchoredRecords
    )
    bridged = {
        s.id
        for s in view.callsites
        if s.phase in ("definition_default", "deferred_generator")
    }
    assert bridged
    assert all(a.reasons == ("timeout",) for a in run.answers if a.subject in bridged)
    assert run.availability == "partial" and meta["requests"]["timeout"] == 2


def test_full_pipeline_reuses_existing_product_stages_without_graph_write(
    tmp_path, monkeypatch
):
    snapshot, root, view = make_view(tmp_path, filename="source.txt")
    monkeypatch.setattr(pipeline, "analyze", lambda *a, **kw: view.bundle)
    result = pipeline.run(
        snapshot,
        root,
        sys.executable,
        Replay(root),
        pyright_version="recorded",
        configuration={},
    )
    bundle, main, fallback, shadow, meta, raw = result
    assert bundle is view.bundle
    assert main.structure == fallback.structure == bundle.id
    assert set(meta["stages_seconds"]) == {
        "structure",
        "pyright",
        "arcgraph",
        "admission_projection",
    }
    assert raw["nodes"] and raw["edges"] and shadow["admission"][0]["allowed"]
    assert not (root / "output").exists()


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("empty", "available"),
        ("timeout", "partial"),
        ("error", "partial"),
        ("unsupported", "unavailable"),
    ],
)
def test_anchored_lsp_empty_timeout_error_and_unsupported_are_distinct(
    tmp_path, reply, expected
):
    from arcgraph.semantic_prototype.lsp import LspError

    snapshot, root, view = make_view(tmp_path, source="def g(): pass\ndef f(): g()\n")

    class Peer:
        def request(self, method, params, timeout=30):
            if method == "initialize":
                return {
                    "capabilities": (
                        {}
                        if reply == "unsupported"
                        else {"callHierarchyProvider": True}
                    )
                }
            if reply == "timeout":
                raise TimeoutError("fixture")
            if reply == "error":
                raise LspError("fixture")
            return []

        def notify(self, method, params):
            pass

    backend = producer("pyright", "test", (TARGET,), {})
    run, meta = pyright_records(
        snapshot, root, backend, view, Peer(), record_factory=AnchoredRecords
    )
    assert run.availability == expected
    assert all(a.outcome == "unknown" for a in run.answers)
    assert {a.subject for a in run.answers} == set(view.sites)
    if reply in ("timeout", "error"):
        assert next(
            a for a in run.answers if a.subject == site(view, "g()").id
        ).reasons == ("timeout" if reply == "timeout" else "tool_error",)
    if reply == "unsupported":
        assert all(a.reasons == ("capability_unsupported",) for a in run.answers)
    else:
        assert meta["encoding"] == "utf-16"


def test_nominal_match_and_absent_annotation_remain_open_candidates(tmp_path):
    snapshot, root, view = make_view(
        tmp_path,
        source="class A:\n    def run(self): pass\ndef f(x: A, y):\n    x.run()\n    y.run()\n",
    )
    main, fallback = make_records(snapshot, root, view), make_records(
        snapshot, root, view, "arcgraph"
    )
    add(fallback, "x.run()", "A.run", "name_guess")
    add(fallback, "y.run()", "A.run", "name_guess")
    assert len(project_records(main, fallback)["projected_calls"]) == 2
    assert (
        fallback.candidates[site(view, "x.run()").id][0].receiver_relation
        == "nominal_match"
    )


def test_unavailable_primary_allows_admitted_fallback_but_corrupt_identity_does_not(
    tmp_path,
):
    snapshot, root, view = make_view(tmp_path)
    main, fallback = make_records(snapshot, root, view), make_records(
        snapshot, root, view, "arcgraph"
    )
    add(fallback, "x.run()", "Concrete.run", "name_guess")
    absent = main.finish(availability="unavailable", failure="timeout")
    report = project(view, absent, fallback.finish(), (main.backend, fallback.backend))
    assert len(report["projected_calls"]) == 1
    assert report["admission"][0]["reason"] == "backend_unavailable"
    with pytest.raises(ValueError, match="structure_identity_mismatch"):
        project(
            view,
            absent.model_copy(update={"structure": "changed"}).seal(),
            fallback.finish(),
            (main.backend, fallback.backend),
        )


def test_streamed_validation_still_rejects_mutable_payloads_and_model_copy(tmp_path):
    snapshot, root, view = make_view(tmp_path)
    bundle = view.bundle
    r = next(r for r in bundle.records if r.kind == "callsite")
    corrupt_span = r.span.model_copy(update={"start": (-1, 0)})
    corrupt = bundle.model_copy(
        update={
            "records": tuple(
                x.model_copy(update={"span": corrupt_span}) if x.id == r.id else x
                for x in bundle.records
            )
        }
    )
    corrupt = corrupt.model_copy(update={"checksum": corrupt.content_digest()})
    with pytest.raises(ValueError):
        corrupt.checked()
    r.payload["token_span"] = [-1, 0, 0, 1]
    corrupt = bundle.model_copy(update={"checksum": bundle.content_digest()})
    with pytest.raises(ValueError):
        corrupt.checked()
    assert digest(
        json.loads("".join(chunks({"a": [1, "é", True, None], "b": {"z": 2}})))
    ) == digest({"a": [1, "é", True, None], "b": {"z": 2}})
    with pytest.raises(ValueError):
        list(chunks({1: "bad"}))
    with pytest.raises(ValueError):
        list(chunks(float("nan")))


def test_backend_checks_unmapped_answers_candidates_and_raw_identities(tmp_path):
    from arcgraph.semantic_prototype.contract import MappingRow

    snapshot, root, view = make_view(tmp_path)
    records = make_records(snapshot, root, view)
    result = records.finish()
    answer = result.answers[0]

    def change_answer(a):
        return result.model_copy(update={"answers": (a, *result.answers[1:])}).seal()

    assert (
        admit(
            change_answer(answer.model_copy(update={"subject": "missing"})),
            view,
            snapshot,
            records.backend,
        )[1]
        == "structure_subject_mismatch"
    )
    candidate = Candidate(
        target="missing",
        dispatch="invoke",
        semantics="implementation",
        method="fixture",
    )
    changed = answer.model_copy(
        update={
            "outcome": "candidates",
            "completeness": "open",
            "candidates": (candidate,),
            "reasons": (),
        }
    )
    assert (
        admit(change_answer(changed), view, snapshot, records.backend)[1]
        == "unmapped_candidate"
    )
    mapped = result.model_copy(
        update={
            "mapping": (
                MappingRow(
                    raw_id="one",
                    stage="coordinate",
                    first_reason="failure",
                    subject="missing",
                ),
            ),
            "raw_count": 1,
        }
    ).seal()
    assert (
        admit(mapped, view, snapshot, records.backend)[1] == "unmapped_mapping_identity"
    )
    with pytest.raises(ValueError, match="backend envelope"):
        change_answer(
            answer.model_copy(
                update={
                    "envelope": answer.envelope.model_copy(update={"snapshot": "wrong"})
                }
            )
        )
    with pytest.raises(ValueError, match="duplicate backend"):
        result.model_copy(update={"answers": (*result.answers, answer)}).seal()
    with pytest.raises(ValueError, match="duplicate raw"):
        result.model_copy(
            update={
                "mapping": (
                    MappingRow(
                        raw_id="one", stage="coordinate", first_reason="failure"
                    ),
                )
                * 2,
                "raw_count": 2,
            }
        ).seal()


def test_module_entry_writes_complete_records_and_streams_raw_dialogue(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    snapshot, root, view = make_view(tmp_path, filename="source.txt")
    (root / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    snap = tmp_path / "snapshot.json"
    snap.write_text(snapshot.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(pipeline, "analyze", lambda *a, **kw: view.bundle)
    peers = []

    class Peer(Replay):
        def __init__(self, command, cwd, env, configuration, transcript=None):
            super().__init__(cwd)
            self.process = SimpleNamespace(poll=lambda: 0)
            self.sink = transcript
            peers.append(self)

        def close(self):
            self.sink.append(
                {"direction": "fixture", "message": {"method": "shutdown"}}
            )

    monkeypatch.setattr(pipeline, "Client", Peer)
    out = tmp_path / "result"
    assert (
        pipeline.main(
            [
                "--snapshot",
                str(snap),
                "--root",
                str(root),
                "--interpreter",
                sys.executable,
                "--pyright",
                "fixture",
                "--pyright-version",
                "recorded",
                "--output",
                str(out),
            ]
        )
        == 0
    )
    assert BackendRun.model_validate_json((out / "pyright.json").read_bytes()).complete
    assert (
        json.loads((out / "projection.json").read_text(encoding="utf-8"))["policy"]
        == "shadow-open-targets/0.2"
    )
    assert (
        json.loads((out / "lsp.jsonl").read_text(encoding="utf-8"))["message"]["method"]
        == "shutdown"
    )


def test_pipeline_missing_target_interpreter_fails_before_backend(tmp_path):
    snapshot, root, view = make_view(tmp_path)
    with pytest.raises(ValueError):
        pipeline.run(
            snapshot,
            root,
            "/unavailable/interpreter",
            Replay(root),
            pyright_version="test",
            configuration={},
        )


def test_independent_backend_snapshot_and_raw_count_guards(tmp_path):
    snapshot, root, view = make_view(tmp_path)
    records = make_records(snapshot, root, view)
    run = records.finish()
    other = snapshot.model_copy(update={"platform": "other"})
    changed = run.model_copy(
        update={
            "snapshot": other,
            "answers": tuple(
                a.model_copy(
                    update={
                        "envelope": a.envelope.model_copy(update={"snapshot": other.id})
                    }
                )
                for a in run.answers
            ),
        }
    ).seal()
    assert (
        admit(changed, view, snapshot, records.backend)[1]
        == "backend_identity_mismatch"
    )
    with pytest.raises(ValueError, match="incomplete or corrupt backend"):
        run.model_copy(update={"raw_count": 1}).seal()


def test_independent_structural_envelope_and_jsonl_count_guards(tmp_path, monkeypatch):
    from types import SimpleNamespace

    snapshot, root, view = make_view(tmp_path)
    bundle = view.bundle
    record = bundle.records[0]
    corrupt = bundle.model_copy(
        update={
            "records": (
                record.model_copy(
                    update={
                        "envelope": record.envelope.model_copy(
                            update={"producer": "changed"}
                        )
                    }
                ),
                *bundle.records[1:],
            )
        }
    )
    corrupt = corrupt.model_copy(update={"checksum": corrupt.content_digest()})
    with pytest.raises(ValueError, match="structural envelope"):
        corrupt.checked()
    data = {
        "wire_format": "jsonl/1",
        "profile": "structure-facts/0.1",
        "interpreter": dict(bundle.producer.interpreter),
        "files": [f.model_dump(mode="json") for f in bundle.files],
        "record_count": 1,
    }
    monkeypatch.setattr(
        structure_provider.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            stdout=json.dumps(data, separators=(",", ":")) + "\n"
        ),
    )
    incomplete = structure_provider.analyze(snapshot, root, {TARGET: "recorded"})
    assert not incomplete.complete and incomplete.reasons == (
        "invalid_structural_records",
    )


def test_property_primary_does_not_block_a_fallback_execution_candidate(tmp_path):
    snapshot, root, view = make_view(tmp_path)
    main, fallback = make_records(snapshot, root, view), make_records(
        snapshot, root, view, "arcgraph"
    )
    add(main, "x.run()", "Concrete.value")
    add(fallback, "x.run()", "Concrete.run", "name_guess")
    report = project_records(main, fallback)
    assert len(report["projected_calls"]) == 1
    assert {e["relation"] for e in report["edges"]} == {"execution", "property_access"}


def test_unannotated_method_guess_is_explicitly_unverified(tmp_path):
    snapshot, root, view = make_view(
        tmp_path,
        source="class A:\n    def run(self): pass\ndef f(value):\n    value.run()\n",
    )
    records = make_records(snapshot, root, view, "arcgraph")
    add(records, "value.run()", "A.run", "name_guess")
    candidate = records.finish().answers[0].candidates[0]
    assert candidate.receiver_relation == "unverified"
    assert candidate.confidence == "uncalibrated"


@pytest.mark.parametrize(
    "mode,reason",
    [("missing-config", "frozen pyright configuration"), ("bad-heap", "at least 128")],
)
def test_module_requires_frozen_configuration_and_a_valid_heap_limit(
    tmp_path, mode, reason
):
    snapshot, root, view = make_view(tmp_path)
    if mode == "missing-config":
        snapshot = snapshot.model_copy(
            update={
                "files": tuple(
                    f for f in snapshot.files if f.path != "pyrightconfig.json"
                )
            }
        )
    snap = tmp_path / "snapshot.json"
    snap.write_text(snapshot.model_dump_json(), encoding="utf-8")
    args = [
        "--snapshot",
        str(snap),
        "--root",
        str(root),
        "--interpreter",
        sys.executable,
        "--pyright",
        "unused",
        "--pyright-version",
        "test",
        "--output",
        str(tmp_path / "out"),
    ]
    if mode == "bad-heap":
        args += ["--pyright-heap-mib", "64"]
    with pytest.raises(ValueError, match=reason):
        pipeline.main(args)
