"""ArcGraph package."""

import importlib.metadata
from pathlib import Path

import tomllib

from arcgraph.core.schemas import READ_SCHEMA_VERSION, SCHEMA_VERSION

_DISTRIBUTION_NAME = "arcgraph"
_UNKNOWN_VERSION = "0.0.0+unknown"


def _version_from_pyproject() -> str | None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def _package_version() -> str:
    source_version = _version_from_pyproject()
    if source_version is not None:
        return source_version
    try:
        return importlib.metadata.version(_DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return _UNKNOWN_VERSION


__version__ = _package_version()

__all__ = ["READ_SCHEMA_VERSION", "SCHEMA_VERSION", "__version__"]
