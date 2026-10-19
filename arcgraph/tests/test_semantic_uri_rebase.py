"""Native URI spellings must not bind reused answers to a previous workspace."""

import hashlib
import json
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from arcgraph.semantic import external_targets, uri_paths
from arcgraph.semantic.contract import digest
from arcgraph.semantic.journal import JournalReplay, wire_line
from arcgraph.semantic.reuse import Reuse, rebase
from arcgraph.semantic.runtime import initialize_params
from arcgraph.tests.project_environment_support import project_site_packages


@pytest.mark.parametrize(
    "uri,relative",
    [
        ("file:///c%3A/Old%20root/demo.py", "demo.py"),
        ("file:///C:/old%20ROOT/pkg/a%23%25.py", "pkg/a%23%25.py"),
        ("file://localhost/c%3a/Old%20root/demo.py", "demo.py"),
        ("file:///D:/Old%20root/demo.py", None),
        ("file:///C:/Old%20root-sibling/demo.py", None),
        ("https://example.test/C:/Old%20root/demo.py", None),
        ("file:///C:/Old%20root/../outside.py", None),
        ("file:///C:/Old%20root/demo.py?query", None),
        ("file://[", None),
    ],
)
def test_rebase_uses_windows_path_containment(uri, relative):
    replacement = "file:///D:/New%20root"
    expected = replacement + "/" + relative if relative else uri
    assert (
        rebase(uri, [("file:///C:/Old%20root", replacement)], windows=True) == expected
    )


def test_reused_windows_paths_are_rebound_for_every_capture(tmp_path):
    reuse = Reuse.__new__(Reuse)
    reuse.root = PureWindowsPath("C:/old/workspace")
    reuse.environment = reuse.root.parent / "environment"
    reuse.typeshed = PureWindowsPath("C:/old/backend/typeshed-fallback")
    definition = {"path": "demo.py"}
    reuse.responses = {
        digest(definition): [
            {"uri": "file:///c%3A/old/workspace/demo.py"},
            {"uri": "file:///c%3A/old/environment/dep.py"},
            {"uri": "file:///c%3A/old/backend/typeshed-fallback/stdlib/builtins.pyi"},
        ]
    }
    root = tmp_path / "new/workspace"
    typeshed = tmp_path / "backend/typeshed-fallback"
    rows = list(reuse.rows(definition, root, {"typeshed": str(typeshed)}))
    assert rows == [
        {"uri": (root / "demo.py").as_uri()},
        {"uri": (root.parent / "environment/dep.py").as_uri()},
        {"uri": (typeshed / "stdlib/builtins.pyi").as_uri()},
    ]


def test_replay_rebinds_encoded_windows_uris_and_keeps_original_checksum(tmp_path):
    root = tmp_path / "replayed workspace"
    old = PureWindowsPath("C:/Old root")
    old_item = {"uri": "file:///c%3A/Old%20root/demo.py", "name": "f"}
    new_item = {**old_item, "uri": (root / "demo.py").as_uri()}
    position = {"line": 0, "character": 4}
    rows = [
        {
            "method": "initialize",
            "params": initialize_params(old),
            "result": {"capabilities": {"callHierarchyProvider": True}},
        },
        {
            "method": "textDocument/prepareCallHierarchy",
            "params": {
                "textDocument": {"uri": (old / "demo.py").as_uri()},
                "position": position,
            },
            "result": [old_item],
        },
        {
            "method": "callHierarchy/outgoingCalls",
            "params": {"item": old_item},
            "result": [{"to": old_item}],
        },
    ]
    path = tmp_path / "responses.jsonl"
    checksum = hashlib.sha256()
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        wire_line(handle, {"snapshot": "frozen"})
        for row in rows:
            checksum.update(wire_line(handle, row))
        wire_line(
            handle,
            {"complete": True, "count": len(rows), "checksum": checksum.hexdigest()},
        )
    replay = JournalReplay(path, "frozen", root=root, windows=True)
    replay.request("initialize", initialize_params(root))
    assert replay.request(
        "textDocument/prepareCallHierarchy",
        {"textDocument": {"uri": new_item["uri"]}, "position": position},
    ) == [new_item]
    assert replay.request("callHierarchy/outgoingCalls", {"item": new_item}) == [
        {"to": new_item}
    ]
    replay.close()
    # Rebinding affects logical answers, never the bytes authenticated by close().
    assert (
        json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["checksum"]
        == checksum.hexdigest()
    )


@pytest.mark.parametrize(
    "original,uri,expected,windows",
    [
        (
            "file://server/share/old",
            "file://SERVER/share/OLD/a%20b.py",
            "file:///new/a%20b.py",
            True,
        ),
        (
            "file:///old",
            "file://localhost/old/%61%23%25.py",
            "file:///new/a%23%25.py",
            False,
        ),
        ("file:///old", "file:///OLD/a.py", "file:///OLD/a.py", False),
    ],
)
def test_rebinding_retains_unc_and_posix_path_rules(original, uri, expected, windows):
    assert rebase(uri, [(original, "file:///new")], windows=windows) == expected


@pytest.mark.parametrize(
    "layout", ["Lib/site-packages", "lib/python3.14/site-packages"]
)
def test_project_site_packages_uses_the_created_environment_layout(tmp_path, layout):
    site = tmp_path / layout
    site.mkdir(parents=True)
    assert project_site_packages(tmp_path) == site


def test_native_call_comparison_decodes_the_workspace_uri(tmp_path, monkeypatch):
    from arcgraph.tests import test_semantic_incremental as incremental

    root = "file:///C:/Old%20root"
    item = {"uri": "file:///c%3A/Old%20root/demo.py"}
    rows = [
        {"snapshot": "frozen"},
        {"method": "initialize", "params": {"rootUri": root}},
        {
            "method": "callHierarchy/outgoingCalls",
            "params": {"item": item},
            "result": [{"to": item}],
        },
    ]
    (tmp_path / "responses.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr(incremental, "generation_directory", lambda path: path)
    # Exercise the actual Windows parser, including stripping the URI drive slash.
    monkeypatch.setattr(external_targets, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(external_targets, "Path", PureWindowsPath)
    monkeypatch.setattr(uri_paths, "os", SimpleNamespace(name="nt"))
    assert incremental.native_calls(tmp_path, "demo.py") == [
        json.dumps([{"to": {"uri": "file:///workspace/demo.py"}}], sort_keys=True)
    ]
