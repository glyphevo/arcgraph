"""Build-pin records, active indexes, and conservative reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import uuid
from typing import Any

from pydantic import ValidationError

from arcgraph.change.contracts import (
    BuildPin,
    CHANGE_CONTRACT_VERSION,
    PinReleaseEvent,
)
from arcgraph.change.errors import (
    ChangeStoreCorrupt,
    ChangeStoreNotFound,
    OutputContainmentError,
)
from arcgraph.change.paths import resolve_under_root
from arcgraph.change.store import atomic_write_json, read_json_object
from arcgraph.core.schemas import SCHEMA_VERSION


class BuildPinManager:
    """Manage immutable pins and mutable active indexes under ``output/pins``.

    The manager deliberately never deletes a record.  Reconciliation only
    reports possible orphan state; an explicit archive/release flow decides
    whether an active pin may be released after all stores are healthy.
    """

    def __init__(self, output_dir: Path, *, repo_id: str) -> None:
        self.output_dir = output_dir.resolve()
        self.repo_id = repo_id
        self.root = resolve_under_root(self.output_dir, "pins")

    def make_pin(
        self,
        *,
        plan_id: str,
        plan_revision: int,
        plan_content_digest: str,
        index_version: str,
        build_relative_path: str,
    ) -> BuildPin:
        return BuildPin(
            repo_id=self.repo_id,
            pin_id=f"pin-{uuid.uuid4().hex}",
            plan_id=plan_id,
            plan_revision=plan_revision,
            plan_content_digest=plan_content_digest,
            index_version=index_version,
            build_relative_path=build_relative_path,
        )

    def create(self, pin: BuildPin) -> BuildPin:
        self._require_repo(pin.repo_id)
        self._validate_build(pin)
        record_path = self._path("records", f"{pin.pin_id}.json")
        if record_path.exists():
            existing = self.get(pin.pin_id)
            if _pin_binding(existing) != _pin_binding(pin):
                raise ChangeStoreCorrupt("pin id already identifies another record")
            # ``created_at`` is immutable history rather than activation
            # authority.  A retry necessarily constructs a fresh model clock,
            # so it must reuse the original durable record instead of treating
            # that timestamp difference as a conflicting pin.
            pin = existing
        else:
            self._write_json(record_path, pin.model_dump(mode="json"))

        # Record first, then make it active.  A failed activation can create an
        # orphan record but never a visible plan lacking a durable pin record.
        payload = self._read_active_payload()
        pins_by_index_version = self._active_pin_map(payload)
        pin_ids = set(pins_by_index_version.get(pin.index_version, []))
        pin_ids.add(pin.pin_id)
        pins_by_index_version[pin.index_version] = sorted(pin_ids)
        self._write_active_payload(pins_by_index_version)
        return pin

    def get(self, pin_id: str) -> BuildPin:
        try:
            payload = read_json_object(
                self._path("records", f"{pin_id}.json"), root=self.root
            )
        except ChangeStoreNotFound:
            raise
        self._validate_header(payload)
        try:
            pin = BuildPin.model_validate(payload)
        except ValidationError as exc:
            raise ChangeStoreCorrupt(
                "pin record does not match BuildPin contract"
            ) from exc
        self._require_repo(pin.repo_id)
        if pin.pin_id != pin_id:
            raise ChangeStoreCorrupt("pin record does not match its file name")
        return pin

    def active_pin_ids(self) -> set[str]:
        return {
            pin_id
            for pin_ids in self._active_pin_map(self._read_active_payload()).values()
            for pin_id in pin_ids
        }

    def active_pins(self) -> list[BuildPin]:
        return [self.get(pin_id) for pin_id in sorted(self.active_pin_ids())]

    def list_records(self) -> list[BuildPin]:
        records_root = self._path("records")
        paths = self._json_record_paths(records_root, label="pin record")
        return [self.get(path.stem) for path in paths]

    def list_release_events(self) -> list[PinReleaseEvent]:
        releases_root = self._path("releases")
        paths = self._json_record_paths(releases_root, label="pin release")
        events: list[PinReleaseEvent] = []
        for path in paths:
            payload = read_json_object(path, root=self.root)
            self._validate_header(payload)
            try:
                event = PinReleaseEvent.model_validate(payload)
            except ValidationError as exc:
                raise ChangeStoreCorrupt(
                    "pin release record does not match PinReleaseEvent"
                ) from exc
            if path.name != f"{event.release_id}.json":
                raise ChangeStoreCorrupt(
                    "pin release record does not match its file name"
                )
            events.append(event)
        return events

    def is_build_pinned(self, index_version: str) -> bool:
        return bool(
            self._active_pin_map(self._read_active_payload()).get(index_version, [])
        )

    def release(
        self,
        pin_id: str,
        *,
        reason: str,
        store_healthy: bool,
    ) -> PinReleaseEvent:
        """Release only an active pin after the caller proves store health."""

        if not store_healthy:
            raise ChangeStoreCorrupt(
                "refusing pin release while the state store is unhealthy"
            )
        pin = self.get(pin_id)
        payload = self._read_active_payload()
        pins_by_index_version = self._active_pin_map(payload)
        active_pin_ids = pins_by_index_version.get(pin.index_version, [])
        if pin_id not in active_pin_ids:
            raise ChangeStoreCorrupt(
                "refusing to release a pin absent from its active index"
            )
        matching_events = [
            event for event in self.list_release_events() if event.pin_id == pin_id
        ]
        if len(matching_events) > 1:
            raise ChangeStoreCorrupt("pin has multiple immutable release events")
        if matching_events:
            event = matching_events[0]
            if event.plan_id != pin.plan_id or event.plan_revision != pin.plan_revision:
                raise ChangeStoreCorrupt(
                    "pin release event does not bind the immutable pin record"
                )
        else:
            event = PinReleaseEvent(
                repo_id=self.repo_id,
                release_id=f"release-{uuid.uuid4().hex}",
                pin_id=pin_id,
                plan_id=pin.plan_id,
                plan_revision=pin.plan_revision,
                reason=reason,
            )
            self._write_json(
                self._path("releases", f"{event.release_id}.json"),
                event.model_dump(mode="json"),
            )
        # If the prior call wrote its immutable event but failed before the
        # active projection update, reuse that event and complete the same
        # release.  This preserves a recoverable release-pending state without
        # creating duplicate release authority.
        remaining = [value for value in active_pin_ids if value != pin_id]
        if remaining:
            pins_by_index_version[pin.index_version] = remaining
        else:
            pins_by_index_version.pop(pin.index_version, None)
        self._write_active_payload(pins_by_index_version, updated_at=event.released_at)
        return event

    def reconcile(
        self,
        *,
        plan_exists_fn: Callable[[BuildPin], bool] | None = None,
    ) -> dict[str, Any]:
        """Report divergence without automatic destructive repair."""

        records = self.list_records()
        record_ids = {pin.pin_id for pin in records}
        active_ids = self.active_pin_ids()
        release_events = self.list_release_events()
        released_ids = {event.pin_id for event in release_events}
        if len(released_ids) != len(release_events):
            raise ChangeStoreCorrupt("pin has multiple immutable release events")
        dangling_active = sorted(active_ids - record_ids)
        inactive_records = sorted(record_ids - active_ids - released_ids)
        active_pins = [self.get(pin_id) for pin_id in sorted(active_ids & record_ids)]
        orphan_active = sorted(
            pin.pin_id
            for pin in active_pins
            if plan_exists_fn is not None and not plan_exists_fn(pin)
        )
        return {
            "status": (
                "attention_required"
                if dangling_active or inactive_records or orphan_active
                else "consistent"
            ),
            "active_pin_ids": sorted(active_ids),
            "dangling_active_pin_ids": dangling_active,
            "inactive_record_pin_ids": inactive_records,
            "released_pin_ids": sorted(record_ids & released_ids),
            "orphan_active_pin_ids": orphan_active,
            "released": [],
            "automatic_release": False,
        }

    def health_check(self) -> None:
        """Validate active pin authority before a lifecycle write or export."""

        if not self.root.exists():
            return
        if self.root.is_symlink() or not self.root.is_dir():
            raise ChangeStoreCorrupt("pin store root is unsafe")
        for entry in self.root.iterdir():
            if entry.is_symlink():
                raise ChangeStoreCorrupt("pin store root contains a symlink")
            if entry.is_file() and entry.name == "active.json":
                continue
            if entry.is_dir() and entry.name in {"records", "releases"}:
                continue
            raise ChangeStoreCorrupt("pin store root contains an unsafe entry")
        records = {pin.pin_id: pin for pin in self.list_records()}
        releases = self.list_release_events()
        released_pin_ids: set[str] = set()
        active_map = self._active_pin_map(self._read_active_payload())
        for index_version, pin_ids in active_map.items():
            for pin_id in pin_ids:
                pin = records.get(pin_id)
                if pin is None:
                    raise ChangeStoreCorrupt(
                        "active pin index references a missing immutable pin record"
                    )
                if pin.index_version != index_version:
                    raise ChangeStoreCorrupt(
                        "active pin index version does not match immutable pin record"
                    )
                self._validate_build(pin)
        for event in releases:
            pin = records.get(event.pin_id)
            if (
                pin is None
                or pin.plan_id != event.plan_id
                or pin.plan_revision != event.plan_revision
            ):
                raise ChangeStoreCorrupt(
                    "pin release event does not bind an immutable pin record"
                )
            if event.pin_id in released_pin_ids:
                raise ChangeStoreCorrupt("pin has multiple immutable release events")
            released_pin_ids.add(event.pin_id)

    def _validate_build(self, pin: BuildPin) -> None:
        parts = Path(pin.build_relative_path).parts
        if parts != ("builds", pin.index_version):
            raise ChangeStoreCorrupt("pin build path must be builds/<index_version>")
        try:
            # Rejects a symlink at builds/ or builds/<index_version> as well.
            build_dir = resolve_under_root(self.output_dir, *parts)
        except OutputContainmentError as exc:
            raise ChangeStoreCorrupt(
                "pin build path escapes output builds root"
            ) from exc
        sqlite_path = build_dir / "index.sqlite"
        if sqlite_path.is_symlink() or not sqlite_path.is_file():
            raise ChangeStoreCorrupt("cannot pin a missing or unsafe graph build")

    def _active_path(self) -> Path:
        return self._path("active.json")

    def _read_active_payload(self) -> dict[str, Any]:
        """Read the one atomic, rebuildable active-pin projection."""

        path = self._active_path()
        if not path.exists():
            legacy_directory = self._path("active")
            if legacy_directory.exists():
                raise ChangeStoreCorrupt(
                    "legacy per-build active pin index is unsupported; rebuild active.json"
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "change_contract_version": CHANGE_CONTRACT_VERSION,
                "repo_id": self.repo_id,
                "pins_by_index_version": {},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        payload = read_json_object(path, root=self.root)
        self._validate_header(payload)
        self._active_pin_map(payload)
        return payload

    def _active_pin_map(self, payload: dict[str, Any]) -> dict[str, list[str]]:
        values = payload.get("pins_by_index_version")
        if not isinstance(values, dict):
            raise ChangeStoreCorrupt("active pin index lacks pins_by_index_version")
        normalized: dict[str, list[str]] = {}
        for index_version, pin_ids in values.items():
            self._validate_index_version(index_version)
            if (
                not isinstance(pin_ids, list)
                or not pin_ids
                or not all(isinstance(pin_id, str) and pin_id for pin_id in pin_ids)
            ):
                raise ChangeStoreCorrupt("active pin index has invalid pin ids")
            for pin_id in pin_ids:
                self._validate_pin_id(pin_id)
            if pin_ids != sorted(set(pin_ids)):
                raise ChangeStoreCorrupt(
                    "active pin index pin ids must be stable and unique"
                )
            normalized[index_version] = list(pin_ids)
        return normalized

    def _write_active_payload(
        self,
        pins_by_index_version: dict[str, list[str]],
        *,
        updated_at: datetime | None = None,
    ) -> None:
        normalized = {
            index_version: sorted(set(pin_ids))
            for index_version, pin_ids in sorted(pins_by_index_version.items())
        }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "change_contract_version": CHANGE_CONTRACT_VERSION,
            "repo_id": self.repo_id,
            "pins_by_index_version": normalized,
            "updated_at": (updated_at or datetime.now(timezone.utc)).isoformat(),
        }
        self._active_pin_map(payload)
        self._write_json(self._active_path(), payload)

    @staticmethod
    def _validate_index_version(index_version: Any) -> None:
        if (
            not isinstance(index_version, str)
            or not index_version
            or "/" in index_version
            or "\\" in index_version
            or index_version in {".", ".."}
        ):
            raise ChangeStoreCorrupt("active pin index has an unsafe index version")

    @staticmethod
    def _validate_pin_id(pin_id: str) -> None:
        if "/" in pin_id or "\\" in pin_id or "\x00" in pin_id or pin_id in {".", ".."}:
            raise ChangeStoreCorrupt("active pin index has an unsafe pin id")

    def _path(self, *components: str) -> Path:
        return resolve_under_root(self.root, *components)

    def _json_record_paths(self, directory: Path, *, label: str) -> list[Path]:
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise ChangeStoreCorrupt(f"{label} directory is unsafe")
        paths = sorted(directory.iterdir(), key=lambda item: item.name)
        if any(
            path.is_symlink() or not path.is_file() or path.suffix != ".json"
            for path in paths
        ):
            raise ChangeStoreCorrupt(f"{label} directory contains an unsafe entry")
        return paths

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self._validate_header(payload)
        atomic_write_json(path, payload, root=self.root)

    def _validate_header(self, payload: dict[str, Any]) -> None:
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ChangeStoreCorrupt("pin record schema version is unsupported")
        if payload.get("change_contract_version") != CHANGE_CONTRACT_VERSION:
            raise ChangeStoreCorrupt("pin record contract version is unsupported")
        self._require_repo(payload.get("repo_id"))

    def _require_repo(self, repo_id: Any) -> None:
        if repo_id != self.repo_id:
            raise ChangeStoreCorrupt("pin record repo_id does not match")


def _pin_binding(pin: BuildPin) -> tuple[str, str, int, str, str, str, str]:
    """Return the immutable authority fields used to resume activation."""

    return (
        pin.repo_id,
        pin.pin_id,
        pin.plan_id,
        pin.plan_revision,
        pin.plan_content_digest,
        pin.index_version,
        pin.build_relative_path,
    )
