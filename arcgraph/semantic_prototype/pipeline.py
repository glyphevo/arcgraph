"""Explicit, standalone frozen-source pipeline. Does not publish a product graph."""

import argparse
import json
from pathlib import Path
import time

from .anchors import AnchoredRecords, View
from .backends import producer, pyright_records, wrap_graph
from .contract import Snapshot, write_generation
from .lsp import Client
from .projection import project
from .structure_provider import analyze, admit_structure
from .snapshot import verify, sha


class Transcript:
    def __init__(self, handle):
        self.handle = handle

    def append(self, row):
        self.handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.handle.flush()


def existing_graph(snapshot, root):
    """Reuse product stages in memory, including the existing merge semantics."""
    from arcgraph.core.scanner import FileScanner, SourceRoot
    from arcgraph.core.structural import generate_structural_hierarchy
    from arcgraph.core.schemas import IndexMetadata
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.core.merge import EvidenceMergeEngine
    from arcgraph.pipeline.frontends import python_compat_pass_dispatcher

    indexer = ArcGraphIndexer(root, source_roots=[SourceRoot(".")])
    plan = indexer._build_plan()
    manifest = {f.path for f in snapshot.files}
    files = [
        f
        for f in FileScanner(root, [SourceRoot(".")], file_extensions={".py"}).scan()
        if f.path in manifest
    ]
    content = indexer.analyze_files(files, [], frontends=plan)
    nodes, edges = generate_structural_hierarchy(
        indexer.source_roots,
        [
            {
                "path": f.path,
                "source_root": f.source_root,
                "module": f.module,
                "is_package": f.is_package,
            }
            for f in content.files
        ],
        existing_nodes=content.nodes,
    )
    content.nodes.extend(nodes)
    content.edges.extend(edges)
    metadata = IndexMetadata(
        repo_root=str(root),
        commit_sha="prototype-frozen",
        index_version="prototype-shadow",
        source_roots=["."],
    )
    merged = EvidenceMergeEngine(
        root, dispatcher=python_compat_pass_dispatcher()
    ).merge_graph(metadata, content.nodes, content.edges, content.warnings)
    return {
        "nodes": [n.model_dump(mode="json") for n in merged.nodes],
        "edges": [e.model_dump(mode="json") for e in merged.edges],
        "warnings": [w.model_dump(mode="json") for w in content.warnings],
    }


def run(
    snapshot: Snapshot,
    root: Path,
    interpreter: str,
    peer,
    *,
    pyright_version: str,
    configuration: dict,
    environment=None,
    timeout=30,
    structure=None,
    graph=None,
):
    times = {}
    started = time.perf_counter()
    bundle = structure
    if bundle is None:
        bundle = analyze(
            snapshot,
            root,
            {snapshot.target_python: interpreter},
            environment=environment,
            access_facts=True,
        )
    allowed, reason = admit_structure(bundle, snapshot)
    if not allowed:
        raise ValueError(reason)
    view = View(bundle)
    times["structure"] = time.perf_counter() - started
    pb = producer("pyright", pyright_version, (snapshot.target_python,), configuration)
    from arcgraph import __version__
    import sys

    ab = producer(
        "arcgraph",
        __version__,
        (snapshot.target_python,),
        {
            "host_structural_failures": "explicit-unknown",
            "graph": "existing-merge",
            "host_python": sys.version,
            "wrapper_digest": sha(Path(__file__).read_bytes()),
            "anchors_digest": sha(Path(__file__).with_name("anchors.py").read_bytes()),
        },
    )
    started = time.perf_counter()
    primary, meta = pyright_records(
        snapshot, root, pb, view, peer, timeout=timeout, record_factory=AnchoredRecords
    )
    times["pyright"] = time.perf_counter() - started
    # The caller owns the peer; the real entry point releases its process before
    # the host analyzer starts. Replay peers have no process to release.
    if hasattr(peer, "close"):
        peer.close()
    started = time.perf_counter()
    raw = existing_graph(snapshot, root) if graph is None else graph
    fallback = wrap_graph(snapshot, root, ab, view, raw, record_factory=AnchoredRecords)
    times["arcgraph"] = time.perf_counter() - started
    started = time.perf_counter()
    shadow = project(view, primary, fallback, (pb, ab))
    times["admission_projection"] = time.perf_counter() - started
    verify(snapshot, root)
    return (
        bundle,
        primary,
        fallback,
        shadow,
        {"stages_seconds": times, "pyright": meta},
        raw,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--interpreter", required=True)
    parser.add_argument("--pyright", required=True)
    parser.add_argument("--pyright-version", required=True)
    parser.add_argument("--pyright-heap-mib", type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--in-process", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    import os

    snapshot = Snapshot.model_validate_json(args.snapshot.read_bytes())
    verify(snapshot, args.root)
    if "pyrightconfig.json" not in {f.path for f in snapshot.files}:
        raise ValueError("frozen pyright configuration required")
    if args.pyright_heap_mib is not None and args.pyright_heap_mib < 128:
        raise ValueError("pyright heap must be at least 128 MiB")
    config = json.loads((args.root / "pyrightconfig.json").read_text(encoding="utf-8"))
    config["prototype_node_heap_mib"] = args.pyright_heap_mib or "runtime-default"
    environment = dict(os.environ)
    if args.pyright_heap_mib is not None:
        environment["NODE_OPTIONS"] = f"--max-old-space-size={args.pyright_heap_mib}"
    config["prototype_node_options_digest"] = sha(
        environment.get("NODE_OPTIONS", "").encode()
    )
    if not args.in_process:
        import sys

        # Replace this interpreter too: retaining its imported graph schemas
        # while the transport runs would spend memory without doing work.
        os.execve(
            sys.executable,
            [
                sys.executable,
                str(Path(__file__).with_name("staged.py")),
                *(sys.argv[1:] if argv is None else argv),
            ],
            environment,
        )
    args.output.mkdir(parents=True, exist_ok=True)
    transcript = (args.output / "lsp.jsonl").open("w", encoding="utf-8")
    peer = Client(
        [args.pyright, "--stdio"],
        args.root,
        environment,
        {"python": {"analysis": config}},
        transcript=Transcript(transcript),
    )
    try:
        bundle, primary, fallback, shadow, meta, raw = run(
            snapshot,
            args.root,
            args.interpreter,
            peer,
            pyright_version=args.pyright_version,
            configuration=config,
        )
    finally:
        if peer.process.poll() is None:
            peer.close()
        transcript.close()
    for name, generation in (
        ("structure", bundle),
        ("pyright", primary),
        ("arcgraph", fallback),
    ):
        write_generation(generation, args.output / (name + ".json"))
    for name, value in (("projection", shadow), ("run", meta), ("existing-graph", raw)):
        with (args.output / (name + ".json")).open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
