"""Node-only pyright sessions with generated configuration and private state."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

from arcgraph.core.project_environment import project_environment_path
from .install import VERSION, InstallError, validate_installation, validate_content
from .lsp import Client, LspError


def semantic_core_enabled(repo_root):
    """Read the project switch; accept only TOML booleans."""
    path = Path(repo_root) / "pyproject.toml"
    if not path.exists():
        return False
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    tool = config.get("tool", {})
    arcgraph = tool.get("arcgraph", {}) if isinstance(tool, dict) else {}
    if not isinstance(arcgraph, dict):
        raise RuntimeError("[tool.arcgraph] must be a TOML table")
    value = arcgraph.get("semantic_core", False)
    if type(value) is not bool:
        raise RuntimeError(
            "[tool.arcgraph] semantic_core must be a boolean: true or false"
        )
    return value


def semantic_core_status(status, *, reason=None, node=None, fix=None):
    """The approved summary fields, shared by build and sync."""
    return {
        "status": status,
        "pyright_version": VERSION if status != "disabled" else None,
        "node": node,
        "reason": reason,
        "fix": fix,
    }


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


def _node_identity(node):
    info = node.stat()
    return (str(node), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


class SessionPool:
    """One checked private installation for a single sequential collection."""

    def __init__(self):
        self.temporary = None
        self.receipt = None
        self.node_receipt = None

    def close(self):
        if self.temporary is not None:
            self.temporary.cleanup()

    def content_receipt(self, root=None):
        import stat
        from .install import no_links
        from .external_targets import read_allowed
        from .snapshot import sha

        root = root or self.package.parent
        no_links(root, boundary=self.private)
        result = []
        for path in [root, *sorted(root.rglob("*"))]:
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise InstallError(
                    "Private pyright installation contains a link or special file."
                )
            result.append(
                (
                    path.relative_to(root).as_posix(),
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_size,
                    (
                        sha(read_allowed(path, root))
                        if stat.S_ISREG(info.st_mode)
                        else None
                    ),
                )
            )
        no_links(root, boundary=self.private)
        return tuple(result)

    def check_node(self):
        """Resolve and probe Node again, admitting only the original file identity."""
        located = shutil.which("node")
        if located is None:
            raise InstallError(
                "Node is missing; install Node.js (version 14 or newer)."
            )
        node = Path(located).resolve()

        before = _node_identity(node)
        self.version = subprocess.run(  # nosec B603
            [str(node), "--version"],
            cwd=self.private,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        ).stdout.strip()
        after = _node_identity(node)
        if before != after or (
            self.node_receipt is not None and after != self.node_receipt
        ):
            raise InstallError("Node identity changed during collection.")
        try:
            major = int(self.version.removeprefix("v").split(".")[0])
        except ValueError as exc:
            raise InstallError("Cannot determine Node version.") from exc
        if major < 14:
            raise InstallError(
                f"Node {self.version} is unsupported; install Node.js 14 or newer."
            )
        self.node = node
        self.node_receipt = after

    def check_view(self, root):
        receipt = self.content_receipt(root)

        # Directories have independent sizes/inodes. Every file must have the
        # admitted bytes and mode, and must not share the retained copy's inode.
        def content(rows):
            return [(r[0], r[3], r[4] if r[5] else None, r[5]) for r in rows]

        if content(receipt) != content(self.receipt) or any(
            view[5] is not None and view[1:3] == base[1:3]
            for view, base in zip(receipt, self.receipt)
        ):
            raise InstallError(
                "Private pyright installation changed during collection."
            )
        return receipt

    @contextmanager
    def package_view(self):
        # Independent URI partitions preserve declaration/reexport mapping.
        # Copy bytes: mutations of the retained installation cannot reach a view.
        with tempfile.TemporaryDirectory(prefix="session-", dir=self.private) as name:
            copied = Path(name) / "installation"
            shutil.copytree(self.package.parent, copied, symlinks=True)
            receipt = self.check_view(copied)
            try:
                yield copied / "package"
            finally:
                if self.content_receipt(copied) != receipt:
                    raise InstallError(
                        "Private pyright installation changed during collection."
                    )

    def prepare(self):
        if self.temporary is not None:
            self.check_node()
            if self.content_receipt() != self.receipt:
                raise InstallError(
                    "Private pyright installation changed during collection."
                )
            return
        package = validate_installation()
        located = shutil.which("node")
        if located is None:
            raise InstallError(
                "Node is missing; install Node.js (version 14 or newer)."
            )
        self.node = Path(located).resolve()
        self.temporary = tempfile.TemporaryDirectory(prefix="arcgraph-semantic-")
        self.private = Path(self.temporary.name).resolve()
        copied = self.private / "installation"
        shutil.copytree(package.parent, copied, symlinks=True)
        self.package = validate_content(copied, trusted_root=self.private)
        self.env = isolated_environment(self.private / "state", self.node)
        self.check_node()
        self.receipt = self.content_receipt()


@contextmanager
def session(root, config, *, transcript=None, pool=None):
    """Launch from a checked private installation; reuse only within this run."""
    if pool is None:
        pool = SessionPool()
        try:
            with session(root, config, transcript=transcript, pool=pool) as result:
                yield result
        finally:
            pool.close()
        return
    pool.prepare()
    (root / "pyrightconfig.json").write_text(
        json.dumps(config, sort_keys=True), encoding="utf-8"
    )
    with pool.package_view() as package:
        pool.check_node()
        pool.check_view(package.parent)
        peer = Client(
            [str(pool.node), str(package / "langserver.index.js"), "--stdio"],
            root,
            pool.env,
            {"python": {"analysis": config}},
            transcript=transcript,
        )
        try:
            yield peer, {
                "node": str(pool.node),
                "node_version": pool.version,
                "pyright_version": VERSION,
                "typeshed": str(package / "dist/typeshed-fallback"),
            }
        finally:
            peer.close()
            if pool.content_receipt() != pool.receipt:
                raise InstallError(
                    "Private pyright installation changed during collection."
                )


def semantic_core_check(
    repo_root, *, python_version, environment=None, output_dir=None
):
    """Probe only the semantic toolchain; a supplied D2 version is never reprobed."""
    fix = "Run arcgraph setup semantic; install Node.js 14 or newer if Node is missing."
    try:
        build_state = {}
        if semantic_core_enabled(repo_root):
            from arcgraph.core.graph_store import GraphStoreReader

            output = (
                Path(output_dir)
                if output_dir is not None
                else Path(repo_root) / "output/arcgraph"
            )
            try:
                store = GraphStoreReader.from_current(output)
                build_state = store.metadata.get("toolchain_status", {}).get(
                    "semantic_core", {}
                )
            except FileNotFoundError:
                pass
            except (OSError, ValueError, RuntimeError) as exc:
                return {
                    "name": "semantic_core",
                    "status": "warn",
                    "message": "Cannot read the published semantic build: " + str(exc),
                    "fix": "Run arcgraph build to replace the missing or incompatible index.",
                }
            if build_state.get("status") in ("unavailable", "partial"):
                return {
                    "name": "semantic_core",
                    "status": "warn",
                    "message": "The current build uses semantic_core="
                    + build_state["status"]
                    + ": "
                    + str(build_state.get("reason") or "not fully covered"),
                    "fix": str(build_state.get("fix") or fix)
                    + " Run arcgraph build to retry semantic analysis.",
                }
        with tempfile.TemporaryDirectory(
            prefix="arcgraph-semantic-doctor-"
        ) as temporary:
            root = Path(temporary).resolve()
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
            "message": "Pyright 1.1.414 and Node are verified; isolated callHierarchy is available."
            + (" " + build_state["reason"] if build_state.get("reason") else ""),
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
