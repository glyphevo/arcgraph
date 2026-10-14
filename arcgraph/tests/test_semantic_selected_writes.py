"""Only statically determined writes to the selected target prevent proofs."""

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
    # The static model rejects a selected constant member. An unresolved member
    # name is ignored even when it might select that member at runtime.
    for index, attribute in enumerate(('"get"', "name")):
        root = tmp_path / ("selected" + str(index))
        root.mkdir()
        changed = dict(files)
        changed["pkg.py"] += "setattr(x, " + attribute + ", replacement)\n"
        before, after = mark(
            root, changed, "extsym:pkg.Child.get", ["extsym:base.Base.get"]
        )
        assert (after["resolution"]["status"] == "conflict") is (attribute == '"get"')
        if attribute == "name":
            assert after == before


@pytest.mark.parametrize(
    "mutation,conflict",
    [
        ("setattr(x, name, replacement)", False),
        ('setattr(x, "get", replacement)', True),
        ('delattr(x, "get")', True),
        ("x.get = replacement", True),
        ("del x.get", True),
        ('exec("x.get = replacement")', False),
        ('def patch():\n exec("x.get = replacement")', False),
        ("exec(dynamic_source)", False),
        ("eval(dynamic_source)", False),
        ('globals()["run"] = replacement', False),
        ("globals()[name] = replacement", False),
        ('vars().update({"run": replacement})', False),
        ("def patch():\n global run\n run = replacement", True),
        # There is no enclosing function binding here: nonlocal cannot select
        # the module's run. The separate static-identity test covers a real
        # enclosing-function nonlocal write and rejects that binding.
        ("def patch():\n nonlocal run\n run = replacement", False),
        ("exec(\"namespace = globals(); namespace['run'] = replacement\")", False),
        ('def patch():\n exec("run = replacement", globals())', False),
        ("def patch():\n exec(\"ns = globals(); ns['run'] = replacement\")", False),
    ],
)
def test_object_identity_and_relevant_namespace_or_member_write(
    tmp_path, mutation, conflict
):
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
    before, after = mark(
        negative, files, "extsym:pkg." + name, ["extsym:base." + original]
    )
    assert (after["resolution"]["status"] == "conflict") is conflict
    if not conflict:
        # These runtime effects deliberately do not change static identity.
        assert after == before
