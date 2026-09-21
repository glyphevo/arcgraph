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
