"""Node-only pyright sessions with generated configuration and private state."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import traceback

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


class _ViewPermissionError(PermissionError):
    """View recovery already diagnosed permissions; skip the mode receipt check."""


def _restore_view_mode(path, mode):
    immutable = bool(getattr(path.stat(), "st_flags", 0) & stat.UF_IMMUTABLE)
    # Like tempfile._resetperms, clear flags before chmod.
    if hasattr(os, "chflags"):
        os.chflags(path, 0)
    path.chmod(mode)
    return immutable


def _without_immutable(rows):
    return tuple((*r[:6], r[6] & ~getattr(stat, "UF_IMMUTABLE", 0)) for r in rows)


class SessionPool:
    """One checked private installation for a single sequential collection."""

    def __init__(self):
        self.temporary = None
        self.receipt = None
        self.node_receipt = None

    def close(self):
        if self.temporary is not None:
            self.temporary.cleanup()

    def _close_after_error(self, error):
        try:
            self.close()
        except BaseException as cleanup:
            if isinstance(error, GeneratorExit):
                # generator.close() discards GeneratorExit, including its notes.
                if isinstance(cleanup, GeneratorExit):
                    raise RuntimeError(
                        "Private pyright installation cleanup raised GeneratorExit."
                    ) from cleanup
                raise
            # Keep the existing cause and expose the cleanup's full traceback.
            error.add_note(
                "Private pyright installation cleanup failed:\n"
                + "".join(traceback.format_exception(cleanup, chain=False))
            )

    @contextmanager
    def lifetime(self):
        try:
            yield self
        except BaseException as error:
            self._close_after_error(error)
            raise
        else:
            self.close()

    def content_receipt(self, root=None):
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
                    getattr(info, "st_flags", 0),
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
        if receipt != self.receipt:
            raise InstallError(
                "Private pyright installation changed during collection."
            )
        return receipt

    @contextmanager
    def readonly_view(self, root, receipt):
        """Freeze checked modes through launch, then restore them for cleanup.

        This guards accidental damage and drift from concurrent tools. It does
        not isolate a process running as the same user and controlling timing;
        that process can also rewrite ArcGraph itself.

        On POSIX, removing directory write bits prevents creating, deleting or
        renaming entries, and removing file write bits prevents in-place writes.
        On Windows, chmod only marks existing files read-only: in-place writes
        and deletion are denied, but directory entries can still be replaced.
        """
        container_mode = root.parent.stat().st_mode
        pending = None
        try:
            for row in reversed(receipt):
                (root / row[0]).chmod(row[3] & ~0o222)
            root.parent.chmod(container_mode & ~0o222)
            sealed = tuple((*r[:3], r[3] & ~0o222, *r[4:]) for r in receipt)
            try:
                yield
            finally:
                current = self.content_receipt(root)

                # Immutable flags are diagnosed after clearing them for cleanup;
                # preserve an exception already propagating from the session.
                if _without_immutable(current) != _without_immutable(sealed):
                    raise InstallError(
                        "Private pyright installation changed during collection."
                    )
        except BaseException as exc:
            pending = exc
            raise
        finally:
            restore_error = None
            immutable = False

            try:
                immutable |= _restore_view_mode(root.parent, container_mode)
            except OSError as exc:
                restore_error = exc
            for row in receipt:
                try:
                    path = root / row[0]
                    # Never follow a replacement link or restore another inode.
                    if not path.is_symlink() and path.exists():
                        info = path.stat()
                        if (info.st_dev, info.st_ino) == row[1:3]:
                            immutable |= _restore_view_mode(path, row[3])
                            # The run copy survives successful sessions. Keep
                            # admitted nonzero flags after clearing for chmod.
                            if row[6] and hasattr(os, "chflags"):
                                os.chflags(path, row[6])
                except OSError as exc:
                    if restore_error is None:
                        restore_error = exc
            if restore_error is not None:
                raise _ViewPermissionError(
                    f"Unable to restore private pyright view permissions: {restore_error}"
                ) from (pending if pending is not None else restore_error)
            if immutable and pending is None:
                raise _ViewPermissionError(
                    "Private pyright view permissions changed during collection: "
                    "user-immutable flags were cleared for cleanup."
                )

    @contextmanager
    def package_view(self):
        # Sequential sessions share the sealed run copy, never the cache inode.
        self.check_view(self.package.parent)
        try:
            yield self.package
            self.check_view(self.package.parent)
        except BaseException as error:
            # A failed collection cannot reuse its view; tempfile clears flags
            # before modes, while readonly_view preserves the original error.
            self._close_after_error(error)
            raise

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
        # Verify bytes before removing write permissions, and derive the sealed
        # receipt from those admitted bytes rather than trusting a new snapshot.
        for row in reversed(self.receipt):
            (copied / row[0]).chmod(row[3] & ~0o222)
        self.private.chmod(self.private.stat().st_mode & ~0o222)
        self.receipt = tuple((*r[:3], r[3] & ~0o222, *r[4:]) for r in self.receipt)
        self.check_view(copied)


@contextmanager
def session(root, config, *, transcript=None, pool=None):
    """Launch from a checked private installation; reuse only within this run."""
    if pool is None:
        with SessionPool().lifetime() as pool:
            with session(root, config, transcript=transcript, pool=pool) as result:
                yield result
        return
    pool.prepare()
    (root / "pyrightconfig.json").write_text(
        json.dumps(config, sort_keys=True), encoding="utf-8"
    )
    with pool.package_view() as package:
        pool.check_node()
        receipt = pool.check_view(package.parent)
        with pool.readonly_view(package.parent, receipt):
            pool.check_node()
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
