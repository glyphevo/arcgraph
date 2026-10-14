"""Bounded syntax proofs of implementation identity in the frozen search view."""

import ast
from pathlib import Path

from .external_targets import read_allowed, safe_file, target_condition
from .snapshot import canonical, sha
from .static_writes import member_writes, suite_nodes


class Unproved(ValueError):
    pass


class ImplementationProof:
    """Follow static imports and C3 lookup; never execute project code.

    Like pyright's primary answers, identity describes static declarations,
    not runtime patches, namespace dictionaries, exec/eval or decorator effects.

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
        self.writes = {}
        self.scopes = {}
        self.checking_writes = False
        self.receivers = {}
        self.conditions = {}

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

    def condition(self, module, body, node, seen, sys_bound):
        key = (module, tuple(body), node, sys_bound)
        if key in self.conditions:
            return self.conditions[key]

        def condition_name(expression):
            # Pyright prunes only the original sentinel name or an attribute
            # of a directly imported typing module, not arbitrary re-exports.
            if isinstance(expression, ast.Name) and expression.id == "TYPE_CHECKING":
                local = expression.id
                kind = "alias"
                origins = ("typing.TYPE_CHECKING", "typing_extensions.TYPE_CHECKING")
            elif (
                isinstance(expression, ast.Attribute)
                and expression.attr == "TYPE_CHECKING"
                and isinstance(expression.value, ast.Name)
            ):
                local = expression.value.id
                kind = "module_alias"
                origins = ("typing", "typing_extensions")
            else:
                return None
            try:
                binding = self.binding(module, body, local, seen, node.lineno)
                source = self.module(module)[0].body
                if binding is None and body is not source:
                    binding = self.binding(module, source, local, seen, node.lineno)
                if binding is not None and binding[0] == kind and binding[1] in origins:
                    return True
                return None
            except Unproved:
                return None

        evaluated = []
        result = target_condition(
            node.test,
            self.snapshot,
            condition_name=condition_name,
            sys_bound=sys_bound,
            evaluated=evaluated,
        )
        self.conditions[key] = (result, tuple(evaluated))
        return self.conditions[key]

    def same_bindings(self, module, bindings, seen):
        """Every write must resolve to the same concrete declaration."""
        declaration = None
        for binding in bindings:
            if binding is None or binding[0] == "unknown":
                return False
            try:
                if binding[0] == "definition":
                    value = binding[1]
                else:
                    resolved = self.bound(("module", module, None), "", binding, seen)
                    if resolved[0] not in ("function", "class"):
                        return False
                    value = resolved[2]
                if value is None:
                    return False
                if declaration is not None and value is not declaration:
                    return False
                declaration = value
            except Unproved:
                return False
        return declaration is not None

    def binding(self, module, body, name, seen, before=None, members=()):
        """Evaluate writes in order, rejecting unknown suites and runtime aliases."""
        binding = None
        sys_bound = False
        definitions = 0
        namespace = next(
            (
                node
                for node in ast.walk(self.module(module)[0])
                if getattr(node, "body", None) is body
            ),
            None,
        )
        module_scope = namespace is None or isinstance(namespace, ast.Module)
        function_scope = isinstance(namespace, (ast.FunctionDef, ast.AsyncFunctionDef))
        writes = []

        def unknown_writes(node, selected=name):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                expressions = list(node.decorator_list)
                if isinstance(node, ast.ClassDef):
                    expressions.extend(
                        [*node.bases, *(item.value for item in node.keywords)]
                    )
                else:
                    expressions.extend([*node.args.defaults, *node.args.kw_defaults])
                return node.name == selected or any(
                    unknown_writes(value, selected)
                    for value in expressions
                    if value is not None
                )
            if isinstance(node, ast.Lambda):
                return any(
                    unknown_writes(value, selected)
                    for value in [*node.args.defaults, *node.args.kw_defaults]
                    if value is not None
                )
            if isinstance(
                node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                expressions = (
                    [node.key, node.value]
                    if isinstance(node, ast.DictComp)
                    else [node.elt]
                )
                expressions.extend(
                    value
                    for generator in node.generators
                    for value in [generator.iter, *generator.ifs]
                )
                return any(unknown_writes(value, selected) for value in expressions)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return any(
                    a.name == "*" or (a.asname or a.name.split(".")[0]) == selected
                    for a in node.names
                )
            if isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                return node.id == selected
            if isinstance(node, ast.ExceptHandler) and node.name == selected:
                return True
            if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == selected:
                return True
            if isinstance(node, ast.MatchMapping) and node.rest == selected:
                return True
            # Calls, namespace subscripts and runtime effects do not bind names
            # in this static model. Their ordinary argument expressions may.
            return any(
                unknown_writes(child, selected) for child in ast.iter_child_nodes(node)
            )

        def visit(statements):
            nonlocal binding, sys_bound, definitions
            for node in statements:
                if before is not None and node.lineno >= before:
                    continue
                overload = isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and any(
                    ast.unparse(d).split(".")[-1] == "overload"
                    for d in node.decorator_list
                )
                if (
                    not isinstance(node, (ast.If, ast.ImportFrom))
                    and not overload
                    and unknown_writes(node)
                ):
                    writes.append(None)
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
                            if writes[-1] is None:
                                writes[-1] = binding
                            else:
                                writes.append(binding)
                elif isinstance(node, ast.ImportFrom):
                    origin = self.import_from(module, node)
                    for alias in node.names:
                        if (alias.asname or alias.name) in ("sys", "*"):
                            sys_bound = False
                        if (alias.asname or alias.name) == name:
                            binding = ("alias", origin + "." + alias.name)
                            writes.append(binding)
                        elif alias.name == "*":
                            try:
                                exported = self.exported(origin, name, seen)
                            except Unproved as exc:
                                binding = ("unknown", str(exc))
                            else:
                                if exported:
                                    binding = ("alias", origin + "." + name)
                                    writes.append(binding)
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
                            writes[-1] = binding
                elif isinstance(node, ast.If):
                    active, evaluated = self.condition(
                        module, body, node, seen, sys_bound
                    )
                    if active is None:
                        if unknown_writes(node, "sys"):
                            sys_bound = False
                        if unknown_writes(node):
                            writes.append(None)
                            binding = (
                                "unknown",
                                "conditional_binding: " + module + "." + name,
                            )
                    else:
                        # Pyright's definition list retains a binding expression
                        # even in an operand skipped by boolean short-circuiting.
                        if unknown_writes(node.test):
                            writes.append(None)
                            binding = (
                                "unknown",
                                "assignment_binding: " + module + "." + name,
                            )
                        if any(unknown_writes(value, "sys") for value in evaluated):
                            sys_bound = False
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
        if (
            len(writes) > 1
            and binding is not None
            and binding[0] != "unknown"
            and not self.same_bindings(module, writes, seen)
        ):
            return ("unknown", "multiple_bindings: " + module + "." + name)

        def nested_write(node):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                return False
            nodes = list(suite_nodes(node.body))
            declared = any(
                isinstance(value, ast.Global)
                and module_scope
                and name in value.names
                or isinstance(value, ast.Nonlocal)
                and function_scope
                and name in value.names
                for value in nodes
                if value is not None
            )
            if declared and any(unknown_writes(value) for value in node.body):
                return True
            return any(nested_write(value) for value in nodes if value is not None)

        if any(nested_write(node) for node in suite_nodes(body) if node is not None):
            return ("unknown", "nested_namespace_write: " + module + "." + name)
        return binding

    def module_writes(self, module, source):
        if module not in self.writes:
            self.writes[module] = member_writes(
                source[0], module, self.import_from, self.snapshot, self.scopes
            )
        return self.writes[module]

    def static_reference(self, reference, seen=()):
        if reference in seen or len(seen) >= 64:
            return None
        if reference in ("builtins.setattr", "builtins.delattr"):
            return reference
        if self.module(reference) is not None:
            return reference
        parts = reference.split(".")
        for length in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:length])
            source = self.module(module)
            if source is None:
                continue
            self.module_writes(module, source)
            expression = ast.parse(".".join(parts[length:]), mode="eval").body
            target = self.scopes[module].reference(expression)
            if target is None or target == reference:
                return target
            return self.static_reference(target, (*seen, reference))
        return reference

    def member_write(self, owner, name):
        """Reject only writes whose receiver resolves to this class or module."""
        for module, source in tuple(self.modules.items()):
            if source is None:
                continue
            for receiver, attribute, line, callee in self.module_writes(module, source):
                if attribute != name:
                    continue
                if callee is not None and self.static_reference(callee) not in (
                    "builtins.setattr",
                    "builtins.delattr",
                ):
                    continue
                reference = self.static_reference(receiver)
                if reference is None:
                    continue
                try:
                    self.checking_writes = True
                    if reference not in self.receivers:
                        self.receivers[reference] = self.resolve(reference, ())
                    resolved = self.receivers[reference]
                except (Unproved, OSError, ValueError, SyntaxError, UnicodeError):
                    continue
                finally:
                    self.checking_writes = False
                if resolved[:2] == owner[:2]:
                    return module, line
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
        if kind in ("module", "class") and not self.checking_writes:
            write = self.member_write(value, name)
            if write is not None:
                writer, line = write
                raise Unproved(f"static_member_write: {writer}.{name} at line {line}")
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
