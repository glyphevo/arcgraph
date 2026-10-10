"""Binary results use a proven operator signature, never same-name guesses."""

import pytest

from arcgraph.tests.test_generator_and_dynamic_exports import _targets


@pytest.mark.parametrize("assigned", [True, False])
def test_binary_operator_annotation_preserves_result(tmp_path, assigned):
    body = (
        "    h=Headers()\n    h=h|Headers()\n    h.items()\n"
        if assigned
        else "    (Headers()|Headers()).items()\n"
    )
    targets = _targets(
        tmp_path,
        {
            "user": "class Headers:\n    def __or__(self, other) -> 'Headers': return self\n    def items(self): pass\ndef use():\n"
            + body
        },
        v2=True,
    )
    assert "method:pkg.user.Headers.items" in targets


@pytest.mark.parametrize("annotation", ["", " -> int", " -> Headers | int"])
def test_binary_operator_does_not_assume_same_operand_result(tmp_path, annotation):
    targets = _targets(
        tmp_path,
        {
            "user": "class Headers:\n    def items(self): pass\n    def __or__(self, other)"
            + annotation
            + ": return 1\ndef use():\n    h=Headers()\n    h=h|Headers()\n    h.items()\n"
        },
        v2=True,
    )
    assert "method:pkg.user.Headers.items" not in targets


@pytest.mark.parametrize("assigned", [True, False])
def test_binary_reflected_subclass_is_not_assumed_to_use_left_signature(
    tmp_path, assigned
):
    body = (
        "    h=Headers()|Other()\n    h.items()\n"
        if assigned
        else "    (Headers()|Other()).items()\n"
    )
    targets = _targets(
        tmp_path,
        {
            "user": "class Headers:\n    def items(self): pass\n    def __or__(self, other) -> 'Headers': return self\nclass Other(Headers):\n    def __ror__(self, other) -> int: return 1\ndef use():\n"
            + body
        },
        v2=True,
    )
    assert "method:pkg.user.Headers.items" not in targets


@pytest.mark.parametrize("prefix", ["async ", "@wrap\n    "])
@pytest.mark.parametrize("assigned", [True, False])
def test_binary_unproved_operator_result_is_not_called_as_declared(
    tmp_path, prefix, assigned
):
    body = (
        "    h=Headers()|Headers()\n    h.items()\n"
        if assigned
        else "    (Headers()|Headers()).items()\n"
    )
    source = (
        "def wrap(f): return lambda *a: 1\nclass Headers:\n    def items(self): pass\n    "
        + prefix
        + "def __or__(self, other) -> 'Headers': return self\ndef use():\n"
        + body
    )
    assert "method:pkg.user.Headers.items" not in _targets(
        tmp_path, {"user": source}, v2=True
    )


def test_operator_can_return_a_different_proven_class(tmp_path):
    source = "class Result:\n    def items(self): pass\nclass Headers:\n    def __or__(self, other) -> Result: return Result()\ndef use():\n    h=Headers()|Headers()\n    h.items()\n"
    assert "method:pkg.user.Result.items" in _targets(
        tmp_path, {"user": source}, v2=True
    )
