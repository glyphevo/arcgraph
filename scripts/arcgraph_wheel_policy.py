"""The reviewed source-only package boundary shared by both artifact gates.

Do not derive permission to omit files from arbitrary Hatch patterns: an overly
broad exclude would then hide a product omission. Only the exact reviewed
exclude spellings below are accepted. New source-only directories or alternative
spellings require a policy review. This is not a general Hatch glob interpreter;
other selection settings are checked through the resulting artifact membership.
"""

from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Any, Iterable

WHEEL_TEST_DIR = "arcgraph/tests"
SOURCE_ONLY_PACKAGE_DIRS = ("arcgraph/semantic_prototype",)


def source_only_package_files(names: Iterable[str]) -> set[str]:
    """Match a whole directory boundary, including its own archive entry."""
    return {
        name
        for name in names
        if any(
            name == directory or name.startswith(directory + "/")
            for directory in SOURCE_ONLY_PACKAGE_DIRS
        )
    }


def validate_wheel_policy(repo_root: Path) -> list[str]:
    """Cross-check the reviewed boundary against the source build configuration.

    The source configuration and tracked manifest must belong to the artifact's
    tree; this function cannot authenticate caller-supplied provenance. It checks
    exclude declarations, not all Hatch selection or custom hook behavior. Actual
    membership checks must still reject missing product and included source-only
    files. sdist selection is deliberately independent of this wheel policy.
    """
    try:
        config = tomllib.loads(
            (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        build: dict[str, Any] = config["tool"]["hatch"]["build"]
        excluded = build["targets"]["wheel"].get("exclude", [])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"Cannot validate reviewed wheel exclusions: {exc}"]
    expected = {
        pattern
        for directory in (WHEEL_TEST_DIR, *SOURCE_ONLY_PACKAGE_DIRS)
        for pattern in ("/" + directory, "/" + directory + "/**")
    }
    if (
        not isinstance(excluded, list)
        or not all(isinstance(pattern, str) for pattern in excluded)
        or set(excluded) != expected
        or build.get("exclude")
    ):
        return ["Wheel exclusions differ from the reviewed source-only policy"]
    return []
