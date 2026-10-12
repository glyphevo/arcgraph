"""Node-only pyright sessions with generated configuration and private state."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from arcgraph.core.project_environment import project_environment_path
from .install import VERSION, InstallError, validate_installation, validate_content
from .lsp import Client, LspError


def configuration(root, python_version, environment=None, *, source_roots=(".",)):
    """Use D2's established version/environment; never select another interpreter."""
    version = ".".join(python_version.split(".")[:2])
    project = project_environment_path(root, environment)
    if project is None or not (project / "pyvenv.cfg").is_file():
        project = root / ".semantic-empty-environment" / "absent"
    return {
        "pythonVersion": version,
        "pythonPlatform": {"darwin": "Darwin", "win32": "Windows"}.get(
            sys.platform, "Linux"
        ),
        "venvPath": str(project.parent),
        "venv": project.name,
        "extraPaths": list(
            dict.fromkeys([str(root / path) for path in source_roots] + [str(root)])
        ),
        "autoSearchPaths": False,
        "autoImportCompletions": False,
        "useLibraryCodeForTypes": True,
        "typeCheckingMode": "off",
        "include": ["**/*.py"],
    }


def initialize_params(root):
    return {
        "processId": None,
        "rootUri": root.as_uri(),
        "workspaceFolders": [{"uri": root.as_uri(), "name": "frozen"}],
        "capabilities": {
            "general": {"positionEncodings": ["utf-8", "utf-16"]},
            "textDocument": {"callHierarchy": {"dynamicRegistration": False}},
        },
    }


def capabilities(result):
    values = (result or {}).get("capabilities", {})
    unit = values.get("positionEncoding", "utf-16")
    provider = values.get("callHierarchyProvider")
    if unit not in ("utf-8", "utf-16") or not (
        provider is True or isinstance(provider, dict)
    ):
        raise InstallError(
            "Pyright does not declare callHierarchy with a supported position encoding."
        )
    return unit


def isolated_environment(directory, node):
    """PATH contains exactly one node entry; inherited Python/Node settings vanish."""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "bin"
    binary.mkdir()
    private_node = binary / ("node.exe" if os.name == "nt" else "node")
    if os.name == "nt":
        shutil.copyfile(node, private_node)
    else:
        private_node.symlink_to(node)
    env = {"PATH": str(binary)}
    for key in ("HOME", "TMPDIR", "TMP", "TEMP", "USERPROFILE", "LOCALAPPDATA"):
        env[key] = str(directory)
    if os.name == "nt" and "SystemRoot" in os.environ:
        env["SystemRoot"] = os.environ["SystemRoot"]
    return env


@contextmanager
def session(root, config, *, transcript=None):
    """Validate the pinned package before executing fixed argv, without a shell."""
    package = validate_installation()
    located = shutil.which("node")
    if located is None:
        raise InstallError("Node is missing; install Node.js (version 14 or newer).")
    node = Path(located).resolve()
    with tempfile.TemporaryDirectory(prefix="arcgraph-semantic-") as temporary:
        private = Path(temporary)
        # Node reads an independently verified private generation, not mutable
        # cache files that could be replaced after the initial check.
        copied = private / "installation"
        shutil.copytree(package.parent, copied, symlinks=True)
        package = validate_content(copied)
        env = isolated_environment(private / "state", node)
        version = subprocess.run(  # nosec B603
            [str(node), "--version"],
            cwd=private,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        ).stdout.strip()
        try:
            major = int(version.removeprefix("v").split(".")[0])
        except ValueError as exc:
            raise InstallError("Cannot determine Node version.") from exc
        if major < 14:
            raise InstallError(
                f"Node {version} is unsupported; install Node.js 14 or newer."
            )
        # Config is ArcGraph-generated. Do not consume the project's pyright config.
        (root / "pyrightconfig.json").write_text(
            json.dumps(config, sort_keys=True), encoding="utf-8"
        )
        peer = Client(
            [str(node), str(package / "langserver.index.js"), "--stdio"],
            root,
            env,
            {"python": {"analysis": config}},
            transcript=transcript,
        )
        try:
            yield peer, {
                "node": str(node),
                "node_version": version,
                "pyright_version": VERSION,
            }
        finally:
            peer.close()


def semantic_core_check(repo_root, *, python_version, environment=None):
    """Probe only the semantic toolchain; a supplied D2 version is never reprobed."""
    fix = "Run arcgraph setup semantic; install Node.js 14 or newer if Node is missing."
    try:
        with tempfile.TemporaryDirectory(
            prefix="arcgraph-semantic-doctor-"
        ) as temporary:
            root = Path(temporary)
            config = configuration(repo_root, python_version, environment)
            config["include"] = []
            with session(root, config) as (peer, detail):
                initialize = peer.request(
                    "initialize", initialize_params(root), timeout=10
                )
                unit = capabilities(initialize)
                peer.notify("initialized", {})
                detail.update(position_encoding=unit, call_hierarchy=True)
        return {
            "name": "semantic_core",
            "status": "pass",
            "message": "Pyright 1.1.414 and Node are verified; isolated callHierarchy is available.",
            "details": detail,
        }
    except (
        InstallError,
        OSError,
        ValueError,
        LspError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as exc:
        return {
            "name": "semantic_core",
            "status": "warn",
            "message": str(exc),
            "fix": fix,
        }
