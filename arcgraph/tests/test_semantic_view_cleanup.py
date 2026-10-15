"""Permission recovery must preserve the session's error and remove its view."""

from contextlib import ExitStack
import os
from pathlib import Path
import stat
import sys
import traceback

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


@pytest.mark.parametrize("failure", ["body", "drift", "permissions"])
@pytest.mark.parametrize("owned", [False, True])
def test_cleanup_failure_preserves_session_error(pool, monkeypatch, failure, owned):
    instance, node = pool
    cause = LookupError("existing cause")
    body = ValueError("original body")
    cleanup = instance.temporary.cleanup
    restoring = runtime._restore_view_mode
    pending = []

    def exploding_cleanup():
        error = sys.exception()
        pending.append((error, type(error), str(error), error.__cause__))
        raise RuntimeError("cleanup failed")

    def denied_restore(path, mode):
        if path.name == "langserver.index.js":
            raise PermissionError("restore denied")
        return restoring(path, mode)

    class Client:
        def __init__(self, argv, *args, **kwargs):
            if failure == "drift":
                path = Path(argv[1])
                path.chmod(path.stat().st_mode | 0o200)
                path.write_bytes(b"drift")
            elif failure == "permissions":
                monkeypatch.setattr(runtime, "_restore_view_mode", denied_restore)
                raise body from cause

        def close(self):
            pass

    monkeypatch.setattr(runtime, "Client", Client)
    monkeypatch.setattr(instance.temporary, "cleanup", exploding_cleanup)
    if owned:
        monkeypatch.setattr(runtime, "SessionPool", lambda: instance)
    caught = None
    try:
        try:
            with runtime.session(node.parent, {}, pool=None if owned else instance):
                if failure == "body":
                    raise body from cause
        except Exception as exc:
            caught = exc
        original, error_type, message, original_cause = pending[0]
        assert caught is original, "Cleanup replaced the original session error"
        assert type(caught) is error_type
        assert str(caught) == message
        assert caught.__cause__ is original_cause
        expected_type = {
            "body": ValueError,
            "drift": runtime.InstallError,
            "permissions": runtime._ViewPermissionError,
        }[failure]
        assert error_type is expected_type
        if failure == "body":
            assert original_cause is cause
        elif failure == "permissions":
            assert original_cause is body and body.__cause__ is cause
            assert "restore denied" in message
        else:
            assert message == "Private pyright installation changed during collection."
            assert original_cause is None
        assert len(pending) == (2 if owned else 1)
        rendered = "".join(traceback.format_exception(caught))
        assert "RuntimeError: cleanup failed" in rendered
        assert "exploding_cleanup" in rendered
        assert len(caught.__notes__) == len(pending)
        assert instance.private.exists(), "Failed cleanup should leave a retryable path"
    finally:
        monkeypatch.setattr(instance.temporary, "cleanup", cleanup)
        cleanup()
    assert not instance.private.exists()


@pytest.mark.parametrize("owner", ["session", "exit_stack"])
@pytest.mark.parametrize("failures", [(1,), (2,), (1, 2)])
@pytest.mark.parametrize("cleanup_type", [RuntimeError, GeneratorExit])
def test_generator_close_exposes_cleanup_failures(
    pool, monkeypatch, owner, failures, cleanup_type
):
    instance, node = pool
    cleanup = instance.temporary.cleanup
    errors = [
        cleanup_type("first cleanup failed"),
        cleanup_type("second cleanup failed"),
    ]
    attempts = []

    def failing_cleanup():
        attempts.append(len(attempts) + 1)
        if attempts[-1] in failures:
            raise errors[attempts[-1] - 1]
        cleanup()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    def driver():
        if owner == "session":
            with runtime.session(node.parent, {}):
                yield "inside"
        else:
            # Match collect's lifetime/peers stack order around a shared pool.
            with ExitStack() as lifetime, ExitStack() as peers:
                lifetime.enter_context(instance.lifetime())
                peers.enter_context(runtime.session(node.parent, {}, pool=instance))
                yield "inside"

    monkeypatch.setattr(runtime, "Client", Client)
    monkeypatch.setattr(runtime, "SessionPool", lambda: instance)
    monkeypatch.setattr(instance.temporary, "cleanup", failing_cleanup)
    generator = driver()
    caught = None
    try:
        assert next(generator) == "inside"
        try:
            generator.close()
        except BaseException as exc:
            caught = exc
        assert caught is not None, "generator.close() hid cleanup failure"
        assert isinstance(caught, RuntimeError)
        first = errors[failures[0] - 1]
        if cleanup_type is RuntimeError:
            assert caught is first
        else:
            assert caught.__cause__ is first
        assert attempts == [1, 2]
        rendered = "".join(traceback.format_exception(caught))
        for attempt in failures:
            assert str(errors[attempt - 1]) in rendered
        assert len(getattr(caught, "__notes__", [])) == len(failures) - 1
        generator.close()
        assert attempts == [1, 2], "A closed generator ran cleanup again"
        assert instance.private.exists() == (failures == (1, 2))
    finally:
        monkeypatch.setattr(instance.temporary, "cleanup", cleanup)
        cleanup()
    assert not instance.private.exists()


def test_collection_cleanup_failure_preserves_initialization_error(
    pool, tmp_path, monkeypatch
):
    from arcgraph.semantic import transport

    instance, node = pool
    output = tmp_path / "collection"
    output.mkdir()
    original = ValueError("initialize failed")
    cause = LookupError("existing cause")
    cleanup = instance.temporary.cleanup

    def exploding_cleanup():
        raise RuntimeError("cleanup failed")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args):
            raise original from cause

        def close(self):
            pass

    monkeypatch.setattr(runtime, "Client", Client)
    monkeypatch.setattr(transport, "SessionPool", lambda: instance)
    monkeypatch.setattr(
        transport, "load", lambda path: {"snapshot_id": "test", "definitions": []}
    )
    monkeypatch.setattr(instance.temporary, "cleanup", exploding_cleanup)
    caught = None
    try:
        try:
            transport.collect(tmp_path / "plan.json", node.parent, {}, output)
        except Exception as exc:
            caught = exc
        assert caught is original, "Collection teardown replaced the original error"
        assert caught.__cause__ is cause
        assert len(caught.__notes__) == 2
        assert "RuntimeError: cleanup failed" in "".join(
            traceback.format_exception(caught)
        )
        assert instance.private.exists()
    finally:
        monkeypatch.setattr(instance.temporary, "cleanup", cleanup)
        cleanup()
    assert not instance.private.exists()
