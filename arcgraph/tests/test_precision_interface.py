from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.precision import (
    convert_scip_index_to_json,
    generate_pyright_json,
    generate_scip_python_json,
)


def test_convert_scip_index_to_json_uses_scip_print(tmp_path: Path) -> None:
    _write_fake_scip_print(tmp_path)
    (tmp_path / "index.scip").write_text("binary placeholder", encoding="utf-8")

    result = convert_scip_index_to_json(
        repo_root=tmp_path,
        scip_index_path="index.scip",
        output_path="output/scip-index.json",
        scip_command=sys.executable,
        source_roots=[SourceRoot("pkg")],
        commit_sha="abc123",
    )

    output = tmp_path / "output" / "scip-index.json"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "available"
    assert result["documents"] == 1
    assert result["commit_sha"] == "abc123"
    assert result["source_roots"] == ["pkg"]
    assert payload["arcgraph_metadata"]["commit_sha"] == "abc123"
    assert payload["arcgraph_metadata"]["source_roots"] == ["pkg"]
    assert payload["commit_sha"] == "abc123"
    assert payload["metadata"]["projectRoot"] == str(tmp_path)
    assert payload["documents"][0]["relativePath"] == "pkg/service.py"


def test_generate_scip_python_json_runs_indexer_then_converts(
    tmp_path: Path,
) -> None:
    _write_fake_scip_print(tmp_path)
    _write_fake_scip_python_index(tmp_path)

    result = generate_scip_python_json(
        repo_root=tmp_path,
        output_path="output/scip-index.json",
        index_file="output/index.scip",
        project_name="fixture-project",
        project_version="test-version",
        target_only=["pkg"],
        scip_python_command=sys.executable,
        scip_command=sys.executable,
        source_roots=[SourceRoot("pkg")],
        commit_sha="abc123",
    )

    assert result["status"] == "available"
    assert result["action"] == "scip_python_json_generated"
    assert result["project_name"] == "fixture-project"
    assert result["project_version"] == "test-version"
    assert result["target_only"] == ["pkg"]
    assert result["commit_sha"] == "abc123"
    assert result["source_roots"] == ["pkg"]
    assert (tmp_path / "output" / "index.scip").exists()
    assert not (tmp_path / "index.scip").exists()
    assert (tmp_path / "output" / "scip-index.json").exists()


def test_generate_scip_python_json_merges_multiple_targets(
    tmp_path: Path,
) -> None:
    _write_fake_scip_print_from_index(tmp_path)
    _write_fake_scip_python_index_from_target(tmp_path)

    result = generate_scip_python_json(
        repo_root=tmp_path,
        output_path="output/scip-index.json",
        index_file="output/index.scip",
        project_name="fixture-project",
        target_only=["pkg", "scripts"],
        scip_python_command=sys.executable,
        scip_command=sys.executable,
        source_roots=[SourceRoot("pkg"), SourceRoot("scripts")],
        commit_sha="abc123",
    )

    payload = json.loads(
        (tmp_path / "output" / "scip-index.json").read_text(encoding="utf-8")
    )
    paths = {document["relative_path"] for document in payload["documents"]}
    assert result["status"] == "available"
    assert result["documents"] == 2
    assert result["target_only"] == ["pkg", "scripts"]
    assert payload["arcgraph_metadata"]["commit_sha"] == "abc123"
    assert payload["arcgraph_metadata"]["source_roots"] == ["pkg", "scripts"]
    assert len(result["scip_indices"]) == 2
    assert (tmp_path / "output" / "index-pkg.scip").exists()
    assert (tmp_path / "output" / "index-scripts.scip").exists()
    assert paths == {"pkg/module.py", "scripts/module.py"}


def test_generate_pyright_json_emits_type_info_from_lsp_probe(
    tmp_path: Path,
) -> None:
    _write_fake_pyright_langserver(tmp_path)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text(
        "\n".join(
            [
                "class Payload:",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )
    (package / "service.py").write_text(
        "\n".join(
            [
                "from pkg.models import Payload",
                "",
                "def handle(payload: Payload) -> Payload:",
                "    local: Payload = payload",
                "    return local",
                "",
                "def builtin_only(values: list[str]) -> None:",
                "    return None",
            ]
        ),
        encoding="utf-8",
    )

    result = generate_pyright_json(
        repo_root=tmp_path,
        output_path="output/pyright-export.json",
        source_roots=[SourceRoot("pkg")],
        target_only=["pkg"],
        pyright_command=[sys.executable, str(tmp_path / "pyright_langserver.py")],
        commit_sha="abc123",
    )

    payload = json.loads(
        (tmp_path / "output" / "pyright-export.json").read_text(encoding="utf-8")
    )
    type_info = payload["type_info"]
    assert result["status"] == "available"
    assert result["pyright_status"] == "available"
    assert result["type_info"] >= 3
    assert result["pyright_lsp_probe_total"] >= 4
    assert result["pyright_lsp_requestable_probe_total"] >= 3
    assert result["pyright_lsp_skipped_unmappable_total"] >= 1
    assert (
        result["pyright_lsp_requests_total"]
        == result["pyright_lsp_requestable_probe_total"]
    )
    assert result["pyright_lsp_type_info_total"] >= 3
    assert payload["status"] == "available"
    assert payload["commit_sha"] == "abc123"
    assert payload["arcgraph_metadata"]["commit_sha"] == "abc123"
    assert payload["arcgraph_metadata"]["source_roots"] == ["pkg"]
    assert payload["lsp"]["status"] == "available"
    assert payload["lsp"]["probe_total"] >= 4
    assert payload["lsp"]["requestable_probe_total"] >= 3
    assert payload["lsp"]["skipped_unmappable_total"] >= 1
    assert payload["lsp"]["requests_total"] == payload["lsp"]["requestable_probe_total"]
    assert {item["name"] for item in type_info} >= {"payload", "return", "local"}
    assert all(item["target"] == "class:models.Payload" for item in type_info)
    assert all(item["source"] == "pyright_lsp" for item in type_info)
    assert all(item["evidence_kind"] == "pyright_lsp_type_info" for item in type_info)
    assert all(
        item["lsp_method"] == "textDocument/typeDefinition" for item in type_info
    )
    methods = json.loads(
        (tmp_path / "pyright-requests.json").read_text(encoding="utf-8")
    )
    assert "initialize" in methods
    assert "textDocument/didOpen" in methods
    assert "textDocument/typeDefinition" in methods


def test_pyright_annotation_probes_obey_project_exclude(tmp_path: Path) -> None:
    _write_fake_pyright_langserver(tmp_path)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "models.py").write_text("class Payload: pass\n", encoding="utf-8")
    for name in ("visible", "ignored"):
        (package / (name + ".py")).write_text(
            "from pkg.models import Payload\ndef handle(payload: Payload): return payload\n",
            encoding="utf-8",
        )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.arcgraph]\nexclude = ["pkg/ignored.py"]\n', encoding="utf-8"
    )
    result = generate_pyright_json(
        repo_root=tmp_path,
        output_path="output/pyright-export.json",
        source_roots=[SourceRoot("pkg")],
        pyright_command=[sys.executable, str(tmp_path / "pyright_langserver.py")],
    )
    payload = json.loads(
        (tmp_path / "output/pyright-export.json").read_text(encoding="utf-8")
    )
    assert result["type_info"] > 0
    assert {row["path"] for row in payload["type_info"]} == {"pkg/visible.py"}


def test_generate_pyright_json_uses_hover_fallback_from_lsp_probe(
    tmp_path: Path,
) -> None:
    _write_fake_pyright_langserver(tmp_path, resolve_method="hover")
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text("class Payload:\n    pass\n", encoding="utf-8")
    (package / "service.py").write_text(
        "\n".join(
            [
                "from pkg.models import Payload",
                "",
                "def handle(payload: Payload) -> Payload:",
                "    local: Payload = payload",
                "    return local",
            ]
        ),
        encoding="utf-8",
    )

    result = generate_pyright_json(
        repo_root=tmp_path,
        output_path="output/pyright-export.json",
        source_roots=[SourceRoot("pkg")],
        target_only=["pkg"],
        pyright_command=[sys.executable, str(tmp_path / "pyright_langserver.py")],
    )

    payload = json.loads(
        (tmp_path / "output" / "pyright-export.json").read_text(encoding="utf-8")
    )
    type_info = payload["type_info"]
    assert result["pyright_lsp_requests_total"] == (
        result["pyright_lsp_requestable_probe_total"] * 3
    )
    assert result["pyright_lsp_type_info_total"] >= 3
    assert all(item["lsp_method"] == "textDocument/hover" for item in type_info)
    methods = json.loads(
        (tmp_path / "pyright-requests.json").read_text(encoding="utf-8")
    )
    assert "textDocument/typeDefinition" in methods
    assert "textDocument/definition" in methods
    assert "textDocument/hover" in methods


def test_generate_pyright_json_uses_definition_fallback_from_lsp_probe(
    tmp_path: Path,
) -> None:
    _write_fake_pyright_langserver(tmp_path, resolve_method="definition")
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text("class Payload:\n    pass\n", encoding="utf-8")
    (package / "service.py").write_text(
        "from pkg.models import Payload\n\ndef handle(payload: Payload) -> None:\n    return None\n",
        encoding="utf-8",
    )

    result = generate_pyright_json(
        repo_root=tmp_path,
        output_path="output/pyright-export.json",
        source_roots=[SourceRoot("pkg")],
        target_only=["pkg"],
        pyright_command=[sys.executable, str(tmp_path / "pyright_langserver.py")],
    )

    payload = json.loads(
        (tmp_path / "output" / "pyright-export.json").read_text(encoding="utf-8")
    )
    assert result["pyright_lsp_type_info_total"] == 1
    assert payload["type_info"][0]["lsp_method"] == "textDocument/definition"
    methods = json.loads(
        (tmp_path / "pyright-requests.json").read_text(encoding="utf-8")
    )
    assert "textDocument/typeDefinition" in methods
    assert "textDocument/definition" in methods
    assert "textDocument/hover" not in methods


def test_missing_scip_command_error_mentions_windows_installer(tmp_path: Path) -> None:
    (tmp_path / "index.scip").write_text("binary placeholder", encoding="utf-8")

    try:
        convert_scip_index_to_json(
            repo_root=tmp_path,
            scip_index_path="index.scip",
            output_path="output/scip-index.json",
            scip_command="missing-scip-command-ArcGraph-test",
        )
    except RuntimeError as exc:
        message = str(exc)
    else:  # pragma: no cover - protects against a colliding command name.
        raise AssertionError("Expected missing SCIP command to fail.")

    assert "scripts\\install-arcgraph-precision-tools.ps1" in message
    assert "go install github.com/scip-code/scip/cmd/scip@v0.7.1" in message


def _write_fake_scip_print(repo_root: Path) -> None:
    script = repo_root / "print"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "payload = {",
                "    'metadata': {'projectRoot': sys.path[0]},",
                "    'documents': [",
                "        {",
                "            'relativePath': 'pkg/service.py',",
                "            'occurrences': [",
                "                {",
                "                    'symbol': 'scip-python python fixture 1.0 pkg/service.py/`make`().',",
                "                    'symbolRoles': 1,",
                "                    'range': [1, 0, 1, 4],",
                "                }",
                "            ],",
                "        }",
                "    ],",
                "}",
                "print(json.dumps(payload))",
            ]
        ),
        encoding="utf-8",
    )
    _make_executable(script)


def _write_fake_scip_print_from_index(repo_root: Path) -> None:
    script = repo_root / "print"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "from pathlib import Path",
                "target = Path(sys.argv[-1]).read_text(encoding='utf-8')",
                "payload = {",
                "    'metadata': {'projectRoot': sys.path[0]},",
                "    'documents': [{'relative_path': f'{target}/module.py', 'occurrences': []}],",
                "    'external_symbols': [{'symbol': f'external {target}'}],",
                "}",
                "print(json.dumps(payload))",
            ]
        ),
        encoding="utf-8",
    )
    _make_executable(script)


def _write_fake_scip_python_index(repo_root: Path) -> None:
    script = repo_root / "index"
    script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "assert '--project-name' in sys.argv",
                "assert 'fixture-project' in sys.argv",
                "assert '--project-version' in sys.argv",
                "assert 'test-version' in sys.argv",
                "assert '--target-only=pkg' in sys.argv",
                "Path('index.scip').write_text('binary placeholder', encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )
    _make_executable(script)


def _write_fake_scip_python_index_from_target(repo_root: Path) -> None:
    script = repo_root / "index"
    script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "target = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--target-only='))",
                "Path('index.scip').write_text(target, encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )
    _make_executable(script)


def _write_fake_pyright_langserver(
    repo_root: Path, *, resolve_method: str = "typeDefinition"
) -> None:
    script = repo_root / "pyright_langserver.py"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "from pathlib import Path",
                f"resolve_method = {resolve_method!r}",
                "methods = []",
                "target_uri = (Path.cwd() / 'pkg' / 'models.py').resolve().as_uri()",
                "def read_message():",
                "    headers = []",
                "    while True:",
                "        line = sys.stdin.buffer.readline()",
                "        if not line:",
                "            return None",
                "        if line in (b'\\r\\n', b'\\n'):",
                "            break",
                "        headers.append(line.decode('ascii'))",
                "    length = 0",
                "    for line in headers:",
                "        if line.lower().startswith('content-length:'):",
                "            length = int(line.split(':', 1)[1].strip())",
                "    body = sys.stdin.buffer.read(length)",
                "    return json.loads(body.decode('utf-8'))",
                "def write_response(response):",
                "    body = json.dumps(response, separators=(',', ':')).encode('utf-8')",
                "    sys.stdout.buffer.write(b'Content-Length: ' + str(len(body)).encode() + b'\\r\\n\\r\\n' + body)",
                "    sys.stdout.buffer.flush()",
                "while True:",
                "    message = read_message()",
                "    if message is None:",
                "        break",
                "    method = message.get('method')",
                "    if method:",
                "        methods.append(method)",
                "        Path('pyright-requests.json').write_text(json.dumps(methods), encoding='utf-8')",
                "    if method == 'exit':",
                "        break",
                "    if 'id' not in message:",
                "        continue",
                "    if method == 'initialize':",
                "        result = {'capabilities': {}}",
                "    elif method == 'shutdown':",
                "        result = None",
                "    elif method == 'textDocument/typeDefinition' and resolve_method == 'typeDefinition':",
                "        result = [{'uri': target_uri, 'range': {'start': {'line': 0, 'character': 6}, 'end': {'line': 0, 'character': 13}}}]",
                "    elif method == 'textDocument/definition' and resolve_method == 'definition':",
                "        result = [{'uri': target_uri, 'range': {'start': {'line': 0, 'character': 6}, 'end': {'line': 0, 'character': 13}}}]",
                "    elif method == 'textDocument/hover' and resolve_method == 'hover':",
                "        result = {'contents': {'kind': 'markdown', 'value': 'Payload'}}",
                "    else:",
                "        result = None",
                "    write_response({'jsonrpc': '2.0', 'id': message['id'], 'result': result})",
            ]
        ),
        encoding="utf-8",
    )
    _make_executable(script)


def _make_executable(path: Path) -> None:
    if os.name != "nt":
        path.chmod(path.stat().st_mode | 0o111)
