"""Capture external definitions while the verified LSP session is still alive."""

import ast
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit

from .contract import Definition, Envelope, Span, digest
from .snapshot import canonical, native_span, sha


def file_path(uri):
    """Accept only local, absolute file URIs with unambiguous escaping."""
    if not isinstance(uri, str) or re.search(r"%(?![0-9a-fA-F]{2})", uri):
        return None
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "file"
        or parsed.netloc.lower() not in ("", "localhost")
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        value = unquote(parsed.path, errors="strict")
    except UnicodeError:
        return None
    if "\x00" in value or "\\" in value:
        return None
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", value):
        value = value[1:]
    path = Path(value)
    return path if path.is_absolute() and ".." not in path.parts else None


def safe_file(path, root):
    if not path.is_relative_to(root):
        return False
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            return False
    return path.is_file() and path.resolve() == path


def read_allowed(path, root):
    """Open beneath a directory descriptor, so link swaps cannot redirect reads."""
    if os.open in os.supports_dir_fd:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = path.relative_to(root).parts
            for name in parts[:-1]:
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
            )
        finally:
            os.close(directory)
    elif os.name == "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_BINARY)
        try:
            # Check the opened handle's resolved name before reading any byte.
            import ctypes
            import msvcrt

            function = ctypes.WinDLL(
                "kernel32", use_last_error=True
            ).GetFinalPathNameByHandleW
            function.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ]
            function.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32768)
            length = function(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise OSError("Cannot verify the opened external definition")
            final = buffer.value
            if final.startswith("\\\\?\\UNC\\"):
                final = "\\\\" + final[8:]
            elif final.startswith("\\\\?\\"):
                final = final[4:]
            if not Path(final).is_relative_to(root):
                raise OSError("Opened external definition escapes the allowed root")
        except BaseException:
            os.close(descriptor)
            raise
    else:
        raise OSError("Safe external definition reads are unavailable")
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("External definition is not a regular file")
        return stream.read()


def module_name(relative, origin):
    parts = list(Path(relative).parts)
    if origin == "typeshed":
        if parts[0] == "stdlib":
            parts = parts[1:]
        elif len(parts) > 2 and parts[0] == "stubs":
            parts = parts[2:]
        else:
            return None
    if not parts or Path(parts[-1]).suffix not in (".py", ".pyi"):
        return None
    parts[-1] = Path(parts[-1]).stem
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts and all(p.isidentifier() for p in parts) else None


class Capture:
    def __init__(self, root, config):
        self.root = root.resolve()
        self.config = config
        self.files = {}

    def item(self, item, detail):
        path = file_path(item.get("uri"))
        if path is None or path.is_relative_to(self.root):
            return
        roots = []
        typeshed = detail.get("typeshed")
        if typeshed:
            roots.append((Path(typeshed), "typeshed"))
        # The generated search environment is the already frozen private copy.
        environment = self.root.parent / "environment"
        configured = Path(self.config.get("venvPath", "")) / self.config.get("venv", "")
        if configured == environment:
            roots.extend(
                (p, "dependency") for p in environment.glob("lib/python*/site-packages")
            )
            if (environment / "Lib/site-packages").is_dir():
                roots.append((environment / "Lib/site-packages", "dependency"))
        for allowed, origin in roots:
            if (
                allowed.is_symlink()
                or not allowed.is_dir()
                or allowed.resolve() != allowed
            ):
                continue
            if not safe_file(path, allowed):
                continue
            relative = path.relative_to(allowed).as_posix()
            module = module_name(relative, origin)
            if module is None:
                return
            try:
                raw = read_allowed(path, allowed)
            except OSError:
                return
            row = {
                "origin": origin,
                "relative": relative,
                "module": module,
                "text": canonical(raw),
                "raw_digest": sha(raw),
            }
            row["text_digest"] = sha(row["text"].encode())
            old = self.files.get(item["uri"])
            if old is not None and old != row:
                raise ValueError(
                    "input_changed_during_run: external definition changed"
                )
            self.files[item["uri"]] = row
            return


def definition(row, item, unit):
    """Prove module and qualified name from syntax; never infer a runtime alias."""
    if row.get("module") != module_name(row["relative"], row["origin"]) or row.get(
        "text_digest"
    ) != sha(row["text"].encode()):
        raise ValueError("external definition capture mismatch")
    selection = native_span(row["text"], item["selectionRange"], unit)
    tree = ast.parse(row["text"])
    choices = []
    names = []

    def visit(body, parents=(), declaration=False):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = ".".join((row["module"], *parents, node.name))
                names.append(qualified)
                span = Span(
                    start=(node.lineno - 1, node.col_offset),
                    end=(node.end_lineno - 1, node.end_col_offset),
                )
                if (
                    node.name == item["name"]
                    and span.start <= selection.start < selection.end <= span.end
                ):
                    line = row["text"].splitlines()[selection.start[0]].encode()
                    if (
                        selection.start[0] == selection.end[0]
                        and line[selection.start[1] : selection.end[1]].decode()
                        == node.name
                    ):
                        decorators = tuple(ast.unparse(d) for d in node.decorator_list)
                        abstract = declaration or any(
                            d.split(".")[-1] == "abstractmethod" for d in decorators
                        )
                        choices.append(
                            (
                                node,
                                span,
                                ".".join((row["module"], *parents, node.name)),
                                decorators,
                                abstract,
                            )
                        )
                if isinstance(node, ast.ClassDef):
                    protocol = any(
                        ast.unparse(b).split("[")[0].split(".")[-1] == "Protocol"
                        for b in node.bases
                    )
                    visit(node.body, (*parents, node.name), declaration or protocol)
            elif isinstance(node, ast.If):
                visit(node.body, parents, declaration)
                visit(node.orelse, parents, declaration)

    visit(tree.body)
    if len(choices) != 1 or names.count(choices[0][2]) != 1:
        return None
    node, span, qualname, decorators, abstract = choices[0]
    return {
        "id": "extsym:" + qualname,
        "path": row["origin"] + "/" + row["relative"],
        "span": span,
        "name_span": selection,
        "name": node.name,
        "qualname": qualname,
        "symbol_kind": "class" if isinstance(node, ast.ClassDef) else "function",
        "owner": "extsym:" + qualname.rsplit(".", 1)[0],
        "decorators": decorators,
        "declaration_only": row["relative"].endswith(".pyi") or abstract,
    }


class Registry:
    def __init__(self, files, checksum):
        if digest(files) != checksum:
            raise ValueError("external capture checksum mismatch")
        self.files = files
        self.checksum = checksum
        self.definitions = {}
        self.proofs = {}

    def lookup(self, item, unit, records):
        row = self.files.get(item.get("uri"))
        if row is None:
            return None
        try:
            value = definition(row, item, unit)
        except (SyntaxError, UnicodeError, KeyError, TypeError, ValueError):
            return None
        if value is None:
            return None
        target = Definition(
            envelope=Envelope(
                generation=records.generation_id,
                producer=records.producer_id,
                snapshot=records.snapshot_id,
                record_id=digest((self.checksum, value["id"], row["text_digest"])),
                provenance=("external_definition", row["origin"], row["text_digest"]),
            ),
            **value,
        )
        old = self.definitions.get(target.id)
        if old is not None and old != target:
            return None
        self.definitions[target.id] = target
        self.proofs[target.id] = (item, unit)
        records.view.defs[target.id] = target
        return target

    def admitted(self, run, view):
        config = dict(run.producer.configuration)
        if (
            config.get("external_capture") != json.dumps(self.checksum)
            or digest(self.files) != self.checksum
        ):
            return False
        for identity, target in self.definitions.items():
            item, unit = self.proofs[identity]
            value = definition(self.files[item["uri"]], item, unit)
            if (
                value is None
                or view.defs.get(identity) != target
                or any(getattr(target, key) != field for key, field in value.items())
                or target.envelope.snapshot != run.snapshot.id
                or target.envelope.producer != run.producer.id
                or target.envelope.generation != run.id
            ):
                return False
        return True
