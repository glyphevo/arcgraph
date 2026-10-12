"""Checked sequential journals for memory-separated semantic stages."""

import hashlib
import json
import os
import sys


def intern_keys(value):
    if isinstance(value, dict):
        return {sys.intern(key): intern_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [intern_keys(item) for item in value]
    return sys.intern(value) if isinstance(value, str) else value


def wire_line(handle, value):
    line = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    )
    handle.write(line)
    return line.encode()


def dump(value, path):
    pending = path.with_suffix(path.suffix + ".part")
    with pending.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)


def load(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_structure(bundle, path):
    bundle.checked()
    pending = path.with_suffix(".part")
    with pending.open("w", encoding="utf-8") as handle:
        wire_line(handle, bundle.model_dump(mode="json", exclude={"records"}))
        for record in bundle.records:
            wire_line(handle, record.model_dump(mode="json"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)


class JournalReplay:
    def __init__(self, path, snapshot_id):
        self.handle = path.open(encoding="utf-8")
        if json.loads(next(self.handle)) != {"snapshot": snapshot_id}:
            self.handle.close()
            raise ValueError("journal snapshot mismatch")
        self.checksum = hashlib.sha256()
        self.count = 0

    def notify(self, method, params):
        pass

    def request(self, method, params, timeout=30):
        from .lsp import LspError

        row_bytes = next(self.handle)
        row = json.loads(row_bytes)
        if row.get("method") != method or row.get("params") != params:
            raise ValueError("journal request mismatch or incomplete run")
        self.checksum.update(row_bytes.encode())
        self.count += 1
        if "error" in row:
            if row["error"]["kind"] == "timeout":
                raise TimeoutError(row["error"]["message"])
            raise LspError(row["error"]["message"])
        return row["result"]

    def close(self):
        try:
            footer = json.loads(next(self.handle))
            if footer != {
                "complete": True,
                "count": self.count,
                "checksum": self.checksum.hexdigest(),
            } or self.handle.read(1):
                raise ValueError("journal truncated, corrupt or has unused responses")
        finally:
            self.handle.close()
