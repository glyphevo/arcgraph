"""Conflict needs a failed frozen implementation proof, at each callsite."""

import inspect
from types import SimpleNamespace

import pytest

from arcgraph.core.schemas import Edge, FactResolution
from arcgraph.semantic.graph_projection import mark_unverified_external
from arcgraph.semantic.snapshot import capture


def mark(
    tmp_path,
    files,
    analyzer,
    positives,
    *,
    workspace=False,
    extra_site=None,
    dependency_link=False,
):
    source = tmp_path / "live"
    source.mkdir()
    for name, text in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    root = tmp_path / "frozen"
    snapshot = capture(
        source,
        list(files),
        root,
        target_python="3.12",
        platform="linux",
    )
    if workspace:
        modules = tuple(
            (name, name.removesuffix(".py").replace("/", ".").removesuffix(".__init__"))
            for name in files
        )
        snapshot = snapshot.model_copy(update={"module_names": modules})
    else:
        packages = root.parent / "environment/lib/python3.12/site-packages"
        packages.mkdir(parents=True)
        for name in files:
            path = packages / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((root / name).read_bytes())
        if dependency_link:
            outside = source / "impl.py"
            outside.write_text("def run(): return 999\n", encoding="utf-8")
            (packages / "impl.py").symlink_to(outside)
    # A live source mutation cannot affect the proof.
    for name in files:
        (source / name).write_text("raise RuntimeError('live')\n", encoding="utf-8")
    site = SimpleNamespace(
        owner="owner", span=SimpleNamespace(start=(0, 0)), expression="alias()"
    )
    sites = {"site": site}
    facts = [{"line": 1, "column": 0, "call_expression": "alias()"}]
    answers = [
        SimpleNamespace(
            subject="site",
            outcome="candidates",
            candidates=[SimpleNamespace(target=p) for p in positives],
        )
    ]
    if extra_site:
        sites["other"] = SimpleNamespace(
            owner="owner", span=SimpleNamespace(start=(1, 0)), expression="other()"
        )
        facts.append({"line": 2, "column": 0, "call_expression": "other()"})
        answers.append(
            SimpleNamespace(
                subject="other",
                outcome="candidates",
                candidates=[SimpleNamespace(target=extra_site)],
            )
        )
    view = SimpleNamespace(
        sites=sites,
        records={key: SimpleNamespace(kind="callsite") for key in sites},
    )
    primary = SimpleNamespace(
        producer=SimpleNamespace(name="pyright", id="pyright"), answers=answers
    )
    edge = Edge(
        source="fn:demo.run",
        target=analyzer,
        kind="uses",
        confidence="inferred",
        resolution=FactResolution(status="resolved", detail="original"),
        properties={"callsite": facts[0], "callsites": facts},
    )
    before = edge.model_dump(mode="json")
    kwargs = {}
    if "implementation_proof" in inspect.signature(mark_unverified_external).parameters:
        from arcgraph.semantic.implementation import ImplementationProof

        kwargs["implementation_proof"] = ImplementationProof(root, snapshot)
    mark_unverified_external(
        view,
        {"edge": edge},
        {"admission": [{"producer": "pyright", "allowed": True}], "edges": []},
        "pyright",
        primary,
        {"owner": edge.source},
        **kwargs,
    )
    return before, edge.model_dump(mode="json")


@pytest.mark.parametrize(
    "facade,analyzer,positive",
    [
        ("from impl import run\n", "extsym:pkg.run", "extsym:impl.run"),
        (
            "from impl import run\nrun.metadata = 'value'\n",
            "extsym:pkg.run",
            "extsym:impl.run",
        ),
        (
            "from impl import run\nrun.__code__ = replacement\n",
            "extsym:pkg.run",
            "extsym:impl.run",
        ),
        (
            "from impl import run\ndef __getattr__(name): return None\n",
            "extsym:pkg.run",
            "extsym:impl.run",
        ),
        ("from impl import run as alias\n", "extsym:pkg.alias", "extsym:impl.run"),
        ("import impl as alias\n", "extsym:pkg.alias.run", "extsym:impl.run"),
        ("import impl.deep as alias\n", "extsym:pkg.alias.run", "extsym:impl.deep.run"),
        ("from impl import *\n", "extsym:pkg.run", "extsym:impl.run"),
        (
            "from impl import Client\n",
            "extsym:pkg.Client.get",
            "extsym:impl.Client.get",
        ),
    ],
)
def test_import_and_reexported_class_keep_original_resolution(
    tmp_path, facade, analyzer, positive
):
    files = {
        "pkg/__init__.py": facade,
        "impl/__init__.py": "__all__ = ['run', 'Client']\ndef run(): return 1\nclass Client:\n def get(self): return 1\n",
        "impl/deep.py": "def run(): return 1\n",
    }
    before, after = mark(tmp_path, files, analyzer, [positive])
    assert after == before


def test_workspace_star_export_keeps_original_resolution(tmp_path):
    before, after = mark(
        tmp_path,
        {
            "tests/utils/__init__.py": "from .helpers import *\n",
            "tests/utils/helpers.py": "def login(): return 1\n",
        },
        "extsym:tests.utils.login",
        ["fn:tests.utils.helpers.login"],
        workspace=True,
    )
    assert after == before


def test_class_name_does_not_resolve_to_a_lowercase_module(tmp_path):
    before, after = mark(
        tmp_path,
        {
            "pkg/__init__.py": "from .client import Client\n",
            "pkg/client.py": "class Client:\n def get(self): return 1\n",
        },
        "extsym:pkg.Client.get",
        ["extsym:pkg.client.Client.get"],
    )
    assert after == before


@pytest.mark.parametrize(
    "facade,analyzer",
    [
        ("from pkg import run\n", "extsym:facade.run"),
        ("import pkg.run as module\n", "extsym:facade.module"),
    ],
)
def test_same_named_module_and_export_follow_the_import_form(
    tmp_path, facade, analyzer
):
    before, after = mark(
        tmp_path,
        {
            "facade.py": facade,
            "pkg/__init__.py": "from .run import run\n",
            "pkg/run.py": "def run(): return 1\n",
        },
        analyzer,
        ["extsym:pkg.run.run"],
    )
    assert after == before


def test_dotted_import_alias_does_not_invent_a_module_for_a_function_export(tmp_path):
    _, after = mark(
        tmp_path,
        {
            "facade.py": "import pkg.run as module\n",
            "pkg/__init__.py": "from .run import run\n",
            "pkg/run.py": "def run(): return 1\n",
        },
        "extsym:facade.module.run",
        ["extsym:pkg.run.run"],
    )
    assert after["resolution"]["status"] == "conflict"
    assert "non_class_member" in after["resolution"]["detail"]


def test_inherited_member_uses_c3_without_overrides(tmp_path):
    before, after = mark(
        tmp_path,
        {
            "base.py": "class A:\n def get(self): return 1\nclass B(A): pass\nclass C(A): pass\n",
            "pkg.py": "from base import B, C\nclass Client(B, C): pass\n",
        },
        "extsym:pkg.Client.get",
        ["extsym:base.A.get"],
    )
    assert after == before


@pytest.mark.parametrize(
    "text,reason",
    [
        (
            "def __getattr__(name): return None\n",
            "module_getattr",
        ),
        ("if dynamic():\n from impl import run\n", "conditional_binding"),
        ("from other import run\n", "import_cycle"),
        ("from impl import run\nrun = replacement\n", "assignment_binding"),
        (
            "from impl import run\nnamespace = globals()\nnamespace['run'] = replacement\n",
            "assignment_binding",
        ),
        (
            "from impl import run\nnamespace = globals()\nnamespace.update(run=replacement)\n",
            "assignment_binding",
        ),
        (
            "from impl import run\nglobals()['run'] = replacement\n",
            "assignment_binding",
        ),
        ("from impl import run\nexec('run = replacement')\n", "assignment_binding"),
        (
            "from impl import run\ndef mutate():\n global run\n run = replacement\nmutate()\n",
            "nested_namespace_write",
        ),
        (
            "import sys\nsys = replacement\nif sys.platform == 'linux':\n from impl import run\n",
            "conditional_binding",
        ),
        (
            "import sys\nsys.platform = 'win32'\nif sys.platform == 'linux':\n from impl import run\n",
            "conditional_binding",
        ),
        (
            "import sys\nsetattr(sys, 'platform', 'win32')\nif sys.platform == 'linux':\n from impl import run\n",
            "conditional_binding",
        ),
        (
            "def run(): return 1\ndef run(): return 2\n",
            "multiple_definitions",
        ),
        (
            "from impl import Client\nclass Child(Client):\n def get(self): return 2\n",
            "different_implementation",
        ),
    ],
)
def test_unprovable_targets_retain_conflict_and_explain_step(tmp_path, text, reason):
    analyzer = "extsym:pkg.Child.get" if "Child" in text else "extsym:pkg.run"
    positive = "extsym:impl.Client.get" if "Child" in text else "extsym:impl.run"
    before, after = mark(
        tmp_path,
        {
            "pkg.py": text,
            "other.py": "from pkg import run\n",
            "impl.py": "def run(): return 1\nclass Client:\n def get(self): return 1\n",
        },
        analyzer,
        [positive],
    )
    assert after["resolution"]["status"] == "conflict"
    assert reason in after["resolution"]["detail"]
    after["resolution"] = before["resolution"]
    assert after == before


def test_target_version_platform_select_the_frozen_import(tmp_path):
    before, after = mark(
        tmp_path,
        {
            "pkg.py": "import sys\nif sys.version_info >= (3, 12) and sys.platform == 'linux':\n from impl import run\nelse:\n from other import run\n",
            "impl.py": "def run(): return 1\n",
            "other.py": "def run(): return 2\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after == before


def test_aggregate_marks_only_unproved_site_in_detail(tmp_path):
    before, after = mark(
        tmp_path,
        {
            "pkg.py": "from impl import run\n",
            "impl.py": "def run(): return 1\n",
            "other.py": "def run(): return 2\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
        extra_site="extsym:other.run",
    )
    assert after["resolution"]["status"] == "conflict"
    assert "other()" in after["resolution"]["detail"]
    assert "alias()" not in after["resolution"]["detail"]
    after["resolution"] = before["resolution"]
    assert after == before


@pytest.mark.parametrize(
    "base,child,reason",
    [
        (
            "class Base:\n def get(self): return 1\n",
            "class Child(Base):\n get = replacement\n",
            "assignment_binding",
        ),
        (
            "class Base:\n def get(self): return 1\n",
            "class Child(factory()): pass\n",
            "dynamic_base",
        ),
        (
            "class Base:\n def get(self): return 1\n",
            "class Child(Base, metaclass=Factory): pass\n",
            "dynamic_class",
        ),
        (
            "class Base:\n def get(self): return 1\n",
            "class Child(Base): pass\nsetattr(Child, 'get', replacement)\n",
            "assignment_binding",
        ),
    ],
)
def test_member_proof_rejects_overwrites_and_dynamic_mro(tmp_path, base, child, reason):
    before, after = mark(
        tmp_path,
        {"base.py": base, "pkg.py": "from base import Base\n" + child},
        "extsym:pkg.Child.get",
        ["extsym:base.Base.get"],
    )
    assert after["resolution"]["status"] == "conflict"
    assert reason in after["resolution"]["detail"]
    after["resolution"] = before["resolution"]
    assert after == before


def test_dynamic_all_cannot_prove_a_star_export(tmp_path):
    _, after = mark(
        tmp_path,
        {
            "pkg.py": "from impl import *\n",
            "impl.py": "__all__ = exports()\ndef run(): return 1\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after["resolution"]["status"] == "conflict"
    assert "dynamic_all" in after["resolution"]["detail"]


def test_import_chain_has_a_deterministic_bound(tmp_path):
    files = {f"m{i}.py": f"from m{i+1} import run\n" for i in range(70)}
    files["m70.py"] = "def run(): return 1\n"
    _, after = mark(tmp_path, files, "extsym:m0.run", ["extsym:m70.run"])
    assert after["resolution"]["status"] == "conflict"
    assert "proof_limit" in after["resolution"]["detail"]


@pytest.mark.parametrize(
    "mutation",
    [
        "__all__.remove('run')",
        "__all__[0] = 'other'",
        "exports = __all__\nexports.clear()",
    ],
)
def test_literal_all_mutation_or_escape_cannot_prove_a_star_export(tmp_path, mutation):
    _, after = mark(
        tmp_path,
        {
            "pkg.py": "from impl import *\n",
            "impl.py": "__all__ = ['run']\ndef run(): return 1\n" + mutation + "\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after["resolution"]["status"] == "conflict"
    assert "dynamic_all" in after["resolution"]["detail"]


def test_symlink_outside_frozen_dependency_is_never_read(tmp_path):
    # A real symlink outside the allowed root is excluded by safe_file.
    files = {"pkg.py": "from impl import run\n"}
    _, after = mark(
        tmp_path, files, "extsym:pkg.run", ["extsym:impl.run"], dependency_link=True
    )
    assert after["resolution"]["status"] == "conflict"
    assert "frozen_module_unavailable" in after["resolution"]["detail"]
