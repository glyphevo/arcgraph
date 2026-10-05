"""Minimal read-only ArcGraph MCP host example.

The preferred command is now `arcgraph mcp serve`. This example
keeps the lower-level host shape visible for developers who need to embed the
same read-only facade in a compatible MCP runtime.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from arcgraph.interfaces.mcp_server import create_mcp_app, create_tool_group
from arcgraph.interfaces.mcp_tools import ArcGraphMCPToolGroup


def build_tool_group(
    *,
    repo_root: Path,
    output_dir: str,
    repo_id: str,
    allowed_roots: list[Path] | None = None,
) -> ArcGraphMCPToolGroup:
    return create_tool_group(
        repo_root=repo_root,
        repo_id=repo_id,
        output_dir=output_dir,
        allowed_roots=allowed_roots,
        expose_source_snippets=False,
    )


def create_fastmcp_app(
    *,
    name: str,
    repo_root: Path,
    output_dir: str,
    repo_id: str,
    allowed_roots: list[Path] | None = None,
) -> Any:
    return create_mcp_app(
        name=name,
        repo_root=repo_root,
        output_dir=output_dir,
        repo_id=repo_id,
        allowed_roots=allowed_roots,
        expose_source_snippets=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a minimal read-only ArcGraph MCP host example."
    )
    parser.add_argument("--repo-root", default=".", help="Repository to expose.")
    parser.add_argument(
        "--output-dir",
        default="output/arcgraph",
        help="ArcGraph output directory relative to repo root.",
    )
    parser.add_argument("--repo-id", default="default", help="Registered repo id.")
    parser.add_argument(
        "--allowed-root",
        action="append",
        default=[],
        help=(
            "Allowed filesystem root. Can be repeated. Defaults to --repo-root. "
            "Requests outside allowed roots are rejected or sanitized."
        ),
    )
    parser.add_argument("--name", default="ArcGraph Read-Only Example")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    allowed_roots = [Path(root).resolve() for root in args.allowed_root]
    mcp = create_fastmcp_app(
        name=args.name,
        repo_root=repo_root,
        output_dir=args.output_dir,
        repo_id=args.repo_id,
        allowed_roots=allowed_roots or None,
    )
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
