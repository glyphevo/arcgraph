"""Freeze the exact default-build sources and the static dependency search view."""

import hashlib
import os
from pathlib import Path
import shutil
import stat

from arcgraph.core.project_environment import project_environment_path
from arcgraph.core.scanner import FileScanner, TYPESCRIPT_FILE_EXTENSIONS
from .contract import digest
from .snapshot import capture, sha


def dependency_files(environment):
    """Only inputs to static Python import/type resolution, with logical paths."""
    if environment is None or not (environment / "pyvenv.cfg").is_file():
        return []
    result = [("pyvenv.cfg", environment / "pyvenv.cfg")]
    sites = sorted(environment.glob("lib/python*/site-packages"))
    if (environment / "Lib/site-packages").is_dir():
        sites.append(environment / "Lib/site-packages")
    for site in sites:
        for directory, names, files in os.walk(site, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in names):
                raise ValueError("Dependency search directory passes through a link")
            names[:] = sorted(name for name in names if name != "__pycache__")
            base = Path(directory)
            for name in sorted(files):
                path = base / name
                if path.suffix in (".py", ".pyi") or name in (
                    "py.typed",
                    "METADATA",
                    "top_level.txt",
                ):
                    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                        raise ValueError(
                            "Dependency search input is not a regular file"
                        )
                    result.append((path.relative_to(environment).as_posix(), path))
    return result


def environment_identity(environment):
    rows = []
    for name, path in dependency_files(environment):
        checksum = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                checksum.update(block)
        rows.append((name, checksum.hexdigest()))
    return digest(rows), rows


def source_identity(root):
    files = FileScanner(
        root, file_extensions=(".py", ".pyi", ".typed", *TYPESCRIPT_FILE_EXTENSIONS)
    ).scan()
    return (
        digest(sorted((file.path, file.file_hash, file.module) for file in files)),
        files,
    )


def verify_environment(environment, expected):
    if environment_identity(environment)[0] != expected:
        raise ValueError("input_changed_during_run: frozen dependency changed")


def verify_live_sources(snapshot, root, expected):
    from .snapshot import verify

    verify(snapshot, root)
    if source_identity(root)[0] != expected:
        raise ValueError("input_changed_during_run: source scope changed")


def freeze_dependencies(environment, destination):
    rows = []
    for name, source in dependency_files(environment):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # .pth and interpreter binaries are deliberately absent: no computed
        # paths or Python execution is part of this static search profile.
        shutil.copyfile(source, target)
        rows.append((name, sha(target.read_bytes())))
    return digest(rows)


def freeze_build(reader, source_root, destination, python_status, *, environment=None):
    files = reader.read_files()
    # Default graph file hashes bind the capture to what the build actually read.
    for file in files:
        if sha((source_root / file.path).read_bytes()) != file.file_hash:
            raise ValueError("input_changed_during_run: default build source differs")
    paths = {file.path for file in files if file.path.endswith(".py")}
    source_digest, semantic_inputs = source_identity(source_root)
    paths.update(
        file.path
        for file in semantic_inputs
        if file.path.endswith(".pyi") or Path(file.path).name == "py.typed"
    )
    paths.update(
        name
        for name in ("pyproject.toml", ".gitignore", "package.json", "tsconfig.json")
        if (source_root / name).is_file()
    )
    selected = project_environment_path(source_root, environment)
    frozen_environment = destination.parent / "environment"
    dependency_digest = freeze_dependencies(selected, frozen_environment)
    original_digest, rows = environment_identity(selected)
    if dependency_digest != original_digest:
        raise ValueError("input_changed_during_run: dependency snapshot differs")
    snapshot = capture(
        source_root,
        sorted(paths),
        destination,
        target_python=".".join(python_status["version"].split(".")[:2]),
        platform=__import__("sys").platform,
        environment=(
            ("dependencies", dependency_digest),
            ("python_version", python_status["version"]),
            ("search_profile", "frozen-site-packages-no-pth/1"),
            ("source_roots", digest(reader.metadata["source_roots"])),
            ("source_scope", source_digest),
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "module_names": tuple(
                sorted(
                    (file.path, file.module)
                    for file in files
                    if file.path.endswith(".py")
                )
            )
        }
    )
    return (
        snapshot,
        frozen_environment,
        {
            "original_environment": str(selected) if selected else None,
            "dependency_digest": dependency_digest,
            "dependency_file_count": len(rows),
            "source_digest": source_digest,
        },
    )
