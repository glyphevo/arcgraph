"""Fail when tracked Python code does text I/O in the locale's default encoding.

Without an explicit encoding, Python decodes and encodes text with the locale's
code page, which on Windows is often cp1252: a non-ASCII path, source file or
subprocess line then fails or garbles there while passing on UTF-8 hosts. CI
sets PYTHONUTF8=1, so its tests cannot catch this; this static check can.

The check is syntactic. It reports a call when it can see text mode and no
encoding; a call it cannot classify (a dynamic mode, ``**kwargs``) is reported
too and must be fixed or listed in ``ALLOWED`` with its reason.

Tracked Python fixtures executed by tests are listed in ``EXECUTED_FIXTURES``
even when their extension is not .py. Other indexer sample fixtures are data.
New executed fixtures must be added to that list; this check cannot discover
dynamic execution, generated sources or Python embedded in strings/documents.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Indexer sample sources are data, apart from explicitly listed test programs.
EXCLUDED_PREFIXES = ("arcgraph/tests/fixtures/",)
EXECUTED_FIXTURES = ("arcgraph/tests/fixtures/semantic_prototype/replay-stdio.txt",)

# (path, call source with whitespace collapsed) -> why the call is safe.
ALLOWED: dict[tuple[str, str], str] = {}

# Receivers whose ``.open`` is not a text-file open.
NON_TEXT_OPEN_RECEIVERS = frozenset(
    {"os", "tarfile", "zipfile", "gzip", "bz2", "lzma", "webbrowser"}
)
SUBPROCESS_TEXT_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
    }
)
SUBPROCESS_LOCALE_CALLS = frozenset(
    {"subprocess.getoutput", "subprocess.getstatusoutput"}
)
# Positions of mode and encoding; these default to binary mode.
TEMPFILE_CALLS = {
    "tempfile.NamedTemporaryFile": (0, 2),
    "tempfile.TemporaryFile": (0, 2),
    "tempfile.SpooledTemporaryFile": (1, 3),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    call: str


def _imported_names(tree: ast.Module) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _qualified_name(func: ast.expr, imported: dict[str, str]) -> str | None:
    if isinstance(func, ast.Name):
        return imported.get(func.id, func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        module = imported.get(func.value.id, func.value.id)
        return f"{module}.{func.attr}"
    return None


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword
    return None


def _is_constant(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _names_encoding(call: ast.Call, position: int | None) -> bool:
    """Whether the call passes an encoding, by keyword or at ``position``."""

    if any(keyword.arg is None for keyword in call.keywords):
        return False  # **kwargs: cannot tell
    keyword = _keyword(call, "encoding")
    if keyword is not None:
        return not _is_constant(keyword.value, None)
    if position is not None and len(call.args) > position:
        return not _is_constant(call.args[position], None)
    return False


def _mode(call: ast.Call, position: int) -> str | None:
    """The mode string, ``""`` when it is not a literal, ``None`` when absent."""

    keyword = _keyword(call, "mode")
    node = keyword.value if keyword is not None else None
    if node is None and len(call.args) > position:
        node = call.args[position]
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _text_mode_without_encoding(
    call: ast.Call, mode_position: int, encoding_position: int, default_text: bool
) -> bool:
    mode = _mode(call, mode_position)
    if mode is None:
        text = default_text
    else:
        text = mode == "" or "b" not in mode
    return text and not _names_encoding(call, encoding_position)


def _rule(call: ast.Call, imported: dict[str, str]) -> str | None:
    name = _qualified_name(call.func, imported)
    if name in {"open", "io.open"}:
        if _text_mode_without_encoding(call, 1, 3, default_text=True):
            return "open"
        return None
    if name == "os.fdopen":
        if _text_mode_without_encoding(call, 1, 3, default_text=True):
            return "os.fdopen"
        return None
    if name == "io.TextIOWrapper":
        return None if _names_encoding(call, 1) else "io.TextIOWrapper"
    if name in TEMPFILE_CALLS:
        mode_position, encoding_position = TEMPFILE_CALLS[name]
        if _text_mode_without_encoding(
            call, mode_position, encoding_position, default_text=False
        ):
            return "tempfile"
        return None
    if name in SUBPROCESS_LOCALE_CALLS:
        return "subprocess-locale"
    if name in SUBPROCESS_TEXT_CALLS:
        flag = _keyword(call, "text") or _keyword(call, "universal_newlines")
        if flag is None or _is_constant(flag.value, False):
            return None
        return None if _names_encoding(call, None) else "subprocess-text"
    if not isinstance(call.func, ast.Attribute):
        return None
    attribute = call.func.attr
    if attribute == "read_text":
        # pathlib's first positional parameter is the encoding. The filename
        # importlib.metadata's read_text takes there is safe too: it decodes
        # the file as UTF-8 itself.
        return None if _names_encoding(call, 0) else "read_text"
    if attribute == "write_text":
        return None if _names_encoding(call, 1) else "write_text"
    if attribute == "open":
        receiver = call.func.value
        if isinstance(receiver, ast.Name):
            module = imported.get(receiver.id, receiver.id)
            if module in NON_TEXT_OPEN_RECEIVERS:
                return None
        if _text_mode_without_encoding(call, 0, 2, default_text=True):
            return "method-open"
    return None


def scan_source(path: str, source: str) -> list[Finding]:
    tree = ast.parse(source, filename=path)
    imported = _imported_names(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        rule = _rule(node, imported)
        if rule is None:
            continue
        segment = ast.get_source_segment(source, node) or ""
        findings.append(Finding(path, node.lineno, rule, " ".join(segment.split())))
    return sorted(findings, key=lambda finding: (finding.path, finding.line))


def tracked_python_files(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.py", *EXECUTED_FIXTURES],
        capture_output=True,
        check=True,
    )
    paths = completed.stdout.decode("utf-8").split("\0")
    return sorted(
        path
        for path in paths
        if path
        and (path in EXECUTED_FIXTURES or not path.startswith(EXCLUDED_PREFIXES))
    )


def check(
    root: Path, allowed: dict[tuple[str, str], str] | None = None
) -> dict[str, list[dict[str, object]]]:
    allowlist = ALLOWED if allowed is None else allowed
    findings: list[Finding] = []
    used: set[tuple[str, str]] = set()
    for path in tracked_python_files(root):
        source = (root / path).read_text(encoding="utf-8")
        for finding in scan_source(path, source):
            key = (finding.path, finding.call)
            if key in allowlist:
                used.add(key)
                continue
            findings.append(finding)
    stale = [
        {"path": path, "call": call, "reason": reason}
        for (path, call), reason in sorted(allowlist.items())
        if (path, call) not in used
    ]
    return {
        "findings": [asdict(finding) for finding in findings],
        "stale_allowlist": stale,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    result = check(args.root)
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 1 if result["findings"] or result["stale_allowlist"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
