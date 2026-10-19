"""Exercise the real Windows URI parser branch without a Windows filesystem."""

import json
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from arcgraph.semantic import external_targets, uri_paths
from arcgraph.semantic.contract import digest
from arcgraph.semantic.reuse import rebase
from arcgraph.semantic.snapshot import sha
from arcgraph.tests.test_batch30_review_fifth_conditions import lookup


@pytest.fixture
def windows_paths(monkeypatch):
    # Replace only this module's bindings; do not change pathlib's host factory
    # or the process-wide os.name (pytest still needs native filesystem paths).
    monkeypatch.setattr(external_targets, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(external_targets, "Path", PureWindowsPath)


def registry_item(uri):
    text = "def target(): return 1\n"
    row = dict(
        origin="dependency",
        relative="example.py",
        module="example",
        text=text,
        text_digest=sha(text.encode()),
    )
    files = {uri: row}
    registry = external_targets.Registry(files, digest(files))
    item = dict(
        uri=uri,
        name="target",
        selectionRange=dict(
            start=dict(line=0, character=4), end=dict(line=0, character=10)
        ),
    )
    records = SimpleNamespace(
        generation_id="generation",
        producer_id="producer",
        snapshot_id="snapshot",
        view=SimpleNamespace(defs={}),
    )
    return registry, item, records, row


def test_windows_frozen_uri_keeps_target_branch_mapping(windows_paths):
    assert external_targets.file_path("file:///frozen/stdlib/example.pyi") is None
    text = "import sys\nclass Client:\n if sys.version_info >= (3, 13):\n  def ping(self): return 1\n else:\n  def ping(self): return 2\n"
    assert lookup(text, 3, "3.13") is not None
    assert lookup(text, 5, "3.13") is None


@pytest.mark.parametrize(
    "uri", ["file:///frozen/stdlib/example.pyi", "file:///C:/frozen/example.py"]
)
def test_windows_registry_uses_one_key_for_lookup_and_admission(windows_paths, uri):
    registry, item, records, _ = registry_item(uri)
    target = registry.lookup(item, "utf-16", records)
    assert target is not None
    assert target.id == "extsym:example.target"
    run = SimpleNamespace(
        id="generation",
        snapshot=SimpleNamespace(id="snapshot"),
        producer=SimpleNamespace(
            id="producer",
            configuration=[("external_capture", json.dumps(registry.checksum))],
        ),
        mapping=(),
        answers=(),
    )
    assert registry.admitted(run, records.view)
    assert registry.row(uri + "-different") is None


def test_windows_drive_aliases_deduplicate_and_reject_changed_content(windows_paths):
    uri = "file:///C:/Root/example.py"
    registry, item, records, row = registry_item(uri)
    alias = "file:///c%3A/root/EXAMPLE.py"
    assert external_targets.file_path(alias) == PureWindowsPath("C:/Root/example.py")
    target = registry.lookup({**item, "uri": alias}, "utf-16", records)
    assert target is not None
    assert (
        registry.lookup(
            {**item, "uri": "file:///D:/Root/example.py"}, "utf-16", records
        )
        is None
    )
    capture = external_targets.Capture.__new__(external_targets.Capture)
    capture.files, capture.paths = {}, {}
    capture._store(uri, row)
    capture._store(alias, row)
    assert len(capture.files) == 1
    with pytest.raises(ValueError, match="input_changed_during_run"):
        capture._store(alias, {**row, "text_digest": "changed"})
    capture._store("file:///D:/Root/example.py", row)
    assert len(capture.files) == 2


def test_drive_shaped_root_keeps_posix_case_rules(monkeypatch):
    monkeypatch.setattr(uri_paths, "os", SimpleNamespace(name="posix"), raising=False)
    original = "file:///C:/repo"
    other = "file:///c%3A/repo/a.py"
    assert rebase(other, [(original, "file:///new")]) == other
    assert rebase(original + "/A.py", [(original, "file:///new")]) == "file:///new/A.py"
