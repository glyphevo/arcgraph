"""Runtime class checks prove nominal types, never erased generic parameters."""

import pytest

from arcgraph.tests.test_flow_attribute_chains import _calls


@pytest.mark.parametrize(
    "source,expression,target",
    [
        (
            "class Foo:\n    def work(self): pass\nclass Bar:\n    def other(self): pass\ndef use(x: Foo | Bar):\n    if isinstance(x,Foo): x.work()\n",
            "x.work()",
            "method:pkg.user.Foo.work",
        ),
        (
            "from typing import Any\ndef use(x: Any | None):\n    if isinstance(x,dict): x.get('key')\n",
            "x.get('key')",
            "extsym:builtins.dict.get",
        ),
        (
            "from datetime import date,datetime\ndef use(value: date | str):\n    if isinstance(value,datetime): value.isoformat()\n",
            "value.isoformat()",
            "extsym:datetime.datetime.isoformat",
        ),
        (
            "def use(v):\n    if isinstance(v,str) and v: v.split()\n",
            "v.split()",
            "extsym:builtins.str.split",
        ),
    ],
)
def test_runtime_nominal_guard_proves_the_checked_receiver(
    tmp_path, source, expression, target
):
    assert _calls(tmp_path, source).get(expression) == {target}


@pytest.mark.parametrize("exit", ["return", "raise TypeError()"])
def test_negative_isinstance_exit_proves_surviving_statements(tmp_path, exit):
    calls = _calls(
        tmp_path,
        f"def use(value):\n    if not isinstance(value,str): {exit}\n    value.split()\n",
    )
    assert calls.get("value.split()") == {"extsym:builtins.str.split"}


def test_negative_isinstance_continue_proves_only_current_iteration(tmp_path):
    calls = _calls(
        tmp_path,
        "def use(values: list[object]):\n    for value in values:\n        if not isinstance(value,str): continue\n        value.split()\n",
    )
    assert calls.get("value.split()") == {"extsym:builtins.str.split"}


@pytest.mark.parametrize(
    "source,expression",
    [
        (
            "class Meta(type):\n    def __instancecheck__(cls,obj): return True\nclass Foo(metaclass=Meta):\n    def work(self): pass\ndef use(x: object):\n    if isinstance(x,Foo): x.work()\n",
            "x.work()",
        ),
        (
            "from pkg.external import Base\nclass Foo(Base):\n    def work(self): pass\nclass Bar: pass\ndef use(x: Foo | Bar):\n    if isinstance(x,Foo): x.work()\n",
            "x.work()",
        ),
        (
            "from pathlib import Path\ndef use(value):\n    if not isinstance(value,Path): return\n    value=object()\n    value.exists()\n",
            "value.exists()",
        ),
        (
            "def use(value):\n    if isinstance(value,str) or value: value.split()\n",
            "value.split()",
        ),
    ],
)
def test_non_nominal_or_invalid_guard_does_not_claim_the_type(
    tmp_path, source, expression
):
    calls = _calls(tmp_path, source)
    assert calls.get(expression) != {"extsym:builtins.str.split"}
    assert calls.get(expression) != {"method:pkg.user.Foo.work"}
    assert calls.get(expression) != {"extsym:pathlib.Path.exists"}
