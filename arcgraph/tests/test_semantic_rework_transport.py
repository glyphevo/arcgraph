"""Physical handshakes are counted and checked even across session restarts."""

from contextlib import contextmanager
import hashlib
import json

from arcgraph.semantic import transport
from arcgraph.semantic.journal import JournalReplay
from arcgraph.semantic.runtime import initialize_params


def test_every_restart_handshake_is_counted_and_journaled(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "demo.py").write_text("def f(): pass\n", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    position = {
        "textDocument": {"uri": (root / "demo.py").as_uri()},
        "position": {"line": 0, "character": 4},
    }
    plan = output / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "snapshot_id": "frozen",
                "definitions": [
                    {"path": "demo.py", "positions": {"utf-16": position["position"]}}
                ]
                * 513,
            }
        ),
        encoding="utf-8",
    )

    class Peer:
        stderr = []

        def request(self, method, params):
            if method == "initialize":
                return {"capabilities": {"callHierarchyProvider": True}}
            return (
                [{"name": "f"}] if method == "textDocument/prepareCallHierarchy" else []
            )

        def notify(self, method, params):
            pass

    @contextmanager
    def session(*args, **kwargs):
        yield Peer(), {
            "node": "fixed",
            "node_version": "v22",
            "pyright_version": "1.1.414",
        }

    monkeypatch.setattr(transport, "session", session)
    result = transport.collect(plan, root, {}, output)
    lines = (output / "responses.jsonl").read_bytes().splitlines(keepends=True)
    rows = [json.loads(line) for line in lines]
    assert result["requests"]["initialize"] == len(result["sessions"]) == 2
    assert sum(r.get("method") == "initialize" for r in rows) == 2
    assert rows[-1]["count"] == 1028
    assert rows[-1]["checksum"] == hashlib.sha256(b"".join(lines[1:-1])).hexdigest()
    replay = JournalReplay(output / "responses.jsonl", "frozen")
    replay.request("initialize", initialize_params(root))
    for _ in range(513):
        replay.request("textDocument/prepareCallHierarchy", position)
        replay.request("callHierarchy/outgoingCalls", {"item": {"name": "f"}})
    replay.close()
