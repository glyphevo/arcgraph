from __future__ import annotations


import json
import subprocess
from pathlib import Path
from typing import Any

from arcgraph.interfaces.docs import render_docs
from arcgraph.interfaces import visual_smoke
from arcgraph.interfaces.visual_smoke import (
    VisualSmokeOptions,
    run_visual_smoke,
    synthetic_large_graph_expectations,
)


def test_visual_smoke_skips_when_npx_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(visual_smoke.shutil, "which", lambda name: None)

    result = run_visual_smoke(
        object(),  # type: ignore[arg-type]
        options=VisualSmokeOptions(output_dir=tmp_path),
    )

    assert result["status"] == "skipped"
    assert result["_exit_code"] == 0
    assert result["warnings"][0]["kind"] == "playwright_unavailable"
    assert (tmp_path / "result.json").exists()


def test_visual_smoke_runner_records_browser_assertions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands: list[list[str]] = []
    fake_server = _FakeServer()
    monkeypatch.setattr(visual_smoke.shutil, "which", lambda name: "npx")
    monkeypatch.setattr(
        visual_smoke,
        "create_visual_workbench_server",
        lambda *args, **kwargs: fake_server,
    )

    result = run_visual_smoke(
        object(),  # type: ignore[arg-type]
        options=VisualSmokeOptions(output_dir=tmp_path),
        command_runner=_fake_runner(commands),
    )

    assert result["status"] == "pass"
    assert result["_exit_code"] == 0
    assert fake_server.shutdown_called
    assert fake_server.closed
    assert any("open" in command for command in commands)
    assert any("run-code" in command for command in commands)
    assert any("screenshot" in command for command in commands)
    assert all(item["status"] == "pass" for item in result["assertions"])
    stored = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert stored["schema"] == "ArcGraphVisualSmoke"


def _screenshot_failing_runner(commands: list[list[str]], *, exit_code: int):
    base = _fake_runner(commands)

    class Runner:
        def run(
            self, command: list[str], *, timeout_s: float
        ) -> subprocess.CompletedProcess[str]:
            if "screenshot" in command and "focus-drawer" in " ".join(command):
                commands.append(command)
                return subprocess.CompletedProcess(command, exit_code, "", "")
            return base.run(command, timeout_s=timeout_s)

    return Runner()


def test_visual_smoke_fails_and_omits_a_screenshot_that_was_not_written(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for exit_code in (1, 0):  # failed command; success without a file
        output_dir = tmp_path / f"exit-{exit_code}"
        output_dir.mkdir()
        (output_dir / "focus-drawer.png").write_bytes(b"older image")
        monkeypatch.setattr(visual_smoke.shutil, "which", lambda name: "npx")
        monkeypatch.setattr(
            visual_smoke,
            "create_visual_workbench_server",
            lambda *args, **kwargs: _FakeServer(),
        )

        result = run_visual_smoke(
            object(),  # type: ignore[arg-type]
            options=VisualSmokeOptions(output_dir=output_dir),
            command_runner=_screenshot_failing_runner([], exit_code=exit_code),
        )

        assert result["status"] == "failed", exit_code
        assert result["_exit_code"] == 1
        written = next(
            item
            for item in result["assertions"]
            if item["name"] == "screenshots_written"
        )
        assert written["status"] == "fail"
        assert written["details"] == {
            "overview_screenshot": True,
            "focus_drawer_screenshot": False,
        }
        assert "focus_drawer_screenshot" not in result["artifacts"]
        assert "overview_screenshot" in result["artifacts"]
        assert (output_dir / "focus-drawer.png").read_bytes() == b"older image"
        assert not [p for p in output_dir.iterdir() if p.name.startswith(".")]


def test_visual_smoke_documents_synthetic_thresholds() -> None:
    expectations = synthetic_large_graph_expectations()

    assert expectations["nodes_100"] == {"renderer": "svg", "density": "full"}
    assert expectations["nodes_800"] == {"renderer": "canvas", "density": "sparse"}
    assert expectations["nodes_2000"] == {"renderer": "canvas", "density": "sparse"}


def test_visual_smoke_is_documented_as_maintainer_qa() -> None:
    docs = render_docs("visualization")

    assert "arcgraph visual smoke" in docs
    assert "maintainer" in docs.lower()
    assert "Playwright" in docs


class _FakeServer:
    server_address = ("127.0.0.1", 8765)

    def __init__(self) -> None:
        self.shutdown_called = False
        self.closed = False

    def serve_forever(self) -> None:
        return

    def shutdown(self) -> None:
        self.shutdown_called = True

    def server_close(self) -> None:
        self.closed = True


def _fake_runner(
    commands: list[list[str]],
):
    class FakeRunner:
        def run(
            self, command: list[str], *, timeout_s: float
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if "run-code" in command:
                stdout = json.dumps(_fake_browser_result())
            elif "console" in command and "error" in command:
                stdout = "### Result\nTotal messages: 0 (Errors: 0, Warnings: 0)\n"
            else:
                stdout = ""
            if "screenshot" in command:
                Path(command[command.index("--filename") + 1]).write_bytes(b"PNG")
            return subprocess.CompletedProcess(command, 0, stdout, "")

    return FakeRunner()


def _fake_browser_result() -> dict[str, Any]:
    return {
        "initial": {
            "nodes": 8,
            "edges": 12,
            "modeChip": "Mode: remote",
            "payloadChip": "Payload: ok",
            "renderChip": "Render: SVG 8/12",
            "freshnessChip": "Freshness: fresh",
            "ciChip": "CI: pass",
            "evidenceChip": "Evidence: warn",
        },
        "canvas": {"nodes": 8, "edges": 12, "renderer": "Renderer: CANVAS"},
        "svg": {"nodes": 8, "edges": 12, "renderer": "Renderer: SVG"},
        "density": {
            "full": {"nodes": 8, "edges": 12},
            "balanced": {"nodes": 8, "edges": 9},
            "sparse": {"nodes": 8, "edges": 5},
        },
        "reducible": {"balancedHidden": 3, "sparseHidden": 7},
        "focused": {"nodes": 4, "edges": 3},
        "drawer": {
            "nodes": 4,
            "edges": 3,
            "drawerOpen": True,
            "auditText": "Provenance Impact Tests Unresolved",
            "auditTextLength": 34,
        },
        "afterClear": {"nodes": 8, "edges": 12},
        "focusResultClicked": True,
        "synthetic": {
            "nodes_100": {"renderer": "svg", "density": "full"},
            "nodes_800": {"renderer": "canvas", "density": "sparse"},
            "nodes_2000": {"renderer": "canvas", "density": "sparse"},
        },
    }
