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
    "star_only": "from pkg.other_helper import *\n" + USE,
    "star_nested_lacking": HELPER + "from pkg.middle import *\n" + USE,
    "middle": "from pkg.provider import *\n",
    "star_private": HELPER + "from pkg.private_helper import *\n" + USE,
    "private_helper": "def _helper():\n    return 4\n\n\nhelper = _helper\n"
    "__all__ = ['_helper']\n",
    "star_not_listed": HELPER + "from pkg.listed_helper import *\n" + USE,
    "listed_helper": "def helper(x):\n    return 5\n\n\n__all__ = ['other_name']\n"
    "other_name = 1\n",
    "star_imports_import": "from pkg.reexporter import *\n"
    "\n\ndef use():\n    return join('a')\n",
    "reexporter": "from os.path import join\n",
    "star_overrides_import": "from os.path import join\nfrom pkg.posix_exporter import *\n"
    "\n\ndef use():\n    return join('a')\n",
    "posix_exporter": "from posixpath import join\n",
    "type_checking_after": HELPER + "from typing import TYPE_CHECKING\n"
    "if TYPE_CHECKING:\n    from os.path import join as helper\n" + USE,
    "star_underscore": "def _hidden():\n    return 7\n\n\nfrom pkg.hidden_provider import *\n"
    "\n\ndef use():\n    return _hidden()\n",
    "hidden_provider": "def _hidden():\n    return 8\n",
    "empty_loop_else": HELPER + "for _ in []:\n    break\nelse:\n"
    "    from os.path import join as helper\n" + USE,
    "unbroken_loop_else": HELPER + "for item in ITEMS:\n    pass\nelse:\n"
    "    from os.path import join as helper\n" + USE,
    "broken_loop_else": HELPER + "for item in ITEMS:\n    break\nelse:\n"
    "    from os.path import join as helper\n" + USE,
    "finally_import": HELPER + "try:\n    pass\nfinally:\n"
    "    from os.path import join as helper\n" + USE,
    "semicolon_after": HELPER + "x = 1; helper = str\n" + USE,
    "loop_target": HELPER + "for helper in ITEMS:\n    pass\n" + USE,
    "import_under_if": HELPER
    + "if FLAG:\n    from os.path import join as helper\n"
    + USE,
    "import_under_with": HELPER + "with CONTEXT:\n"
    "    from os.path import join as helper\n" + USE,
    "if_false_only": "if False:\n    from pkg.other_helper import helper\n" + USE,
    "if_flag_only": "if FLAG:\n    from pkg.other_helper import helper\n" + USE,
    "loop_body_only": "for item in ITEMS:\n    from pkg.other_helper import helper\n"
    + USE,
    "if_else_same": "if FLAG:\n    from pkg.other_helper import helper\n"
    "else:\n    from pkg.other_helper import helper\n" + USE,
    "try_except_same": "try:\n    from pkg.other_helper import helper\n"
    "except ImportError:\n    from pkg.other_helper import helper\n" + USE,
    "code_in_block": "if FLAG:\n    from pkg.other_helper import helper\n"
    "    value = helper(1)\n",
    "code_in_sibling": "if FLAG:\n    from pkg.other_helper import helper\n"
    "else:\n    value = helper(1)\n",
    "code_before_import": "if FLAG:\n    from pkg.other_helper import helper\n"
    "else:\n    value = helper(1)\n    from pkg.other_helper import helper\n",
    "star_excluded": "from pkg.excluder import *\n\n\ndef use():\n    return lonely(1)\n",
    "excluder": "def lonely(x):\n    return x\n\n\n__all__ = ['kept']\nkept = 1\n",
    "star_empty_all": "from pkg.empty_all import *\n\n\ndef use():\n    return alone(1)\n",
    "empty_all": "def alone(x):\n    return x\n\n\n__all__ = []\n",
    "star_appended": "from pkg.appended import *\n\n\ndef use():\n    return added(1)\n",
    "appended": "def added(x):\n    return x\n\n\n__all__ = []\n__all__.append('added')\n",
    "star_all_twice": "from pkg.all_twice import *\n\n\ndef use():\n    return twice(1)\n",
    "all_twice": "def twice(x):\n    return x\n\n\n__all__ = ['twice']\n__all__ = ['kept']\n"
    "kept = 1\n",
    "module_code_after_rebind": "from os.path import join\n\njoin = str\nvalue = join('a')\n",
    "try_except_pass": "try:\n    from pkg.other_helper import helper\n"
    "except ImportError:\n    pass\n" + USE,
    "match_partial": "match FLAG:\n    case 1:\n        from pkg.other_helper import helper\n"
    "    case 2:\n        from pkg.other_helper import helper\n" + USE,
    "match_complete": "match FLAG:\n    case 1:\n        from pkg.other_helper import helper\n"
    "    case _:\n        from pkg.other_helper import helper\n" + USE,
    "star_empty_all_over_own": "def alone(x):\n    return x\n\n\nfrom pkg.empty_all import *\n"
    "\n\ndef use():\n    return alone(1)\n",
    "star_appended_over_own": "def added(x):\n    return x\n\n\nfrom pkg.appended import *\n"
    "\n\ndef use():\n    return added(1)\n",
    "star_cycle_a": "from pkg.star_cycle_b import *\n" + USE,
    "star_cycle_b": "from pkg.star_cycle_a import *\n",
    "star_unread_all": HELPER + "from pkg.computed_all import *\n" + USE,
    "computed_all": "def helper(x):\n    return 6\n\n\n__all__ = sorted(['helper'])\n",
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
        # A star import binds what its indexed module exports: its helper,
        # or, past a module that exports none, the module's own.
        ("star_after_defining", "fn:pkg.other_helper.helper"),
        ("star_only", "fn:pkg.other_helper.helper"),
        ("star_nested_lacking", "fn:pkg.star_nested_lacking.helper"),
        ("star_private", "fn:pkg.star_private.helper"),
        ("star_not_listed", "fn:pkg.star_not_listed.helper"),
        # Without __all__ a star import skips a name starting with _.
        ("star_underscore", "fn:pkg.star_underscore._hidden"),
        ("star_imports_import", "extsym:os.path.join"),
        # The star import after the module's own import rebinds join.
        ("star_overrides_import", "extsym:posixpath.join"),
        # Some binding surely runs: one in each branch, or one in the block
        # the module's own code reads it in.
        ("if_else_same", "fn:pkg.other_helper.helper"),
        ("try_except_same", "fn:pkg.other_helper.helper"),
        ("code_in_block", "fn:pkg.other_helper.helper"),
        ("match_complete", "fn:pkg.other_helper.helper"),
        # An empty __all__ exports nothing, so the module's own alone holds.
        ("star_empty_all_over_own", "fn:pkg.star_empty_all_over_own.alone"),
        # A loop's else with no break that can run, and a finally, run.
        ("empty_loop_else", "extsym:os.path.join"),
        ("unbroken_loop_else", "extsym:os.path.join"),
        ("finally_import", "extsym:os.path.join"),
        # An import only for type checkers binds nothing at run time.
        ("type_checking_after", "fn:pkg.type_checking_after.helper"),
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
        # A break may skip the else; a loop may not run; a statement after
        # a semicolon is at the top level too.
        "broken_loop_else",
        # The import may not have run, so the def may hold.
        "import_under_if",
        "import_under_with",
        "loop_target",
        "semicolon_after",
        # No binding surely runs: the name may be unbound, a NameError.
        "if_false_only",
        "if_flag_only",
        "loop_body_only",
        # A handler that binds nothing, or a match with no case for the rest.
        "try_except_pass",
        "match_partial",
        # appended may export added, so the module's own may not hold.
        "star_appended_over_own",
        # Read in the other branch, or in the branch before its import.
        "code_in_sibling",
        "code_before_import",
        # The module's own code reads join after it is rebound.
        "module_code_after_rebind",
        # The star import does not bring the name, so it is no symbol of
        # that name elsewhere: __all__ leaves it out, even empty; or __all__
        # is changed or bound twice, and whether it does is not known.
        "star_excluded",
        "star_empty_all",
        "star_appended",
        "star_all_twice",
        # Star imports that import back, or an __all__ not read as a list.
        "star_cycle_a",
        "star_unread_all",
    ],
)
def test_a_name_that_may_hold_something_else_links_nothing(targets, module):
    assert targets.get(module, set()) == set()
