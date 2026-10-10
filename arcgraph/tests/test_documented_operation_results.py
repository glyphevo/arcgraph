"""Documented results propagate through assignments and inline receivers."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def _edges(tmp_path, source):
    _targets(tmp_path, {"user": source}, v2=True)
    return [
        e
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use" and e.resolution.status == "resolved"
    ]


@pytest.mark.parametrize("operation", ["iterdir()", "glob('*')", "rglob('*')"])
def test_path_iterator_elements_keep_the_path_type(tmp_path, operation):
    source = f"from pathlib import Path\ndef use(p: Path):\n    for child in p.{operation}:\n        child.read_text().split()\n"
    edges = _edges(tmp_path, source)
    read = next(e for e in edges if e.target == "extsym:pathlib.Path.read_text")
    assert read.resolution.strategy == "external_receiver_type", [
        (e.target, e.resolution.strategy) for e in edges
    ]
    assert "extsym:builtins.str.split" in {e.target for e in edges}
    assert "extsym:builtins.bytes.split" not in {e.target for e in edges}


def test_path_parents_index_is_a_path_without_name_guessing(tmp_path):
    edges = _edges(
        tmp_path, "from pathlib import Path\ndef use(p: Path): p.parents[0].exists()\n"
    )
    assert any(
        e.target == "extsym:pathlib.Path.exists"
        and e.resolution.strategy == "external_receiver_type"
        for e in edges
    )


@pytest.mark.parametrize(
    "operation", ["sub('a', 'b', value)", "escape(value)", "subn('a','b',value)[0]"]
)
@pytest.mark.parametrize("annotation,owner", [("str", "str"), ("bytes", "bytes")])
def test_re_results_keep_the_input_string_type(tmp_path, operation, annotation, owner):
    if annotation == "bytes":
        operation = operation.replace("'a'", "b'a'").replace("'b'", "b'b'")
    edges = _edges(
        tmp_path,
        f"import re\ndef use(value: {annotation}):\n    result=re.{operation}\n    result.split()\n",
    )
    assert any(
        e.target == f"extsym:builtins.{owner}.split"
        and e.resolution.strategy == "builtin_receiver_type"
        for e in edges
    )


@pytest.mark.parametrize(
    "keywords,owner",
    [
        ("capture_output=True,text=True", "str"),
        ("capture_output=True,encoding='utf-8'", "str"),
        ("capture_output=True,universal_newlines=True", "str"),
        ("capture_output=True", "bytes"),
    ],
)
@pytest.mark.parametrize("member", ["stdout", "stderr"])
def test_completed_process_streams_follow_text_mode(tmp_path, keywords, owner, member):
    edges = _edges(
        tmp_path,
        f"import subprocess\ndef use():\n    result=subprocess.run(['echo'],{keywords})\n    result.{member}.split()\n",
    )
    assert any(
        e.target == f"extsym:builtins.{owner}.split"
        and e.resolution.strategy == "builtin_receiver_type"
        for e in edges
    )


@pytest.mark.parametrize("assigned", [False, True])
def test_sqlite_connect_names_connection_methods(tmp_path, assigned):
    body = (
        "db=sqlite3.connect(':memory:')\n    db.execute('select 1')"
        if assigned
        else "sqlite3.connect(':memory:').execute('select 1')"
    )
    edges = _edges(tmp_path, "import sqlite3\ndef use():\n    " + body + "\n")
    assert "extsym:sqlite3.Connection.execute" in {e.target for e in edges}
    assert "extsym:pep249.Cursor.execute" not in {e.target for e in edges}


def test_stable_outer_annotation_supplies_the_dictionary_value_type(tmp_path):
    edges = _edges(
        tmp_path,
        "TABLE: dict[str,tuple[str,str|None]]={}\ndef use():\n    result=TABLE.get('key')\n    if result is None: return\n    owner,element=result\n    owner.split(':')\n",
    )
    assert "extsym:builtins.str.split" in {e.target for e in edges}
    assert "extsym:builtins.bytes.split" not in {e.target for e in edges}


@pytest.mark.parametrize(
    "source",
    [
        "import re\ndef use(value):\n    result=re.sub('a','b',value)\n    result.split()\n",
        "import re\ndef use(re):\n    result=re.sub('a','b','text')\n    result.split()\n",
        "import sqlite3\ndef use(factory):\n    result=sqlite3.connect(':memory:',factory=factory)\n    result.execute('select 1')\n",
        "import sqlite3\ndef use(sqlite3):\n    result=sqlite3.connect(':memory:')\n    result.execute('select 1')\n",
        "import subprocess\ndef use(flag):\n    result=subprocess.run(['echo'],capture_output=True,text=flag)\n    result.stdout.split()\n",
        "import subprocess\ndef use():\n    result=subprocess.run(['echo'],text=True)\n    result.stdout.split()\n",
        "TABLE: dict[str,tuple[str,str|None]]={}\ndef use(TABLE):\n    result=TABLE.get('key')\n    if result is None: return\n    owner,element=result\n    owner.split(':')\n",
    ],
)
def test_unproven_or_shadowed_results_do_not_gain_static_types(tmp_path, source):
    edges = _edges(tmp_path, source)
    assert "extsym:sqlite3.Connection.execute" not in {e.target for e in edges}
    assert not any(
        e.confidence != "heuristic"
        and e.target
        in {"extsym:builtins.str.split", "extsym:sqlite3.Connection.execute"}
        for e in edges
    )


def test_documented_result_contracts_match_runtime(tmp_path):
    import re
    import sqlite3
    import subprocess
    import sys

    tmp_path.joinpath("child").write_text("data", encoding="utf-8")
    assert all(type(child) is type(tmp_path) for child in tmp_path.iterdir())
    assert type(tmp_path.parents[0]) is type(tmp_path)
    assert isinstance(re.sub("a", "b", "a"), str)
    assert isinstance(re.sub(b"a", b"b", b"a"), bytes)
    with sqlite3.connect(":memory:") as connection:
        assert type(connection) is sqlite3.Connection
        assert connection.execute("select 1").fetchone() == (1,)
    for text, expected in [(True, str), (False, bytes)]:
        result = subprocess.run(
            [sys.executable, "-c", "print('value')"],
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            check=True,
        )
        assert isinstance(result.stdout, expected)
        assert isinstance(result.stderr, expected)
