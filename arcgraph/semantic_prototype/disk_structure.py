"""Full structural generations with immutable records read from a checked journal."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any
from pydantic import PrivateAttr

from .staged import intern_keys
from .structure_provider import StructuralGeneration, StructuralRecord
from .contract import digest


@dataclass(frozen=True, init=False, eq=False)
class DiskRecords(Sequence):
    def __init__(self, path):
        offsets, identities = [], {}
        with path.open(encoding="utf-8") as handle:
            header = json.loads(handle.readline())
            while True:
                offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                record = StructuralRecord.model_validate(intern_keys(json.loads(line)))
                if record.id in identities:
                    raise ValueError("duplicate structural identity")
                offsets.append(offset)
                identities[record.id] = offset
        for key, value in {
            "path": path,
            "offsets": tuple(offsets),
            "identities": MappingProxyType(identities),
            "header": header,
            "by_id": RecordMap(self),
        }.items():
            object.__setattr__(self, key, value)

    def byte_digest(self):
        checksum = hashlib.sha256()
        with self.path.open("rb") as handle:
            for block in iter(lambda: handle.read(1048576), b""):
                checksum.update(block)
        return checksum.hexdigest()

    def __len__(self):
        return len(self.offsets)

    def at(self, offset):
        with self.path.open(encoding="utf-8") as handle:
            handle.seek(offset)
            return StructuralRecord.model_validate(
                intern_keys(json.loads(handle.readline()))
            )

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self.at(offset) for offset in self.offsets[index])
        return self.at(self.offsets[index])

    def __iter__(self):
        with self.path.open(encoding="utf-8") as handle:
            handle.readline()
            for line in handle:
                yield StructuralRecord.model_validate(intern_keys(json.loads(line)))


@dataclass(frozen=True, eq=False)
class RecordMap(Mapping):
    records: DiskRecords

    def __len__(self):
        return len(self.records)

    def __iter__(self):
        return iter(self.records.identities)

    def __getitem__(self, identity):
        return self.records.at(self.records.identities[identity])


class DiskGeneration(StructuralGeneration):
    # Private, serialization-compatible representation. checked() still validates
    # every record, complete/count/digest, owners, envelopes and all nested spans.
    records: Any
    _receipt: tuple | None = PrivateAttr(default=None)

    def checked(self):
        try:
            return self._checked()
        except OSError as exc:
            raise ValueError("structural journal unavailable") from exc

    def _checked(self):
        if not isinstance(self.records, DiskRecords):
            raise ValueError("disk record sequence required")
        token = (
            digest(self.model_dump(mode="json", exclude={"records"})),
            self.records.byte_digest(),
        )
        if token == self._receipt:
            return self
        result = super().checked()
        if self.records.byte_digest() != token[1]:
            raise ValueError("structural journal changed during validation")
        # Reuse validation only for identical metadata and identical file bytes.
        # The offset/index sequence is immutable; never use mtimes as proof.
        self._receipt = token
        return result


def read(path):
    records = DiskRecords(path)
    return DiskGeneration.model_validate(
        {**records.header, "records": records}
    ).checked()
