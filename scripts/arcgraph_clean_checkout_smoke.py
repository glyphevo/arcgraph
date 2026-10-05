"""Clean-checkout smoke for ArcGraph.

The smoke validates an installed ArcGraph source checkout from a temporary Git
checkout at a concrete commit. It intentionally avoids the caller's working tree
state, local indexes, and generated artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "1.0"
DEFAULT_TIMEOUT_SECONDS = 240.0


@dataclass(frozen=True)
class CommandSpec:
    name: str
    command: list[str]
    cwd: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate ArcGraph source-checkout operation from a "
            "temporary clean Git checkout."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="ArcGraph repository to clone from. Defaults to this checkout.",
    )
    parser.add_argument(
        "--ref",
        default="HEAD",
        help="Git ref to validate. It is resolved to a commit before cloning.",
    )
    parser.add_argument(
        "--clone-source",
        choices=("local", "origin"),
        default="local",
        help=(
            "Clone from the local repository by default. Use origin to validate "
            "a fresh remote clone when private repo credentials are available."
        ),
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip the inner `arcgraph ci` step inside the source-checkout smoke.",
    )
    parser.add_argument(
        "--skip-mcp-extra",
        action="store_true",
        help=(
            "Do not install the optional MCP extra. MCP help still runs because "
            "it must not require the optional runtime."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned clean-checkout commands without cloning or installing.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary workspace for debugging.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-command timeout in seconds.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_clean_checkout_smoke(
        repo_root=Path(args.repo_root),
        ref=args.ref,
        clone_source=args.clone_source,
        quick=args.quick,
        install_mcp_extra=not args.skip_mcp_extra,
        dry_run=args.dry_run,
        keep_temp=args.keep_temp,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] in {"pass", "planned"} else 1


def run_clean_checkout_smoke(
    *,
    repo_root: Path,
    ref: str = "HEAD",
    clone_source: str = "local",
    quick: bool = False,
    install_mcp_extra: bool = True,
    dry_run: bool = False,
    keep_temp: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    repo = repo_root.resolve()
    commit_sha = _git_stdout(repo, ["rev-parse", f"{ref}^{{commit}}"])
    plan = build_command_plan(
        checkout_dir=Path("<temp>") / "checkout",
        venv_dir=Path("<temp>") / "venv",
        install_mcp_extra=install_mcp_extra,
        quick=quick,
        timeout_seconds=timeout_seconds,
    )
    if dry_run:
        return _base_payload(
            status="planned",
            repo_root=repo,
            commit_sha=commit_sha,
            clone_source=clone_source,
            install_mcp_extra=install_mcp_extra,
            quick=quick,
            temp_path=None,
            temp_path_kept=False,
            commands=[_planned_command_record(command) for command in plan],
            warnings=[
                "dry_run_only: no checkout, virtualenv, install, or smoke command was executed."
            ],
        )

    workspace_path: Path | None = None
    temp_context: tempfile.TemporaryDirectory[str] | None = None
    if keep_temp:
        workspace_path = Path(tempfile.mkdtemp(prefix="arcgraph-clean-checkout-"))
    else:
        temp_context = tempfile.TemporaryDirectory(prefix="arcgraph-clean-checkout-")
        workspace_path = Path(temp_context.name)

    commands: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    warnings: list[str] = []
    checkout = workspace_path / "checkout"
    venv_dir = workspace_path / "venv"
    status = "pass"
    clean_status = ""
    staged_stat = ""
    try:
        _clone_checkout(
            repo=repo,
            checkout=checkout,
            commit_sha=commit_sha,
            clone_source=clone_source,
            timeout_seconds=timeout_seconds,
            commands=commands,
        )
        _run_checked(
            CommandSpec(
                "create-venv",
                [sys.executable, "-m", "venv", str(venv_dir)],
                workspace_path,
                timeout_seconds,
            ),
            commands,
        )
        plan = build_command_plan(
            checkout_dir=checkout,
            venv_dir=venv_dir,
            install_mcp_extra=install_mcp_extra,
            quick=quick,
            timeout_seconds=timeout_seconds,
        )
        for command in plan:
            _run_checked(command, commands)
        clean_status = _git_stdout(checkout, ["status", "--short"])
        staged_stat = _git_stdout(checkout, ["diff", "--cached", "--stat"])
        if clean_status.strip() or staged_stat.strip():
            raise RuntimeError(
                "Clean checkout produced tracked or staged changes: "
                f"status={clean_status!r} staged={staged_stat!r}"
            )
    except Exception as exc:
        status = "fail"
        failures.append({"check": "clean-checkout-smoke", "message": str(exc)})
    finally:
        temp_path = str(workspace_path) if keep_temp else None
        if temp_context is not None:
            temp_context.cleanup()

    payload = _base_payload(
        status=status,
        repo_root=repo,
        commit_sha=commit_sha,
        clone_source=clone_source,
        install_mcp_extra=install_mcp_extra,
        quick=quick,
        temp_path=temp_path,
        temp_path_kept=keep_temp,
        commands=commands,
        warnings=warnings,
    )
    payload["failures"] = failures
    payload["clean_checkout"] = {
        "git_status_short": clean_status,
        "git_diff_cached_stat": staged_stat,
        "tracked_or_staged_generated_artifacts": bool(
            clean_status.strip() or staged_stat.strip()
        ),
    }
    return payload


def build_command_plan(
    *,
    checkout_dir: Path,
    venv_dir: Path,
    install_mcp_extra: bool,
    quick: bool,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[CommandSpec]:
    python = _venv_executable(venv_dir, "python")
    arcgraph = _venv_executable(venv_dir, "arcgraph")
    commands = [
        CommandSpec(
            "upgrade-pip",
            [str(python), "-m", "pip", "install", "--upgrade", "pip"],
            checkout_dir,
            timeout_seconds,
        ),
        CommandSpec(
            "install-editable",
            [str(python), "-m", "pip", "install", "-e", "."],
            checkout_dir,
            timeout_seconds,
        ),
    ]
    if install_mcp_extra:
        commands.append(
            CommandSpec(
                "install-editable-mcp-extra",
                [str(python), "-m", "pip", "install", "-e", ".[mcp]"],
                checkout_dir,
                timeout_seconds,
            )
        )
    commands.extend(
        [
            CommandSpec("arcgraph-help", [str(arcgraph), "--help"], checkout_dir),
            CommandSpec(
                "wrapper-help",
                [str(python), "scripts/arcgraph.py", "--help"],
                checkout_dir,
            ),
            CommandSpec(
                "docs-quickstart",
                [str(arcgraph), "docs", "quickstart"],
                checkout_dir,
            ),
            CommandSpec(
                "docs-agent-cli-contract",
                [str(arcgraph), "docs", "agent-cli-contract"],
                checkout_dir,
            ),
            CommandSpec(
                "docs-mcp-server",
                [str(arcgraph), "docs", "mcp-server"],
                checkout_dir,
            ),
            CommandSpec(
                "docs-source-checkout-smoke",
                [str(arcgraph), "docs", "source-checkout-smoke"],
                checkout_dir,
            ),
            CommandSpec(
                "mcp-help",
                [str(arcgraph), "mcp", "--help"],
                checkout_dir,
            ),
            CommandSpec(
                "mcp-serve-help",
                [str(arcgraph), "mcp", "serve", "--help"],
                checkout_dir,
            ),
            CommandSpec(
                "mcp-module-help",
                [str(python), "-m", "arcgraph.interfaces.mcp_server", "--help"],
                checkout_dir,
            ),
        ]
    )
    private_smoke = [
        str(python),
        "scripts/arcgraph_source_checkout_smoke.py",
    ]
    if quick:
        private_smoke.append("--skip-ci")
    commands.append(
        CommandSpec(
            "source-checkout-smoke",
            private_smoke,
            checkout_dir,
            timeout_seconds,
        )
    )
    return commands


def _clone_checkout(
    *,
    repo: Path,
    checkout: Path,
    commit_sha: str,
    clone_source: str,
    timeout_seconds: float,
    commands: list[dict[str, Any]],
) -> None:
    if clone_source == "origin":
        origin = _git_stdout(repo, ["remote", "get-url", "origin"])
        clone_command = ["git", "clone", "--quiet", "--no-tags", origin, str(checkout)]
    else:
        clone_command = [
            "git",
            "clone",
            "--quiet",
            "--no-tags",
            "--no-hardlinks",
            str(repo),
            str(checkout),
        ]
    _run_checked(
        CommandSpec(
            "clone-clean-checkout", clone_command, repo.parent, timeout_seconds
        ),
        commands,
    )
    _run_checked(
        CommandSpec(
            "checkout-commit",
            ["git", "checkout", "--quiet", commit_sha],
            checkout,
            timeout_seconds,
        ),
        commands,
    )


def _run_checked(
    spec: CommandSpec, commands: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        completed = subprocess.run(
            spec.command,
            cwd=spec.cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        record = _command_record(
            spec,
            exit_code=124,
            duration_seconds=time.perf_counter() - started,
            stderr=f"Timed out after {spec.timeout_seconds}s.",
        )
        commands.append(record)
        raise RuntimeError(record["stderr_tail"]) from exc
    record = _command_record(
        spec,
        exit_code=completed.returncode,
        duration_seconds=time.perf_counter() - started,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    commands.append(record)
    if completed.returncode != 0:
        raise RuntimeError(
            f"{spec.name} failed with exit {completed.returncode}: "
            f"{record['stderr_tail'] or record['stdout_tail']}"
        )
    return completed


def _git_stdout(repo: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def _venv_executable(venv_dir: Path, name: str) -> Path:
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    return bin_dir / f"{name}{suffix}"


def _base_payload(
    *,
    status: str,
    repo_root: Path,
    commit_sha: str,
    clone_source: str,
    install_mcp_extra: bool,
    quick: bool,
    temp_path: str | None,
    temp_path_kept: bool,
    commands: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "commit_sha": commit_sha,
        "repo_root": str(repo_root),
        "clone_source": clone_source,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "python": {
            "host_executable": sys.executable,
            "host_version": platform.python_version(),
        },
        "install_mode": "source-checkout-editable",
        "mcp_extra_installed": install_mcp_extra,
        "quick": quick,
        "commands": commands,
        "matrix": _matrix_summary(
            clone_source=clone_source,
            install_mcp_extra=install_mcp_extra,
            quick=quick,
        ),
        "temp_path": temp_path,
        "temp_path_kept": temp_path_kept,
        "warnings": warnings,
    }


def _matrix_summary(
    *, clone_source: str, install_mcp_extra: bool, quick: bool
) -> dict[str, Any]:
    executed = [
        f"{clone_source}_clean_checkout_from_git_commit",
        "source_checkout_editable_install",
        "cli_subprocess_contract_help_and_docs",
        "mcp_server_help_and_module_help",
        "source_checkout_smoke",
        "clean_checkout_git_status",
    ]
    if install_mcp_extra:
        executed.append("source_checkout_editable_install_with_mcp_extra")
    if quick:
        skipped = ["inner_source_checkout_smoke_arcgraph_ci"]
    else:
        executed.append("inner_source_checkout_smoke_arcgraph_ci")
        skipped = []
    deferred = [
        "linux_clean_checkout_smoke_unless_run_on_linux",
        "macos_clean_checkout_smoke_unless_run_on_macos",
        "wheel_or_sdist_install_package_readiness",
        "full_mcp_protocol_client_handshake",
        "automatic_agent_configuration_installer",
        "http_or_network_mcp_transport",
        "public_release_or_package_publishing",
    ]
    if clone_source != "origin":
        deferred.append("remote_origin_clone")
    if not install_mcp_extra:
        skipped.append("source_checkout_editable_install_with_mcp_extra")
    return {
        "executed": executed,
        "skipped": skipped,
        "deferred": deferred,
    }


def _planned_command_record(spec: CommandSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "command": spec.command,
        "cwd": str(spec.cwd),
        "timeout_seconds": spec.timeout_seconds,
        "planned": True,
    }


def _command_record(
    spec: CommandSpec,
    *,
    exit_code: int,
    duration_seconds: float,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    return {
        "name": spec.name,
        "command": spec.command,
        "cwd": str(spec.cwd),
        "exit_code": exit_code,
        "duration_seconds": round(duration_seconds, 3),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }


def _tail(value: str | None, *, max_chars: int = 2000) -> str:
    stripped = (value or "").strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[-max_chars:]


if __name__ == "__main__":
    raise SystemExit(main())
