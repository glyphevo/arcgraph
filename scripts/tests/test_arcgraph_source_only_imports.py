"""Prevent tracked wheel Python modules from statically importing source-only code.

This guard shares the reviewed wheel boundary; it does not execute imports.
It covers import/from statements, relative imports, aliases, and nested or
TYPE_CHECKING blocks. Dynamic imports, module names in strings, attribute access
through an imported package, and indirect re-exports/star imports are blind
spots. Only tracked .py sources are inspected; generated code, custom build
hooks, other languages and artifact bytes need their separate artifact checks.
"""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess

import pytest

from scripts.arcgraph_wheel_policy import SOURCE_ONLY_PACKAGE_DIRS, WHEEL_TEST_DIR

ROOT = Path(__file__).resolve().parents[2]


def _source_only_imports(path: str, source: str) -> list[str]:
    package = path.removesuffix(".py").split("/")[:-1]
    forbidden = [directory.replace("/", ".") for directory in SOURCE_ONLY_PACKAGE_DIRS]
    violations = []
    for node in ast.walk(ast.parse(source, filename=path)):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            prefix = package[: len(package) - node.level + 1] if node.level else []
            if node.module:
                prefix.extend(node.module.split("."))
            base = ".".join(prefix)
            modules = [base, *(base + "." + alias.name for alias in node.names)]
        for module in modules:
            if any(
                module == name or module.startswith(name + ".") for name in forbidden
            ):
                violations.append(f"{path}:{node.lineno}: {module}")
    return violations


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("arcgraph/core/example.py", "import arcgraph.semantic_prototype"),
        (
            "arcgraph/core/example.py",
            "import arcgraph.semantic_prototype.model as model",
        ),
        ("arcgraph/core/example.py", "from arcgraph.semantic_prototype import model"),
        (
            "arcgraph/core/example.py",
            "from arcgraph import semantic_prototype as proto",
        ),
        ("arcgraph/core/example.py", "from ..semantic_prototype import model"),
        ("arcgraph/core/example.py", "from .. import semantic_prototype"),
        ("arcgraph/__init__.py", "from . import semantic_prototype"),
        ("arcgraph/core/__init__.py", "from ..semantic_prototype.model import Record"),
        ("arcgraph/core/nested/example.py", "from ... import semantic_prototype"),
        (
            "arcgraph/core/example.py",
            "def f():\n    import arcgraph.semantic_prototype",
        ),
        (
            "arcgraph/core/example.py",
            "if TYPE_CHECKING:\n    from arcgraph.semantic_prototype import model",
        ),
    ],
)
def test_source_only_import_guard_rejects_static_forms(path: str, source: str) -> None:
    assert _source_only_imports(path, source)


@pytest.mark.parametrize(
    "source",
    [
        "import arcgraph.core.schemas",
        "from .. import core",
        "from arcgraph import semantic_prototype_product",
        "import arcgraph.semantic_prototype_product",
        'message = "import arcgraph.semantic_prototype"',
        "# import arcgraph.semantic_prototype",
    ],
)
def test_source_only_import_guard_preserves_directory_boundary(source: str) -> None:
    assert _source_only_imports("arcgraph/core/example.py", source) == []


def test_tracked_wheel_modules_do_not_import_source_only_packages() -> None:
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z", "--", "arcgraph"], cwd=ROOT)
        .decode("utf-8")
        .split("\0")
    )
    excluded = (WHEEL_TEST_DIR, *SOURCE_ONLY_PACKAGE_DIRS)
    violations = []
    for path in tracked:
        if not path.endswith(".py") or any(
            path == directory or path.startswith(directory + "/")
            for directory in excluded
        ):
            continue
        violations.extend(
            _source_only_imports(path, (ROOT / path).read_text(encoding="utf-8"))
        )
    assert (
        violations == []
    ), "Shipped modules import source-only packages:\n" + "\n".join(violations)
