"""Bounded syntax proofs of implementation identity in the frozen search view."""

import ast
from pathlib import Path

from .external_targets import read_allowed, safe_file, target_condition
from .snapshot import canonical, sha


class Unproved(ValueError):
    pass


class ImplementationProof:
    """Follow imports and C3 member lookup; never import or execute project code.

    Only captured workspace paths and the build's private dependency copy are
    searched. Declarations from an ambient interpreter are never implementation
    evidence. Each proof has a depth and work bound, including wildcard and MRO
    traversal. Cache entries describe this immutable input, not live directories.
    """

    def __init__(self, root, snapshot, public_names=None):
        self.root = Path(root)
        self.snapshot = snapshot
        self.public_names = public_names or {}
        self.sources = {s.path: s for s in snapshot.files}
        self.workspace = {}
        for path, module in snapshot.module_names:
            self.workspace.setdefault(module, []).append(path)
        environment = self.root.parent / "environment"
        self.packages = sorted(environment.glob("lib/python*/site-packages"))
        if (environment / "Lib/site-packages").is_dir():
            self.packages.append(environment / "Lib/site-packages")
        self.modules = {}
        self.results = {}
        self.directory_names = {}
        self.code_trees = {}

    def exact_file(self, path, root):
        if not safe_file(path, root):
            return False
        current = root
        for part in path.relative_to(root).parts:
            if current not in self.directory_names:
                self.directory_names[current] = {p.name for p in current.iterdir()}
            if part not in self.directory_names[current]:
                return False
            current /= part
        return True

    def same(self, analyzer, positive):
        left, left_reason = self.identity(analyzer)
        right, right_reason = self.identity(positive)
        if left is not None and left == right:
            return True, "same_implementation"
        if left is None:
            return False, f"analyzer {analyzer}: {left_reason}"
        if right is None:
            return False, f"pyright {positive}: {right_reason}"
        return False, f"different_implementation: {left[1]} != {right[1]}"

    def identity(self, public):
        if public not in self.results:
            name = self.public_names.get(public)
            if name is None and public.startswith("extsym:"):
                name = public[len("extsym:") :]
            # Existing workspace function/method/class IDs carry a qualname.
            if name is None and public.startswith(("fn:", "method:", "class:")):
                name = public.split(":", 1)[1]
            self.remaining = 256
            try:
                if name is None:
                    raise Unproved("public_symbol_unmapped")
                value = self.resolve(name, ())
                if value[0] not in ("function", "class"):
                    raise Unproved("target_is_not_a_definition")
                node = value[2]
                self.results[public] = (
                    (
                        *value[:2],
                        (
                            (
                                node.lineno,
                                node.col_offset,
                                node.end_lineno,
                                node.end_col_offset,
                            )
                            if node is not None
                            else None
                        ),
                    ),
                    None,
                )
            except (Unproved, OSError, ValueError, SyntaxError, UnicodeError) as exc:
                self.results[public] = (None, str(exc))
        return self.results[public]

    def step(self, key, seen):
        self.remaining -= 1
        if self.remaining < 0 or len(seen) >= 64:
            raise Unproved("proof_limit")
        if key in seen:
            raise Unproved("import_cycle: " + key)
        return (*seen, key)

    def module(self, name):
        if name in self.modules:
            return self.modules[name]
        paths = self.workspace.get(name, [])
        raw, relative = None, None
        if paths:
            if len(paths) != 1:
                raise Unproved("ambiguous_workspace_module: " + name)
            relative = paths[0]
            path = self.root / relative
            if relative not in self.sources or not safe_file(path, self.root):
                raise Unproved("unsafe_workspace_module: " + name)
            raw = read_allowed(path, self.root)
            if sha(raw) != self.sources[relative].raw_digest:
                raise Unproved("frozen_workspace_changed: " + name)
        else:
            choices = []
            for packages in self.packages:
                if packages.is_symlink() or packages.resolve() != packages:
                    continue
                for suffix in ("/__init__.py", ".py"):
                    path = packages / (name.replace(".", "/") + suffix)
                    # Python module names are case sensitive even on a host
                    # filesystem that accepts Session.py for session.py.
                    if self.exact_file(path, packages):
                        choices.append((path, packages))
            if len(choices) > 1:
                raise Unproved("ambiguous_dependency_module: " + name)
            if choices:
                path, packages = choices[0]
                relative = path.relative_to(packages).as_posix()
                raw = read_allowed(path, packages)
        if raw is None:
            self.modules[name] = None
            return None
        tree = ast.parse(canonical(raw))
        value = (tree, relative.endswith("/__init__.py") or relative == "__init__.py")
        self.modules[name] = value
        return value

    def resolve(self, name, seen, members=()):
        seen = self.step(name, seen)
        parts = name.split(".")
        if not all(p.isidentifier() for p in parts):
            raise Unproved("non_static_qualname: " + name)
        exact = self.module(name)
        if exact is not None and len(parts) > 1:
            parent = ".".join(parts[:-1])
            source = self.module(parent)
            if source is not None:
                binding = self.binding(
                    parent, source[0].body, parts[-1], seen, members=members
                )
                if binding is not None and binding not in (
                    ("alias", name),
                    ("module_alias", name),
                ):
                    # Package attributes may be functions re-exported from a
                    # same-named submodule (tests.utils.jsonify, for example).
                    return self.bound(
                        ("module", parent, source[0]), parts[-1], binding, seen, members
                    )
                if (
                    binding is None
                    and self.binding(parent, source[0].body, "__getattr__", seen)
                    is not None
                ):
                    raise Unproved("module_getattr: " + parent)
        for length in range(len(parts), 0, -1):
            module = ".".join(parts[:length])
            source = self.module(module)
            if source is None:
                continue
            value = ("module", module, source[0])
            suffix = (*parts[length:], *members)
            for index, part in enumerate(parts[length:]):
                value = self.member(value, part, seen, suffix[index + 1 :])
            return value
        if name == "builtins.object":
            return ("class", name, None)
        raise Unproved("frozen_module_unavailable: " + name)

    def import_from(self, module, node):
        if not node.level:
            return node.module or ""
        source = self.module(module)
        package = module.split(".") if source[1] else module.split(".")[:-1]
        if node.level > len(package):
            raise Unproved("relative_import_escapes_package")
        return ".".join(
            (*package[: len(package) - node.level + 1], *(node.module or "").split("."))
        ).rstrip(".")

    def binding(self, module, body, name, seen, before=None, members=()):
        """Evaluate writes in order, rejecting unknown suites and runtime aliases."""
        binding = None
        sys_bound = False
        definitions = 0
        namespace_aliases = set()
        global_aliases = set()
        module_scope = not any(
            isinstance(node, ast.ClassDef) and node.body is body
            for node in ast.walk(self.module(module)[0])
        )

        def namespace(value, global_only=False):
            if isinstance(value, ast.Name):
                return value.id in (
                    global_aliases if global_only else namespace_aliases
                )
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and not value.args
            ):
                return (
                    value.func.id == "globals"
                    and module_scope
                    or value.func.id in ("locals", "vars")
                    and not global_only
                )
            return False

        # Locals in a nested function/class belong to that nested namespace.
        # globals() still denotes the module namespace in any nested suite.
        assignments = []
        pending = [(node, False) for node in body]
        while pending:
            node, nested = pending.pop()
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                assignments.append((node, nested))
            nested = nested or isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            pending.extend((child, nested) for child in ast.iter_child_nodes(node))
        for _ in range(64):
            previous = (namespace_aliases.copy(), global_aliases.copy())
            for statement, nested in assignments:
                if statement.value is not None and namespace(statement.value, nested):
                    targets = (
                        statement.targets
                        if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    names = {t.id for t in targets if isinstance(t, ast.Name)}
                    namespace_aliases.update(names)
                    if module_scope and (
                        not nested or namespace(statement.value, True)
                    ):
                        global_aliases.update(names)
            if previous == (namespace_aliases, global_aliases):
                break
        else:
            raise Unproved("proof_limit")

        def key_writes(key, selected):
            return not isinstance(key, ast.Constant) or key.value == selected

        def namespace_write(node, selected, global_only=False):
            if isinstance(node, ast.Subscript) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                return namespace(node.value, global_only) and key_writes(
                    node.slice, selected
                )
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                return False
            if not namespace(node.func.value, global_only):
                return False
            method = node.func.attr
            if method in ("__setitem__", "__delitem__", "pop", "setdefault"):
                return not node.args or key_writes(node.args[0], selected)
            if method in ("clear", "popitem"):
                return True
            if method == "update":
                if any(k.arg is None or k.arg == selected for k in node.keywords):
                    return True
                return any(
                    not isinstance(arg, ast.Dict)
                    or any(k is None or key_writes(k, selected) for k in arg.keys)
                    for arg in node.args
                )
            return False

        def code_aliases(code, global_only=False):
            for _ in range(64):
                previous = (namespace_aliases.copy(), global_aliases.copy())
                for v in ast.walk(code):
                    if isinstance(v, ast.Assign) and namespace(v.value, global_only):
                        names = {t.id for t in v.targets if isinstance(t, ast.Name)}
                        namespace_aliases.update(names)
                        if module_scope and (
                            not global_only or namespace(v.value, True)
                        ):
                            global_aliases.update(names)
                if previous == (namespace_aliases, global_aliases):
                    return
            raise Unproved("proof_limit")

        def unknown_writes(node, selected=name, depth=0):
            if depth > 64:
                raise Unproved("proof_limit")
            if namespace_write(node, selected):
                return True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return node.name == selected
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return any(
                    a.name == "*" or (a.asname or a.name.split(".")[0]) == selected
                    for a in node.names
                )
            if isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                return node.id == selected
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                return selected in node.names
            if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == selected:
                return True
            if isinstance(node, ast.MatchMapping) and node.rest == selected:
                return True
            if isinstance(node, ast.Call):
                if selected == "__all__" and any(
                    isinstance(v, ast.Name) and v.id == "__all__"
                    for v in ast.walk(node)
                ):
                    return True
                if (
                    selected == "sys"
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ("setattr", "delattr")
                    and node.args
                    and any(
                        isinstance(v, ast.Name) and v.id == "sys"
                        for v in ast.walk(node.args[0])
                    )
                ):
                    return True
                if isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval"):
                    code = self.literal_code(node)
                    if code is not None:
                        code_aliases(code)
                    return code is None or unknown_writes(code, selected, depth + 1)
            if isinstance(node, ast.Subscript) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                return selected == "__all__" and any(
                    isinstance(v, ast.Name) and v.id == selected
                    for v in ast.walk(node.value)
                )
            if isinstance(node, ast.Attribute) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                return selected == "sys" and any(
                    isinstance(v, ast.Name) and v.id == selected
                    for v in ast.walk(node.value)
                )
            if (
                selected == "__all__"
                and isinstance(node, (ast.Assign, ast.AnnAssign))
                and node.value is not None
                and any(
                    isinstance(v, ast.Name) and v.id == selected
                    for v in ast.walk(node.value)
                )
            ):
                return True
            return any(
                unknown_writes(child, selected, depth)
                for child in ast.iter_child_nodes(node)
            )

        def visit(statements):
            nonlocal binding, sys_bound, definitions
            for node in statements:
                if before is not None and node.lineno >= before:
                    continue
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        local = alias.asname or alias.name.split(".")[0]
                        if local == "sys":
                            sys_bound = alias.name == "sys"
                        if local == name:
                            binding = (
                                "module_alias",
                                alias.name if alias.asname else local,
                            )
                elif isinstance(node, ast.ImportFrom):
                    origin = self.import_from(module, node)
                    for alias in node.names:
                        if (alias.asname or alias.name) in ("sys", "*"):
                            sys_bound = False
                        if (alias.asname or alias.name) == name:
                            binding = ("alias", origin + "." + alias.name)
                        elif alias.name == "*":
                            try:
                                exported = self.exported(origin, name, seen)
                            except Unproved as exc:
                                binding = ("unknown", str(exc))
                            else:
                                if exported:
                                    binding = ("alias", origin + "." + name)
                elif isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    if node.name == "sys":
                        sys_bound = False
                    if node.name == name:
                        # Overloads can precede their one concrete implementation.
                        if any(
                            ast.unparse(d).split(".")[-1] == "overload"
                            for d in node.decorator_list
                        ):
                            binding = ("unknown", "overload_without_implementation")
                        else:
                            definitions += 1
                            binding = (
                                ("definition", node)
                                if definitions == 1
                                else (
                                    "unknown",
                                    "multiple_definitions: " + module + "." + name,
                                )
                            )
                elif isinstance(node, ast.If):
                    active = (
                        target_condition(node.test, self.snapshot)
                        if sys_bound
                        else None
                    )
                    if active is None:
                        if unknown_writes(node, "sys"):
                            sys_bound = False
                        if unknown_writes(node):
                            binding = (
                                "unknown",
                                "conditional_binding: " + module + "." + name,
                            )
                    else:
                        visit(node.body if active else node.orelse)
                else:
                    if unknown_writes(node):
                        binding = (
                            "unknown",
                            "assignment_binding: " + module + "." + name,
                        )
                    if unknown_writes(node, "sys"):
                        sys_bound = False

        visit(body)

        def nested_write(node, depth=0):
            if depth > 64:
                raise Unproved("proof_limit")
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                return module_scope and name in node.names
            if namespace_write(node, name, True):
                return True
            if name == "__all__" and isinstance(node, ast.Name) and node.id == name:
                return True
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("exec", "eval")
            ):
                code = self.literal_code(node)
                if code is None:
                    return True
                code_aliases(code, True)
                if any(namespace(arg, True) for arg in node.args[1:]):
                    return unknown_writes(code)
                # Constant default exec uses the nested local namespace. Only
                # explicit globals/nonlocals or namespace writes affect ours.
                return any(nested_write(v, depth + 1) for v in ast.walk(code))
            return False

        for statement in body:
            if isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                if any(nested_write(v) for v in ast.walk(statement)):
                    return ("unknown", "nested_namespace_write: " + module + "." + name)
        if members:
            line = self.member_write(self.module(module)[0], members)
            if line is not None and binding is not None and binding[0] != "unknown":
                direct = any(
                    getattr(v, "lineno", None) == line
                    and (
                        isinstance(v, ast.Attribute)
                        and isinstance(v.ctx, (ast.Store, ast.Del))
                        and isinstance(v.value, ast.Name)
                        and v.value.id == name
                        or isinstance(v, ast.Call)
                        and isinstance(v.func, ast.Name)
                        and v.func.id in ("setattr", "delattr")
                        and v.args
                        and isinstance(v.args[0], ast.Name)
                        and v.args[0].id == name
                    )
                    for v in ast.walk(self.module(module)[0])
                )
                if direct:
                    return ("unknown", "assignment_binding: " + module + "." + name)
                return (
                    "unknown",
                    "unproved_hidden_write: "
                    + module
                    + "."
                    + name
                    + " at line "
                    + str(line),
                )
        return binding

    def literal_code(self, node):
        if (
            not node.args
            or not isinstance(node.args[0], ast.Constant)
            or not isinstance(node.args[0].value, str)
        ):
            return None
        text = node.args[0].value
        mode = "exec" if node.func.id == "exec" else "eval"
        key = (text, mode)
        if key not in self.code_trees:
            self.remaining -= 1
            if self.remaining < 0:
                raise Unproved("proof_limit")
            self.code_trees[key] = ast.parse(text, mode=mode)
        return self.code_trees[key]

    def member_write(self, tree, members, depth=0):
        """Any receiver may alias the class; only selected attributes matter."""
        if depth > 64:
            raise Unproved("proof_limit")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and node.attr in members
            ):
                return node.lineno
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("setattr", "delattr") and len(node.args) >= 2:
                    attr = node.args[1]
                    if not isinstance(attr, ast.Constant) or attr.value in members:
                        return node.lineno
                if node.func.id in ("exec", "eval"):
                    code = self.literal_code(node)
                    if (
                        code is None
                        or self.member_write(code, members, depth + 1) is not None
                    ):
                        return node.lineno
            if isinstance(node, ast.Subscript) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                value, key = node.value, node.slice
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "__dict__"
                    or isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "vars"
                    and value.args
                ) and (not isinstance(key, ast.Constant) or key.value in members):
                    return node.lineno
        return None

    def exported(self, module, name, seen):
        seen = self.step("star:" + module + "." + name, seen)
        source = self.module(module)
        if source is None:
            raise Unproved("star_module_unavailable: " + module)
        export = self.binding(module, source[0].body, "__all__", seen)
        if export is not None:
            # __all__ is export metadata, not an implementation alias assignment.
            assignments = [
                n
                for n in source[0].body
                if isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets
                )
            ]
            if len(assignments) != 1:
                raise Unproved("dynamic_all: " + module)
            try:
                names = ast.literal_eval(assignments[0].value)
            except (ValueError, TypeError):
                raise Unproved("dynamic_all: " + module) from None
            if not isinstance(names, (list, tuple)) or not all(
                isinstance(n, str) for n in names
            ):
                raise Unproved("dynamic_all: " + module)
            # Any additional write/augmentation makes the literal list uncertain.
            remaining = [n for n in source[0].body if n is not assignments[0]]
            if self.binding(module, remaining, "__all__", seen) is not None:
                raise Unproved("dynamic_all: " + module)
            return name in names
        if name.startswith("_"):
            return False
        return self.binding(module, source[0].body, name, seen) is not None

    def member(self, value, name, seen, members=()):
        kind, qualified, node = value
        module = qualified if kind == "module" else self.defining_module(qualified)
        if kind == "module":
            binding = self.binding(module, node.body, name, seen, members=members)
            # A module getter only supplies missing attributes. Explicit imports
            # (e.g. httpx2.Client) do not invoke an unrelated deprecated getter.
            if (
                binding is None
                and self.binding(module, node.body, "__getattr__", seen) is not None
            ):
                raise Unproved("module_getattr: " + module)
        elif kind == "class":
            binding = (
                None
                if node is None
                else self.binding(
                    module, node.body, name, seen, members=(name, *members)
                )
            )
            if binding is None:
                for base in self.mro(value, seen, (name, *members))[1:]:
                    if base[2] is None:
                        continue
                    base_module = self.defining_module(base[1])
                    found = self.binding(
                        base_module, base[2].body, name, seen, members=(name, *members)
                    )
                    if found is not None:
                        return self.bound(base, name, found, seen, members)
        else:
            raise Unproved("non_class_member: " + qualified + "." + name)
        if binding is None:
            raise Unproved("binding_not_found: " + qualified + "." + name)
        return self.bound(value, name, binding, seen, members)

    def defining_module(self, qualified):
        parts = qualified.split(".")
        for length in range(len(parts) - 1, 0, -1):
            name = ".".join(parts[:length])
            if self.module(name) is not None:
                return name
        raise Unproved("defining_module_unavailable: " + qualified)

    def bound(self, owner, name, binding, seen, members=()):
        kind, target = binding
        if kind == "unknown":
            raise Unproved(target)
        if kind == "module_alias":
            if "." in target:
                # IMPORT_FROM for a dotted "import ... as" reads the package
                # attribute. It can be a same-named re-exported function.
                return self.resolve(target, seen, members)
            source = self.module(target)
            if source is None:
                raise Unproved("frozen_module_unavailable: " + target)
            return ("module", target, source[0])
        if kind == "alias":
            return self.resolve(target, seen, members)
        return (
            "class" if isinstance(target, ast.ClassDef) else "function",
            owner[1] + "." + name,
            target,
        )

    def mro(self, value, seen, members=()):
        seen = self.step("mro:" + value[1], seen)
        node = value[2]
        if node is None:
            return [value]
        if node.keywords or node.decorator_list:
            raise Unproved("dynamic_class: " + value[1])
        module = self.defining_module(value[1])
        parents = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                if base.id == "object":
                    binding = self.binding(
                        module,
                        self.module(module)[0].body,
                        "object",
                        seen,
                        node.lineno,
                        members=members,
                    )
                    if binding is None:
                        parents.append(("class", "builtins.object", None))
                        continue
                binding = self.binding(
                    module,
                    self.module(module)[0].body,
                    base.id,
                    seen,
                    node.lineno,
                    members=members,
                )
                if binding is None:
                    raise Unproved("base_binding_not_found: " + module + "." + base.id)
                parent = self.bound(
                    ("module", module, None), base.id, binding, seen, members
                )
            elif isinstance(base, ast.Attribute):
                parts = ast.unparse(base).split(".")
                if not all(p.isidentifier() for p in parts):
                    raise Unproved("dynamic_base: " + value[1])
                binding = self.binding(
                    module,
                    self.module(module)[0].body,
                    parts[0],
                    seen,
                    node.lineno,
                    members=members,
                )
                if binding is None:
                    raise Unproved("base_binding_not_found: " + parts[0])
                parent = self.bound(
                    ("module", module, None), parts[0], binding, seen, members
                )
                for part in parts[1:]:
                    parent = self.member(parent, part, seen, members)
            else:
                raise Unproved("dynamic_base: " + value[1])
            if parent[0] != "class":
                raise Unproved("base_is_not_class: " + parent[1])
            parents.append(parent)
        if not parents:
            parents = [("class", "builtins.object", None)]
        if len({p[1] for p in parents}) != len(parents):
            raise Unproved("duplicate_base: " + value[1])
        sequences = [self.mro(parent, seen, members) for parent in parents] + [
            parents[:]
        ]
        result = [value]
        while any(sequences):
            self.remaining -= 1
            if self.remaining < 0:
                raise Unproved("proof_limit")
            head = next(
                (
                    s[0]
                    for s in sequences
                    if s
                    and not any(s[0][1] in [v[1] for v in t[1:]] for t in sequences)
                ),
                None,
            )
            if head is None:
                raise Unproved("inconsistent_mro: " + value[1])
            result.append(head)
            for sequence in sequences:
                if sequence and sequence[0][1] == head[1]:
                    sequence.pop(0)
        return result
