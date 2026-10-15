"""A collection shares one sealed copy and refuses drift before its next launch."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import sys

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.semantic import runtime, transport
from arcgraph.semantic.external_targets import Capture, proved_reexport
from arcgraph.semantic.journal import dump, load
from arcgraph.tests.test_batch30_build import configure, generation_directory
from arcgraph.tests.test_semantic_isolation import prepared as prepared
from arcgraph.tests.test_semantic_session_reuse import pool as pool


@pytest.mark.skipif(
    os.name == "nt", reason="Pool fixture uses a POSIX shell Node probe"
)
def test_multiple_sessions_share_one_copy_until_pool_close(pool, monkeypatch):
    instance, node = pool
    instance.close()
    instance = runtime.SessionPool()
    entries = []
    copied = []
    copytree = runtime.shutil.copytree

    def copy(*args, **kwargs):
        if Path(args[1]).name == "installation":
            copied.append(args)
        return copytree(*args, **kwargs)

    class Client:
        def __init__(self, argv, *args, **kwargs):
            entry = Path(argv[1])
            entries.append((entry, entry.stat().st_ino))
            assert all(
                not path.stat().st_mode & 0o222
                for path in [instance.private, *instance.package.parent.rglob("*")]
            )

        def close(self):
            pass

    monkeypatch.setattr(runtime.shutil, "copytree", copy)
    monkeypatch.setattr(runtime, "Client", Client)
    try:
        for _ in range(3):
            with runtime.session(node.parent, {}, pool=instance):
                pass
            assert instance.package.exists()
            assert not instance.package.stat().st_mode & 0o222
        assert (
            entries == [(instance.package / "langserver.index.js", entries[0][1])] * 3
        )
        assert len(copied) == 1, "Collection made multiple installation copies"
        entry = entries[0][0]
    finally:
        instance.close()
    assert not entry.exists()


@pytest.mark.parametrize("mutation", ["bytes", "mode", "inode", "flags"])
@pytest.mark.skipif(
    os.name == "nt", reason="Pool fixture uses a POSIX shell Node probe"
)
def test_between_session_drift_is_rejected_before_next_client(
    pool, monkeypatch, mutation
):
    if mutation == "flags" and sys.platform != "darwin":
        pytest.skip("Real st_flags mutation requires macOS")
    instance, node = pool
    launches = []

    class Client:
        def __init__(self, *args, **kwargs):
            launches.append(args)

        def close(self):
            pass

    monkeypatch.setattr(runtime, "Client", Client)
    with runtime.session(node.parent, {}, pool=instance):
        pass
    entry = Path(launches[0][0][1])
    assert entry.exists(), "The first session discarded the collection's copy"
    mode = entry.stat().st_mode
    try:
        if mutation == "flags":
            os.chflags(entry, entry.stat().st_flags | stat.UF_IMMUTABLE)
        elif mutation == "mode":
            entry.chmod(mode ^ 0o100)
        elif mutation == "inode":
            entry.parent.chmod(entry.parent.stat().st_mode | 0o200)
            replacement = entry.with_name("replacement")
            replacement.write_bytes(entry.read_bytes())
            replacement.chmod(mode)
            os.replace(replacement, entry)
        else:
            entry.chmod(mode | 0o200)
            entry.write_bytes(b"replaced")
            entry.chmod(mode)
        with pytest.raises(runtime.InstallError, match="changed during collection"):
            with runtime.session(node.parent, {}, pool=instance):
                pass
        assert len(launches) == 1, "Drift reached the next Client"
    finally:
        if hasattr(os, "chflags") and entry.exists():
            os.chflags(entry, 0)


def shared_typeshed(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    typeshed = tmp_path / "typeshed"
    stdlib = typeshed / "stdlib"
    (stdlib / "collections").mkdir(parents=True)
    (stdlib / "typing.pyi").write_text(
        "from collections.abc import MutableMapping as MutableMapping\n",
        encoding="utf-8",
    )
    (stdlib / "collections/abc.pyi").write_text(
        "from _collections_abc import MutableMapping as MutableMapping\n",
        encoding="utf-8",
    )
    (stdlib / "_collections_abc.pyi").write_text(
        "class MutableMapping: pass\n", encoding="utf-8"
    )
    return root, typeshed, stdlib


def test_shared_typeshed_capture_is_unique_across_sessions(tmp_path, monkeypatch):
    root, typeshed, stdlib = shared_typeshed(tmp_path)
    original = (stdlib / "typing.pyi").as_uri()
    (root / "demo.py").write_text("def f(): pass\n", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    plan = output / "plan.json"
    dump(
        {
            "snapshot_id": "frozen",
            "definitions": [
                {
                    "path": "demo.py",
                    "positions": {"utf-16": {"line": 0, "character": 4}},
                }
            ]
            * 2,
        },
        plan,
    )

    class Peer:
        stderr = []

        def request(self, method, params):
            if method == "initialize":
                return {"capabilities": {"callHierarchyProvider": True}}
            if method == "textDocument/prepareCallHierarchy":
                return [{"name": "f"}]
            return [{"to": {"uri": original}}]

        def notify(self, method, params):
            pass

    @contextmanager
    def session(*args, **kwargs):
        yield Peer(), {"typeshed": str(typeshed)}

    monkeypatch.setattr(transport, "session", session)
    monkeypatch.setattr(transport, "SESSION_DEFINITIONS", 1)
    detail = transport.collect(plan, root, {}, output)
    assert len(detail["sessions"]) == 2
    files = load(output / "external-targets.json")
    assert set(files) == {
        (stdlib / relative).as_uri()
        for relative in ("typing.pyi", "collections/abc.pyi", "_collections_abc.pyi")
    }
    assert proved_reexport(files, "typing", "MutableMapping", "_collections_abc")
    responses = [
        json.loads(line)
        for line in (output / "responses.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [
        row["result"][0]["to"]["uri"]
        for row in responses
        if row.get("method") == "callHierarchy/outgoingCalls"
    ] == [original, original]

    # Repeated captures still check both declarations and their proof bytes.
    capture = Capture(root, {})
    capture.files = files
    for relative in ("typing.pyi", "collections/abc.pyi", "_collections_abc.pyi"):
        path = stdlib / relative
        raw = path.read_bytes()
        try:
            path.write_bytes(raw + b"# changed\n")
            with pytest.raises(ValueError, match="input_changed_during_run"):
                capture.item({"uri": original}, {"typeshed": str(typeshed)})
        finally:
            path.write_bytes(raw)
    # A second physical file for the same module must still invalidate proof.
    files["another-file"] = dict(files[(stdlib / "collections/abc.pyi").as_uri()])
    assert not proved_reexport(files, "typing", "MutableMapping", "_collections_abc")


def test_semantic_exports_are_independent_of_session_size(
    prepared, tmp_path, monkeypatch
):
    project, _, _, _ = prepared
    (project / "demo.py").write_text(
        "from collections.abc import MutableMapping\n"
        "def dict_one(d: dict[str, int]): return d.setdefault('one', 1)\n"
        "def map_one(kwargs: MutableMapping[str, int]): return kwargs.setdefault('one', 1)\n"
        "def dict_two(d: dict[str, int]): return d.setdefault('two', 2)\n"
        "def map_two(kwargs: MutableMapping[str, int]): return kwargs.setdefault('two', 2)\n",
        encoding="utf-8",
    )
    configure(project)

    def stable(value):
        if isinstance(value, dict):
            return {
                key: stable(item)
                for key, item in value.items()
                if key not in {"created_at", "index_version"}
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    exports = []
    for size, sessions in ((1, 4), (10**9, 1)):
        monkeypatch.setattr(transport, "SESSION_DEFINITIONS", size)
        output = tmp_path / f"out-{size}"
        metadata, directory = ArcGraphIndexer(project, output).build()
        assert metadata.toolchain_status["semantic_core"]["status"] == "active"
        generation = generation_directory(directory)
        assert len(load(generation / "transport.json")["sessions"]) == sessions
        exported = GraphStoreReader.from_current(output).export_jsonl()
        exports.append(
            {
                name: [
                    stable(json.loads(line))
                    for line in (exported / f"{name}.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                for name in ("edges", "semantic_facts")
            }
        )
    assert exports[0] == exports[1]
    for name in ("dict_one", "dict_two", "map_one", "map_two"):
        edges = [e for e in exports[0]["edges"] if e["source"] == f"fn:demo.{name}"]
        assert any(
            e["target"] == "extsym:collections.abc.MutableMapping.setdefault"
            and e["resolution"]["status"] == "resolved"
            and any(v["kind"] == "type_inference" for v in e["evidence"])
            for e in edges
        ), name
        if name.startswith("dict_"):
            assert any(
                e["target"] == "extsym:builtins.dict.setdefault"
                and e["resolution"]["status"] == "conflict"
                for e in edges
            ), name
