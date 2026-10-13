"""External captures stay inside live trusted roots and pass shared admission."""

from contextlib import contextmanager
import json

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.semantic.contract import digest
from arcgraph.semantic.external_targets import Capture, Registry, definition, file_path
from arcgraph.semantic.journal import dump, load
from arcgraph.semantic import transport
from arcgraph.tests.test_batch30_build import configure, generation_directory
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def item(path, name="ping", line=1, column=8):
    span = {
        "start": {"line": line, "character": column},
        "end": {"line": line, "character": column + len(name)},
    }
    return {"uri": path.as_uri(), "name": name, "selectionRange": span, "range": span}


def dependency(tmp_path, suffix=".py"):
    root = tmp_path / "source"
    root.mkdir()
    environment = tmp_path / "environment"
    sites = environment / "lib/python3.14/site-packages"
    sites.mkdir(parents=True)
    path = sites / ("example" + suffix)
    path.write_text("class Client:\n    def ping(self): return 1\n", encoding="utf-8")
    config = {"venvPath": str(tmp_path), "venv": "environment"}
    return root, config, path


@pytest.mark.parametrize(
    "uri",
    [
        "https://host/a.py",
        "file://host/a.py",
        "file:///a%GG.py",
        "file:///a%FF.py",
        "file:///a%00.py",
        "file:///a.py?q=1",
        "file:///a.py#frag",
        "file:///a/%2e%2e/b.py",
        "file:relative.py",
    ],
)
def test_external_uri_rejects_ambiguous_or_nonlocal_input(uri):
    assert file_path(uri) is None


def test_capture_never_reads_outside_the_allowed_directory_or_through_links(
    tmp_path, monkeypatch
):
    root, config, path = dependency(tmp_path)
    outside = tmp_path / "secret.py"
    outside.write_text("secret", encoding="utf-8")
    link = path.parent / "link.py"
    link.symlink_to(outside)
    linked_dir = path.parent / "linked"
    linked_dir.symlink_to(tmp_path, target_is_directory=True)
    from arcgraph.semantic import external_targets

    original = external_targets.read_allowed
    reads = []

    def read(path, root):
        reads.append(path)
        return original(path, root)

    monkeypatch.setattr(external_targets, "read_allowed", read)
    capture = Capture(root, config)
    for value in (outside, link, linked_dir / "secret.py"):
        capture.item(item(value), {})
    assert reads == []
    capture.item(item(path), {})
    assert reads == [path]
    assert len(capture.files) == 1


def test_stub_identity_is_kept_as_declaration_and_never_aliases_an_implementation(
    tmp_path,
):
    root, config, path = dependency(tmp_path, ".pyi")
    capture = Capture(root, config)
    target = item(path)
    capture.item(target, {})
    result = definition(capture.files[path.as_uri()], target, "utf-16")
    assert result["id"] == "extsym:example.Client.ping"
    assert result["declaration_only"] is True
    row = dict(
        capture.files[path.as_uri()],
        origin="typeshed",
        relative="stdlib/collections/abc.pyi",
        module="collections.abc",
    )
    row["text"] = "class Mapping:\n    def get(self): ...\n"
    from arcgraph.semantic.snapshot import sha

    row["text_digest"] = sha(row["text"].encode())
    result = definition(row, item(path, "get"), "utf-16")
    assert result["id"] == "extsym:collections.abc.Mapping.get"
    assert result["declaration_only"] is True


def test_external_capture_changes_and_registry_corruption_are_rejected(tmp_path):
    root, config, path = dependency(tmp_path)
    capture = Capture(root, config)
    capture.item(item(path), {})
    Registry(capture.files, digest(capture.files))
    with pytest.raises(ValueError, match="checksum"):
        Registry(capture.files, "changed")
    path.write_text("class Client:\n    def ping(self): return 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="external definition changed"):
        capture.item(item(path), {})


def test_link_swap_after_boundary_check_cannot_redirect_the_opened_file(
    tmp_path, monkeypatch
):
    from arcgraph.semantic import external_targets

    root, config, path = dependency(tmp_path)
    outside = tmp_path / "secret.py"
    outside.write_text("secret", encoding="utf-8")
    original = external_targets.safe_file

    def swap(value, allowed):
        result = original(value, allowed)
        if result:
            value.unlink()
            value.symlink_to(outside)
        return result

    monkeypatch.setattr(external_targets, "safe_file", swap)
    capture = Capture(root, config)
    capture.item(item(path), {})
    assert capture.files == {}


def test_transport_captures_before_a_private_session_is_destroyed(
    tmp_path, monkeypatch
):
    root, config, path = dependency(tmp_path)
    (root / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    plan = output / "plan.json"
    dump(
        {
            "snapshot_id": "snapshot",
            "definitions": [
                {
                    "path": "demo.py",
                    "positions": {"utf-16": {"line": 0, "character": 4}},
                }
            ],
        },
        plan,
    )

    class Peer:
        stderr = []

        def request(self, method, params):
            if method == "initialize":
                return {"capabilities": {"callHierarchyProvider": True}}
            if method == "textDocument/prepareCallHierarchy":
                return [{"name": "f"}]
            return [{"to": item(path), "fromRanges": []}]

        def notify(self, *args):
            pass

    @contextmanager
    def session(*args, **kwargs):
        yield Peer(), {"node": "fixed"}
        path.unlink()

    monkeypatch.setattr(transport, "session", session)
    result = transport.collect(plan, root, config, output)
    files = load(output / "external-targets.json")
    assert not path.exists()
    assert result["external_capture"] == digest(files)
    assert (
        definition(files[path.as_uri()], item(path), "utf-16")["id"]
        == "extsym:example.Client.ping"
    )


def test_real_external_implementation_is_admitted_and_projected(prepared, tmp_path):
    project, environment, _, _ = prepared
    sites = next(environment.glob("lib/python*/site-packages"))
    package = sites / "example.py"
    package.write_text(
        "class Client:\n    def ping(self): return 1\n", encoding="utf-8"
    )
    (project / "demo.py").write_text(
        "from example import Client\ndef f(client: Client):\n    return client.ping()\n",
        encoding="utf-8",
    )
    configure(project)
    metadata, directory = ArcGraphIndexer(project, tmp_path / "out").build()
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    generation = generation_directory(directory)
    primary = load(generation / "pyright.json")
    assert any(
        c["target"] == "extsym:example.Client.ping"
        and c["semantics"] == "implementation"
        for a in primary["answers"]
        for c in a["candidates"]
    )
    mapping = primary["mapping"]
    assert primary["raw_count"] == len(mapping)
    assert any(
        m["target"] == "extsym:example.Client.ping" and m["stage"] == "mapped"
        for m in mapping
    )
    reader = GraphStoreReader.from_current(tmp_path / "out")
    assert any(
        e.source == "fn:demo.f"
        and e.target == "extsym:example.Client.ping"
        and e.semantic_role is None
        and any(x.kind == "type_inference" for x in e.evidence)
        for e in reader.read_edges()
    )
    assert (
        len([n for n in reader.read_nodes() if n.id == "extsym:example.Client.ping"])
        == 1
    )
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    assert "external-targets.json" in manifest["artifacts"]
