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
                "--in-process",
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
        == "shadow-open-targets/0.3"
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


def test_overridden_stub_methods_are_declarations_but_noops_are_execution(tmp_path):
    snapshot, root, view = make_view(tmp_path, filename="policy-v3.txt")
    main = make_records(snapshot, root, view)
    fallback = make_records(snapshot, root, view, "arcgraph")
    stubs = ("raises", "empty", "documented", "ellipsis")
    for name in (*stubs, "effect", "unoverridden"):
        add(main, f"x.{name}()", f"Base.{name}")
    # A real override remains an execution target alongside its base declaration.
    add(main, "x.raises()", "Concrete.raises")
    generation = main.finish()
    assert sum(len(a.candidates) for a in generation.answers) == 7
    report = project(
        view, generation, fallback.finish(), (main.backend, fallback.backend)
    )
    dependencies = {
        view.defs[e["key"][1]].qualname: e
        for e in report["edges"]
        if e["relation"] == "declaration_dependency"
    }
    assert set(dependencies) == {f"demo.Base.{name}" for name in stubs}
    for edge in dependencies.values():
        support = edge["supports"][0]
        assert support["decision"] == "retain_declaration_dependency:overridden_stub"
        assert support["evidence"][-2] == "overridden_stub"
        assert view.defs[support["evidence"][-1]].owner == find(view, "Concrete").id
    assert {view.defs[k[1]].qualname for k in report["projected_calls"]} == {
        "demo.Base.effect",
        "demo.Base.unoverridden",
        "demo.Concrete.raises",
    }


def test_typing_only_mixin_members_keep_dependency_and_runtime_candidate(tmp_path):
    snapshot, root, view = make_view(tmp_path, filename="policy-v3.txt")
    main = make_records(snapshot, root, view)
    fallback = make_records(snapshot, root, view, "arcgraph")
    add(main, "self.declared()", "RuntimeMixin.declared")
    add(main, "self.second()", "RuntimeMixin.second")
    add(main, "self.runtime()", "RuntimeMixin.runtime")
    add(fallback, "self.declared()", "RuntimeImplementation.declared", "name_guess")
    report = project_records(main, fallback)
    assert {view.defs[k[1]].qualname for k in report["projected_calls"]} == {
        "demo.RuntimeImplementation.declared",
        "demo.RuntimeMixin.runtime",
    }
    dependencies = [
        e for e in report["edges"] if e["relation"] == "declaration_dependency"
    ]
    assert len(dependencies) == 2
    assert all(
        e["supports"][0]["decision"]
        == "retain_declaration_dependency:typing_only_definition"
        and "typing_only_definition" in e["supports"][0]["evidence"]
        for e in dependencies
    )
    assert all(a.outcome == "candidates" for a in main.finish().answers if a.candidates)


@pytest.mark.parametrize(
    "import_line,base",
    [
        ("from demo import Base as Imported", "Imported"),
        ("from .demo import Base as Imported", "Imported"),
        ("import demo as imported", "imported.Base"),
        ("import demo", "demo.Base[int]"),
    ],
)
def test_stub_override_can_follow_unambiguous_import_aliases(
    tmp_path, import_line, base
):
    source = (
        f"{import_line}\nclass Base:\n    def f(self): ...\n"
        f"class Child({base}):\n    def f(self): return 1\n"
    )
    _, _, view = make_view(tmp_path, source=source)
    target = find(view, "Base.f").id
    assert view.declaration_evidence[target] == (
        "overridden_stub",
        find(view, "Child.f").id,
    )


@pytest.mark.parametrize(
    "base,extra,override",
    [
        ("unknown()", "", "return 1"),
        ("Missing", "", "return 1"),
        ("Base", "", "..."),
        ("Base", "", "@abstractmethod\n    def f(self): return 1"),
        (
            "Imported",
            "from other import Base as Imported\nfrom another import Base as Imported\n",
            "return 1",
        ),
        (
            "Base",
            "if TYPE_CHECKING:\n    class Child(Base):\n        def f(self): return 1\n",
            None,
        ),
        ("Base", "", None),
    ],
)
def test_unproven_or_declarative_override_does_not_demote_runtime_noop(
    tmp_path, base, extra, override
):
    source = (
        "from typing import TYPE_CHECKING\nclass Base:\n    def f(self): pass\n" + extra
    )
    if override is not None:
        if override.startswith("@"):
            source += f"class Child({base}):\n    {override}\n"
        else:
            source += f"class Child({base}):\n    def f(self): {override}\n"
    _, _, view = make_view(tmp_path, source=source)
    assert find(view, "Base.f").id not in view.declaration_evidence


def test_ambiguous_base_names_do_not_select_the_first_definition(tmp_path):
    _, _, view = make_view(tmp_path, filename="ambiguous-bases.txt")
    bases = [d for d in view.definitions if d.qualname == "demo.Base"]
    methods = [d for d in view.definitions if d.qualname == "demo.Base.f"]
    assert len({d.id for d in bases}) == 2
    assert len({d.id for d in methods}) == 2
    assert all(view.records[d.id].payload["stub_body"] for d in methods)
    assert view.records[find(view, "Child.f").id].payload["stub_body"] is None
    # Both conditional definitions are possible. An arbitrary first match
    # cannot establish an override relationship with either base method.
    assert not {d.id for d in methods}.intersection(view.declaration_evidence)


def staged_case(tmp_path):
    from types import SimpleNamespace

    snapshot, root, view = make_view(tmp_path, filename="source.txt")
    path = tmp_path / "snapshot.json"
    path.write_text(snapshot.model_dump_json(), encoding="utf-8")
    out = tmp_path / "staged"
    out.mkdir()
    cache = out / ".stage-cache"
    cache.mkdir()
    return (
        SimpleNamespace(
            snapshot=path,
            root=root,
            output=out,
            interpreter=sys.executable,
            pyright="fixture",
            pyright_version="1.1.414",
            pyright_heap_mib=None,
        ),
        cache,
        view,
    )


class StagedReplay(Replay):
    def __init__(self, command, cwd, environment, configuration, transcript):
        super().__init__(cwd)
        self.stderr = []
        self.transcript = transcript

    def request(self, method, params, timeout=30):
        result = super().request(method, params, timeout)
        self.transcript.append(
            {"direction": "fixture", "method": method, "result": result}
        )
        return result

    def close(self):
        self.transcript.append({"direction": "fixture", "method": "shutdown"})


def test_staged_records_and_checked_disk_view_preserve_complete_generation(tmp_path):
    from arcgraph.semantic_prototype import staged
    from arcgraph.semantic_prototype.disk_structure import read

    args, cache, original = staged_case(tmp_path)
    staged.structure_stage(args, cache)
    eager = staged.read_structure_cache(cache / "structure.jsonl")
    lazy = read(cache / "structure.jsonl")
    assert lazy.content_digest() == eager.content_digest()
    assert list(lazy.records) == list(eager.records)
    assert lazy.records[-1] == eager.records[-1]
    assert lazy.records[:2] == eager.records[:2]
    assert len(lazy.records.by_id) == eager.count
    assert set(lazy.records.by_id) == {r.id for r in eager.records}
    assert lazy.records.by_id[eager.records[0].id] == eager.records[0]
    with pytest.raises(KeyError):
        lazy.records.by_id["absent"]
    encoded = tmp_path / "disk-generation.json"
    write_generation(lazy, encoded)
    assert encoded.read_bytes() == (args.output / "structure.json").read_bytes()
    staged.transport_stage(args, cache, StagedReplay)
    staged.graph_stage(args, cache)
    staged.map_stage(args, cache)
    primary = BackendRun.model_validate_json(
        (args.output / "pyright.json").read_bytes()
    )
    assert primary.complete and primary.raw_count == 4
    assert sum(len(a.candidates) for a in primary.answers) == 3
    projected = staged.load(args.output / "projection.json")
    assert len(projected["projected_calls"]) == 3
    assert len(primary.answers) == len(original.callsites)
    assert staged.load(cache / "mapping.json")["pyright"]["requests"]["error"] == 0
    # Every existing field and candidate remains present after string sharing.
    value = {"same": [{"value": "é😀"}, {"value": "é😀"}], "number": 4}
    before = json.loads(json.dumps(value))
    assert staged.intern_keys(value) is value and value == before
    assert value["same"][0]["value"] is value["same"][1]["value"]


def test_disk_structure_rejects_truncation_duplicates_and_payload_corruption(tmp_path):
    from arcgraph.semantic_prototype import staged
    from arcgraph.semantic_prototype.disk_structure import read

    _, cache, view = staged_case(tmp_path)
    file = cache / "structure.jsonl"
    staged.write_structure_cache(view.bundle, file)
    original = file.read_bytes()
    lines = original.splitlines(keepends=True)
    for broken in (b"".join(lines[:-1]), original + lines[1]):
        file.write_bytes(broken)
        with pytest.raises(ValueError):
            read(file)
    file.write_bytes(original)
    lazy = read(file)
    row = json.loads(lines[1])
    row["payload"]["name"] = "changed"
    lines[1] = (json.dumps(row) + "\n").encode()
    file.write_bytes(b"".join(lines))
    with pytest.raises(ValueError, match="corrupt"):
        lazy.checked()


def write_test_journal(path, snapshot_id, rows, footer=None, extra=""):
    import hashlib
    from arcgraph.semantic_prototype import staged

    checksum = hashlib.sha256()
    with path.open("w", encoding="utf-8") as handle:
        staged.wire_line(handle, {"snapshot": snapshot_id})
        for row in rows:
            checksum.update(staged.wire_line(handle, row))
        staged.wire_line(
            handle,
            footer
            or {"complete": True, "count": len(rows), "checksum": checksum.hexdigest()},
        )
        handle.write(extra)


@pytest.mark.parametrize("reason", ["timeout", "tool_error"])
def test_journal_errors_preserve_unknown_reason_and_checked_completion(
    tmp_path, reason
):
    from arcgraph.semantic_prototype.staged import JournalReplay
    from arcgraph.semantic_prototype.lsp import LspError

    file = tmp_path / "responses.jsonl"
    write_test_journal(
        file,
        "snapshot",
        [
            {
                "method": "example",
                "params": {},
                "error": {"kind": reason, "message": "fixture"},
            }
        ],
    )
    peer = JournalReplay(file, "snapshot")
    with pytest.raises(TimeoutError if reason == "timeout" else LspError):
        peer.request("example", {})
    peer.close()
    assert peer.handle.closed


@pytest.mark.parametrize(
    "broken",
    ["count", "digest", "complete", "extra", "snapshot", "request", "truncated"],
)
def test_journal_rejects_mismatched_or_incomplete_evidence(tmp_path, broken):
    from arcgraph.semantic_prototype.staged import JournalReplay

    path = tmp_path / "responses.jsonl"
    rows = [{"method": "example", "params": {"position": 1}, "result": []}]
    write_test_journal(path, "snapshot", rows)
    text = path.read_text(encoding="utf-8").splitlines()
    if broken in ("count", "digest", "complete"):
        footer = json.loads(text[-1])
        key = {"count": "count", "digest": "checksum", "complete": "complete"}[broken]
        footer[key] = {"count": 99, "digest": "wrong", "complete": False}[broken]
        text[-1] = json.dumps(footer)
    if broken == "extra":
        text.append("{}")
    if broken == "truncated":
        text = text[:-1]
    path.write_text("\n".join(text) + "\n", encoding="utf-8")
    if broken == "snapshot":
        with pytest.raises(ValueError):
            JournalReplay(path, "different")
        return
    peer = JournalReplay(path, "snapshot")
    try:
        if broken == "request":
            with pytest.raises(ValueError):
                peer.request("different", {})
        else:
            assert peer.request("example", {"position": 1}) == []
            with pytest.raises((ValueError, StopIteration)):
                peer.close()
    finally:
        peer.handle.close()


@pytest.mark.parametrize("failure", ["empty", "unsupported", "timeout", "tool_error"])
def test_staged_transport_preserves_empty_unsupported_and_failed_scope_answers(
    tmp_path, failure
):
    from arcgraph.semantic_prototype import staged

    args, cache, view = staged_case(tmp_path)
    staged.structure_stage(args, cache)

    class Peer:
        def __init__(self, *a, **kw):
            self.stderr = []

        def request(self, method, params, timeout=30):
            if failure == "timeout":
                raise TimeoutError("fixture")
            if failure == "tool_error":
                raise OSError("fixture")
            if method == "initialize":
                return {
                    "capabilities": {"callHierarchyProvider": failure != "unsupported"}
                }
            return []

        def notify(self, *a):
            pass

        def close(self):
            pass

    staged.transport_stage(args, cache, Peer)
    staged.graph_stage(args, cache)
    staged.map_stage(args, cache)
    generation = BackendRun.model_validate_json(
        (args.output / "pyright.json").read_bytes()
    )
    assert generation.complete
    assert not any(a.candidates for a in generation.answers)
    assert generation.availability == (
        "available" if failure == "empty" else "unavailable"
    )
    assert set(a.reasons for a in generation.answers) == {
        (
            {
                "empty": "no_answer",
                "unsupported": "capability_unsupported",
                "timeout": "timeout",
                "tool_error": "tool_error",
            }[failure],
        )
    }
    assert len(generation.answers) == len(view.callsites)


@pytest.mark.parametrize("changed_encoding", [False, True])
def test_transport_restarts_keep_all_documents_and_queries_and_encoding(
    tmp_path, monkeypatch, changed_encoding
):
    from arcgraph.semantic_prototype import staged

    args, cache, view = staged_case(tmp_path)
    staged.structure_stage(args, cache)
    monkeypatch.setattr(staged, "SESSION_DEFINITIONS", 1)
    peers = []

    class Peer:
        def __init__(self, *a, **kw):
            self.stderr = []
            self.notifications = []
            self.queries = []
            self.closed = False
            peers.append(self)

        def request(self, method, params, timeout=30):
            self.queries.append(method)
            if method == "initialize":
                return {
                    "capabilities": {
                        "callHierarchyProvider": True,
                        **(
                            {"positionEncoding": "utf-8"}
                            if changed_encoding and len(peers) > 1
                            else {}
                        ),
                    }
                }
            return []

        def notify(self, method, params):
            self.notifications.append(method)

        def close(self):
            self.closed = True

    if changed_encoding:
        with pytest.raises(ValueError, match="capabilities changed"):
            staged.transport_stage(args, cache, Peer)
        assert not (cache / "responses.jsonl").exists()
    else:
        staged.transport_stage(args, cache, Peer)
        assert len(peers) == 3
        assert (
            sum(p.queries.count("textDocument/prepareCallHierarchy") for p in peers)
            == 3
        )
        assert all(
            p.notifications == ["initialized", "textDocument/didOpen"] for p in peers
        )
        staged.graph_stage(args, cache)
        staged.map_stage(args, cache)
        assert (
            staged.load(cache / "mapping.json")["pyright"]["requests"]["prepare"] == 3
        )
    assert all(p.closed for p in peers)


def test_transport_rejects_changed_frozen_source_before_starting_server(tmp_path):
    from arcgraph.semantic_prototype import staged

    args, cache, _ = staged_case(tmp_path)
    staged.structure_stage(args, cache)
    (args.root / "demo.py").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen source changed"):
        staged.transport_stage(args, cache, StagedReplay)
    assert not (cache / "responses.jsonl").exists()


def staged_arguments(args, output=None):
    return [
        "--snapshot",
        str(args.snapshot),
        "--root",
        str(args.root),
        "--interpreter",
        args.interpreter,
        "--pyright",
        args.pyright,
        "--pyright-version",
        args.pyright_version,
        "--output",
        str(output or args.output),
    ]


def test_pipeline_default_replaces_heavy_parent_before_starting_transport(
    tmp_path, monkeypatch
):
    import os

    args, _, _ = staged_case(tmp_path)
    seen = []

    def execute(executable, argv, environment):
        seen.append((executable, argv, environment))
        raise RuntimeError("exec boundary")

    monkeypatch.setattr(os, "execve", execute)
    monkeypatch.setattr(
        pipeline, "Client", lambda *a, **kw: pytest.fail("heavy parent started LSP")
    )
    with pytest.raises(RuntimeError, match="exec boundary"):
        pipeline.main(staged_arguments(args))
    assert Path(seen[0][1][1]).name == "staged.py"
    assert seen[0][1][2:] == staged_arguments(args)
    assert not (args.output / "lsp.jsonl").exists()


def test_staged_coordinator_completes_only_after_all_serial_stages(
    tmp_path, monkeypatch
):
    from arcgraph.semantic_prototype import staged

    args, _, _ = staged_case(tmp_path)
    target = tmp_path / "final"
    calls = []
    collect = staged.transport_stage
    monkeypatch.setattr(
        staged, "transport_stage", lambda a, c: collect(a, c, StagedReplay)
    )

    process_run = staged.subprocess.run
    monkeypatch.setenv("NODE_OPTIONS", "")

    def dispatch(command, check=False, **kwargs):
        if len(command) > 2 and command[2] == str(Path(staged.__file__).resolve()):
            assert check
            calls.append(command[-1])
            assert staged.main(command[3:]) == 0
        else:
            return process_run(command, check=check, **kwargs)

    monkeypatch.setattr(staged.subprocess, "run", dispatch)
    assert (
        staged.main(staged_arguments(args, target) + ["--pyright-heap-mib", "128"]) == 0
    )
    assert calls == ["structure", "transport", "graph", "map"]
    assert staged.load(target / "run.json")["staged_complete"] is True
    assert staged.load(target / ".stage-cache/phase.json")["phase"] == "complete"
    with pytest.raises(ValueError):
        staged.main(
            staged_arguments(args, tmp_path / "invalid") + ["--pyright-heap-mib", "0"]
        )
    with pytest.raises(FileExistsError):
        staged.main(staged_arguments(args, target))


def test_failed_stage_cannot_publish_complete_pipeline_result(tmp_path, monkeypatch):
    import subprocess
    from arcgraph.semantic_prototype import staged

    args, _, _ = staged_case(tmp_path)
    target = tmp_path / "failed"

    def fail(command, check):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(staged.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        staged.main(staged_arguments(args, target))
    assert not (target / "run.json").exists()
    assert not (target / "projection.json").exists()


def test_rebound_class_name_is_not_a_known_override(tmp_path):
    _, _, view = make_view(
        tmp_path,
        source="class Base:\n    def f(self): pass\nBase = factory()\nclass Child(Base):\n    def f(self): return 1\n",
    )
    assert find(view, "Base.f").id not in view.declaration_evidence


@pytest.mark.parametrize("broken", ["payload", "count", "truncated", "extra"])
def test_graph_journal_preserves_fields_and_rejects_corruption(tmp_path, broken):
    from arcgraph.semantic_prototype import staged

    file = tmp_path / "graph.jsonl"
    original = {
        "nodes": [{"id": "n", "properties": {"unicode": "é😀", "unknown": None}}],
        "edges": [{"source": "n", "target": "n"}],
        "warnings": [],
    }
    staged.write_graph_cache(original, file)
    assert staged.read_graph_cache(file) == original
    lines = file.read_text(encoding="utf-8").splitlines()
    if broken == "payload":
        lines[1] = lines[1].replace('"unknown":null', '"unknown":false')
    if broken == "count":
        lines[0] = lines[0].replace('"nodes":1', '"nodes":2')
    if broken == "truncated":
        lines = lines[:-1]
    if broken == "extra":
        lines.append("{}")
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        staged.read_graph_cache(file)


def test_disk_generation_rejects_non_disk_records(tmp_path):
    from arcgraph.semantic_prototype.disk_structure import DiskGeneration

    _, _, view = make_view(tmp_path)
    invalid = DiskGeneration.model_validate(view.bundle.model_dump(mode="json"))
    with pytest.raises(ValueError, match="disk record sequence"):
        invalid.checked()


@pytest.mark.parametrize("failure", ["timeout", "tool_error"])
def test_transport_scope_errors_replay_as_partial_without_losing_universe(
    tmp_path, failure
):
    from arcgraph.semantic_prototype import staged

    args, cache, view = staged_case(tmp_path)
    staged.structure_stage(args, cache)

    class Peer:
        def __init__(self, *a, **kw):
            self.stderr = []

        def request(self, method, params, timeout=30):
            if method == "initialize":
                return {"capabilities": {"callHierarchyProvider": True}}
            if failure == "timeout":
                raise TimeoutError("fixture")
            raise OSError("fixture")

        def notify(self, *a):
            pass

        def close(self):
            pass

    staged.transport_stage(args, cache, Peer)
    staged.graph_stage(args, cache)
    staged.map_stage(args, cache)
    result = BackendRun.model_validate_json((args.output / "pyright.json").read_bytes())
    assert result.availability == "partial" and result.complete
    assert len(result.answers) == len(view.callsites)
    assert any(failure in a.reasons for a in result.answers)
    assert (
        staged.load(cache / "mapping.json")["pyright"]["requests"][
            failure if failure == "timeout" else "error"
        ]
        == 3
    )


def test_disk_validation_receipt_checks_bytes_and_metadata_not_mtime(
    tmp_path, monkeypatch
):
    import os
    from dataclasses import FrozenInstanceError
    from arcgraph.semantic_prototype import staged
    from arcgraph.semantic_prototype.disk_structure import read

    _, cache, view = staged_case(tmp_path)
    file = cache / "structure.jsonl"
    staged.write_structure_cache(view.bundle, file)
    generation = read(file)
    original = file.read_bytes()
    stat = file.stat()
    with pytest.raises(ValueError):
        generation.model_copy(update={"count": 0}).checked()
    # Revalidation of identical bytes can reuse the receipt, but a changed file
    # with preserved timestamp must not pass the admission boundary.
    assert generation.checked() is generation
    with pytest.raises(FrozenInstanceError):
        generation.records.path = tmp_path / "other"
    with pytest.raises(TypeError):
        generation.records.identities["forged"] = 0
    with pytest.raises(FrozenInstanceError):
        generation.records.by_id.records = None
    file.write_bytes(
        original.replace(b'"stub_body":"empty"', b'"stub_body":"other"', 1)
    )
    os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert file.stat().st_mtime_ns == stat.st_mtime_ns
    with pytest.raises(ValueError, match="corrupt"):
        generation.checked()


def test_missing_disk_record_store_is_rejected_by_admission(tmp_path):
    from arcgraph.semantic_prototype import staged
    from arcgraph.semantic_prototype.disk_structure import read

    _, cache, view = staged_case(tmp_path)
    file = cache / "structure.jsonl"
    staged.write_structure_cache(view.bundle, file)
    disk = read(file)
    file.unlink()
    assert structure_provider.admit_structure(disk, view.bundle.snapshot) == (
        False,
        "structural journal unavailable",
    )
