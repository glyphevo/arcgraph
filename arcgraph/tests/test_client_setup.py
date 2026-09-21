"""Onboarding regressions: real indexing/protocol plus non-destructive config edits."""

import argparse
import json
import os
from pathlib import Path
import tomllib

import pytest
import yaml

from arcgraph.interfaces import client_setup as setup


def args(repo, client, config=None, dry=False):
    return argparse.Namespace(
        repo_root=str(repo),
        output_dir="private index",
        client=client,
        client_config=config,
        dry_run=dry,
        timeout=30,
    )


@pytest.mark.parametrize("client", setup.CLIENTS)
def test_dry_run_does_not_write_or_spawn(tmp_path, monkeypatch, client):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes profile"))
    monkeypatch.setattr(
        setup.subprocess, "run", lambda *a, **k: pytest.fail("dry run spawned")
    )
    result = setup.handle_setup(args(tmp_path, client, dry=True))
    assert result["status"] == "planned"
    assert list(tmp_path.iterdir()) == []
    assert result["output_dir"] == str(tmp_path / "private index")
    if client != "pi":
        assert result["serve_command"][-1] == result["cli_prefix"][-1]
    assert not result["client_connection_verified"]
    assert not result["model_call_verified"]


@pytest.mark.parametrize("client", setup.CLIENTS)
def test_real_setup_is_idempotent_and_uses_one_index(tmp_path, monkeypatch, client):
    pytest.importorskip("mcp")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes profile"))
    (tmp_path / "sample.py").write_text(
        "def source(): return 1\ndef caller(): return source()\n"
    )
    first = setup.handle_setup(args(tmp_path, client))
    assert first["status"] == "prepared", first
    assert first["configuration_written"]
    config = Path(first["client_config"])
    before = config.read_bytes()
    second = setup.handle_setup(args(tmp_path, client))
    assert second["status"] == "prepared", second
    assert second["index_version"] == first["index_version"]
    assert not second["configuration_written"]
    assert config.read_bytes() == before
    assert not second["client_connection_verified"]
    assert not second["model_call_verified"]
    if client != "pi":
        assert second["protocol_probe"]["tool_count"] == 14
        assert second["protocol_probe"]["index_version"] == second["index_version"]
    (tmp_path / "sample.py").write_text("def replacement(): return 2\n")
    third = setup.handle_setup(args(tmp_path, client))
    assert third["status"] == "prepared", third
    assert third["index_version"] != second["index_version"]
    assert third["index_command"][-2:] == ["sync", "--if-stale"]


@pytest.mark.parametrize("client", ["claude", "cursor", "hermes", "codex"])
def test_merge_preserves_other_servers_and_backup(tmp_path, client):
    server = {"command": "/tools/arcgraph", "args": ["mcp", "serve", "a path/含空格"]}
    if client == "codex":
        old = b'# comment\nmodel = "keep"\n[mcp_servers.other]\ncommand="other"\n'
        decode = tomllib.loads
    elif client == "hermes":
        old = b"model: keep\nmcp_servers:\n  other:\n    command: other\n"
        decode = yaml.safe_load
    else:
        old = b'{"model":"keep","mcpServers":{"other":{"command":"other"}}}'
        decode = json.loads
    path = tmp_path / "config"
    path.write_bytes(old)
    new = setup._render(client, old, "arcgraph-test", server, "")
    backup = setup._write_config(path, old, new)
    assert Path(backup).read_bytes() == old
    data = decode(path.read_text())
    key = "mcp_servers" if client in {"hermes", "codex"} else "mcpServers"
    assert data["model"] == "keep"
    assert data[key]["other"] == {"command": "other"}
    assert data[key]["arcgraph-test"]["args"] == server["args"]
    if client == "codex":
        assert new.startswith(old)
    assert setup._render(client, new, "arcgraph-test", server, "") == new
    with pytest.raises(ValueError, match="differs"):
        setup._render(
            client, new, "arcgraph-test", {"command": "foreign", "args": []}, ""
        )


@pytest.mark.parametrize(
    "client,old",
    [
        ("claude", b'{"mcpServers":{},"mcpServers":{}}'),
        ("cursor", b"{/* jsonc is not silently destroyed */}"),
        ("hermes", b"mcp_servers: {}\nmcp_servers: {}"),
        ("codex", b"mcp_servers = {}\n"),
        ("pi", b"existing user skill"),
    ],
)
def test_unsupported_or_conflicting_config_is_not_overwritten(tmp_path, client, old):
    path = tmp_path / "config"
    path.write_bytes(old)
    result = setup.handle_setup(args(tmp_path, client, config=path, dry=True))
    assert result["status"] == "blocked"
    assert path.read_bytes() == old
    assert not (tmp_path / "private index").exists()


def test_changed_config_refused_before_replacement(tmp_path):
    path = tmp_path / "config"
    path.write_bytes(b"new user edit")
    with pytest.raises(ValueError, match="changed"):
        setup._write_config(path, b"old", b"replacement")
    assert path.read_bytes() == b"new user edit"


@pytest.mark.skipif(os.name != "posix", reason="symlink privileges vary on Windows")
def test_symlink_and_symlink_parent_refused(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    result = setup.handle_setup(
        args(tmp_path, "claude", config=link / "config", dry=True)
    )
    assert result["status"] == "blocked"
    assert not (actual / "config").exists()


def test_no_config_written_if_protocol_probe_fails(tmp_path, monkeypatch):
    (tmp_path / "sample.py").write_text("def f(): pass\n")

    def fail(*a):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(setup, "_probe", fail)
    result = setup.handle_setup(args(tmp_path, "claude"))
    assert result["status"] == "blocked"
    assert result["error"] == "probe failed"
    assert not (tmp_path / ".mcp.json").exists()
    assert (tmp_path / "private index/current.json").exists()


def test_foreign_index_is_not_republished_for_another_project(tmp_path):
    first_repo = tmp_path / "first"
    second_repo = tmp_path / "second"
    first_repo.mkdir()
    second_repo.mkdir()
    (first_repo / "sample.py").write_text("def first(): pass\n")
    (second_repo / "sample.py").write_text("def second(): pass\n")
    first = setup.handle_setup(args(first_repo, "pi"))
    assert first["status"] == "prepared", first
    pointer = Path(first["output_dir"]) / "current.json"
    before = pointer.read_bytes()
    options = args(second_repo, "pi")
    options.output_dir = first["output_dir"]
    second = setup.handle_setup(options)
    assert second["status"] == "blocked", second
    assert "another repository" in second["error"]
    assert pointer.read_bytes() == before
    assert not (second_repo / ".pi").exists()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_has_no_effect(tmp_path, timeout):
    options = args(tmp_path, "pi")
    options.timeout = timeout
    assert setup.handle_setup(options)["status"] == "blocked"
    assert list(tmp_path.iterdir()) == []
