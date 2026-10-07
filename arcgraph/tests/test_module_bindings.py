"""A module-level name is what the module binds it to where it is read.

A function or method runs after the module has, so a name of the module is
its last binding there; the module's own code and a class body run in
order, so they read the last binding made before the call. A binding under
if or try may not have run, so the one before it may hold too. Where what
may hold is not one definition or one import, as after helper = str, a
second def, a del, a fallback in an except, a global write in a function, or
a star import of a module the index cannot read, the call links nothing,
neither the definition, the builtin of the name nor a symbol elsewhere.
"""

from __future__ import annotations

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer

HELPER = "def helper(x):\n    return x\n\n\n"
USE = "\n\ndef use():\n    return helper(1)\n"

MODULES = {
    "plain": HELPER + USE,
    "assigned_before": "helper = str\n\n\n" + HELPER + USE,
    "star_before": "from unknown_module import *\n\n\n" + HELPER + USE,
    "star_after_lacking": HELPER + "from pkg.provider import *\n" + USE,
    "decorated": "import functools\n\n\n@functools.cache\n" + HELPER + USE,
    "listed": HELPER + "__all__ = ['helper', 'use']\n" + USE,
    "module_after": HELPER + "value = helper(1)\n",
    "module_between": HELPER + "value = helper(1)\nhelper = str\n",
    "class_between": HELPER + "class Box:\n    value = helper(1)\n\n\nhelper = str\n",
    "builtin": "def use(x):\n    return len(x)\n",
    "builtin_before": "value = len([])\nlen = None\n",
    "imported": "import os\nimport os.path\n\n\ndef use():\n    return os.path.join('a')\n",
    "imported_submodule": "import os.path\n\n\ndef use():\n    return os.path.join('a')\n",
    "imported_as": "import os.path as osp\n\n\ndef use():\n    return osp.join('a')\n",
    "assigned_after": HELPER + "helper = str\n" + USE,
    "redefined": HELPER + "def helper(x):\n    return 2\n" + USE,
    "redefined_under_if": HELPER
    + "if FLAG:\n    def helper(x):\n        return 2\n"
    + USE,
    "deleted": HELPER + "del helper\n" + USE,
    "star_after": HELPER + "from unknown_module import *\n" + USE,
    "star_after_defining": HELPER + "from pkg.other_helper import *\n" + USE,
    "module_before": "value = helper(1)\n\n\n" + HELPER,
    "builtin_rebound": "len = None\n\n\ndef use(x):\n    return len(x)\n",
    "either_branch": "if FLAG:\n"
    "    def helper(x):\n        return x\n"
    "else:\n    helper = len\n" + USE,
    "import_fallback": "try:\n    from fast import helper\n"
    "except ImportError:\n    def helper(x):\n        return x\n" + USE,
    "import_or_none": "try:\n    import tomllib\nexcept ImportError:\n    tomllib = None\n"
    "\n\ndef use():\n    return tomllib.loads('')\n",
    "global_write": HELPER + "def init():\n    global helper\n    helper = str\n" + USE,
    "assigned_after_import": "from os.path import join\n\njoin = str\n"
    "\n\ndef use():\n    return join('a')\n",
    "import_maybe_replaced": "import json\n\ntry:\n    import simplejson as json\n"
    "except ImportError:\n    pass\n\n\ndef use():\n    return json.loads('')\n",
    "assigned_elsewhere": "tool = str\n\n\ndef use():\n    return tool(1)\n",
    "tool_provider": "def tool(x):\n    return x\n",
    "called_before": "value = early(1)\n\n\ndef early(x):\n    return x\n",
    "provider": "def other(x):\n    return x\n",
    "other_helper": "def helper(x):\n    return x\n",
}


@pytest.fixture(scope="module")
def targets(tmp_path_factory: pytest.TempPathFactory) -> dict[str, set[str]]:
    """Each module's resolved targets, from use() or the module's own code."""

    root = tmp_path_factory.mktemp("bindings")
    package = root / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, text in MODULES.items():
        (package / f"{name}.py").write_text(text, encoding="utf-8")
    output = root / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
    ).build()
    result: dict[str, set[str]] = {}
    for edge in GraphStoreReader.from_current(output).read_edges():
        if edge.resolution.status != "resolved" or not edge.properties.get("callsite"):
            continue
        for prefix in ("fn:pkg.", "mod:pkg.", "class:pkg."):
            if edge.source.startswith(prefix):
                module = edge.source.removeprefix(prefix).split(".")[0]
                if edge.source.endswith(".init"):
                    break
                result.setdefault(module, set()).add(edge.target)
    return result


def test_modules_parse() -> None:
    for name, text in MODULES.items():
        compile(text, f"{name}.py", "exec")


@pytest.mark.parametrize(
    ("module", "target"),
    [
        ("plain", "fn:pkg.plain.helper"),
        # The def rebinds the name after the assignment or the star import.
        ("assigned_before", "fn:pkg.assigned_before.helper"),
        ("star_before", "fn:pkg.star_before.helper"),
        # The star import's module is indexed and binds no helper.
        ("star_after_lacking", "fn:pkg.star_after_lacking.helper"),
        ("decorated", "fn:pkg.decorated.helper"),
        # __all__ lists the name; it does not bind it.
        ("listed", "fn:pkg.listed.helper"),
        # Module code and a class body read what is bound by the call.
        ("module_after", "fn:pkg.module_after.helper"),
        ("module_between", "fn:pkg.module_between.helper"),
        ("class_between", "fn:pkg.class_between.helper"),
        ("builtin", "extsym:builtins.len"),
        ("builtin_before", "extsym:builtins.len"),
        # import os and import os.path both bind os to the package.
        ("imported", "extsym:os.path.join"),
        # It binds os, so os.path.join is not os.path.path.join.
        ("imported_submodule", "extsym:os.path.join"),
        ("imported_as", "extsym:os.path.join"),
    ],
)
def test_a_name_resolves_to_what_holds_where_it_is_read(targets, module, target):
    assert targets.get(module, set()) == {target}


@pytest.mark.parametrize(
    "module",
    [
        "assigned_after",
        "redefined",
        "redefined_under_if",
        "deleted",
        "star_after",
        "star_after_defining",
        # A NameError at run time, whether the name is defined elsewhere
        # too or only here.
        "module_before",
        "called_before",
        "builtin_rebound",
        "either_branch",
        "import_fallback",
        # tomllib or None: the guard before the call is not read here.
        "import_or_none",
        "global_write",
        "assigned_after_import",
        # json or simplejson, whichever imported.
        "import_maybe_replaced",
        # tool is the module's str, not the one tool defined elsewhere.
        "assigned_elsewhere",
    ],
)
def test_a_name_that_may_hold_something_else_links_nothing(targets, module):
    assert targets.get(module, set()) == set()
