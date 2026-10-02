"""SQLite and JSONL storage for ArcGraph indexes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from arcgraph.core.ids import stable_callsite_subject_key
from arcgraph.core.operation_lock import arcgraph_pid_file_lock
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    Evidence,
    FileRecord,
    FactResolution,
    IndexMetadata,
    MergeMetrics,
    Node,
    SchemaMigrationRecord,
    SemanticDiagnostic,
    SemanticFact,
)
from arcgraph.core.sharing_retry import retry_sharing_violation


class GraphStoreWriter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def write(
        self,
        metadata: IndexMetadata,
        files: list[FileRecord],
        nodes: list[Node],
        edges: list[Edge],
        warnings: list[BuildWarning],
        *,
        semantic_facts: list[SemanticFact] | None = None,
        diagnostics: list[SemanticDiagnostic] | None = None,
        merge_metrics: MergeMetrics | None = None,
        schema_migrations: list[SchemaMigrationRecord] | None = None,
        adapter_metrics: dict[str, Any] | None = None,
        precision_metrics: dict[str, Any] | None = None,
        coverage_metrics: dict[str, Any] | None = None,
        runtime_metrics: dict[str, Any] | None = None,
        phase_timings: dict[str, Any] | None = None,
    ) -> Path:
        semantic_facts = list(semantic_facts or [])
        diagnostics = list(diagnostics or [])
        schema_migrations = list(schema_migrations or [])

        with self._write_lock():
            diagnostics = self._apply_diagnostic_lifecycle(metadata, diagnostics)
            build_dir = self.output_dir / "builds" / metadata.index_version
            build_dir.mkdir(parents=True, exist_ok=False)

            self._write_jsonl(build_dir / "semantic_facts.jsonl", semantic_facts)
            self._write_jsonl(build_dir / "nodes.jsonl", nodes)
            self._write_jsonl(build_dir / "edges.jsonl", edges)
            self._write_jsonl(build_dir / "diagnostics.jsonl", diagnostics)
            if merge_metrics is not None:
                self._write_json(build_dir / "merge_metrics.json", merge_metrics)
            self._write_sqlite(
                build_dir / "index.sqlite",
                metadata,
                files,
                nodes,
                edges,
                warnings,
                semantic_facts,
                diagnostics,
                merge_metrics,
                schema_migrations,
            )
            self._write_summary(
                build_dir / "summary.json",
                metadata,
                warnings,
                diagnostics,
                merge_metrics,
                adapter_metrics,
                precision_metrics,
                coverage_metrics,
                runtime_metrics,
                phase_timings,
            )
            self._publish_current(metadata, build_dir)
            return build_dir

    @contextmanager
    def _write_lock(self) -> Iterable[None]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.output_dir / "write.lock"

        def _write_lock_error(pid: int | None) -> RuntimeError:
            detail = f" (PID {pid})" if pid else ""
            return RuntimeError(f"ArcGraph writer lock exists: {lock_path}{detail}")

        with arcgraph_pid_file_lock(
            lock_path,
            error_factory=_write_lock_error,
        ):
            yield

    @staticmethod
    def _write_jsonl(
        path: Path,
        rows: Iterable[Node | Edge | SemanticFact | SemanticDiagnostic],
    ) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(
                    json.dumps(
                        row.model_dump(mode="json", exclude_none=True), sort_keys=True
                    )
                )
                handle.write("\n")

    @staticmethod
    def _write_json(path: Path, row: MergeMetrics) -> None:
        path.write_text(
            json.dumps(row.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _write_summary(
        path: Path,
        metadata: IndexMetadata,
        warnings: list[BuildWarning],
        diagnostics: list[SemanticDiagnostic],
        merge_metrics: MergeMetrics | None,
        adapter_metrics: dict[str, Any] | None = None,
        precision_metrics: dict[str, Any] | None = None,
        coverage_metrics: dict[str, Any] | None = None,
        runtime_metrics: dict[str, Any] | None = None,
        phase_timings: dict[str, Any] | None = None,
    ) -> None:
        payload = metadata.model_dump(mode="json")
        warning_truncated_count = max(0, len(warnings) - 200)
        diagnostic_truncated_count = max(0, len(diagnostics) - 200)
        truncated_count = warning_truncated_count + diagnostic_truncated_count
        payload["warnings"] = [
            warning.model_dump(mode="json", exclude_none=True)
            for warning in warnings[:200]
        ]
        payload["diagnostics"] = [
            diagnostic.model_dump(mode="json", exclude_none=True)
            for diagnostic in diagnostics[:200]
        ]
        payload["truncated_count"] = truncated_count
        payload["summary_truncation"] = {
            "warnings": warning_truncated_count,
            "diagnostics": diagnostic_truncated_count,
        }
        payload["diagnostic_lifecycle"] = (
            GraphStoreWriter._diagnostic_lifecycle_summary(
                metadata,
                diagnostics,
            )
        )
        if merge_metrics is not None:
            metrics_payload = merge_metrics.model_dump(mode="json")
            metrics_payload["truncated_count"] = (
                merge_metrics.truncated_count + truncated_count
            )
            payload["merge_metrics"] = metrics_payload
        if adapter_metrics:
            payload["adapter_metrics"] = adapter_metrics
        if precision_metrics:
            payload["precision_metrics"] = precision_metrics
        if coverage_metrics:
            payload["coverage_metrics"] = coverage_metrics
        if runtime_metrics:
            payload["runtime_metrics"] = runtime_metrics
        if phase_timings:
            payload["phase_timings"] = phase_timings
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _apply_diagnostic_lifecycle(
        self,
        metadata: IndexMetadata,
        diagnostics: list[SemanticDiagnostic],
    ) -> list[SemanticDiagnostic]:
        previous_diagnostics = self._read_previous_diagnostics()
        previous = {
            self._diagnostic_lifecycle_key(diagnostic): diagnostic
            for diagnostic in previous_diagnostics
        }
        # Same subject, any frontend. Used only when the frontend-aware lookup
        # misses, and only where the subject identifies one diagnostic.
        previous_by_subject: dict[tuple[Any, ...], SemanticDiagnostic | None] = {}
        for diagnostic in previous_diagnostics:
            key = self._diagnostic_lifecycle_key(diagnostic, include_frontend=False)
            previous_by_subject[key] = (
                None if key in previous_by_subject else diagnostic
            )
        # A previous record already claimed by an exact frontend-aware match in
        # this pass has not been re-attributed, so the subject fallback must not
        # hand it to a second diagnostic that merely shares repo/kind/path/line.
        # Doing so publishes a genuinely new diagnostic with another frontend's
        # age and an "existing" status.
        claimed = {
            key
            for key in (
                self._diagnostic_lifecycle_key(diagnostic) for diagnostic in diagnostics
            )
            if key in previous
        }
        # The fallback models a one-to-one re-attribution, so it is only well
        # defined when the subject identifies a single diagnostic on the
        # current side too. With two, nothing distinguishes the correction from
        # a genuinely new sibling, and handing the old lifecycle to whichever
        # is iterated first makes the published result depend on input order.
        current_subject_counts: dict[tuple[Any, ...], int] = {}
        for diagnostic in diagnostics:
            subject = self._diagnostic_lifecycle_key(diagnostic, include_frontend=False)
            current_subject_counts[subject] = current_subject_counts.get(subject, 0) + 1
        current: list[SemanticDiagnostic] = []
        for diagnostic in diagnostics:
            updated = diagnostic.model_copy(deep=True)
            updated.index_version = metadata.index_version
            previous_diagnostic = previous.get(self._diagnostic_lifecycle_key(updated))
            if previous_diagnostic is None:
                # A diagnostic whose frontend attribution was corrected is the
                # same diagnostic; match it on its subject so lifecycle counters
                # do not reset on the upgrade build.
                subject_key = self._diagnostic_lifecycle_key(
                    updated, include_frontend=False
                )
                candidate = previous_by_subject.get(subject_key)
                if candidate is not None and (
                    self._diagnostic_lifecycle_key(candidate) in claimed
                    or current_subject_counts.get(subject_key, 0) > 1
                ):
                    candidate = None
                previous_diagnostic = candidate
            if previous_diagnostic is None:
                updated.first_seen_index = (
                    updated.first_seen_index or metadata.index_version
                )
                updated.seen_count = max(1, updated.seen_count)
                lifecycle_status = "new"
            else:
                updated.first_seen_index = (
                    previous_diagnostic.first_seen_index
                    or previous_diagnostic.index_version
                )
                updated.seen_count = max(1, previous_diagnostic.seen_count) + 1
                lifecycle_status = "existing"
            updated.last_seen_index = metadata.index_version
            updated.properties = {
                **updated.properties,
                "diagnostic_lifecycle_status": lifecycle_status,
            }
            current.append(updated)
        return current

    def _read_previous_diagnostics(self) -> list[SemanticDiagnostic]:
        try:
            return GraphStoreReader.from_current(self.output_dir).read_diagnostics()
        except (
            FileNotFoundError,
            KeyError,
            OSError,
            RuntimeError,
            sqlite3.DatabaseError,
            json.JSONDecodeError,
            ValueError,
        ):
            return []

    @staticmethod
    def _stable_callsite_lifecycle_subject(
        diagnostic: SemanticDiagnostic,
    ) -> str | None:
        """Return a position-independent subject for a callsite diagnostic.

        A diagnostic that names a stable callsite subject and its per-subject
        occurrence already identifies itself without its line number. Using it
        here keeps the lifecycle stable across edits that only move code. The
        value is deliberately not written to ``fact_id``: that field means a row
        in ``semantic_facts``, and Change Safety reads it as the diagnostic's
        identity subject, so borrowing it would silently change that contract.
        """

        properties = diagnostic.properties
        subject = properties.get("stable_callsite_subject")
        occurrence = properties.get("stable_callsite_occurrence")
        if not isinstance(subject, dict) or occurrence is None:
            return None
        return json.dumps(
            [*stable_callsite_subject_key(subject), occurrence],
            ensure_ascii=False,
        )

    @classmethod
    def _diagnostic_lifecycle_key(
        cls,
        diagnostic: SemanticDiagnostic,
        *,
        include_frontend: bool = True,
    ) -> tuple[str, str, str | None, str | None, str | None, str | None, int | None]:
        """Return a lifecycle key, optionally omitting corrected provenance."""

        fact_id = diagnostic.fact_id
        subject = (
            None if fact_id else cls._stable_callsite_lifecycle_subject(diagnostic)
        )
        positional = not (fact_id or subject)
        return (
            diagnostic.repo_id,
            diagnostic.diagnostic_kind,
            diagnostic.frontend_name if include_frontend else None,
            fact_id,
            subject,
            diagnostic.path if positional else None,
            diagnostic.start_line if positional else None,
        )

    @staticmethod
    def _diagnostic_lifecycle_summary(
        metadata: IndexMetadata,
        diagnostics: list[SemanticDiagnostic],
    ) -> dict[str, Any]:
        new_count = 0
        existing_count = 0
        by_kind: dict[str, dict[str, int]] = {}
        for diagnostic in diagnostics:
            lifecycle_status = (
                "new"
                if diagnostic.first_seen_index == metadata.index_version
                else "existing"
            )
            if lifecycle_status == "new":
                new_count += 1
            else:
                existing_count += 1
            bucket = by_kind.setdefault(
                diagnostic.diagnostic_kind,
                {"new": 0, "existing": 0},
            )
            bucket[lifecycle_status] += 1
        return {
            "index_version": metadata.index_version,
            "diagnostics_total": len(diagnostics),
            "new_diagnostics": new_count,
            "existing_diagnostics": existing_count,
            "retention_policy": {"recent_index_versions": 3},
            "dedupe_key": (
                "repo_id + diagnostic_kind + frontend_name + the first "
                "available of fact_id, stable callsite subject and "
                "occurrence, or path and start_line"
            ),
            "by_kind": dict(sorted(by_kind.items())),
        }

    def _publish_current(self, metadata: IndexMetadata, build_dir: Path) -> None:
        current_path = self.output_dir / "current.json"
        current_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": metadata.schema_version,
            "index_version": metadata.index_version,
            "build_dir": build_dir.relative_to(self.output_dir).as_posix(),
            "summary_path": f"{build_dir.relative_to(self.output_dir).as_posix()}/summary.json",
        }
        temp_path = current_path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        retry_sharing_violation(lambda: temp_path.replace(current_path))

    @staticmethod
    def _write_sqlite(
        path: Path,
        metadata: IndexMetadata,
        files: list[FileRecord],
        nodes: list[Node],
        edges: list[Edge],
        warnings: list[BuildWarning],
        semantic_facts: list[SemanticFact],
        diagnostics: list[SemanticDiagnostic],
        merge_metrics: MergeMetrics | None,
        schema_migrations: list[SchemaMigrationRecord],
    ) -> None:
        # ``sqlite3.Connection.__exit__`` commits or rolls back but never closes,
        # so a plain ``with sqlite3.connect(...)`` leaks the file handle until the
        # object is collected. ``ClosingSQLiteConnection`` keeps the transaction
        # semantics and closes on exit.
        with sqlite3.connect(path, factory=ClosingSQLiteConnection) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE index_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE files (
                    path TEXT PRIMARY KEY,
                    module TEXT NOT NULL,
                    source_root TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    line_count INTEGER NOT NULL,
                    file_size INTEGER NOT NULL,
                    is_package INTEGER NOT NULL
                );
                CREATE TABLE semantic_facts (
                    fact_id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    index_version TEXT NOT NULL,
                    language TEXT,
                    frontend_name TEXT NOT NULL,
                    frontend_version TEXT NOT NULL,
                    fact_kind TEXT NOT NULL,
                    node_id TEXT,
                    source TEXT,
                    target TEXT,
                    edge_kind TEXT,
                    confidence TEXT NOT NULL,
                    semantic_role TEXT,
                    path TEXT,
                    start_line INTEGER,
                    end_line INTEGER,
                    resolution_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    properties_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX idx_semantic_facts_stable
                ON semantic_facts(repo_id, index_version, fact_id);
                CREATE INDEX idx_semantic_facts_edge_merge
                ON semantic_facts(repo_id, index_version, source, target, edge_kind, semantic_role);
                CREATE INDEX idx_semantic_facts_path ON semantic_facts(path);
                CREATE INDEX idx_semantic_facts_edge_kind ON semantic_facts(edge_kind);
                CREATE TABLE nodes (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    qualname TEXT,
                    path TEXT,
                    start_line INTEGER,
                    end_line INTEGER,
                    canonical_identity TEXT,
                    properties_json TEXT NOT NULL
                );
                CREATE INDEX idx_nodes_path ON nodes(path);
                CREATE INDEX idx_nodes_kind ON nodes(kind);
                CREATE INDEX idx_nodes_name_kind_id ON nodes(name, kind, id);
                CREATE INDEX idx_nodes_qualname_kind_id ON nodes(qualname, kind, id);
                CREATE INDEX idx_nodes_canonical_identity ON nodes(canonical_identity);
                CREATE TABLE edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    semantic_role TEXT,
                    resolution_json TEXT NOT NULL,
                    confidence_sources_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    properties_json TEXT NOT NULL
                );
                CREATE INDEX idx_edges_source_kind ON edges(source, kind);
                CREATE INDEX idx_edges_target_kind ON edges(target, kind);
                CREATE INDEX idx_edges_source_kind_role ON edges(source, kind, semantic_role);
                CREATE INDEX idx_edges_target_kind_role ON edges(target, kind, semantic_role);
                CREATE TABLE warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    path TEXT,
                    message TEXT NOT NULL,
                    frontend_name TEXT
                );
                CREATE TABLE diagnostics (
                    diagnostic_id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    index_version TEXT NOT NULL,
                    diagnostic_kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    path TEXT,
                    start_line INTEGER,
                    end_line INTEGER,
                    frontend_name TEXT NOT NULL,
                    fact_id TEXT,
                    first_seen_index TEXT,
                    last_seen_index TEXT,
                    seen_count INTEGER NOT NULL,
                    properties_json TEXT NOT NULL
                );
                CREATE INDEX idx_diagnostics_kind ON diagnostics(diagnostic_kind);
                CREATE INDEX idx_diagnostics_path ON diagnostics(path);
                CREATE TABLE merge_metrics (
                    repo_id TEXT NOT NULL,
                    index_version TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (repo_id, index_version)
                );
                CREATE TABLE schema_migrations (
                    migration_id TEXT PRIMARY KEY,
                    from_schema_version TEXT,
                    to_schema_version TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    required_rebuild INTEGER NOT NULL,
                    applied_at TEXT NOT NULL,
                    notes TEXT
                );
                """)
            for key, value in metadata.model_dump(mode="json").items():
                conn.execute(
                    "INSERT INTO index_metadata(key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )

            conn.executemany(
                """
                INSERT INTO files(path, module, source_root, file_hash, line_count, file_size, is_package)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        file.path,
                        file.module,
                        file.source_root,
                        file.file_hash,
                        file.line_count,
                        file.file_size,
                        1 if file.is_package else 0,
                    )
                    for file in files
                ],
            )
            conn.executemany(
                """
                INSERT INTO semantic_facts(
                    fact_id, repo_id, index_version, language, frontend_name,
                    frontend_version, fact_kind, node_id, source, target, edge_kind,
                    confidence, semantic_role, path, start_line, end_line,
                    resolution_json, evidence_json, properties_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        fact.fact_id,
                        fact.repo_id,
                        fact.index_version,
                        fact.language,
                        fact.frontend_name,
                        fact.frontend_version,
                        fact.fact_kind,
                        fact.node_id,
                        fact.source,
                        fact.target,
                        fact.edge_kind,
                        fact.confidence,
                        fact.semantic_role,
                        fact.path,
                        fact.start_line,
                        fact.end_line,
                        fact.resolution.model_dump_json(exclude_none=True),
                        json.dumps(
                            [
                                item.model_dump(mode="json", exclude_none=True)
                                for item in fact.evidence
                            ]
                        ),
                        json.dumps(fact.properties, sort_keys=True),
                        fact.created_at.isoformat(),
                    )
                    for fact in semantic_facts
                ],
            )
            conn.executemany(
                """
                INSERT INTO nodes(id, kind, name, qualname, path, start_line, end_line, canonical_identity, properties_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        node.id,
                        node.kind,
                        node.name,
                        node.qualname,
                        node.path,
                        node.start_line,
                        node.end_line,
                        node.canonical_identity,
                        json.dumps(node.properties, sort_keys=True),
                    )
                    for node in nodes
                ],
            )
            conn.executemany(
                """
                INSERT INTO edges(
                    source, target, kind, confidence, semantic_role, resolution_json,
                    confidence_sources_json, evidence_json, properties_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        edge.source,
                        edge.target,
                        edge.kind,
                        edge.confidence,
                        edge.semantic_role,
                        edge.resolution.model_dump_json(exclude_none=True),
                        json.dumps(edge.confidence_sources, sort_keys=True),
                        json.dumps(
                            [
                                item.model_dump(mode="json", exclude_none=True)
                                for item in edge.evidence
                            ]
                        ),
                        json.dumps(edge.properties, sort_keys=True),
                    )
                    for edge in edges
                ],
            )
            conn.executemany(
                """
                INSERT INTO warnings(kind, path, message, frontend_name)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        warning.kind,
                        warning.path,
                        warning.message,
                        warning.frontend_name,
                    )
                    for warning in warnings
                ],
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO diagnostics(
                    diagnostic_id, repo_id, index_version, diagnostic_kind, message,
                    severity, path, start_line, end_line, frontend_name, fact_id,
                    first_seen_index, last_seen_index, seen_count, properties_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        diagnostic.diagnostic_id,
                        diagnostic.repo_id,
                        diagnostic.index_version,
                        diagnostic.diagnostic_kind,
                        diagnostic.message,
                        diagnostic.severity,
                        diagnostic.path,
                        diagnostic.start_line,
                        diagnostic.end_line,
                        diagnostic.frontend_name,
                        diagnostic.fact_id,
                        diagnostic.first_seen_index,
                        diagnostic.last_seen_index,
                        diagnostic.seen_count,
                        json.dumps(diagnostic.properties, sort_keys=True),
                    )
                    for diagnostic in diagnostics
                ],
            )
            if merge_metrics is not None:
                conn.execute(
                    """
                    INSERT INTO merge_metrics(repo_id, index_version, metrics_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        merge_metrics.repo_id,
                        merge_metrics.index_version,
                        merge_metrics.model_dump_json(),
                        merge_metrics.created_at.isoformat(),
                    ),
                )
            conn.executemany(
                """
                INSERT INTO schema_migrations(
                    migration_id, from_schema_version, to_schema_version, strategy,
                    required_rebuild, applied_at, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        migration.migration_id,
                        migration.from_schema_version,
                        migration.to_schema_version,
                        migration.strategy,
                        1 if migration.required_rebuild else 0,
                        migration.applied_at.isoformat(),
                        migration.notes,
                    )
                    for migration in schema_migrations
                ],
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class ClosingSQLiteConnection(sqlite3.Connection):
    """SQLite connection whose context-manager exit closes the file handle."""

    def __exit__(self, *args: Any) -> bool | None:
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class GraphStoreReader:
    def __init__(self, sqlite_path: Path, metadata: dict[str, Any]) -> None:
        self.sqlite_path = sqlite_path
        self.metadata = metadata

    @classmethod
    def from_current(cls, output_dir: Path) -> "GraphStoreReader":
        current_path = output_dir / "current.json"
        if not current_path.exists():
            raise FileNotFoundError(f"No ArcGraph current index at {current_path}")

        current = json.loads(
            retry_sharing_violation(lambda: current_path.read_text(encoding="utf-8"))
        )
        build_rel = Path(str(current["build_dir"]))
        if (
            build_rel.is_absolute()
            or len(build_rel.parts) != 2
            or build_rel.parts[0] != "builds"
            or any(part in {"", ".", ".."} for part in build_rel.parts)
        ):
            raise FileNotFoundError(
                f"ArcGraph current index has unsafe build path: {build_rel}"
            )
        return cls.from_build(output_dir, build_rel.parts[1])

    @classmethod
    def from_build(cls, output_dir: Path, index_version: str) -> "GraphStoreReader":
        """Open one immutable published build by its exact index version."""

        if (
            not index_version
            or "/" in index_version
            or "\\" in index_version
            or index_version in {".", ".."}
        ):
            raise FileNotFoundError("ArcGraph build index version is unsafe")
        output_root = output_dir.resolve()
        builds_root = (output_root / "builds").resolve(strict=False)
        build_dir = (builds_root / index_version).resolve(strict=False)
        try:
            build_dir.relative_to(builds_root)
        except ValueError as exc:
            raise FileNotFoundError(
                "ArcGraph build path is outside builds root"
            ) from exc
        return cls.from_build_path(build_dir)

    @classmethod
    def from_build_path(cls, build_dir: Path) -> "GraphStoreReader":
        """Open a build directory after checking its required immutable files."""

        resolved_build = build_dir.resolve(strict=False)
        if build_dir.is_symlink() or not resolved_build.is_dir():
            raise FileNotFoundError(f"ArcGraph build directory is missing: {build_dir}")
        sqlite_path = resolved_build / "index.sqlite"
        if sqlite_path.is_symlink() or not sqlite_path.is_file():
            raise FileNotFoundError(f"ArcGraph SQLite index is missing: {sqlite_path}")
        summary_path = resolved_build / "summary.json"
        if summary_path.is_symlink() or not summary_path.is_file():
            raise FileNotFoundError(f"ArcGraph summary is missing: {summary_path}")
        try:
            metadata = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FileNotFoundError(
                f"ArcGraph summary cannot be read: {summary_path}"
            ) from exc
        if not isinstance(metadata, dict):
            raise FileNotFoundError(
                f"ArcGraph summary is not an object: {summary_path}"
            )
        return cls(sqlite_path, metadata)

    def connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.sqlite_path, factory=ClosingSQLiteConnection)
            conn.execute("PRAGMA schema_version").fetchone()
        except sqlite3.OperationalError:
            try:
                conn.close()
            except UnboundLocalError:
                pass
            uri = self.sqlite_path.resolve().as_uri() + "?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, factory=ClosingSQLiteConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def read_files(self) -> list[FileRecord]:
        repo_root = Path(str(self.metadata["repo_root"]))
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT path, module, source_root, file_hash, line_count, file_size, is_package
                FROM files
                ORDER BY path
                """).fetchall()

        return [
            FileRecord(
                path=row["path"],
                abs_path=str((repo_root / row["path"]).resolve()),
                source_root=row["source_root"],
                module=row["module"],
                file_hash=row["file_hash"],
                line_count=row["line_count"],
                file_size=row["file_size"],
                is_package=bool(row["is_package"]),
            )
            for row in rows
        ]

    def iter_files(self):
        """Yield file records in stable path order without materializing a graph."""

        repo_root = Path(str(self.metadata["repo_root"]))
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT path, module, source_root, file_hash, line_count, file_size, is_package
                FROM files
                ORDER BY path
                """)
            for row in rows:
                yield FileRecord(
                    path=row["path"],
                    abs_path=str((repo_root / row["path"]).resolve()),
                    source_root=row["source_root"],
                    module=row["module"],
                    file_hash=row["file_hash"],
                    line_count=row["line_count"],
                    file_size=row["file_size"],
                    is_package=bool(row["is_package"]),
                )

    def read_nodes(self) -> list[Node]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT id, kind, name, qualname, path, start_line, end_line, canonical_identity, properties_json
                FROM nodes
                ORDER BY id
                """).fetchall()

        return [
            Node(
                id=row["id"],
                kind=row["kind"],
                name=row["name"],
                qualname=row["qualname"],
                path=row["path"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                canonical_identity=row["canonical_identity"],
                properties=json.loads(row["properties_json"]),
            )
            for row in rows
        ]

    def iter_nodes(self):
        """Yield nodes in stable id order for streaming delta comparison."""

        with self.connect() as conn:
            rows = conn.execute("""
                SELECT id, kind, name, qualname, path, start_line, end_line,
                    canonical_identity, properties_json
                FROM nodes
                ORDER BY id
                """)
            for row in rows:
                yield Node(
                    id=row["id"],
                    kind=row["kind"],
                    name=row["name"],
                    qualname=row["qualname"],
                    path=row["path"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    canonical_identity=row["canonical_identity"],
                    properties=json.loads(row["properties_json"]),
                )

    def read_edges(self) -> list[Edge]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT source, target, kind, confidence, semantic_role, resolution_json, confidence_sources_json, evidence_json, properties_json
                FROM edges
                ORDER BY source, target, kind, semantic_role, confidence
                """).fetchall()

        return [
            Edge(
                source=row["source"],
                target=row["target"],
                kind=row["kind"],
                confidence=row["confidence"],
                semantic_role=row["semantic_role"],
                resolution=FactResolution.model_validate(
                    json.loads(row["resolution_json"])
                ),
                confidence_sources=json.loads(row["confidence_sources_json"]),
                evidence=[
                    Evidence.model_validate(item)
                    for item in json.loads(row["evidence_json"])
                ],
                properties=json.loads(row["properties_json"]),
            )
            for row in rows
        ]

    def iter_edges(self):
        """Yield edges in the stable comparison order used by graph deltas."""

        with self.connect() as conn:
            rows = conn.execute("""
                SELECT source, target, kind, confidence, semantic_role,
                    resolution_json, confidence_sources_json, evidence_json,
                    properties_json
                FROM edges
                ORDER BY source, target, kind, semantic_role, confidence
                """)
            for row in rows:
                yield Edge(
                    source=row["source"],
                    target=row["target"],
                    kind=row["kind"],
                    confidence=row["confidence"],
                    semantic_role=row["semantic_role"],
                    resolution=FactResolution.model_validate(
                        json.loads(row["resolution_json"])
                    ),
                    confidence_sources=json.loads(row["confidence_sources_json"]),
                    evidence=[
                        Evidence.model_validate(item)
                        for item in json.loads(row["evidence_json"])
                    ],
                    properties=json.loads(row["properties_json"]),
                )

    def read_warnings(self) -> list[BuildWarning]:
        with self.connect() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(warnings)")
            }
            frontend_projection = (
                "frontend_name"
                if "frontend_name" in columns
                else "NULL AS frontend_name"
            )
            rows = conn.execute(f"""
                SELECT kind, path, message, {frontend_projection}
                FROM warnings
                ORDER BY id
                """).fetchall()

        return [
            BuildWarning(
                kind=row["kind"],
                path=row["path"],
                message=row["message"],
                frontend_name=row["frontend_name"],
            )
            for row in rows
        ]

    def read_diagnostics(self) -> list[SemanticDiagnostic]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT diagnostic_id, repo_id, index_version, diagnostic_kind,
                    message, severity, path, start_line, end_line, frontend_name,
                    fact_id, first_seen_index, last_seen_index, seen_count,
                    properties_json
                FROM diagnostics
                ORDER BY diagnostic_kind, path, start_line, diagnostic_id
                """).fetchall()

        return [
            SemanticDiagnostic(
                diagnostic_id=row["diagnostic_id"],
                repo_id=row["repo_id"],
                index_version=row["index_version"],
                diagnostic_kind=row["diagnostic_kind"],
                message=row["message"],
                severity=row["severity"],
                path=row["path"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                frontend_name=row["frontend_name"],
                fact_id=row["fact_id"],
                first_seen_index=row["first_seen_index"],
                last_seen_index=row["last_seen_index"],
                seen_count=row["seen_count"],
                properties=json.loads(row["properties_json"]),
            )
            for row in rows
        ]

    def iter_diagnostics(self):
        """Yield diagnostics in stable order for lifecycle-aware comparison."""

        with self.connect() as conn:
            rows = conn.execute("""
                SELECT diagnostic_id, repo_id, index_version, diagnostic_kind,
                    message, severity, path, start_line, end_line, frontend_name,
                    fact_id, first_seen_index, last_seen_index, seen_count,
                    properties_json
                FROM diagnostics
                ORDER BY diagnostic_kind, message, severity, frontend_name,
                    fact_id, properties_json
                """)
            for row in rows:
                yield SemanticDiagnostic(
                    diagnostic_id=row["diagnostic_id"],
                    repo_id=row["repo_id"],
                    index_version=row["index_version"],
                    diagnostic_kind=row["diagnostic_kind"],
                    message=row["message"],
                    severity=row["severity"],
                    path=row["path"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    frontend_name=row["frontend_name"],
                    fact_id=row["fact_id"],
                    first_seen_index=row["first_seen_index"],
                    last_seen_index=row["last_seen_index"],
                    seen_count=row["seen_count"],
                    properties=json.loads(row["properties_json"]),
                )
