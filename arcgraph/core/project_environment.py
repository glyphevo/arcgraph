"""Read a project's venv metadata and selected source without executing Python.

Only an explicit directory or the project's .venv is used. Ambient VIRTUAL_ENV,
the tool's interpreter and executable .pth files are deliberately not consulted.
This is source evidence; editable installs and computed import paths stay unknown.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from email.parser import Parser
from pathlib import Path

SOURCES = {
    "starlette": ("testclient.py",),
    "fastapi": ("testclient.py",),
    "httpx": ("_client.py",),
    "httpx2": ("_client.py",),
}


def project_environment_path(
    repo_root: Path | None, explicit: Path | None = None
) -> Path | None:
    if explicit is not None:
        return explicit.resolve()
    if repo_root is None:
        return None
    try:
        config = tomllib.loads((repo_root / "pyproject.toml").read_text("utf-8"))
        configured = (
            config.get("tool", {}).get("arcgraph", {}).get("python_environment")
        )
    except (OSError, ValueError):
        configured = None
    if not isinstance(configured, str) or not configured:
        configured = ".venv"
    return (repo_root / configured).resolve()


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def project_interpreter_path(
    repo_root: Path, explicit=None, *, environment: Path | None = None
) -> Path | None:
    """Locate, but never execute, a project interpreter using the shared path.

    Preserve the venv executable path: resolving its symlink to the base Python
    would run with the base environment instead of the project's packages.
    """
    if explicit is not None:
        candidate = Path(explicit).absolute()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise RuntimeError(f"Project interpreter is unavailable: {candidate}")
        return candidate
    environment = project_environment_path(repo_root, environment)
    if environment is None or not (environment / "pyvenv.cfg").is_file():
        return None
    candidate = environment / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


class ProjectEnvironment:
    """A frozen read of the relevant dependency inputs for one analysis."""

    def __init__(self, repo_root: Path | None, explicit: Path | None = None):
        self.path = project_environment_path(repo_root, explicit)
        self.source_bytes: dict[tuple[str, str], bytes] = {}
        self.metadata = {
            "path": str(self.path) if self.path else None,
            "status": "unknown",
            "reason": "project_environment_missing",
            "distributions": {},
        }
        if self.path is None or not self.path.exists():
            return
        try:
            cfg = (self.path / "pyvenv.cfg").read_bytes()
            sites = sorted(self.path.glob("lib/python*/site-packages"))
            windows_site = self.path / "Lib/site-packages"
            if windows_site.is_dir():
                sites.append(windows_site)
            if len(sites) != 1:
                self.metadata["reason"] = "project_environment_ambiguous"
                return
            self.metadata.update(
                status="available",
                reason=None,
                interpreter_configuration_sha256=hashlib.sha256(cfg).hexdigest(),
            )
            self._read_distributions(sites[0])
        except (OSError, UnicodeError):
            self.metadata.update(
                status="unknown", reason="project_environment_unreadable"
            )

    def _read_distributions(self, site: Path) -> None:
        candidates = {}
        for path in sorted(site.glob("*.dist-info/METADATA")):
            raw = path.read_bytes()
            parsed = Parser().parsestr(raw.decode("utf-8"))
            name = _name(parsed.get("Name", ""))
            if name in SOURCES:
                candidates.setdefault(name, []).append((raw, parsed.get("Version")))
        for name, matches in candidates.items():
            if len(matches) != 1 or not matches[0][1]:
                continue
            raw, version = matches[0]
            sources = {}
            for relative in SOURCES[name]:
                try:
                    data = (site / name / relative).read_bytes()
                except OSError:
                    continue
                self.source_bytes[name, relative] = data
                sources[relative] = hashlib.sha256(data).hexdigest()
            self.metadata["distributions"][name] = {
                "name": name,
                "version": version,
                "metadata_sha256": hashlib.sha256(raw).hexdigest(),
                "sources": sources,
            }

    def source(self, name: str, relative: str) -> bytes | None:
        return self.source_bytes.get((name, relative))


def dependency_environment_changed(metadata: dict, repo_root: Path) -> bool:
    """Legacy Python indexes must replace the unbound host dependency evidence."""
    python = metadata.get("extractor_metadata", {}).get("python-v1-compat-shim")
    stored = python.get("dependency_environment") if isinstance(python, dict) else None
    if not isinstance(stored, dict):
        return isinstance(python, dict)
    if stored.get("path") is None:
        # A caller-supplied frontend without project context did not consult
        # an environment. Changing the indexer's root cannot change its inputs.
        return False
    explicit = stored.get("explicit_path")
    current = ProjectEnvironment(repo_root, Path(explicit) if explicit else None)
    if explicit:
        current.metadata["explicit_path"] = explicit
    return stored != current.metadata
