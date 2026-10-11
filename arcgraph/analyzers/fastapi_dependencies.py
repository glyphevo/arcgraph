"""Source-only dependency injection contracts evaluated before call resolution.

These are framework binding facts, not the result of calling Depends in ordinary
Python. Function defaults are supported; unannotated class fields, arbitrary
dependency instances, dynamic factories, overloads and unannotated providers
stay unknown. Annotated class fields retain their declared types.
"""

from __future__ import annotations

import ast

from arcgraph.analyzers.calls.lexical import import_binding_target

_DEPENDS = {"fastapi.Depends", "fastapi.params.Depends", "fastapi.Security"}
_YIELD_TYPES = {
    "typing.Generator",
    "typing.Iterator",
    "typing.AsyncGenerator",
    "typing.AsyncIterator",
    "collections.abc.Generator",
    "collections.abc.Iterator",
    "collections.abc.AsyncGenerator",
    "collections.abc.AsyncIterator",
}


def dependency_result(expression, scope, context):
    parsed = context.parse_expression(expression)
    contracts = context.callable_contracts
    if (
        not isinstance(parsed, ast.Call)
        or contracts.name(parsed.func, scope) not in _DEPENDS
    ):
        return None
    provider = (
        parsed.args[0]
        if parsed.args
        else next((k.value for k in parsed.keywords if k.arg == "dependency"), None)
    )
    if not isinstance(provider, (ast.Name, ast.Attribute)):
        return None
    name = ast.unparse(provider)
    root, *tail = name.split(".")
    scopes = contracts.scopes(scope)
    found = scopes.lookup(scope, root)
    if (
        not found
        or not scopes.stable(*found)
        or (found[0].path, root) in scopes.global_writes
    ):
        return None
    binding = found[1][0]
    if binding.get("kind") == "import_alias":
        target = import_binding_target(binding)
    elif binding.get("kind") in {"function_definition", "class_definition"}:
        target = binding.get("target_qualname")
    else:
        return None
    if not isinstance(target, str):
        return None
    qualified = ".".join([target, *tail])
    definitions = [
        n
        for n in context.nodes
        if n.qualname == qualified and n.kind in {"class", "function", "method"}
    ]
    if len(definitions) != 1:
        return None
    definition = definitions[0]
    if definition.kind == "class":
        concrete = qualified
    else:
        if context.callable_return_type(definition) is None:
            return None
        returns = definition.properties.get("returns")
        annotation = (
            context.parse_expression(returns) if isinstance(returns, str) else None
        )
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            annotation = context.parse_expression(annotation.value)
        if annotation is None:
            return None
        owners = contracts.scopes(definition).chain(definition)[1:]
        declaration = owners[0] if owners else definition
        if definition.properties.get("generator"):
            if (
                not isinstance(annotation, ast.Subscript)
                or contracts.name(annotation.value, declaration) not in _YIELD_TYPES
            ):
                return None
            annotation = (
                annotation.slice.elts[0]
                if isinstance(annotation.slice, ast.Tuple)
                else annotation.slice
            )
        concrete = contracts.concrete_return(annotation, definition, declaration)
        if concrete is None:
            return None
    target_id = (
        "builtin:" + concrete
        if concrete
        in {"str", "int", "float", "bool", "bytes", "dict", "list", "tuple", "set"}
        else contracts.exported_class(concrete)
        or (
            "class:" + concrete
            if "class:" + concrete in context.classes_by_id
            else "extsym:" + concrete
        )
    )
    return {
        "type_id": target_id,
        "symbol_id": target_id,
        "type_expression": concrete,
        "strategy": "fastapi_dependency_result",
        "dependency_target": definition.id,
    }
