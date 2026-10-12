"""One structural view for Python analysis, independent of the host grammar."""

from __future__ import annotations

import ast
import json
import marshal
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import warnings

from arcgraph.core.project_environment import project_interpreter_path
from arcgraph.core.schemas import BuildWarning

STRUCTURE_FIX = (
    "Use the project .venv or set tool.arcgraph.python_environment to an environment "
    "with a Python version satisfying requires-python."
)
MISSING_INTERPRETER = (
    "No project interpreter found; Python structure uses the ArcGraph host parser. "
    + STRUCTURE_FIX
)


def structure_state(root: Path, environment: Path | None = None) -> str:
    """Private incremental cache key; never added to graph/export metadata."""
    view = StructureView(root, environment)
    candidate = project_interpreter_path(root, environment=environment)
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
        if root is None:
            return
        self.interpreter = project_interpreter_path(root, environment=environment)
        if self.interpreter is None:
            self.reason = MISSING_INTERPRETER
            return
        try:
            self._read_project({})
            if self.requirement and not satisfies_python(
                self.version, self.requirement
            ):
                self.reason = (
                    f"Project interpreter Python {self.version} does not satisfy "
                    f"requires-python {self.requirement}; Python structure uses "
                    f"the ArcGraph host parser. {STRUCTURE_FIX}"
                )
        except (
            OSError,
            ValueError,
            EOFError,
            TypeError,
            subprocess.SubprocessError,
        ) as exc:
            self._failed(exc)
        if self.reason:
            self.interpreter = None

    def _failed(self, exc):
        self.reason = (
            f"Project interpreter failed ({type(exc).__name__}); Python structure "
            f"uses the ArcGraph host parser. {STRUCTURE_FIX}"
        )
        self.interpreter = None

    def status(self) -> dict:
        """The effective parser, independently of analysis warnings."""
        return {
            "status": "project" if self.interpreter is not None else "host_fallback",
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
            timeout=30,
            check=True,
        )
        # Primitive structural transport; decoded objects are never executed.
        protocol, self.version, definitions, records = marshal.loads(
            result.stdout
        )  # nosec B302
        if protocol != 1:
            raise ValueError("Unsupported project structure protocol")
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
        return {
            p: (_decode(types, tree) if tree is not None else None, error)
            for p, (tree, error) in records.items()
        }

    def read(
        self, paths: dict[str, Path]
    ) -> tuple[dict[str, ast.Module], list[BuildWarning]]:
        paths = {name: str(path.absolute()) for name, path in paths.items()}
        if self.interpreter is not None:
            try:
                records = self._read_project(paths)
                if set(records) != set(paths):
                    raise ValueError("Incomplete project structure response")
            except (
                OSError,
                ValueError,
                EOFError,
                TypeError,
                subprocess.SubprocessError,
            ) as exc:
                self._failed(exc)
                records = self._read_host(paths)
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
