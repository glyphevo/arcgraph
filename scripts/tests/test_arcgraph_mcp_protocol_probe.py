from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest

from arcgraph.interfaces.agent_capabilities import (
    mcp_capability_names,
    mcp_tool_descriptions,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_probe_module() -> Any:
    script_path = REPO_ROOT / "scripts" / "arcgraph_mcp_protocol_probe.py"
    spec = importlib.util.spec_from_file_location(
        "arcgraph_mcp_protocol_probe",
        script_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_protocol_probe_tracks_the_product_tool_surface() -> None:
    probe = load_probe_module()

    assert probe.EXPECTED_TOOL_NAMES == mcp_capability_names()


def test_protocol_probe_client_harness_never_imports_arcgraph() -> None:
    source = (REPO_ROOT / "scripts" / "arcgraph_mcp_protocol_probe.py").read_text(
        encoding="utf-8"
    )

    assert not re.search(r"^\s*(?:from|import)\s+arcgraph\b", source, re.MULTILINE)


def test_protocol_probe_normalizes_v1_and_v2_schema_fields() -> None:
    probe = load_probe_module()
    schema = {"type": "object", "properties": {"repo_id": {"type": "string"}}}
    v1 = [
        SimpleNamespace(
            name=name,
            description=description,
            inputSchema=schema,
        )
        for name, description in mcp_tool_descriptions().items()
    ]
    v2 = [
        SimpleNamespace(
            name=name,
            description=description,
            input_schema=schema,
        )
        for name, description in mcp_tool_descriptions().items()
    ]

    assert probe._tool_contract(v1) == probe._tool_contract(v2)


@pytest.mark.parametrize(
    ("version", "major"),
    [
        ("1.28.1", 1),
        ("2.0.0", 2),
        ("v2.1.0", 2),
    ],
)
def test_protocol_probe_reads_client_major(version: str, major: int) -> None:
    probe = load_probe_module()

    assert probe._major_version(version) == major


def test_protocol_probe_preserves_virtualenv_python_symlink(
    tmp_path: Path,
) -> None:
    probe = load_probe_module()
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    try:
        executable.symlink_to(Path("/usr/bin/python3"))
    except OSError:
        pytest.skip("This platform does not permit test symlink creation.")
    args = argparse.Namespace(
        server_python=str(executable),
        repo_root=str(tmp_path),
        output_dir=str(tmp_path / "output"),
        repo_id="sample",
    )

    parameters = probe._server_parameters(args)

    assert Path(parameters.command) == executable
    assert Path(parameters.command).is_symlink()


def test_protocol_probe_requires_clean_server_stderr_and_shutdown(
    monkeypatch: Any,
) -> None:
    probe = load_probe_module()
    base = {
        "protocol_version": "2026-07-28",
        "server": {"name": "arcgraph", "version": "0.1.0rc6"},
        "tool_contract": {
            "tool_count": len(probe.EXPECTED_TOOL_NAMES),
            "tool_names": list(probe.EXPECTED_TOOL_NAMES),
            "sha256": "a" * 64,
        },
        "calls": [],
        "server_stderr_empty": True,
        "server_stderr_tail": "",
    }

    async def clean(_args: argparse.Namespace) -> dict[str, Any]:
        return base

    monkeypatch.setattr(probe, "_probe_v2", clean)
    monkeypatch.setattr(probe.importlib_metadata, "version", lambda _name: "2.0.0")
    result = asyncio.run(
        probe._run_probe(
            argparse.Namespace(
                mode="auto",
                expect_protocol="2026-07-28",
                expect_server_version="0.1.0rc6",
            )
        )
    )
    assert result["clean_shutdown"] is True

    async def noisy(_args: argparse.Namespace) -> dict[str, Any]:
        return {
            **base,
            "server_stderr_empty": False,
            "server_stderr_tail": "unexpected diagnostic",
        }

    monkeypatch.setattr(probe, "_probe_v2", noisy)
    with pytest.raises(RuntimeError, match="unexpected stderr"):
        asyncio.run(
            probe._run_probe(
                argparse.Namespace(
                    mode="auto",
                    expect_protocol="2026-07-28",
                    expect_server_version="0.1.0rc6",
                )
            )
        )
