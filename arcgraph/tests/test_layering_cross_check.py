"""Check this repository's layering without using this repository's analyzer.

`arcgraph ci`'s `layer_violations` check reads `imports` edges out of the graph,
which ArcGraph's own `ImportAnalyzer` produced. A blind spot in that analyzer
therefore reaches the gate as a missing edge, and a missing edge reads as clean
layering: the gate would report zero violations because it saw nothing, not
because there was nothing to see.

These tests recompute the same verdict from the standard library's `ast`, so the
two derivations share only the layer model in `pyproject.toml`. If the analyzer
ever stops reporting an import that crosses a layer inward, the gate stays green
and this file turns red.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _layer_model() -> (
    tuple[dict[str, int], list[tuple[str, str]], set[tuple[str, str]]]
):
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ci = config["tool"]["arcgraph"]["ci"]
    order = {layer["name"]: index for index, layer in enumerate(ci["layers"])}
    prefixes = [
        (path, layer["name"]) for layer in ci["layers"] for path in layer["paths"]
    ]
    exemptions = {
        (entry["from"], entry["to"]) for entry in ci.get("layer_exemptions", [])
    }
    return order, prefixes, exemptions


def _layer_for(candidate: str, prefixes: list[tuple[str, str]]) -> str | None:
    for prefix, name in prefixes:
        if candidate.startswith(prefix):
            return name
    return None


def _imported_modules(path: Path, node: ast.AST) -> list[str]:
    """Resolve an import node to the absolute module names it names.

    A relative import has to be resolved the way Python resolves it, or a
    crossing hides inside the dots: `from ..interfaces.cli import x` in
    `arcgraph/core/` names the interfaces layer, and treating it as the
    importing file's own package classifies it as same-layer and drops it.
    This mirrors `ImportAnalyzer._resolve_import_from_base`, which the gate
    already gets right, so the two derivations agree on what an import names
    while still disagreeing about where the fact came from.
    """

    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if not node.level:
        return [node.module] if node.module else []

    # The importing file's package: its parent directory, for a module and for
    # an `__init__.py` alike.
    package_parts = list(path.parent.relative_to(REPO_ROOT).parts)
    keep_count = len(package_parts) - node.level + 1
    if keep_count < 0:
        return []
    base_parts = package_parts[:keep_count]
    if node.module:
        base_parts.extend(node.module.split("."))
    resolved = ".".join(part for part in base_parts if part)
    return [resolved] if resolved else []


def _cross_layer_imports() -> list[tuple[str, str, str, str]]:
    """Return (source_path, imported_module, source_layer, target_layer) tuples.

    Every `Import`/`ImportFrom` node is walked, not only module-level ones: an
    import nested in a function still couples the two layers, and a lazy import
    is exactly the shape a static analyzer is most likely to miss.
    """

    order, prefixes, _ = _layer_model()
    found: list[tuple[str, str, str, str]] = []
    for path in sorted(REPO_ROOT.glob("arcgraph/**/*.py")):
        posix = path.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in posix:
            continue
        source_layer = _layer_for(posix, prefixes)
        if source_layer is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for module in _imported_modules(path, node):
                if not module.startswith("arcgraph"):
                    continue
                target_layer = _layer_for(module.replace(".", "/") + "/", prefixes)
                if target_layer is None or target_layer == source_layer:
                    continue
                if target_layer not in order or source_layer not in order:
                    continue
                found.append((posix, module, source_layer, target_layer))
    return found


def test_no_inward_import_escapes_the_layer_gate() -> None:
    """An independent parse agrees with the gate that nothing imports inward."""

    order, _, exemptions = _layer_model()
    crossings = _cross_layer_imports()
    # A vacuous pass is the failure this whole file exists to prevent, so the
    # scan has to have classified something before its verdict means anything.
    assert len(crossings) > 100, (
        f"only {len(crossings)} cross-layer imports were classified; the scan "
        "is not seeing this tree and its verdict proves nothing"
    )
    violations = [
        crossing
        for crossing in crossings
        if order[crossing[3]] > order[crossing[2]]
        and (crossing[2], crossing[3]) not in exemptions
    ]
    assert violations == []


def test_every_declared_exemption_still_describes_a_real_import() -> None:
    """A stale exemption silently widens the gate, so it must stay earned."""

    _, _, exemptions = _layer_model()
    observed = {(source, target) for _, _, source, target in _cross_layer_imports()}
    unused = sorted(exemptions - observed)
    assert unused == [], (
        f"these layer exemptions no longer match any import: {unused}; "
        "remove them rather than leaving the gate wider than the tree needs"
    )


def _parse_one(source: str) -> ast.AST:
    return ast.parse(source).body[0]


def test_relative_imports_resolve_to_the_module_they_actually_name() -> None:
    """The scan never exercises this: the tree has no relative imports.

    That is exactly why it is tested directly. A resolver that is wrong only
    on a shape the repository does not currently contain is a gap that opens
    the day someone writes one, and the scan would stay green while it opened.
    """

    module = REPO_ROOT / "arcgraph" / "core" / "graph_store.py"
    package_init = REPO_ROOT / "arcgraph" / "core" / "__init__.py"

    # `from . import x` and `from .x import y` name the file's own package.
    assert _imported_modules(module, _parse_one("from . import scanner")) == [
        "arcgraph.core"
    ]
    assert _imported_modules(module, _parse_one("from .scanner import Scanner")) == [
        "arcgraph.core.scanner"
    ]
    # One dot more leaves the package, which is where a crossing can hide.
    assert _imported_modules(
        module, _parse_one("from ..interfaces.cli import build_parser")
    ) == ["arcgraph.interfaces.cli"]
    assert _imported_modules(module, _parse_one("from .. import interfaces")) == [
        "arcgraph"
    ]
    # An `__init__.py` is its own package, so one dot means the same package.
    assert _imported_modules(package_init, _parse_one("from . import scanner")) == [
        "arcgraph.core"
    ]
    # Dots that climb past the root name nothing rather than raising.
    assert _imported_modules(module, _parse_one("from ..... import x")) == []


def test_a_relative_inward_import_is_classified_as_a_crossing() -> None:
    """The layer verdict must not depend on how an import is spelled."""

    _, prefixes, _ = _layer_model()
    module = REPO_ROOT / "arcgraph" / "core" / "graph_store.py"
    relative = _imported_modules(
        module, _parse_one("from ..interfaces.cli import build_parser")
    )
    absolute = _imported_modules(
        module, _parse_one("from arcgraph.interfaces.cli import build_parser")
    )
    assert relative == absolute
    assert _layer_for(relative[0].replace(".", "/") + "/", prefixes) == "interfaces"
