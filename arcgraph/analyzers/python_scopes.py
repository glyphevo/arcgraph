"""Lexical callable discovery shared by the Python symbol and binding passes."""

from __future__ import annotations

import ast
from collections.abc import Iterator


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


def nodes_that_may_not_run(body: list[ast.stmt]) -> set[int]:
    """The ids of the nodes of a module ``body`` in a block that may not run
    by the module's end: under if, with, match, a loop's body, a try's body,
    handlers or else, or a loop's else that a break may skip. A finally runs,
    and so does a loop's else with no break out of the loop."""

    may_not_run: set[int] = set()

    def visit(statements: list[ast.stmt], uncertain: bool) -> None:
        for stmt in statements:
            if uncertain:
                may_not_run.update(id(node) for node in ast.walk(stmt))
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for block, block_uncertain in _blocks_and_whether_they_may_not_run(stmt):
                visit(block, block_uncertain)

    visit(body, False)
    return may_not_run
