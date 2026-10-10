"""Inherited descriptor evidence stops at the first defining class."""

import pytest
from arcgraph.tests.test_generator_and_dynamic_exports import _targets

PREFIX = "import functools\ndef make(): return 1\nclass Leaf:\n    def find(self): pass\nclass Parent:\n    @property\n    def leaf(self) -> Leaf: return Leaf()\n"


@pytest.mark.parametrize(
    "body",
    [
        "leaf=make()",
        "def leaf(self): return 1",
        "@property\n    def leaf(self): return 1",
    ],
)
def test_child_binding_blocks_inherited_property_return(tmp_path, body):
    source = (
        PREFIX
        + "class Root(Parent):\n    "
        + body
        + "\ndef use(root: Root): root.leaf.find()\n"
    )
    assert "method:pkg.user.Leaf.find" not in _targets(
        tmp_path, {"user": source}, v2=True
    )


def test_inherited_cached_property_uses_existing_descriptor_evidence(tmp_path):
    source = (
        PREFIX.replace("@property", "@functools.cached_property")
        + "class Root(Parent): pass\ndef use(root: Root): root.leaf.find()\n"
    )
    assert "method:pkg.user.Leaf.find" in _targets(tmp_path, {"user": source}, v2=True)


def test_async_inherited_property_is_not_its_annotated_value(tmp_path):
    source = (
        PREFIX.replace("def leaf(self) -> Leaf", "async def leaf(self) -> Leaf")
        + "class Root(Parent): pass\ndef use(root: Root): root.leaf.find()\n"
    )
    assert "method:pkg.user.Leaf.find" not in _targets(
        tmp_path, {"user": source}, v2=True
    )
