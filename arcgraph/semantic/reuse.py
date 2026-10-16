"""Checked native-response reuse; graph records always undergo fresh admission."""

import ast
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tokenize
import unicodedata

from arcgraph.core.scanner import FileScanner, TYPESCRIPT_FILE_EXTENSIONS
from arcgraph.core.utils import symlink_below
from .contract import Snapshot, digest
from .external_targets import file_path
from .frozen import interpreter_identity
from .install import INTEGRITY, VERSION, validate_installation
from .journal import JournalReplay, load, replay_sources
from .runtime import _node_identity, capabilities, initialize_params
from .snapshot import canonical, sha
from .search_paths import pth_roots, python_files

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


def dependency_index(files, trees, roots):
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
            exports = package_exports(tree)
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


def package_exports(tree):
    """Union literal export lists; any other use/binding of __all__ is unknown."""
    allowed, exports = set(), set()
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
                value = node.value
                if not isinstance(value, (ast.List, ast.Tuple)) or any(
                    not isinstance(v, ast.Constant) or not isinstance(v.value, str)
                    for v in value.elts
                ):
                    return None
                allowed.add(target)
                exports.update(v.value for v in value.elts)
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
            (a.asname or a.name.split(".")[0]) == "__all__" for a in node.names
        ):
            return None
    if any(
        isinstance(node, ast.Name) and node.id == "__all__" and node not in allowed
        for node in ast.walk(tree)
    ):
        return None
    return exports


def absolute_import_roots(text):
    """Tokenize all branches, including syntax newer than the host AST grammar."""
    roots, state, from_root = set(), None, None
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type in {
            tokenize.NL,
            tokenize.COMMENT,
            tokenize.INDENT,
            tokenize.DEDENT,
        }:
            continue
        if token.type == tokenize.NEWLINE or token.string == ";":
            state = None
        elif state == "from":
            from_root = token.string if token.type == tokenize.NAME else None
            state = "from-module"
        elif state == "from-module":
            if token.string == "import":
                if from_root is not None:
                    roots.add(from_root)
                state = "from-symbols"
        elif state == "from-symbols":
            continue
        elif state == "import":
            if token.type == tokenize.NAME:
                roots.add(token.string)
                state = "import-tail"
        elif state == "import-tail":
            if token.string == ",":
                state = "import"
        elif token.type == tokenize.NAME and token.string in {"from", "import"}:
            state = token.string
    return roots


def guard_external_dependencies(index, environment, typeshed):
    """Unknown external-to-workspace import paths invalidate the whole run.

    Rather than assert that third-party reexports cannot reach the workspace,
    conservatively block each workspace top-level name mentioned by an external
    absolute import. Relative fragments and imported symbol names are excluded.
    Tokenization failure blocks every possibly referenced name in that file.
    Static .pth directories are content-bound separately; import hooks are never
    executed by the backend's static model.
    """
    if index is None:
        return None
    names = {m.split(".")[0] for values in index["modules"].values() for m in values}
    identifiers = (
        re.compile(
            r"(?<!\w)(" + "|".join(re.escape(n) for n in sorted(names)) + r")(?!\w)"
        )
        if names
        else None
    )
    blocked = set()
    for root in (
        environment,
        typeshed,
        *(path for _, path in pth_roots(environment) if path is not None),
    ):
        for path in python_files(root):
            text = unicodedata.normalize("NFKC", canonical(path.read_bytes()))
            possible = (
                set(identifiers.findall(text)) - blocked
                if identifiers is not None
                else set()
            )
            if not possible:
                continue
            try:
                blocked.update(absolute_import_roots(text) & possible)
            except (tokenize.TokenError, IndentationError, SyntaxError):
                blocked.update(possible)
    return {**index, "external_roots": sorted(blocked)}


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
        for old, new in replacements:
            if value == old or value.startswith(old + "/"):
                return new + value[len(old) :]
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
