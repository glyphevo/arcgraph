"""One captured input and one structural parse for the semantic build path."""

from pathlib import Path
from contextlib import contextmanager
import json
import shutil
import subprocess
import sys
import tempfile
import time

from arcgraph import __version__
from arcgraph.core.project_environment import project_environment_path
from arcgraph.core.scanner import FileScanner
from arcgraph.core.schemas import Node
from arcgraph.core.graph_store import current_pointer_path, GraphStoreReader
from arcgraph.core.structure import StructureView
from arcgraph.core.utils import symlink_below, replace_text_file
from .anchors import View
from .backends import producer, pyright_records, wrap_graph
from .contract import Snapshot, Source, digest, write_generation
from .frozen import (
    environment_identity,
    freeze_dependencies,
    interpreter_identity,
    verify_environment,
    verify_interpreter,
)
from .graph_projection import project_graph, public_edges
from .journal import JournalReplay, compact_generation, dump, load, write_structure
from .projection import project
from .runtime import configuration, semantic_core_status
from .snapshot import canonical, native_position, sha, verify
from .structure_provider import admit_structure, analyze
from .transport import collect
from .external_targets import Registry
from .artifact_inputs import FrozenInputs

PROFILE = "semantic-core/1"
LEGACY_PROFILE = "legacy/1"
STORAGE = "semantic-generation/gzip-blobs/2"
FIX = "Run arcgraph setup semantic; install Node.js 14 or newer; check the project Python environment."


def generation_directory(build_dir):
    return build_dir.parent.parent / "semantic" / "generations" / build_dir.name


def freshness_reason(store, root):
    """Match the generation bound to this graph, including non-indexed inputs."""
    from arcgraph.core.utils import symlink_below

    directory = generation_directory(store.sqlite_path.parent)
    unavailable = (
        store.metadata.get("toolchain_status", {})
        .get("semantic_core", {})
        .get("status")
        == "unavailable"
    )
    request = store.sqlite_path.parent / "semantic-request.json"
    input_files = (
        [request]
        if unavailable
        else [directory / "snapshot.json", directory / "manifest.json"]
    )
    if any(
        symlink_below(store.sqlite_path.parent.parent.parent, path)
        for path in input_files
    ):
        return "semantic_generation_unsafe"
    try:
        snapshot = Snapshot.model_validate(
            json.loads(
                (request if unavailable else directory / "snapshot.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        from .artifact_inputs import freshness_reason as artifact_freshness

        artifact_reason = artifact_freshness(
            store.sqlite_path.parent,
            root,
            dict(snapshot.environment)["artifact_inputs"],
        )
        if artifact_reason is not None:
            return artifact_reason
        if not unavailable:
            manifest = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
            if manifest.get("storage_format") != STORAGE:
                return "semantic_generation_storage_unsupported: rebuild required"
            if (
                not manifest.get("complete")
                or manifest.get("revision") != store.metadata["index_version"]
                or manifest.get("snapshot") != snapshot.id
            ):
                return "semantic_generation_revision_mismatch"
            for name, checksum in manifest.get("artifacts", {}).items():
                path = directory / name
                if Path(name).name != name or symlink_below(
                    store.sqlite_path.parent.parent.parent, path
                ):
                    return "semantic_generation_unsafe"
                if sha(path.read_bytes()) != checksum:
                    return "semantic_generation_artifact_changed"
        for file in snapshot.files:
            if sha((root / file.path).read_bytes()) != file.raw_digest:
                return "semantic_snapshot_changed"
        metadata = (
            store.metadata.get("extractor_metadata", {})
            .get("python-v1-compat-shim", {})
            .get("dependency_environment", {})
        )
        explicit = metadata.get("explicit_path")
        environment = project_environment_path(
            root, Path(explicit) if explicit else None
        )
        expected = dict(snapshot.environment)
        if environment_identity(environment)[0] != expected["dependencies"]:
            return "semantic_dependency_changed"
        identity = interpreter_identity(root, environment)
        if any(expected.get(key) != value for key, value in identity.items()):
            return "semantic_interpreter_changed"
        return None
    except (OSError, ValueError, KeyError):
        return "semantic_generation_unreadable"


class SemanticBuild:
    """Private staging; no graph or active pointer changes until publication."""

    def __init__(self, indexer):
        started = time.perf_counter()
        self.indexer = indexer
        self.temporary = tempfile.TemporaryDirectory(prefix="arcgraph-build-semantic-")
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "source"
        self.root.mkdir()
        self.generation = self.directory / "generation"
        self.generation.mkdir()
        self.inputs = FrozenInputs(indexer)
        self.environment = project_environment_path(
            indexer.repo_root, indexer.project_environment
        )
        self.parser = StructureView(indexer.repo_root, indexer.project_environment)
        self.interpreter = interpreter_identity(indexer.repo_root, self.environment)
        self.status = semantic_core_status(
            "unavailable", reason="Semantic analysis has not completed.", fix=FIX
        )
        self.bundle = None
        self.snapshot = None
        self.timings = {}
        pointer = current_pointer_path(indexer.output_dir)
        if pointer is None:
            raise RuntimeError("Semantic publication pointer is unsafe")
        self.pointer = pointer
        self.previous_pointer = pointer.read_bytes() if pointer.exists() else None
        self.previous_generations = []
        registry = indexer.output_dir / "semantic" / "retained.json"
        if symlink_below(indexer.output_dir, registry):
            raise RuntimeError("Semantic retention registry is unsafe")
        if registry.is_file():
            self.previous_generations = json.loads(
                registry.read_text(encoding="utf-8")
            )["generations"]
        if self.previous_pointer is not None:
            previous = GraphStoreReader.from_current(indexer.output_dir)
            bound = previous.sqlite_path.parent / "semantic-registry.json"
            if symlink_below(indexer.output_dir, bound):
                raise RuntimeError("Semantic build registry is unsafe")
            if bound.is_file():
                self.previous_generations = json.loads(
                    bound.read_text(encoding="utf-8")
                )["generations"]
        self.configuration_bytes = {}
        for name in ("pyproject.toml", ".gitignore", "package.json", "tsconfig.json"):
            path = indexer.repo_root / name
            if path.is_file():
                raw = path.read_bytes()
                self.configuration_bytes[name] = raw
                (self.root / name).write_bytes(raw)

        self.timings["initialize"] = time.perf_counter() - started
        self.timings["total"] = self.timings["initialize"]

    @contextmanager
    def _phase(self, name):
        """Exclusive named work; the total is measured separately at entry points."""
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            self.timings[name] = self.timings.get(name, 0.0) + elapsed

    def close(self):
        self.temporary.cleanup()

    def capture(self, files):
        started = time.perf_counter()
        try:
            with self._phase("capture"):
                self._capture(files)
        except (OSError, ValueError, UnicodeError) as exc:
            self.status = semantic_core_status("unavailable", reason=str(exc), fix=FIX)
        finally:
            self.timings["total"] += time.perf_counter() - started

    def _capture(self, files):
        self.files = files
        auxiliary = FileScanner(
            self.indexer.repo_root,
            self.indexer.source_roots,
            self.indexer.ignore_rules,
            file_extensions=(".pyi", ".typed"),
        )
        auxiliary.capture_directory = self.root
        auxiliary_files = auxiliary.scan()
        dependency_digest = freeze_dependencies(
            self.environment, self.directory / "environment"
        )
        if environment_identity(self.environment)[0] != dependency_digest:
            raise ValueError("input_changed_during_run: dependency snapshot differs")
        self.dependency_digest = dependency_digest
        paths = {f.path for f in [*files, *auxiliary_files]} | set(
            self.configuration_bytes
        )
        sources = tuple(
            Source(
                path=name,
                raw_digest=sha((self.root / name).read_bytes()),
                text_digest=sha(canonical((self.root / name).read_bytes()).encode()),
            )
            for name in sorted(paths)
        )
        self.snapshot = Snapshot(
            files=sources,
            target_python=".".join(self.parser.status()["version"].split(".")[:2]),
            platform=sys.platform,
            environment=tuple(
                sorted(
                    {
                        "dependencies": dependency_digest,
                        "python_version": self.parser.status()["version"],
                        "search_profile": "frozen-site-packages/2",
                        "source_roots": digest(
                            [r.path for r in self.indexer.source_roots]
                        ),
                        "projection_profile": PROFILE,
                        "artifact_inputs": self.inputs.checksum,
                        **self.interpreter,
                    }.items()
                )
            ),
            module_names=tuple(
                sorted((f.path, f.module) for f in files if f.path.endswith(".py"))
            ),
        )
        for file in files:
            if sha((self.root / file.path).read_bytes()) != file.file_hash:
                raise ValueError(
                    "input_changed_during_run: captured source hash differs"
                )
        dump(self.snapshot.model_dump(mode="json"), self.generation / "snapshot.json")

    def structure_from_trees(self, trees, notices, hosted):
        if self.snapshot is None:
            return
        started = time.perf_counter()
        try:
            self.bundle = analyze(
                self.snapshot,
                self.root,
                structure_view=self.parser,
                parsed_files=trees,
                parse_notices=notices,
                hosted_paths=hosted,
            )
        except (OSError, ValueError) as exc:
            self.status = semantic_core_status("unavailable", reason=str(exc), fix=FIX)
        elapsed = time.perf_counter() - started
        self.timings["structure_records"] = elapsed
        self.timings["total"] += elapsed

    def before_parse(self):
        started = time.perf_counter()
        with self._phase("interpreter_guard"):
            verify_interpreter(
                self.indexer.repo_root, self.interpreter, self.environment
            )
        self.timings["total"] += time.perf_counter() - started

    def apply(self, content):
        """Admit before changing any graph models; failed attempts retain fallback."""
        started = time.perf_counter()
        if self.bundle is None:
            if self.status["reason"] == "Semantic analysis has not completed.":
                self.status = semantic_core_status(
                    "unavailable",
                    reason="No Python structural generation was produced.",
                    fix=FIX,
                )
            return self._finish(content, started)
        with self._phase("structure_admission"):
            allowed, reason = admit_structure(self.bundle, self.snapshot)
        if not allowed:
            self.status = semantic_core_status("unavailable", reason=reason, fix=FIX)
            return self._finish(content, started)
        try:
            with self._phase("request_plan"):
                view = View(self.bundle)
                config = configuration(
                    self.root,
                    self.parser.status()["version"],
                    self.directory / "environment",
                    source_roots=[r.path for r in self.indexer.source_roots],
                )
                wrapper = {
                    p.name: sha(p.read_bytes())
                    for p in Path(__file__).parent.glob("*.py")
                }
                primary_producer = producer(
                    "pyright",
                    "1.1.414",
                    (self.snapshot.target_python,),
                    {"configuration": config, "wrapper": wrapper},
                )
                fallback_producer = producer(
                    "arcgraph",
                    __version__,
                    (self.snapshot.target_python,),
                    {"graph": "captured-build", "wrapper": wrapper},
                )
                texts = {
                    f.path: canonical((self.root / f.path).read_bytes())
                    for f in self.snapshot.files
                    if f.path.endswith(".py")
                }
                plan = {
                    "snapshot_id": self.snapshot.id,
                    "definitions": [
                        {
                            "path": d.path,
                            "positions": {
                                unit: native_position(
                                    texts[d.path], d.name_span.start, unit
                                )
                                for unit in ("utf-8", "utf-16")
                            },
                        }
                        for d in view.definitions
                        if d.symbol_kind in ("function", "class")
                    ],
                }
                dump(plan, self.generation / "plan.json")
            with self._phase("transport"):
                detail = collect(
                    self.generation / "plan.json", self.root, config, self.generation
                )
            with self._phase("external_registry"):
                if detail["requests"]["error"] or detail["requests"]["timeout"]:
                    raise ValueError(
                        "Pyright requests failed or timed out; retaining the existing analyzer results."
                    )
                view.external = Registry(
                    load(self.generation / "external-targets.json"),
                    detail["external_capture"],
                    public_symbols=[
                        n.id for n in content.nodes if n.kind == "external_symbol"
                    ],
                    snapshot=self.snapshot,
                )
                primary_producer = producer(
                    "pyright",
                    "1.1.414",
                    (self.snapshot.target_python,),
                    {
                        "configuration": config,
                        "wrapper": wrapper,
                        "external_capture": detail["external_capture"],
                    },
                )
            with self._phase("primary_mapping"):
                replay = JournalReplay(
                    self.generation / "responses.jsonl", self.snapshot.id
                )
                try:
                    primary, mapping = pyright_records(
                        self.snapshot, self.root, primary_producer, view, replay
                    )
                finally:
                    replay.close()
            with self._phase("graph_capture"):
                python_nodes = [
                    n.model_dump(mode="json")
                    for n in content.nodes
                    if n.path and n.path.endswith(".py")
                ]
                ids = {n["id"] for n in python_nodes}
                raw = {
                    "nodes": python_nodes,
                    "edges": [
                        e.model_dump(mode="json")
                        for e in content.edges
                        if e.source in ids
                        and (
                            e.kind
                            in ("calls", "constructs", "dynamic_call", "initializes")
                            or (
                                e.kind == "uses"
                                and e.target.startswith("extsym:")
                                and e.properties.get("callsite")
                            )
                        )
                    ],
                }
                from .public_owners import materialize_owners

                raw = materialize_owners(view, raw)
                known_nodes = {n.id: n for n in content.nodes}
                for target in view.external.definitions.values():
                    # Registry definitions are proven external identities, including
                    # .py declarations; the projection retains their ledger relation.
                    raw["nodes"].append(
                        (
                            known_nodes[target.id]
                            if target.id in known_nodes
                            else Node(
                                id=target.id,
                                kind="external_symbol",
                                name=target.name,
                                qualname=target.qualname,
                                properties={
                                    "source": "semantic_core",
                                    "boundary": "external",
                                },
                            )
                        ).model_dump(mode="json")
                    )
            with self._phase("fallback_mapping"):
                fallback = wrap_graph(
                    self.snapshot, self.root, fallback_producer, view, raw
                )
            decisions = project(
                view,
                primary,
                fallback,
                (primary_producer, fallback_producer),
                timings=self.timings,
            )
            with self._phase("graph_bridge"):
                if not decisions["admission"][0]["allowed"]:
                    raise ValueError(decisions["admission"][0]["reason"])
                graph, bridge = project_graph(
                    view,
                    raw,
                    fallback,
                    decisions,
                    primary_producer=primary.producer.id,
                    primary=primary,
                    model_output=True,
                )
            with self._phase("input_verification"):
                self.verify()
            with self._phase("release_mapping_indexes"):
                del raw["edges"]
                del view, texts, plan, replay
            with self._phase("generation_write"):
                write_structure(self.bundle, self.generation / "structure.jsonl")
                write_generation(primary, self.generation / "pyright.json")
                write_generation(fallback, self.generation / "fallback.json")
                dump(decisions, self.generation / "projection.json")
                dump(bridge, self.generation / "bridge.json")
                dump(mapping, self.generation / "mapping.json")
            with self._phase("generation_storage"):
                compact_generation(self.generation, self.snapshot, self.root)
            with self._phase("generation_manifest"):
                dump(
                    {
                        "revision": self.indexer.semantic_revision,
                        "snapshot": self.snapshot.id,
                        "projection_profile": PROFILE,
                        "storage_format": STORAGE,
                        "complete": True,
                        "artifacts": {
                            p.name: sha(p.read_bytes())
                            for p in self.generation.iterdir()
                            if p.is_file()
                        },
                    },
                    self.generation / "manifest.json",
                )
            with self._phase("graph_adoption"):
                out_of_scope = [
                    e
                    for e in content.edges
                    if not (
                        e.source in ids
                        and (
                            e.kind
                            in ("calls", "constructs", "dynamic_call", "initializes")
                            or (
                                e.kind == "uses"
                                and e.target.startswith("extsym:")
                                and e.properties.get("callsite")
                            )
                        )
                    )
                ]
                content.edges = public_edges([*out_of_scope, *graph])
                projected_ids = {n["id"] for n in raw["nodes"]}
                content.nodes = [
                    n for n in content.nodes if n.id not in projected_ids
                ] + [Node.model_validate(n) for n in raw["nodes"]]
                uncovered = [f for f in self.bundle.files if f.status != "parsed"]
                self.status = semantic_core_status(
                    "partial" if uncovered else "active",
                    node=detail["node"],
                    reason=(
                        (
                            str(len(uncovered))
                            + " Python file(s) not covered: "
                            + ", ".join(sorted({f.status for f in uncovered}))
                        )
                        if uncovered
                        else None
                    ),
                    fix=FIX if uncovered else None,
                )
                self.adopted = True
            with self._phase("release_records"):
                self.bundle = None
                del primary, fallback, decisions, mapping, bridge
            with self._phase("artifact_hashes"):
                self.generation_hashes = {
                    p.name: sha(p.read_bytes())
                    for p in self.generation.iterdir()
                    if p.is_file()
                }
        except (
            RuntimeError,
            OSError,
            ValueError,
            TimeoutError,
            subprocess.SubprocessError,
        ) as exc:
            self.status = semantic_core_status("unavailable", reason=str(exc), fix=FIX)
        return self._finish(content, started)

    def _finish(self, content, started):
        tick = time.perf_counter()
        candidates = [
            self.indexer.scip_index_path,
            self.indexer.pyright_export_path,
            ".scip/index.json",
            "scip-index.json",
            "output/arcgraph/scip-index.json",
            self.indexer.output_dir / "scip-index.json",
            self.indexer.output_dir / "pyright-export.json",
        ]
        if any(
            (self.indexer.repo_root / path).is_file() for path in candidates if path
        ):
            note = "Unverified precision_scip / precision_pyright evidence is retained but excluded from the graph; use the built-in semantic core with captured inputs."
            self.status["reason"] = (
                self.status["reason"] + " " if self.status["reason"] else ""
            ) + note
        content.toolchain_status["semantic_core"] = self.status
        self.timings["finalize"] = time.perf_counter() - tick
        self.timings["total"] += time.perf_counter() - started
        content.phase_timings["semantic_core"] = self.timings
        return content

    def verify(self):
        verify(self.snapshot, self.root)
        verify_environment(self.directory / "environment", self.dependency_digest)
        verify_interpreter(self.indexer.repo_root, self.interpreter, self.environment)

    def publish(self, build_dir):
        self.inputs.verify()
        self.inputs.persist(build_dir)
        dump({"projection_profile": PROFILE}, build_dir / "projection-profile.json")
        if self.snapshot is not None:
            dump(
                self.snapshot.model_dump(mode="json"),
                build_dir / "semantic-request.json",
            )
        self.retained = list(self.previous_generations)
        if getattr(self, "adopted", False):
            self.verify()
            if {
                p.name: sha(p.read_bytes())
                for p in self.generation.iterdir()
                if p.is_file()
            } != self.generation_hashes:
                raise ValueError(
                    "input_changed_during_run: semantic generation artifact changed"
                )
            destination = generation_directory(build_dir)
            if symlink_below(self.indexer.output_dir, destination):
                raise RuntimeError("Semantic generation path is unsafe")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(self.generation, destination)
            self.retained = [build_dir.name, *self.retained][:2]
        dump(
            {"revision": build_dir.name, "generations": self.retained},
            build_dir / "semantic-registry.json",
        )
        observed = self.pointer.read_bytes() if self.pointer.exists() else None
        if observed != self.previous_pointer:
            raise RuntimeError("Semantic publish revision changed; rebuild required")

    def after_publish(self):
        """Prune only after the graph's authoritative registry was published."""
        root = self.indexer.output_dir / "semantic"
        root.mkdir(parents=True, exist_ok=True)
        replace_text_file(
            root / "retained.json",
            json.dumps({"generations": self.retained}, sort_keys=True),
        )
        generations = root / "generations"
        if generations.is_dir():
            for directory in generations.iterdir():
                if directory.name in self.retained or not directory.is_dir():
                    continue
                if symlink_below(self.indexer.output_dir, directory):
                    continue
                if any(p.is_symlink() for p in directory.rglob("*")):
                    continue
                shutil.rmtree(directory)
