"""Name guesses never manufacture a missing link in a typed attribute chain."""

import pytest

from arcgraph.tests.test_attribute_binding_flow import _targets as attribute_targets
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def test_missing_attribute_does_not_guess_bytes_find(tmp_path):
    assert (
        attribute_targets(tmp_path, "        return self.matcher.inner.find(row)\n")
        == set()
    )


def test_typed_property_chain_preserves_its_return_annotation(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "class Leaf:\n    def find(self): pass\nclass Parent:\n    @property\n    def leaf(self) -> Leaf: return Leaf()\nclass Root(Parent): pass\ndef use(root: Root): root.leaf.find()\n"
        },
        v2=True,
    )
    assert "method:pkg.user.Leaf.find" in targets
    assert "extsym:builtins.bytes.find" not in targets


@pytest.mark.parametrize("other", ["Unrelated", "StructuralGeneration"])
def test_unrelated_same_method_does_not_hide_named_receiver(tmp_path, other):
    targets = _targets(
        tmp_path,
        {
            "user": f"class Generation:\n    def checked(self): pass\nclass {other}:\n    def checked(self): pass\ndef use(generation): generation.checked()\n"
        },
        v2=True,
    )
    assert "method:pkg.user.Generation.checked" in targets
    assert f"method:pkg.user.{other}.checked" not in targets


def test_chain_cannot_borrow_its_roots_class_name(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "class Generation:\n    def checked(self): pass\ndef use(generation): generation.inner.checked()\n"
        },
        v2=True,
    )
    assert "method:pkg.user.Generation.checked" not in targets


def test_equally_named_project_classes_remain_ambiguous(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "one": "class Generation:\n    def checked(self): pass\n",
            "two": "class Generation:\n    def checked(self): pass\n",
            "user": "def use(generation): generation.checked()\n",
        },
        v2=True,
    )
    assert not any(t.endswith("Generation.checked") for t in targets)


def test_unrelated_receiver_does_not_take_a_unique_method(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "class Generation:\n    def checked(self): pass\ndef use(foreign): foreign.checked()\n"
        },
        v2=True,
    )
    assert "method:pkg.user.Generation.checked" not in targets


def test_unknown_local_factory_result_does_not_take_its_variable_names_class(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "class Generation:\n    def checked(self): pass\nclass Other:\n    def checked(self): pass\ndef use(factory):\n    generation=factory()\n    generation.checked()\n"
        },
        v2=True,
    )
    assert "method:pkg.user.Generation.checked" not in targets
