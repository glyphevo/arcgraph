"""Lightweight TypeRef summaries for Python AST files."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from arcgraph.analyzers.calls.lexical import LexicalScopes, import_binding_target
from arcgraph.analyzers.exports import exported_class
from arcgraph.analyzers.external_types import (
    EXTERNAL_METHOD_RETURN_OWNERS,
    ASYNC_GENERATOR_TYPE_ID,
    GENERATOR_TYPE_ID,
    PATH_TYPE_IDS,
    function_return_type,
    literal_type_name,
    mapping_default,
    mapping_value_type,
    may_be_path_segment,
    method_return_type,
    sliced_value_type,
)
from arcgraph.analyzers.imports import ImportAnalyzer
from arcgraph.analyzers.python_scopes import breaks_out
from arcgraph.analyzers.type_unions import (
    contains_union,
    single_value_type,
    tuple_element_type,
    type_identity,
    union_alternatives,
    unknown_union_result,
)
from arcgraph.core.ids import class_id, type_ref_id
from arcgraph.core.schemas import Evidence, FileRecord, Node

_BUILTIN_TYPES = {
    "bool",
    "bytes",
    "complex",
    "dict",
    "float",
    "frozenset",
    "int",
    "list",
    "object",
    "set",
    "str",
    "tuple",
}
_NONE_NAMES = {"None", "NoneType"}
_TYPING_ORIGINS = {
    "Annotated",
    "Callable",
    "ClassVar",
    "Final",
    "Iterable",
    "Iterator",
    "Literal",
    "Mapping",
    "MutableMapping",
    "Optional",
    "Protocol",
    "Sequence",
    "Type",
    "Union",
}
_TRANSPARENT_ORIGINS = {"Annotated", "ClassVar", "Final", "Optional", "Type", "Union"}
_SCOPE_KINDS = {"module", "class", "function", "method"}
_EXTERNAL_CONSTRUCTOR_TYPES = {
    "extsym:argparse.ArgumentParser",
    "extsym:pathlib.Path",
    "extsym:threading.Thread",
    "extsym:tempfile.TemporaryDirectory",
}
_PYDANTIC_MODEL_BASES = {
    "pydantic.BaseModel",
    "pydantic.BaseSettings",
    "pydantic_settings.BaseSettings",
}
_PYDANTIC_INSTANCE_CLASSMETHOD_STRATEGIES = {
    "model_construct": "pydantic_model_construct",
    "model_validate": "pydantic_model_validate",
    "parse_obj": "pydantic_parse_obj",
}


def _statement_blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
    blocks = [
        getattr(stmt, field)
        for field in ("body", "orelse", "finalbody")
        if isinstance(getattr(stmt, field, None), list)
    ]
    for handler in getattr(stmt, "handlers", []) or []:
        blocks.append(handler.body)
    for case in getattr(stmt, "cases", []) or []:
        blocks.append(case.body)
    return [block for block in blocks if block]


def _binds_name(stmt: ast.AST, name: str) -> bool:
    for node in ast.walk(stmt):
        if isinstance(node, ast.Name) and node.id == name:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                return True
        elif isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.ExceptHandler,
                ast.MatchAs,
                ast.MatchStar,
            ),
        ):
            if node.name == name:
                return True
        elif isinstance(node, ast.MatchMapping):
            if node.rest == name:
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(
                (alias.asname or alias.name.split(".")[0]) == name
                for alias in node.names
            ):
                return True
    return False


_EXITS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _ends_in_exit(block: list[ast.stmt]) -> bool:
    return bool(block) and isinstance(block[-1], _EXITS)


def _is_name(node: ast.AST, name: str) -> bool:
    """Whether ``node`` reads name, or binds and reads it as (name := ...)."""

    if isinstance(node, ast.NamedExpr):
        node = node.target
    return isinstance(node, ast.Name) and node.id == name


def _compared_with_none(
    test: ast.expr, name: str, ops: tuple[type, ...]
) -> ast.expr | None:
    """The operand that reads name in ``test`` if it is name <op> None or
    None <op> name, op among ``ops``."""

    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ops)
    ):
        return None
    sides = (test.left, test.comparators[0])
    return next(
        (
            side
            for side, other in (sides, sides[::-1])
            if _is_name(side, name)
            and isinstance(other, ast.Constant)
            and other.value is None
        ),
        None,
    )


def _parts(test: ast.expr, op: type) -> list[ast.expr]:
    """The operands of ``test`` as a chain of ``op``, or ``test`` alone."""

    if isinstance(test, ast.BoolOp) and isinstance(test.op, op):
        return list(test.values)
    return [test]


def _absent_operand(part: ast.expr, name: str) -> ast.expr | None:
    """The operand reading name if ``part`` holds whenever name is None: not
    name, or name is None or == None, None on either side."""

    if (
        isinstance(part, ast.UnaryOp)
        and isinstance(part.op, ast.Not)
        and _is_name(part.operand, name)
    ):
        return part.operand
    # name == None holds whenever name is None, as name is None does; another
    # value it holds for only takes the same branch too.
    return _compared_with_none(part, name, (ast.Is, ast.Eq))


def _present_operand(part: ast.expr, name: str) -> ast.expr | None:
    """The operand reading name if ``part`` fails whenever name is None:
    name, or name is not None or != None, None on either side."""

    if _is_name(part, name):
        return part
    return _compared_with_none(part, name, (ast.IsNot, ast.NotEq))


def _constant_truth(part: ast.expr) -> bool | None:
    """The truth of a constant, or of a walrus that binds one."""

    if isinstance(part, ast.NamedExpr):
        part = part.value
    if isinstance(part, ast.Constant):
        return bool(part.value)
    return None


def _walruses(test: ast.expr, name: str) -> list[ast.NamedExpr]:
    """The walruses in ``test`` that bind name, in the order they run."""

    found = [
        node
        for node in ast.walk(test)
        if isinstance(node, ast.NamedExpr) and _is_name(node.target, name)
    ]
    return sorted(found, key=lambda node: (node.lineno, node.col_offset))


def _decides(
    test: ast.expr,
    name: str,
    op: type,
    read: Callable[[ast.expr, str], ast.expr | None],
    neutral: bool,
) -> bool:
    """Whether ``test`` decides that name is not None where the chain of
    ``op`` ends one way: the branch an or of absence tests fails on, or one
    an and of presence tests passes, or the other.

    Where some operand of the chain reads name that way, every operand runs
    on that branch, so the value name keeps is the last a walrus in the test
    binds, which must be one the chain reads. Otherwise every operand must
    read name that way or be a constant of the truth ``neutral`` that cannot
    end the chain; the branch is then taken at the operand that ends it,
    which reads the value name has there, so every walrus that binds name
    must be one the chain reads."""

    walruses = _walruses(test, name)
    parts = _parts(test, op)
    operands = [o for part in parts if (o := read(part, name)) is not None]
    if operands:
        return not walruses or any(o is walruses[-1] for o in operands)
    other = ast.And if op is ast.Or else ast.Or
    parts = _parts(test, other)
    operands = []
    for part in parts:
        operand = read(part, name)
        if operand is not None:
            operands.append(operand)
        elif _constant_truth(part) is not neutral or _walruses(part, name):
            return False
    return bool(operands) and all(
        any(o is walrus for o in operands) for walrus in walruses
    )


def _true_when_absent(test: ast.expr, name: str) -> bool:
    """Whether ``test`` holds whenever name is None, so that where it fails
    name is not None: an or with one operand that tests name is None, or an
    and of such tests and true constants."""

    return _decides(test, name, ast.Or, _absent_operand, True)


def _true_only_when_present(test: ast.expr, name: str) -> bool:
    """Whether ``test`` fails whenever name is None, so that where it holds
    name is not None: an and with one operand that tests name is present, or
    an or of such tests and false constants."""

    return _decides(test, name, ast.And, _present_operand, False)


def _matches_none(pattern: ast.pattern) -> bool:
    """Whether a case pattern surely takes None: None, or None | another."""

    if isinstance(pattern, ast.MatchSingleton):
        return pattern.value is None
    if isinstance(pattern, ast.MatchOr):
        return any(_matches_none(alternative) for alternative in pattern.patterns)
    return False


def _statement_excludes_none(stmt: ast.stmt, name: str) -> bool:
    """Whether the statement after ``stmt`` runs only with name not None, by
    ``stmt`` alone: if not name: return (or raise, continue, break) with no
    else; while name is None: with no break out of it; assert name is not
    None; match name: with a first case None (or None | another), with no
    guard or a true constant one, that leaves, and no case that binds name."""

    if isinstance(stmt, ast.If):
        return (
            not stmt.orelse
            and _ends_in_exit(stmt.body)
            and _true_when_absent(stmt.test, name)
        )
    if isinstance(stmt, ast.While):
        # The loop ends normally only once its test is false, read on the
        # name's value then, however its body rebinds it.
        return (
            _true_when_absent(stmt.test, name)
            and not breaks_out(stmt)
            and not any(_binds_name(part, name) for part in stmt.orelse)
        )
    if isinstance(stmt, ast.Assert):
        # Taken as run, as type checkers take it; python -O drops it.
        return _true_only_when_present(stmt.test, name)
    if isinstance(stmt, ast.Match) and stmt.cases:
        # A case before it, such as case _:, could take None and go on.
        first = stmt.cases[0]
        return (
            isinstance(stmt.subject, ast.Name)
            and stmt.subject.id == name
            and _matches_none(first.pattern)
            and (
                first.guard is None
                or isinstance(first.guard, ast.Constant)
                and bool(first.guard.value)
            )
            and _ends_in_exit(first.body)
            and not _binds_name(stmt, name)
        )
    return False


def _settled_before(statements: list[ast.stmt], name: str) -> bool | None:
    """Reading ``statements`` back from the last: True at one after which
    name is not None, False at one that may bind it, None if neither."""

    for stmt in reversed(statements):
        if _statement_excludes_none(stmt, name):
            return True
        if (
            isinstance(stmt, ast.Try)
            and stmt.handlers
            and all(_ends_in_exit(handler.body) for handler in stmt.handlers)
            and not any(_binds_name(part, name) for part in stmt.finalbody)
        ):
            # Past a try whose handlers all leave, its body and else ran to
            # their end.
            settled = _settled_before([*stmt.body, *stmt.orelse], name)
            if settled is not None:
                return settled
        if _binds_name(stmt, name):
            return False
    return None


def _header_binds(stmt: ast.stmt, name: str) -> bool:
    """Whether what runs before a block of ``stmt``, other than its blocks,
    may bind name: a test, a with item, a case pattern or guard."""

    parts: list[ast.AST] = []
    if isinstance(stmt, (ast.If, ast.While)):
        parts = [stmt.test]
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        parts = list(stmt.items)
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        parts = [stmt.target, stmt.iter]
    elif isinstance(stmt, ast.Match):
        parts = [stmt.subject]
        for case in stmt.cases:
            parts.append(case.pattern)
            if case.guard is not None:
                parts.append(case.guard)
    return any(_binds_name(part, name) for part in parts)


def _entered_settles(stmt: ast.stmt, block: list[ast.stmt], name: str) -> bool | None:
    """What entering ``block`` of ``stmt`` says of name: True if it is entered
    only with name not None, False if name may have been bound on the way in,
    None if neither."""

    if isinstance(stmt, ast.If):
        # A walrus in the test that these accept binds the value they test.
        if block is stmt.body and _true_only_when_present(stmt.test, name):
            return True
        if block is stmt.orelse and _true_when_absent(stmt.test, name):
            return True
    if _header_binds(stmt, name):
        return False
    if isinstance(stmt, ast.If):
        return None
    if isinstance(stmt, ast.Try):
        if block is stmt.orelse:
            # The body ran to its end.
            return _settled_before(stmt.body, name)
        # A handler or finally may run after any part of the body, and
        # finally after a handler or else too.
        ran: list[ast.AST] = list(stmt.body)
        handler = next((h for h in stmt.handlers if h.body is block), None)
        if handler is not None:
            ran.append(handler)
        elif block is stmt.finalbody:
            ran.extend([*stmt.handlers, *stmt.orelse])
        if any(_binds_name(part, name) for part in ran):
            return False
    return None


def _generator_return_type(node: Node, nodes: list[Node]) -> "_ResolvedType | None":
    """What calling a generator body returns, independently of its annotation:
    a generator, or an async generator for an async def. Unknown
    decorators can replace that return value, including when copying annotations."""

    if not node.properties.get("generator"):
        return None
    if not _generator_decorators_preserve_return(node, nodes):
        return None
    type_id = (
        ASYNC_GENERATOR_TYPE_ID if node.properties.get("async") else GENERATOR_TYPE_ID
    )
    return _ResolvedType(
        expression=type_id.removeprefix("extsym:"),
        type_id=type_id,
        symbol_id=type_id,
        status="resolved",
    )


def _generator_decorators_preserve_return(
    node: Node,
    nodes: list[Node],
    *,
    decorators: list[str] | None = None,
    allow_property: bool = False,
) -> bool:
    """Only unshadowed builtin descriptors preserve a decorated generator's
    return. Check the definition's enclosing scopes, not its parameters/body.
    Qualified descriptors need a visible import of the standard builtins module,
    with no observed descriptor write or module escape before decoration.
    """

    decorators = (
        node.properties.get("decorators", []) if decorators is None else decorators
    )
    if not decorators:
        return True
    if node.kind != "method" and any(
        str(name).rsplit(".", 1)[-1] in {"classmethod", "property"}
        for name in decorators
    ):
        # The descriptor is callable only after binding it through a class.
        return False
    descriptor_names = {"staticmethod", "classmethod"}
    if allow_property:
        descriptor_names.add("property")
    bare = {name for name in decorators if name in descriptor_names}
    qualified = set()
    for name in decorators:
        if name in bare:
            continue
        root, dot, member = str(name).rpartition(".")
        if not dot or not root.isidentifier() or member not in descriptor_names:
            return False
        qualified.add(root)
    names = bare | qualified
    by_id = {scope.id: scope for scope in nodes if scope.path == node.path}
    for scope in by_id.values():
        bindings = scope.properties.get("bindings", [])
        for name in names:
            own = [binding for binding in bindings if binding.get("name") == name]
            if any(binding.get("kind") == "global" for binding in own) and any(
                binding.get("kind") != "global" for binding in own
            ):
                return False
    module = next((scope for scope in by_id.values() if scope.kind == "module"), None)
    parent_id = node.properties.get("lexical_parent")
    if not parent_id and node.kind == "method" and node.qualname:
        parent_id = class_id(node.qualname.rsplit(".", 1)[0])
    owners = []
    seen = set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = by_id.get(str(parent_id))
        if parent is None:
            return False
        owners.append(parent)
        parent_id = parent.properties.get("lexical_parent")
    if module is None:
        return False
    owners.append(module)
    imports = {}
    for owner in owners:
        for binding in owner.properties.get("bindings", []):
            if binding.get("static_only"):
                continue
            if owner.kind in {"module", "class"} and (binding.get("line") or 0) >= (
                node.start_line or 0
            ):
                continue
            name = binding.get("name")
            if name in {*bare, "*"}:
                return False
            if name in qualified and binding.get("kind") != "re_export":
                if name in imports and imports[name][0] != owner.id:
                    continue
                if owner.kind not in {"module", "class"} and (
                    (binding.get("line") or 0) >= (node.start_line or 0)
                    or not binding.get("generator_descriptor_import_direct")
                ):
                    # A later local import makes the name local but unavailable
                    # when this decorator is evaluated. Do not fall back outward.
                    return False
                if (
                    binding.get("kind") != "import_alias"
                    or (binding.get("target_qualname") or binding.get("value"))
                    != "builtins"
                    or binding.get("may_not_run")
                    or not binding.get("scope_direct", True)
                ):
                    return False
                imports[str(name)] = (owner.id, binding)
    return qualified <= imports.keys() and _generator_descriptor_module_unchanged(
        node, list(by_id.values()), qualified
    )


def _generator_descriptor_module_unchanged(
    node: Node,
    scopes: list[Node],
    roots: set[str],
    *,
    attributes: set[str] | None = None,
) -> bool:
    """Visible writes and escapes invalidate the qualified descriptor proof.
    This is not a model of arbitrary reflection or cross-module monkeypatching.
    """

    if not roots:
        return True
    if attributes is None:
        attributes = {"staticmethod", "classmethod"}
    for scope in scopes:

        def before_decoration(record: dict[str, Any]) -> bool:
            return scope.kind not in {"module", "class"} or (
                record.get("line") or 0
            ) < (node.start_line or 0)

        for write in scope.properties.get("generator_descriptor_writes", []):
            if (
                write.get("receiver") in roots
                and write.get("attribute") in attributes
                and before_decoration(write)
            ):
                return False
        expressions = [
            (call.get("call_expression"), call)
            for call in scope.properties.get("callsites", [])
            if isinstance(call, dict)
        ]
        expressions.extend(
            (binding.get("value"), binding)
            for binding in scope.properties.get("bindings", [])
            if binding.get("kind") not in {"import_alias", "re_export"}
            and not binding.get("static_only")
        )
        for expression, record in expressions:
            if not expression or not before_decoration(record):
                continue
            try:
                parsed = ast.parse(str(expression), mode="eval")
            except SyntaxError:
                continue
            parents = {
                child: part
                for part in ast.walk(parsed)
                for child in ast.iter_child_nodes(part)
            }
            for part in ast.walk(parsed):
                if not isinstance(part, ast.Name) or part.id not in roots:
                    continue
                parent = parents.get(part)
                if not (isinstance(parent, ast.Attribute) and parent.value is part):
                    return False
    return True


@dataclass(frozen=True)
class _ResolvedType:
    expression: str
    type_id: str | None = None
    symbol_id: str | None = None
    origin: str | None = None
    type_args: tuple[dict[str, Any], ...] = ()
    status: str = "unresolved"


@dataclass
class TypeRefAnalysis:
    type_refs_by_scope: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    diagnostics_by_scope: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    type_refs_by_binding: dict[str, dict[str, Any]] = field(default_factory=dict)

    def attach_to_nodes(self, nodes: list[Node]) -> None:
        by_id = {node.id: node for node in nodes}
        for scope_id, type_refs in self.type_refs_by_scope.items():
            node = by_id.get(scope_id)
            if node is not None and type_refs:
                existing = node.properties.get("type_refs", [])
                if not isinstance(existing, list):
                    existing = []
                node.properties["type_refs"] = sorted(
                    [*existing, *type_refs],
                    key=lambda item: (
                        item.get("line") or 0,
                        item.get("column") or 0,
                        item.get("name") or "",
                        item.get("strategy") or "",
                    ),
                )
                self._attach_binding_type_refs(node)
        for scope_id, diagnostics in self.diagnostics_by_scope.items():
            node = by_id.get(scope_id)
            if node is not None and diagnostics:
                existing = node.properties.get("type_diagnostics", [])
                if not isinstance(existing, list):
                    existing = []
                node.properties["type_diagnostics"] = [*existing, *diagnostics]

    def _attach_binding_type_refs(self, node: Node) -> None:
        bindings = node.properties.get("bindings", [])
        if not isinstance(bindings, list):
            return
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            binding_ref = self.type_refs_by_binding.get(str(binding.get("binding_id")))
            if binding_ref is None:
                continue
            binding["type_ref_id"] = binding_ref["type_ref_id"]
            binding["type_ref_strategy"] = binding_ref["strategy"]
            binding["type_ref_confidence"] = binding_ref["confidence"]
            type_id = binding_ref.get("type_id")
            if isinstance(type_id, str):
                binding["type_ref"] = type_id


class TypeRefAnalyzer:
    """Collect V2.2 TypeRef summaries without changing call resolution."""

    def prepare_return_analysis(self, nodes: list[Node]) -> dict[str, bool]:
        """Start a type pass after binding collection has completed.

        Only decorator identity evidence is shared. Annotation resolution still
        depends on each file's imports. Standalone analyze calls remain uncached.
        A new preparation discards all evidence from the previous binding pass.
        """
        self._prepared_nodes = nodes
        self._return_decorator_cache: dict[str, bool] = {}
        return self._return_decorator_cache

    def analyze(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        nodes: list[Node],
        module_names: set[str],
    ) -> TypeRefAnalysis:
        cache = (
            self._return_decorator_cache
            if getattr(self, "_prepared_nodes", None) is nodes
            else None
        )
        context = _TypeContext(file_record, tree, nodes, module_names, cache)
        analysis = TypeRefAnalysis()
        context.attach_type_traits()

        for node in context.current_scope_nodes():
            annotations = [node.properties.get("returns")]
            annotations.extend(
                binding.get("annotation")
                for binding in node.properties.get("bindings", [])
                if isinstance(binding, dict)
            )
            # Keep the dependency marker even when the imported type is
            # currently missing, so a later provider restoration refreshes it.
            if any(
                context.uses_imported_annotation(value) for value in annotations
            ) or context.uses_imported_call(node):
                node.properties["imported_type_input"] = True
            self._record_return_type(node, context, analysis)
            self._record_binding_type_refs(node, context, analysis)

        return analysis

    def _record_return_type(
        self,
        node: Node,
        context: "_TypeContext",
        analysis: TypeRefAnalysis,
    ) -> None:
        returns = node.properties.get("returns")
        resolved = context.callable_return_type(node)
        declared_only = resolved is None
        if declared_only:
            if (
                node.kind not in {"function", "method"}
                or not isinstance(returns, str)
                or not returns
            ):
                return
            # Keep what the source declares visible to queries. It is not
            # evidence of the value returned after an unknown decoration.
            resolved = context.resolve_annotation(returns, use_imports=True)
        generated = bool(node.properties.get("generator")) and not declared_only
        if generated:
            returns = resolved.expression
        type_ref = self._type_ref_record(
            context,
            scope_id=node.id,
            scope_kind=node.kind,
            name="return",
            subject_kind="declared_return" if declared_only else "return",
            strategy="generator_function" if generated else "return_annotation",
            confidence="confirmed" if resolved.type_id else "unresolved",
            source_expression=returns,
            resolved=resolved,
            line=node.start_line,
            end_line=node.end_line,
            column=None,
        )
        analysis.type_refs_by_scope.setdefault(node.id, []).append(type_ref)
        if contains_union(type_ref):
            node.properties["union_type_input"] = True
        if not resolved.type_id and context.looks_custom_type(returns):
            self._add_diagnostic(
                analysis,
                node.id,
                "type_resolution_unresolved",
                "return",
                returns,
                context.file_record.path,
                node.start_line,
                None,
            )

    def _record_binding_type_refs(
        self,
        node: Node,
        context: "_TypeContext",
        analysis: TypeRefAnalysis,
    ) -> None:
        bindings = node.properties.get("bindings", [])
        if not isinstance(bindings, list):
            return

        local_types: dict[str, dict[str, Any]] = {}
        ordered = sorted(
            (item for item in bindings if isinstance(item, dict)),
            key=lambda item: (
                item.get("line") or 0,
                item.get("column") or 0,
                item.get("name") or "",
                item.get("kind") or "",
            ),
        )
        if node.properties.get("comprehension_contexts"):
            # The binding visitor follows generator evaluation order. Textual
            # order is different for result expressions and nested generators.
            ordered = [item for item in bindings if isinstance(item, dict)]
        for binding in ordered:
            value_types = local_types
            is_comprehension = binding.get("kind") == "comprehension_target"
            if is_comprehension:
                value_types = local_types.copy()
                for name, provider in binding.get("comprehension_inputs", {}).items():
                    for field in list(value_types):
                        if field.startswith(name + "."):
                            del value_types[field]
                    value_types[name] = analysis.type_refs_by_binding.get(provider) or {
                        **unknown_union_result(name),
                        "comprehension_binding": True,
                    }
            type_ref = self._binding_type_ref(binding, node, context, value_types)
            if is_comprehension and type_ref is not None:
                type_ref["comprehension_binding"] = True
                type_ref["typed_value_evidence"] = True
            if type_ref is None:
                previous = local_types.get(str(binding.get("name")))
                if previous and (
                    previous.get("typed_value_evidence")
                    or union_alternatives(previous) is not None
                ):
                    local_types[str(binding["name"])] = unknown_union_result(
                        str(binding.get("value") or binding["name"])
                    )
                continue
            analysis.type_refs_by_scope.setdefault(node.id, []).append(type_ref)
            if contains_union(type_ref):
                node.properties["union_type_input"] = True
            binding_id = binding.get("binding_id")
            if isinstance(binding_id, str):
                analysis.type_refs_by_binding[binding_id] = type_ref
            if is_comprehension:
                continue
            if (
                isinstance(type_ref.get("type_id"), str)
                or union_alternatives(type_ref) is not None
            ):
                local_types[str(binding["name"])] = type_ref
                found = context.lexical.lookup(node, str(binding["name"]))
                preserved = context.preserve_nullable_binding(
                    node, str(binding["name"]), type_ref
                )
                if (
                    union_alternatives(type_ref) is not None
                    and binding.get("annotation")
                    and found
                    and not context.lexical.stable(*found)
                    and not preserved
                ):
                    # A later write invalidates propagation through aliases too.
                    local_types[str(binding["name"])] = unknown_union_result(
                        str(binding["name"])
                    )
                context.register_class_instance_attr(
                    node,
                    str(binding["name"]),
                    type_ref,
                )

            if (
                type_ref.get("resolution_status") == "unresolved"
                and isinstance(binding.get("annotation"), str)
                and context.looks_custom_type(binding["annotation"])
            ):
                self._add_diagnostic(
                    analysis,
                    node.id,
                    "type_resolution_unresolved",
                    str(binding["name"]),
                    binding["annotation"],
                    context.file_record.path,
                    self._int_or_none(binding.get("line")),
                    self._int_or_none(binding.get("column")),
                    binding_id=binding_id if isinstance(binding_id, str) else None,
                )

    def _binding_type_ref(
        self,
        binding: dict[str, Any],
        node: Node,
        context: "_TypeContext",
        local_types: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        name = binding.get("name")
        if not isinstance(name, str) or not name:
            return None

        annotation = binding.get("annotation")
        if isinstance(annotation, str) and annotation:
            resolved = context.resolve_annotation(annotation, use_imports=True)
            return self._type_ref_record(
                context,
                scope_id=node.id,
                scope_kind=node.kind,
                name=name,
                subject_kind="binding",
                strategy="annotation",
                confidence="confirmed" if resolved.type_id else "unresolved",
                source_expression=annotation,
                resolved=resolved,
                line=self._int_or_none(binding.get("line")),
                end_line=self._int_or_none(
                    binding.get("evidence", {}).get("end_line")
                    if isinstance(binding.get("evidence"), dict)
                    else None
                ),
                column=self._int_or_none(binding.get("column")),
                binding_id=(
                    binding.get("binding_id")
                    if isinstance(binding.get("binding_id"), str)
                    else None
                ),
            )

        value = binding.get("value")
        if isinstance(value, str) and value:
            propagated = self._propagated_type_ref(
                value,
                name,
                binding,
                node,
                context,
                local_types,
            )
            if propagated is not None:
                return propagated
        return None

    def _propagated_type_ref(
        self,
        value: str,
        name: str,
        binding: dict[str, Any],
        node: Node,
        context: "_TypeContext",
        local_types: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        parsed = context.parse_expression(value)
        if parsed is None:
            return None

        source = (
            unknown_union_result(value)
            if binding.get("comprehension_write")
            else (
                context.resolve_conditional_value(parsed, node, local_types, binding)
                if isinstance(parsed, ast.IfExp)
                else context.resolve_scoped_value(parsed, node, local_types)
            )
        )
        if (
            source is not None
            and isinstance(parsed, ast.Name)
            and binding.get("kind") in {"assignment", "annotated_assignment"}
            and any(
                member.get("type_id") == "builtin:None"
                for member in union_alternatives(source) or []
            )
            and isinstance(binding.get("line"), int)
            and context.none_excluded_before(
                node, parsed.id, binding["line"], int(binding.get("column") or 0)
            )
        ):
            # if not source: return before shared = source: what is assigned
            # is not None.
            source = {**single_value_type(source), "strategy": "none_excluded"}
        untyped_comprehension = (
            binding.get("kind") == "comprehension_target" and source is None
        )
        if binding.get("kind") == "instance_attribute":
            joined = context.typing_only_attribute(node, name, local_types)
            if joined is not None:
                source = joined
        if source is None:
            if binding.get("kind") != "comprehension_target":
                return None
            source = unknown_union_result(value)

        if binding.get("kind") == "with_as":
            context_manager_kind = binding.get("context_manager_kind")
            if context_manager_kind not in {"sync", "async"}:
                return None
            entered = context.context_manager_enter_source(
                source,
                is_async=context_manager_kind == "async",
            )
            if entered is None:
                return None
            source = entered

        if binding.get("kind") in {"for_target", "comprehension_target"}:
            iterated = self._iterated_type_source(source)
            if iterated is None and isinstance(parsed, (ast.Tuple, ast.List, ast.Set)):
                iterated = self._display_element_type(
                    parsed, node, context, local_types
                )
            if iterated is not None:
                source = iterated
            elif binding.get("kind") == "for_target":
                # An element of a container of unknown element type has no
                # known type; it is not the container, which typed the loop
                # variable of for path in (*a, *b) as a tuple.
                return None
            elif binding.get("kind") == "comprehension_target":
                untyped_comprehension = (
                    untyped_comprehension or union_alternatives(source) is None
                )
                source = unknown_union_result(value)

        if binding.get("unpack_path"):
            source = self._unpacked_type_source(source, binding["unpack_path"], value)

        strategy = source.get("strategy")
        if binding.get("kind") == "with_as":
            strategy = (
                "async_context_manager_enter"
                if binding.get("context_manager_kind") == "async"
                else "context_manager_enter"
            )
        elif binding.get("kind") == "instance_attribute" and isinstance(
            parsed, ast.Name
        ):
            strategy = "instance_attribute_propagation"
        elif binding.get("kind") in {"for_target", "comprehension_target"}:
            strategy = "iteration_element"
        elif isinstance(parsed, (ast.Attribute, ast.Name)):
            strategy = "assignment_propagation"
        if not isinstance(strategy, str):
            strategy = "assignment_propagation"

        result = self._type_ref_record(
            context,
            scope_id=node.id,
            scope_kind=node.kind,
            name=name,
            subject_kind="binding",
            strategy=strategy,
            confidence="inferred",
            source_expression=value,
            resolved=_ResolvedType(
                expression=str(source.get("type_expression") or value),
                type_id=(
                    source.get("type_id")
                    if isinstance(source.get("type_id"), str)
                    else None
                ),
                symbol_id=(
                    source.get("symbol_id")
                    if isinstance(source.get("symbol_id"), str)
                    else None
                ),
                origin=(
                    source.get("origin")
                    if isinstance(source.get("origin"), str)
                    else None
                ),
                type_args=(
                    tuple(
                        item
                        for item in source.get("type_args", [])
                        if isinstance(item, dict)
                    )
                    if isinstance(source.get("type_args"), list)
                    else ()
                ),
                status=(
                    "resolved"
                    if isinstance(source.get("type_id"), str)
                    else "unresolved"
                ),
            ),
            line=self._int_or_none(binding.get("line")),
            end_line=self._int_or_none(
                binding.get("evidence", {}).get("end_line")
                if isinstance(binding.get("evidence"), dict)
                else None
            ),
            column=self._int_or_none(binding.get("column")),
            binding_id=(
                binding.get("binding_id")
                if isinstance(binding.get("binding_id"), str)
                else None
            ),
            inferred_from_type_ref_id=(
                source.get("type_ref_id")
                if isinstance(source.get("type_ref_id"), str)
                else None
            ),
        )

        if untyped_comprehension:
            result["untyped_comprehension"] = True
        if binding.get("unpack_path") or source.get("typed_value_evidence"):
            result["typed_value_evidence"] = True
        return result

    @staticmethod
    def _unpacked_type_source(
        source: dict[str, Any], path: list[list[int]], expression: str
    ) -> dict[str, Any]:
        result = source
        for index, size in path:
            result = single_value_type(result)
            args: list[dict[str, Any]] = result.get("type_args", [])
            if (
                result.get("type_id") != "builtin:tuple"
                or size < 0
                or len(args) != size
                or any(arg.get("type_expression") == "..." for arg in args)
            ):
                return unknown_union_result(expression)
            result = args[index]
        return {**result, "strategy": "tuple_element", "source_expression": expression}

    def _display_element_type(
        self,
        display: ast.Tuple | ast.List | ast.Set,
        scope_node: Node,
        context: "_TypeContext",
        local_types: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """The one type every element of a display has, a starred element
        contributing the elements of what it unpacks: for path in (*a, *b)
        iterates paths when a and b are tuples of paths."""

        items: list[dict[str, Any]] = []
        for element in display.elts:
            if isinstance(element, ast.Starred):
                container = context.resolve_scoped_value(
                    element.value, scope_node, local_types
                )
                item = self._iterated_type_source(container) if container else None
            else:
                item = context.resolve_scoped_value(element, scope_node, local_types)
            if (
                not item
                or union_alternatives(item) is not None
                or not isinstance(item.get("type_id"), str)
            ):
                return None
            items.append(item)
        if not items or len({type_identity(item) for item in items}) != 1:
            return None
        element = dict(items[0])
        element["strategy"] = "iteration_element"
        if any(item.get("typed_value_evidence") for item in items):
            element["typed_value_evidence"] = True
        return element

    @staticmethod
    def _iterated_type_source(source: dict[str, Any]) -> dict[str, Any] | None:
        wrapped = union_alternatives(source) is not None
        source = single_value_type(source)
        type_id = source.get("type_id")
        origin = source.get("origin")
        type_args = source.get("type_args")
        if not isinstance(type_args, list) or not type_args:
            return None
        if type_id == "builtin:tuple":
            item = tuple_element_type(source)
        elif type_id in {
            "builtin:list",
            "builtin:tuple",
            "builtin:set",
            "builtin:frozenset",
        }:
            item = type_args[0]
        elif origin in {
            "Iterable",
            "Iterator",
            "Sequence",
            "Generator",
            "AsyncGenerator",
            "AsyncIterator",
            "AsyncIterable",
        }:
            item = type_args[0]
        elif type_id == "builtin:dict":
            item = type_args[0]
        else:
            return None
        if not isinstance(item, dict):
            return None
        propagated = dict(item)
        propagated["strategy"] = "iteration_element"
        if wrapped or source.get("typed_value_evidence"):
            propagated["typed_value_evidence"] = True
        propagated["source_expression"] = str(source.get("source_expression") or "")
        if isinstance(source.get("type_ref_id"), str):
            propagated["type_ref_id"] = source["type_ref_id"]
        return propagated

    def _type_ref_record(
        self,
        context: "_TypeContext",
        *,
        scope_id: str,
        scope_kind: str,
        name: str,
        subject_kind: str,
        strategy: str,
        confidence: str,
        source_expression: str,
        resolved: _ResolvedType,
        line: int | None,
        end_line: int | None,
        column: int | None,
        binding_id: str | None = None,
        inferred_from_type_ref_id: str | None = None,
    ) -> dict[str, Any]:
        evidence = Evidence(
            kind=f"ast_type_ref:{strategy}",
            path=context.file_record.path,
            start_line=line,
            end_line=end_line or line,
            column=column,
            detail=name,
        )
        record: dict[str, Any] = {
            "type_ref_id": type_ref_id(
                scope_id,
                name,
                subject_kind,
                strategy,
                context.file_record.path,
                line,
                column,
                source_expression,
            ),
            "scope_id": scope_id,
            "scope_kind": scope_kind,
            "name": name,
            "subject_kind": subject_kind,
            "strategy": strategy,
            "confidence": confidence,
            "path": context.file_record.path,
            "line": line,
            "column": column,
            "source_expression": source_expression,
            "type_expression": resolved.expression,
            "resolution_status": resolved.status,
            "fallbacks": [strategy],
            "evidence": evidence.model_dump(exclude_none=True),
        }
        self._set_optional(record, "binding_id", binding_id)
        self._set_optional(record, "type_id", resolved.type_id)
        self._set_optional(record, "symbol_id", resolved.symbol_id)
        self._set_optional(record, "origin", resolved.origin)
        if resolved.type_args:
            record["type_args"] = list(resolved.type_args)
        self._set_optional(
            record, "inferred_from_type_ref_id", inferred_from_type_ref_id
        )
        return record

    def _add_diagnostic(
        self,
        analysis: TypeRefAnalysis,
        scope_id: str,
        kind: str,
        name: str,
        expression: str,
        path: str,
        line: int | None,
        column: int | None,
        *,
        binding_id: str | None = None,
    ) -> None:
        diagnostic: dict[str, Any] = {
            "kind": kind,
            "scope_id": scope_id,
            "name": name,
            "expression": expression,
            "path": path,
            "line": line,
            "column": column,
            "message": f"Could not resolve TypeRef for {name!r}: {expression}.",
        }
        self._set_optional(diagnostic, "binding_id", binding_id)
        analysis.diagnostics_by_scope.setdefault(scope_id, []).append(diagnostic)

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        return value if isinstance(value, int) else None

    @staticmethod
    def _set_optional(record: dict[str, Any], key: str, value: Any | None) -> None:
        if value is not None:
            record[key] = value


class _TypeContext:
    def __init__(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        nodes: list[Node],
        module_names: set[str],
        return_decorator_cache: dict[str, bool] | None = None,
    ) -> None:
        self._return_decorator_cache = (
            return_decorator_cache if return_decorator_cache is not None else {}
        )
        self.file_record = file_record
        self.tree = tree
        self.nodes = nodes
        self.parameter_defaults: dict[int, dict[str, str]] = self._parameter_defaults(
            tree
        )
        self.module_names = module_names
        self._nodes_by_path: dict[str | None, list[Node]] = {}
        for scope in nodes:
            self._nodes_by_path.setdefault(scope.path, []).append(scope)
        self.lexical = LexicalScopes(self._nodes_by_path.get(file_record.path, []), {})
        self._decorator_scopes = {file_record.path: self.lexical}
        self.aliases = self._import_aliases(tree.body)
        self.classes_by_id = {node.id: node for node in nodes if node.kind == "class"}
        self.export_nodes = {
            node.id: node for node in nodes if node.kind in {"module", "class"}
        }
        self.classes_by_name: dict[str, list[Node]] = {}
        for node in self.classes_by_id.values():
            self.classes_by_name.setdefault(node.name, []).append(node)
        self.return_types_by_function_name: dict[str, list[_ResolvedType]] = {}
        self.return_types_by_function_qualname: dict[str, _ResolvedType] = {}
        self.return_types_by_method: dict[tuple[str, str], _ResolvedType] = {}
        self.property_return_types_by_method: dict[tuple[str, str], _ResolvedType] = {}
        self.class_instance_attrs: dict[str, dict[str, dict[str, Any]]] = {}
        self._build_return_type_maps()

    def current_scope_nodes(self) -> list[Node]:
        return [
            node
            for node in sorted(self.nodes, key=lambda item: item.id)
            if node.kind in _SCOPE_KINDS and node.path == self.file_record.path
        ]

    def uses_imported_annotation(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        expression = self.parse_expression(value)
        if expression is None:
            return False
        return any(
            (isinstance(node, ast.Name) and node.id in self.aliases)
            or (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value != value
                and self.uses_imported_annotation(node.value)
            )
            for node in ast.walk(expression)
        )

    def uses_imported_call(self, scope: Node) -> bool:
        # Unannotated constructors and inline calls depend on import/export
        # bindings too. Record inputs, not only successful resolutions: missing
        # providers must remain refreshable when they return. Scope aliases also
        # cover imports inside functions and captured imports in nested helpers.
        names = set(self.aliases) | set(self.lexical.aliases(scope))
        if not names:
            return False
        for callsite in scope.properties.get("callsites", []):
            expression = self.parse_expression(str(callsite.get("call_expression", "")))
            if expression is not None and any(
                isinstance(part, ast.Name) and part.id in names
                for part in ast.walk(expression)
            ):
                return True
        return False

    def attach_type_traits(self) -> None:
        for node in self.current_scope_nodes():
            if node.kind != "class":
                continue
            bases = node.properties.get("bases", [])
            if not isinstance(bases, list):
                continue
            base_names = {str(base).strip("\"'").rsplit(".", 1)[-1] for base in bases}
            traits: dict[str, bool] = {}
            if "Protocol" in base_names:
                traits["protocol"] = True
            if base_names & {"ABC", "ABCMeta"}:
                traits["abstract_base"] = True
            if traits:
                node.properties["type_traits"] = traits

    def resolve_annotation(
        self, expression: str, *, use_imports: bool
    ) -> _ResolvedType:
        parsed = self.parse_expression(expression.strip())
        if parsed is None:
            return _ResolvedType(expression=expression)
        return self._resolve_type_node(parsed, expression, use_imports=use_imports)

    def parse_expression(self, expression: str) -> ast.AST | None:
        try:
            return ast.parse(expression, mode="eval").body
        except SyntaxError:
            stripped = expression.strip("\"'")
            if stripped == expression:
                return None
            try:
                return ast.parse(stripped, mode="eval").body
            except SyntaxError:
                return None

    def resolve_value(
        self,
        node: ast.AST,
        local_types: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        while isinstance(node, ast.Await):
            node = node.value
        if isinstance(node, ast.Name):
            return local_types.get(node.id)
        literal = self._literal_value_type(node)
        if literal is not None:
            return self._value_type_record(
                literal,
                strategy="literal",
                source_expression=self._unparse(node),
            )
        if isinstance(node, ast.Call):
            return self._resolve_call(node, local_types)
        return None

    def resolve_scoped_value(
        self,
        node: ast.AST,
        scope_node: Node,
        local_types: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        if (
            isinstance(node, ast.BoolOp)
            and isinstance(node.op, ast.Or)
            and len(node.values) == 2
            and isinstance(node.values[0], ast.Name)
            and node.values[0].id in scope_node.properties.get("nullable_bindings", {})
        ):
            source = local_types.get(node.values[0].id)
            fallback = node.values[1]
            literal = self._literal_value_type(fallback)
            empty = not isinstance(
                fallback, (ast.Dict, ast.List, ast.Set, ast.Tuple)
            ) or not (
                fallback.keys if isinstance(fallback, ast.Dict) else fallback.elts
            )
            if (
                source
                and literal
                and empty
                and literal.type_id == source.get("type_id")
            ):
                return {
                    **single_value_type(source),
                    "strategy": "nullable_default",
                    "typed_value_evidence": True,
                }
            if source:
                return unknown_union_result(self._unparse(node))
        while isinstance(node, ast.Await):
            node = node.value
        if isinstance(node, ast.Attribute):
            full_name = self._unparse(node)
            if full_name in local_types:
                return local_types[full_name]
            if (
                isinstance(node.value, ast.Name)
                and node.value.id in {"self", "cls"}
                and not local_types.get(node.value.id, {}).get("comprehension_binding")
                and self._own_receiver(scope_node, node.value.id)
            ):
                class_qualname = self._class_qualname_for_scope(scope_node)
                if class_qualname:
                    attr_ref = self.class_instance_attrs.get(class_qualname, {}).get(
                        node.attr
                    )
                    if attr_ref is not None:
                        return attr_ref
                    property_ref = self._property_type_ref(
                        class_qualname,
                        node.attr,
                        source_expression=full_name,
                    )
                    if property_ref is not None:
                        return property_ref
            receiver = self.resolve_scoped_value(node.value, scope_node, local_types)
            if union_alternatives(receiver) is not None and not receiver.get("type_id"):
                return unknown_union_result(self._unparse(node))
            type_id = receiver.get("type_id") if receiver else None
            if node.attr == "parent" and type_id in PATH_TYPE_IDS:
                return self._path_value_type(receiver, node, strategy="path_parent")
            if isinstance(type_id, str) and type_id.startswith("class:"):
                class_qualname = type_id.removeprefix("class:")
                return self.class_instance_attrs.get(class_qualname, {}).get(
                    node.attr
                ) or self._property_type_ref(
                    class_qualname,
                    node.attr,
                    source_expression=full_name,
                )
            return None
        if isinstance(node, ast.Subscript):
            receiver = self.resolve_scoped_value(node.value, scope_node, local_types)
            if not receiver:
                return None
            if union_alternatives(receiver) is not None and not receiver.get("type_id"):
                return unknown_union_result(self._unparse(node))
            if isinstance(node.slice, ast.Slice):
                return sliced_value_type(receiver)
            return self._subscript_value_type_source(receiver)
        if isinstance(node, ast.Call):
            return self._resolve_call(node, local_types, scope_node=scope_node)
        if isinstance(node, ast.JoinedStr):
            # An f-string is a str whatever it formats.
            return self._value_type_record(
                self._resolve_named_type("str", use_imports=False),
                strategy="literal",
                source_expression=self._unparse(node),
            )
        if isinstance(node, ast.BoolOp):
            # a or b and a and b give one of their operands: of a type every
            # operand has, when they share one.
            return self.same_type_of(node.values, scope_node, local_types)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = self.resolve_scoped_value(node.left, scope_node, local_types)
            if (
                left is not None
                and left.get("type_id") in PATH_TYPE_IDS
                and self._may_be_path_segment(node.right, scope_node, local_types)
            ):
                return self._path_value_type(left, node, strategy="path_join")
            # ``segment / path`` is a path of the right operand's flavour, by
            # its ``__rtruediv__``.
            right = self.resolve_scoped_value(node.right, scope_node, local_types)
            if (
                right is not None
                and right.get("type_id") in PATH_TYPE_IDS
                and self._may_be_path_segment(node.left, scope_node, local_types)
            ):
                return self._path_value_type(right, node, strategy="path_join")
            return None
        return self.resolve_value(node, local_types)

    def none_excluded_before(
        self, scope: Node, name: str, line: int, column: int
    ) -> bool:
        """Whether, where a statement at ``line`` and ``column`` runs, ``name``
        is known not to be None: an earlier statement of an enclosing block
        leaves the function or loop when it is, as in if not name: return, or
        asserts it is not; or the statement is in a branch taken only when it
        is not, as if name is not None:, or in the else of a try whose body
        did one of these; and nothing binds it in between, nor anywhere in a
        loop around it."""

        function = next(
            (
                n
                for n in ast.walk(self.tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.lineno == scope.start_line
            ),
            None,
        )
        if function is None:
            return False
        levels: list[tuple[list[ast.stmt], int, ast.stmt, list[ast.stmt] | None]] = []
        statements: list[ast.stmt] = function.body
        while True:
            index = next(
                (
                    i
                    for i, stmt in enumerate(statements)
                    if (stmt.lineno, stmt.col_offset)
                    <= (line, column)
                    <= (stmt.end_lineno or stmt.lineno, stmt.end_col_offset or 0)
                ),
                None,
            )
            if index is None:
                return False
            stmt = statements[index]
            if (stmt.lineno, stmt.col_offset) == (line, column):
                levels.append((statements, index, stmt, None))
                break
            inner = next(
                (
                    block
                    for block in _statement_blocks(stmt)
                    if any(
                        (child.lineno, child.col_offset)
                        <= (line, column)
                        <= (child.end_lineno or child.lineno, child.end_col_offset or 0)
                        for child in block
                    )
                ),
                None,
            )
            if inner is None:
                return False
            levels.append((statements, index, stmt, inner))
            statements = inner
        for statements, index, stmt, inner in reversed(levels):
            if inner is not None:
                if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                    # A later iteration may run after the name is rebound.
                    if _binds_name(stmt, name):
                        return False
                entered = _entered_settles(stmt, inner, name)
                if entered is not None:
                    return entered
            settled = _settled_before(statements[:index], name)
            if settled is not None:
                return settled
        return False

    def same_type_of(
        self,
        operands: list[ast.expr],
        scope_node: Node,
        local_types: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """The single type every one of ``operands`` has, or None if one has
        no known type, may be of several, or differs from the others.

        A name among them must have one stable binding in this scope and no
        default, so that its type is the value it holds here: a rewritten or
        conditionally bound name, or a parameter whose default may differ
        from its annotation, gives no type.
        """

        for operand in operands:
            for name in ast.walk(operand):
                if not isinstance(name, ast.Name):
                    continue
                found = self.lexical.lookup(scope_node, name.id)
                if (
                    found is None
                    or not self.lexical.stable(*found)
                    or self.parameter_defaults.get(scope_node.start_line, {}).get(
                        name.id
                    )
                    is not None
                ):
                    return None
        shared: dict[str, Any] | None = None
        for operand in operands:
            source = self.resolve_scoped_value(operand, scope_node, local_types)
            if (
                not source
                or union_alternatives(source) is not None
                or not isinstance(source.get("type_id"), str)
            ):
                return None
            if shared is None:
                shared = source
            elif source.get("type_id") != shared.get("type_id") or source.get(
                "type_args"
            ) != shared.get("type_args"):
                return None
        if shared is None:
            return None
        return {**shared, "strategy": "same_type_operands"}

    def _may_be_path_segment(
        self,
        node: ast.expr,
        scope_node: Node,
        local_types: dict[str, dict[str, Any]],
    ) -> bool:
        return may_be_path_segment(
            node,
            lambda operand: self.resolve_scoped_value(operand, scope_node, local_types),
        )

    def _path_value_type(
        self,
        source: dict[str, Any],
        node: ast.AST,
        *,
        strategy: str,
    ) -> dict[str, Any] | None:
        # A union such as ``Path | None`` does not say which member is joined.
        if union_alternatives(source) is not None:
            return None
        type_id = str(source["type_id"])
        return self._value_type_record(
            _ResolvedType(
                expression=type_id.rsplit(".", 1)[-1],
                type_id=type_id,
                symbol_id=type_id,
                status="resolved",
            ),
            strategy=strategy,
            source_expression=self._unparse(node),
        )

    def preserve_nullable_binding(
        self, scope: Node, name: str, ref: dict[str, Any]
    ) -> bool:
        """All writes retain the one annotated non-None runtime class."""
        members = union_alternatives(ref)
        if members is None or not ref.get("type_id"):
            return False
        found = self.lexical.lookup(scope, name)
        if (
            not found
            or found[0].id != scope.id
            or (scope.id, name) in self.lexical.mutated
        ):
            return False
        bindings: list[dict[str, Any]] = found[1]
        first = bindings[0]
        if first.get("kind") not in {
            "parameter",
            "annotated_assignment",
        } or not first.get("annotation"):
            return False
        if first.get("kind") != "parameter" and [
            first.get("line"),
            first.get("column"),
        ] not in scope.properties.get("scope_body_positions", []):
            return False
        expected = str(ref["type_id"])
        for binding in bindings:
            if binding.get("kind") == "parameter":
                default = self.parameter_defaults.get(scope.start_line, {}).get(name)
                if default is not None and default != "None":
                    return False
                continue
            if binding.get("kind") not in {"assignment", "annotated_assignment"}:
                return False
            value = self.parse_expression(str(binding.get("value") or ""))
            if (
                isinstance(value, ast.BoolOp)
                and isinstance(value.op, ast.Or)
                and len(value.values) == 2
                and isinstance(value.values[0], ast.Name)
                and value.values[0].id == name
            ):
                value = value.values[1]
            if isinstance(value, ast.Constant) and value.value is None:
                continue
            literal = self._literal_value_type(value) if value is not None else None
            if literal and literal.type_id == expected:
                if isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Tuple)) and (
                    value.keys if isinstance(value, ast.Dict) else value.elts
                ):
                    return False
                continue
            if isinstance(value, ast.Call):
                constructor = self._name(value.func)
                root = constructor.split(".")[0] if constructor else None
                definition = self.lexical.lookup(scope, root) if root else None
                if (
                    constructor
                    and definition
                    and self._stable_conditional_binding(definition)
                ):
                    item: dict[str, Any] = definition[1][0]
                    if item.get("kind") == "class_definition" and constructor != root:
                        return False
                    actual = (
                        item.get("target")
                        if item.get("kind") == "class_definition"
                        else f"extsym:{self._resolve_alias(constructor, use_imports=True)}"
                    )
                    if actual == expected and item.get("kind") in {
                        "class_definition",
                        "import_alias",
                    }:
                        continue
            return False
        scope.properties.setdefault("nullable_bindings", {})[name] = {
            "line": first.get("line"),
            "column": first.get("column"),
            "type_id": expected,
        }
        return True

    def resolve_conditional_value(
        self,
        expression: ast.IfExp,
        scope: Node,
        local_types: dict[str, dict[str, Any]],
        binding: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Join proven class instances, never guess the unknown fallback arm.

        This deliberately accepts only a stable local assignment, annotated
        parameters and named class constructors. General alias/dataflow and
        arbitrary factory calls need their own evidence rules.
        """
        scope.properties["conditional_type_input"] = True
        found = self.lexical.lookup(scope, str(binding.get("name")))
        if (
            scope.kind not in {"function", "method"}
            or binding.get("kind") != "assignment"
            or not found
            or found[0].id != scope.id
            or not self._stable_conditional_binding(found)
            or any(isinstance(n, ast.NamedExpr) for n in ast.walk(expression))
        ):
            return None
        branches = []
        for truth, arm in ((True, expression.body), (False, expression.orelse)):
            source = self._conditional_branch_type(arm, scope, local_types)
            if source is None:
                return None
            candidates: set[str] = self._conditional_class_candidates(source)
            if isinstance(arm, ast.Name) and self._excludes_none(
                expression.test, arm.id, truth
            ):
                candidates.discard("builtin:None")
            if len(candidates) != 1:
                return None
            type_id = next(iter(candidates))
            if not type_id.startswith("class:"):
                return None
            branches.append(type_id)
        if branches[0] != branches[1]:
            return None
        # The call resolver must apply lexical availability/mutation checks to
        # these new facts, including aliases and calls before the assignment.
        scope.properties["conditional_type_inference"] = True
        self.lexical.strict.add(scope.id)
        return {
            "type_id": branches[0],
            "symbol_id": branches[0],
            "type_expression": branches[0].removeprefix("class:"),
            "strategy": "conditional_join",
        }

    def _conditional_branch_type(
        self, arm: ast.AST, scope: Node, local_types: dict[str, dict[str, Any]]
    ) -> dict[str, Any] | None:
        name = arm.id if isinstance(arm, ast.Name) else None
        if isinstance(arm, ast.Call) and isinstance(arm.func, ast.Name):
            name = arm.func.id
        if name is None:
            return None
        found = self.lexical.lookup(scope, name)
        if not found or not self._stable_conditional_binding(found):
            return None
        definition: dict[str, Any] = found[1][0]
        if isinstance(arm, ast.Name):
            if found[0].id != scope.id or definition.get("kind") != "parameter":
                return None
            source = local_types.get(name)
            default = self.parameter_defaults.get(scope.start_line, {}).get(name)
            if default is not None:
                if default != "None" or source is None:
                    return None
                # A None default is runtime evidence even when the annotation
                # omitted Optional. Only an explicit identity guard removes it.
                return {
                    "origin": "Union",
                    "type_args": [
                        source,
                        {"type_id": "builtin:None", "origin": "None"},
                    ],
                }
            return source
        if definition.get("kind") not in {"class_definition", "import_alias"}:
            return None
        source = self._resolve_call(arm, local_types, scope_node=scope)
        if source is None:
            return None
        expected = definition.get("target")
        if definition.get("kind") == "import_alias":
            qualified = str(definition.get("target_qualname", ""))
            expected = (
                exported_class(qualified, self.export_nodes) or f"class:{qualified}"
            )
        return source if source.get("type_id") == expected else None

    def _conditional_class_candidates(self, source: dict[str, Any]) -> set[str]:
        origin = source.get("origin")
        if origin in {"Union", "Optional"}:
            candidates: set[str] = {"builtin:None"} if origin == "Optional" else set()
            for arg in source.get("type_args", []):
                candidates.update(self._conditional_class_candidates(arg))
            return candidates or {"unknown"}
        if origin is not None and origin != "None":
            return {"unknown"}
        type_id = str(source.get("type_id") or "unknown")
        if type_id.startswith("class:"):
            # The legacy annotation summary can select a unique short class
            # name. A new join requires the actual module/import identity.
            parsed = self.parse_expression(str(source.get("type_expression") or ""))
            name = self._name(parsed) if parsed is not None else None
            module = self.lexical.modules.get(self.file_record.path)
            found = (
                self.lexical.lookup(module, name.split(".")[0])
                if module and name
                else None
            )
            if not name or not found or not self._stable_conditional_binding(found):
                return {"unknown"}
            definition: dict[str, Any] = found[1][0]
            if definition.get("kind") not in {"class_definition", "import_alias"} or (
                (
                    exported_class(
                        self._resolve_alias(name, use_imports=True), self.export_nodes
                    )
                    or f"class:{self._resolve_alias(name, use_imports=True)}"
                )
                != type_id
            ):
                return {"unknown"}
        return {type_id}

    def _stable_conditional_binding(
        self, found: tuple[Node, list[dict[str, Any]], int | None]
    ) -> bool:
        owner, bindings, _ = found
        if not self.lexical.stable(*found):
            return False
        binding: dict[str, Any] = bindings[0]
        return owner.kind != "module" or any(
            (stmt.lineno, stmt.col_offset)
            == (binding.get("line"), binding.get("column"))
            for stmt in self.tree.body
        )

    @staticmethod
    def _excludes_none(test: ast.AST, name: str, truth: bool) -> bool:
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            return False
        left, right = test.left, test.comparators[0]
        if isinstance(left, ast.Constant) and left.value is None:
            left, right = right, left
        return (
            isinstance(left, ast.Name)
            and left.id == name
            and isinstance(right, ast.Constant)
            and right.value is None
            and (
                (truth and isinstance(test.ops[0], ast.IsNot))
                or (not truth and isinstance(test.ops[0], ast.Is))
            )
        )

    @staticmethod
    def _parameter_defaults(tree: ast.Module) -> dict[int, dict[str, str]]:
        result = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = [*node.args.posonlyargs, *node.args.args]
            defaults = {
                arg.arg: ast.unparse(value)
                for arg, value in zip(
                    args[len(args) - len(node.args.defaults) :], node.args.defaults
                )
            }
            defaults.update(
                {
                    arg.arg: ast.unparse(value)
                    for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults)
                    if value is not None
                }
            )
            result[node.lineno] = defaults
        return result

    @staticmethod
    def _subscript_value_type_source(source: dict[str, Any]) -> dict[str, Any] | None:
        wrapped = union_alternatives(source) is not None
        source = single_value_type(source)
        type_id = source.get("type_id")
        type_args = source.get("type_args")
        if not isinstance(type_args, list) or not type_args:
            return None
        if type_id == "builtin:dict" and len(type_args) >= 2:
            item = type_args[1]
        elif type_id == "builtin:tuple":
            item = tuple_element_type(source)
        elif type_id == "builtin:list":
            item = type_args[0]
        else:
            return None
        if not isinstance(item, dict):
            return None
        propagated = dict(item)
        propagated["strategy"] = "subscript_value"
        if wrapped or source.get("typed_value_evidence"):
            propagated["typed_value_evidence"] = True
        propagated["source_expression"] = str(source.get("source_expression") or "")
        if isinstance(source.get("type_ref_id"), str):
            propagated["type_ref_id"] = source["type_ref_id"]
        return propagated

    def _literal_value_type(self, node: ast.AST) -> _ResolvedType | None:
        if isinstance(node, ast.GeneratorExp):
            return _ResolvedType(
                expression=GENERATOR_TYPE_ID.removeprefix("extsym:"),
                type_id=GENERATOR_TYPE_ID,
                symbol_id=GENERATOR_TYPE_ID,
                status="resolved",
            )
        type_name = literal_type_name(node)
        if type_name is None:
            return None
        return self._resolve_named_type(type_name, use_imports=False)

    def looks_custom_type(self, expression: str) -> bool:
        parsed = self.parse_expression(expression)
        if parsed is None:
            return False
        name = self._primary_type_name(parsed)
        if not name:
            return False
        short = name.rsplit(".", 1)[-1]
        return short not in _BUILTIN_TYPES and short not in _TYPING_ORIGINS

    def callable_return_type(self, node: Node) -> _ResolvedType | None:
        """One return rule for type records and inferred factory bindings.

        The body's generator shape determines its runtime object. A compatible
        protocol annotation can still describe yielded elements, never replace
        that object with an annotated concrete class.
        """
        returns = node.properties.get("returns")
        generated = _generator_return_type(node, self._nodes_by_path.get(node.path, []))
        if node.properties.get("generator"):
            if generated is None:
                return None
            if isinstance(returns, str) and returns:
                annotated = self.resolve_annotation(
                    returns, use_imports=node.path == self.file_record.path
                )
                if annotated.origin in {
                    "Generator",
                    "Iterator",
                    "Iterable",
                    "AsyncGenerator",
                    "AsyncIterator",
                    "AsyncIterable",
                } and not (annotated.type_id or "").startswith("class:"):
                    generated = replace(
                        generated,
                        origin=annotated.origin,
                        type_args=annotated.type_args,
                    )
            return generated
        if not isinstance(returns, str) or not returns:
            return None
        if node.id not in self._return_decorator_cache:
            self._return_decorator_cache[node.id] = (
                self._annotation_decorators_preserve_return(node)
            )
        if not self._return_decorator_cache[node.id]:
            return None
        return self.resolve_annotation(
            returns, use_imports=node.path == self.file_record.path
        )

    def _annotation_decorators_preserve_return(self, node: Node) -> bool:
        """Small allowlist with identity evidence, not decorator suffix guesses.

        Builtin descriptors/property and functools caches are proved from direct
        member imports or a single attribute of a stable stdlib module import.
        Dynamic aliases/configuration and arbitrary identity decorators remain
        unknown. Reflective or cross-module monkeypatching is not modeled.
        """
        decorators = node.properties.get("decorators", [])
        scope_nodes = self._nodes_by_path.get(node.path, [])
        if not decorators or _generator_decorators_preserve_return(node, scope_nodes):
            return True
        if node.path not in self._decorator_scopes:
            self._decorator_scopes[node.path] = LexicalScopes(scope_nodes, {})
        lexical = self._decorator_scopes[node.path]
        owners = lexical.chain(node)[1:]
        if node.kind == "method" and node.qualname:
            parent = lexical.nodes.get(class_id(node.qualname.rsplit(".", 1)[0]))
            if parent is None:
                return False
            owners = lexical.chain(parent)
        for decorator in decorators:
            if decorator in {"staticmethod", "classmethod", "property"}:
                if decorator == "property" and len(decorators) != 1:
                    return False
                if not _generator_decorators_preserve_return(
                    node, scope_nodes, decorators=[decorator], allow_property=True
                ):
                    return False
                continue
            expression = self.parse_expression(str(decorator))
            callee = expression.func if isinstance(expression, ast.Call) else expression
            name = (
                callee.id
                if isinstance(callee, ast.Name)
                else (
                    callee.value.id
                    if isinstance(callee, ast.Attribute)
                    and isinstance(callee.value, ast.Name)
                    else None
                )
            )
            if not owners or name is None:
                return False
            if any("*" in lexical.bindings.get(owner.id, {}) for owner in owners):
                return False
            found = lexical.lookup(owners[0], name)
            if not found or not lexical.stable(*found):
                return False
            binding = found[1][0]
            target = import_binding_target(binding)
            if isinstance(callee, ast.Attribute):
                if (
                    target != "functools"
                    or binding.get("value") != "functools"
                    or "functools" in self.module_names
                    or (node.path, name) in lexical.global_writes
                    or not _generator_descriptor_module_unchanged(
                        node, scope_nodes, {name}, attributes={callee.attr}
                    )
                ):
                    return False
                target = f"functools.{callee.attr}"
            if (
                binding.get("kind") != "import_alias"
                or (binding.get("line") or 0) >= (node.start_line or 0)
                or binding.get("may_not_run")
                or target
                not in {
                    "functools.cache",
                    "functools.lru_cache",
                    "functools.cached_property",
                }
                or (
                    target == "functools.cached_property"
                    and (node.kind != "method" or len(decorators) != 1)
                )
            ):
                return False
            if isinstance(expression, ast.Call):
                # lru_cache(callable) would cache a different function, then
                # invoke it as the decorator; only literal configuration is safe.
                if (
                    target != "functools.lru_cache"
                    or len(expression.args) > 1
                    or any(
                        k.arg not in {"maxsize", "typed"} for k in expression.keywords
                    )
                    or any(
                        not isinstance(a, ast.Constant)
                        or not isinstance(a.value, (int, bool, type(None)))
                        for a in [
                            *expression.args,
                            *(k.value for k in expression.keywords),
                        ]
                    )
                ):
                    return False
        return True

    def _build_return_type_maps(self) -> None:
        for node in self.nodes:
            if node.kind not in {"function", "method"} or not node.qualname:
                continue
            returns = node.properties.get("returns")
            if not node.properties.get("generator") and not (
                isinstance(returns, str) and returns
            ):
                continue
            resolved = self.callable_return_type(node)
            if resolved is None:
                continue
            if (
                not resolved.type_id
                and union_alternatives(
                    self._value_type_record(
                        resolved,
                        strategy="return_annotation",
                        source_expression=returns,
                    )
                )
                is None
            ):
                continue
            if node.kind == "function":
                # A nested factory is not visible to unrelated lexical scopes.
                if not node.properties.get("lexical_parent"):
                    self.return_types_by_function_name.setdefault(node.name, []).append(
                        resolved
                    )
                self.return_types_by_function_qualname[node.qualname] = resolved
            elif node.kind == "method":
                class_qualname = node.qualname.rsplit(".", 1)[0]
                self.return_types_by_method[(class_qualname, node.name)] = resolved
                if self._is_property_method(node):
                    self.property_return_types_by_method[
                        (class_qualname, node.name)
                    ] = resolved

    def typing_only_attribute(
        self, scope: Node, name: str, local_types: dict[str, dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Join only the constructor's explicit optional-injection pattern.

        Recovering a typing-only annotation must not make a mixed or later
        overwritten instance attribute look like that parameter's class.
        """
        inputs = [
            b
            for b in scope.properties.get("bindings", [])
            if b.get("name") == name
            and str(b.get("value")) in local_types
            and any(
                member.get("typing_only_annotation")
                for member in (union_alternatives(local_types[str(b["value"])]) or [])
            )
        ]
        if not inputs:
            return None
        unknown = unknown_union_result(name)
        if scope.kind != "method" or scope.name != "__init__" or len(inputs) != 1:
            return unknown
        receiver = self.lexical.lookup(scope, "self")
        if (
            not receiver
            or not self.lexical.stable(*receiver)
            or receiver[1][0].get("kind") != "parameter"
        ):
            return unknown
        parameter = str(inputs[0]["value"])
        source = local_types[parameter]
        if self.parameter_defaults.get(scope.start_line, {}).get(parameter) not in {
            None,
            "None",
        }:
            return unknown
        expected = source.get("type_id")
        found = self.lexical.lookup(scope, parameter)
        if (
            not found
            or not self.lexical.stable(*found)
            or found[1][0].get("kind") != "parameter"
        ):
            return unknown
        function = next(
            (
                n
                for n in ast.walk(self.tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.lineno == scope.start_line
            ),
            None,
        )
        owner = next(
            (
                n
                for n in ast.walk(self.tree)
                if isinstance(n, ast.ClassDef) and function in n.body
            ),
            None,
        )
        if owner is None or function is None:
            return unknown
        writes = [
            n
            for n in ast.walk(owner)
            if isinstance(n, ast.Attribute)
            and isinstance(n.ctx, (ast.Store, ast.Del))
            and self._unparse(n) == name
        ]
        if len(writes) != 2:
            return unknown
        for branch in function.body:
            if not isinstance(branch, ast.If) or not branch.body or not branch.orelse:
                continue
            arms = [branch.body[-1], branch.orelse[-1]]
            if not all(
                isinstance(a, ast.Assign)
                and len(a.targets) == 1
                and a.targets[0] in writes
                for a in arms
            ):
                continue
            for truth, arm, fallback in (
                (True, arms[0], arms[1]),
                (False, arms[1], arms[0]),
            ):
                if not (
                    isinstance(arm.value, ast.Name)
                    and arm.value.id == parameter
                    and self._excludes_none(branch.test, parameter, truth)
                ):
                    continue
                if not isinstance(fallback.value, ast.Call):
                    return unknown
                constructor = self._name(fallback.value.func)
                root = constructor.split(".")[0] if constructor else ""
                imported = self.lexical.lookup(scope, root)
                if not imported or not self._stable_conditional_binding(imported):
                    return unknown
                definition = imported[1][0]
                if (
                    imported[0].id == scope.id
                    and definition.get("line", 0) >= branch.lineno
                ):
                    return unknown
                if definition.get("kind") == "import_alias":
                    qualified = (
                        str(definition.get("target_qualname", ""))
                        + constructor[len(root) :]
                    )
                    actual = (
                        exported_class(qualified, self.export_nodes)
                        or f"class:{qualified}"
                    )
                elif (
                    definition.get("kind") == "class_definition" and constructor == root
                ):
                    actual = definition.get("target")
                else:
                    return unknown
                if actual == expected:
                    return {
                        "type_id": expected,
                        "symbol_id": expected,
                        "type_expression": str(expected).removeprefix("class:"),
                        "strategy": "typing_only_injection_join",
                    }
        return unknown

    def register_class_instance_attr(
        self,
        scope_node: Node,
        name: str,
        type_ref: dict[str, Any],
    ) -> None:
        if not isinstance(type_ref.get("type_id"), str):
            return
        class_qualname: str | None = None
        attr_name: str | None = None
        if scope_node.kind == "class" and scope_node.qualname:
            if "." in name or name == "return":
                return
            class_qualname = scope_node.qualname
            attr_name = name
        elif (
            scope_node.kind == "method"
            and scope_node.qualname
            and name.startswith("self.")
        ):
            class_qualname = scope_node.qualname.rsplit(".", 1)[0]
            attr_name = name.removeprefix("self.")
        if not class_qualname or not attr_name:
            return
        self.class_instance_attrs.setdefault(class_qualname, {}).setdefault(
            attr_name,
            type_ref,
        )

    def context_manager_enter_source(
        self,
        source: dict[str, Any],
        *,
        is_async: bool,
    ) -> dict[str, Any] | None:
        type_id = source.get("type_id")
        if not isinstance(type_id, str) or not type_id.startswith("class:"):
            return None
        method_name = "__aenter__" if is_async else "__enter__"
        class_qualname = type_id.removeprefix("class:")
        resolved = self.return_types_by_method.get((class_qualname, method_name))
        if resolved is None:
            return None
        enter_source = self._value_type_record(
            resolved,
            strategy=(
                "async_context_manager_enter" if is_async else "context_manager_enter"
            ),
            source_expression=str(
                source.get("source_expression") or source.get("type_expression") or ""
            ),
        )
        if isinstance(source.get("type_ref_id"), str):
            enter_source["type_ref_id"] = source["type_ref_id"]
        return enter_source

    def _resolve_call(
        self,
        node: ast.Call,
        local_types: dict[str, dict[str, Any]],
        *,
        scope_node: Node | None = None,
    ) -> dict[str, Any] | None:
        cast_type = self._resolve_cast_call(node)
        if cast_type is not None:
            return self._value_type_record(
                cast_type,
                strategy="cast",
                source_expression=self._unparse(node),
            )

        func_name = self._name(node.func)
        if func_name:
            constructed = self._resolve_named_type(func_name, use_imports=True)
            if scope_node is not None and self._binding_decides(scope_node, func_name):
                root = self.lexical.root_name(func_name)
                found = self.lexical.lookup(scope_node, root) if root else None
                if found is not None:
                    binding = found[1][0]
                    kind = binding.get("kind")
                    if (
                        not self.lexical.stable(*found)
                        or kind not in {"class_definition", "import_alias"}
                        or (
                            kind == "class_definition"
                            and binding.get("target") != constructed.type_id
                        )
                    ):
                        constructed = _ResolvedType(expression=func_name)
            if (
                constructed.type_id == "extsym:tempfile.TemporaryDirectory"
                and scope_node is not None
            ):
                root = self.lexical.root_name(func_name)
                found = self.lexical.lookup(scope_node, root) if root else None
                if (
                    not found
                    or not self._stable_conditional_binding(found)
                    or found[1][0].get("kind") != "import_alias"
                ):
                    constructed = _ResolvedType(expression=func_name)
            if constructed.type_id and (
                constructed.type_id.startswith("class:")
                or constructed.type_id.startswith("builtin:")
                or constructed.type_id in _EXTERNAL_CONSTRUCTOR_TYPES
            ):
                return self._value_type_record(
                    constructed,
                    strategy="constructor",
                    source_expression=self._unparse(node),
                )
            documented = self._external_function_return_type(
                node, func_name, scope_node
            )
            if documented is not None:
                return self._value_type_record(
                    documented,
                    strategy="external_function_return",
                    source_expression=self._unparse(node),
                )

        if isinstance(node.func, ast.Attribute):
            receiver = (
                self.resolve_scoped_value(node.func.value, scope_node, local_types)
                if scope_node is not None
                else self.resolve_value(node.func.value, local_types)
            )
            if receiver is None:
                receiver = self._class_reference_value(node.func.value, scope_node)
            if (
                receiver is None
                and scope_node is not None
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"self", "cls"}
                and self._own_receiver(scope_node, node.func.value.id)
            ):
                # self.factory() returns what the class's factory is annotated
                # to return, as the call analyzer reads the same call.
                class_qualname = self._class_qualname_for_scope(scope_node)
                if class_qualname:
                    receiver = {"type_id": f"class:{class_qualname}"}
            if receiver is not None:
                if union_alternatives(receiver) is not None and not receiver.get(
                    "type_id"
                ):
                    return unknown_union_result(self._unparse(node))
                type_id = receiver.get("type_id")
                builtin_return = self._builtin_method_return_type(
                    node,
                    receiver,
                    local_types,
                    scope_node=scope_node,
                )
                if builtin_return is not None:
                    return self._value_type_record(
                        builtin_return,
                        strategy="builtin_method_return",
                        source_expression=self._unparse(node),
                    )
                external_return = self._external_method_return_type(
                    receiver, node.func.attr
                )
                if external_return is not None:
                    return self._value_type_record(
                        external_return,
                        strategy="external_method_return",
                        source_expression=self._unparse(node),
                    )
                if (
                    receiver.get("strategy") == "class_reference"
                    and node.func.attr in _PYDANTIC_INSTANCE_CLASSMETHOD_STRATEGIES
                    and isinstance(type_id, str)
                    and type_id.startswith("class:")
                ):
                    class_qualname = type_id.removeprefix("class:")
                    if self._is_pydantic_model_class(class_qualname):
                        return self._value_type_record(
                            _ResolvedType(
                                expression=class_qualname.rsplit(".", 1)[-1],
                                type_id=type_id,
                                symbol_id=type_id,
                                status="resolved",
                            ),
                            strategy=_PYDANTIC_INSTANCE_CLASSMETHOD_STRATEGIES[
                                node.func.attr
                            ],
                            source_expression=self._unparse(node),
                        )
                if isinstance(type_id, str) and type_id.startswith("class:"):
                    class_qualname = type_id.removeprefix("class:")
                    resolved = self.return_types_by_method.get(
                        (class_qualname, node.func.attr)
                    )
                    if resolved is not None:
                        return self._value_type_record(
                            resolved,
                            strategy="provider_return_annotation",
                            source_expression=self._unparse(node),
                        )
            return None

        if func_name:
            resolved = self._function_return_type(func_name, scope_node)
            if resolved is not None:
                return self._value_type_record(
                    resolved,
                    strategy="factory_return_annotation",
                    source_expression=self._unparse(node),
                )
        return None

    def _external_function_return_type(
        self,
        node: ast.Call,
        func_name: str,
        scope_node: Node | None,
    ) -> _ResolvedType | None:
        """The documented return of an imported standard library function, as
        the call analyzer reads the same call."""

        if not self._is_external_alias(func_name):
            return None
        if scope_node is not None:
            # The name must still be the import: a parameter or an assignment
            # of the same name, or an import under a condition, gives no type.
            root = self.lexical.root_name(func_name)
            found = self.lexical.lookup(scope_node, root) if root else None
            if (
                found is None
                or not self.lexical.stable(*found)
                or found[1][0].get("kind") != "import_alias"
            ):
                return None
        returned = function_return_type(
            self._resolve_alias(func_name, use_imports=True), node
        )
        if returned is None:
            return None
        return self._resolved_type_from_record(returned)

    def _external_method_return_type(
        self,
        receiver: dict[str, Any],
        method_name: str,
    ) -> _ResolvedType | None:
        """Resolve a documented method on a known external or builtin type."""

        type_id = receiver.get("type_id")
        if isinstance(type_id, str) and type_id.startswith("builtin:"):
            owner = type_id
        else:
            qualname = self._external_receiver_qualname(receiver)
            if qualname is None:
                return None
            owner = f"extsym:{qualname}"
        returned = method_return_type(owner, method_name)
        if returned is None:
            return None
        resolved = self._resolved_type_from_record(returned)
        return _ResolvedType(
            expression=str(returned["type_expression"]).rsplit(".", 1)[-1],
            type_id=resolved.type_id,
            symbol_id=resolved.type_id,
            type_args=resolved.type_args,
            status="resolved",
        )

    def _external_receiver_qualname(self, receiver: dict[str, Any]) -> str | None:
        """Name the external type a receiver is, or inherits from in this file."""

        type_id = receiver.get("type_id")
        if not isinstance(type_id, str):
            return None
        if type_id.startswith("extsym:"):
            return type_id.removeprefix("extsym:")
        if type_id.startswith("class:"):
            return self._external_base_qualname(type_id.removeprefix("class:"))
        return None

    def _external_base_qualname(self, class_qualname: str) -> str | None:
        node = self.classes_by_id.get(class_id(class_qualname))
        if node is None or node.path != self.file_record.path:
            return None
        bases = node.properties.get("bases", [])
        if not isinstance(bases, list):
            return None
        for base in bases:
            if not isinstance(base, str):
                continue
            simple_base = base.strip("\"'").split("[", 1)[0].strip()
            if not simple_base:
                continue
            if simple_base in EXTERNAL_METHOD_RETURN_OWNERS:
                return simple_base
            resolved = self._resolve_alias(simple_base, use_imports=True)
            if resolved in EXTERNAL_METHOD_RETURN_OWNERS:
                return resolved
        return None

    def _is_pydantic_model_class(self, class_qualname: str) -> bool:
        node = self.classes_by_id.get(class_id(class_qualname))
        if node is None or node.path != self.file_record.path:
            return False
        bases = node.properties.get("bases", [])
        if not isinstance(bases, list):
            return False
        for base in bases:
            if not isinstance(base, str):
                continue
            simple_base = base.strip("\"'").split("[", 1)[0].strip()
            if not simple_base:
                continue
            resolved = self._resolve_alias(simple_base, use_imports=True)
            if simple_base in _PYDANTIC_MODEL_BASES:
                return True
            if resolved in _PYDANTIC_MODEL_BASES:
                return True
        return False

    def _resolve_cast_call(self, node: ast.Call) -> _ResolvedType | None:
        """Resolve ``typing.cast(T, value)`` as an explicit static type source."""
        if len(node.args) < 2 or not self._is_typing_cast(node.func):
            return None
        expression = self._unparse(node.args[0])
        resolved = self._resolve_type_node(
            node.args[0],
            expression,
            use_imports=True,
        )
        if not resolved.type_id:
            return None
        return resolved

    def _is_typing_cast(self, func: ast.AST) -> bool:
        name = self._name(func)
        if not name:
            return False
        return self._resolve_alias(name, use_imports=True) == "typing.cast"

    def _builtin_method_return_type(
        self,
        node: ast.Call,
        receiver: dict[str, Any],
        local_types: dict[str, dict[str, Any]],
        *,
        scope_node: Node | None,
    ) -> _ResolvedType | None:
        if not isinstance(node.func, ast.Attribute):
            return None
        default = mapping_default(node)
        default_source = None
        if default is not None:
            default_source = (
                self.resolve_scoped_value(default, scope_node, local_types)
                if scope_node is not None
                else self.resolve_value(default, local_types)
            )
        value = mapping_value_type(receiver, node.func.attr, default_source)
        return self._resolved_type_from_record(value) if value is not None else None

    @staticmethod
    def _resolved_type_from_record(record: dict[str, Any]) -> _ResolvedType:
        return _ResolvedType(
            expression=str(record.get("type_expression") or record.get("type_id")),
            type_id=(
                record.get("type_id")
                if isinstance(record.get("type_id"), str)
                else None
            ),
            symbol_id=(
                record.get("symbol_id")
                if isinstance(record.get("symbol_id"), str)
                else (
                    record.get("type_id")
                    if isinstance(record.get("type_id"), str)
                    else None
                )
            ),
            origin=(
                record.get("origin") if isinstance(record.get("origin"), str) else None
            ),
            type_args=(
                tuple(
                    item
                    for item in record.get("type_args", [])
                    if isinstance(item, dict)
                )
                if isinstance(record.get("type_args"), list)
                else ()
            ),
            status=(
                str(record.get("resolution_status"))
                if isinstance(record.get("resolution_status"), str)
                else "resolved"
            ),
        )

    def _class_reference_value(
        self, node: ast.AST, scope_node: Node | None = None
    ) -> dict[str, Any] | None:
        name = self._name(node)
        if not name:
            return None
        if scope_node is not None and self._binding_decides(scope_node, name):
            root = self.lexical.root_name(name)
            found = self.lexical.lookup(scope_node, root) if root else None
            if found is not None and (
                not self.lexical.stable(*found)
                or found[1][0].get("kind") not in {"class_definition", "import_alias"}
            ):
                return None
        resolved = self._resolve_named_type(name, use_imports=True)
        if not resolved.type_id or not resolved.type_id.startswith("class:"):
            return None
        return self._value_type_record(
            resolved,
            strategy="class_reference",
            source_expression=self._unparse(node),
        )

    def _function_return_type(
        self, name: str, scope_node: Node | None = None
    ) -> _ResolvedType | None:
        if scope_node is not None and self._binding_decides(scope_node, name):
            root = self.lexical.root_name(name)
            found = self.lexical.lookup(scope_node, root) if root else None
            if found is not None:
                if not self.lexical.stable(*found):
                    return None
                binding = found[1][0]
                if binding.get("kind") == "function_definition":
                    return self.return_types_by_function_qualname.get(
                        str(binding.get("target_qualname"))
                    )
                if binding.get("kind") != "import_alias":
                    return None
        resolved_name = self._resolve_alias(name, use_imports=True)
        exact = self.return_types_by_function_qualname.get(resolved_name)
        if exact is not None:
            return exact
        if "." in name:
            return None
        candidates = self.return_types_by_function_name.get(name, [])
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _own_receiver(self, scope_node: Node | None, name: str) -> bool:
        """In a method, whether self or cls is still its own receiver, as the
        call analyzer reads it; other scopes keep the name as before."""

        if scope_node is None or scope_node.kind != "method":
            return True
        return self.lexical.own_receiver(scope_node, name)

    def _binding_decides(self, scope_node: Node, name: str) -> bool:
        """Whether the binding the root of ``name`` has in scope, rather than a
        module-wide alias or a unique name elsewhere, decides what it names: in
        a strict scope, and where the root is a local value."""

        if scope_node.id in self.lexical.strict:
            return True
        root = self.lexical.root_name(name)
        return root is not None and self.lexical.local_value(scope_node, root)

    @staticmethod
    def _class_qualname_for_scope(node: Node) -> str | None:
        if node.kind == "class":
            return node.qualname
        if node.kind == "method" and node.qualname:
            return node.qualname.rsplit(".", 1)[0]
        return None

    def _property_type_ref(
        self,
        class_qualname: str,
        attr_name: str,
        *,
        source_expression: str,
    ) -> dict[str, Any] | None:
        resolved = self.property_return_types_by_method.get((class_qualname, attr_name))
        if resolved is None:
            return None
        return self._value_type_record(
            resolved,
            strategy="return_annotation",
            source_expression=source_expression,
        )

    @staticmethod
    def _is_property_method(node: Node) -> bool:
        decorators = node.properties.get("decorators", [])
        if not isinstance(decorators, list):
            return False
        for decorator in decorators:
            if not isinstance(decorator, str):
                continue
            name = decorator.split("(", 1)[0].strip().rsplit(".", 1)[-1]
            if name in {"property", "cached_property"}:
                return True
        return False

    def _value_type_record(
        self,
        resolved: _ResolvedType,
        *,
        strategy: str,
        source_expression: str,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "strategy": strategy,
            "type_expression": resolved.expression,
            "source_expression": source_expression,
            "resolution_status": resolved.status,
        }
        if resolved.type_id:
            record["type_id"] = resolved.type_id
        if resolved.symbol_id:
            record["symbol_id"] = resolved.symbol_id
        if resolved.origin:
            record["origin"] = resolved.origin
        if resolved.type_args:
            record["type_args"] = list(resolved.type_args)
        return record

    def _resolve_type_node(
        self,
        node: ast.AST,
        expression: str,
        *,
        use_imports: bool,
    ) -> _ResolvedType:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self.resolve_annotation(node.value, use_imports=use_imports)
        if isinstance(node, ast.Constant) and node.value is None:
            return self._resolve_named_type("None", use_imports=use_imports)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            args = [
                self._type_arg(node.left, use_imports=use_imports),
                self._type_arg(node.right, use_imports=use_imports),
            ]
            args = self._validated_union_args(args, use_imports=use_imports)
            primary = self._single_union_arg(args)
            return _ResolvedType(
                expression=expression,
                type_id=primary.get("type_id") if primary else None,
                symbol_id=primary.get("symbol_id") if primary else None,
                origin="Union",
                type_args=tuple(args),
                status="resolved" if primary else "unresolved",
            )
        if isinstance(node, ast.Subscript):
            return self._resolve_subscript(node, expression, use_imports=use_imports)
        name = self._name(node)
        if name:
            return self._resolve_named_type(name, use_imports=use_imports)
        return _ResolvedType(expression=expression)

    def _resolve_subscript(
        self,
        node: ast.Subscript,
        expression: str,
        *,
        use_imports: bool,
    ) -> _ResolvedType:
        origin = self._name(node.value) or self._unparse(node.value)
        resolved_origin = self._resolve_alias(origin, use_imports=use_imports)
        short_origin = (
            resolved_origin
            if resolved_origin.startswith(("typing.", "typing_extensions."))
            else origin
        ).rsplit(".", 1)[-1]
        args = tuple(self._slice_type_args(node.slice, use_imports=use_imports))

        if short_origin in _TRANSPARENT_ORIGINS:
            if short_origin in {"Union", "Optional"}:
                module = self.lexical.modules.get(self.file_record.path)
                found = (
                    self.lexical.lookup(module, origin.split(".")[0])
                    if module
                    else None
                )
                definition: dict[str, Any] = found[1][0] if found else {}
                if (
                    use_imports
                    and found
                    and (
                        not self._stable_conditional_binding(found)
                        or definition.get("kind") != "import_alias"
                        or not resolved_origin.startswith(
                            ("typing.", "typing_extensions.")
                        )
                    )
                ):
                    args = (
                        {
                            "type_expression": expression,
                            "resolution_status": "unresolved",
                        },
                    )
                args = tuple(
                    self._validated_union_args(list(args), use_imports=use_imports)
                )
                primary = self._single_union_arg(list(args))
            else:
                primary = args[0] if args and args[0].get("type_id") else None
            return _ResolvedType(
                expression=expression,
                type_id=primary.get("type_id") if primary else None,
                symbol_id=primary.get("symbol_id") if primary else None,
                origin=short_origin,
                type_args=args,
                status="resolved" if primary else "unresolved",
            )

        origin_type = self._resolve_named_type(origin, use_imports=use_imports)
        return _ResolvedType(
            expression=expression,
            type_id=origin_type.type_id,
            symbol_id=origin_type.symbol_id,
            origin=short_origin,
            type_args=args,
            status="resolved" if origin_type.type_id else "unresolved",
        )

    def _slice_type_args(
        self, node: ast.AST, *, use_imports: bool
    ) -> list[dict[str, Any]]:
        if isinstance(node, ast.Tuple):
            return [self._type_arg(item, use_imports=use_imports) for item in node.elts]
        return [self._type_arg(node, use_imports=use_imports)]

    def _type_arg(self, node: ast.AST, *, use_imports: bool) -> dict[str, Any]:
        if isinstance(node, ast.Constant) and node.value is Ellipsis:
            return {"type_expression": "...", "resolution_status": "unresolved"}
        expression = self._unparse(node)
        resolved = self._resolve_type_node(node, expression, use_imports=use_imports)
        record = {
            "type_expression": resolved.expression,
            "resolution_status": resolved.status,
        }
        if resolved.type_id:
            record["type_id"] = resolved.type_id
        if resolved.symbol_id:
            record["symbol_id"] = resolved.symbol_id
        if resolved.origin:
            record["origin"] = resolved.origin
        if resolved.type_args:
            record["type_args"] = list(resolved.type_args)
        return record

    def _validated_union_args(
        self, args: list[dict[str, Any]], *, use_imports: bool
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for arg in args:
            if (
                use_imports
                and arg.get("origin") is None
                and str(arg.get("type_id", "")).startswith("class:")
                and self._conditional_class_candidates(arg) == {"unknown"}
            ):
                if self._type_checking_annotation(arg):
                    arg = {**arg, "typing_only_annotation": True}
                else:
                    arg = {
                        k: v
                        for k, v in arg.items()
                        if k not in {"type_id", "symbol_id"}
                    }
                    arg["resolution_status"] = "unresolved"
            result.append(arg)
        return result

    def _type_checking_annotation(self, ref: dict[str, Any]) -> bool:
        """Accept an unambiguous typing-only import as annotation evidence.

        This exception is deliberately outside lexical runtime lookup and the
        conditional-value join: a typing-only import does not bind a runtime
        constructor. Only direct imports in a proven top-level typing guard
        qualify; rebinding, alternate arms and nested conditions stay unknown.
        """
        expression = self.parse_expression(str(ref.get("type_expression", "")))
        name = self._name(expression) if expression is not None else None
        module = self.lexical.modules.get(self.file_record.path)
        if not name or module is None:
            return False
        bindings = self.lexical.bindings.get(module.id, {}).get(name.split(".")[0], [])
        if len(bindings) != 1:
            return False
        binding = bindings[0]
        if binding.get("kind") != "type_checking_import_alias":
            return False
        target = self._resolve_alias(name, use_imports=True)
        if (exported_class(target, self.export_nodes) or f"class:{target}") != ref.get(
            "type_id"
        ):
            return False
        for stmt in self.tree.body:
            if not isinstance(stmt, ast.If) or stmt.orelse:
                continue
            guard_name = self._name(stmt.test)
            if (
                not guard_name
                or self._resolve_alias(guard_name, use_imports=True)
                != "typing.TYPE_CHECKING"
            ):
                continue
            root = guard_name.split(".")[0]
            guard = self.lexical.lookup(module, root)
            if not guard or not self._stable_conditional_binding(guard):
                continue
            if any(
                isinstance(item, (ast.Import, ast.ImportFrom))
                and (item.lineno, item.col_offset)
                == (binding.get("line"), binding.get("column"))
                for item in stmt.body
            ):
                return True
        return False

    @staticmethod
    def _single_union_arg(args: list[dict[str, Any]]) -> dict[str, Any] | None:
        members = union_alternatives({"origin": "Union", "type_args": args}) or []
        non_none = [arg for arg in members if arg.get("type_id") != "builtin:None"]
        if not non_none or any(not arg.get("type_id") for arg in non_none):
            return None
        first = non_none[0]
        return (
            first
            if all(type_identity(arg) == type_identity(first) for arg in non_none)
            else None
        )

    def _resolve_named_type(self, name: str, *, use_imports: bool) -> _ResolvedType:
        cleaned = name.strip("\"'")
        short = cleaned.rsplit(".", 1)[-1]
        if short in _NONE_NAMES:
            type_id = "builtin:None"
            return _ResolvedType(
                expression=cleaned,
                type_id=type_id,
                symbol_id=type_id,
                origin="None",
                status="resolved",
            )
        if short in _BUILTIN_TYPES:
            type_id = f"builtin:{short}"
            return _ResolvedType(
                expression=cleaned,
                type_id=type_id,
                symbol_id=type_id,
                origin=short,
                status="resolved",
            )
        if short in _TYPING_ORIGINS or cleaned.startswith("typing."):
            type_id = f"typing:{short}"
            return _ResolvedType(
                expression=cleaned,
                type_id=type_id,
                symbol_id=type_id,
                origin=short,
                status="resolved",
            )

        resolved = self._resolve_alias(cleaned, use_imports=use_imports)
        candidate_id = class_id(resolved)
        if candidate_id not in self.classes_by_id and use_imports:
            candidate_id = exported_class(resolved, self.export_nodes) or candidate_id
        if candidate_id in self.classes_by_id:
            return _ResolvedType(
                expression=cleaned,
                type_id=candidate_id,
                symbol_id=candidate_id,
                status="resolved",
            )

        matches = self.classes_by_name.get(short, [])
        # An explicit import identifies its provider. A missing member must
        # not bind to an unrelated class just because its short name matches.
        explicit_import = use_imports and cleaned.split(".", 1)[0] in self.aliases
        if len(matches) == 1 and not explicit_import:
            match = matches[0]
            return _ResolvedType(
                expression=cleaned,
                type_id=match.id,
                symbol_id=match.id,
                status="resolved",
            )

        if use_imports and self._is_external_alias(cleaned):
            type_id = f"extsym:{resolved}"
            return _ResolvedType(
                expression=cleaned,
                type_id=type_id,
                symbol_id=type_id,
                status="resolved",
            )
        return _ResolvedType(expression=cleaned)

    def _resolve_alias(self, name: str, *, use_imports: bool) -> str:
        if not use_imports:
            return name
        parts = name.split(".")
        if parts[0] in self.aliases:
            return ".".join([self.aliases[parts[0]], *parts[1:]])
        if "." in name:
            return name
        return f"{self.file_record.module}.{name}"

    def _is_external_alias(self, name: str) -> bool:
        parts = name.split(".")
        if parts[0] not in self.aliases:
            return False
        resolved = self.aliases[parts[0]]
        return not any(
            resolved == module or resolved.startswith(f"{module}.")
            for module in self.module_names
        )

    def _import_aliases(self, body: list[ast.stmt]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for stmt in body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    local = alias.asname or alias.name.split(".", 1)[0]
                    aliases[local] = alias.name
            elif isinstance(stmt, ast.ImportFrom):
                base = ImportAnalyzer._resolve_import_from_base(self.file_record, stmt)
                if not base:
                    continue
                for alias in stmt.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    aliases[local] = f"{base}.{alias.name}"
            elif isinstance(stmt, ast.If) and self._is_type_checking_guard(stmt.test):
                aliases.update(self._import_aliases(stmt.body))
        return aliases

    @staticmethod
    def _is_type_checking_guard(test: ast.AST) -> bool:
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING"
        return False

    def _primary_type_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Subscript):
            short_origin = (self._name(node.value) or "").rsplit(".", 1)[-1]
            if short_origin in _TRANSPARENT_ORIGINS:
                for item in self._slice_nodes(node.slice):
                    name = self._primary_type_name(item)
                    if name and name.rsplit(".", 1)[-1] not in _NONE_NAMES:
                        return name
            return self._name(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._primary_type_name(node.left) or self._primary_type_name(
                node.right
            )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parsed = self.parse_expression(node.value)
            return self._primary_type_name(parsed) if parsed is not None else None
        return self._name(node)

    @staticmethod
    def _slice_nodes(node: ast.AST) -> list[ast.AST]:
        return list(node.elts) if isinstance(node, ast.Tuple) else [node]

    def _name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    @staticmethod
    def _unparse(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return node.__class__.__name__
