"""Private shadow harness. Does not register a CLI command or ship in the wheel."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import resource
import subprocess
import sys
import time

WRITE_INPUTS = (
    "python-edges.json",
    "python-nodes.json",
    "projection.json",
    "admission.json",
    "snapshot.json",
    "configuration.json",
    "environment.json",
    "structure.jsonl",
    "responses.jsonl",
    "pyright.json",
    "fallback.json",
    "wrapper.json",
)


def save(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def wrapper_identity():
    import hashlib

    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path(__file__).resolve().parents[1] / "arcgraph/semantic").glob(
            "*.py"
        )
    }


def verify_write_inputs(evidence):
    import hashlib

    expected = load(evidence / "write-inputs.json")
    if set(expected) != set(WRITE_INPUTS):
        raise RuntimeError("input_changed_during_run: invalid shadow artifact manifest")
    for name, checksum in expected.items():
        if hashlib.sha256((evidence / name).read_bytes()).hexdigest() != checksum:
            raise RuntimeError(
                "input_changed_during_run: shadow artifact changed: " + name
            )


def usage():
    return {
        kind: list(resource.getrusage(who))
        for kind, who in [
            ("self", resource.RUSAGE_SELF),
            ("children", resource.RUSAGE_CHILDREN),
        ]
    }


def stage_build(args, evidence):
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.semantic.frozen import interpreter_identity, verify_interpreter

    interpreter = interpreter_identity(args.repo_root, args.project_environment)
    metadata, build = ArcGraphIndexer(
        args.repo_root, args.output_dir, project_environment=args.project_environment
    ).build()
    verify_interpreter(args.repo_root, interpreter, args.project_environment)
    reader = GraphStoreReader.from_current(args.output_dir)
    reader.export_jsonl()
    save(evidence / "default-current.json", load(args.output_dir / "current.json"))
    save(
        evidence / "default.json",
        {
            "index_version": metadata.index_version,
            "build_dir": str(build),
            "python_status": metadata.toolchain_status["python_structure"],
            "interpreter": interpreter,
        },
    )


def stage_freeze(args, evidence):
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.semantic.frozen import freeze_build, verify_interpreter
    from arcgraph.semantic.runtime import configuration

    default = load(evidence / "default.json")
    verify_interpreter(args.repo_root, default["interpreter"], args.project_environment)
    reader = GraphStoreReader.from_build(args.output_dir, default["index_version"])
    snapshot, environment, detail = freeze_build(
        reader,
        args.repo_root,
        evidence / "snapshot",
        default["python_status"],
        environment=args.project_environment,
    )
    verify_interpreter(args.repo_root, default["interpreter"], args.project_environment)
    config = configuration(
        evidence / "snapshot",
        default["python_status"]["version"],
        environment,
        source_roots=reader.metadata["source_roots"],
    )
    save(evidence / "snapshot.json", snapshot.model_dump(mode="json"))
    save(evidence / "configuration.json", config)
    save(evidence / "environment.json", detail)
    save(evidence / "wrapper.json", wrapper_identity())


def stage_structure(args, evidence):
    from arcgraph.core.structure import StructureView
    from arcgraph.semantic.anchors import View
    from arcgraph.semantic.contract import Snapshot
    from arcgraph.semantic.journal import write_structure
    from arcgraph.semantic.snapshot import canonical, native_position
    from arcgraph.semantic.structure_provider import analyze
    from arcgraph.semantic.frozen import verify_interpreter

    environment = load(evidence / "environment.json")
    verify_interpreter(
        args.repo_root, environment["interpreter"], args.project_environment
    )
    snapshot = Snapshot.model_validate(load(evidence / "snapshot.json"))
    root = evidence / "snapshot"
    parser = StructureView(args.repo_root, args.project_environment)
    bundle = analyze(snapshot, root, structure_view=parser)
    verify_interpreter(
        args.repo_root, environment["interpreter"], args.project_environment
    )
    view = View(bundle)
    texts = {f.path: canonical((root / f.path).read_bytes()) for f in snapshot.files}
    plan = {
        "snapshot_id": snapshot.id,
        "definitions": [
            {
                "path": d.path,
                "positions": {
                    unit: native_position(texts[d.path], d.name_span.start, unit)
                    for unit in ("utf-8", "utf-16")
                },
            }
            for d in view.definitions
            if d.symbol_kind in ("class", "function")
        ],
    }
    write_structure(bundle, evidence / "structure.jsonl")
    save(evidence / "plan.json", plan)


def stage_transport(args, evidence):
    from arcgraph.semantic.contract import Snapshot
    from arcgraph.semantic.snapshot import verify
    from arcgraph.semantic.transport import collect
    from arcgraph.semantic.frozen import verify_environment

    snapshot = Snapshot.model_validate(load(evidence / "snapshot.json"))
    root = evidence / "snapshot"
    verify(snapshot, root)
    verify_environment(
        evidence / "environment",
        load(evidence / "environment.json")["dependency_digest"],
    )
    if load(evidence / "wrapper.json") != wrapper_identity():
        raise RuntimeError("input_changed_during_run: semantic wrapper changed")
    collect(
        evidence / "plan.json", root, load(evidence / "configuration.json"), evidence
    )
    verify(snapshot, root)
    verify_environment(
        evidence / "environment",
        load(evidence / "environment.json")["dependency_digest"],
    )


def stage_map(args, evidence):
    from arcgraph import __version__
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.semantic.anchors import View
    from arcgraph.semantic.backends import producer, pyright_records, wrap_graph
    from arcgraph.semantic.contract import Snapshot, write_generation
    from arcgraph.semantic.disk_structure import read
    from arcgraph.semantic.graph_projection import project_graph
    from arcgraph.semantic.journal import JournalReplay, intern_keys
    from arcgraph.semantic.projection import project
    from arcgraph.semantic.records import semantic_digest
    from arcgraph.semantic.snapshot import verify
    from arcgraph.semantic.frozen import verify_environment

    snapshot = Snapshot.model_validate(load(evidence / "snapshot.json"))
    root = evidence / "snapshot"
    verify(snapshot, root)
    verify_environment(
        evidence / "environment",
        load(evidence / "environment.json")["dependency_digest"],
    )
    view = View(read(evidence / "structure.jsonl"))
    wrapper = load(evidence / "wrapper.json")
    if wrapper != wrapper_identity():
        raise RuntimeError("input_changed_during_run: semantic wrapper changed")
    config = {
        "configuration": load(evidence / "configuration.json"),
        "wrapper": wrapper,
    }
    primary_producer = producer("pyright", "1.1.414", (snapshot.target_python,), config)
    fallback_producer = producer(
        "arcgraph",
        __version__,
        (snapshot.target_python,),
        {"graph": "default-build", "wrapper": wrapper},
    )
    replay = JournalReplay(evidence / "responses.jsonl", snapshot.id)
    primary, meta = pyright_records(snapshot, root, primary_producer, view, replay)
    replay.close()
    reader = GraphStoreReader.from_build(
        args.output_dir, load(evidence / "default.json")["index_version"]
    )
    # The full default graph remains in SQLite. The wrapper needs Python node
    # identities/callsites, not similarity profiles or TypeScript payloads.
    nodes = [
        {
            **n.model_dump(mode="json", exclude={"properties"}),
            "properties": {"callsites": n.properties.get("callsites", [])},
        }
        for n in reader.iter_nodes()
        if n.path and n.path.endswith(".py")
    ]
    node_ids = {n["id"] for n in nodes}
    edges = [
        intern_keys(e.model_dump(mode="json"))
        for e in reader.iter_edges()
        if e.source in node_ids and e.kind in ("calls", "constructs", "dynamic_call")
    ]
    raw = {
        "nodes": nodes,
        "edges": edges,
        "warnings": [w.model_dump(mode="json") for w in reader.read_warnings()],
    }
    fallback = wrap_graph(snapshot, root, fallback_producer, view, raw)
    tick = time.perf_counter()
    policy_timings = {}
    projection = project(
        view,
        primary,
        fallback,
        (primary_producer, fallback_producer),
        timings=policy_timings,
    )
    admission_projection_seconds = time.perf_counter() - tick
    write_generation(primary, evidence / "pyright.json")
    write_generation(fallback, evidence / "fallback.json")
    save(evidence / "admission.json", projection["admission"])
    save(evidence / "projection.json", projection)
    graph, bridge = project_graph(
        view, raw, fallback, projection, primary_producer=primary.producer.id
    )
    save(evidence / "python-edges.json", graph)
    save(evidence / "bridge.json", bridge)
    save(evidence / "python-nodes.json", sorted(node_ids))
    save(
        evidence / "records-summary.json",
        {
            "snapshot_id": snapshot.id,
            "primary": {
                "generation": primary.id,
                "checksum": primary.checksum,
                "semantic_digest": semantic_digest(primary),
                "records": primary.count,
                "raw": primary.raw_count,
            },
            "fallback": {
                "generation": fallback.id,
                "checksum": fallback.checksum,
                "semantic_digest": semantic_digest(fallback),
                "records": fallback.count,
                "raw": fallback.raw_count,
            },
            "pyright": meta,
            "admission_projection_seconds": admission_projection_seconds,
            "policy_seconds": policy_timings,
        },
    )
    verify(snapshot, root)
    import hashlib

    save(
        evidence / "write-inputs.json",
        {
            name: hashlib.sha256((evidence / name).read_bytes()).hexdigest()
            for name in WRITE_INPUTS
        },
    )


def stage_write(args, evidence):
    from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
    from arcgraph.core.metadata import make_index_version
    from arcgraph.core.schemas import Edge, IndexMetadata, MergeMetrics
    from arcgraph.core.semantic import semantic_facts_from_graph
    from arcgraph.semantic.contract import Snapshot
    from arcgraph.semantic.frozen import (
        environment_identity,
        verify_environment,
        verify_live_sources,
        verify_interpreter,
    )
    from arcgraph.semantic.snapshot import sha, verify

    environment = load(evidence / "environment.json")
    selected_environment = getattr(args, "project_environment", None)
    verify_interpreter(args.repo_root, environment["interpreter"], selected_environment)
    snapshot = Snapshot.model_validate(load(evidence / "snapshot.json"))
    verify_write_inputs(evidence)
    verify(snapshot, evidence / "snapshot")
    verify_environment(evidence / "environment", environment["dependency_digest"])
    verify_live_sources(snapshot, args.repo_root, environment["source_digest"])
    reader = GraphStoreReader.from_build(
        args.output_dir, load(evidence / "default.json")["index_version"]
    )
    if load(args.output_dir / "current.json") != load(
        evidence / "default-current.json"
    ):
        raise RuntimeError("Current build changed during shadow; refusing publication")
    files = reader.read_files()
    for file in files:
        if sha((args.repo_root / file.path).read_bytes()) != file.file_hash:
            raise RuntimeError(
                "input_changed_during_run: source changed before publication"
            )
    if (
        environment_identity(
            Path(environment["original_environment"])
            if environment["original_environment"]
            else None
        )[0]
        != environment["dependency_digest"]
    ):
        raise RuntimeError(
            "input_changed_during_run: dependency changed before publication"
        )
    metadata = IndexMetadata.model_validate(
        {k: v for k, v in reader.metadata.items() if k in IndexMetadata.model_fields}
    )
    metadata.index_version = make_index_version(metadata.commit_sha)
    node_ids = set(load(evidence / "python-nodes.json"))
    edges = [
        e
        for e in reader.iter_edges()
        if e.source not in node_ids
        or e.kind not in ("calls", "constructs", "dynamic_call")
    ]
    edges.extend(
        Edge.model_validate(row) for row in load(evidence / "python-edges.json")
    )
    nodes = reader.read_nodes()
    metadata.edge_count = len(edges)
    summary = reader.metadata

    class ShadowWriter(GraphStoreWriter):
        @contextmanager
        def _write_lock(self):
            with super()._write_lock():
                if load(args.output_dir / "current.json") != load(
                    evidence / "default-current.json"
                ):
                    raise RuntimeError(
                        "Current build changed during shadow; refusing publication"
                    )
                yield

        def _publish_current(self, metadata, build_dir):
            # Recheck at the transaction's final publication point as well.
            verify_interpreter(
                args.repo_root, environment["interpreter"], selected_environment
            )
            verify_write_inputs(evidence)
            verify_environment(
                evidence / "environment", environment["dependency_digest"]
            )
            verify_live_sources(snapshot, args.repo_root, environment["source_digest"])
            if load(evidence / "wrapper.json") != wrapper_identity():
                raise RuntimeError("input_changed_during_run: semantic wrapper changed")
            for file in files:
                if sha((args.repo_root / file.path).read_bytes()) != file.file_hash:
                    raise RuntimeError(
                        "input_changed_during_run: source changed before publication"
                    )
            if (
                environment_identity(
                    Path(environment["original_environment"])
                    if environment["original_environment"]
                    else None
                )[0]
                != environment["dependency_digest"]
            ):
                raise RuntimeError(
                    "input_changed_during_run: dependency changed before publication"
                )
            super()._publish_current(metadata, build_dir)

    build = ShadowWriter(args.output_dir).write(
        metadata,
        files,
        nodes,
        edges,
        reader.read_warnings(),
        semantic_facts=semantic_facts_from_graph(metadata, nodes, edges),
        diagnostics=reader.read_diagnostics(),
        merge_metrics=(
            MergeMetrics.model_validate(summary["merge_metrics"])
            if summary.get("merge_metrics")
            else None
        ),
        adapter_metrics=summary.get("adapter_metrics"),
        precision_metrics=summary.get("precision_metrics"),
        coverage_metrics=summary.get("coverage_metrics"),
        runtime_metrics=summary.get("runtime_metrics"),
        phase_timings=summary.get("phase_timings"),
    )
    save(
        evidence / "shadow-build.json",
        {"index_version": metadata.index_version, "build_dir": str(build)},
    )
    GraphStoreReader.from_current(args.output_dir).export_jsonl()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--project-environment", type=Path)
    parser.add_argument(
        "--stage",
        choices=("build", "freeze", "structure", "transport", "map", "write"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    args.repo_root = args.repo_root.resolve()
    args.output_dir = args.output_dir.resolve()
    if args.output_dir.is_relative_to(args.repo_root):
        raise RuntimeError(
            "Private shadow output must be outside repo-root to avoid indexing retained frozen sources"
        )
    evidence = args.output_dir / "semantic-shadow"
    if args.stage:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        started = time.perf_counter()
        globals()["stage_" + args.stage](args, evidence)
        save(
            evidence / (args.stage + "-usage.json"),
            {
                "seconds": time.perf_counter() - started,
                "rusage_raw": usage(),
                "rusage_maxrss_semantics": "largest single process, not a process-tree sum",
            },
        )
        return 0
    evidence.mkdir(parents=True, exist_ok=False)
    start = datetime.now(timezone.utc).isoformat()
    phases = {}
    for name in ("build", "freeze", "structure", "transport", "map", "write"):
        command = [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).resolve()),
            "--repo-root",
            str(args.repo_root),
            "--output-dir",
            str(args.output_dir),
            "--stage",
            name,
        ]
        if args.project_environment is not None:
            command += [
                "--project-environment",
                str(args.project_environment.resolve()),
            ]
        tick = time.perf_counter()
        with (
            (evidence / (name + "-stdout.txt")).open("w", encoding="utf-8") as out,
            (evidence / (name + "-stderr.txt")).open("w", encoding="utf-8") as err,
        ):
            subprocess.run(command, check=True, stdout=out, stderr=err)  # nosec B603
        phases[name] = time.perf_counter() - tick
    save(
        evidence / "run.json",
        {
            "started_utc": start,
            "ended_utc": datetime.now(timezone.utc).isoformat(),
            "stages_seconds": phases,
            "total_seconds": sum(phases.values()),
            "rusage_raw": usage(),
            "rusage_maxrss_semantics": "largest single process, not a process-tree sum",
            "complete": True,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
