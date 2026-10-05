"""Optional browser smoke checks for the local ArcGraph Explorer workbench."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from arcgraph.core.force_graph_export import ForceGraphExportOptions
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.sharing_retry import retry_sharing_violation
from arcgraph.core.utils import replace_text_file
from arcgraph.interfaces.visual_server import create_visual_workbench_server

VISUAL_SMOKE_SCHEMA = "ArcGraphVisualSmoke"
VISUAL_SMOKE_VERSION = 1
# Pinned so a smoke run fetches the same CLI each time instead of the latest.
PLAYWRIGHT_CLI_PACKAGE = "@playwright/cli@0.1.22"
DEFAULT_VISUAL_SMOKE_TARGET = "arcgraph.interfaces.cli.handle_visual_workbench"


class CommandRunner(Protocol):
    def run(
        self,
        command: list[str],
        *,
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class VisualSmokeOptions:
    output_dir: Path
    target: str = DEFAULT_VISUAL_SMOKE_TARGET
    host: str = "127.0.0.1"
    port: int = 0
    timeout_s: float = 45.0


def run_visual_smoke(
    query_engine: QueryEngine,
    *,
    graph_options: ForceGraphExportOptions | None = None,
    options: VisualSmokeOptions,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Run an optional Playwright CLI smoke against ``visual serve``.

    The smoke is intentionally outside the default test dependencies. If the
    Playwright CLI cannot be launched, the command records a skipped result
    instead of making normal local verification fail.
    """

    options.output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC).isoformat()
    npx_path = shutil.which("npx")
    if not npx_path:
        return _write_result(
            options.output_dir,
            _base_result(
                status="skipped",
                started_at=started_at,
                warnings=[
                    {
                        "kind": "playwright_unavailable",
                        "message": "npx was not found; install Node.js/npx to run visual smoke.",
                    }
                ],
            ),
        )

    runner = command_runner or SubprocessCommandRunner()
    session = f"arcgraph-visual-smoke-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    server = create_visual_workbench_server(
        query_engine,
        host=options.host,
        port=options.port,
        graph_options=graph_options,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bound_host, bound_port = server.server_address[:2]
    url = f"http://{bound_host}:{bound_port}/"

    result = _base_result(status="running", started_at=started_at)
    result["url"] = url
    result["session"] = session

    try:
        try:
            open_result = _playwright(
                npx_path,
                session,
                ["open", url],
                runner=runner,
                timeout_s=options.timeout_s,
            )
        except RuntimeError as exc:
            result["status"] = "skipped"
            result["warnings"].append(
                {
                    "kind": "playwright_unavailable",
                    "message": f"Playwright CLI could not be launched: {exc}",
                }
            )
            return _write_result(options.output_dir, result)
        result["steps"].append(_step("open", open_result))
        if open_result.returncode != 0:
            result["status"] = "skipped"
            result["warnings"].append(
                {
                    "kind": "playwright_unavailable",
                    "message": "Playwright CLI could not open a browser session.",
                    "stderr": open_result.stderr.strip(),
                }
            )
            return _write_result(options.output_dir, result)

        _playwright(
            npx_path,
            session,
            ["console", "error", "--clear"],
            runner=runner,
            timeout_s=options.timeout_s,
        )
        overview_screenshot = options.output_dir / "overview.png"
        result["steps"].append(
            _step(
                "overview_screenshot",
                _screenshot(
                    npx_path,
                    session,
                    overview_screenshot,
                    runner=runner,
                    timeout_s=options.timeout_s,
                ),
            )
        )

        scenario_file = options.output_dir / "scenario.js"
        replace_text_file(
            scenario_file,
            f"async page => await page.evaluate({_browser_scenario(options.target)})",
        )
        scenario = _playwright(
            npx_path,
            session,
            [
                "--raw",
                "run-code",
                "--filename",
                str(scenario_file),
            ],
            runner=runner,
            timeout_s=options.timeout_s,
        )
        result["steps"].append(_step("scenario", scenario))
        if scenario.returncode != 0:
            result["status"] = "failed"
            result["_exit_code"] = 1
            result["diagnostics"].append(
                {
                    "kind": "scenario_failed",
                    "stderr": scenario.stderr.strip(),
                    "stdout": scenario.stdout.strip(),
                }
            )
            return _write_result(options.output_dir, result)

        try:
            parsed = _parse_raw_json(scenario.stdout)
        except ValueError as exc:
            result["status"] = "failed"
            result["_exit_code"] = 1
            result["diagnostics"].append(
                {
                    "kind": "scenario_output_unparseable",
                    "message": str(exc),
                    "stdout": scenario.stdout[-4000:],
                    "stderr": scenario.stderr[-4000:],
                }
            )
            return _write_result(options.output_dir, result)
        result["browser"] = parsed
        focus_screenshot = options.output_dir / "focus-drawer.png"
        result["steps"].append(
            _step(
                "focus_drawer_screenshot",
                _screenshot(
                    npx_path,
                    session,
                    focus_screenshot,
                    runner=runner,
                    timeout_s=options.timeout_s,
                ),
            )
        )
        console = _playwright(
            npx_path,
            session,
            ["console", "error"],
            runner=runner,
            timeout_s=options.timeout_s,
        )
        result["steps"].append(_step("console_errors", console))
        console_has_errors = "Errors: 0" not in console.stdout
        assertions = _visual_assertions(parsed, console_has_errors)
        result["assertions"] = assertions
        result["artifacts"] = {
            "overview_screenshot": str(_located(overview_screenshot)),
            "focus_drawer_screenshot": str(_located(focus_screenshot)),
            "scenario_js": str(_located(scenario_file)),
            "result_json": str(_located(options.output_dir / "result.json")),
        }
        failed = [item for item in assertions if item.get("status") != "pass"]
        result["status"] = "failed" if failed else "pass"
        result["_exit_code"] = 1 if failed else 0
        return _write_result(options.output_dir, result)
    except RuntimeError as exc:
        result["status"] = "failed"
        result["_exit_code"] = 1
        result["diagnostics"].append({"kind": "playwright_error", "message": str(exc)})
        return _write_result(options.output_dir, result)
    finally:
        try:
            _playwright(
                npx_path,
                session,
                ["close"],
                runner=runner,
                timeout_s=10,
            )
        except RuntimeError:
            pass
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def synthetic_large_graph_expectations() -> dict[str, dict[str, str]]:
    """Document the synthetic browser thresholds used by the smoke scenario."""

    return {
        "nodes_100": {"renderer": "svg", "density": "full"},
        "nodes_800": {"renderer": "canvas", "density": "sparse"},
        "nodes_2000": {"renderer": "canvas", "density": "sparse"},
    }


class SubprocessCommandRunner:
    def run(
        self,
        command: list[str],
        *,
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )


def _playwright(
    npx_path: str,
    session: str,
    args: list[str],
    *,
    runner: CommandRunner,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner.run(
            [
                npx_path,
                "--yes",
                "--package",
                PLAYWRIGHT_CLI_PACKAGE,
                "playwright-cli",
                f"-s={session}",
                *args,
            ],
            timeout_s=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc


def _screenshot(
    npx_path: str,
    session: str,
    target: Path,
    *,
    runner: CommandRunner,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    """Take a screenshot into a fresh file, then rename it over *target*.

    The Playwright CLI writes the file with Node's ``writeFile``, which follows
    a symlink standing at the name it is given; an unpredictable temporary
    name keeps a link left at *target* from redirecting the image.
    """
    temporary = target.with_name(
        f".{target.stem}.{secrets.token_hex(8)}{target.suffix}"
    )
    try:
        completed = _playwright(
            npx_path,
            session,
            ["screenshot", "--filename", str(temporary)],
            runner=runner,
            timeout_s=timeout_s,
        )
        if (
            completed.returncode == 0
            and temporary.is_file()
            and not temporary.is_symlink()
        ):
            retry_sharing_violation(lambda: os.replace(temporary, target))
        return completed
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _located(path: Path) -> Path:
    # Files here are replaced in place, so a symlink at the name is not followed.
    return path.parent.resolve() / path.name


def _browser_scenario(target: str) -> str:
    safe_target = json.dumps(target)
    return f"""async () => {{
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const text = selector => document.querySelector(selector)?.textContent || '';
    const click = selector => {{
        const el = document.querySelector(selector);
        if(!el) throw new Error(`Missing selector ${{selector}}`);
        el.click();
        return true;
    }};
    const parseFirstNumber = value => {{
        const match = String(value || '').match(/(\\d+)/);
        return match ? Number(match[1]) : 0;
    }};
    const counts = () => ({{
        nodes: parseFirstNumber(text('#stat-nodes')),
        edges: parseFirstNumber(text('#stat-edges')),
        renderer: text('#stat-renderer'),
        density: text('#stat-density'),
        modeChip: text('#status-mode'),
        payloadChip: text('#status-payload'),
        renderChip: text('#status-rendering'),
        freshnessChip: text('#status-freshness'),
        ciChip: text('#status-ci'),
        evidenceChip: text('#status-evidence'),
        drawerOpen: document.querySelector('#drawer')?.classList.contains('open') || false,
        zoomRight: getComputedStyle(document.querySelector('.zoom-controls')).right,
        auditText: text('#audit-container').slice(0, 600),
        auditTextLength: text('#audit-container').length,
    }});
    const setInput = (selector, value) => {{
        const el = document.querySelector(selector);
        if(!el) throw new Error(`Missing input ${{selector}}`);
        el.value = value;
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
    }};
    const choose = async selector => {{
        click(selector);
        await sleep(500);
        return counts();
    }};
    const synthetic = (nodeCount, edgeCount) => {{
        const prevRenderer = rendererMode;
        const prevDensity = densityMode;
        const prevLabels = labelMode;
        rendererMode = 'auto';
        densityMode = 'auto';
        labelMode = 'auto';
        const nodes = Array.from({{length: nodeCount}}, (_, i) => ({{id: `n${{i}}`, name: `n${{i}}`, kind: 'function'}}));
        const edges = Array.from({{length: edgeCount}}, (_, i) => ({{source: `n${{i % nodeCount}}`, target: `n${{(i + 1) % nodeCount}}`, kind: 'calls', weight: 1, confidence: 'inferred'}}));
        const renderer = resolveRenderer(nodes, edges);
        const density = resolveDensityMode(nodes, edges);
        const labels = {{
            normal: shouldDrawLabel(nodes[0], nodes, {{n0: 1}}, false, false, false),
            focusSeed: shouldDrawLabel(nodes[0], nodes, {{n0: 1}}, false, false, true),
            forcedNone: (() => {{
                labelMode = 'none';
                return shouldDrawLabel(nodes[0], nodes, {{n0: 1}}, false, false, false);
            }})(),
        }};
        rendererMode = prevRenderer;
        densityMode = prevDensity;
        labelMode = prevLabels;
        return {{nodeCount, edgeCount, renderer, density, labels}};
    }};
    const reductionPotential = () => {{
        const raw = filtered();
        const protectedIds = protectedNodeIds(raw.edges);
        return {{
            balancedHidden: raw.edges.filter(edge => !shouldKeepEdgeForDensity(edge, 'balanced', protectedIds)).length,
            sparseHidden: raw.edges.filter(edge => !shouldKeepEdgeForDensity(edge, 'sparse', protectedIds)).length,
        }};
    }};

    const initial = counts();
    const reducible = reductionPotential();
    const canvas = await choose('#renderer-group [data-renderer="canvas"]');
    const svg = await choose('#renderer-group [data-renderer="svg"]');
    await choose('#renderer-group [data-renderer="auto"]');
    const full = await choose('#density-group [data-density="full"]');
    const balanced = await choose('#density-group [data-density="balanced"]');
    const sparse = await choose('#density-group [data-density="sparse"]');
    await choose('#density-group [data-density="auto"]');
    await choose('#labels-group [data-labels="key"]');
    await choose('#labels-group [data-labels="none"]');
    await choose('#labels-group [data-labels="auto"]');

    setInput('#search-input', {safe_target});
    await sleep(1200);
    const firstResult = document.querySelector('#search-results .search-item');
    if(firstResult) firstResult.click();
    await sleep(1600);
    const focused = counts();
    const firstNode = document.querySelector('.node');
    if(firstNode) {{
        firstNode.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true, view: window}}));
        await sleep(900);
    }}
    const drawer = counts();
    click('#btn-zoom-in');
    click('#btn-fit');
    click('#btn-clear-focus');
    await sleep(600);
    const afterClear = counts();
    setInput('#search-input', {safe_target});
    await sleep(900);
    const refocusResult = document.querySelector('#search-results .search-item');
    if(refocusResult) refocusResult.click();
    await sleep(1200);
    const refocusNode = document.querySelector('.node');
    if(refocusNode) {{
        refocusNode.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true, view: window}}));
        await sleep(900);
    }}
    const finalDrawer = counts();
    return {{
        initial,
        canvas,
        svg,
        density: {{full, balanced, sparse}},
        reducible,
        focused,
        drawer: finalDrawer,
        firstDrawer: drawer,
        afterClear,
        focusResultClicked: Boolean(firstResult),
        refocusResultClicked: Boolean(refocusResult),
        synthetic: {{
            nodes_100: synthetic(100, 180),
            nodes_800: synthetic(800, 1600),
            nodes_2000: synthetic(2000, 4000),
        }},
    }};
}}"""


def _visual_assertions(
    browser: dict[str, Any],
    console_has_errors: bool,
) -> list[dict[str, Any]]:
    focused = browser.get("focused", {})
    density = browser.get("density", {})
    reducible = browser.get("reducible", {})
    full_edges = int(density.get("full", {}).get("edges", 0))
    balanced_edges = int(density.get("balanced", {}).get("edges", 0))
    sparse_edges = int(density.get("sparse", {}).get("edges", 0))
    synthetic = browser.get("synthetic", {})
    return [
        _assertion(
            "overview_non_empty",
            int(browser.get("initial", {}).get("nodes", 0)) > 0,
            browser.get("initial", {}),
        ),
        _assertion(
            "focus_non_empty",
            int(focused.get("nodes", 0)) > 0 and browser.get("focusResultClicked"),
            focused,
        ),
        _assertion(
            "density_reduces_or_reports_none",
            balanced_edges <= full_edges
            and sparse_edges <= full_edges
            and (
                sparse_edges < full_edges or int(reducible.get("sparseHidden", 0)) == 0
            ),
            {
                "full": full_edges,
                "balanced": balanced_edges,
                "sparse": sparse_edges,
                "reducible": reducible,
            },
        ),
        _assertion(
            "drawer_audit_visible",
            bool(browser.get("drawer", {}).get("drawerOpen"))
            and int(browser.get("drawer", {}).get("auditTextLength", 0)) > 0,
            browser.get("drawer", {}),
        ),
        _assertion(
            "status_strip_visible",
            bool(browser.get("initial", {}).get("modeChip"))
            and bool(browser.get("initial", {}).get("payloadChip"))
            and bool(browser.get("initial", {}).get("renderChip"))
            and bool(browser.get("initial", {}).get("freshnessChip"))
            and bool(browser.get("initial", {}).get("ciChip"))
            and bool(browser.get("initial", {}).get("evidenceChip")),
            browser.get("initial", {}),
        ),
        _assertion(
            "controls_reachable_with_drawer",
            bool(browser.get("afterClear", {}).get("nodes", 0)),
            browser.get("afterClear", {}),
        ),
        _assertion(
            "synthetic_renderer_thresholds",
            synthetic.get("nodes_100", {}).get("renderer") == "svg"
            and synthetic.get("nodes_800", {}).get("renderer") == "canvas"
            and synthetic.get("nodes_2000", {}).get("renderer") == "canvas",
            synthetic,
        ),
        _assertion(
            "synthetic_density_thresholds",
            synthetic.get("nodes_100", {}).get("density") == "full"
            and synthetic.get("nodes_800", {}).get("density") == "sparse"
            and synthetic.get("nodes_2000", {}).get("density") == "sparse",
            synthetic,
        ),
        _assertion(
            "console_errors",
            not console_has_errors,
            {"console_has_errors": console_has_errors},
        ),
    ]


def _assertion(name: str, passed: bool, details: Any) -> dict[str, Any]:
    return {"name": name, "status": "pass" if passed else "fail", "details": details}


def _base_result(
    *,
    status: str,
    started_at: str,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": VISUAL_SMOKE_SCHEMA,
        "version": VISUAL_SMOKE_VERSION,
        "status": status,
        "started_at": started_at,
        "steps": [],
        "assertions": [],
        "warnings": warnings or [],
        "diagnostics": [],
        "_exit_code": 0,
    }


def _step(
    name: str,
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "returncode": result.returncode,
        "stdout_tail": (result.stdout or "")[-2000:],
        "stderr_tail": (result.stderr or "")[-2000:],
    }


def _parse_raw_json(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise ValueError("Playwright eval returned no output.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Could not parse JSON object from eval output: {exc}"
                ) from exc
        raise ValueError(f"Could not parse JSON object from eval output: {text[:120]}")


def _write_result(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    result["completed_at"] = datetime.now(UTC).isoformat()
    result_path = output_dir / "result.json"
    result["artifacts"] = {
        **result.get("artifacts", {}),
        "result_json": str(_located(result_path)),
    }
    replace_text_file(
        result_path, json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False)
    )
    return result


__all__ = [
    "DEFAULT_VISUAL_SMOKE_TARGET",
    "VISUAL_SMOKE_SCHEMA",
    "VISUAL_SMOKE_VERSION",
    "VisualSmokeOptions",
    "run_visual_smoke",
    "synthetic_large_graph_expectations",
]
