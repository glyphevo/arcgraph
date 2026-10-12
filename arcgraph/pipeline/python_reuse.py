"""Prove append-only Python changes before reusing a completed generation."""

import ast
import builtins
import hashlib
import json
import re
from pathlib import Path

from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer
from arcgraph.core.structure import StructureView


def appended_private_fragment(indexer, store, files, nodes, edges, changed):
    """Return a reusable fragment only for unobserved, inert private definitions.

    Existing bytes (including positions) must be an exact prefix. Definition
    creation must evaluate no user expression. Names must be absent from all
    old bindings and callsites, and bodies may use only locals and builtins.
    Otherwise the regular dependency closure decides what needs analysis.
    """
    if len(changed) != 1 or len(files) != 1 or not files[0].path.endswith(".py"):
        return None
    file = files[0]
    previous = store.python_source(file.path)
    if previous is None:
        return None
    try:
        raw = Path(file.abs_path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != file.file_hash:
            return None
        source = raw.decode("utf-8-sig")
        if not source.startswith(previous):
            return None
        # Preserve every location, even when the old source has no final newline.
        if not previous.endswith("\n"):
            return None
        parsed, notices = StructureView(
            indexer.repo_root, indexer.frontend._project_environment
        ).read({file.path: Path(file.abs_path)})
        whole = parsed.get(file.path)
        if whole is None:
            return None
        boundary = previous.count("\n")
        # Appending must not complete or alter a previous top-level statement.
        if any(n.lineno <= boundary < n.end_lineno for n in whole.body):
            return None
        tree = ast.Module(
            body=[n for n in whole.body if n.lineno > boundary],
            type_ignores=whole.type_ignores,
        )
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    if not tree.body or any(
        not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in tree.body
    ):
        return None
    names = {n.name for n in tree.body}
    if len(names) != len(tree.body) or any(
        not n.startswith("_") or n.startswith("__") for n in names
    ):
        return None
    for function in tree.body:
        arguments = [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ]
        arguments += [a for a in (function.args.vararg, function.args.kwarg) if a]
        if (
            function.decorator_list
            or function.returns
            or function.args.defaults
            or any(function.args.kw_defaults)
            or any(a.annotation for a in arguments)
        ):
            return None
        if any(
            isinstance(
                n,
                (
                    ast.Import,
                    ast.ImportFrom,
                    ast.Global,
                    ast.Nonlocal,
                    ast.ClassDef,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                ),
            )
            for statement in function.body
            for n in ast.walk(statement)
        ):
            return None
        locals_ = {a.arg for a in arguments} | {
            n.id
            for n in ast.walk(function)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }
        if any(
            isinstance(n, ast.Name)
            and isinstance(n.ctx, ast.Load)
            and n.id not in locals_
            and n.id not in vars(builtins)
            for n in ast.walk(function)
        ):
            return None
    if any(node.name in names for node in nodes):
        return None
    # The reader retains immutable canonical JSON text for all old payloads.
    # Scan it directly rather than regenerating every dictionary representation.
    # Include the JSON spelling of Unicode names and do not require word bounds:
    # escaped whitespace must not hide a recorded reference.
    spellings = {s for n in names for s in (n, json.dumps(n)[1:-1])}
    pattern = re.compile("(?:" + "|".join(re.escape(s) for s in spellings) + ")")
    if not store.encoded_payloads or any(
        pattern.search(payload) for payload in store.encoded_payloads
    ):
        return None
    old_module = next(
        (n for n in nodes if n.kind == "module" and n.path == file.path), None
    )
    if old_module is None:
        return None
    shadowed = {b.get("name") for b in old_module.properties.get("bindings", [])}
    if any(
        isinstance(n, ast.Name)
        and isinstance(n.ctx, ast.Load)
        and n.id in vars(builtins)
        and n.id in shadowed
        for n in ast.walk(tree)
    ):
        return None
    # A fresh helper is analyzed in the same complete call/similarity context,
    # without rerunning existing scopes or framework registration statements.
    fragment = PythonGraphAnalyzer(
        enable_v2_call_resolution=indexer.enable_v2_call_resolution,
        repo_root=indexer.repo_root,
        project_environment=indexer.frontend._project_environment,
    ).analyze(
        files,
        warnings=notices,
        module_names={n.qualname for n in nodes if n.kind == "module"},
        call_context_nodes=nodes,
        incremental_similarity_buckets={
            n.properties["similarity"]["bucket"]
            for n in nodes
            if n.path == file.path
            and isinstance(n.properties.get("similarity"), dict)
            and "bucket" in n.properties["similarity"]
        },
        parsed_files_override={file.path: tree},
    )
    module = next(n for n in fragment.nodes if n.kind == "module")
    properties = dict(old_module.properties)
    properties["line_count"] = file.line_count
    properties["bindings"] = [
        *properties.get("bindings", []),
        *module.properties.get("bindings", []),
    ]
    module.properties = properties
    module.canonical_identity = old_module.canonical_identity
    fresh_ids = {n.id for n in fragment.nodes}
    fragment.nodes.extend(
        n for n in nodes if n.path == file.path and n.id not in fresh_ids
    )
    fragment.edges.extend(
        e
        for e in edges
        if e.kind != "similar_to" and any(v.path == file.path for v in e.evidence)
    )
    return fragment


def closure_needs_global_context(nodes):
    """Framework/type state lacking reusable phase inputs makes closure unknown."""
    return any(n.kind in {"route", "pytest_fixture"} for n in nodes)


def complete_python_generation(files, nodes):
    """Every old Python file must have one unambiguous module summary."""
    python_files = [f for f in files if f.path.endswith(".py")]
    summaries = {n.path for n in nodes if n.kind == "module"}
    return len({f.module for f in python_files}) == len(python_files) and all(
        f.path in summaries for f in python_files
    )
