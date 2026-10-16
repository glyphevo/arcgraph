"""Exact legacy comparison and work eliminated from external import guards."""

import io
import json
from pathlib import Path
from types import SimpleNamespace
import re
import tokenize
import unicodedata
import warnings

import pytest

from arcgraph.semantic import reuse
from arcgraph.semantic.search_paths import pth_roots, python_files
from arcgraph.semantic.snapshot import canonical


def legacy_absolute_import_roots(text):
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


def legacy_guard_external_dependencies(index, environment, typeshed):
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
                blocked.update(legacy_absolute_import_roots(text) & possible)
            except (tokenize.TokenError, IndentationError, SyntaxError):
                blocked.update(possible)
    return {**index, "external_roots": sorted(blocked)}


CASES = [
    "import utils.sub, models as m\n",
    "from utils.sub import models, config\n",
    "from .utils import models\nfrom ..config import app\n",
    "if False:\n import utils\nelse:\n from models import X\n",
    "def f():\n import utils\nclass C:\n from models import X\n",
    "try:\n import utils\nexcept ImportError:\n from models import X\nfinally:\n import app\n",
    "match x:\n case 1:\n  import utils\n",
    "def f():\n yield from utils\nimport models\n",
    "raise error from utils\nimport models\n",
    "import utils; from models import app\n",
    "from utils import (\n models,\n config,\n)\n",
    "# utils models\n__all__ = ['utils']\n__all__.append('models')\n",
    "import utils\rimport models\r",
    "import ｕｔｉｌｓ\n",
    "import café\nfrom α import name\n",
    "# app\n'''unterminated\nutils\n",
    "# utils models\n(\n",
    "import utils\n  import models\n",
    "import utils\n!invalid\nfrom models import x\n",
    "type Value[T] = T\nimport utils\n",
    "f'{x}'\nimport utils\n",
    "\ufeffimport utils\n",
    "import utils\x00\n",
]


def result_or_error(function, text):
    try:
        return function(text)
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        return type(exc)


@pytest.mark.parametrize("text", CASES)
def test_import_roots_match_legacy_including_failed_tokenization(text):
    assert result_or_error(reuse.absolute_import_roots, text) == result_or_error(
        legacy_absolute_import_roots, text
    )


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize(
    "names", [["utils", "models", "app"], ["a", "ab", "a-b", "b"], ["α", "café"], []]
)
@pytest.mark.parametrize(
    "text",
    [
        "import utils\nfrom models import app\n",
        "# xa-b a-b ab\n(\n",
        "import α\n# café\n",
        '# utils models app\n"unterminated\n',
    ],
)
def test_external_guard_matches_legacy(tmp_path, names, text, isolated):
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "provider.py").write_text(text, encoding="utf-8")
    typeshed = tmp_path / "typeshed"
    typeshed.mkdir()
    index = {"modules": {"demo.py": names}, "imports": {"demo.py": []}}
    assert reuse.guard_external_dependencies(
        index, environment, typeshed, isolated=isolated
    ) == legacy_guard_external_dependencies(index, environment, typeshed)


def test_content_change_is_recomputed_without_reuse_cache(tmp_path):
    environment = tmp_path / "environment"
    environment.mkdir()
    provider = environment / "provider.py"
    typeshed = tmp_path / "typeshed"
    typeshed.mkdir()
    index = {"modules": {"demo.py": ["utils", "models"]}, "imports": {"demo.py": []}}
    for text, expected in [
        ("import utils\n", ["utils"]),
        ("import models\n", ["models"]),
        ("# utils models\n(\n", ["models", "utils"]),
    ]:
        provider.write_text(text, encoding="utf-8")
        assert (
            reuse.guard_external_dependencies(index, environment, typeshed)[
                "external_roots"
            ]
            == expected
        )
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["provider.py"]


def test_parse_warnings_do_not_escape_the_import_scanner():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert reuse.absolute_import_roots("value = '\\d'\nimport utils\n") == {"utils"}
    assert caught == []


@pytest.mark.parametrize("isolated", [False, True])
def test_empty_workspace_still_checks_external_file_bytes(tmp_path, isolated):
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "broken.py").write_bytes(b"\xff")
    typeshed = tmp_path / "typeshed"
    typeshed.mkdir()
    index = {"modules": {}, "imports": {}}
    for guard in (
        legacy_guard_external_dependencies,
        reuse.guard_external_dependencies,
    ):
        with pytest.raises(SyntaxError, match="encoding declaration"):
            if guard is reuse.guard_external_dependencies:
                guard(index, environment, typeshed, isolated=isolated)
            else:
                guard(index, environment, typeshed)


@pytest.mark.parametrize("kind", ["text", "files"])
@pytest.mark.parametrize("failure", ["invalid", "digest", "runtime", "process"])
def test_isolated_worker_failure_recomputes_original_scan(
    tmp_path, monkeypatch, failure, kind
):
    records = [["import utils\n", ["utils", "models"]]]
    if kind == "files":
        (tmp_path / "provider.py").write_text("import utils\n", encoding="utf-8")
        empty = tmp_path / "empty"
        empty.mkdir()
        records = {
            "index": {"modules": {"demo.py": ["utils", "models"]}, "imports": {}},
            "environment": str(tmp_path),
            "typeshed": str(empty),
        }
    calls = []
    real_scan = reuse.scan

    def scanner(value):
        calls.append(value)
        return real_scan(value)

    def process(*args, **kwargs):
        if failure == "process":
            raise OSError("worker unavailable")
        result = (
            b"broken"
            if failure == "invalid"
            else json.dumps(
                {
                    "format": "semantic-import-scan/2",
                    "coverage": reuse.input_coverage(records),
                    "input": (
                        reuse.sha(kwargs["input"]) if failure == "runtime" else "wrong"
                    ),
                    "runtime": (
                        ["different"]
                        if failure == "runtime"
                        else [
                            reuse.sys.version,
                            reuse.sys.getfilesystemencoding(),
                            reuse.sys.getfilesystemencodeerrors(),
                        ]
                    ),
                    "roots": ["models"],
                }
            ).encode()
        )
        return SimpleNamespace(stdout=result)

    monkeypatch.setattr(reuse, "scan", scanner)
    monkeypatch.setattr(reuse.subprocess, "run", process)
    assert reuse._isolated_import_scan(records) == {"utils"}
    assert calls == [records]


def test_isolated_worker_uses_the_tool_interpreter_without_site(monkeypatch):
    calls = []
    real_run = reuse.subprocess.run

    def process(args, **kwargs):
        calls.append((args, kwargs))
        return real_run(args, **kwargs)

    def fallback(records):
        raise AssertionError("The real isolated worker must complete without fallback")

    monkeypatch.setattr(reuse.subprocess, "run", process)
    monkeypatch.setattr(reuse, "scan", fallback)
    assert reuse._isolated_import_scan([["import utils\n", ["utils"]]]) == {"utils"}
    args, kwargs = calls[0]
    assert args[:4] == [reuse.sys.executable, "-I", "-S", "-B"]
    assert set(kwargs["env"]) <= {"SystemRoot"}
    assert args[-1] == str(reuse.Path(reuse.__file__).with_name("import_scan.py"))


def test_isolated_guard_uses_callers_relative_directories(tmp_path, monkeypatch):
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "provider.py").write_text("import utils\n", encoding="utf-8")
    (tmp_path / "typeshed").mkdir()
    monkeypatch.chdir(tmp_path)
    index = {"modules": {"demo.py": ["utils"]}, "imports": {}}
    assert reuse.guard_external_dependencies(
        index, Path("environment"), Path("typeshed"), isolated=True
    ) == legacy_guard_external_dependencies(
        index, Path("environment"), Path("typeshed")
    )


@pytest.mark.parametrize(
    "failure",
    ["subset", "empty", "missing_file", "missing_all", "bytes", "path", "size"],
)
def test_isolated_worker_checks_actual_input_coverage(tmp_path, monkeypatch, failure):
    environment = tmp_path / "environment"
    environment.mkdir()
    first = environment / "first.py"
    second = environment / "second.pyi"
    first.write_bytes(b"import utils\n")
    second.write_bytes(b"import models\n")
    typeshed = tmp_path / "typeshed"
    typeshed.mkdir()
    records = {
        "index": {"modules": {"demo.py": ["utils", "models"]}, "imports": {}},
        "environment": str(environment),
        "typeshed": str(typeshed),
    }
    original = {p: p.read_bytes() for p in (first, second)}
    real_run, real_scan = reuse.subprocess.run, reuse.scan
    calls = []

    def scanner(value):
        calls.append(value)
        return real_scan(value)

    def process(args, **kwargs):
        # The worker genuinely runs on a truncated/changed view, then the host
        # sees its original inputs again. The request/runtime envelope is valid.
        if failure in {"missing_file", "missing_all"}:
            second.unlink()
            if failure == "missing_all":
                first.unlink()
        elif failure == "bytes":
            second.write_bytes(b"# no models! \n")
        elif failure == "path":
            second.rename(environment / "renamed.pyi")
        elif failure == "size":
            second.write_bytes(original[second] + b"# extra\n")
        try:
            result = json.loads(real_run(args, **kwargs).stdout)
        finally:
            (environment / "renamed.pyi").unlink(missing_ok=True)
            for path, raw in original.items():
                path.write_bytes(raw)
        if failure in {"subset", "empty"}:
            result["roots"] = ["utils"] if failure == "subset" else []
            result.pop("coverage", None)
        return SimpleNamespace(stdout=json.dumps(result).encode("utf-8"))

    monkeypatch.setattr(reuse.subprocess, "run", process)
    monkeypatch.setattr(reuse, "scan", scanner)
    assert reuse._isolated_import_scan(records) == set(real_scan(records))
    assert calls == [records]


def test_isolated_worker_accepts_complete_coverage_without_host_tokenization(
    tmp_path, monkeypatch
):
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "first.py").write_bytes(b"import utils\n")
    (environment / "second.pyi").write_bytes(b"import models\n")
    (environment / "empty.py").write_bytes(b"")
    typeshed = tmp_path / "typeshed"
    typeshed.mkdir()
    records = {
        "index": {"modules": {"demo.py": ["utils", "models"]}, "imports": {}},
        "environment": str(environment),
        "typeshed": str(typeshed),
    }

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "The host must only enumerate and hash valid worker inputs"
        )

    from arcgraph.semantic import import_scan

    monkeypatch.setattr(reuse, "scan", forbidden)
    monkeypatch.setattr(import_scan, "absolute_import_roots", forbidden)
    monkeypatch.setattr(import_scan, "canonical", forbidden)
    monkeypatch.setattr(
        import_scan, "unicodedata", SimpleNamespace(normalize=forbidden)
    )
    assert reuse._isolated_import_scan(records) == {"utils", "models"}
