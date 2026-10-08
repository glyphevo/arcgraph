"""Structural facts, target identity and parse-only failure boundaries."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from arcgraph.semantic_prototype import structure_facts as worker
from arcgraph.semantic_prototype import structure_provider as provider
from arcgraph.semantic_prototype.contract import write_generation
from arcgraph.semantic_prototype.snapshot import capture

FIXTURE = Path(__file__).parent / "fixtures/semantic_prototype"
CASES = json.loads((FIXTURE / "structure-cases.json").read_text(encoding="utf-8"))
TARGET = f"{sys.version_info.major}.{sys.version_info.minor}"


def facts(text, version=(3, 11)):
    tree = ast.parse(text, type_comments=True)
    scanner = worker.Facts("example.py", text, version)
    scanner.future = text.startswith("from __future__ import annotations")
    scanner.visit(tree)
    return scanner.records


def definitions(records):
    return {r["payload"]["name"]: r for r in records if r["kind"] == "definition"}


def calls(records):
    return {r["payload"]["expression"]: r for r in records if r["kind"] == "callsite"}


def frozen(tmp_path, text="def f(): return f()\n", target=TARGET):
    source = tmp_path / "source"
    source.mkdir()
    (source / "example.py").write_bytes(text.encode())
    root = tmp_path / "frozen"
    snapshot = capture(
        source, ["example.py"], root, target_python=target, platform="Darwin"
    )
    return snapshot, root


def test_defaults_decorators_class_body_and_nested_execution_owners():
    records = facts(CASES["scopes-py311"]["source"])
    ds, cs = definitions(records), calls(records)
    outer = ds["outer"]["id"]
    nested = ds["nested"]["id"]
    local = ds["Local"]["id"]
    assert cs["decorate(factory())"]["execution_owner"] == "module:example.py"
    assert cs["decorate(factory())"]["syntactic_owner"] == outer
    assert cs["default()"]["phase"] == "definition_default"
    assert cs["inside_default()"]["execution_owner"] == outer
    assert cs["inside_default()"]["syntactic_owner"] == nested
    assert cs["inner(y)"]["execution_owner"] == nested
    assert cs["base()"]["phase"] == "definition_base"
    assert cs["meta()"]["execution_owner"] == outer
    assert cs["field_init()"]["execution_owner"] == local
    assert cs["method_default()"]["execution_owner"] == local
    assert cs["method_default()"]["syntactic_owner"] == ds["method"]["id"]
    assert cs["method_body(x)"]["execution_owner"] == ds["method"]["id"]
    assert cs["lambda_default()"]["execution_owner"] == outer
    assert cs["lambda_body(z)"]["phase"] == "lambda_body"
    params = ds["outer"]["payload"]["parameters"]
    assert [(p["name"], p["kind"]) for p in params] == [
        ("x", "positional_only"),
        ("args", "vararg"),
        ("flag", "keyword_only"),
        ("kw", "kwarg"),
    ]
    assert params[0]["default"]["source"] == "default()"
    assert params[1]["annotation"]["source"] == "Var"
    assert ds["Local"]["payload"]["bases"][0]["source"] == "base()"


def test_class_body_phase_is_distinct_from_method_body_and_defaults():
    records = facts(CASES["scopes-py311"]["source"])
    ds, cs = definitions(records), calls(records)
    assert cs["field_init()"]["phase"] == "class_body"
    assert cs["field_init()"]["execution_owner"] == ds["Local"]["id"]
    assert cs["method_default()"]["phase"] == "definition_default"
    assert cs["method_default()"]["execution_owner"] == ds["Local"]["id"]
    assert cs["method_body(x)"]["phase"] == "function_body"
    assert cs["method_body(x)"]["execution_owner"] == ds["method"]["id"]


@pytest.mark.parametrize(
    "version,inlined", [((3, 11), False), ((3, 12), True), ((3, 14), True)]
)
def test_comprehension_lexical_scope_and_first_iterable(version, inlined):
    text = "def f():\n    return [emit(x) for x in first() if keep(x)], (later(x) for x in second())\n"
    records = facts(text, version)
    outer = definitions(records)["f"]["id"]
    cs = calls(records)
    assert cs["first()"]["execution_owner"] == outer
    assert cs["first()"]["lexical_scope"] == outer
    assert cs["second()"]["phase"] == "function_body"
    assert cs["emit(x)"]["lexical_scope"] != outer
    assert (cs["emit(x)"]["execution_owner"] == outer) is inlined
    assert cs["later(x)"]["execution_owner"] != outer
    assert cs["later(x)"]["phase"] == "deferred_generator"
    assert cs["keep(x)"]["phase"] == "comprehension_eager"


@pytest.mark.parametrize(
    "version,future,phase",
    [
        ((3, 11), False, "annotation_eager"),
        ((3, 13), False, "annotation_eager"),
        ((3, 14), False, "annotation_lazy"),
        ((3, 11), True, "annotation_stringized"),
        ((3, 14), True, "annotation_stringized"),
    ],
)
def test_annotation_phases_are_versioned_and_include_varargs(version, future, phase):
    text = "def f(x: arg(), *args: var(), **kw: keyword()) -> result():\n    local: ignored() = body()\n"
    if future:
        text = "from __future__ import annotations\n" + text
    records = facts(text, version)
    cs = calls(records)
    for name in ("arg()", "var()", "keyword()", "result()"):
        assert cs[name]["phase"] == phase
        assert (cs[name]["execution_owner"] is None) is future
    assert cs["ignored()"]["phase"] == "annotation_local_no_eval"
    assert cs["ignored()"]["execution_owner"] is None
    assert cs["ignored()"]["payload"]["execution"] == "syntax_only"
    assert cs["body()"]["phase"] == "function_body"


def test_generic_defaults_remain_outer_and_bounds_are_lazy():
    text = "def f(value: annotate() = make()) -> result():\n    return body(value)\n"
    tree = ast.parse(text)
    fn = tree.body[0]
    # The new AST field is provided directly on host 3.11. Actual grammar is
    # separately exercised by the recorded 3.14 run and opt-in target matrix.
    fn.type_params = [
        SimpleNamespace(
            name="T",
            lineno=1,
            col_offset=6,
            end_lineno=1,
            end_col_offset=11,
            bound=fn.returns,
            default_value=None,
        )
    ]
    scanner = worker.Facts("example.py", text, (3, 13))
    scanner.visit(tree)
    cs = [r for r in scanner.records if r["kind"] == "callsite"]
    make = next(r for r in cs if r["payload"]["expression"] == "make()")
    assert make["execution_owner"] == "module:example.py"
    assert make["lexical_scope"] == "module:example.py"
    bound = next(r for r in cs if r["phase"] == "type_parameter_lazy")
    assert bound["execution_owner"] != "module:example.py"
    assert bound["lexical_scope"] == bound["execution_owner"]


def test_adapters_import_aliases_bindings_exports_and_control_are_preserved():
    records = facts(CASES["adapters-bindings"]["source"])
    ds = definitions(records)
    assert ds["endpoint"]["payload"]["decorators"][0]["source"].startswith(
        "router.get("
    )
    assert ds["Record"]["payload"]["bases"][0]["source"] == "BaseModel"
    im = [r["payload"] for r in records if r["kind"] == "import"]
    assert any(r["level"] == 2 and r["module"] is None for r in im)
    assert any(a["binding"] == "Router" for r in im for a in r["aliases"])
    assert any(r["star"] and r["aliases"][0]["binding"] is None for r in im)
    exports = [r["payload"] for r in records if r["kind"] == "export"]
    assert exports == [
        {"operation": "Assign", "status": "literal", "names": ["Model", "endpoint"]}
    ]
    binding = [
        r for r in records if r["kind"] == "binding" and r["payload"].get("annotation")
    ]
    assert {r["payload"]["annotation"]["source"] for r in binding} == {"str", "int"}
    other = facts(CASES["binding-control"]["source"])
    roles = {r["payload"]["role"] for r in other if r["kind"] == "binding"}
    assert {
        "Global",
        "Nonlocal",
        "AugAssign",
        "AnnAssign",
        "NamedExpr",
        "with_target",
    } <= roles
    assert (
        next(r for r in other if r["kind"] == "export")["payload"]["status"]
        == "dynamic"
    )
    assert {r["payload"]["syntax"] for r in other if r["kind"] == "control"} == {
        "If",
        "With",
    }


def test_all_three_call_ranges_and_duplicate_definition_names_are_distinct():
    text = "def café(x):\n    café(x); (café)(x); factory()(); (x\n        .méthod)(x)\ndef café(x): return café(x)\n"
    records = facts(text)
    ds = [r for r in records if r["kind"] == "definition"]
    assert len({r["id"] for r in ds}) == 2
    assert len({r["payload"]["qualname"] for r in ds}) == 1
    cs = [r for r in records if r["kind"] == "callsite"]
    assert len(cs) == 6
    parenthesized = next(r for r in cs if r["payload"]["expression"] == "(café)(x)")
    assert parenthesized["span"] != parenthesized["payload"]["callee_span"]
    assert (
        parenthesized["payload"]["callee_span"]
        == parenthesized["payload"]["token_span"]
    )
    outer_factory = next(r for r in cs if r["payload"]["expression"] == "factory()()")
    assert outer_factory["payload"]["token_span"] is None
    attribute = next(r for r in cs if r["payload"]["callee"]["syntax"] == "Attribute")
    assert attribute["payload"]["token_span"] == [2, 9, 2, 16]
    assert ds[0]["payload"]["name_span"] == [0, 4, 0, 9]


def test_worker_returns_per_file_failure_and_never_executes_source(tmp_path):
    (tmp_path / "good.py").write_text(
        "raise RuntimeError('must not execute')\ndef f(): return call()\n",
        encoding="utf-8",
    )
    (tmp_path / "bad.py").write_text(CASES["error-middle"]["source"], encoding="utf-8")
    (tmp_path / "bom.py").write_bytes(CASES["bom-crlf"]["source"].encode())
    (tmp_path / "encoding.py").write_bytes(b"# coding: utf-8\n\xff")
    data = worker.scan(tmp_path, ["good.py", "bad.py", "bom.py", "encoding.py"], TARGET)
    assert [f["status"] for f in data["files"]] == [
        "parsed",
        "parse_error",
        "parsed",
        "parse_error",
    ]
    assert all(r["path"] not in ("bad.py", "encoding.py") for r in data["records"])
    assert data["files"][1]["diagnostic"]["recovery"] == "none"
    assert any(r["path"] == "bom.py" for r in data["records"])
    (tmp_path / "link.py").symlink_to(tmp_path / "good.py")
    with pytest.raises(ValueError, match="outside"):
        worker.scan(tmp_path, ["link.py"], TARGET)


def test_current_interpreter_generation_and_atomic_artifact(tmp_path):
    snapshot, root = frozen(tmp_path)
    bundle = provider.analyze(snapshot, root, {TARGET: sys.executable})
    assert bundle.availability == "available"
    assert bundle.complete
    assert provider.admit_structure(bundle, snapshot) == (True, "experimental_admitted")
    assert all(r.envelope.snapshot == snapshot.id for r in bundle.records)
    path = tmp_path / "artifact.json"
    write_generation(bundle, path)
    assert (
        provider.StructuralGeneration.model_validate_json(path.read_bytes()).checked()
        == bundle
    )
    wrong = snapshot.model_copy(update={"platform": "Linux"})
    assert provider.admit_structure(bundle, wrong) == (
        False,
        "snapshot_identity_mismatch",
    )


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("missing", "target_interpreter_missing"),
        ("unsupported", "target_python_unsupported"),
        ("mismatch", "interpreter_identity_mismatch"),
        ("executable_missing", "tool_error_or_invalid_output"),
        ("timeout", "timeout"),
        ("crash", "tool_error_or_invalid_output"),
        ("bad_json", "tool_error_or_invalid_output"),
    ],
)
def test_unavailable_is_incomplete_without_lower_version_fallback(
    tmp_path, monkeypatch, mode, reason
):
    target = (
        "3.15"
        if mode == "unsupported"
        else (
            "3.13"
            if TARGET == "3.14" and mode == "mismatch"
            else "3.14" if mode == "mismatch" else TARGET
        )
    )
    snapshot, root = frozen(tmp_path, target=target)
    registry = (
        {}
        if mode == "missing"
        else {
            target: (
                "/missing/interpreter"
                if mode == "executable_missing"
                else sys.executable
            )
        }
    )

    def run(*args, **kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired(args[0], 1)
        if mode == "crash":
            raise subprocess.CalledProcessError(1, args[0])
        return SimpleNamespace(stdout="not json")

    if mode in ("timeout", "crash", "bad_json"):
        monkeypatch.setattr(provider.subprocess, "run", run)
    result = provider.analyze(snapshot, root, registry)
    assert not result.complete and result.availability == "unavailable"
    assert result.reasons == (reason,)
    assert result.records == ()
    assert provider.admit_structure(result, snapshot)[0] is False


def test_recorded_target_314_produces_new_syntax_with_host_311(tmp_path, monkeypatch):
    names = ["py313-type-defaults", "py314-template-annotations", "future-annotations"]
    source = tmp_path / "source"
    source.mkdir()
    for name in names:
        (source / (name + ".py")).write_text(CASES[name]["source"], encoding="utf-8")
    root = tmp_path / "frozen"
    snapshot = capture(
        source,
        [name + ".py" for name in names],
        root,
        target_python="3.14",
        platform="Darwin",
    )
    recorded = (FIXTURE / "recorded-structure-3.14.json").read_text(encoding="utf-8")

    def run(command, **kwargs):
        assert command[0] == "explicit-3.14"
        assert "-I" in command and "-S" in command and "-B" in command
        return SimpleNamespace(stdout=recorded)

    monkeypatch.setattr(provider.subprocess, "run", run)
    bundle = provider.analyze(snapshot, root, {"3.14": "explicit-3.14"})
    assert bundle.complete and bundle.availability == "available"
    defs = [r for r in bundle.records if r.kind == "definition"]
    assert any(r.payload["symbol_kind"] == "type_alias" for r in defs)
    assert any(p["default"] for r in defs for p in r.payload["type_parameters"])
    assert any(
        r.kind == "callsite" and r.payload["expression"] == "width()"
        for r in bundle.records
    )
    assert any(r.phase == "annotation_stringized" for r in bundle.records)


@pytest.mark.parametrize(
    "mutation",
    [
        "complete",
        "count",
        "checksum",
        "file",
        "file_digest",
        "file_duplicate",
        "duplicate",
        "envelope",
        "owner",
        "payload",
        "callee",
        "token",
        "scope_parent",
        "availability",
        "diagnostic",
    ],
)
def test_structural_generation_rejects_broken_invariants(tmp_path, mutation):
    snapshot, root = frozen(
        tmp_path, "def f(x: int):\n    return [f(x) for x in source()]\n"
    )
    result = provider.analyze(snapshot, root, {TARGET: sys.executable})
    payload = result.model_dump(mode="json")
    record = next(r for r in payload["records"] if r["kind"] == "callsite")
    if mutation == "complete":
        payload["complete"] = False
    elif mutation == "count":
        payload["count"] += 1
    elif mutation == "checksum":
        payload["checksum"] = "bad"
    elif mutation == "file":
        payload["files"] = []
    elif mutation == "file_digest":
        payload["files"][0]["raw_digest"] = "bad"
    elif mutation == "file_duplicate":
        payload["files"].append(payload["files"][0])
    elif mutation == "duplicate":
        payload["records"].append(payload["records"][0])
    elif mutation == "envelope":
        record["envelope"]["snapshot"] = "bad"
    elif mutation == "owner":
        record["execution_owner"] = "missing"
    elif mutation == "payload":
        record["payload"].pop("callee_span")
    elif mutation == "callee":
        record["payload"]["callee_span"] = [0, 0, 0, 1]
    elif mutation == "token":
        end = record["payload"]["token_span"][2:]
        record["payload"]["callee_span"] = [*end, end[0], end[1] + 1]
    elif mutation == "scope_parent":
        next(r for r in payload["records"] if r["kind"] == "scope")["payload"][
            "parent"
        ] = "missing"
    elif mutation == "availability":
        payload["availability"] = "partial"
    elif mutation == "diagnostic":
        payload["files"][0]["diagnostic"] = {"error": "wrong"}
    broken = provider.StructuralGeneration.model_validate(payload)
    if mutation not in ("complete", "count", "checksum"):
        broken = broken.model_copy(
            update={"count": len(broken.records), "checksum": broken.content_digest()}
        )
    with pytest.raises(ValueError):
        broken.checked()


def test_structure_entry_and_partial_files(tmp_path, capsys):
    snapshot, root = frozen(tmp_path, CASES["error-middle"]["source"])
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(snapshot.model_dump_json(), encoding="utf-8")
    output = tmp_path / "out.json"
    args = [
        "--snapshot",
        str(snapshot_file),
        "--root",
        str(root),
        "--interpreter",
        sys.executable,
        "--output",
        str(output),
    ]
    assert provider.main(args) == 0
    partial = provider.StructuralGeneration.model_validate_json(output.read_bytes())
    assert (
        partial.complete and partial.availability == "partial" and partial.records == ()
    )
    assert provider.admit_structure(partial, snapshot) == (True, "experimental_partial")
    bad_args = args.copy()
    bad_args[bad_args.index("--interpreter") + 1] = "/missing/interpreter"
    bad_args[-1] = str(tmp_path / "unwritten.json")
    assert provider.main(bad_args) == 2
    assert "unavailable" in capsys.readouterr().err


def test_span_payload_rejects_invalid_shape_and_decorator_placement(tmp_path):
    with pytest.raises(ValueError, match="four-coordinate"):
        list(provider.spans({"span": [0, 1]}))
    assert list(provider.spans({"token_span": None})) == []
    snapshot, root = frozen(tmp_path, "@decorate()\ndef f(): return body()\n")
    bundle = provider.analyze(snapshot, root, {TARGET: sys.executable})
    assert bundle.complete
    d = next(r for r in bundle.records if r.kind == "definition")
    wrong = d.model_copy(
        update={"payload": {**d.payload, "decorators": [{"span": [2, 0, 2, 1]}]}}
    )
    changed = bundle.model_copy(
        update={"records": tuple(wrong if r.id == d.id else r for r in bundle.records)}
    )
    changed = changed.model_copy(update={"checksum": changed.content_digest()})
    with pytest.raises(ValueError, match="decorator"):
        changed.checked()


@pytest.mark.parametrize(
    "data,reason",
    [
        ([], "tool_error_or_invalid_output"),
        ({"interpreter": []}, "tool_error_or_invalid_output"),
        ({"profile": "wrong", "interpreter": {}}, "interpreter_identity_mismatch"),
        (
            {
                "profile": worker.PROFILE,
                "interpreter": {
                    "target": TARGET,
                    "implementation": "cpython",
                    "version": [],
                },
            },
            "interpreter_identity_mismatch",
        ),
        (
            {
                "profile": worker.PROFILE,
                "interpreter": {
                    "target": TARGET,
                    "implementation": "cpython",
                    "version": TARGET + ".0",
                },
                "files": [],
                "records": [{}],
            },
            "invalid_structural_records",
        ),
    ],
)
def test_invalid_worker_output_never_becomes_complete(
    tmp_path, monkeypatch, data, reason
):
    snapshot, root = frozen(tmp_path)
    monkeypatch.setattr(
        provider.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=json.dumps(data)),
    )
    result = provider.analyze(snapshot, root, {TARGET: "explicit"})
    assert not result.complete and result.reasons == (reason,)


@pytest.mark.parametrize("target", ["3.11", "3.12", "3.13", "3.14"])
def test_opt_in_target_interpreters_parse_frozen_new_syntax(tmp_path, target):
    registry = json.loads(os.environ.get("ARCGRAPH_STRUCTURE_INTERPRETERS", "{}"))
    if target not in registry:
        pytest.skip("explicit target interpreter not specified")
    source = tmp_path / "source"
    source.mkdir()
    names = [name for name, case in CASES.items() if case["target"] == target]
    for name in names:
        (source / (name + ".py")).write_bytes(CASES[name]["source"].encode())
    root = tmp_path / "frozen"
    snapshot = capture(
        source,
        [name + ".py" for name in names],
        root,
        target_python=target,
        platform="Darwin",
    )
    bundle = provider.analyze(snapshot, root, registry)
    assert bundle.complete
    expected_errors = sum(CASES[name]["error_fixture"] for name in names)
    assert sum(f.status != "parsed" for f in bundle.files) == expected_errors
    assert provider.admit_structure(bundle, snapshot)[0]


def test_worker_identity_is_checked_before_any_file_parse(tmp_path):
    wrong = "3.13" if TARGET == "3.14" else "3.14"
    (tmp_path / "example.py").write_text("def f(): return f()\n", encoding="utf-8")
    result = worker.scan(tmp_path, ["example.py"], wrong)
    assert result["files"] == [] and result["records"] == []
    assert result["interpreter"]["target"] == TARGET


def test_frozen_identity_is_checked_before_and_after_worker(tmp_path, monkeypatch):
    snapshot, root = frozen(tmp_path)
    original = (root / "example.py").read_bytes()
    (root / "example.py").write_bytes(original + b"# dirty\n")
    with pytest.raises(ValueError, match="frozen source changed"):
        provider.analyze(snapshot, root, {})
    (root / "example.py").write_bytes(original)
    data = worker.scan(root, ["example.py"], TARGET)

    def run(*a, **kw):
        (root / "example.py").write_bytes(original + b"# changed during parse\n")
        return SimpleNamespace(stdout=json.dumps(data))

    monkeypatch.setattr(provider.subprocess, "run", run)
    with pytest.raises(ValueError, match="frozen source changed"):
        provider.analyze(snapshot, root, {TARGET: "explicit"})


def test_provider_rejects_byte_positions_cutting_unicode(tmp_path, monkeypatch):
    snapshot, root = frozen(tmp_path, "def café(): return café()\n")
    data = worker.scan(root, ["example.py"], TARGET)
    definition = next(r for r in data["records"] if r["kind"] == "definition")
    definition["payload"]["name_span"][-1] = 8  # inside é, not its UTF-8 endpoint 9
    monkeypatch.setattr(
        provider.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=json.dumps(data)),
    )
    bundle = provider.analyze(snapshot, root, {TARGET: "explicit"})
    assert not bundle.complete and bundle.reasons == ("invalid_structural_records",)


def test_provider_valid_unicode_ranges_are_byte_columns(tmp_path):
    snapshot, root = frozen(tmp_path, "def café(): return café()\n")
    bundle = provider.analyze(snapshot, root, {TARGET: sys.executable})
    assert bundle.complete
    definition = next(r for r in bundle.records if r.kind == "definition")
    assert definition.payload["name_span"] == [0, 4, 0, 9]


def test_structural_admission_rejects_rebound_unsupported_and_wrong_interpreters(
    tmp_path,
):
    snapshot, root = frozen(tmp_path)
    bundle = provider.analyze(snapshot, root, {TARGET: sys.executable})
    for target, reason in [
        ("3.15", "target_python_unsupported"),
        (TARGET, "interpreter_identity_mismatch"),
    ]:
        rebound = snapshot.model_copy(update={"target_python": target})
        producer = bundle.producer.model_copy(
            update={
                "interpreter": (
                    ("implementation", "cpython" if target == "3.15" else "pypy"),
                    ("target", target),
                )
            }
        )
        records = tuple(
            r.model_copy(
                update={
                    "envelope": r.envelope.model_copy(
                        update={"producer": producer.id, "snapshot": rebound.id}
                    )
                }
            )
            for r in bundle.records
        )
        changed = bundle.model_copy(
            update={"snapshot": rebound, "producer": producer, "records": records}
        )
        changed = changed.model_copy(update={"checksum": changed.content_digest()})
        changed.checked()
        assert provider.admit_structure(changed, rebound) == (False, reason)


@pytest.mark.parametrize(
    "source,typing_only",
    [
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    def f(): ...\n",
            True,
        ),
        ("import typing as t\nif t.TYPE_CHECKING:\n    def f(): ...\n", True),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING and test():\n    def f(): ...\n",
            True,
        ),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n    def f(): ...\n",
            False,
        ),
        (
            "from typing import TYPE_CHECKING\nTYPE_CHECKING = True\nif TYPE_CHECKING:\n    def f(): ...\n",
            False,
        ),
        (
            "from typing import TYPE_CHECKING\ndef outer(TYPE_CHECKING):\n    if TYPE_CHECKING:\n        def f(): ...\n",
            False,
        ),
        (
            "from typing import TYPE_CHECKING\nfrom other import TYPE_CHECKING\nif TYPE_CHECKING:\n    def f(): ...\n",
            False,
        ),
        (
            "from typing import TYPE_CHECKING\nfrom other import *\nif TYPE_CHECKING:\n    def f(): ...\n",
            False,
        ),
        (
            "import typing\ntyping.TYPE_CHECKING = True\nif typing.TYPE_CHECKING:\n    def f(): ...\n",
            False,
        ),
        (
            "from typing import TYPE_CHECKING\ndef TYPE_CHECKING(): return True\nif TYPE_CHECKING:\n    def f(): ...\n",
            False,
        ),
        ("TYPE_CHECKING = True\nif TYPE_CHECKING:\n    def f(): ...\n", False),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING or True:\n    def f(): ...\n",
            False,
        ),
    ],
)
def test_type_checking_guard_requires_standard_unshadowed_binding(source, typing_only):
    definition = definitions(facts(source))["f"]
    assert definition["payload"]["typing_only"] is typing_only
    assert definition["payload"]["stub_body"] == "empty"


@pytest.mark.parametrize(
    "body,expected",
    [
        ('"doc"', "empty"),
        ("pass", "empty"),
        ("...", "empty"),
        ('"doc"; pass; ...', "empty"),
        ("raise NotImplementedError", "not_implemented"),
        ('raise NotImplementedError("override")', "not_implemented"),
        ("raise NotImplementedError(message='override')", "not_implemented"),
        ("raise NotImplementedError(factory())", None),
        ("raise NotImplementedError(message=factory())", None),
        ("raise NotImplementedError from cause", None),
        ("return None", None),
        ("effect(); raise NotImplementedError", None),
        ("raise OtherError", None),
    ],
)
def test_stub_body_does_not_hide_effects_or_other_exceptions(body, expected):
    assert (
        definitions(facts(f"def f(): {body}\n"))["f"]["payload"]["stub_body"]
        == expected
    )


def test_shadowed_not_implemented_error_is_not_a_stub_convention():
    result = definitions(
        facts("def f(NotImplementedError): raise NotImplementedError\n")
    )
    assert result["f"]["payload"]["stub_body"] is None
