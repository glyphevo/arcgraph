"""Unified structure anchors, independent generations, and shadow policy rules."""

import json


from pathlib import Path


import sys


import pytest


from arcgraph.semantic import structure_provider


from arcgraph.semantic.anchors import AnchoredRecords, View, admit


from arcgraph.semantic.backends import producer, pyright_records


from arcgraph.semantic.contract import (
    Candidate,
    Span,
    digest,
)


from arcgraph.semantic.json_stream import content_digest, chunks


from arcgraph.semantic.projection import project


from arcgraph.semantic.snapshot import capture

from arcgraph.tests.project_environment_support import runtime_environment

FIXTURE = Path(__file__).parent / "fixtures/semantic"


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
        environment=(("dependencies", "none"),),
    )
    bundle = structure_provider.analyze(
        snapshot, root, environment=runtime_environment(tmp_path), access_facts=True
    )
    assert bundle.complete, bundle.reasons
    return snapshot, root, View(bundle)


def make_records(snapshot, root, view, name="pyright"):
    backend = producer(name, "test", (TARGET,), {})
    return AnchoredRecords(snapshot, root, backend, view)


@pytest.mark.parametrize(
    "positive,enabled", [(True, True), (False, True), (True, False)]
)
def test_unverified_external_target_conflict_requires_positive_core_answer(
    tmp_path, positive, enabled
):
    import copy
    import inspect
    from arcgraph.core.scanner import FileScanner
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.semantic.backends import wrap_graph
    from arcgraph.semantic.graph_projection import project_graph

    snapshot, root, view = make_view(
        tmp_path, source="def target(): return 1\ndef run(): return target()\n"
    )
    content = ArcGraphIndexer(root, tmp_path / "out").analyze_files(
        FileScanner(root).scan()
    )
    raw = {
        "nodes": [n.model_dump(mode="json") for n in content.nodes],
        "edges": [e.model_dump(mode="json") for e in content.edges],
    }
    original = next(
        e for e in raw["edges"] if e["source"] == "fn:demo.run" and e["kind"] == "calls"
    )
    wrong = copy.deepcopy(original)
    wrong.update(target="extsym:unverified.call", kind="uses", confidence="inferred")
    wrong["confidence_sources"] = {"inferred": ["analyzer_fixture"]}
    wrong["semantic_role"] = "external_call"
    for fact in [
        wrong["properties"]["callsite"],
        *wrong["properties"].get("callsites", []),
    ]:
        fact.update(confidence="inferred", edge_kind="uses")
    raw["edges"].append(wrong)
    records = make_records(snapshot, root, view)
    site = next(c for c in view.callsites if c.owner == find(view, "run").id)
    target = find(view, "target")
    if positive:
        records.candidate(site, target, "type_inference", "native:positive")
        records.mapped("native:positive", "mapped_success", "mapped", site, target)
    primary = records.finish()
    fallback_producer = producer("arcgraph", "test", (TARGET,), {})
    fallback = wrap_graph(snapshot, root, fallback_producer, view, raw)
    decisions = project(view, primary, fallback, (primary.producer, fallback_producer))
    kwargs = {"primary_producer": primary.producer.id, "primary": primary}
    # The parent bridge has no opt-in flag; exercising its existing output still
    # proves the new positive-core behavior by an assertion failure.
    if "conflict_unverified_external" in inspect.signature(project_graph).parameters:
        kwargs["conflict_unverified_external"] = enabled
    graph, _ = project_graph(view, raw, fallback, decisions, **kwargs)
    retained = next(e for e in graph if e["target"] == "extsym:unverified.call")
    assert retained["resolution"]["status"] == (
        "conflict" if positive and enabled else "resolved"
    )
    assert retained["confidence"] == "inferred"
    if positive and enabled:
        assert (
            "Unverified analyzer external target:" in retained["resolution"]["detail"]
        )
    correct = [
        e
        for e in graph
        if e["source"] == "fn:demo.run" and e["target"] == "fn:demo.target"
    ]
    assert len(correct) == 1
    if positive:
        assert any(e["kind"] == "type_inference" for e in correct[0]["evidence"])


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
    from arcgraph.semantic.lsp import LspError

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
    from arcgraph.semantic.contract import MappingRow

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
    assert set(dependencies) == {"demo.Base.raises"}
    for edge in dependencies.values():
        support = edge["supports"][0]
        assert support["decision"] == "retain_declaration_dependency:overridden_stub"
        assert support["evidence"][-2] == "overridden_stub"
        assert view.defs[support["evidence"][-1]].owner == find(view, "Concrete").id
    assert {view.defs[k[1]].qualname for k in report["projected_calls"]} == {
        "demo.Base.empty",
        "demo.Base.documented",
        "demo.Base.ellipsis",
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
        f"{import_line}\nclass Base:\n    def f(self): raise NotImplementedError()\n"
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


def test_only_notimplemented_base_with_concrete_override_is_declaration(tmp_path):
    _, _, view = make_view(tmp_path, filename="policy-v3.txt")
    assert find(view, "Base.raises").id in view.declaration_evidence
    for name in ("empty", "documented", "ellipsis", "effect", "unoverridden"):
        assert find(view, "Base." + name).id not in view.declaration_evidence
