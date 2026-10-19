"""Decode file URIs before comparing workspace boundaries or rebinding roots."""

import ntpath
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
from urllib.parse import quote, unquote, urlsplit


def workspace_path(root, uri: str) -> str | None:
    """Decode using the snapshot's path flavour, then check containment.

    Pure paths allow Windows drive/UNC cases to be checked on any host;
    native paths additionally resolve symlinks before mapping a symbol.
    """
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.query or parsed.fragment:
        return None
    value = unquote(parsed.path)
    if "\x00" in value:
        return None
    if isinstance(root, PureWindowsPath):
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            value = "//" + unquote(parsed.netloc) + value
        elif (
            len(value) >= 4
            and value[0] == "/"
            and value[1].isalpha()
            and value[2:4] == ":/"
        ):
            value = value[1:]
        value = ntpath.normpath(value)
    else:
        if parsed.netloc.lower() not in ("", "localhost"):
            return None
        value = posixpath.normpath(value)
    path = (
        PureWindowsPath(value)
        if isinstance(root, PureWindowsPath)
        else Path(value) if isinstance(root, Path) else PurePosixPath(value)
    )
    if not path.is_absolute():
        return None
    if isinstance(path, Path):
        path = path.resolve()
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else None


def uri_root(uri):
    """Use pure paths when rebinding journals whose original tree may be gone."""
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.query or parsed.fragment:
        return None
    value = unquote(parsed.path)
    if "\x00" in value:
        return None
    if parsed.netloc.lower() not in ("", "localhost"):
        path = PureWindowsPath("//" + unquote(parsed.netloc) + value)
    elif (
        len(value) >= 4
        and value[0] == "/"
        and value[1].isalpha()
        and value[2:4] == ":/"
    ):
        path = PureWindowsPath(value[1:])
    else:
        path = PurePosixPath(value)
    return path if path.is_absolute() else None


def rebase_uri(uri, original, replacement):
    if not uri.startswith("file:"):
        return uri
    try:
        root = uri_root(original)
        relative = workspace_path(root, uri) if root is not None else None
    except ValueError:
        return uri
    if relative is None:
        return uri
    return (
        replacement
        if relative == "."
        else replacement + "/" + quote(relative, safe="/")
    )
