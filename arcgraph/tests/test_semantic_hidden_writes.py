"""Explicit static writes differ from ignored runtime patch effects."""

import json
import subprocess
import sys
import textwrap

import pytest

from arcgraph.tests.test_semantic_implementation_conflicts import mark

CASES = [
    (
        "alias-then-attribute",
        {
            "base.py": "class Base:\n def get(self):\n  return 'base'\n",
            "pkg.py": "\n            from base import Base\n            class Child(Base):\n                pass\n            alias = Child\n            def replacement(self):\n                return 'patched'\n            alias.get = replacement\n        ",
        },
        "extsym:pkg.Child.get",
        "extsym:base.Base.get",
    ),
    (
        "setattr-through-alias",
        {
            "base.py": "class Base:\n def get(self):\n  return 'base'\n",
            "pkg.py": "\n            from base import Base\n            class Child(Base):\n                pass\n            alias = Child\n            def replacement(self):\n                return 'patched'\n            setattr(alias, 'get', replacement)\n        ",
        },
        "extsym:pkg.Child.get",
        "extsym:base.Base.get",
    ),
    (
        "called-nested-function",
        {
            "base.py": "class Base:\n def get(self):\n  return 'base'\n",
            "pkg.py": "\n            from base import Base\n            class Child(Base):\n                pass\n            def replacement(self):\n                return 'patched'\n            def install():\n                Child.get = replacement\n            install()\n        ",
        },
        "extsym:pkg.Child.get",
        "extsym:base.Base.get",
    ),
    (
        "called-patch-helper",
        {
            "base.py": "class Base:\n def get(self):\n  return 'base'\n",
            "pkg.py": "\n            from base import Base\n            class Child(Base):\n                pass\n            def replacement(self):\n                return 'patched'\n            def patch(cls):\n                cls.get = replacement\n            patch(Child)\n        ",
        },
        "extsym:pkg.Child.get",
        "extsym:base.Base.get",
    ),
    (
        "nested-function-assigns-through-alias",
        {
            "base.py": "class Base:\n def get(self):\n  return 'base'\n",
            "pkg.py": "\n            from base import Base\n            class Child(Base):\n                pass\n            def replacement(self):\n                return 'patched'\n            def install():\n                alias = Child\n                alias.get = replacement\n            install()\n        ",
        },
        "extsym:pkg.Child.get",
        "extsym:base.Base.get",
    ),
    (
        "nested-class-body-assigns",
        {
            "base.py": "class Base:\n def get(self):\n  return 'base'\n",
            "pkg.py": "\n            from base import Base\n            class Child(Base):\n                pass\n            def replacement(self):\n                return 'patched'\n            class Holder:\n                Child.get = replacement\n        ",
        },
        "extsym:pkg.Child.get",
        "extsym:base.Base.get",
    ),
    (
        "nested-exec-global",
        {
            "impl.py": "def run():\n return 'impl'\n",
            "pkg.py": "\n            from impl import run\n            def other():\n                return 'other'\n            def hide():\n                exec(\"global run\\nrun = other\")\n            hide()\n        ",
        },
        "extsym:pkg.run",
        "extsym:impl.run",
    ),
    (
        "match-rebind",
        {
            "impl.py": "def run():\n return 'impl'\n",
            "pkg.py": "\n            from impl import run\n            match 0:\n                case run:\n                    pass\n        ",
        },
        "extsym:pkg.run",
        "extsym:impl.run",
    ),
]


@pytest.mark.parametrize("name,files,left,right", CASES, ids=[c[0] for c in CASES])
def test_static_proof_and_runtime_identity_have_explicitly_different_limits(
    tmp_path, name, files, left, right
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    files = {path: textwrap.dedent(text) for path, text in files.items()}
    for path, text in files.items():
        (runtime / path).write_text(text, encoding="utf-8")
    script = """
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
def resolve(name):
    parts = name.removeprefix('extsym:').split('.')
    obj = importlib.import_module(parts[0])
    for part in parts[1:]:
        obj = getattr(obj, part)
    return obj
print(json.dumps(resolve(sys.argv[2]) is resolve(sys.argv[3])))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(runtime), left, right],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout) is False, name
    before, after = mark(tmp_path, files, left, [right])
    if name in ("called-patch-helper", "nested-exec-global"):
        # Static model: parameter receivers and exec contents are ignored.
        assert after == before
        return
    assert after["resolution"]["status"] == "conflict", name
    assert "same_implementation" not in after["resolution"]["detail"]
    after["resolution"] = before["resolution"]
    assert after == before


@pytest.mark.parametrize(
    "mutation",
    [
        "alias = Child\ndel alias.get\n",
        "def patch(cls):\n delattr(cls, 'get')\npatch(Child)\n",
        "def patch():\n setattr(Child, 'get', replacement)\npatch()\n",
        "def patch():\n eval(\"exec('Child.get = replacement')\")\npatch()\n",
    ],
)
def test_static_receivers_deletion_and_nested_calls(tmp_path, mutation):
    before, after = mark(
        tmp_path,
        {
            "base.py": "class Base:\n def get(self): return 1\n",
            "pkg.py": "from base import Base\nclass Child(Base): pass\ndef replacement(self): return 2\n"
            + mutation,
        },
        "extsym:pkg.Child.get",
        ["extsym:base.Base.get"],
    )
    if "cls" in mutation or "eval(" in mutation:
        # Static model: unknown receivers and eval contents are ignored.
        assert after == before
    else:
        assert after["resolution"]["status"] == "conflict"


@pytest.mark.parametrize(
    "pattern", ["run", "[run, *rest]", "[*run]", "{'key': run}", "{**run}"]
)
def test_match_capture_rebinds_selected_name(tmp_path, pattern):
    _, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): return 1\n",
            "pkg.py": "from impl import run\nmatch value:\n case "
            + pattern
            + ":\n  pass\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after["resolution"]["status"] == "conflict"


@pytest.mark.parametrize("location", ["facade", "definition"])
def test_writes_are_checked_in_each_import_chain_module(tmp_path, location):
    files = {
        "pkg.py": "from middle import Client\n",
        "middle.py": "from impl import Client\n",
        "impl.py": "class Client:\n def get(self): return 1\n",
    }
    path = "middle.py" if location == "facade" else "impl.py"
    files[
        path
    ] += "alias = Client\ndef replacement(self): return 2\nalias.get = replacement\n"
    _, after = mark(
        tmp_path, files, "extsym:pkg.Client.get", ["extsym:impl.Client.get"]
    )
    assert after["resolution"]["status"] == "conflict"


@pytest.mark.parametrize(
    "mutation",
    [
        "if True:\n def patch():\n  exec('Child.get = replacement')\n patch()\n",
        "if True:\n def patch():\n  eval(\"exec('Child.get = replacement')\")\n patch()\n",
        "namespace = globals()\ndef patch():\n alias = namespace\n alias['Child'] = Base\npatch()\n",
    ],
)
def test_static_model_ignores_exec_and_namespace_aliases_inside_other_suites(
    tmp_path, mutation
):
    before, after = mark(
        tmp_path,
        {
            "base.py": "class Base:\n def get(self): return 1\n",
            "pkg.py": "from base import Base\nclass Child(Base): pass\ndef replacement(self): return 2\n"
            + mutation,
        },
        "extsym:pkg.Child.get",
        ["extsym:base.Base.get"],
    )
    assert after == before
