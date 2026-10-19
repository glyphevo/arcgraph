"""Checked sequential journals for memory-separated semantic stages."""

import hashlib
import gzip
import base64
import json
import os
import shutil
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
    with open_artifact(path) as handle:
        value = json.load(handle)
    if isinstance(value, dict) and value.get("format") == "external-text-blobs/1":
        texts = value["texts"]
        for checksum, text in texts.items():
            if hashlib.sha256(text.encode()).hexdigest() != checksum:
                raise ValueError("external text blob checksum mismatch")
        return {
            uri: {**row, "text": texts[row["text_digest"]]}
            for uri, row in value["files"].items()
        }
    return value


def open_artifact(path, mode="rt"):
    with path.open("rb") as handle:
        compressed = handle.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else path.open
    kwargs = {"encoding": "utf-8"} if "b" not in mode else {}
    return opener(path, mode, **kwargs) if compressed else opener(mode, **kwargs)


def compact_generation(directory, snapshot, root):
    """Retain native replay and exact input bytes without a duplicate transcript."""
    files = load(directory / "external-targets.json")
    texts, rows = {}, {}
    for uri, row in files.items():
        checksum = row["text_digest"]
        text = row["text"]
        if hashlib.sha256(text.encode()).hexdigest() != checksum:
            raise ValueError("external text blob checksum mismatch")
        texts[checksum] = text
        rows[uri] = {key: value for key, value in row.items() if key != "text"}
    dump(
        {"format": "external-text-blobs/1", "texts": texts, "files": rows},
        directory / "external-targets.json",
    )
    seen = set()
    with (directory / "source-blobs.jsonl").open("w", encoding="utf-8") as handle:
        wire_line(handle, {"format": "source-blobs/1", "snapshot": snapshot.id})
        for file in snapshot.files:
            if file.raw_digest in seen:
                continue
            raw = (root / file.path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != file.raw_digest:
                raise ValueError("frozen source changed before storage")
            wire_line(
                handle,
                {
                    "digest": file.raw_digest,
                    "bytes": base64.b64encode(raw).decode("ascii"),
                },
            )
            seen.add(file.raw_digest)
        handle.flush()
        os.fsync(handle.fileno())
    # responses.jsonl contains every native request/result and handshake, with a
    # checked completion footer. didOpen text lives in exact source blobs; the
    # rest of this transport transcript is a duplicate or operational logging.
    (directory / "lsp.jsonl").unlink()
    for path in sorted(directory.iterdir()):
        if path.name in ("snapshot.json", "manifest.json") or path.suffix not in (
            ".json",
            ".jsonl",
        ):
            continue
        pending = path.with_suffix(path.suffix + ".compressed")
        with path.open("rb") as source, pending.open("wb") as output:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=output, mtime=0, compresslevel=1
            ) as zipped:
                shutil.copyfileobj(source, zipped, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)


def replay_sources(directory, destination):
    """Reconstruct a checked frozen workspace using only this generation."""
    from .contract import Snapshot
    from .snapshot import verify

    snapshot = Snapshot.model_validate(load(directory / "snapshot.json"))
    blobs = {}
    with open_artifact(directory / "source-blobs.jsonl") as handle:
        if json.loads(next(handle)) != {
            "format": "source-blobs/1",
            "snapshot": snapshot.id,
        }:
            raise ValueError("source blob snapshot mismatch")
        for line in handle:
            row = json.loads(line)
            raw = base64.b64decode(row["bytes"], validate=True)
            if (
                hashlib.sha256(raw).hexdigest() != row["digest"]
                or row["digest"] in blobs
            ):
                raise ValueError("source blob checksum mismatch or duplicate")
            blobs[row["digest"]] = raw
    if set(blobs) != {file.raw_digest for file in snapshot.files}:
        raise ValueError("source blob coverage mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    for file in snapshot.files:
        path = destination / file.path
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("unsafe replay source path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blobs[file.raw_digest])
    verify(snapshot, destination)
    return snapshot


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
    def __init__(self, path, snapshot_id, *, root=None):
        self.handle = open_artifact(path)
        if json.loads(next(self.handle)) != {"snapshot": snapshot_id}:
            self.handle.close()
            raise ValueError("journal snapshot mismatch")
        self.checksum = hashlib.sha256()
        self.count = 0
        self.initialize_count = 0
        self.initialize_params = None
        self.encoding = None
        self.replay_root = root.resolve().as_uri() if root is not None else None
        self.original_root = None

    def _rebase(self, value):
        if self.replay_root is None:
            return value
        if isinstance(value, list):
            return [self._rebase(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if (
                key in ("uri", "rootUri")
                and isinstance(item, str)
                and self.original_root is not None
            ):
                from .uri_paths import rebase_uri

                result[key] = rebase_uri(item, self.original_root, self.replay_root)
            else:
                result[key] = self._rebase(item)
        return result

    def notify(self, method, params):
        pass

    def request(self, method, params, timeout=30):
        from .lsp import LspError

        row_bytes = next(self.handle)
        row = json.loads(row_bytes)
        if method == "initialize" and self.replay_root is not None:
            self.original_root = row.get("params", {}).get("rootUri")
            if not isinstance(
                self.original_root, str
            ) or not self.original_root.startswith("file://"):
                raise ValueError("journal workspace URI missing")
        row = self._rebase(row)
        while method != "initialize" and row.get("method") == "initialize":
            # The semantic mapper replays one logical workspace, while the
            # collector used multiple physical sessions. Verify every handshake
            # before consuming it, including its bytes in the journal checksum.
            from .runtime import capabilities
            from .install import InstallError

            try:
                unit = capabilities(row.get("result"))
            except (InstallError, TypeError, AttributeError) as exc:
                raise ValueError("journal restarted handshake invalid") from exc

            if (
                row.get("params") != self.initialize_params
                or "error" in row
                or unit != self.encoding
            ):
                raise ValueError("journal restarted handshake mismatch")
            self.checksum.update(row_bytes.encode())
            self.count += 1
            self.initialize_count += 1
            row_bytes = next(self.handle)
            row = self._rebase(json.loads(row_bytes))
        if row.get("method") != method or row.get("params") != params:
            raise ValueError("journal request mismatch or incomplete run")
        self.checksum.update(row_bytes.encode())
        self.count += 1
        if method == "initialize":
            self.initialize_params = params
            self.encoding = ((row.get("result") or {}).get("capabilities") or {}).get(
                "positionEncoding", "utf-16"
            )
            self.initialize_count += 1
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
