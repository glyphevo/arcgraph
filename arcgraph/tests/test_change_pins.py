from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph.change.errors import ChangeStoreCorrupt
from arcgraph.change.pins import BuildPinManager
from arcgraph.core.cleanup import plan_arcgraph_output_cleanup


def _write_build(output_dir: Path, name: str) -> Path:
    build = output_dir / "builds" / name
    build.mkdir(parents=True)
    (build / "index.sqlite").write_bytes(b"sqlite")
    (build / "summary.json").write_text("{}", encoding="utf-8")
    return build


def _write_current(output_dir: Path, name: str) -> None:
    (output_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "index_version": name,
                "build_dir": f"builds/{name}",
            }
        ),
        encoding="utf-8",
    )


def test_active_pin_keeps_old_build_out_of_cleanup_candidates(tmp_path: Path) -> None:
    output = tmp_path / "output"
    old = _write_build(output, "old")
    _write_build(output, "current")
    _write_current(output, "current")
    pins = BuildPinManager(output, repo_id="repo")
    pin = pins.make_pin(
        plan_id="plan",
        plan_revision=1,
        plan_content_digest="digest",
        index_version="old",
        build_relative_path="builds/old",
    )
    pins.create(pin)

    plan = plan_arcgraph_output_cleanup(output, keep_builds=1)

    assert old.exists()
    assert "old" in plan.pinned_builds
    assert all(candidate.index_version != "old" for candidate in plan.candidates)


def test_release_refuses_when_state_store_is_not_healthy(tmp_path: Path) -> None:
    output = tmp_path / "output"
    _write_build(output, "current")
    pins = BuildPinManager(output, repo_id="repo")
    pin = pins.make_pin(
        plan_id="plan",
        plan_revision=1,
        plan_content_digest="digest",
        index_version="current",
        build_relative_path="builds/current",
    )
    pins.create(pin)

    with pytest.raises(ChangeStoreCorrupt):
        pins.release(pin.pin_id, reason="unsafe", store_healthy=False)
    assert pins.is_build_pinned("current")


def test_pin_release_recovers_after_its_event_was_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    _write_build(output, "current")
    pins = BuildPinManager(output, repo_id="repo")
    pin = pins.make_pin(
        plan_id="plan",
        plan_revision=1,
        plan_content_digest="digest",
        index_version="current",
        build_relative_path="builds/current",
    )
    pins.create(pin)
    original_write_active = pins._write_active_payload

    def fail_active_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated active pointer failure")

    monkeypatch.setattr(pins, "_write_active_payload", fail_active_write)
    with pytest.raises(OSError, match="simulated"):
        pins.release(pin.pin_id, reason="archive", store_healthy=True)
    written_event = pins.list_release_events()[0]
    assert pins.is_build_pinned("current")

    monkeypatch.setattr(pins, "_write_active_payload", original_write_active)
    recovered_event = pins.release(
        pin.pin_id,
        reason="archive retry",
        store_healthy=True,
    )

    assert recovered_event.release_id == written_event.release_id
    assert pins.list_release_events() == [written_event]
    assert not pins.is_build_pinned("current")
    reconciliation = pins.reconcile()
    assert reconciliation["status"] == "consistent"
    assert reconciliation["inactive_record_pin_ids"] == []
    assert reconciliation["released_pin_ids"] == [pin.pin_id]


def test_corrupt_pin_index_blocks_cleanup_instead_of_deleting_history(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    old = _write_build(output, "old")
    _write_build(output, "current")
    _write_current(output, "current")
    active = output / "pins" / "active.json"
    active.parent.mkdir(parents=True)
    active.write_text("not-json", encoding="utf-8")

    plan = plan_arcgraph_output_cleanup(output, keep_builds=1)

    assert old.exists()
    assert plan.candidates == []
    assert any("Pinned build" in warning for warning in plan.warnings)


def test_active_pin_pointer_must_reference_a_matching_immutable_record(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    old = _write_build(output, "old")
    _write_build(output, "current")
    _write_current(output, "current")
    pins = BuildPinManager(output, repo_id="repo")
    pin = pins.make_pin(
        plan_id="plan",
        plan_revision=1,
        plan_content_digest="digest",
        index_version="old",
        build_relative_path="builds/old",
    )
    pins.create(pin)
    active_path = output / "pins" / "active.json"
    active = json.loads(active_path.read_text(encoding="utf-8"))
    active["pins_by_index_version"]["old"] = ["missing-pin"]
    active_path.write_text(json.dumps(active), encoding="utf-8")

    plan = plan_arcgraph_output_cleanup(output, keep_builds=1)

    assert old.exists()
    assert plan.candidates == []
    assert any("Pinned build" in warning for warning in plan.warnings)


def test_missing_active_pin_pointer_blocks_cleanup_instead_of_dropping_history(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    old = _write_build(output, "old")
    _write_build(output, "current")
    _write_current(output, "current")
    pins = BuildPinManager(output, repo_id="repo")
    pin = pins.make_pin(
        plan_id="plan",
        plan_revision=1,
        plan_content_digest="digest",
        index_version="old",
        build_relative_path="builds/old",
    )
    pins.create(pin)
    (output / "pins" / "active.json").unlink()

    plan = plan_arcgraph_output_cleanup(output, keep_builds=1)

    assert old.exists()
    assert plan.candidates == []
    assert any("index is missing" in warning for warning in plan.warnings)


def test_pin_store_health_check_rejects_unrecognized_record_entries(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    _write_build(output, "current")
    pins = BuildPinManager(output, repo_id="repo")
    pin = pins.make_pin(
        plan_id="plan",
        plan_revision=1,
        plan_content_digest="digest",
        index_version="current",
        build_relative_path="builds/current",
    )
    pins.create(pin)
    (output / "pins" / "records" / "sidecar.txt").write_text(
        "unexpected",
        encoding="utf-8",
    )

    with pytest.raises(ChangeStoreCorrupt, match="unsafe entry"):
        pins.health_check()


def test_pin_store_health_check_rejects_unrecognized_root_entries(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    _write_build(output, "current")
    pins = BuildPinManager(output, repo_id="repo")
    pin = pins.make_pin(
        plan_id="plan",
        plan_revision=1,
        plan_content_digest="digest",
        index_version="current",
        build_relative_path="builds/current",
    )
    pins.create(pin)
    (output / "pins" / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ChangeStoreCorrupt, match="unsafe entry"):
        pins.health_check()
