"""Static pytest provider lookup shared by injection and receiver type facts.

Only local/class definitions and ancestor conftest providers are selected.
Ties at the winning scope stay ambiguous; they never fall back to an ancestor.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

from arcgraph.adapters.common import (
    call_name,
    import_aliases,
    iter_symbol_defs,
    literal_str,
    literal_str_list,
    resolve_alias,
    symbol_id_for_def,
)
from arcgraph.adapters.pytest_collection import PytestCollection
from arcgraph.analyzers.calls.lexical import LexicalScopes
from arcgraph.core.ids import class_id, module_id, pytest_fixture_id
from arcgraph.core.schemas import FileRecord, Node

Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class FixtureFact:
    name: str
    node_id: str
    handler_id: str
    file: FileRecord
    stmt: Function
    owner: str
    autouse: bool
    async_enabled: bool
    stable: bool


class FixtureCatalog:
    def __init__(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        module_names: set[str],
        nodes: list[Node],
    ) -> None:
        self.collection = PytestCollection.from_files(files)
        self.fixtures: list[FixtureFact] = []
        self.uncertain_scopes: set[tuple[str, str]] = set()
        self.lexical = LexicalScopes(nodes, {})
        self.aliases = {}
        self.trees = parsed_files
        for file in files:
            tree = parsed_files.get(file.path)
            if tree is None:
                continue
            aliases = import_aliases(file, tree, module_names)
            self.aliases[file.path] = aliases
            scopes = [
                ("", tree.body),
                *((s.name, s.body) for s in tree.body if isinstance(s, ast.ClassDef)),
            ]
            for owner, body in scopes:
                for item in body:
                    if isinstance(
                        item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        continue
                    if any(
                        isinstance(child, ast.ImportFrom)
                        and any(alias.name == "*" for alias in child.names)
                        for child in ast.walk(item)
                    ):
                        self.uncertain_scopes.add((file.path, owner))
                    if any(
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and any(
                            resolve_alias(
                                call_name(d.func if isinstance(d, ast.Call) else d),
                                aliases,
                            )
                            in {"pytest.fixture", "pytest_asyncio.fixture", "fixture"}
                            for d in child.decorator_list
                        )
                        for child in ast.walk(item)
                    ):
                        self.uncertain_scopes.add((file.path, owner))
            for stmt, qualname, id_factory in iter_symbol_defs(tree):
                decorators = [
                    d
                    for d in stmt.decorator_list
                    if resolve_alias(
                        call_name(d.func if isinstance(d, ast.Call) else d), aliases
                    )
                    in {"pytest.fixture", "pytest_asyncio.fixture", "fixture"}
                ]
                if len(decorators) != 1:
                    continue
                decorator = decorators[0]
                options = (
                    {k.arg: k.value for k in decorator.keywords}
                    if isinstance(decorator, ast.Call)
                    else {}
                )
                # A dynamic name cannot be registered under the Python name.
                name = literal_str(options["name"]) if "name" in options else stmt.name
                if not name:
                    self.uncertain_scopes.add((file.path, qualname.rpartition(".")[0]))
                    continue
                owner = qualname.rpartition(".")[0]
                scope = self.lexical.nodes.get(
                    class_id(f"{file.module}.{owner}")
                    if owner
                    else module_id(file.module)
                )
                root = self.lexical.root_name(
                    call_name(
                        decorator.func if isinstance(decorator, ast.Call) else decorator
                    )
                )
                decorator_binding = (
                    self.lexical.lookup(scope, root) if scope and root else None
                )
                definition = (
                    self.lexical.bindings.get(scope.id, {}).get(stmt.name, [])
                    if scope
                    else []
                )
                stable = bool(
                    scope
                    and self.lexical.stable(scope, definition, None)
                    and decorator_binding
                    and self.lexical.stable(*decorator_binding)
                    and decorator_binding[1][0].get("kind") == "import_alias"
                )
                self.fixtures.append(
                    FixtureFact(
                        name=name,
                        node_id=pytest_fixture_id(f"{file.module}.{qualname}"),
                        handler_id=symbol_id_for_def(file, qualname, id_factory),
                        file=file,
                        stmt=stmt,
                        owner=owner,
                        stable=stable,
                        autouse=(
                            isinstance(options.get("autouse"), ast.Constant)
                            and options["autouse"].value is True
                        ),
                        async_enabled=resolve_alias(
                            call_name(
                                decorator.func
                                if isinstance(decorator, ast.Call)
                                else decorator
                            ),
                            aliases,
                        )
                        == "pytest_asyncio.fixture",
                    )
                )

    def rank(self, fixture: FixtureFact, file: FileRecord, owner: str) -> int | None:
        return self.scope_rank(fixture.file.path, fixture.owner, file, owner)

    @staticmethod
    def scope_rank(
        path: str, fixture_owner: str, file: FileRecord, owner: str
    ) -> int | None:
        if path == file.path:
            if fixture_owner:
                return 10001 if fixture_owner == owner else None
            return 10000
        if fixture_owner:
            return None
        path = PurePosixPath(path)
        if path.name == "conftest.py" and (
            path.parent == PurePosixPath(".")
            or path.parent in PurePosixPath(file.path).parents
        ):
            return len(path.parents)
        return None

    def resolve(self, name: str, file: FileRecord, owner: str) -> FixtureFact | None:
        # Inherited providers and re-exported fixtures are outside this local
        # catalog. Do not silently select a lower-precedence conftest instead.
        if owner:
            cls = next(
                (
                    s
                    for s in self.trees[file.path].body
                    if isinstance(s, ast.ClassDef) and s.name == owner
                ),
                None,
            )
            if cls and cls.bases:
                return None
        alias = self.aliases[file.path].aliases.get(name)
        if alias and not any(
            f.name == name and f.file.path == file.path for f in self.fixtures
        ):
            return None
        candidates = [
            (rank, fixture)
            for fixture in self.fixtures
            if fixture.name == name
            and (rank := self.rank(fixture, file, owner)) is not None
        ]
        if not candidates:
            return None
        best = max(rank for rank, _ in candidates)
        if any(
            (rank := self.scope_rank(path, scope_owner, file, owner)) is not None
            and rank >= best
            for path, scope_owner in self.uncertain_scopes
        ):
            return None
        if any(
            name in aliases.aliases
            and (rank := self.scope_rank(path, "", file, owner)) is not None
            and rank >= best
            for path, aliases in self.aliases.items()
        ):
            return None
        selected = [fixture for rank, fixture in candidates if rank == best]
        return selected[0] if len(selected) == 1 and selected[0].stable else None

    def builtin_type(self, name: str, file: FileRecord, owner: str) -> str | None:
        """Built-ins are providers of last resort, never name-based fallbacks."""
        types = {
            "monkeypatch": "pytest.MonkeyPatch",
            "tmp_path": "pathlib.Path",
            "tmp_path_factory": "pytest.TempPathFactory",
            "capsys": "pytest.CaptureFixture",
            "capfd": "pytest.CaptureFixture",
            "capsysbinary": "pytest.CaptureFixture",
            "capfdbinary": "pytest.CaptureFixture",
            "caplog": "pytest.LogCaptureFixture",
            "pytestconfig": "pytest.Config",
            "request": "pytest.FixtureRequest",
            "recwarn": "pytest.WarningsRecorder",
            "cache": "pytest.Cache",
        }
        if name not in types:
            return None
        if owner and any(
            isinstance(s, ast.ClassDef) and s.name == owner and s.bases
            for s in self.trees[file.path].body
        ):
            return None
        if any(
            f.name == name and self.rank(f, file, owner) is not None
            for f in self.fixtures
        ):
            return None
        if any(
            self.scope_rank(path, scope, file, owner) is not None
            for path, scope in self.uncertain_scopes
        ):
            return None
        for path, aliases in self.aliases.items():
            if self.scope_rank(path, "", file, owner) is None:
                continue
            if name in aliases.aliases:
                return None
            # A statically declared plugin can replace built-ins; plugin execution
            # and discovery are outside this catalog.
            if any(
                (
                    isinstance(s, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "pytest_plugins"
                        for t in s.targets
                    )
                )
                or (
                    isinstance(s, ast.AnnAssign)
                    and s.value is not None
                    and isinstance(s.target, ast.Name)
                    and s.target.id == "pytest_plugins"
                )
                for s in self.trees[path].body
            ):
                return None
        return types[name]

    def autouse_names(self, file: FileRecord, owner: str) -> list[str]:
        return sorted(
            {
                fixture.name
                for fixture in self.fixtures
                if fixture.autouse and self.rank(fixture, file, owner) is not None
            }
        )

    @staticmethod
    def parameters(stmt: Function, owner: str) -> list[str]:
        """Pytest injects mandatory keyword-capable parameters, excluding self."""
        positional = [*stmt.args.posonlyargs, *stmt.args.args]
        default_names = (
            {a.arg for a in positional[len(positional) - len(stmt.args.defaults) :]}
            if stmt.args.defaults
            else set()
        )
        default_names.update(
            arg.arg
            for arg, default in zip(stmt.args.kwonlyargs, stmt.args.kw_defaults)
            if default is not None
        )
        args = [*stmt.args.args, *stmt.args.kwonlyargs]
        if (
            owner
            and not stmt.args.posonlyargs
            and args
            and not any(call_name(d) == "staticmethod" for d in stmt.decorator_list)
        ):
            args = args[1:]
        return [arg.arg for arg in args if arg.arg not in default_names]

    def direct_parameters(
        self, stmt: Function, file: FileRecord, owner: str
    ) -> set[str]:
        """Direct parametrization overrides a fixture, including inherited marks.

        Unknown mark lists/indirect expressions conservatively suppress inference.
        """
        decorators = list(stmt.decorator_list)
        tree = self.trees[file.path]
        mark_body = list(tree.body)
        if owner:
            for item in tree.body:
                if isinstance(item, ast.ClassDef) and item.name == owner:
                    decorators.extend(item.decorator_list)
                    mark_body.extend(item.body)
        for item in mark_body:
            if isinstance(item, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in item.targets
            ):
                value = item.value
                if isinstance(value, (ast.List, ast.Tuple)):
                    decorators.extend(value.elts)
                elif isinstance(value, ast.Call):
                    decorators.append(value)
                else:
                    return set(self.parameters(stmt, owner))
        names: set[str] = set()
        for decorator in decorators:
            if not isinstance(decorator, ast.Call):
                continue
            name = resolve_alias(call_name(decorator.func), self.aliases[file.path])
            if name not in {"pytest.mark.parametrize", "mark.parametrize"}:
                continue
            options = {k.arg: k.value for k in decorator.keywords}
            first = decorator.args[0] if decorator.args else options.get("argnames")
            value = literal_str(first)
            args = (
                set(v.strip() for v in value.split(",") if v.strip())
                if value
                else set(literal_str_list(first))
            )
            if not args:
                return set(self.parameters(stmt, owner))
            indirect = options.get("indirect")
            if isinstance(indirect, ast.Constant) and indirect.value is True:
                continue
            if isinstance(indirect, (ast.List, ast.Tuple)):
                args -= set(literal_str_list(indirect))
            names.update(args)
        return names
