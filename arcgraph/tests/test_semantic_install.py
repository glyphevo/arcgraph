"""Offline setup, archive safety and content verification of the pinned backend."""

import io
import json
import os
from pathlib import Path
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arcgraph.interfaces.cli import build_parser, main
from arcgraph.semantic import install as setup


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return tmp_path


@pytest.fixture
def tgz():
    value = os.environ.get("ARCGRAPH_TEST_PYRIGHT_TGZ")
    if not value:
        if os.environ.get("ARCGRAPH_REQUIRE_SEMANTIC") == "1":
            pytest.fail("ARCGRAPH_TEST_PYRIGHT_TGZ is required for semantic tests")
        pytest.skip(
            "ARCGRAPH_TEST_PYRIGHT_TGZ is required for offline pinned-pyright tests"
        )
    return Path(value)


def test_offline_cli_install_is_verified_idempotent_and_repairs_content(
    cache, tgz, monkeypatch, capsys
):
    monkeypatch.setattr(
        setup.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("offline setup used network"),
    )
    assert main(["setup", "semantic", "--from", str(tgz)]) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["status"] == "installed"
    package = setup.validate_installation()
    assert (
        package == Path(installed["path"]) and installed["pyright_version"] == "1.1.414"
    )
    pointer = setup.installation_root() / "active.json"
    before = pointer.read_bytes(), pointer.stat().st_mtime_ns
    assert main(["setup", "semantic", "--from", str(tgz)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "unchanged"
    assert (pointer.read_bytes(), pointer.stat().st_mtime_ns) == before
    (package / "langserver.index.js").write_text("corrupt", encoding="utf-8")
    with pytest.raises(setup.InstallError, match="content mismatch"):
        setup.validate_installation()
    repaired = setup.install(tgz)
    assert repaired["status"] == "installed" and Path(repaired["path"]) != package
    setup.validate_installation()


def test_hash_mismatch_precedes_extraction_and_publication(cache, tgz):
    changed = cache / "tampered.tgz"
    changed.write_bytes(tgz.read_bytes() + b"tampered")
    rejected = False
    try:
        setup.install(changed)
    except setup.InstallError as exc:
        assert "hash mismatch" in str(exc)
        rejected = True
    assert (
        rejected
    ), "The locked hash must reject an altered archive before installation"
    assert not (setup.installation_root() / "active.json").exists()


def test_local_archive_directory_is_rejected_before_copy(cache):
    directory = cache / "archive-directory"
    directory.mkdir()
    with pytest.raises(setup.InstallError, match="regular file"):
        setup.install(directory)
    assert not list(setup.installation_root().glob("installation-*"))


@pytest.mark.parametrize(
    "name,kind",
    [
        ("/escape", tarfile.REGTYPE),
        ("package/../escape", tarfile.REGTYPE),
        ("package/link", tarfile.SYMTYPE),
        ("package/hard", tarfile.LNKTYPE),
        ("package/fifo", tarfile.FIFOTYPE),
        ("package/device", tarfile.CHRTYPE),
        ("package/dir", tarfile.DIRTYPE),
        ("package/C:/escape", tarfile.REGTYPE),
        ("package/a\\b", tarfile.REGTYPE),
    ],
)
def test_unsafe_archive_members_are_rejected_without_writes(cache, name, kind):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "package/ordinary"
        archive.addfile(member, io.BytesIO())
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:gz") as archive:
        rejected = False
        try:
            setup.members(archive)
        except setup.InstallError as exc:
            assert "Unsafe" in str(exc)
            rejected = True
        assert rejected, "Non-regular or escaped archive members must be rejected"


@pytest.mark.parametrize(
    "names", [("package/x", "package/x"), ("package/x", "package/x/y")]
)
def test_duplicate_and_prefix_collisions_fail(cache, names):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name in names:
            archive.addfile(tarfile.TarInfo(name), io.BytesIO())
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:gz") as archive:
        with pytest.raises(setup.InstallError, match="Unsafe"):
            setup.members(archive)


def test_download_path_uses_only_a_local_http_service(cache, tgz, monkeypatch):
    body = tgz.read_bytes()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            monkeypatch.setattr(
                setup, "URL", f"http://127.0.0.1:{server.server_port}/pyright.tgz"
            )
            assert setup.install()["status"] == "installed"
        finally:
            server.shutdown()
            thread.join()
    assert requests == ["/pyright.tgz"]


def test_network_error_has_offline_remedy(cache, monkeypatch):
    def fail(*args, **kwargs):
        raise setup.urllib.error.URLError("local simulated failure")

    monkeypatch.setattr(setup.urllib.request, "urlopen", fail)
    with pytest.raises(setup.InstallError, match="download failed.*--from LOCAL_TGZ"):
        setup.install()


def test_unwritable_cache_and_symlink_are_clear_failures(cache, tgz, monkeypatch):
    file = cache / "not-a-directory"
    file.write_bytes(b"occupied")
    monkeypatch.setattr(setup, "cache_root", lambda: file)
    with pytest.raises(setup.InstallError, match="permissions"):
        setup.install(tgz)
    link = cache / "linked"
    link.symlink_to(cache)
    monkeypatch.setattr(setup, "cache_root", lambda: link)
    with pytest.raises(setup.InstallError, match="link"):
        setup.install(tgz)


def test_archive_verification_cannot_be_forged_by_an_install_receipt(cache, tgz):
    package = Path(setup.install(tgz)["path"])
    (package / "package.json").write_text('{"version":"1.1.414"}', encoding="utf-8")
    with pytest.raises(setup.InstallError, match="content mismatch"):
        setup.validate_installation()


def test_setup_client_required_behavior_is_preserved():
    with pytest.raises(ValueError, match="required: --client"):
        build_parser().parse_args(["setup"])
    args = build_parser().parse_args(["setup", "--client", "codex", "--dry-run"])
    assert args.client == "codex" and args.dry_run
    semantic = build_parser().parse_args(["setup", "semantic", "--from", "local.tgz"])
    assert semantic.from_tgz == Path("local.tgz") and semantic.client is None


@pytest.mark.parametrize(
    "platform,tail",
    [
        ("darwin", "Library/Caches/arcgraph"),
        ("linux", "cache/arcgraph"),
        ("win32", "local/arcgraph"),
    ],
)
def test_platform_cache_locations(cache, monkeypatch, platform, tail):
    monkeypatch.setattr(setup.sys, "platform", platform)
    assert setup.cache_root() == cache / tail


def test_size_limits_are_checked_before_extracting(cache, monkeypatch):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        member = tarfile.TarInfo("package/large")
        member.size = 10
        archive.addfile(member, io.BytesIO(b"0123456789"))
    monkeypatch.setattr(setup, "MAX_CONTENT_BYTES", 5)
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:gz") as archive:
        with pytest.raises(setup.InstallError, match="limits"):
            setup.members(archive)
