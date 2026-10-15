"""Capture external definitions while the verified LSP session is still alive."""

import ast
from collections import OrderedDict
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
                if stub_body(base) in ("empty", "not_implemented"):
                    declarations.add(parent + "." + method)
    return declarations


def _captured_uri(uri, origin, partition):
    # One physical run copy retains independent declaration/reexport partitions;
    # the LSP transcript still contains the unmodified wire URI.
    if origin == "typeshed" and partition is not None:
        return uri + f"#session-{partition}"
    return uri


class Capture:
    def __init__(self, root, config):
        self.root = root.resolve()
        self.config = config
        self.files = {}

    def item(self, item, detail, partition=None):
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

            item["uri"] = _captured_uri(item["uri"], origin, partition)
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
                        uri = _captured_uri(proof.as_uri(), origin, partition)
                        previous = self.files.get(uri)
                        if previous is not None and previous != proof_row:
                            raise ValueError(
                                "input_changed_during_run: external definition changed"
                            )
                        self.files[uri] = proof_row
            return


def target_condition(
    node, snapshot, *, condition_name=None, sys_bound=True, evaluated=None
):
    """Evaluate static booleans and target syntax, with optional frozen aliases."""
    if snapshot is None:
        return None
    if evaluated is not None and not (
        isinstance(node, ast.BoolOp)
        or isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
    ):
        evaluated.append(node)

    def attribute(value, name):
        return (
            sys_bound
            and isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "sys"
            and value.attr == name
        )

    def integer(value):
        return isinstance(value, ast.Constant) and type(value.value) is int

    version = tuple(int(p) for p in snapshot.target_python.split("."))

    def version_value(value):
        if attribute(value, "version_info"):
            return version
        if isinstance(value, ast.Subscript) and attribute(value.value, "version_info"):
            index = value.slice
            if integer(index) and 0 <= index.value < len(version):
                return version[index.value]
            if isinstance(index, ast.Slice) and index.step is None:
                lower = (
                    0
                    if index.lower is None
                    else (index.lower.value if integer(index.lower) else None)
                )
                upper = index.upper.value if integer(index.upper) else None
                if (
                    lower is not None
                    and upper is not None
                    and 0 <= lower < upper <= len(version)
                ):
                    return version[lower:upper]
        return None

    def literal(value):
        if integer(value):
            return value.value
        if (
            isinstance(value, ast.Tuple)
            and value.elts
            and all(integer(v) for v in value.elts)
        ):
            return tuple(v.value for v in value.elts)
        return None

    def evaluate(value):
        return target_condition(
            value,
            snapshot,
            condition_name=condition_name,
            sys_bound=sys_bound,
            evaluated=evaluated,
        )

    if isinstance(node, ast.Constant) and type(node.value) is bool:
        return node.value
    if isinstance(node, (ast.Name, ast.Attribute)) and condition_name is not None:
        return condition_name(node)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = evaluate(node.operand)
        return None if value is None else not value
    if isinstance(node, ast.BoolOp):
        conjunction = isinstance(node.op, ast.And)
        unknown = False
        for operand in node.values:
            value = evaluate(operand)
            if value is None:
                unknown = True
            elif value != conjunction:
                return value
        return None if unknown else conjunction
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and attribute(node.func.value, "platform")
        and node.func.attr == "startswith"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return snapshot.platform.startswith(node.args[0].value)
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    left, right = node.left, node.comparators[0]
    op = node.ops[0]
    if attribute(right, "platform"):
        left, right = right, left
    if attribute(left, "platform"):
        if not isinstance(right, ast.Constant) or not isinstance(right.value, str):
            return None
        if isinstance(op, ast.Eq):
            return snapshot.platform == right.value
        if isinstance(op, ast.NotEq):
            return snapshot.platform != right.value
        return None
    a, b = version_value(left), literal(right)
    if a is None:
        a, b = literal(left), version_value(right)
    if a is None or b is None or type(a) is not type(b):
        return None
    if attribute(left, "version_info") or attribute(right, "version_info"):
        # Pyright's pythonVersion omits micro/release details: compare only
        # components known on both sides, with a one-item tuple's minor = 0.
        a = a if len(a) > 1 else (*a, 0)
        b = b if len(b) > 1 else (*b, 0)
        length = min(len(a), len(b))
        a, b = a[:length], b[:length]
    if isinstance(op, ast.Eq):
        return a == b
    if isinstance(op, ast.NotEq):
        return a != b
    if isinstance(op, ast.Lt):
        return a < b
    if isinstance(op, ast.LtE):
        return a <= b
    if isinstance(op, ast.Gt):
        return a > b
    if isinstance(op, ast.GtE):
        return a >= b
    return None


def definition_index(row, snapshot=None):
    """Read-only syntax facts, bound to one captured text and target context."""
    tree = ast.parse(row["text"])
    from .structure_facts import typing_only_nodes

    typing_only = typing_only_nodes(tree)
    groups = {}
    class_bodies = set()
    class_scopes = {}
    class_writes = {}

    def bound_names(target):
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, (ast.Tuple, ast.List)):
            for value in target.elts:
                yield from bound_names(value)
        elif isinstance(target, ast.Starred):
            yield from bound_names(target.value)

    def record_write(class_scope, name, line):
        class_writes.setdefault(class_scope, []).append((name, line))

    def expression_writes(node, class_scope):
        # Statement bodies have their own visit so inactive branches and nested
        # scopes cannot leak writes. Lambda defaults run here; its body does not.
        if isinstance(node, getattr(ast, "TypeAlias", ())):
            # Alias values and type parameters are evaluated in a lazy scope.
            return
        if isinstance(node, ast.NamedExpr):
            for name in bound_names(node.target):
                record_write(class_scope, name, node.lineno)
        children = (
            [node.args] if isinstance(node, ast.Lambda) else ast.iter_child_nodes(node)
        )
        for child in children:
            if not isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                expression_writes(child, class_scope)

    def visit(body, parents=(), declaration=False, class_scope=None):
        for node in body:
            if class_scope is not None and id(node) not in typing_only:
                expression_writes(node, class_scope)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = ".".join((row["module"], *parents, node.name))
                span = Span(
                    start=(node.lineno - 1, node.col_offset),
                    end=(node.end_lineno - 1, node.end_col_offset),
                )
                decorators = tuple(ast.unparse(d) for d in node.decorator_list)
                abstract = declaration or any(
                    d.split(".")[-1] == "abstractmethod" for d in decorators
                )
                choice = (node, span, qualified, decorators, abstract)
                class_scopes[id(node)] = class_scope
                groups.setdefault(qualified, []).append((choice, body))
                if isinstance(node, ast.ClassDef):
                    class_bodies.add(id(node.body))
                    protocol = any(
                        ast.unparse(b).split("[")[0].split(".")[-1] == "Protocol"
                        for b in node.bases
                    )
                    visit(
                        node.body,
                        (*parents, node.name),
                        declaration or protocol,
                        id(node.body),
                    )
            elif isinstance(node, ast.If):
                active = target_condition(node.test, snapshot)
                if active is not False:
                    visit(node.body, parents, declaration, class_scope)
                if active is not True:
                    visit(node.orelse, parents, declaration, class_scope)
            elif class_scope is not None and id(node) not in typing_only:
                targets = []
                if isinstance(node, (ast.Assign, ast.Delete)):
                    targets = node.targets
                elif isinstance(
                    node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)
                ):
                    targets = [node.target]
                elif isinstance(node, getattr(ast, "TypeAlias", ())):
                    targets = [node.name]
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    targets = [item.optional_vars for item in node.items]
                for target in targets:
                    for name in bound_names(target):
                        record_write(class_scope, name, node.lineno)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        name = alias.asname or (
                            alias.name.split(".")[0]
                            if isinstance(node, ast.Import)
                            else alias.name
                        )
                        record_write(class_scope, name, node.lineno)
                # These suites share the containing class namespace. Inspect
                # every possible branch, retaining target-aware If handling.
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, ()), parents, declaration, class_scope)
                if isinstance(node, (ast.Try, ast.TryStar)):
                    for handler in node.handlers:
                        if handler.name:
                            record_write(class_scope, handler.name, handler.lineno)
                        expression_writes(handler, class_scope)
                        visit(handler.body, parents, declaration, class_scope)
                elif isinstance(node, ast.Match):
                    for case in node.cases:
                        for pattern in ast.walk(case.pattern):
                            if isinstance(pattern, (ast.MatchAs, ast.MatchStar)):
                                name = pattern.name
                            elif isinstance(pattern, ast.MatchMapping):
                                name = pattern.rest
                            else:
                                continue
                            if name:
                                record_write(class_scope, name, pattern.lineno)
                        if case.guard:
                            expression_writes(case.guard, class_scope)
                        visit(case.body, parents, declaration, class_scope)

    visit(tree.body)
    overridden = overridden_stubs(tree, row["module"])

    def select(item, selection):
        from .structure_facts import stub_body

        choices = []
        for group in groups.values():
            for choice, _ in group:
                node, span, _, _, _ = choice
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
                        choices.append(choice)
        if len(choices) != 1:
            return None
        node, span, qualname, decorators, abstract = choices[0]
        group = groups[qualname]
        if len(group) > 1:
            runtime = [
                choice
                for choice, _ in group
                if id(choice[0]) not in typing_only
                and not any(d.split(".")[-1] == "overload" for d in choice[3])
                and stub_body(choice[0]) not in ("empty", "not_implemented")
            ]
            if len(runtime) == 1:
                implementation = runtime[0][0]
                body = next(
                    scope for choice, scope in group if choice[0] is implementation
                )
                if id(body) in class_bodies and any(
                    scope is body
                    and choice[0].lineno > implementation.lineno
                    and id(choice[0]) not in typing_only
                    for choice, scope in group
                ):
                    # A later class assignment overwrites even a concrete body.
                    # Filtering placeholders must not resurrect that earlier body.
                    return None
                # A selection on a declaration or placeholder names the unique body.
                node, span, qualname, decorators, abstract = runtime[0]
                line = row["text"].splitlines()[node.lineno - 1].encode()
                column = line.index(node.name.encode(), node.col_offset)
                selection = Span(
                    start=(node.lineno - 1, column),
                    end=(node.lineno - 1, column + len(node.name.encode())),
                )
            elif runtime:
                return None
            else:
                # Pure stub overloads retain the existing same-scope proof. Branch
                # alternatives and repeated runtime bodies remain ambiguous.
                body = group[0][1]
                positions = [body.index(c[0]) for c, scope in group if scope is body]
                if not (
                    len(positions) == len(group)
                    and positions
                    == list(range(positions[0], positions[0] + len(group)))
                    and all(
                        isinstance(c[0], (ast.FunctionDef, ast.AsyncFunctionDef))
                        and any(d.split(".")[-1] == "overload" for d in c[3])
                        for c, _ in group
                    )
                ):
                    return None
        if any(
            name == node.name and line > node.lineno
            for name, line in class_writes.get(class_scopes[id(node)], ())
        ):
            # A class write invalidates this method proof; aliases are not followed.
            return None
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
            or any(d.split(".")[-1] == "overload" for d in decorators)
            or id(node) in typing_only
            or qualname in overridden,
        }
        if not value["declaration_only"] and stub_body(node) in (
            "empty",
            "not_implemented",
        ):
            # A placeholder body is not evidence of the actual external dispatch.
            # Keep it unknown unless the established override rule proves a
            # declaration; do not guess which unseen subclass supplies the body.
            return None
        return value

    return select


def definition(row, item, unit, snapshot=None, *, index=None):
    """Prove module and qualified name from syntax; never infer a runtime alias."""
    if row.get("module") != module_name(row["relative"], row["origin"]) or row.get(
        "text_digest"
    ) != sha(row["text"].encode()):
        raise ValueError("external definition capture mismatch")
    selection = native_span(row["text"], item["selectionRange"], unit)
    select = definition_index(row, snapshot) if index is None else index
    return select(item, selection)


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
    def __init__(self, files, checksum, public_symbols=(), snapshot=None):
        if digest(files) != checksum:
            raise ValueError("external capture checksum mismatch")
        self.files = files
        self.checksum = checksum
        self.definitions = {}
        self.proofs = {}
        self.parsed = {}
        self.public_symbols = set(public_symbols)
        self.identities = {}
        self.snapshot = snapshot
        self.indices = OrderedDict()

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

    def definition(self, row, item, unit, snapshot_id):
        key = (digest(row), snapshot_id)
        if key not in self.indices:
            self.indices[key] = definition_index(row, self.snapshot)
            if len(self.indices) > 2:
                self.indices.popitem(last=False)
        self.indices.move_to_end(key)
        return definition(row, item, unit, self.snapshot, index=self.indices[key])

    def lookup(self, item, unit, records):
        row = self.files.get(item.get("uri"))
        if row is None:
            return None
        try:
            key = (
                item["uri"],
                item["name"],
                digest(item["selectionRange"]),
                unit,
                records.snapshot_id if self.snapshot is not None else None,
            )
            if key not in self.parsed:
                self.parsed[key] = self.canonical(
                    self.definition(row, item, unit, records.snapshot_id), row
                )
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
            or (self.snapshot is not None and self.snapshot != run.snapshot)
        ):
            return False
        snapshot_id, producer_id = run.snapshot.id, run.producer.id
        for identity, target in self.definitions.items():
            item, unit = self.proofs[identity]
            row = self.files[item["uri"]]
            value = self.canonical(self.definition(row, item, unit, snapshot_id), row)
            if (
                value is None
                or view.defs.get(identity) != target
                or any(getattr(target, key) != field for key, field in value.items())
                or target.envelope.snapshot != snapshot_id
                or target.envelope.producer != producer_id
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
