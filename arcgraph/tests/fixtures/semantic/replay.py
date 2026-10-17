"""Shared recorded LSP peer for semantic contract tests."""

import json
from pathlib import Path

FIXTURE = Path(__file__).parent


class Replay:
    def __init__(self, root):
        self.root = root
        self.messages = json.loads(
            (FIXTURE / "recorded-lsp.json").read_text(encoding="utf-8")
        )
        self.requests = [
            r["message"]
            for r in self.messages
            if r["direction"] == "send"
            and "method" in r["message"]
            and "id" in r["message"]
            and r["message"]["method"] != "shutdown"
        ]
        self.index = 0
        self.notifications = []

    def request(self, method, params, timeout=30):
        expected = self.requests[self.index]
        self.index += 1
        normalized = json.loads(
            json.dumps(params).replace(self.root.as_uri(), "file:///FROZEN")
        )
        assert (method, normalized) == (expected["method"], expected["params"])
        response = next(
            r["message"]
            for r in self.messages
            if r["direction"] == "receive"
            and r["message"].get("id") == expected["id"]
            and "method" not in r["message"]
        )
        return json.loads(
            json.dumps(response.get("result")).replace(
                "file:///FROZEN", self.root.as_uri()
            )
        )

    def notify(self, method, params):
        self.notifications.append((method, params))
