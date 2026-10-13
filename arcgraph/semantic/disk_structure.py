"""Full structural generations with immutable records read from a checked journal."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import shutil
import tempfile
from types import MappingProxyType
from typing import Any
from pydantic import PrivateAttr

from .journal import intern_keys, open_artifact
from .structure_provider import StructuralGeneration, StructuralRecord
from .contract import digest


@dataclass(frozen=True, init=False, eq=False)
class DiskRecords(Sequence):
    def __init__(self, path):
        offsets, identities = [], {}
        # Project from a private copy; replacement of the source journal cannot
        # alter the records between validation and projection.
        handle = tempfile.TemporaryFile(mode="w+b")
        with open_artifact(path, "rb") as source:
            shutil.copyfileobj(source, handle)
        handle.seek(0)
        checksum = hashlib.sha256()
        for block in iter(lambda: handle.read(1048576), b""):
            checksum.update(block)
        handle.seek(0)
        try:
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
        except BaseException:
            handle.close()
            raise
        for key, value in {
            "path": path,
            "handle": handle,
            "expected_digest": checksum.hexdigest(),
            "offsets": tuple(offsets),
            "identities": MappingProxyType(identities),
            "header": header,
            "by_id": RecordMap(self),
        }.items():
            object.__setattr__(self, key, value)

    def byte_digest(self):
        checksum = hashlib.sha256()
        with open_artifact(self.path, "rb") as handle:
            for block in iter(lambda: handle.read(1048576), b""):
                checksum.update(block)
        value = checksum.hexdigest()
        if value != self.expected_digest:
            raise ValueError("structural journal changed after capture")
        return value

    def __len__(self):
        return len(self.offsets)

    def at(self, offset):
        self.handle.seek(offset)
        return StructuralRecord.model_validate(
            intern_keys(json.loads(self.handle.readline()))
        )

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self.at(offset) for offset in self.offsets[index])
        return self.at(self.offsets[index])

    def __iter__(self):
        for offset in self.offsets:
            yield self.at(offset)


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
