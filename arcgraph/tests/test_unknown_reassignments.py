"""Unknown writes invalidate inferred values without creating a name guess."""

import pytest

from arcgraph.tests.test_flow_attribute_chains import _calls


@pytest.mark.parametrize(
    "initial",
    ["p / 'a'", "p", "Path('a')"],
)
@pytest.mark.parametrize("alias", [False, True])
def test_unknown_write_erases_unannotated_local_type(tmp_path, initial, alias):
    call = "r.exists()" if alias else "q.exists()"
    body = "r=q\n    r.exists()" if alias else call
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path, other):\n"
        f"    q={initial}\n    q=other\n    {body}\n",
    )
    assert call not in calls


def test_unknown_write_then_known_write_recovers_type(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path, other):\n"
        "    q=p/'a'\n    q=other\n    q=p/'b'\n    q.exists()\n",
    )
    assert calls.get("q.exists()") == {"extsym:pathlib.Path.exists"}


@pytest.mark.parametrize("annotation", ["", ": Any"])
def test_declared_parameter_assigned_any_keeps_existing_behavior(tmp_path, annotation):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\nfrom typing import Any\n"
        f"def use(q: Path, other{annotation}):\n    q=other\n    q.exists()\n",
    )
    assert calls.get("q.exists()") == {"extsym:pathlib.Path.exists"}


def test_unknown_write_does_not_erase_an_earlier_call(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(p: Path, other):\n"
        "    q=p/'a'\n    q.exists()\n    q=other\n",
    )
    assert calls.get("q.exists()") == {"extsym:pathlib.Path.exists"}
