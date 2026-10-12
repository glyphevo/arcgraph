"""Exception constructor arguments do not narrow the NotImplemented convention."""

import pytest

from arcgraph.tests.test_semantic_core import make_view, find


@pytest.mark.parametrize(
    "exception",
    [
        "NotImplementedError(msg)",
        "NotImplementedError(factory())",
        "NotImplementedError(*values, **options)",
        "NotImplementedError(msg) from cause",
    ],
)
def test_nonconstant_notimplemented_with_concrete_override_is_declaration(
    tmp_path, exception
):
    source = f"class Base:\n    def run(self):\n        raise {exception}\nclass Concrete(Base):\n    def run(self): return 1\n"
    _, _, view = make_view(tmp_path, source=source)
    assert find(view, "Base.run").id in view.declaration_evidence
    assert find(view, "Concrete.run").id not in view.declaration_evidence


def test_nonconstant_notimplemented_without_concrete_override_stays_execution(tmp_path):
    source = "class Base:\n    def run(self): raise NotImplementedError(message)\nclass Other(Base):\n    def run(self): pass\n"
    _, _, view = make_view(tmp_path, source=source)
    assert find(view, "Base.run").id not in view.declaration_evidence
