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


def native_identity(target, python_version=None):
    """A concrete builtin method has an existing, tested runtime symbol identity.

    Its source declaration remains a stub. This proof never rewrites a protocol
    or abstract declaration to a builtin implementation, nor imports a project.
    """
    from arcgraph.analyzers.calls.constants import (
        BUILTIN_METHODS_BY_TYPE,
        BUILTIN_TYPE_ATTRIBUTE_METHODS,
    )

    path = target["path"] if isinstance(target, dict) else target.path
    qualname = target["qualname"] if isinstance(target, dict) else target.qualname
    if path != "typeshed/stdlib/builtins.pyi":
        return False
    parts = qualname.split(".")
    minimum = {
        "builtins.int.is_integer": (3, 12),
        "builtins.bytearray.resize": (3, 14),
        "builtins.float.from_number": (3, 14),
    }.get(qualname)
    if (
        minimum is not None
        and python_version is not None
        and tuple(int(p) for p in python_version.split(".")[:2]) < minimum
    ):
        return False
    return (
        len(parts) == 3
        and parts[0] == "builtins"
        and parts[2]
        in (
            BUILTIN_METHODS_BY_TYPE.get(parts[1], set())
            | BUILTIN_TYPE_ATTRIBUTE_METHODS.get(parts[1], set())
        )
    )


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


def overridden_stubs(tree, module):
    """Apply the existing same-file concrete-override declaration convention."""
    from collections import defaultdict
    from .structure_facts import stub_body

    classes = defaultdict(list)
    rebound = set()

    def collect(body, prefix):
        for node in body:
            if isinstance(node, ast.ClassDef):
                name = prefix + "." + node.name
                classes[name].append(node)
                collect(node.body, name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rebound.add(prefix + "." + node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    rebound.update(
                        prefix + "." + value.id
                        for value in ast.walk(target)
                        if isinstance(value, ast.Name)
                    )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                rebound.update(
                    prefix + "." + (alias.asname or alias.name.split(".")[0])
                    for alias in node.names
                )
            elif isinstance(
                node, (ast.If, ast.Try, ast.For, ast.While, ast.With, ast.Match)
            ):
                for possible in ast.walk(node):
                    if isinstance(
                        possible, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        rebound.add(prefix + "." + possible.name)
                    elif isinstance(possible, ast.Name) and isinstance(
                        possible.ctx, ast.Store
                    ):
                        rebound.add(prefix + "." + possible.id)
                    elif isinstance(possible, (ast.Import, ast.ImportFrom)):
                        rebound.update(
                            prefix + "." + (alias.asname or alias.name.split(".")[0])
                            for alias in possible.names
                        )

    collect(tree.body, module)
    unique = {name: values[0] for name, values in classes.items() if len(values) == 1}
    parents = defaultdict(set)
    methods = {}
    shadowed = any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "NotImplementedError"
        or isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        and node.id == "NotImplementedError"
        or isinstance(node, ast.arg)
        and node.arg == "NotImplementedError"
        or isinstance(node, (ast.Import, ast.ImportFrom))
        and any(
            (alias.asname or alias.name.split(".")[0]) == "NotImplementedError"
            for alias in node.names
        )
        for node in ast.walk(tree)
    )
    for name, cls in unique.items():
        prefix = name.rsplit(".", 1)[0]
        methods[name] = {
            method.name: method
            for method in cls.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for base in cls.bases:
            expression = base.value if isinstance(base, ast.Subscript) else base
            if isinstance(expression, ast.Name):
                target = prefix + "." + expression.id
                if (
                    target in unique
                    and target not in rebound
                    and unique[target].lineno < cls.lineno
                ):
                    parents[name].add(target)
    declarations = set()
    for name, overrides in methods.items():
        seen, pending = set(), list(parents[name])
        while pending:
            parent = pending.pop()
            if parent in seen:
                continue
            seen.add(parent)
            pending.extend(parents[parent] - seen)
            for method, override in overrides.items():
                base = methods[parent].get(method)
                if (
                    base is None
                    or shadowed
                    or stub_body(override) is not None
                    or any(
                        ast.unparse(d).split(".")[-1] == "abstractmethod"
                        for d in override.decorator_list
                    )
                ):
                    continue
                if stub_body(base) == "not_implemented":
                    declarations.add(parent + "." + method)
    return declarations


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
            if origin == "typeshed" and module == "typing":
                for relative in (
                    "stdlib/collections/abc.pyi",
                    "stdlib/_collections_abc.pyi",
                ):
                    proof = allowed / relative
                    if safe_file(proof, allowed):
                        try:
                            proof_bytes = read_allowed(proof, allowed)
                            text = canonical(proof_bytes)
                        except OSError:
                            continue
                        proof_row = {
                            "origin": origin,
                            "relative": relative,
                            "module": module_name(relative, origin),
                            "text": text,
                            "raw_digest": sha(proof_bytes),
                            "text_digest": sha(text.encode()),
                        }
                        previous = self.files.get(proof.as_uri())
                        if previous is not None and previous != proof_row:
                            raise ValueError(
                                "input_changed_during_run: external definition changed"
                            )
                        self.files[proof.as_uri()] = proof_row
            return


def definition(row, item, unit):
    """Prove module and qualified name from syntax; never infer a runtime alias."""
    if row.get("module") != module_name(row["relative"], row["origin"]) or row.get(
        "text_digest"
    ) != sha(row["text"].encode()):
        raise ValueError("external definition capture mismatch")
    selection = native_span(row["text"], item["selectionRange"], unit)
    tree = ast.parse(row["text"])
    from .structure_facts import stub_body, typing_only_nodes

    typing_only = typing_only_nodes(tree)
    choices = []
    names = []
    groups = {}

    def visit(body, parents=(), declaration=False):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = ".".join((row["module"], *parents, node.name))
                names.append(qualified)
                groups.setdefault(qualified, []).append((node, body))
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
    if len(choices) != 1:
        return None
    node, span, qualname, decorators, abstract = choices[0]
    value = {
        "id": "extsym:" + qualname,
        "path": row["origin"] + "/" + row["relative"],
        "span": span,
        "name_span": selection,
        "name": node.name,
        "qualname": qualname,
        "symbol_kind": "class" if isinstance(node, ast.ClassDef) else "function",
        "owner": "extsym:" + qualname.rsplit(".", 1)[0],
        "decorators": decorators,
        "declaration_only": row["relative"].endswith(".pyi")
        or abstract
        or id(node) in typing_only
        or qualname in overridden_stubs(tree, row["module"]),
    }
    if not value["declaration_only"] and stub_body(node) == "not_implemented":
        # A placeholder body is not evidence of the actual external dispatch.
        # Keep it unknown unless the established override rule proves a
        # declaration; do not guess which unseen subclass supplies the body.
        return None
    if names.count(qualname) != 1:
        group = groups[qualname]
        body = group[0][1]
        positions = [body.index(n) for n, scope in group if scope is body]
        if not (
            value["declaration_only"]
            and len(positions) == len(group)
            and positions == list(range(positions[0], positions[0] + len(group)))
            and all(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(
                    ast.unparse(d).split(".")[-1] == "overload"
                    for d in n.decorator_list
                )
                for n, _ in group
            )
        ):
            return None
    return value


def proved_reexport(files, module, name, source, seen=()):
    """Follow only captured, unambiguous imports and literal wildcard exports."""
    if module == source:
        return True
    if module in seen:
        return False
    rows = [
        row
        for row in files.values()
        if row["module"] == module and row["origin"] == "typeshed"
    ]
    if len(rows) != 1:
        return False
    row = rows[0]
    if row["module"] != module_name(row["relative"], row["origin"]) or row[
        "text_digest"
    ] != sha(row["text"].encode()):
        return False
    tree = ast.parse(row["text"])
    imports = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        for alias in node.names:
            if alias.name == name and alias.asname == name:
                imports.append(node.module)
            elif alias.name == "*":
                sources = [r for r in files.values() if r["module"] == node.module]
                if len(sources) != 1:
                    continue
                for export in ast.parse(sources[0]["text"]).body:
                    if isinstance(export, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "__all__"
                        for t in export.targets
                    ):
                        try:
                            exported = ast.literal_eval(export.value)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(exported, (list, tuple)) and name in exported:
                            imports.append(node.module)
    # A same-name assignment, definition, or conditional import invalidates proof.
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return False
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id == name
        ):
            return False
        if (
            isinstance(node, (ast.Import, ast.ImportFrom))
            and node not in tree.body
            and any((a.asname or a.name) == name for a in node.names)
        ):
            return False
    return len(imports) == 1 and proved_reexport(
        files, imports[0], name, source, (*seen, module)
    )


class Registry:
    def __init__(self, files, checksum, public_symbols=()):
        if digest(files) != checksum:
            raise ValueError("external capture checksum mismatch")
        self.files = files
        self.checksum = checksum
        self.definitions = {}
        self.proofs = {}
        self.parsed = {}
        self.public_symbols = set(public_symbols)
        self.identities = {}

    def canonical(self, value, row):
        if (
            value is None
            or row["origin"] != "typeshed"
            or not value["declaration_only"]
        ):
            return value
        if value["id"] in self.identities:
            identity = self.identities[value["id"]]
            return (
                {
                    **value,
                    "id": identity,
                    "qualname": identity[len("extsym:") :],
                    "owner": identity.rsplit(".", 1)[0],
                }
                if identity is not None
                else None
            )
        suffix = value["qualname"][len(row["module"]) + 1 :]
        possible = [
            identity
            for identity in self.public_symbols
            if identity != value["id"] and identity.endswith("." + suffix)
        ]
        aliases = [
            identity
            for identity in possible
            if proved_reexport(
                self.files,
                identity[len("extsym:") : -len(suffix) - 1],
                suffix.split(".")[0],
                row["module"],
            )
        ]
        if len(aliases) != 1:
            identity = (
                None
                if possible and value["id"] not in self.public_symbols
                else value["id"]
            )
        else:
            identity = aliases[0]
        self.identities[value["id"]] = identity
        if identity is None:
            return None
        return {
            **value,
            "id": identity,
            "qualname": identity[len("extsym:") :],
            "owner": identity.rsplit(".", 1)[0],
        }

    def lookup(self, item, unit, records):
        row = self.files.get(item.get("uri"))
        if row is None:
            return None
        try:
            key = (item["uri"], item["name"], digest(item["selectionRange"]), unit)
            if key not in self.parsed:
                self.parsed[key] = self.canonical(definition(row, item, unit), row)
            value = self.parsed[key]
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
            if native_identity(old) and native_identity(target):
                # Keep the declaration proof; the journal preserves every raw
                # overload range, which all name this one existing native symbol.
                return old
            if (
                old.declaration_only
                and target.declaration_only
                and any(d.split(".")[-1] == "overload" for d in old.decorators)
                and any(d.split(".")[-1] == "overload" for d in target.decorators)
            ):
                return old
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
            row = self.files[item["uri"]]
            value = self.canonical(definition(row, item, unit), row)
            if (
                value is None
                or view.defs.get(identity) != target
                or any(getattr(target, key) != field for key, field in value.items())
                or target.envelope.snapshot != run.snapshot.id
                or target.envelope.producer != run.producer.id
                or target.envelope.generation != run.id
            ):
                return False
        mapped = {row.raw_id: row for row in run.mapping if row.stage == "mapped"}
        for answer in run.answers:
            for candidate in answer.candidates:
                if candidate.target not in self.definitions:
                    continue
                target = self.definitions[candidate.target]
                native = native_identity(target, run.snapshot.target_python)
                if target.declaration_only and candidate.semantics != (
                    "implementation" if native else "declaration"
                ):
                    return False
                if (
                    native
                    and "native_runtime_identity:" + target.id not in candidate.evidence
                ):
                    return False
                raw = mapped.get(candidate.evidence[0]) if candidate.evidence else None
                if (
                    raw is None
                    or raw.subject != answer.subject
                    or raw.target != target.id
                ):
                    return False
        return True
