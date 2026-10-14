"""Permission recovery must preserve the session's error and remove its view."""

import os
from pathlib import Path
import stat
import sys

import pytest

from arcgraph.semantic import runtime
from arcgraph.tests.test_semantic_session_reuse import pool as pool


def exception_chain(error):
    while error is not None:
        yield error
        error = error.__cause__ or error.__context__


@pytest.mark.skipif(
    sys.platform != "darwin" or not hasattr(os, "chflags"),
    reason="Requires macOS os.chflags and UF_IMMUTABLE to reproduce chmod EPERM",
)
@pytest.mark.parametrize("failure", [None, "body", "constructor", "handled_outer"])
def test_immutable_view_reports_permissions_or_preserves_original(
    pool, monkeypatch, failure
):
    instance, node = pool
    original = RuntimeError("original " + str(failure))
    observed = {}

    class Client:
        def __init__(self, argv, *args, **kwargs):
            path = Path(argv[1])
            observed["container"] = path.parent.parent.parent
            os.chflags(path, stat.UF_IMMUTABLE)
            assert path.stat().st_flags & stat.UF_IMMUTABLE
            if failure == "constructor":
                raise original

        def close(self):
            pass

    monkeypatch.setattr(runtime, "Client", Client)
    caught = None
    try:
        if failure == "handled_outer":
            try:
                raise LookupError("already handled by the caller")
            except LookupError:
                with runtime.session(node.parent, {}, pool=instance):
                    pass
        else:
            with runtime.session(node.parent, {}, pool=instance):
                if failure == "body":
                    raise original
    except Exception as exc:
        caught = exc
    assert not observed["container"].exists(), "Immutable session left a directory"
    assert not list(instance.private.glob("session-*"))
    private = instance.private
    instance.close()
    assert not private.exists(), "Pool cleanup left its temporary directory"
    assert caught is not None, "Immutable view needs an explicit permission error"
    assert caught is original or isinstance(caught, PermissionError), str(caught)
    assert "installation changed" not in str(caught)
    if failure in ("body", "constructor"):
        assert original in exception_chain(caught)
    else:
        assert "permission" in str(caught).lower()


@pytest.mark.parametrize("operation", ["chmod", "chflags"])
@pytest.mark.parametrize("failure", [None, "body", "constructor"])
def test_restore_failure_is_explicit_and_keeps_original(
    pool, monkeypatch, operation, failure
):
    instance, node = pool
    original = ValueError("original " + str(failure))
    restore_error = PermissionError("restore denied")
    observed = {}
    chmod = Path.chmod
    chflags = getattr(os, "chflags", lambda *args, **kwargs: None)

    def deny_once(path, *args, **kwargs):
        if Path(path) == observed.get("file") and not observed.get("denied"):
            observed["denied"] = True
            raise restore_error
        target = chmod if operation == "chmod" else chflags
        return target(path, *args, **kwargs)

    class Client:
        def __init__(self, argv, *args, **kwargs):
            observed["file"] = Path(argv[1])
            observed["container"] = observed["file"].parent.parent.parent
            # Install the fault after sealing; let TemporaryDirectory retry it.
            if operation == "chmod":
                monkeypatch.setattr(Path, "chmod", deny_once)
            else:
                monkeypatch.setattr(os, "chflags", deny_once, raising=False)
            if failure == "constructor":
                raise original

        def close(self):
            pass

    monkeypatch.setattr(runtime, "Client", Client)
    caught = None
    try:
        with runtime.session(node.parent, {}, pool=instance):
            if failure == "body":
                raise original
    except Exception as exc:
        caught = exc
    assert not observed["container"].exists()
    assert not list(instance.private.glob("session-*"))
    private = instance.private
    instance.close()
    assert not private.exists()
    assert observed.get("denied"), "Recovery never attempted the requested operation"
    assert isinstance(caught, PermissionError), str(caught)
    assert "restore" in str(caught).lower()
    assert "installation changed" not in str(caught)
    assert (original if failure else restore_error) in exception_chain(caught)
