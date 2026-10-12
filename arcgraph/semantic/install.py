"""The only semantic operation that downloads: locked, verified pyright setup."""

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import uuid

VERSION = "1.1.414"
URL = "https://registry.npmjs.org/pyright/-/pyright-1.1.414.tgz"
INTEGRITY = "sha512-FPZZb51jepDX4eP7TEYDeNFtmE3WgwkkEcJpvH3/QmUSsj0EAy3LXu+xB4T/FWDejtsXlCfFr/rbWFjwuwuXww=="
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_CONTENT_BYTES = 64 * 1024 * 1024


class InstallError(RuntimeError):
    pass


def cache_root():
    if sys.platform == "darwin":
        root = Path.home() / "Library/Caches/arcgraph"
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise InstallError("Semantic cache unavailable: LOCALAPPDATA is not set.")
        root = Path(local) / "arcgraph"
    else:
        root = (
            Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
            / "arcgraph"
        )
    # HOME/cache roots are trusted anchors, including macOS filesystem aliases.
    # All subsequent installation paths are anchored in this resolved directory.
    return root.resolve()


def installation_root():
    return cache_root() / "semantic" / ("pyright-" + VERSION)


def no_links(path, *, boundary=None):
    boundary = (boundary or cache_root()).resolve()
    path = path.absolute()
    if not path.is_relative_to(boundary):
        raise InstallError("Semantic content escapes its trusted directory.")
    for item in (path, *path.parents):
        if (
            item.is_symlink()
            or getattr(item.lstat() if item.exists() else None, "st_reparse_tag", 0)
            == 0xA0000003
        ):
            raise InstallError(f"Semantic cache path passes through a link: {item}")
        if item == boundary:
            break


def check_hash(path):
    checksum = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    actual = "sha512-" + base64.b64encode(checksum.digest()).decode("ascii")
    if actual != INTEGRITY:
        raise InstallError(
            "Pyright archive hash mismatch; expected the locked 1.1.414 npm sha512."
        )


def members(archive):
    """Validate all entries before creating any package file; no extractall."""
    result, seen, total = [], set(), 0
    for member in archive:
        path = PurePosixPath(member.name)
        if (
            not member.isreg()
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != member.name
            or "\\" in member.name
            or PureWindowsPath(member.name).drive
            or len(path.parts) < 2
            or path.parts[0] != "package"
            or any(":" in part for part in path.parts)
            or member.name in seen
            or member.size < 0
        ):
            raise InstallError(f"Unsafe pyright archive member: {member.name!r}")
        total += member.size
        if total > MAX_CONTENT_BYTES or len(result) >= 10000:
            raise InstallError("Unsafe pyright archive: content limits exceeded.")
        seen.add(member.name)
        result.append(member)
    if not result:
        raise InstallError("Unsafe pyright archive: no package files.")
    for name in seen:
        if any(parent.as_posix() in seen for parent in PurePosixPath(name).parents):
            raise InstallError("Unsafe pyright archive: file/directory collision.")
    return result


def validate_installation():
    """Recompute expected bytes from the pinned archive, never trust a receipt."""
    root = installation_root()
    pointer = root / "active.json"
    no_links(pointer)
    try:
        if not stat.S_ISREG(pointer.stat().st_mode):
            raise InstallError("Semantic installation pointer is not a regular file.")
        info = json.loads(pointer.read_text(encoding="utf-8"))
        name = info["installation"]
        if set(info) != {"installation"} or not re.fullmatch(
            r"installation-[0-9a-f]{32}", name
        ):
            raise InstallError("Invalid semantic installation pointer.")
        target = root / name
        return validate_content(target)
    except InstallError:
        raise
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        raise InstallError(f"Pyright {VERSION} is missing or invalid: {exc}") from exc


def validate_content(target, *, trusted_root=None):
    """Verify either an installed generation or its private runtime copy."""
    try:
        boundary = trusted_root or cache_root()
        no_links(target, boundary=boundary)
        archive_path = target / "archive.tgz"
        no_links(archive_path, boundary=boundary)
        if not stat.S_ISREG(archive_path.stat().st_mode):
            raise InstallError("Semantic archive is not a regular file.")
        check_hash(archive_path)
        for path in target.rglob("*"):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0):
                raise InstallError(f"Semantic installation contains a link: {path}")
        expected = {"archive.tgz"}
        directories = set()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in members(archive):
                path = target / member.name
                if (
                    not stat.S_ISREG(path.stat().st_mode)
                    or path.stat().st_size != member.size
                ):
                    raise InstallError(
                        f"Pyright installation content mismatch: {member.name}"
                    )
                with (
                    archive.extractfile(member) as source,
                    path.open("rb") as installed,
                ):
                    while True:
                        block = source.read(1024 * 1024)
                        if block != installed.read(len(block)):
                            raise InstallError(
                                f"Pyright installation content mismatch: {member.name}"
                            )
                        if not block:
                            break
                expected.add(member.name)
                directories.update(
                    parent.as_posix()
                    for parent in PurePosixPath(member.name).parents
                    if parent.as_posix() != "."
                )
        for path in target.rglob("*"):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0):
                raise InstallError(f"Semantic installation contains a link: {path}")
            if path.is_dir():
                if path.relative_to(target).as_posix() not in directories:
                    raise InstallError("Unexpected directory in pyright installation.")
            elif (
                not stat.S_ISREG(path.stat().st_mode)
                or path.relative_to(target).as_posix() not in expected
            ):
                raise InstallError("Unexpected file in pyright installation.")
        package = target / "package"
        if (
            json.loads((package / "package.json").read_text(encoding="utf-8"))[
                "version"
            ]
            != VERSION
        ):
            raise InstallError("Pyright installation version mismatch.")
        if not (package / "langserver.index.js").is_file():
            raise InstallError("Pyright language server entry is missing.")
        return package
    except InstallError:
        raise
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        raise InstallError(f"Pyright {VERSION} is missing or invalid: {exc}") from exc


def install(from_path=None):
    root = installation_root()
    no_links(root)
    try:
        package = validate_installation()
    except InstallError:
        package = None
    if package is not None:
        return {
            "status": "unchanged",
            "pyright_version": VERSION,
            "path": str(package),
            "message": "Pyright 1.1.414 is already installed and verified.",
        }
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=root) as temporary:
            stage = Path(temporary)
            tgz = stage / "archive.tgz"
            if from_path is not None:
                local = Path(from_path)
                info = local.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARCHIVE_BYTES:
                    raise InstallError(
                        "Local pyright archive must be a regular file within the archive size limit."
                    )
                shutil.copyfile(local, tgz)
            else:
                try:
                    # URL is the locked HTTPS npm archive, never a user argument.
                    with (
                        urllib.request.urlopen(
                            URL, timeout=30
                        ) as response,  # nosec B310
                        tgz.open("xb") as output,
                    ):  # nosec B310
                        size = 0
                        while block := response.read(1024 * 1024):
                            size += len(block)
                            if size > MAX_ARCHIVE_BYTES:
                                raise InstallError(
                                    "Pyright download exceeds the locked archive size limit."
                                )
                            output.write(block)
                except (OSError, urllib.error.URLError) as exc:
                    raise InstallError(
                        f"Pyright download failed: {exc}; use arcgraph setup semantic --from LOCAL_TGZ for offline setup."
                    ) from exc
            check_hash(tgz)
            with tarfile.open(tgz, "r:gz") as archive:
                approved = members(archive)
                for member in approved:
                    target = stage / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        archive.extractfile(member) as source,
                        target.open("xb") as output,
                    ):
                        shutil.copyfileobj(source, output)
            if (
                json.loads(
                    (stage / "package/package.json").read_text(encoding="utf-8")
                )["version"]
                != VERSION
            ):
                raise InstallError("Pyright archive version mismatch.")
            name = "installation-" + uuid.uuid4().hex
            os.replace(stage, root / name)
            # Old installations remain auditable; replacing this pointer is the
            # one publication step, including when repairing a corrupt install.
            pending = root / (".active-" + uuid.uuid4().hex)
            try:
                with pending.open("x", encoding="utf-8") as handle:
                    json.dump({"installation": name}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(pending, root / "active.json")
            finally:
                pending.unlink(missing_ok=True)
        package = validate_installation()
        return {
            "status": "installed",
            "pyright_version": VERSION,
            "path": str(package),
            "message": "Installed and verified pyright 1.1.414.",
        }
    except InstallError:
        raise
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        raise InstallError(
            f"Cannot install pyright in {root}: {exc}; check the archive and cache directory permissions."
        ) from exc
