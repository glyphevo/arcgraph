from __future__ import annotations


import json
from pathlib import Path
from typing import Any

from arcgraph import READ_SCHEMA_VERSION, SCHEMA_VERSION, __version__
from arcgraph.interfaces.cli import main
from arcgraph.version_info import (
    _build_source_label,
    _embedded_build_provenance,
    _installed_artifact_provenance,
    _source_provenance,
    version_info,
)


def test_source_checkout_version_info_has_exact_runtime_identity() -> None:
    payload = version_info()

    assert payload["product_version"] == __version__
    assert payload["display_version"].startswith(f"{__version__}+g")
    assert payload["execution_mode"] == "source_checkout"
    assert payload["provenance_status"] == "source_checkout"
    assert len(payload["source_provenance"]["commit_sha"]) == 40
    assert len(payload["runtime_fingerprint"]["sha256"]) == 64
    assert payload["read_schema_version"] == READ_SCHEMA_VERSION
    assert payload["index_schema_version"] == SCHEMA_VERSION


def test_version_json_cli_emits_provenance_payload(capsys: Any) -> None:
    assert main(["version", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["product_version"] == __version__
    assert payload["display_version"] != __version__


def test_embedded_build_provenance_requires_a_complete_source_identity(
    tmp_path: Any,
) -> None:
    package_dir = tmp_path / "arcgraph"
    package_dir.mkdir()
    path = package_dir / "_build_provenance.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "build_version": __version__,
                "source": {
                    "commit_sha": "a" * 40,
                    "tree_sha": "b" * 40,
                    "working_tree_clean": True,
                    "working_tree_status_sha256": "c" * 64,
                },
            }
        ),
        encoding="utf-8",
    )

    assert _embedded_build_provenance(package_dir)["source"]["commit_sha"] == "a" * 40

    path.write_text('{"schema_version":"1.0.0","source":{}}', encoding="utf-8")
    assert _embedded_build_provenance(package_dir) is None


def test_dirty_wheel_source_label_discloses_dirty_fingerprint() -> None:
    label = _build_source_label(
        {
            "source": {
                "commit_sha": "a" * 40,
                "working_tree_clean": False,
                "working_tree_status_sha256": "b" * 64,
            }
        }
    )

    assert label == f"g{'a' * 12}.dirty.{'b' * 12}."


def test_installed_artifact_accepts_legacy_pep610_archive_hash(
    tmp_path: Any, monkeypatch: Any
) -> None:
    package_dir = tmp_path / "site" / "arcgraph"
    package_dir.mkdir(parents=True)

    class Distribution:
        def locate_file(self, value: str) -> Any:
            return tmp_path / "site" / value

        def read_text(self, name: str) -> str | None:
            assert name == "direct_url.json"
            return json.dumps({"archive_info": {"hash": f"sha256={'d' * 64}"}})

    monkeypatch.setattr(
        "arcgraph.version_info.importlib.metadata.distribution",
        lambda _name: Distribution(),
    )

    artifact = _installed_artifact_provenance(package_dir)

    assert artifact["sha256"] == "d" * 64


def test_source_provenance_treats_git_timeout_as_unavailable(
    tmp_path: Any, monkeypatch: Any
) -> None:
    import subprocess

    monkeypatch.setattr(
        "arcgraph.version_info._git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("git", 30)
        ),
    )

    assert _source_provenance(tmp_path) is None


def _load_hatch_build(monkeypatch):
    """Load the real build hook with a stubbed hatchling interface.

    hatchling is a build-time-only dependency, so importing it here would
    turn a genuine regression probe into an environment-dependent skip.
    """

    import importlib.util
    import sys
    import types

    if "hatchling" not in sys.modules:
        hatchling = types.ModuleType("hatchling")
        builders = types.ModuleType("hatchling.builders")
        hooks = types.ModuleType("hatchling.builders.hooks")
        plugin = types.ModuleType("hatchling.builders.hooks.plugin")
        interface = types.ModuleType("hatchling.builders.hooks.plugin.interface")

        class BuildHookInterface:  # minimal stand-in for the plugin base
            pass

        interface.BuildHookInterface = BuildHookInterface
        for name, module in (
            ("hatchling", hatchling),
            ("hatchling.builders", builders),
            ("hatchling.builders.hooks", hooks),
            ("hatchling.builders.hooks.plugin", plugin),
            ("hatchling.builders.hooks.plugin.interface", interface),
        ):
            monkeypatch.setitem(sys.modules, name, module)

    path = Path(__file__).resolve().parents[2] / "hatch_build.py"
    spec = importlib.util.spec_from_file_location("arcgraph_hatch_build", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubMetadata:
    version = "0.0.0"


def _make_hook(module, root: Path):
    # The stubbed plugin base declares no descriptors, so the hook can carry
    # root/metadata as plain instance attributes.
    hook = module.CustomBuildHook.__new__(module.CustomBuildHook)
    hook.root = str(root)
    hook.metadata = _StubMetadata()
    return hook


def test_build_hook_restores_provenance_when_build_never_finalizes(
    tmp_path: Path, monkeypatch
) -> None:
    """finalize only runs on success, so the generated file must also be
    removed at interpreter exit; otherwise a later build inherits it."""

    import atexit

    module = _load_hatch_build(monkeypatch)
    registered: list = []
    monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn) or fn)

    package = tmp_path / "arcgraph"
    package.mkdir()
    (tmp_path / ".git").mkdir()
    hook = _make_hook(module, tmp_path)

    hook.initialize("standard", {})
    provenance = package / "_build_provenance.json"
    assert provenance.exists()

    # Simulate a build that dies before finalize: only the atexit hook runs.
    assert registered, "cleanup must be registered for interpreter exit"
    for cleanup in registered:
        cleanup()
    assert not provenance.exists()


def test_build_hook_refuses_stale_provenance_in_a_git_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    """A leftover provenance file must never be inherited inside a git
    checkout whose own provenance query failed."""

    import atexit

    module = _load_hatch_build(monkeypatch)
    monkeypatch.setattr(atexit, "register", lambda fn: fn)
    monkeypatch.setattr(module, "_git_provenance", lambda root: None)

    package = tmp_path / "arcgraph"
    package.mkdir()
    (tmp_path / ".git").mkdir()
    stale = package / "_build_provenance.json"
    stale.write_text(
        json.dumps({"source": {"commit_sha": "dead" * 10}}),
        encoding="utf-8",
    )
    hook = _make_hook(module, tmp_path)

    hook.initialize("standard", {})
    written = json.loads(stale.read_text(encoding="utf-8"))
    assert written["source"] == {}


def test_build_hook_keeps_packaged_provenance_without_a_repository(
    tmp_path: Path, monkeypatch
) -> None:
    """The sdist -> wheel chain has no .git, so the packaged file is the only
    provenance that can exist and must be preserved."""

    import atexit

    module = _load_hatch_build(monkeypatch)
    monkeypatch.setattr(atexit, "register", lambda fn: fn)
    monkeypatch.setattr(module, "_git_provenance", lambda root: None)

    package = tmp_path / "arcgraph"
    package.mkdir()
    packaged = package / "_build_provenance.json"
    packaged.write_text(
        json.dumps({"source": {"commit_sha": "abc123", "working_tree_clean": True}}),
        encoding="utf-8",
    )
    hook = _make_hook(module, tmp_path)

    hook.initialize("standard", {})
    written = json.loads(packaged.read_text(encoding="utf-8"))
    assert written["source"]["commit_sha"] == "abc123"
