"""Bounded stdio JSON-RPC transport; deterministic replay uses the same framing."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Protocol


class Peer(Protocol):
    def request(self, method: str, params: object, timeout: float = 30) -> object: ...
    def notify(self, method: str, params: object) -> None: ...


class LspError(RuntimeError):
    pass


class Client:
    def __init__(
        self,
        command: list[str],
        cwd,
        env: dict[str, str],
        configuration: dict,
        *,
        transcript=None,
    ):
        self.configuration = configuration
        self.events: queue.Queue = queue.Queue()
        self.ids = 0
        self.transcript = [] if transcript is None else transcript
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.stderr: list[bytes] = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.err_reader = threading.Thread(target=self._errors, daemon=True)
        self.reader.start()
        self.err_reader.start()

    def _errors(self):
        for line in self.process.stderr:
            self.stderr.append(line)

    def _read(self):
        try:
            while True:
                length = None
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        raise EOFError("LSP stream closed")
                    if line in (b"\r\n", b"\n"):
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if length is None or length < 0 or length > 32_000_000:
                    raise LspError("invalid Content-Length")
                body = self.process.stdout.read(length)
                if len(body) != length:
                    raise EOFError("truncated LSP message")
                self.events.put(json.loads(body))
        except (ValueError, OSError, EOFError, LspError) as exc:
            self.events.put(exc)

    def _send(self, message):
        self.transcript.append({"direction": "send", "message": message})
        body = json.dumps(message).encode()
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.process.stdin.flush()

    def notify(self, method: str, params: object) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _server_request(self, item):
        values = None
        if item["method"] == "workspace/configuration":
            values = []
            for query in item.get("params", {}).get("items", []):
                value = self.configuration
                for key in query.get("section", "").split("."):
                    if key:
                        value = value.get(key, {}) if isinstance(value, dict) else {}
                values.append(value)
        self._send({"jsonrpc": "2.0", "id": item["id"], "result": values})

    def request(self, method: str, params: object, timeout: float = 30) -> object:
        # Single outstanding request. Late responses are ignored by request ID.
        self.ids += 1
        request_id = self.ids
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            try:
                item = self.events.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(method) from exc
            if isinstance(item, Exception):
                raise LspError(str(item)) from item
            self.transcript.append({"direction": "receive", "message": item})
            if "method" in item and "id" in item:
                self._server_request(item)
            elif item.get("id") == request_id:
                if "error" in item:
                    raise LspError(str(item["error"]))
                return item.get("result")

    def close(self):
        try:
            self.request("shutdown", None, timeout=1)
            self.notify("exit", None)
            self.process.wait(timeout=1)
        except (TimeoutError, LspError, OSError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except OSError:
                # A dead server can leave buffered stdin with a broken pipe.
                pass
        self.reader.join(timeout=1)
        self.err_reader.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
