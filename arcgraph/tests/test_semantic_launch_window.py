"""The last launch checks and immutable session permissions narrow drift windows."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from arcgraph.semantic import runtime
from arcgraph.tests.test_semantic_session_reuse import pool as pool


def test_node_replaced_during_second_view_check_never_reaches_client(pool, monkeypatch):
    instance, node = pool
    original = instance.check_view
    calls = []
    launched = []

    def check(root):
        calls.append(root)
        receipt = original(root)
        if len(calls) == 2:
            node.write_text("#!/bin/sh\necho v8.0.0\n", encoding="utf-8")
        return receipt

    class RecordingClient:
        def __init__(self, *args, **kwargs):
            launched.append(args)

        def close(self):
            pass

    monkeypatch.setattr(instance, "check_view", check)
    monkeypatch.setattr(runtime, "Client", RecordingClient)
    error = None
    try:
        with runtime.session(node.parent, {}, pool=instance):
            pass
    except runtime.InstallError as exc:
        error = str(exc)
    assert (
        error and not launched
    ), "Node replacement after the second view check was launched"
    assert instance.version == "v8.0.0"


def test_session_detail_uses_version_probed_after_last_view_check(pool, monkeypatch):
    instance, node = pool
    original = instance.check_view
    version = ["v20.0.0"]
    calls = []

    def check(root):
        calls.append(root)
        receipt = original(root)
        if len(calls) == 2:
            version[0] = "v20.9.0"
        return receipt

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(instance, "check_view", check)
    monkeypatch.setattr(
        runtime.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=version[0])
    )
    monkeypatch.setattr(runtime, "Client", Client)
    with runtime.session(node.parent, {}, pool=instance) as (_, detail):
        assert (
            detail["node_version"] == "v20.9.0"
        ), "Detail did not use the final launch probe"


@pytest.mark.parametrize("constructor_fails", [False, True])
def test_checked_view_is_read_only_at_client_and_removable_after_session(
    pool, monkeypatch, constructor_fails
):
    instance, node = pool
    observed = {}

    class Client:
        def __init__(self, argv, *args, **kwargs):
            root = Path(argv[1]).parent.parent
            observed["root"] = root
            observed["writable"] = [
                p.relative_to(root).as_posix()
                for p in [root, *root.rglob("*")]
                if p.stat().st_mode & 0o222
            ]
            observed["parent_writable"] = bool(root.parent.stat().st_mode & 0o222)
            if constructor_fails:
                raise ValueError("client failed")

        def close(self):
            pass

    monkeypatch.setattr(runtime, "Client", Client)
    if constructor_fails:
        with pytest.raises(ValueError, match="client failed"):
            with runtime.session(node.parent, {}, pool=instance):
                pass
    else:
        with runtime.session(node.parent, {}, pool=instance):
            pass
    assert not observed["writable"], "Checked files and directories remained writable"
    assert not observed[
        "parent_writable"
    ], "The session container could replace the checked root"
    assert not observed["root"].exists(), "Read-only session was not removed"
    assert instance.content_receipt() == instance.receipt
