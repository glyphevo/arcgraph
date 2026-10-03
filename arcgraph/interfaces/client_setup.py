"""Explicit multi-client onboarding. No client approval or model call is implied."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import stat
import sys
import subprocess
import tempfile
import tomllib
from typing import Any

import yaml

from arcgraph.interfaces.trial_setup import _arcgraph_executable_selection

CLIENTS = ("claude", "codex", "cursor", "hermes", "pi")


def add_setup_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "setup",
        help="Build/refresh an index and connect a client, or preview with --dry-run.",
    )
    parser.add_argument("--client", choices=CLIENTS, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and render a plan without writes, indexing or subprocesses.",
    )
    parser.add_argument(
        "--client-config",
        type=Path,
        help="Explicit client config path (Pi: SKILL.md); useful for isolated profiles.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="Positive per-step timeout in seconds.",
    )
    parser.set_defaults(handler=handle_setup)


def _no_links(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Setup refuses symlinked configuration or output paths")
        if item.exists() and item != path and not item.is_dir():
            raise ValueError("A setup parent is not a directory")
    if path.exists() and not path.is_file():
        raise ValueError("Configuration destination is not a regular file")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key")
        result[key] = value
    return result


class _UniqueYamlLoader(yaml.SafeLoader):
    pass


def _yaml_mapping(loader: _UniqueYamlLoader, node: Any) -> dict[str, Any]:
    loader.flatten_mapping(node)
    return _unique_object(
        [
            (loader.construct_object(k), loader.construct_object(v))
            for k, v in node.value
        ]
    )


_UniqueYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping
)


def _load_unique_yaml(text: str) -> Any:
    # SafeLoader subclass with duplicate-key rejection. Avoid yaml.load() so
    # Bandit B506 does not treat this as unsafe full-loader YAML.
    loader = _UniqueYamlLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _config_path(client: str, repo: Path) -> Path:
    if client == "hermes":
        return (
            Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
            / "config.yaml"
        )
    return (
        repo
        / {
            "claude": ".mcp.json",
            "codex": ".codex/config.toml",
            "cursor": ".cursor/mcp.json",
            "pi": ".pi/skills/arcgraph/SKILL.md",
        }[client]
    )


def _guide(executable: str, repo: Path, output: Path) -> str:
    # JSON argv is portable; the shell illustration is explicitly POSIX only.
    argv = [executable, "--repo-root", str(repo), "--output-dir", str(output)]
    return "\n".join(
        [
            "---",
            "name: arcgraph",
            "description: Inspect code relationships and change impact with the local ArcGraph CLI.",
            "---",
            "",
            "# ArcGraph project workflow",
            "",
            "Use the following fixed argument prefix (JSON argv; invoke without a shell where possible):",
            "",
            "```json",
            json.dumps(argv, ensure_ascii=False),
            "```",
            "",
            "POSIX shell example (on Windows use the argv array with the host's native quoting):",
            "```sh",
            shlex.join([*argv, "help"]),
            "```",
            "",
            "1. Run help, then current before trusting graph answers and after editing source.",
            "2. For stale indexes run sync --if-stale with this SAME prefix; build a missing/incompatible index. These are explicit index writes.",
            "3. Use bounded context/explain/impact/callers/callees/tests queries on concrete targets. Inspect freshness, target resolution, warnings, analysis limits and truncation.",
            "4. Use source search for exact text and tests for behavior. Empty or unresolved results do not prove absence. CLI shares the same analysis limitations as MCP.",
            "5. Do not default to --raw, execute project code to index it, or change unrelated client configuration.",
            "",
        ]
    )


def _render(
    client: str, old: bytes | None, name: str, server: dict[str, Any], guide: str
) -> bytes:
    if client == "pi":
        result = guide.encode()
        if old is not None and old != result:
            raise ValueError(
                "Existing Pi skill differs; choose another --client-config path or review it manually"
            )
        return result
    try:
        text = (old or b"").decode("utf-8")
        if client == "codex":
            data = tomllib.loads(text)
        elif client == "hermes":
            data = _load_unique_yaml(text) if text.strip() else {}
        else:
            data = (
                json.loads(text, object_pairs_hook=_unique_object)
                if text.strip()
                else {}
            )
    except (ValueError, yaml.YAMLError, UnicodeError) as error:
        raise ValueError(
            "Client configuration is not valid supported JSON/TOML/YAML; existing bytes were not changed"
        ) from error
    if not isinstance(data, dict):
        raise ValueError("Client configuration must be a mapping")
    key = "mcp_servers" if client in {"codex", "hermes"} else "mcpServers"
    servers = data.get(key, {})
    if not isinstance(servers, dict):
        raise ValueError("MCP configuration must be a mapping")
    entry = dict(server)
    if client in {"claude", "cursor"}:
        entry["type"] = "stdio"
    if name in servers:
        if servers[name] != entry:
            raise ValueError(
                "Existing ArcGraph server entry differs; refusing to replace it automatically"
            )
        return old or b""
    if client == "codex":
        # Append one new table; preserve every existing byte/comment and let
        # tomllib reject layouts (e.g. inline sealed tables) that cannot extend.
        result = text + "\n[mcp_servers." + json.dumps(name) + "]\n"
        result += "command = " + json.dumps(server["command"]) + "\n"
        result += "args = " + json.dumps(server["args"]) + "\n"
        try:
            parsed = tomllib.loads(result)
        except ValueError as error:
            raise ValueError(
                "Cannot safely extend this TOML layout; use a separate config"
            ) from error
        expected = {**data, key: {**servers, name: entry}}
        if parsed != expected:
            raise ValueError("TOML append changed unrelated configuration")
        return result.encode()
    data[key] = {**servers, name: entry}
    if client == "hermes":
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()


def _write_config(path: Path, old: bytes | None, new: bytes) -> str | None:
    if old == new:
        return None
    _no_links(path)
    if (path.read_bytes() if path.exists() else None) != old:
        raise ValueError(
            "Configuration changed during setup; rerun instead of overwriting"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if old is not None:
        descriptor, backup = tempfile.mkstemp(
            prefix=path.name + ".arcgraph-backup-", dir=path.parent
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(old)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".arcgraph-config-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix" and path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        _no_links(path)
        if (path.read_bytes() if path.exists() else None) != old:
            raise ValueError(
                "Configuration changed during setup; rerun instead of overwriting"
            )
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def _run_json(command: list[str], timeout: float, repo: Path) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"ArcGraph {command[-1]} exited {result.returncode}; run the reported command directly for diagnostics"
        )
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError("Expected an ArcGraph JSON object")
    return data


def _probe(server: dict[str, Any], timeout: float) -> dict[str, Any]:
    import anyio
    from mcp import StdioServerParameters
    from mcp.client import Client
    from mcp.client.stdio import stdio_client
    from arcgraph.interfaces.agent_capabilities import mcp_capability_names

    async def check() -> dict[str, Any]:
        with anyio.fail_after(timeout):
            with tempfile.TemporaryFile(
                mode="w+", encoding="utf-8", errors="replace"
            ) as errors:
                params = StdioServerParameters(
                    command=server["command"], args=server["args"]
                )
                async with Client(
                    stdio_client(params, errlog=errors),
                    mode="auto",
                    raise_exceptions=True,
                    read_timeout_seconds=timeout,
                ) as session:
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    if names != set(mcp_capability_names()):
                        raise ValueError("Unexpected MCP tool inventory")
                    response = await session.call_tool("arcgraph_index_status", {})
                    payload = response.structured_content
                    if (
                        response.is_error
                        or not payload
                        or payload.get("freshness", {}).get("status") != "fresh"
                    ):
                        raise ValueError("MCP index status is not fresh")
                    return {
                        "status": "pass",
                        "tool_count": len(names),
                        "index_version": payload.get("index_version"),
                        "build_identity": payload.get("build_identity"),
                    }

    return anyio.run(check)


def handle_setup(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "1.0.0",
        "client": args.client,
        "status": "blocked",
        "configuration_written": False,
        "client_connection_verified": False,
        "model_call_verified": False,
        "_exit_code": 1,
    }
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("--timeout must be positive and finite")
        repo = Path(args.repo_root).resolve()
        if not repo.is_dir():
            raise ValueError("Repository root is not a directory")
        output = Path(os.path.abspath(repo / args.output_dir))
        # Validate ancestors without rejecting the index directory itself.
        _no_links(output / "current.json")
        config = Path(
            os.path.abspath(args.client_config or _config_path(args.client, repo))
        )
        _no_links(config)
        selected = _arcgraph_executable_selection()
        executable = str(selected.path)
        if (
            selected.source not in {"invocation", "distribution_record"}
            or not selected.path.is_file()
        ):
            raise ValueError(
                "Use an installed arcgraph executable belonging to this runtime"
            )
        name = "arcgraph-" + hashlib.sha256(str(repo).encode()).hexdigest()[:12]
        prefix = [executable, "--repo-root", str(repo), "--output-dir", str(output)]
        server = {
            "command": executable,
            "args": [
                "mcp",
                "serve",
                "--repo-root",
                str(repo),
                "--output-dir",
                str(output),
            ],
        }
        guide = _guide(executable, repo, output)
        old = config.read_bytes() if config.exists() else None
        rendered = _render(args.client, old, name, server, guide)
        result.update(
            {
                "repo_root": str(repo),
                "output_dir": str(output),
                "client_config": str(config),
                "server_name": name if args.client != "pi" else None,
                "transport": "cli_skill" if args.client == "pi" else "stdio",
                "serve_command": (
                    [server["command"], *server["args"]]
                    if args.client != "pi"
                    else None
                ),
                "cli_prefix": prefix,
                "planned_change": old != rendered,
                "configuration_entry": (
                    server if args.client != "pi" else {"skill": guide}
                ),
                "guidance": guide,
                "warnings": (
                    [
                        "Hermes writes its selected profile config; JSON/YAML edits preserve values but may reformat comments. Existing files are backed up."
                    ]
                    if args.client == "hermes"
                    else []
                ),
                "next_steps": [
                    "Open the target project in the selected client; approve project trust/MCP if requested and reconnect.",
                    "Ask the agent to call arcgraph_help then arcgraph_index_status (Pi: use the arcgraph skill and CLI help/current).",
                    "Registration and a protocol probe do not prove that the client exposed tools to its model.",
                ],
            }
        )
        if args.dry_run:
            result.update(status="planned", _exit_code=0)
            return result
        if args.client != "pi":
            try:
                import mcp.client  # noqa: F401
            except ImportError as error:
                raise ValueError(
                    "Install arcgraph[mcp] in this same environment before setup"
                ) from error
        print("ArcGraph setup: checking index", file=sys.stderr)
        current_path = output / "current.json"
        current = (
            _run_json([*prefix, "current"], args.timeout, repo)
            if current_path.exists()
            else {}
        )
        if current and Path(current.get("repo_root", "")).resolve() != repo:
            raise ValueError(
                "Existing index belongs to another repository; choose a separate --output-dir"
            )
        if current.get("freshness", {}).get("status") != "fresh":
            operation = (
                "sync"
                if current.get("freshness", {}).get("status") == "stale"
                else "build"
            )
            command = [*prefix, operation] + (
                ["--if-stale"] if operation == "sync" else []
            )
            result["index_command"] = command
            print("ArcGraph setup: " + operation + " index", file=sys.stderr)
            _run_json(command, args.timeout, repo)
        current = _run_json([*prefix, "current"], args.timeout, repo)
        if Path(current.get("repo_root", "")).resolve() != repo:
            raise ValueError("Index repository does not match the selected project")
        if current.get("freshness", {}).get("status") != "fresh":
            raise ValueError("Index is not fresh after initialization")
        result["index_version"] = current.get("index_version")
        if args.client != "pi":
            result["protocol_probe"] = _probe(server, min(args.timeout, 60))
        else:
            _run_json([*prefix, "help"], args.timeout, repo)
            result["cli_probe"] = {"status": "pass"}
        backup = _write_config(config, old, rendered)
        result.update(
            status="prepared",
            configuration_written=old != rendered,
            backup_path=backup,
            _exit_code=0,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        ImportError,
        subprocess.SubprocessError,
    ) as error:
        result["error"] = (
            str(error)
            if isinstance(error, (ValueError, RuntimeError))
            else type(error).__name__
        )
    except Exception as error:
        # MCP SDK errors may be exception groups; don't serialize transport
        # internals or client configuration into user-visible diagnostics.
        result["error"] = (
            f"Setup probe failed ({type(error).__name__}); configuration was not applied"
        )
    return result
