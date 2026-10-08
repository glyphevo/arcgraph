"""Explicit experiment entry point, separate from arcgraph's CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from .backends import arcgraph_records, oracle, producer, pyright_records
from .contract import write_generation
from .lsp import Client
from .shadow import project
from .snapshot import capture, canonical, sha
from .contract import Source


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument(
        "--files", required=True, type=Path, help="JSON array of relative source paths"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--pyright-package",
        required=True,
        type=Path,
        help="Existing pyright 1.1.414 package directory",
    )
    parser.add_argument("--node", required=True)
    parser.add_argument("--target-python", default="3.14")
    parser.add_argument("--platform", default="Darwin")
    parser.add_argument("--oracle-python")
    args = parser.parse_args(argv)
    package = json.loads(
        (args.pyright_package / "package.json").read_text(encoding="utf-8")
    )
    if package["version"] != "1.1.414":
        parser.error("this prototype requires pyright 1.1.414")
    if args.output.exists():
        parser.error("output must be a new directory")
    args.output.mkdir(parents=True)
    paths = json.loads(args.files.read_text(encoding="utf-8"))
    frozen = args.output.resolve() / "snapshot"
    snapshot = capture(
        args.source.resolve(),
        paths,
        frozen,
        target_python=args.target_python,
        platform=args.platform,
    )
    # The generated target configuration is also a frozen input in the manifest.
    config = {
        "pythonVersion": args.target_python,
        "pythonPlatform": args.platform,
        "include": paths,
        "venvPath": str(args.output.resolve() / "empty-env"),
        "venv": "none",
    }
    config_bytes = json.dumps(config, sort_keys=True).encode()
    (frozen / "pyrightconfig.json").write_bytes(config_bytes)
    rows = [f for f in snapshot.files if f.path != "pyrightconfig.json"]
    rows.append(
        Source(
            path="pyrightconfig.json",
            raw_digest=sha(config_bytes),
            text_digest=sha(canonical(config_bytes).encode()),
        )
    )
    snapshot = snapshot.model_copy(
        update={"files": tuple(sorted(rows, key=lambda f: f.path))}
    )
    data = oracle(snapshot, frozen, args.oracle_python)
    p = producer(
        "pyright", package["version"], ("3.11", "3.12", "3.13", "3.14"), config
    )
    a = producer(
        "arcgraph",
        "prototype/0.1",
        ("3.11", "3.12", "3.13", "3.14"),
        {"oracle": data["python"]},
    )
    command = [
        args.node,
        str(args.pyright_package.resolve() / "langserver.index.js"),
        "--stdio",
    ]
    # Even a caller with a real HOME gets an isolated backend process.
    with tempfile.TemporaryDirectory(prefix="semantic-peer-") as temporary:
        env = dict(os.environ)
        for key in list(env):
            if (
                key.startswith(("PYTHON", "PIP_", "npm_config_", "NPM_CONFIG_"))
                or key == "VIRTUAL_ENV"
            ):
                env.pop(key)
        for key in (
            "HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "TMPDIR",
        ):
            env[key] = temporary
        with Client(command, frozen, env, {}) as peer:
            pg, metadata = pyright_records(snapshot, frozen, p, data, peer)
        metadata["transcript"] = peer.transcript
    ag, _ = arcgraph_records(snapshot, frozen, a, data)
    write_generation(pg, args.output / "pyright.json")
    write_generation(ag, args.output / "arcgraph.json")
    (args.output / "lsp.json").write_text(
        json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
    )
    (args.output / "shadow.json").write_text(
        json.dumps(project(pg, ag), ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
