"""Checked native-response reuse; graph records always undergo fresh admission."""

import ast
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tokenize

from arcgraph.core.scanner import FileScanner, TYPESCRIPT_FILE_EXTENSIONS
from arcgraph.core.utils import symlink_below
from .contract import Snapshot, digest
from .external_targets import file_path
from .frozen import interpreter_identity
from .import_scan import (
    absolute_import_roots as absolute_import_roots,
    guard,
    input_coverage,
    scan,
)
from .install import INTEGRITY, VERSION, validate_installation
from .journal import JournalReplay, load, replay_sources
from .runtime import _node_identity, capabilities, initialize_params
from .snapshot import sha

FORMAT = "semantic-native-reuse/1"
REQUIRED = {
    "snapshot.json",
    "plan.json",
    "responses.jsonl",
    "transport.json",
    "external-targets.json",
    "source-blobs.jsonl",
    "structure.jsonl",
    "pyright.json",
    "fallback.json",
    "projection.json",
    "bridge.json",
    "mapping.json",
    "reuse.json",
}


class ReuseRejected(ValueError):
    """Discard the entire reused response set and collect a fresh journal."""


def backend_identity():
    """Validate pinned installation bytes and bind Node selection and bytes."""
    package = validate_installation()
    located = shutil.which("node")
    if located is None:
        raise ValueError("Node is missing")
    node = Path(located).resolve(strict=True)
    receipt = _node_identity(node)
    before = interpreter_identity(package, executable=node)
    environment = {"PATH": str(node.parent)}
    if os.name == "nt" and "SystemRoot" in os.environ:
        environment["SystemRoot"] = os.environ["SystemRoot"]
    version = subprocess.run(  # nosec B603
        [str(node), "--version"],
        cwd=package,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    ).stdout.strip()
    if before != interpreter_identity(
        package, executable=node
    ) or receipt != _node_identity(node):
        raise ValueError("Node changed during identity check")
    if int(version.removeprefix("v").split(".")[0]) < 14:
        raise ValueError("Unsupported Node version")
    return {
        "pyright": VERSION,
        "integrity": INTEGRITY,
        "installation": str(package),
        "node": before,
        "node_file": list(receipt),
        "node_version": version,
        "wrapper": {
            p.name: sha(p.read_bytes()) for p in Path(__file__).parent.glob("*.py")
        },
    }


def certificate(snapshot, plan, backend, config, roots, dependencies):
    # Physical staging directories are deliberately absent from this identity.
    config = {
        k: v for k, v in config.items() if k not in {"venvPath", "venv", "extraPaths"}
    }
    return {
        "format": FORMAT,
        "snapshot": snapshot.id,
        "plan": digest(plan),
        "backend": backend,
        "configuration": config,
        "roots": [[r.path, r.module_prefix] for r in roots],
        "dependencies": dependencies,
    }


def checked_generation(store, destination=None):
    """Hash every retained artifact; optionally copy admitted bytes privately."""
    from .build import PROFILE, STORAGE, generation_directory

    directory = generation_directory(store.sqlite_path.parent)
    boundary = store.sqlite_path.parent.parent.parent
    if symlink_below(boundary, directory / "manifest.json"):
        raise ValueError("Unsafe semantic generation")
    raw = (directory / "manifest.json").read_bytes()
    if sha(raw) != (store.metadata.get("evidence_manifest") or {}).get(
        "semantic_generation", {}
    ).get("manifest_sha256"):
        raise ValueError("Semantic manifest differs from the published graph")
    manifest = json.loads(raw)
    artifacts = manifest["artifacts"]
    if (
        not manifest["complete"]
        or manifest["revision"] != store.metadata["index_version"]
        or manifest["projection_profile"] != PROFILE
        or manifest["storage_format"] != STORAGE
        or not REQUIRED.issubset(artifacts)
        or set(artifacts)
        != {p.name for p in directory.iterdir() if p.name != "manifest.json"}
    ):
        raise ValueError("Incomplete semantic generation")
    if destination is not None:
        destination.mkdir()
    for name, checksum in artifacts.items():
        path = directory / name
        if Path(name).name != name or symlink_below(boundary, path):
            raise ValueError("Unsafe semantic artifact")
        value = path.read_bytes()
        if sha(value) != checksum:
            raise ValueError("Changed semantic artifact")
        if destination is not None:
            (destination / name).write_bytes(value)
    if (directory / "manifest.json").read_bytes() != raw:
        raise ValueError("Semantic manifest changed during verification")
    directory = destination or directory
    snapshot = Snapshot.model_validate(load(directory / "snapshot.json"))
    plan = load(directory / "plan.json")
    proof = load(directory / "reuse.json")
    if (
        manifest["snapshot"] != snapshot.id
        or plan["snapshot_id"] != snapshot.id
        or proof["format"] != FORMAT
        or proof["snapshot"] != snapshot.id
        or proof["plan"] != digest(plan)
    ):
        raise ValueError("Semantic snapshot or plan binding changed")
    return directory, snapshot, plan, proof


def unchanged(store, indexer, files):
    from .build import freshness_reason

    try:
        if freshness_reason(store, indexer.repo_root) is not None:
            return False
        _, snapshot, _, proof = checked_generation(store)
        if proof["dependencies"] is None:
            # An unknown dependency closure cannot certify unchanged answers.
            return False
        auxiliary = FileScanner(
            indexer.repo_root,
            indexer.source_roots,
            indexer.ignore_rules,
            file_extensions=(".pyi", ".typed"),
        ).scan()
        paths = {f.path for f in [*files, *auxiliary]}
        paths.update(
            n
            for n in ("pyproject.toml", ".gitignore", "package.json", "tsconfig.json")
            if (indexer.repo_root / n).is_file()
        )
        if paths != {f.path for f in snapshot.files}:
            return False
        return (
            proof["backend"] == backend_identity()
            and proof["roots"]
            == [[r.path, r.module_prefix] for r in indexer.source_roots]
            and snapshot.module_names
            == tuple(
                sorted((f.path, f.module) for f in files if f.path.endswith(".py"))
            )
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        return False


def dependency_index(files, trees, roots, *, sources=None):
    """Every lexical import, including unreachable/type-only/star imports.

    Module prefixes include implicit package initializers. Stub and implementation
    are both providers. Unparsed files or ambiguous module ownership fail closed.
    Base/MRO and inferred return dependencies propagate through these imports;
    native target references supplement this index when loading the old journal.
    """
    modules, imports, owners = {}, {}, {}
    for file in files:
        if not file.path.endswith((".py", ".pyi")):
            continue
        module = (
            file.module.removesuffix(".__init__")
            if file.path.endswith("__init__.pyi")
            else file.module
        )
        aliases = {module} if module else set()
        for root in {".", *(r.path for r in roots)}:
            path = Path(file.path)
            if not path.is_relative_to(root):
                continue
            parts = list(path.relative_to(root).with_suffix("").parts)
            if parts and parts[-1] == "__init__":
                # Explicit imports of __init__ (also under an extraPath) are
                # legal aliases of the package initializer's physical file.
                aliases.add(".".join(parts))
                parts.pop()
            if parts and all(p.isidentifier() for p in parts):
                aliases.add(".".join(parts))
        modules[file.path] = sorted(aliases)
        for alias in aliases:
            owners.setdefault(alias, []).append(file.path)
    if any(
        len({p.removesuffix(".pyi").removesuffix(".py") for p in paths}) != 1
        for paths in owners.values()
    ):
        return None
    for path, aliases in modules.items():
        tree = trees.get(path)
        if tree is None:
            return None
        packages = {
            (
                module
                if Path(path).stem == "__init__"
                and module != "__init__"
                and not module.endswith(".__init__")
                else module.rpartition(".")[0]
            )
            for module in aliases
        }
        # A stub can change the answer in its paired implementation as well as
        # in importers. Both providers of the logical module invalidate together.
        names = set(aliases)
        if "__init__" in owners:
            # A source-root initializer also defines the virtual root package
            # seen by relative imports, including imports without call edges.
            names.add("__init__")
        # Every module is resolved in the context of its containing packages.
        names.update(p for p in packages if p)
        if Path(path).stem == "__init__":
            exports = package_exports(tree, (sources or {}).get(path))
            if exports is None:
                return None
            names.update(
                (package + "." if package else "") + name
                for package in packages
                for name in exports
            )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefixes = {node.module or ""}
                if node.level:
                    prefixes = set()
                    for package in packages:
                        parts = package.split(".") if package else []
                        if not parts and node.level == 1:
                            prefixes.add(node.module or "")
                            continue
                        if node.level <= len(parts):
                            prefixes.add(
                                ".".join(
                                    [
                                        *parts[: len(parts) - node.level + 1],
                                        *([node.module] if node.module else []),
                                    ]
                                )
                            )
                    if not prefixes:
                        return None
                for prefix in prefixes:
                    if prefix:
                        names.add(prefix)
                    names.update(
                        (prefix + "." if prefix else "") + a.name
                        for a in node.names
                        if a.name != "*"
                    )
        imports[path] = sorted(names)
    return {"modules": modules, "imports": imports}


def package_exports(tree, source=None):
    """Conservative exports from literal assignments and binder-supported edits.

    Branches and repeated assignments retain their union. Removal needs source
    tokens: AST constants alone cannot distinguish adjacent string literals,
    which the pinned binder rejects rather than removing the combined name.
    """
    allowed, assignments, visited = set(), {}, set()

    def literal(node):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            return None
        if source is not None:
            try:
                segment = ast.get_source_segment(source, node)
                if segment is None:
                    return None
                tokens = [
                    token
                    for token in tokenize.generate_tokens(io.StringIO(segment).readline)
                    if token.type
                    not in {tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER}
                ]
            except (IndexError, tokenize.TokenError, IndentationError):
                return None
            if len(tokens) != 1 or tokens[0].type != tokenize.STRING:
                return None
        return node.value

    def literals(node, types):
        if not isinstance(node, types):
            return None
        values = [literal(item) for item in node.elts]
        return None if any(value is None for value in values) else set(values)

    for node in ast.walk(tree):
        if (
            isinstance(
                node,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                    ast.ExceptHandler,
                    ast.MatchAs,
                    ast.MatchStar,
                ),
            )
            and node.name == "__all__"
            or isinstance(node, ast.MatchMapping)
            and node.rest == "__all__"
        ):
            return None
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                if len(targets) != 1:
                    return None
                values = literals(node.value, (ast.List, ast.Tuple))
                if values is None:
                    return None
                allowed.add(target)
                assignments[node] = values
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
            (a.asname or a.name.split(".")[0]) == "__all__" for a in node.names
        ):
            return None

    def statements(nodes, names, initialized):
        current = set(names)
        for node in nodes:
            if node in assignments:
                visited.add(node)
                current.update(assignments[node])
                initialized = True
            elif isinstance(node, ast.If):
                left, left_initialized = statements(node.body, current, initialized)
                right, right_initialized = statements(node.orelse, current, initialized)
                if left is None or right is None:
                    return None, False
                current = left | right
                initialized = left_initialized and right_initialized
            elif (
                isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "__all__"
            ):
                values = literals(node.value, ast.List)
                if (
                    not initialized
                    or not isinstance(node.op, ast.Add)
                    or values is None
                ):
                    return None, False
                allowed.add(node.target)
                current.update(values)
            elif (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == "__all__"
            ):
                call = node.value
                method = call.func.attr
                if not initialized or len(call.args) != 1 or call.keywords:
                    return None, False
                if method == "extend":
                    values = literals(call.args[0], ast.List)
                elif method in {"append", "remove"}:
                    value = literal(call.args[0])
                    values = None if value is None else {value}
                else:
                    return None, False
                if values is None or method == "remove" and source is None:
                    return None, False
                allowed.add(call.func.value)
                if method == "remove":
                    # Pyright filters every matching entry, unlike list.remove.
                    current.difference_update(values)
                else:
                    current.update(values)
        return current, initialized

    exports, _ = statements(tree.body, set(), False)
    if exports is None or any(
        isinstance(node, ast.Name) and node.id == "__all__" and node not in allowed
        for node in ast.walk(tree)
    ):
        return None
    # Keep the pre-existing conservative treatment of literal assignments in
    # other suites/scopes; their mutations remain unknown via the name check.
    return exports | {
        value
        for node, values in assignments.items()
        if node not in visited
        for value in values
    }


def guard_external_dependencies(index, environment, typeshed, *, isolated=False):
    """Run the same closed guard on the host or in its isolated tool worker."""
    if index is None or not isolated or environment is None or typeshed is None:
        return guard(index, environment, typeshed)
    request = {
        "index": index,
        "environment": str(environment.absolute()),
        "typeshed": str(typeshed.absolute()),
    }
    return {**index, "external_roots": sorted(_isolated_import_scan(request))}


def _isolated_import_scan(records):
    """Check coverage without host tokenization; no site or persistent cache.

    Coverage binds processed bytes, not the correctness of the returned roots.
    Those rely on the same trusted import_scan.py, bound by backend_identity's
    wrapper digest before/after collection. Same-user edits to ArcGraph itself
    are outside this boundary.
    """
    payload = json.dumps(records).encode("utf-8")
    environment = {}
    if os.name == "nt" and "SystemRoot" in os.environ:
        environment["SystemRoot"] = os.environ["SystemRoot"]
    try:
        # Hash first so full builds can overlap this work with transport startup.
        # The worker must independently cover these same bytes; frozen input
        # identities are still rechecked before publication.
        coverage = input_coverage(records)
        process = subprocess.run(  # nosec B603
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(Path(__file__).with_name("import_scan.py")),
            ],
            input=payload,
            capture_output=True,
            env=environment,
            cwd=Path(__file__).parent,
            timeout=600,
            check=True,
        )
        result = json.loads(process.stdout)
        roots = result["roots"]
        names = (
            {
                m.split(".")[0]
                for values in records["index"]["modules"].values()
                for m in values
            }
            if isinstance(records, dict)
            else {name for _, possible in records for name in possible}
        )
        if (
            set(result) == {"format", "input", "coverage", "roots", "runtime"}
            and result["format"] == "semantic-import-scan/2"
            and result["input"] == sha(payload)
            and result["runtime"]
            == [
                sys.version,
                sys.getfilesystemencoding(),
                sys.getfilesystemencodeerrors(),
            ]
            and isinstance(roots, list)
            and all(isinstance(name, str) for name in roots)
            and roots == sorted(set(roots))
            and set(roots) <= names
            and result["coverage"] == coverage
        ):
            return set(roots)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    return set(scan(records))


def affected_paths(old, new, changed, native):
    if old is None or new is None:
        return None
    modules, reverse = {}, {}
    for index in (old, new):
        for path, aliases in index["modules"].items():
            for module in aliases:
                modules.setdefault(module, set()).add(path)
    blocked = set(old["external_roots"]) | set(new["external_roots"])
    # Special implicit typing/builtin providers affect files without imports.
    if any(
        (
            module in {"builtins", "typing", "typing_extensions"}
            or module.split(".")[0] in blocked
        )
        and paths.intersection(changed)
        for module, paths in modules.items()
    ):
        return None
    for index in (old, new):
        for source, names in index["imports"].items():
            for name in names:
                parts = name.split(".")
                for size in range(1, len(parts) + 1):
                    for target in modules.get(".".join(parts[:size]), ()):
                        reverse.setdefault(target, set()).add(source)
    for source, targets in native.items():
        for target in targets:
            reverse.setdefault(target, set()).add(source)
    seen, pending = set(changed), list(changed)
    while pending:
        for path in reverse.get(pending.pop(), ()):
            if path not in seen:
                seen.add(path)
                pending.append(path)
    return seen


def rebase(value, replacements):
    if isinstance(value, str):
        from .uri_paths import rebase_uri

        for old, new in replacements:
            rebound = rebase_uri(value, old, new)
            if rebound != value:
                return rebound
        return value
    if isinstance(value, list):
        return [rebase(v, replacements) for v in value]
    if isinstance(value, dict):
        return {k: rebase(v, replacements) for k, v in value.items()}
    return value


class Reuse:
    def __init__(self, semantic, plan, proof):
        store = semantic.indexer._semantic_previous
        directory, snapshot, old_plan, old_proof = checked_generation(
            store, semantic.directory / "previous"
        )
        from .artifact_inputs import freshness_reason as artifact_freshness

        if (
            artifact_freshness(
                store.sqlite_path.parent,
                semantic.indexer.repo_root,
                dict(snapshot.environment)["artifact_inputs"],
            )
            is not None
        ):
            raise ValueError("Previous artifact input binding changed")
        if (
            any(old_proof[k] != proof[k] for k in ("backend", "configuration", "roots"))
            or old_proof["search_paths"] != proof["search_paths"]
            or proof["search_paths"] is None
            or snapshot.environment != semantic.snapshot.environment
            or snapshot.target_python != semantic.snapshot.target_python
            or snapshot.platform != semantic.snapshot.platform
        ):
            raise ValueError("Semantic environment or backend changed")
        replay_sources(directory, semantic.directory / "previous-source")
        old_files = {f.path: f.raw_digest for f in snapshot.files}
        new_files = {f.path: f.raw_digest for f in semantic.snapshot.files}
        changed = {
            p
            for p in old_files.keys() | new_files.keys()
            if old_files.get(p) != new_files.get(p)
        }
        if any(
            Path(p).suffix not in {".py", ".pyi", *TYPESCRIPT_FILE_EXTENSIONS}
            for p in changed
        ):
            raise ValueError("Semantic configuration changed")
        # The generated backend configuration includes only Python; frontend
        # changes still get a complete graph rebuild but no Python queries.
        changed = {p for p in changed if p.endswith((".py", ".pyi"))}
        detail = load(directory / "transport.json")
        external = load(directory / "external-targets.json")
        if (
            digest(external) != detail["external_capture"]
            or detail["requests"]["error"]
            or detail["requests"]["timeout"]
        ):
            raise ValueError("Incomplete external capture")
        replay = JournalReplay(directory / "responses.jsonl", snapshot.id)
        # The checked journal's first handshake supplies its physical workspace.
        from .journal import open_artifact

        with open_artifact(directory / "responses.jsonl") as handle:
            next(handle)
            self.root = file_path(json.loads(next(handle))["params"]["rootUri"])
        if self.root is None:
            raise ValueError("Invalid previous workspace")
        self.environment = self.root.parent / "environment"
        self.typeshed = Path(detail["typeshed"])
        unit = capabilities(replay.request("initialize", initialize_params(self.root)))
        self.encoding = unit
        responses, native = {}, {}
        try:
            for definition in old_plan["definitions"]:
                params = {
                    "textDocument": {"uri": (self.root / definition["path"]).as_uri()},
                    "position": definition["positions"][unit],
                }
                items = replay.request("textDocument/prepareCallHierarchy", params)
                rows = [
                    {
                        "method": "textDocument/prepareCallHierarchy",
                        "params": params,
                        "result": items,
                    }
                ]
                for item in items or ():
                    outgoing = replay.request(
                        "callHierarchy/outgoingCalls", {"item": item}
                    )
                    rows.append(
                        {
                            "method": "callHierarchy/outgoingCalls",
                            "params": {"item": item},
                            "result": outgoing,
                        }
                    )
                    for entry in outgoing or ():
                        path = file_path(entry["to"]["uri"])
                        if path is not None and path.is_relative_to(self.root):
                            target = path.relative_to(self.root).as_posix()
                            if target not in old_files:
                                raise ValueError("Uncaptured native dependency")
                            native.setdefault(definition["path"], set()).add(target)
                key = digest(definition)
                if key in responses:
                    raise ValueError("Ambiguous previous definition plan")
                responses[key] = rows
        finally:
            replay.close()
        affected = affected_paths(
            old_proof["dependencies"], proof["dependencies"], changed, native
        )
        if affected is None:
            raise ValueError("Semantic dependency closure unknown")
        self.responses = {
            digest(d): responses[digest(d)]
            for d in plan["definitions"]
            if d["path"] not in affected and digest(d) in responses
        }
        self.external = external
        self.affected = affected

    def rows(self, definition, root, detail):
        rows = self.responses.get(digest(definition))
        if rows is None:
            return None
        return iter(
            rebase(
                rows,
                [
                    (self.root.as_uri(), root.as_uri()),
                    (self.environment.as_uri(), (root.parent / "environment").as_uri()),
                    (self.typeshed.as_uri(), Path(detail["typeshed"]).as_uri()),
                ],
            )
        )

    def verify_capture(self, files):
        expected = {(v["origin"], v["relative"]): v for v in self.external.values()}
        if any(expected.get((v["origin"], v["relative"])) != v for v in files.values()):
            raise ReuseRejected("Reused external capture differs from current bytes")
