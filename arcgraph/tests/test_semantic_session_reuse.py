"""Reused sessions must launch the bytes and Node identity they checked."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat

import pytest

from arcgraph.semantic import runtime


@pytest.fixture
def pool(tmp_path, monkeypatch):
    package = tmp_path / "installation/package"
    package.mkdir(parents=True)
    (package / "langserver.index.js").write_text("original", encoding="utf-8")
    monkeypatch.setattr(runtime, "validate_installation", lambda: package)
    monkeypatch.setattr(
        runtime, "validate_content", lambda copied, **kw: copied / "package"
    )
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\necho v20.0.0\n", encoding="utf-8", newline="\n")
    node.chmod(0o755)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: str(node))
    instance = runtime.SessionPool()
    instance.prepare()
    try:
        yield instance, node
    finally:
        instance.close()


def rejection(operation):
    try:
        operation()
    except runtime.InstallError as exc:
        return str(exc)
    return None


@pytest.mark.parametrize("replacement", ["old_version", "same_version", "new_path"])
def test_reused_prepare_rejects_changed_node_identity(pool, monkeypatch, replacement):
    instance, node = pool
    if replacement == "new_path":
        other = node.with_name("other-node")
        other.write_bytes(node.read_bytes())
        other.chmod(0o755)
        monkeypatch.setattr(runtime.shutil, "which", lambda name: str(other))
    else:
        other = node.with_name("replacement")
        version = "v8.0.0" if replacement == "old_version" else "v20.0.0"
        other.write_text(
            "#!/bin/sh\necho " + version + "\n", encoding="utf-8", newline="\n"
        )
        other.chmod(0o755)
        os.replace(other, node)
    assert rejection(instance.prepare), "A reused pool accepted replaced Node"
    if replacement == "old_version":
        assert (
            instance.version == "v8.0.0"
        ), "Keep the version actually probed on rejection"


def test_reused_prepare_reexecutes_node_version(pool, monkeypatch):
    instance, _ = pool
    calls = []
    original = runtime.subprocess.run

    def run(argv, **kwargs):
        calls.append(argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "run", run)
    instance.prepare()
    assert len(calls) == 1 and calls[0][1:] == ["--version"]
    assert instance.node_receipt is not None


def test_node_replaced_during_view_preparation_fails_before_client(pool, monkeypatch):
    instance, node = pool
    original = instance.package_view
    launched = []

    @contextmanager
    def view():
        with original() as package:
            node.write_text("#!/bin/sh\necho v8.0.0\n", encoding="utf-8", newline="\n")
            yield package

    monkeypatch.setattr(instance, "package_view", view)
    monkeypatch.setattr(runtime, "Client", lambda *a, **k: launched.append(a))

    def start():
        with runtime.session(node.parent, {}, pool=instance):
            pass

    # A sentinel Client may lack close() on the broken parent implementation.
    try:
        error = rejection(start)
    except AttributeError:
        error = None
    assert error and not launched, "Node drift reached Client construction"


def test_session_view_is_independent_of_restored_cache_mutation(pool, monkeypatch):
    instance, node = pool
    base = node.parent / "installation/package/langserver.index.js"
    original = base.read_bytes()
    observed = {}

    class WindowClient:
        def __init__(self, argv, *args, **kwargs):
            view = Path(argv[1])
            observed["shared_inode"] = (
                view.stat().st_dev,
                view.stat().st_ino,
            ) == (base.stat().st_dev, base.stat().st_ino)
            base.write_bytes(b"poisoned")
            observed["executed_bytes"] = view.read_bytes()
            base.write_bytes(original)

        def close(self):
            pass

    monkeypatch.setattr(runtime, "Client", WindowClient)
    with runtime.session(node.parent, {}, pool=instance) as (_, detail):
        assert detail["node_version"] == "v20.0.0"
    assert observed == {"shared_inode": False, "executed_bytes": original}


def test_session_rechecks_view_after_context_entry_before_launch(pool, monkeypatch):
    instance, node = pool
    original = instance.package_view
    launched = []

    @contextmanager
    def view():
        with original() as package:
            entry = package / "langserver.index.js"
            mode = entry.stat().st_mode
            entry.chmod(mode | 0o200)
            entry.write_bytes(b"poisoned")
            entry.chmod(mode)
            try:
                yield package
            finally:
                if entry.exists():
                    entry.chmod(mode | 0o200)
                    entry.write_bytes(b"original")
                    entry.chmod(mode)

    class RecordingClient:
        def __init__(self, *args, **kwargs):
            launched.append(args)

        def close(self):
            pass

    monkeypatch.setattr(instance, "package_view", view)
    monkeypatch.setattr(runtime, "Client", RecordingClient)

    def start():
        with runtime.session(node.parent, {}, pool=instance):
            pass

    assert rejection(start) and not launched, "An unchecked view reached launch"


@pytest.mark.skipif(not hasattr(os, "chflags"), reason="Requires real file flags")
def test_reused_run_preserves_admitted_nonimmutable_flags(pool, monkeypatch):
    instance, node = pool
    instance.close()
    cache = node.parent / "installation/package/langserver.index.js"
    flags = cache.stat().st_flags
    instance = runtime.SessionPool()

    class Client:
        def __init__(self, argv, *args, **kwargs):
            assert Path(argv[1]).stat().st_flags & stat.UF_NODUMP

        def close(self):
            pass

    monkeypatch.setattr(runtime, "Client", Client)
    try:
        os.chflags(cache, flags | stat.UF_NODUMP)
        for _ in range(2):
            with runtime.session(node.parent, {}, pool=instance):
                pass
            assert instance.content_receipt() == instance.receipt
            assert cache.stat().st_flags == flags | stat.UF_NODUMP
    finally:
        instance.close()
        os.chflags(cache, flags)
