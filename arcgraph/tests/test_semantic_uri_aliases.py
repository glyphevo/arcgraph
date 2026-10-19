"""One physical external file keeps one capture across native URI spellings."""

from types import SimpleNamespace

import pytest

from arcgraph.semantic.contract import digest
from arcgraph.semantic.external_targets import Capture, Registry


@pytest.fixture
def captured(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    environment = tmp_path / "environment"
    site = environment / "Lib/site-packages"
    site.mkdir(parents=True)
    path = site / "dep.py"
    path.write_text("def target(): return 1\n", encoding="utf-8", newline="\n")
    capture = Capture(root, {"venvPath": str(tmp_path), "venv": "environment"})
    item = {
        "uri": path.as_uri(),
        "name": "target",
        "selectionRange": {
            "start": {"line": 0, "character": 4},
            "end": {"line": 0, "character": 10},
        },
    }
    capture.item(item, {})
    return capture, item, path


def test_capture_deduplicates_equivalent_native_uris_and_still_checks_bytes(captured):
    capture, item, path = captured
    alias = {**item, "uri": item["uri"].replace("dep.py", "%64ep.py")}
    capture.item(alias, {})
    assert len(capture.files) == 1
    path.write_text("def target(): return 2\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="input_changed_during_run"):
        capture.item(alias, {})


def test_registry_maps_an_equivalent_uri_to_the_captured_definition(captured):
    capture, item, _ = captured
    registry = Registry(capture.files, digest(capture.files))
    alias = {**item, "uri": item["uri"].replace("dep.py", "%64ep.py")}
    records = SimpleNamespace(
        generation_id="generation",
        producer_id="producer",
        snapshot_id="snapshot",
        view=SimpleNamespace(defs={}),
    )
    target = registry.lookup(alias, "utf-16", records)
    assert target is not None
    assert target.id == "extsym:dep.target"
    assert registry.lookup(item, "utf-16", records) == target


def test_registry_rejects_conflicting_aliases_even_with_a_valid_capture_checksum(
    captured,
):
    capture, item, _ = captured
    files = dict(capture.files)
    row = next(iter(files.values()))
    files[item["uri"].replace("dep.py", "%64ep.py")] = {**row, "module": "other"}
    with pytest.raises(ValueError, match="conflicting external file URI aliases"):
        Registry(files, digest(files))
