"""One structural view for Python analysis, independent of the host grammar."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from contextvars import ContextVar
import json
import marshal
from pathlib import Path
import re
import subprocess
import sys
import time
import tomllib
import warnings

from arcgraph.core.project_environment import project_interpreter_path
from arcgraph.core.scanner import FileScanner
from arcgraph.core.schemas import BuildWarning

STRUCTURE_FIX = (
    "Use the project .venv or set tool.arcgraph.python_environment to an environment "
    "with a Python version satisfying requires-python."
)
MISSING_INTERPRETER = (
    "No project interpreter found; Python structure uses the ArcGraph host parser. "
    + STRUCTURE_FIX
)
PROJECT_ERRORS = (
    OSError,
    ValueError,
    EOFError,
    TypeError,
    AttributeError,
    IndexError,
    KeyError,
    RecursionError,
    subprocess.SubprocessError,
)
PROJECT_FAILURE_BUDGET = 60.0
_build_failure_seconds: ContextVar[list[float] | None] = ContextVar(
    "structure_build_failure_seconds", default=None
)
_operation_project_versions: ContextVar[dict | None] = ContextVar(
    "structure_operation_project_versions", default=None
)


@contextmanager
def build_structure_budget():
    """Share failed-read time through preflight, parsing and cache fingerprinting."""
    token = _build_failure_seconds.set([0.0])
    versions_token = _operation_project_versions.set({})
    try:
        yield
    finally:
        _operation_project_versions.reset(versions_token)
        _build_failure_seconds.reset(token)


def fallback_scope(partial, *, exhausted=False):
    """Describe the scope separately from an individual block's failure."""
    if partial and exhausted:
        return (
            "uses the ArcGraph host parser for blocks not completed by the project "
            "interpreter; completed blocks retain project interpreter results."
        )
    return (
        "uses the ArcGraph host parser for the affected block; other blocks "
        "use the project interpreter."
        if partial
        else "uses the ArcGraph host parser."
    )


def _interpreter_version_key(candidate):
    if candidate is None:
        return None
    try:
        info = candidate.stat()
        return (
            str(candidate),
            str(candidate.resolve()),
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        )
    except OSError:
        return None


def _block_path_order(name):
    """Canonical directory traversal: local files before child directories."""
    parts = name.split("/")
    return tuple((1, part) for part in parts[:-1]) + ((0, parts[-1]),)


def structure_state(
    root: Path,
    environment: Path | None = None,
    *,
    previous: str | None = None,
    paths: dict[str, Path] | None = None,
    fallback: str | None = None,
) -> str:
    """Private incremental cache key; never added to graph/export metadata."""
    view = StructureView(root, environment)
    probe_paths = json.loads(previous).get("probe_paths") if previous else None
    if (
        paths is not None
        and fallback
        and fallback.startswith("Project interpreter failed")
    ):
        # Remember the actual failed workload, not an empty capability probe.
        # On later syncs recheck these same blocks: stable failures don't rebuild,
        # and recovery (even without executable changes) refreshes the graph.
        # JSON object key sorting must not change the original block boundaries.
        probe_paths = [
            [name, str(paths[name].absolute())]
            for name in sorted(paths, key=_block_path_order)
        ]
        view.reason = fallback
    elif probe_paths is not None:
        # An old dictionary cache refreshes once into the ordered representation.
        if isinstance(probe_paths, dict):
            probe_paths = list(probe_paths.items())
        view.read({name: Path(path) for name, path in probe_paths})
    else:
        view.probe()
    candidate = project_interpreter_path(root, environment=environment)
    if view._project_budget_exhausted and view.version is None:
        versions = _operation_project_versions.get()
        if versions is not None:
            # A skipped empty probe is not evidence of a missing version. Reuse
            # only a successful probe of this same executable in this operation.
            view.version = versions.get(_interpreter_version_key(candidate))
    identity = None
    if candidate is not None:
        try:
            stat = candidate.stat()
            identity = [
                str(candidate),
                str(candidate.resolve()),
                stat.st_size,
                stat.st_mtime_ns,
            ]
        except OSError:
            identity = [str(candidate)]
    return json.dumps(
        {
            "protocol": 1,
            "interpreter": identity,
            "version": view.version,
            "fallback": view.reason,
            "requirement": view.requirement,
            "host": list(sys.version_info[:3]),
            **({"probe_paths": probe_paths} if probe_paths is not None else {}),
        },
        sort_keys=True,
    )


def python_requirement(root: Path | None) -> str | None:
    if root is None:
        return None
    try:
        config = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
        project = config.get("project")
        value = project.get("requires-python") if isinstance(project, dict) else None
        return value if isinstance(value, str) else None
    except (OSError, ValueError, UnicodeError):
        return None


def satisfies_python(version: str, requirement: str) -> bool:
    """Check numeric Python release clauses without adding a runtime dependency.

    Non-release or unsupported specifiers are conservatively unavailable. Python
    release constraints support comparisons, wildcard ==/!= and compatible ~=.
    """
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
        return False
    actual = tuple(map(int, version.split(".")))
    actual += (0,) * (3 - len(actual))
    for clause in requirement.split(","):
        match = re.fullmatch(
            r"\s*(===|~=|==|!=|<=|>=|<|>)\s*(\d+(?:\.\d+){0,2})(\.\*)?\s*", clause
        )
        if not match:
            return False
        op, text, wildcard = match.groups()
        parts = tuple(map(int, text.split(".")))
        wanted = parts + (0,) * (3 - len(parts))
        if wildcard:
            if op not in {"==", "!="}:
                return False
            equal = actual[: len(parts)] == parts
            ok = equal if op == "==" else not equal
        elif op == "~=":
            if len(parts) < 2:
                return False
            ok = actual >= wanted and actual[: len(parts) - 1] == parts[:-1]
        elif op == "===":
            ok = version == text
        else:
            ok = {
                "==": actual == wanted,
                "!=": actual != wanted,
                "<": actual < wanted,
                "<=": actual <= wanted,
                ">": actual > wanted,
                ">=": actual >= wanted,
            }[op]
        if not ok:
            return False
    return True


def _decode(types, value):
    if isinstance(value, list):
        return [_decode(types, v) for v in value]
    if not isinstance(value, tuple):
        return value
    cls, fields, attributes, singleton = types[value[0]]
    if singleton is not None:
        return singleton
    node = cls()
    # Preserve extra fields (e.g. type_params) even on an older host class.
    if fields != cls._fields:
        node._fields = fields
    if attributes != cls._attributes:
        node._attributes = attributes
    for name, child in zip(fields, value[1]):
        setattr(node, name, _decode(types, child))
    for name, location in zip(attributes, value[2]):
        if location is not None:
            setattr(node, name, location)
    return node


class StructureView:
    """Read files once into AST-compatible, lossless structural records.

    Unknown syntax classes are inert AST subclasses, never compiled or executed.
    Consumers can walk their children; familiar node classes keep isinstance and
    visitor dispatch behaviour. All analyzers/adapters share these same objects.
    """

    def __init__(self, root: Path | None, environment: Path | None = None):
        self.interpreter = None
        self.version = None
        self.reason = None
        self.requirement = python_requirement(root)
        self.root = root
        self.trace_available = False
        self._project_interpreter = None
        self._project_blocks = 0
        self._project_failure = None
        shared = _build_failure_seconds.get()
        self._project_failure_seconds = shared if shared is not None else [0.0]
        self._project_timeout = 30.0
        self._project_budget_exhausted = False
        if root is None:
            return
        self.interpreter = project_interpreter_path(root, environment=environment)
        if self.interpreter is None:
            self.reason = MISSING_INTERPRETER
            return
        try:
            self._attempt_project({})
            self.trace_available = True
            if self.requirement and not satisfies_python(
                self.version, self.requirement
            ):
                self.reason = (
                    f"Project interpreter Python {self.version} does not satisfy "
                    f"requires-python {self.requirement}; Python structure uses "
                    f"the ArcGraph host parser. {STRUCTURE_FIX}"
                )
        except PROJECT_ERRORS as exc:
            self._failed(exc)
        if self.reason:
            self.interpreter = None
        self._project_interpreter = self.interpreter

    def _failed(self, exc, *, partial=False):
        if (
            isinstance(exc, subprocess.TimeoutExpired)
            and exc.timeout == 0
            and self._project_budget_exhausted
        ):
            detail = "failed-read budget exhausted"
        elif isinstance(exc, subprocess.TimeoutExpired):
            detail = f"timed out after {exc.timeout:g} seconds"
        elif isinstance(exc, subprocess.CalledProcessError):
            detail = f"exit code {exc.returncode}"
        elif isinstance(exc, OSError):
            detail = f"startup error: {type(exc).__name__}"
        else:
            detail = f"protocol error: {type(exc).__name__}"
        scope = fallback_scope(partial, exhausted=self._project_budget_exhausted)
        self.reason = (
            f"Project interpreter failed ({detail}); Python structure "
            f"{scope} {STRUCTURE_FIX}"
        )
        if self._project_budget_exhausted:
            self.reason += (
                f" Further project reads use the host parser after the "
                f"{PROJECT_FAILURE_BUDGET:g}-second failed-read budget is exhausted."
            )
        if not partial:
            self.interpreter = None

    def probe(self, *, all_files=False):
        """Doctor and healthy cache fingerprints exercise a real project file."""
        if self.interpreter is not None and self.root is not None:
            files = FileScanner(self.root).scan()
            if files:
                self.read(
                    {
                        file.path: Path(file.abs_path)
                        for file in (files if all_files else files[:1])
                    }
                )

    def status(self) -> dict:
        """The effective parser, independently of analysis warnings."""
        return {
            "status": (
                ("partial_fallback" if self.reason else "project")
                if self.interpreter is not None
                else "host_fallback"
            ),
            "interpreter": str(self.interpreter or sys.executable),
            "version": (
                self.version if self.interpreter is not None else sys.version.split()[0]
            ),
            "reason": self.reason,
            "fix": STRUCTURE_FIX if self.reason else None,
        }

    def _read_project(self, paths):
        script = Path(__file__).with_name("_structure_worker.py").read_text("utf-8")
        # No inherited environment (PYTHONPATH/HOME/VIRTUAL_ENV/etc.), and no
        # project directory on the process cwd or Python's isolated import path.
        # The filesystem anchor is neutral even when TMPDIR points into the
        # project. No site means no .pth or sitecustomize in the selected venv.
        # Located executable and fixed arguments, no shell.
        result = subprocess.run(  # nosec B603
            [str(self.interpreter), "-I", "-S", "-c", script],
            input=marshal.dumps(paths, 4),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(sys.executable).anchor,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            timeout=self._project_timeout,
            check=True,
        )
        # Primitive structural transport; decoded objects are never executed.
        protocol, version, definitions, records = marshal.loads(
            result.stdout
        )  # nosec B302
        if protocol != 1:
            raise ValueError("Unsupported project structure protocol")
        if not isinstance(version, str) or not isinstance(records, dict):
            raise ValueError("Invalid project structure response")
        self.version = version
        types = []
        for name, fields, attributes, base in definitions:
            cls = getattr(ast, name, None)
            if cls is None:
                cls = type(
                    name,
                    (getattr(ast, base, ast.AST),),
                    {
                        "_fields": fields,
                        "_attributes": attributes,
                    },
                )
            singleton = cls() if not fields and not attributes else None
            types.append((cls, fields, attributes, singleton))
        if set(records) != set(paths):
            raise ValueError("Incomplete project structure response")
        decoded = {
            p: (_decode(types, tree) if tree is not None else None, error)
            for p, (tree, error) in records.items()
        }
        if any(
            (tree is not None and not isinstance(tree, ast.Module))
            or (error is not None and not isinstance(error, str))
            for tree, error in decoded.values()
        ):
            raise ValueError("Invalid project structure record")
        return decoded

    def _attempt_project(self, paths):
        # Reserve a fixed amount for timeout cancellation/reaping. Successful
        # reads and host fallback work are outside the failed-project budget.
        limit = PROJECT_FAILURE_BUDGET - min(0.5, PROJECT_FAILURE_BUDGET / 100)
        remaining = limit - self._project_failure_seconds[0]
        if remaining <= 0:
            self._project_budget_exhausted = True
            raise subprocess.TimeoutExpired([str(self.interpreter)], 0)
        self._project_timeout = min(30.0, remaining)
        started = time.monotonic()
        version_key = _interpreter_version_key(self.interpreter)
        try:
            result = self._read_project(paths)
            versions = _operation_project_versions.get()
            if (
                versions is not None
                and version_key is not None
                and version_key == _interpreter_version_key(self.interpreter)
            ):
                versions[version_key] = self.version
            return result
        except PROJECT_ERRORS:
            self._project_failure_seconds[0] += time.monotonic() - started
            self._project_budget_exhausted = self._project_failure_seconds[0] >= limit
            raise
        finally:
            self._project_timeout = 30.0

    def read(
        self, paths: dict[str, Path], *, continue_project=False
    ) -> tuple[dict[str, ast.Module], list[BuildWarning]]:
        # Semantic conversion streams multiple reads through this view. Retry
        # its selected interpreter after a workload failure and retain successes
        # across calls; startup/version failures never become retry candidates.
        # Default build/sync keep their existing per-read fallback policy.
        if continue_project:
            self.interpreter = self._project_interpreter
        paths = {name: str(path.absolute()) for name, path in paths.items()}
        if self.interpreter is not None:
            records = {}
            failures = []
            project_blocks = self._project_blocks if continue_project else 0
            items = [
                (name, paths[name]) for name in sorted(paths, key=_block_path_order)
            ]
            # Successful blocks survive failures. Failed project reads share one
            # lifetime budget, including sequential semantic 64-file reads.
            for offset in range(0, len(items), 256):
                block = dict(items[offset : offset + 256])
                try:
                    records.update(self._attempt_project(block))
                    project_blocks += 1
                except PROJECT_ERRORS as exc:
                    failures.append(exc)
                    records.update(self._read_host(block))
            if continue_project:
                self._project_blocks = project_blocks
                if failures:
                    self._project_failure = failures[0]
                if self._project_failure is not None:
                    self._failed(self._project_failure, partial=bool(project_blocks))
            elif failures:
                self._failed(failures[0], partial=bool(project_blocks))
        else:
            records = self._read_host(paths)
        notices = []
        trees = {}
        for path, (tree, error) in records.items():
            if tree is not None:
                trees[path] = tree
            else:
                notices.append(
                    BuildWarning(kind="parse_error", path=path, message=error)
                )
        return trees, notices

    @staticmethod
    def _read_host(paths):
        records = {}
        for name, path in paths.items():
            try:
                source = Path(path).read_text(encoding="utf-8-sig")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(source, filename=name)
                records[name] = (tree, None)
            except (OSError, SyntaxError, UnicodeError, RecursionError) as exc:
                message = str(exc)
                if isinstance(exc, SyntaxError):
                    message += (
                        f" (parsed with Python {sys.version_info[0]}."
                        f"{sys.version_info[1]}; syntax added in a later Python "
                        "needs ArcGraph to run on that version)"
                    )
                records[name] = (None, message)
        return records
