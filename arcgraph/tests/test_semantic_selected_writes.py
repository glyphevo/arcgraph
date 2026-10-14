"""Only writes to the selected namespace or member prevent identity proofs."""

import pytest

from arcgraph.tests.test_semantic_implementation_conflicts import mark


@pytest.mark.parametrize(
    "irrelevant",
    [
        'setattr(x, "__module__", "httpx2")',
        "def unrelated(self, match):\n self.match: str = match",
        "run.Exception = Exception",
        "run.__code__ = replacement",
        "del x.unrelated",
        'delattr(x, "unrelated")',
        'exec("unrelated = replacement")',
        'def helper():\n exec("unrelated = replacement")',
        "eval(\"setattr(x, '__module__', 'httpx2')\")",
        'globals()["unrelated"] = replacement',
        'namespace = globals()\ncopy = namespace\ncopy["unrelated"] = replacement',
        "vars().update(unrelated=replacement)",
        'locals().pop("unrelated", None)',
        'def helper():\n locals()["run"] = replacement',
        'def helper():\n namespace = locals()\n namespace["run"] = replacement',
        'class Other:\n vars()["run"] = replacement',
        'def helper():\n exec("run = replacement")',
    ],
)
def test_irrelevant_writes_preserve_reexport_but_selected_writes_do_not(
    tmp_path, irrelevant
):
    files = {
        "base.py": "def run(): pass\nclass Base:\n def get(self): pass\n",
        "pkg.py": "from base import run, Base\nclass Child(Base): pass\nx = Child\n"
        + irrelevant
        + '\nsetattr(x, "__module__", "httpx2")\n',
    }
    for index, (left, right) in enumerate(
        (("pkg.run", "base.run"), ("pkg.Child.get", "base.Base.get"))
    ):
        root = tmp_path / str(index)
        root.mkdir()
        before, after = mark(root, files, "extsym:" + left, ["extsym:" + right])
        assert after["resolution"] == before["resolution"]
    # Pair each permissive case with a relevant write to avoid accepting all
    # mutations accidentally. Both fixed and dynamic attribute names matter.
    for index, attribute in enumerate(('"get"', "name")):
        root = tmp_path / ("selected" + str(index))
        root.mkdir()
        changed = dict(files)
        changed["pkg.py"] += "setattr(x, " + attribute + ", replacement)\n"
        _, after = mark(root, changed, "extsym:pkg.Child.get", ["extsym:base.Base.get"])
        assert after["resolution"]["status"] == "conflict"


@pytest.mark.parametrize(
    "mutation",
    [
        "setattr(x, name, replacement)",
        'setattr(x, "get", replacement)',
        'delattr(x, "get")',
        "x.get = replacement",
        "del x.get",
        'exec("x.get = replacement")',
        'def patch():\n exec("x.get = replacement")',
        "exec(dynamic_source)",
        "eval(dynamic_source)",
        'globals()["run"] = replacement',
        "globals()[name] = replacement",
        'vars().update({"run": replacement})',
        "def patch():\n global run\n run = replacement",
        "def patch():\n nonlocal run\n run = replacement",
        "exec(\"namespace = globals(); namespace['run'] = replacement\")",
        'def patch():\n exec("run = replacement", globals())',
        "def patch():\n exec(\"ns = globals(); ns['run'] = replacement\")",
    ],
)
def test_object_identity_and_relevant_namespace_or_member_write(tmp_path, mutation):
    # Attributes on the class itself do not change its object identity.
    files = {
        "base.py": "def run(): pass\nclass Base:\n def get(self): pass\n",
        "pkg.py": 'from base import Base, run\nsetattr(x, "__module__", "httpx2")\n',
    }
    positive = tmp_path / "positive"
    positive.mkdir()
    before, after = mark(positive, files, "extsym:pkg.Base", ["extsym:base.Base"])
    assert after["resolution"] == before["resolution"]
    files["pkg.py"] = (
        "from base import Base, run\nclass Child(Base): pass\nx = Child\n"
        + mutation
        + "\n"
    )
    negative = tmp_path / "negative"
    negative.mkdir()
    name, original = (
        ("run", "run")
        if "globals()" in mutation
        or "vars()" in mutation
        or "global run" in mutation
        or "nonlocal run" in mutation
        or "namespace['run']" in mutation
        else ("Child.get", "Base.get")
    )
    _, after = mark(negative, files, "extsym:pkg." + name, ["extsym:base." + original])
    assert after["resolution"]["status"] == "conflict"
