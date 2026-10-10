"""Stable module Path values provide text results without guessing names."""

import pytest

from arcgraph.tests.test_documented_operation_results import _edges


@pytest.mark.parametrize(
    "value",
    [
        "Path(__file__)",
        "Path(__file__).resolve().parents[2]",
        "Path(__file__).parent / 'data'",
    ],
)
def test_stable_module_path_read_text_is_str(tmp_path, value):
    edges = _edges(
        tmp_path,
        f"from pathlib import Path\nROOT = {value}\ndef use():\n    text = (ROOT / 'file').read_text()\n    text.split()\n",
    )
    assert "extsym:builtins.str.split" in {e.target for e in edges}
    assert "extsym:builtins.bytes.split" not in {e.target for e in edges}


@pytest.mark.parametrize(
    "source",
    [
        "from pathlib import Path\nROOT=Path(__file__)\nROOT=other\ndef use():\n    text=ROOT.read_text()\n    text.split()\n",
        "from pathlib import Path\nROOT=Path(__file__)\ndef replace():\n    global ROOT\n    ROOT=other\ndef use():\n    text=ROOT.read_text()\n    text.split()\n",
        "from pathlib import Path\nROOT=Path(__file__)\ndef use(ROOT):\n    text=ROOT.read_text()\n    text.split()\n",
        "ROOT=ROOT\ndef use():\n    text=ROOT.read_text()\n    text.split()\n",
        "from pathlib import Path\nPath=other\nROOT=Path(__file__)\ndef use():\n    text=ROOT.read_text()\n    text.split()\n",
    ],
)
def test_unstable_or_shadowed_module_path_is_not_a_proof(tmp_path, source):
    edges = _edges(tmp_path, source)
    assert not any(
        e.target == "extsym:builtins.str.split"
        and e.resolution.strategy == "builtin_receiver_type"
        for e in edges
    )
