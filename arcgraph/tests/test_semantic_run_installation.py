"""A collection shares one sealed copy and refuses drift before its next launch."""

import os
from pathlib import Path
import stat
import sys

import pytest

from arcgraph.semantic import runtime
from arcgraph.semantic.external_targets import Capture, proved_reexport
from arcgraph.tests.test_semantic_session_reuse import pool as pool


@pytest.mark.skipif(
    os.name == "nt", reason="Pool fixture uses a POSIX shell Node probe"
)
def test_multiple_sessions_share_one_copy_until_pool_close(pool, monkeypatch):
    instance, node = pool
    instance.close()
    instance = runtime.SessionPool()
    entries = []
    copied = []
    copytree = runtime.shutil.copytree

    def copy(*args, **kwargs):
        if Path(args[1]).name == "installation":
            copied.append(args)
        return copytree(*args, **kwargs)

    class Client:
        def __init__(self, argv, *args, **kwargs):
            entry = Path(argv[1])
            entries.append((entry, entry.stat().st_ino))
            assert all(
                not path.stat().st_mode & 0o222
                for path in [instance.private, *instance.package.parent.rglob("*")]
            )

        def close(self):
            pass

    monkeypatch.setattr(runtime.shutil, "copytree", copy)
    monkeypatch.setattr(runtime, "Client", Client)
    try:
        for _ in range(3):
            with runtime.session(node.parent, {}, pool=instance):
                pass
            assert instance.package.exists()
            assert not instance.package.stat().st_mode & 0o222
        assert (
            entries == [(instance.package / "langserver.index.js", entries[0][1])] * 3
        )
        assert len(copied) == 1, "Collection made multiple installation copies"
        entry = entries[0][0]
    finally:
        instance.close()
    assert not entry.exists()


@pytest.mark.parametrize("mutation", ["bytes", "mode", "inode", "flags"])
@pytest.mark.skipif(
    os.name == "nt", reason="Pool fixture uses a POSIX shell Node probe"
)
def test_between_session_drift_is_rejected_before_next_client(
    pool, monkeypatch, mutation
):
    if mutation == "flags" and sys.platform != "darwin":
        pytest.skip("Real st_flags mutation requires macOS")
    instance, node = pool
    launches = []

    class Client:
        def __init__(self, *args, **kwargs):
            launches.append(args)

        def close(self):
            pass

    monkeypatch.setattr(runtime, "Client", Client)
    with runtime.session(node.parent, {}, pool=instance):
        pass
    entry = Path(launches[0][0][1])
    assert entry.exists(), "The first session discarded the collection's copy"
    mode = entry.stat().st_mode
    try:
        if mutation == "flags":
            os.chflags(entry, entry.stat().st_flags | stat.UF_IMMUTABLE)
        elif mutation == "mode":
            entry.chmod(mode ^ 0o100)
        elif mutation == "inode":
            entry.parent.chmod(entry.parent.stat().st_mode | 0o200)
            replacement = entry.with_name("replacement")
            replacement.write_bytes(entry.read_bytes())
            replacement.chmod(mode)
            os.replace(replacement, entry)
        else:
            entry.chmod(mode | 0o200)
            entry.write_bytes(b"replaced")
            entry.chmod(mode)
        with pytest.raises(runtime.InstallError, match="changed during collection"):
            with runtime.session(node.parent, {}, pool=instance):
                pass
        assert len(launches) == 1, "Drift reached the next Client"
    finally:
        if hasattr(os, "chflags") and entry.exists():
            os.chflags(entry, 0)


def test_shared_typeshed_retains_session_declaration_partitions(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    typeshed = tmp_path / "typeshed"
    stdlib = typeshed / "stdlib"
    (stdlib / "collections").mkdir(parents=True)
    (stdlib / "typing.pyi").write_text("", encoding="utf-8")
    (stdlib / "collections/abc.pyi").write_text(
        "from _collections_abc import MutableMapping as MutableMapping\n",
        encoding="utf-8",
    )
    (stdlib / "_collections_abc.pyi").write_text(
        "class MutableMapping: pass\n", encoding="utf-8"
    )
    capture = Capture(root, {})
    original = (stdlib / "typing.pyi").as_uri()
    for partition in range(2):
        item = {"uri": original}
        capture.item(item, {"typeshed": str(typeshed)}, partition=partition)
        assert item["uri"] == original + f"#session-{partition}"
        if partition == 0:
            assert proved_reexport(
                capture.files, "collections.abc", "MutableMapping", "_collections_abc"
            )
    assert len(capture.files) == 6
    # Sharing physical bytes must not broaden the existing admission policy.
    assert not proved_reexport(
        capture.files, "collections.abc", "MutableMapping", "_collections_abc"
    )
