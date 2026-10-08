"""Recover proven reads without trusting escaped exports or wrapped generators."""

from __future__ import annotations

import pytest

from arcgraph.tests.test_generator_and_dynamic_exports import EXPORT_BASE, _targets

READONLY_EXPORT_CASES = [
    ("len", "COUNT = len(__all__)\n", True),
    ("loop", "for name in __all__:\n    pass\n", True),
    ("membership", "PRESENT = 'helper' in __all__\n", True),
    ("not_membership", "MISSING = 'absent' not in __all__\n", True),
    ("sorted", "COPY = sorted(__all__)\nCOPY.remove('helper')\n", True),
    ("sorted_reverse", "COPY = sorted(__all__, reverse=True)\n", True),
    ("sorted_key_none", "COPY = sorted(__all__, key=None)\n", True),
    ("list", "COPY = list(__all__)\nCOPY.remove('helper')\n", True),
    ("tuple", "COPY = tuple(__all__)\n", True),
    ("subscript", "FIRST = __all__[0]\n", True),
    ("slice", "COPY = __all__[:]\nCOPY.remove('helper')\n", True),
    ("comprehension", "COPY = [name for name in __all__]\n", True),
    ("generator_comprehension", "COPY = tuple(name for name in __all__)\n", True),
    ("unpack", "FIRST, SECOND = __all__\n", True),
    ("nested_copies", "COPY = tuple(sorted(list(__all__)))\n", True),
    (
        "copied_argument",
        "def consume(value): value.clear()\nconsume(list(__all__))\n",
        True,
    ),
    (
        "element_argument",
        "def consume(value): return value\nconsume(__all__[0])\n",
        True,
    ),
    ("function_read", "def count(): return len(__all__)\ncount()\n", True),
    (
        "later_builtin_binding",
        "COUNT = len(__all__)\ndef len(value): value.clear()\n",
        True,
    ),
    (
        "type_checking_binding",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    len = lambda value: value.clear()\nCOUNT = len(__all__)\n",
        True,
    ),
    (
        "unknown_function",
        "def consume(value): value.clear()\nconsume(__all__)\n",
        False,
    ),
    (
        "unknown_method",
        "class Box:\n    def consume(self, value): value.clear()\nBox().consume(__all__)\n",
        False,
    ),
    ("alias", "COPY = __all__\nCOPY.clear()\n", False),
    ("container_alias", "BOX = [__all__]\nBOX[0].clear()\n", False),
    ("walrus_alias", "(COPY := __all__).clear()\n", False),
    ("receiver_method", "__all__.remove('helper')\n", False),
    (
        "module_shadow_len",
        "def len(value): value.clear()\nCOUNT = len(__all__)\n",
        False,
    ),
    (
        "module_shadow_sorted",
        "def sorted(value): value.clear()\nCOPY = sorted(__all__)\n",
        False,
    ),
    (
        "module_shadow_list",
        "def list(value): value.clear()\nCOPY = list(__all__)\n",
        False,
    ),
    (
        "module_shadow_tuple",
        "def tuple(value): value.clear()\nCOPY = tuple(__all__)\n",
        False,
    ),
    (
        "parameter_shadow",
        "def count(len): return len(__all__)\ndef consume(value): value.clear()\ncount(consume)\n",
        False,
    ),
    (
        "captured_shadow",
        "def count(len):\n    def inner(): return len(__all__)\n    return inner()\ndef consume(value): value.clear()\ncount(consume)\n",
        False,
    ),
    (
        "global_shadow",
        "def consume(value): value.clear()\ndef change():\n    global len\n    len = consume\nchange()\nCOUNT = len(__all__)\n",
        False,
    ),
    ("star_shadow", "from pkg.shadows import *\nCOUNT = len(__all__)\n", False),
    ("import_shadow", "from pkg.shadows import len\nCOUNT = len(__all__)\n", False),
    (
        "sorted_callback",
        "def key(value): return value\nCOPY = sorted(__all__, key=key)\n",
        False,
    ),
    (
        "keyword_escape",
        "def consume(*, value): value.clear()\nconsume(value=__all__)\n",
        False,
    ),
]


@pytest.mark.parametrize("v2", [False, True], ids=["legacy", "v2"])
@pytest.mark.parametrize(
    "case,reads,expected",
    READONLY_EXPORT_CASES,
    ids=[row[0] for row in READONLY_EXPORT_CASES],
)
def test_readonly_exports_require_proven_builtin_identity(
    tmp_path, v2, case, reads, expected
):
    targets = _targets(
        tmp_path,
        {
            "provider": EXPORT_BASE + "__all__ = ['helper', 'other']\n" + reads,
            "user": "from pkg.provider import *\ndef use(): return helper()\n",
            "shadows": "__all__ = ['len']\ndef len(value): value.clear()\n",
        },
        v2=v2,
    )
    assert ("fn:pkg.provider.helper" in targets) is expected, targets


@pytest.mark.parametrize("descriptor", ["staticmethod", "classmethod"])
@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("qualified", ["builtins", "b"])
def test_qualified_builtin_descriptors_keep_generator_values(
    tmp_path, descriptor, async_, qualified
):
    imports = (
        "import builtins\n" if qualified == "builtins" else "import builtins as b\n"
    )
    parameter = "cls" if descriptor == "classmethod" else ""
    keyword = "async " if async_ else ""
    member = "aclose" if async_ else "send"
    arguments = "" if async_ else "None"
    owner = "AsyncGeneratorType" if async_ else "GeneratorType"
    source = (
        imports
        + f"class Box:\n    @{qualified}.{descriptor}\n    {keyword}def made({parameter}):\n        yield 1\ndef use(): return Box.made().{member}({arguments})\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert f"extsym:types.{owner}.{member}" in targets, targets


@pytest.mark.parametrize(
    "prefix,decorator,suffix,expected",
    [
        ("import builtins\n", "builtins.staticmethod", "", True),
        ("import builtins\n", "builtins.staticmethod", "builtins = None\n", True),
        (
            "import builtins\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    builtins = None\n",
            "builtins.staticmethod",
            "",
            True,
        ),
        ("import builtins\nbuiltins = None\n", "builtins.staticmethod", "", False),
        ("from pkg import fake as builtins\n", "builtins.staticmethod", "", False),
        ("", "builtins.staticmethod", "", False),
        (
            "import builtins\nfrom pkg.shadows import *\n",
            "builtins.staticmethod",
            "",
            False,
        ),
        (
            "import builtins\ndef change():\n    global builtins\n    builtins = None\nchange()\n",
            "builtins.staticmethod",
            "",
            False,
        ),
        (
            "import builtins\ndef wrap(fn): return lambda: 1\nbuiltins.staticmethod = wrap\n",
            "builtins.staticmethod",
            "",
            False,
        ),
        (
            "import builtins\n",
            "builtins.staticmethod",
            "builtins.staticmethod = None\n",
            True,
        ),
        (
            "import builtins\ndef wrap(fn): return lambda: 1\nsetattr(builtins, 'staticmethod', wrap)\n",
            "builtins.staticmethod",
            "",
            False,
        ),
        ("import contextlib\n", "contextlib.contextmanager", "", False),
        ("from contextlib import contextmanager as cm\n", "cm", "", False),
        ("import contextlib as cm\n", "cm.contextmanager", "", False),
    ],
    ids=[
        "standard",
        "later_module_binding",
        "static_binding",
        "shadow_module",
        "wrong_import",
        "missing_import",
        "star_shadow",
        "global_shadow",
        "attribute_shadow",
        "later_attribute",
        "setattr_shadow",
        "qualified_contextmanager",
        "alias_contextmanager",
        "module_alias_contextmanager",
    ],
)
def test_qualified_descriptors_need_the_standard_module(
    tmp_path, prefix, decorator, suffix, expected
):
    source = (
        prefix
        + f"class Box:\n    @{decorator}\n    def made():\n        yield 1\n"
        + suffix
        + "def use(): return Box.made().send(None)\n"
    )
    targets = _targets(
        tmp_path,
        {
            "user": source,
            "fake": "def staticmethod(fn): return lambda: 1\n",
            "shadows": "builtins = None\n",
        },
        v2=True,
    )
    assert ("extsym:types.GeneratorType.send" in targets) is expected, targets


@pytest.mark.parametrize(
    "prefix,class_suffix,module_suffix,expected",
    [
        ("from pkg.shadows import *\n", "", "", False),
        ("", "", "def staticmethod(fn): return lambda: 1\n", True),
        ("", "    staticmethod = lambda fn: lambda: 1\n", "", True),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg.shadows import staticmethod\n",
            "",
            "",
            True,
        ),
        (
            "from typing import TYPE_CHECKING\n",
            "    if TYPE_CHECKING:\n        staticmethod = lambda fn: lambda: 1\n",
            "",
            True,
        ),
    ],
    ids=[
        "star_shadow",
        "later_module_binding",
        "later_class_binding",
        "module_static_only",
        "class_static_only",
    ],
)
def test_descriptor_definition_time_shadow_guards(
    tmp_path, prefix, class_suffix, module_suffix, expected
):
    source = (
        prefix
        + "class Box:\n    @staticmethod\n    def made():\n        yield 1\n"
        + class_suffix
        + module_suffix
        + "def use(): return Box.made().send(None)\n"
    )
    targets = _targets(
        tmp_path,
        {"user": source, "shadows": "def staticmethod(fn): return lambda: 1\n"},
        v2=True,
    )
    assert ("extsym:types.GeneratorType.send" in targets) is expected, targets


@pytest.mark.parametrize("qualified,expected", [(False, True), (True, True)])
def test_nested_descriptor_reads_enclosing_import_not_own_parameter(
    tmp_path, qualified, expected
):
    decorator = "builtins.staticmethod" if qualified else "staticmethod"
    source = f"import builtins\ndef use():\n    @{decorator}\n    def made(builtins=None):\n        yield 1\n    return made().send(None)\n"
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert ("extsym:types.GeneratorType.send" in targets) is expected, targets


def test_qualified_descriptor_does_not_trust_an_enclosing_parameter(tmp_path):
    source = "def use(builtins):\n    @builtins.staticmethod\n    def made():\n        yield 1\n    return made().send(None)\n"
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert "extsym:types.GeneratorType.send" not in targets, targets


def test_descriptor_ignores_type_checking_binding_before_class_method(tmp_path):
    source = "from typing import TYPE_CHECKING\nclass Box:\n    if TYPE_CHECKING:\n        staticmethod = lambda fn: lambda: 1\n    @staticmethod\n    def made():\n        yield 1\ndef use(): return Box.made().send(None)\n"
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert "extsym:types.GeneratorType.send" in targets, targets


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("outer_import", [False, True])
@pytest.mark.parametrize("late", [False, True])
def test_descriptor_import_must_be_available_when_nested_function_is_defined(
    tmp_path, async_, outer_import, late
):
    prefix = "import builtins\n" if outer_import else ""
    keyword = "async " if async_ else ""
    member = "aclose" if async_ else "send"
    arguments = "" if async_ else "None"
    owner = "AsyncGeneratorType" if async_ else "GeneratorType"
    source = (
        prefix
        + "def use():\n"
        + ("" if late else "    import builtins\n")
        + "    @builtins.staticmethod\n"
        + f"    {keyword}def made():\n        yield 1\n"
        + ("    import builtins\n" if late else "")
        + f"    return made().{member}({arguments})\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert (f"extsym:types.{owner}.{member}" in targets) is (not late), targets


@pytest.mark.parametrize("qualified", [False, True])
def test_classmethod_generator_outside_a_class_is_not_directly_callable(
    tmp_path, qualified
):
    prefix = "import builtins\n" if qualified else ""
    decorator = "builtins.classmethod" if qualified else "classmethod"
    source = (
        prefix
        + f"@{decorator}\ndef made(): yield 1\ndef use(): return made().send(None)\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert "extsym:types.GeneratorType.send" not in targets, targets


@pytest.mark.parametrize("outer_import", [False, True])
def test_conditional_local_import_does_not_prove_a_descriptor_module(
    tmp_path, outer_import
):
    source = (
        ("import builtins\n" if outer_import else "")
        + "def use():\n    if False:\n        import builtins\n    @builtins.staticmethod\n    def made():\n        yield 1\n    return made().send(None)\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert "extsym:types.GeneratorType.send" not in targets, targets
