"""Stdlib-only import scanner, shared by the host and its isolated worker."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import sys
import tokenize


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


# Match JavaScript trim() and /^import\s/, used by pinned pyright. Python's
# default strip()/\s differ for BOM and some control characters.
PTH_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def canonical(raw: bytes) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")


def pth_roots(environment, *, virtual=False):
    """Resolve paths in the frozen layout, without executing import lines.

    For freshness, map environment-contained paths back to the live environment.
    Relative paths escaping it are resolved at the same depth below TMPDIR as a
    SemanticBuild, rather than relative to the original project's environment.
    """
    if environment is None:
        return []
    environment = Path(environment).resolve()
    staging = (
        Path(tempfile.gettempdir()).resolve() / "arcgraph-build-semantic-identity"
        if virtual
        else environment.parent
    )
    frozen = staging / "environment" if virtual else environment
    sites = sorted(environment.glob("lib/python*/site-packages"))
    if (environment / "Lib/site-packages").is_dir():
        sites.append(environment / "Lib/site-packages")
    roots = []
    for site in sites:
        for path in sorted(site.glob("*.pth")):
            raw = path.read_bytes()
            # pyright 1.1.414's getPathsFromPthFiles uses this size bound.
            if not 0 < len(raw) < 65536:
                continue
            for line in raw.decode("utf-8").split("\n"):
                line = line.strip(PTH_WHITESPACE)
                if (
                    not line
                    or line.startswith("#")
                    or line.startswith("import")
                    and len(line) > 6
                    and line[6] in PTH_WHITESPACE
                ):
                    continue
                target = (frozen / site.relative_to(environment) / line).resolve()
                if target.is_relative_to(frozen):
                    relative = target.relative_to(frozen)
                    roots.append(
                        ("environment/" + relative.as_posix(), environment / relative)
                    )
                elif target.is_relative_to(staging / "source"):
                    # These bytes already belong to the captured source snapshot.
                    roots.append(
                        (
                            "source/"
                            + target.relative_to(staging / "source").as_posix(),
                            None,
                        )
                    )
                elif target.is_relative_to(staging):
                    # Non-source staging directories are not stable search inputs.
                    raise ValueError("Unmodelled relative semantic search directory")
                else:
                    roots.append((str(target), target))
    return roots


def python_files(root):
    """Include directory aliases in external import guards, without cycles."""
    pending, seen = [root] if root.is_dir() else [], set()
    while pending:
        directory = pending.pop()
        resolved = directory.resolve(strict=True)
        if resolved in seen:
            continue
        seen.add(resolved)
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda entry: entry.name):
                if entry.is_dir():
                    if entry.name != "__pycache__":
                        pending.append(Path(entry.path))
                elif Path(entry.name).suffix in {".py", ".pyi"}:
                    yield Path(entry.path)


def _matching_words(text, names):
    """Equivalent word matches, without scanning duplicate occurrences in Python."""
    found = set()
    for name in names:
        start = text.find(name)
        while start >= 0:
            end = start + len(name)
            if (
                start == 0 or not (text[start - 1].isalnum() or text[start - 1] == "_")
            ) and (end == len(text) or not (text[end].isalnum() or text[end] == "_")):
                found.add(name)
                break
            start = text.find(name, start + 1)
    return found


def _coverage_row(coverage, row):
    coverage.update(json.dumps(row, separators=(",", ":")).encode("utf-8") + b"\n")


def _file_inputs(environment, typeshed, coverage):
    """Bind each root and each processed path/size/raw digest, in fixed order."""
    roots = (
        environment,
        typeshed,
        *(path for _, path in pth_roots(environment) if path is not None),
    )
    for slot, root in enumerate(roots):
        if coverage is not None:
            _coverage_row(coverage, ["root", slot, str(root)])
        for path in sorted(python_files(root)):
            raw = path.read_bytes()
            if coverage is not None:
                _coverage_row(
                    coverage,
                    [
                        "file",
                        slot,
                        path.relative_to(root).as_posix(),
                        len(raw),
                        hashlib.sha256(raw).hexdigest(),
                    ],
                )
            yield raw


def input_coverage(records):
    """Independently enumerate/hash host inputs; never decode or tokenize Python."""
    coverage = hashlib.sha256()
    if isinstance(records, dict):
        for _ in _file_inputs(
            Path(records["environment"]), Path(records["typeshed"]), coverage
        ):
            pass
    else:
        for text, names in records:
            _coverage_row(coverage, [text, sorted(names)])
    return coverage.hexdigest()


def guard(index, environment, typeshed, *, coverage=None):
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
    word_names = all(re.fullmatch(r"\w+", name) for name in names)
    identifiers = (
        re.compile(
            r"(?<!\w)(" + "|".join(re.escape(n) for n in sorted(names)) + r")(?!\w)"
        )
        if names and not word_names
        else None
    )
    blocked = set()
    for raw in _file_inputs(environment, typeshed, coverage):
        text = unicodedata.normalize("NFKC", canonical(raw))
        possible = (
            _matching_words(text, names - blocked)
            if word_names
            else (
                set(identifiers.findall(text)) - blocked
                if identifiers is not None
                else set()
            )
        )
        if not possible:
            continue
        blocked.update(scan([[text, possible]]))
    return {**index, "external_roots": sorted(blocked)}


def scan(records, *, coverage=None):
    if isinstance(records, dict):
        return guard(
            records["index"],
            Path(records["environment"]),
            Path(records["typeshed"]),
            coverage=coverage,
        )["external_roots"]
    blocked = set()
    for text, names in records:
        if coverage is not None:
            _coverage_row(coverage, [text, sorted(names)])
        possible = set(names) - blocked
        if possible:
            try:
                blocked.update(absolute_import_roots(text) & possible)
            except (tokenize.TokenError, IndentationError, SyntaxError):
                blocked.update(possible)
    return sorted(blocked)


def main():
    raw = sys.stdin.buffer.read()
    coverage = hashlib.sha256()
    roots = scan(json.loads(raw), coverage=coverage)
    result = {
        "format": "semantic-import-scan/2",
        "input": hashlib.sha256(raw).hexdigest(),
        "coverage": coverage.hexdigest(),
        "runtime": [
            sys.version,
            sys.getfilesystemencoding(),
            sys.getfilesystemencodeerrors(),
        ],
        "roots": roots,
    }
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
