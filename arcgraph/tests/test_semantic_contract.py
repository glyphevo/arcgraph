"""C01-C05, C07 and C11 invariants on the product semantic modules."""

import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from arcgraph.tests.time_limits import WAIT_HANG_SECONDS
from arcgraph.semantic.anchors import AnchoredRecords, View, admit
from arcgraph.semantic.backends import producer, pyright_records
from arcgraph.semantic.contract import Answer, Candidate, Source, Span
from arcgraph.semantic.records import semantic_digest
from arcgraph.semantic.snapshot import (
    capture,
    canonical,
    column,
    native_position,
    native_span,
    verify,
)
from arcgraph.semantic.structure_provider import analyze
from arcgraph.tests.fixtures.semantic.replay import Replay

from arcgraph.tests.project_environment_support import runtime_environment

TARGET = ".".join(map(str, sys.version_info[:2]))


def make(tmp_path, source=None, environment=(("dependencies", "none"),)):
    src = tmp_path / "source"
    src.mkdir()
    (src / "demo.py").write_text(
        source
        or (Path(__file__).parent / "fixtures/semantic/source.txt").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    frozen = tmp_path / "frozen"
    snapshot = capture(
        src,
        ["demo.py"],
        frozen,
        target_python=TARGET,
        platform="Darwin",
        environment=environment,
    )
    view = View(analyze(snapshot, frozen, environment=runtime_environment(tmp_path)))
    backend = producer("pyright", "1.1.414", (TARGET,), {"wrapper": "semantic/1"})
    return snapshot, frozen, view, backend


def test_c01_dirty_untracked_relocation_and_environment_are_content_identity(tmp_path):
    snapshot, root, view, backend = make(tmp_path)
    relocated = capture(
        root,
        ["demo.py"],
        tmp_path / "relocated",
        target_python=TARGET,
        platform="Darwin",
        environment=snapshot.environment,
    )
    assert relocated.id == snapshot.id
    (tmp_path / "source/demo.py").write_text("def changed(): pass\n", encoding="utf-8")
    (tmp_path / "source/new.py").write_text("def untracked(): pass\n", encoding="utf-8")
    dirty = capture(
        tmp_path / "source",
        ["demo.py", "new.py"],
        tmp_path / "dirty",
        target_python=TARGET,
        platform="Darwin",
        environment=snapshot.environment,
    )
    assert dirty.id != snapshot.id
    for key, value in [
        ("dependency", "changed"),
        ("stub", "changed"),
        ("search_paths", "reversed"),
        ("configuration", "changed"),
    ]:
        assert (
            snapshot.model_copy(update={"environment": ((key, value),)}).id
            != snapshot.id
        )
    other_python = "3.11" if TARGET != "3.11" else "3.14"
    for key, value in [("platform", "Windows"), ("target_python", other_python)]:
        assert snapshot.model_copy(update={key: value}).id != snapshot.id
    unknown = snapshot.model_copy(
        update={"environment": (("dependencies", "unknown"),)}
    )
    records = AnchoredRecords(unknown, root, backend, view).finish()
    assert admit(records, view, unknown, backend)[0] is False
    (root / "demo.py").write_bytes(
        (root / "demo.py").read_bytes().replace(b"\n", b"\r\n")
    )
    with pytest.raises(ValueError, match="changed"):
        verify(snapshot, root)


def test_c02_traceable_envelopes_and_order_independent_semantic_digest(tmp_path):
    snapshot, root, view, backend = make(tmp_path)
    run, meta = pyright_records(snapshot, root, backend, view, Replay(root))
    assert meta["encoding"] == "utf-16"
    assert sum(row.first_reason == "mapped_success" for row in run.mapping) == 3
    assert sum(len(answer.candidates) for answer in run.answers) == 3
    assert run.snapshot.id == snapshot.id and run.producer.id == backend.id
    assert run.structure == view.bundle.id
    assert all(
        a.envelope.producer == backend.id and a.envelope.snapshot == snapshot.id
        for a in run.answers
    )
    assert run.raw_count == len(run.mapping) == 4
    assert semantic_digest(run) == semantic_digest(
        run.model_copy(
            update={
                "answers": tuple(reversed(run.answers)),
                "mapping": tuple(reversed(run.mapping)),
            }
        )
    )
    changed = run.answers[0].model_copy(
        update={"reasons": ("different_native_answer",)}
    )
    assert semantic_digest(
        run.model_copy(update={"answers": (changed, *run.answers[1:])})
    ) != semantic_digest(run)
    assert meta["requests"]["error"] == 0


@pytest.mark.parametrize(
    "unit,col", [("utf-8", 8), ("utf-16", 5), ("unicode_scalar", 4)]
)
def test_c03_coordinate_roundtrip(unit, col):
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
    combined = "e\u0301\u2028😀\ncall()"
    assert native_position(combined, (1, 0), unit) == {"line": 1, "character": 0}


@pytest.mark.parametrize(
    "line,col,unit",
    [
        (-1, 0, "utf-8"),
        (2, 0, "utf-8"),
        (0, 1, "utf-8"),
        (0, 2, "utf-16"),
        (0, 90, "unicode_scalar"),
        (0, 0, "unknown"),
        (True, 0, "utf-8"),
    ],
)
def test_c03_unconvertible_positions_fail(line, col, unit):
    with pytest.raises(ValueError):
        column("é😀", line, col, unit)


@pytest.mark.parametrize(
    "path", ["/x.py", "../x.py", "a/../x.py", "a//x.py", "a\\x.py"]
)
def test_c01_noncanonical_paths_fail(path):
    with pytest.raises(ValidationError):
        Source(path=path, raw_digest="", text_digest="")


def test_c04_calls_references_repeated_definitions_and_execution_owners(tmp_path):
    snapshot, root, view, backend = make(
        tmp_path,
        "def f(): return f\ndef f(): return 1\ndef g(x=f()):\n    f(); f(); f(f); f()()\nclass C:\n    x=f()\n",
    )
    same = [d for d in view.definitions if d.qualname == "demo.f"]
    assert len(same) == 2 and same[0].id != same[1].id
    sites = [s for s in view.callsites if s.expression == "f()"]
    assert len({s.id for s in sites}) == len(sites)
    assert len([s for s in view.callsites if s.expression == "f(f)"]) == 1
    default = next(s for s in view.callsites if s.phase == "definition_default")
    assert (
        default.owner == "module:demo.py" and default.syntactic_owner != default.owner
    )
    body = next(s for s in view.callsites if s.phase == "class_body")
    assert view.defs[body.owner].symbol_kind == "class"
    assert any(s.expression == "f()()" and s.token_span is None for s in view.callsites)


def test_c05_result_algebra_has_no_closed_negative_authority(tmp_path):
    snapshot, root, view, backend = make(tmp_path)
    record = AnchoredRecords(snapshot, root, backend, view)
    args = {"envelope": record.envelope("test"), "subject": view.callsites[0].id}
    candidate = Candidate(
        target=view.definitions[0].id,
        dispatch="invoke",
        semantics="implementation",
        method="lexical_binding",
    )
    for payload in (
        {"outcome": "candidates"},
        {"outcome": "unknown"},
        {"outcome": "negative", "reasons": ("not_callable",)},
        {
            "outcome": "negative",
            "reasons": ("capability_unsupported",),
            "candidates": (candidate,),
        },
    ):
        with pytest.raises(ValidationError):
            Answer(**args, **payload)
    assert all(a.outcome == "unknown" for a in record.finish().answers)


def test_c07_and_c11_no_admission_for_corruption_and_no_missing_subjects(tmp_path):
    snapshot, root, view, backend = make(tmp_path)
    good, _ = pyright_records(snapshot, root, backend, view, Replay(root))
    assert len(good.answers) == len(view.callsites)
    for update in (
        {"complete": False},
        {"checksum": "corrupt"},
        {"raw_count": good.raw_count - 1},
        {"answers": good.answers[1:]},
    ):
        assert not admit(good.model_copy(update=update), view, snapshot, backend)[0]
    assert not admit(
        good, view, snapshot.model_copy(update={"platform": "Windows"}), backend
    )[0]
    assert (
        sum(
            len([m for m in good.mapping if m.stage == stage])
            for stage in ("coordinate", "owner", "symbol", "mapped")
        )
        == good.raw_count
    )
    assert {a.subject for a in good.answers} == set(view.sites)
    assert json.loads(good.model_dump_json())["complete"]


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
    from arcgraph.semantic.lsp import Client, LspError

    with Client(
        [
            sys.executable,
            str((Path(__file__).parent / "fixtures/semantic") / "replay-stdio.txt"),
            mode,
        ],
        tmp_path,
        dict(os.environ),
        {"python": {"analysis": {"x": 1}}},
    ) as peer:
        if mode == "config":
            assert peer.request("probe", {}, timeout=WAIT_HANG_SECONDS) == "ok"
        else:
            with pytest.raises(
                TimeoutError if mode in ("timeout", "stubborn") else LspError
            ):
                peer.request(
                    "probe",
                    {},
                    timeout=(
                        0.1 if mode in ("timeout", "stubborn") else WAIT_HANG_SECONDS
                    ),
                )
    assert peer.process.poll() is not None


@pytest.mark.parametrize(
    "start,end",
    [((0, 0), (0, 0)), ((0, 2), (0, 1)), ((-1, 0), (0, 2)), ((True, 0), (1, 2))],
)
def test_span_requires_nonempty_strict_integer_half_open_range(start, end):
    with pytest.raises(ValueError):
        Span(start=start, end=end)


def test_source_root_module_mapping_binds_structure_to_default_graph(tmp_path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/demo.py").write_text(
        "class Base:\n    def run(self): raise NotImplementedError()\n"
        "class Concrete(Base):\n    def run(self): return 1\n",
        encoding="utf-8",
    )
    root = tmp_path / "frozen"
    snapshot = capture(
        source,
        ["src/demo.py"],
        root,
        target_python=TARGET,
        platform="Darwin",
        environment=(("dependencies", "none"),),
    )
    mapped = snapshot.model_copy(update={"module_names": (("src/demo.py", "demo"),)})
    assert mapped.id != snapshot.id
    view = View(analyze(mapped, root, environment=runtime_environment(tmp_path)))
    assert "demo.Concrete.run" in {d.qualname for d in view.definitions}
    assert not any(d.qualname.startswith("src.") for d in view.definitions)
    base = next(d for d in view.definitions if d.qualname == "demo.Base.run")
    assert view.declaration_evidence[base.id][0] == "overridden_stub"


def test_disk_records_are_private_and_changed_artifact_cannot_be_admitted(tmp_path):
    from arcgraph.semantic.disk_structure import read
    from arcgraph.semantic.journal import write_structure

    snapshot, root, view, backend = make(tmp_path)
    path = tmp_path / "structure.jsonl"
    write_structure(view.bundle, path)
    disk = read(path)
    original = disk.records[0].model_dump(mode="json")
    path.write_text("replaced\n", encoding="utf-8")
    assert disk.records[0].model_dump(mode="json") == original
    run = AnchoredRecords(snapshot, root, backend, view).finish()
    assert not admit(run, View(disk), snapshot, backend)[0]


def test_call_hierarchy_options_object_is_supported(tmp_path):
    snapshot, root, view, backend = make(tmp_path)

    class OptionsReplay(Replay):
        def request(self, method, params, timeout=30):
            result = super().request(method, params, timeout)
            if method == "initialize":
                result["capabilities"]["callHierarchyProvider"] = {}
            return result

    run, meta = pyright_records(snapshot, root, backend, view, OptionsReplay(root))
    assert run.availability == "available"
    assert sum(len(answer.candidates) for answer in run.answers) == 3
    assert meta["encoding"] == "utf-16"
