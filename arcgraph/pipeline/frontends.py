"""Language frontend contracts and the current Python compatibility frontend."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from arcgraph.core.ids import diagnostic_id
from arcgraph.core.language_tiers import language_tier_capabilities
from arcgraph.core.passes import (
    AnalysisPassContext,
    AnalysisPassResult,
    PassDispatcher,
)
from arcgraph.pipeline.contracts import FrontendGraphFragment
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    FileRecord,
    FrontendAnalyzeRequest,
    FrontendAnalyzeResult,
    FrontendCapabilities,
    IndexMetadata,
    Node,
    SemanticDiagnostic,
)
from arcgraph.core.semantic import (
    COMPAT_FRONTEND_NAME,
    COMPAT_FRONTEND_VERSION,
    diagnostics_from_warnings,
    semantic_facts_from_graph,
)
from arcgraph.pipeline.typescript_frontend import (
    TYPESCRIPT_FILE_EXTENSIONS,
    TYPESCRIPT_FRONTEND_NAME,
    TYPESCRIPT_FRONTEND_VERSION,
    TypeScriptGraphAnalyzer,
)


class LanguageFrontend(ABC):
    name: str
    language_ids: tuple[str, ...]
    version: str

    @property
    def file_extensions(self) -> tuple[str, ...]:
        """File extensions this frontend can analyze (e.g. ('.py',)).

        Used by FileScanner to dynamically determine which files to scan.
        Default returns all extensions based on language_ids.
        """
        _LANG_EXTENSIONS: dict[str, tuple[str, ...]] = {
            "python": (".py",),
            "typescript": (".ts", ".tsx"),
            "javascript": (".js", ".jsx"),
            "java": (".java",),
            "go": (".go",),
        }
        extensions: list[str] = []
        for lang in self.language_ids:
            extensions.extend(_LANG_EXTENSIONS.get(lang, ()))
        return tuple(extensions) or (".py",)  # Default to Python

    def accepts(self, file: FileRecord) -> bool:
        """Return whether this frontend should analyze an indexed file."""
        return Path(file.path).suffix in self.file_extensions

    @abstractmethod
    def capabilities(self) -> FrontendCapabilities:
        """Return the frontend's declared capabilities."""

    @abstractmethod
    def detect(self, request: FrontendAnalyzeRequest) -> bool:
        """Return whether this frontend should analyze the request."""

    @abstractmethod
    def analyze(self, request: FrontendAnalyzeRequest) -> FrontendAnalyzeResult:
        """Analyze the request and return semantic facts."""

    @abstractmethod
    def analyze_to_graph(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning] | None = None,
        module_names: set[str] | None = None,
        call_context_nodes: list[Node] | None = None,
        incremental_similarity_buckets: set[str] | None = None,
    ) -> FrontendGraphFragment:
        """Analyze files and return a raw graph fragment."""

    def analyze_incremental(
        self, request: FrontendAnalyzeRequest
    ) -> FrontendAnalyzeResult:
        return self.analyze(request)


class PythonSemanticFrontend(LanguageFrontend):
    """Compatibility wrapper for the existing Python V1 graph analyzers."""

    name = COMPAT_FRONTEND_NAME
    language_ids = ("python",)
    version = COMPAT_FRONTEND_VERSION

    def __init__(
        self,
        *,
        enable_v2_call_resolution: bool = True,
        adapter_registry: object | None = None,
    ) -> None:
        self._enable_v2 = enable_v2_call_resolution
        self._adapter_registry = adapter_registry

    def capabilities(self) -> FrontendCapabilities:
        return FrontendCapabilities(
            name=self.name,
            version=self.version,
            language="python",
            capabilities={
                "mode": "v1-compat",
                "fact_kinds": "node,edge,diagnostic",
                "confidence": "canonical",
                "incremental": "file",
                **language_tier_capabilities(
                    tier="L3",
                    source="native-python",
                    scope=(
                        "Python semantic graph facts with receiver-aware static "
                        "analysis; runtime evidence remains separate."
                    ),
                    languages=("python",),
                ),
            },
            file_extensions=list(self.file_extensions),
        )

    def detect(self, request: FrontendAnalyzeRequest) -> bool:
        repo_root = Path(request.repo_root)
        candidate_paths = request.changed_files or request.source_roots or ["."]
        for raw_path in candidate_paths:
            path = repo_root / raw_path
            if path.is_file() and path.suffix == ".py":
                return True
            if path.is_dir():
                try:
                    if next(path.rglob("*.py"), None) is not None:
                        return True
                except OSError:
                    continue
        return False

    def analyze(self, request: FrontendAnalyzeRequest) -> FrontendAnalyzeResult:
        return FrontendAnalyzeResult(
            frontend=self.capabilities(),
            diagnostics=[
                SemanticDiagnostic(
                    diagnostic_id=diagnostic_id(
                        request.repo_id,
                        "frontend_graph_context_note",
                        self.name,
                        request.index_version or "working-tree",
                    ),
                    repo_id=request.repo_id,
                    index_version=request.index_version or "working-tree",
                    diagnostic_kind="frontend_graph_context_note",
                    message=(
                        "PythonSemanticFrontend.analyze() generates semantic "
                        "facts via analyze_graph(); use analyze_to_graph() for "
                        "raw graph fragments."
                    ),
                    severity="info",
                    frontend_name=self.name,
                )
            ],
            status="available",
        )

    def analyze_to_graph(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning] | None = None,
        module_names: set[str] | None = None,
        call_context_nodes: list[Node] | None = None,
        incremental_similarity_buckets: set[str] | None = None,
    ) -> FrontendGraphFragment:
        """Run the Python graph analysis pipeline and return a raw fragment.

        This is the primary entry point for the indexer to get
        Python-specific graph data without going through the semantic
        fact / merge pipeline.
        """
        analyzer = PythonGraphAnalyzer(
            enable_v2_call_resolution=self._enable_v2,
            adapter_registry=self._adapter_registry,
        )
        return analyzer.analyze(
            files,
            warnings=warnings,
            module_names=module_names,
            call_context_nodes=call_context_nodes,
            incremental_similarity_buckets=incremental_similarity_buckets,
        )

    def analyze_graph(
        self,
        metadata: IndexMetadata,
        nodes: list[Node],
        edges: list[Edge],
        warnings: list[BuildWarning],
        *,
        repo_id: str = "default",
    ) -> FrontendAnalyzeResult:
        facts = semantic_facts_from_graph(metadata, nodes, edges, repo_id=repo_id)
        diagnostics = diagnostics_from_warnings(metadata, warnings, repo_id=repo_id)
        return FrontendAnalyzeResult(
            frontend=self.capabilities(),
            facts=facts,
            diagnostics=diagnostics,
            metrics={
                "node_facts": sum(1 for fact in facts if fact.fact_kind == "node"),
                "edge_facts": sum(1 for fact in facts if fact.fact_kind == "edge"),
                "diagnostics": len(diagnostics),
            },
        )


class TypeScriptSemanticFrontend(LanguageFrontend):
    """Static TypeScript/React frontend using the TypeScript compiler API."""

    name = TYPESCRIPT_FRONTEND_NAME
    language_ids = ("typescript", "javascript")
    version = TYPESCRIPT_FRONTEND_VERSION

    def __init__(
        self,
        repo_root: Path | None = None,
        *,
        timeout_seconds: float = 60,
    ) -> None:
        self._repo_root = repo_root.resolve() if repo_root is not None else None
        self._timeout_seconds = timeout_seconds

    def capabilities(self) -> FrontendCapabilities:
        return FrontendCapabilities(
            name=self.name,
            version=self.version,
            language="typescript",
            capabilities={
                "mode": "typescript-compiler-ast",
                "fact_kinds": "node,edge,diagnostic",
                "confidence": "confirmed-inferred-and-heuristic",
                "incremental": "file",
                **language_tier_capabilities(
                    tier="L3",
                    source="typescript-compiler-api",
                    scope=(
                        "TypeScript and JavaScript semantic static facts from "
                        "the compiler API; typechecker call targets are inferred."
                    ),
                    languages=("typescript", "javascript"),
                ),
            },
            file_extensions=list(self.file_extensions),
        )

    @property
    def file_extensions(self) -> tuple[str, ...]:
        return TYPESCRIPT_FILE_EXTENSIONS

    def detect(self, request: FrontendAnalyzeRequest) -> bool:
        repo_root = Path(request.repo_root)
        candidate_paths = request.changed_files or request.source_roots or ["."]
        for raw_path in candidate_paths:
            path = repo_root / raw_path
            if path.is_file() and path.suffix in self.file_extensions:
                return True
            if path.is_dir():
                try:
                    if any(
                        next(path.rglob(f"*{extension}"), None) is not None
                        for extension in self.file_extensions
                    ):
                        return True
                except OSError:
                    continue
        return False

    def analyze(self, request: FrontendAnalyzeRequest) -> FrontendAnalyzeResult:
        return FrontendAnalyzeResult(
            frontend=self.capabilities(),
            diagnostics=[
                SemanticDiagnostic(
                    diagnostic_id=diagnostic_id(
                        request.repo_id,
                        "frontend_graph_context_note",
                        self.name,
                        request.index_version or "working-tree",
                    ),
                    repo_id=request.repo_id,
                    index_version=request.index_version or "working-tree",
                    diagnostic_kind="frontend_graph_context_note",
                    message=(
                        "TypeScriptSemanticFrontend.analyze() generates graph "
                        "fragments via analyze_to_graph()."
                    ),
                    severity="info",
                    frontend_name=self.name,
                )
            ],
            status="available",
        )

    def analyze_to_graph(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning] | None = None,
        module_names: set[str] | None = None,
        call_context_nodes: list[Node] | None = None,
        incremental_similarity_buckets: set[str] | None = None,
    ) -> FrontendGraphFragment:
        repo_root = self._repo_root or self._infer_repo_root(files)
        analyzer = TypeScriptGraphAnalyzer(
            repo_root,
            timeout_seconds=self._timeout_seconds,
        )
        return analyzer.analyze(
            files,
            warnings=warnings,
            module_names=module_names,
            call_context_nodes=call_context_nodes,
            incremental_similarity_buckets=incremental_similarity_buckets,
        )

    @staticmethod
    def _infer_repo_root(files: list[FileRecord]) -> Path:
        if not files:
            return Path(".").resolve()
        first_abs = Path(files[0].abs_path).resolve()
        for parent in [first_abs.parent, *first_abs.parents]:
            if (parent / ".git").exists() or (
                parent / "frontend" / "package.json"
            ).exists():
                return parent
        return first_abs.parent


class PythonSemanticGraphPass:
    name = "p1-python-v1-compat"

    def __init__(self, frontend: PythonSemanticFrontend | None = None) -> None:
        self.frontend = frontend or PythonSemanticFrontend()

    def run(self, context: AnalysisPassContext) -> AnalysisPassResult:
        return AnalysisPassResult(
            name=self.name,
            result=self.frontend.analyze_graph(
                context.metadata,
                context.nodes,
                context.edges,
                context.warnings,
                repo_id=context.repo_id,
            ),
        )


def python_compat_pass_dispatcher() -> PassDispatcher:
    return PassDispatcher([PythonSemanticGraphPass()])


class FrontendRegistry:
    """Minimal registry for language frontend discovery and dispatch."""

    def __init__(self, frontends: list[LanguageFrontend] | None = None) -> None:
        self._frontends: list[LanguageFrontend] = []
        for frontend in frontends or []:
            self.register(frontend)

    def register(self, frontend: LanguageFrontend) -> None:
        if any(existing.name == frontend.name for existing in self._frontends):
            raise ValueError(f"Frontend already registered: {frontend.name}")
        self._frontends.append(frontend)

    def detect_frontends(
        self, request: FrontendAnalyzeRequest
    ) -> list[LanguageFrontend]:
        return [frontend for frontend in self._frontends if frontend.detect(request)]

    def build_plan(self, request: FrontendAnalyzeRequest) -> list[LanguageFrontend]:
        """Return detected frontends in registration order for build context flow."""
        return self.detect_frontends(request)

    def analyze(self, request: FrontendAnalyzeRequest) -> list[FrontendAnalyzeResult]:
        return [
            frontend.analyze(request) for frontend in self.detect_frontends(request)
        ]

    @property
    def frontends(self) -> tuple[LanguageFrontend, ...]:
        return tuple(self._frontends)

    def inventory(self) -> tuple[dict[str, object], ...]:
        """Return a stable, JSON-serializable inventory of registered frontends."""
        entries: list[dict[str, object]] = []
        for frontend in self._frontends:
            capabilities = frontend.capabilities()
            entries.append(
                {
                    "name": frontend.name,
                    "version": frontend.version,
                    "language_ids": list(frontend.language_ids),
                    "file_extensions": list(frontend.file_extensions),
                    "capabilities": capabilities.model_dump(mode="json"),
                }
            )
        return tuple(entries)


def default_frontend_registry(
    *,
    repo_root: Path | None = None,
    typescript_timeout_seconds: float = 60,
    enable_v2_call_resolution: bool = True,
    adapter_registry: object | None = None,
) -> FrontendRegistry:
    return FrontendRegistry(
        [
            PythonSemanticFrontend(
                enable_v2_call_resolution=enable_v2_call_resolution,
                adapter_registry=adapter_registry,
            ),
            TypeScriptSemanticFrontend(
                repo_root=repo_root,
                timeout_seconds=typescript_timeout_seconds,
            ),
        ]
    )
