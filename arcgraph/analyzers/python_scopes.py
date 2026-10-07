"""Lexical callable discovery shared by the Python symbol and binding passes."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field


def nested_functions(
    body: list[ast.stmt],
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield immediate nested callables, including definitions in control blocks.

    A function's own children belong to its scope, not the caller's. Nested
    classes and lambdas are separate boundaries and are not expanded here.
    """
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield stmt
        elif not isinstance(stmt, (ast.ClassDef, ast.Lambda)):
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    yield from nested_functions([child])
                elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                    yield from nested_functions(child.body)


def local_definition_regions(
    body: list[ast.stmt],
) -> dict[tuple[int, int], list[list[int]]]:
    """Positions after each local definition, within its own statement block.

    An if/try/match branch is a separate block. The range never reaches another
    branch or a statement after the enclosing control statement. Nested callable
    and class bodies are separate scopes and are not visited.
    """
    regions = {}
    if not body:
        return regions
    end = [body[-1].end_lineno, body[-1].end_col_offset]
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            regions[(stmt.lineno, stmt.col_offset)] = [
                [stmt.end_lineno, stmt.end_col_offset],
                end,
            ]
        elif not isinstance(stmt, ast.ClassDef):
            for _, value in ast.iter_fields(stmt):
                if not isinstance(value, list):
                    continue
                if value and all(isinstance(child, ast.stmt) for child in value):
                    regions.update(local_definition_regions(value))
                for child in value:
                    if isinstance(child, (ast.ExceptHandler, ast.match_case)):
                        regions.update(local_definition_regions(child.body))
    return regions


def breaks_out(loop: ast.While | ast.For | ast.AsyncFor) -> bool:
    """Whether a break in ``loop``'s body ends that loop, not an inner one."""

    pending: list[ast.AST] = list(loop.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Break):
            return True
        if isinstance(
            node,
            (
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
            ),
        ):
            # A break in an inner loop's else still ends this loop.
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                pending.extend(node.orelse)
            continue
        pending.extend(ast.iter_child_nodes(node))
    return False


def _empty_literal(node: ast.expr) -> bool:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    return isinstance(node, ast.Constant) and node.value in ("", b"")


def _blocks_and_whether_they_may_not_run(
    stmt: ast.stmt,
) -> list[tuple[list[ast.stmt], bool]]:
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        # A loop's else runs unless a break ends the loop, and no break runs
        # where the body never does.
        never_breaks = not breaks_out(stmt) or (
            isinstance(stmt, (ast.For, ast.AsyncFor)) and _empty_literal(stmt.iter)
        )
        return [(stmt.body, True), (stmt.orelse, not never_breaks)]
    if isinstance(stmt, (ast.Try, ast.TryStar)):
        return [
            (stmt.body, True),
            *((handler.body, True) for handler in stmt.handlers),
            (stmt.orelse, True),
            (stmt.finalbody, False),
        ]
    if isinstance(stmt, (ast.If, ast.With, ast.AsyncWith)):
        # A with body may stop where its context manager suppresses an
        # exception.
        return [(stmt.body, True), (getattr(stmt, "orelse", []), True)]
    if isinstance(stmt, ast.Match):
        return [(case.body, True) for case in stmt.cases]
    return []


def _span(first: ast.AST, last: ast.AST) -> list[int]:
    return [
        getattr(first, "lineno", 0),
        getattr(first, "col_offset", 0),
        getattr(last, "end_lineno", None) or getattr(last, "lineno", 0),
        getattr(last, "end_col_offset", None) or 0,
    ]


def _block_span(block: list[ast.stmt]) -> list[int]:
    return _span(block[0], block[-1])


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for element in target.elts for name in _target_names(element)}
    return set()


def _meet(paths: list[set[str] | None]) -> set[str] | None:
    """The names every path that goes on binds; None if no path goes on."""

    going_on = [path for path in paths if path is not None]
    if not going_on:
        return None
    return set.intersection(*going_on)


def _bound_after_block(block: list[ast.stmt], bound: set[str]) -> set[str] | None:
    for stmt in block:
        after = _bound_after(stmt, bound)
        if after is None:
            return None
        bound = after
    return bound


def _bound_after(stmt: ast.stmt, bound: set[str]) -> set[str] | None:
    """The names surely bound once ``stmt`` has run and the next statement
    runs, given ``bound`` before it; None if the next statement never runs
    after it, as after return or raise."""

    if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return None
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return bound | {stmt.name}
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return bound | {
            alias.asname or alias.name.split(".")[0]
            for alias in stmt.names
            if alias.name != "*"
        }
    if isinstance(stmt, ast.Assign):
        return bound | {n for target in stmt.targets for n in _target_names(target)}
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        if isinstance(stmt, ast.AnnAssign) and stmt.value is None:
            return bound
        return bound | _target_names(stmt.target)
    if isinstance(stmt, ast.Delete):
        return bound - {n for target in stmt.targets for n in _target_names(target)}
    if isinstance(stmt, ast.If):
        return _meet(
            [
                _bound_after_block(stmt.body, bound),
                _bound_after_block(stmt.orelse, bound),
            ]
        )
    if isinstance(stmt, (ast.Try, ast.TryStar)):
        # The body and else to their end, or a handler after part of the
        # body; a handler's own name is deleted when it ends.
        paths = [_bound_after_block([*stmt.body, *stmt.orelse], bound)]
        for handler in stmt.handlers:
            after = _bound_after_block(handler.body, bound)
            paths.append(None if after is None else after - {handler.name or ""})
        after_paths = _meet(paths)
        if after_paths is None:
            return None
        return _bound_after_block(stmt.finalbody, after_paths)
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        never_breaks = not breaks_out(stmt) or (
            isinstance(stmt, (ast.For, ast.AsyncFor)) and _empty_literal(stmt.iter)
        )
        # The body may not run; the else runs where no break skips it.
        if not never_breaks:
            return bound
        return _bound_after_block(stmt.orelse, bound)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        # Its body may stop where the context manager suppresses an error.
        return bound | {
            n
            for item in stmt.items
            if item.optional_vars is not None
            for n in _target_names(item.optional_vars)
        }
    if isinstance(stmt, ast.Match):
        last = stmt.cases[-1] if stmt.cases else None
        irrefutable = (
            last is not None
            and last.guard is None
            and isinstance(last.pattern, ast.MatchAs)
            and last.pattern.pattern is None
        )
        if not irrefutable:
            return bound
        return _meet([_bound_after_block(case.body, bound) for case in stmt.cases])
    return bound


@dataclass
class ModuleFlow:
    """Where the bindings of a module body hold.

    ``blocks`` maps a node to the span of the innermost block around it that
    may not run by the module's end: under if, with, match, a loop's body, a
    try's body, handlers or else, or a loop's else that a break may skip. A
    finally runs, and so does a loop's else with no break out of the loop.
    A node not in it runs. ``loop_bodies`` maps a loop to its body, where its
    target is bound. ``settling`` lists the statements that run and surely
    bind names in their blocks, as if and else both binding one, with the
    names, outermost first."""

    blocks: dict[int, list[int]] = field(default_factory=dict)
    loop_bodies: dict[int, list[int]] = field(default_factory=dict)
    settling: list[tuple[list[int], frozenset[str]]] = field(default_factory=list)


def module_flow(body: list[ast.stmt]) -> ModuleFlow:
    flow = ModuleFlow()

    def mark(nodes: list[ast.AST], span: list[int]) -> None:
        for root in nodes:
            for node in ast.walk(root):
                flow.blocks[id(node)] = span

    def visit(statements: list[ast.stmt], span: list[int] | None) -> None:
        for stmt in statements:
            if span is not None:
                mark([stmt], span)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, (ast.For, ast.AsyncFor)) and stmt.body:
                flow.loop_bodies[id(stmt)] = _block_span(stmt.body)
            blocks = _blocks_and_whether_they_may_not_run(stmt)
            if span is None and blocks:
                names = _bound_after(stmt, set()) or set()
                if names:
                    flow.settling.append((_span(stmt, stmt), frozenset(names)))
            # A case's pattern and guard, and a handler's name, bind only
            # where its body runs.
            if isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    if case.body:
                        parts = [case.pattern, *([case.guard] if case.guard else [])]
                        mark(parts, _block_span(case.body))
            if isinstance(stmt, (ast.Try, ast.TryStar)):
                for handler in stmt.handlers:
                    if handler.body:
                        flow.blocks[id(handler)] = _block_span(handler.body)
            for block, uncertain in blocks:
                if block:
                    visit(block, _block_span(block) if uncertain else span)

    visit(body, None)
    return flow
