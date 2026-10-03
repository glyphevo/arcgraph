"""Additive wrapper for external semantic extractor payloads."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from arcgraph.core.language_tiers import language_tier_capabilities
from arcgraph.core.schemas import (
    BuildWarning,
    FileRecord,
    FrontendAnalyzeRequest,
    FrontendAnalyzeResult,
    FrontendCapabilities,
    Node,
)
from arcgraph.pipeline.contracts import FrontendGraphFragment
from arcgraph.pipeline.external_protocol import (
    ExternalExtractorRequest,
    ExternalExtractorValidationError,
    ToolchainRequirement,
    ToolchainStatus,
    external_payload_to_fragment,
)
from arcgraph.pipeline.frontends import LanguageFrontend

DEFAULT_MAX_EXTRACTOR_STDOUT_BYTES = 10_000_000
DEFAULT_MAX_EXTRACTOR_STDERR_BYTES = 1_000_000
DEFAULT_MAX_EXTRACTOR_PAYLOAD_BYTES = 10_000_000


class ExternalSemanticExtractorFrontend(LanguageFrontend):
    """LanguageFrontend wrapper for future external semantic extractors.

    This frontend is intentionally not registered by default. Callers must add it
    explicitly through a registry or future trusted config.
    """

    def __init__(
        self,
        *,
        name: str,
        version: str,
        language: str,
        language_ids: tuple[str, ...] | None = None,
        file_extensions: tuple[str, ...] = (),
        command: list[str] | None = None,
        payload_path: str | Path | None = None,
        repo_root: str | Path | None = None,
        tier: str = "L3",
        timeout_seconds: float = 30,
        max_stdout_bytes: int = DEFAULT_MAX_EXTRACTOR_STDOUT_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_EXTRACTOR_STDERR_BYTES,
        max_payload_bytes: int = DEFAULT_MAX_EXTRACTOR_PAYLOAD_BYTES,
        required_toolchains: tuple[ToolchainRequirement, ...] = (),
        supported_node_kinds: tuple[str, ...] = (
            "module",
            "function",
            "class",
            "method",
            "symbol",
        ),
        supported_edge_kinds: tuple[str, ...] = (
            "contains",
            "defines",
            "imports",
            "references",
            "calls",
        ),
    ) -> None:
        self.name = name
        self.version = version
        self.language = language
        self.language_ids = language_ids or (language,)
        self._file_extensions = file_extensions
        self.command = command
        self.payload_path = Path(payload_path) if payload_path is not None else None
        self.repo_root = Path(repo_root).resolve() if repo_root is not None else None
        self.tier = tier
        self.timeout_seconds = timeout_seconds
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_payload_bytes = max_payload_bytes
        self.required_toolchains = required_toolchains
        self.supported_node_kinds = supported_node_kinds
        self.supported_edge_kinds = supported_edge_kinds

    @property
    def file_extensions(self) -> tuple[str, ...]:
        return self._file_extensions

    def capabilities(self) -> FrontendCapabilities:
        return FrontendCapabilities(
            name=self.name,
            version=self.version,
            language=self.language,
            capabilities={
                "mode": "external-semantic-extractor",
                "fact_kinds": "node,edge,diagnostic",
                "confidence": "confirmed-inferred-and-heuristic",
                "incremental": "file",
                "requires_enablement": "true",
                "supported_node_kinds": ",".join(self.supported_node_kinds),
                "supported_edge_kinds": ",".join(self.supported_edge_kinds),
                **language_tier_capabilities(
                    tier=self.tier,
                    source="external-semantic-extractor",
                    scope=(
                        "External semantic extractor payload validated through "
                        "ArcGraph protocol before graph merge."
                    ),
                    languages=(self.language,),
                ),
            },
            file_extensions=list(self.file_extensions),
        )

    def detect(self, request: FrontendAnalyzeRequest) -> bool:
        if self.payload_path is not None:
            return True
        repo_root = Path(request.repo_root)
        candidate_paths = request.changed_files or request.source_roots or [""]
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
        return FrontendAnalyzeResult(frontend=self.capabilities(), status="available")

    def analyze_to_graph(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning] | None = None,
        module_names: set[str] | None = None,
        call_context_nodes: list[Node] | None = None,
    ) -> FrontendGraphFragment:
        del module_names, call_context_nodes
        started = time.monotonic()
        warnings = list(warnings or [])
        toolchain_status = self.toolchain_status()
        if toolchain_status.status == "missing" and self.payload_path is None:
            warnings.append(
                BuildWarning(
                    kind="external_extractor_missing_toolchain",
                    message=toolchain_status.detail
                    or f"External extractor toolchain is missing for {self.name}.",
                )
            )
            return self._unavailable_fragment(
                warnings,
                toolchain_status=toolchain_status,
                started=started,
            )

        raw_payload = self._load_payload(files, warnings, toolchain_status)
        if raw_payload is None:
            return self._unavailable_fragment(
                warnings,
                toolchain_status=toolchain_status,
                started=started,
            )

        capabilities = self.capabilities()
        try:
            fragment = external_payload_to_fragment(
                raw_payload,
                repo_root=self._repo_root_for(files),
                expected_frontend=capabilities,
                supported_node_kinds=set(self.supported_node_kinds),
                supported_edge_kinds=set(self.supported_edge_kinds),
            )
        except ExternalExtractorValidationError as exc:
            invalid_status = ToolchainStatus(
                name=self.name,
                status="invalid_output",
                required=toolchain_status.required,
                command=toolchain_status.command,
                detail=str(exc),
            )
            warnings.append(
                BuildWarning(
                    kind="external_extractor_invalid_payload",
                    message=str(exc),
                )
            )
            return self._unavailable_fragment(
                warnings,
                toolchain_status=invalid_status,
                started=started,
            )

        fragment.warnings = [*warnings, *fragment.warnings]
        fragment.phase_timings["external_extractor"] = time.monotonic() - started
        fragment.adapter_metrics = {
            **fragment.adapter_metrics,
            "status": "available",
            "language": self.language,
            "tier": self.tier,
            "toolchain_status": fragment.toolchain_status,
            "extractor": fragment.extractor_metadata,
        }
        return fragment

    def toolchain_status(self) -> ToolchainStatus:
        if self.payload_path is not None:
            return ToolchainStatus(
                name=self.name,
                status="available",
                command=str(self.payload_path),
                detail="Using checked payload fixture.",
            )
        if not self.command:
            return ToolchainStatus(
                name=self.name,
                status="unavailable",
                detail="No external extractor command configured.",
            )
        command = self.command[0]
        resolved = command if Path(command).is_absolute() else shutil.which(command)
        required = any(requirement.required for requirement in self.required_toolchains)
        if resolved is None:
            return ToolchainStatus(
                name=self.name,
                status="missing",
                required=required,
                command=command,
                detail=f"External extractor command not found: {command}",
            )
        return ToolchainStatus(
            name=self.name,
            status="available",
            required=required,
            command=str(resolved),
        )

    def _load_payload(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning],
        toolchain_status: ToolchainStatus,
    ) -> dict[str, Any] | None:
        if self.payload_path is not None:
            try:
                payload_size = self.payload_path.stat().st_size
                if payload_size > self.max_payload_bytes:
                    return self._output_too_large(
                        warnings,
                        toolchain_status,
                        stream="payload",
                        size=payload_size,
                        limit=self.max_payload_bytes,
                        path=str(self.payload_path),
                    )
                return json.loads(self.payload_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                toolchain_status.status = "invalid_output"
                toolchain_status.detail = str(exc)
                warnings.append(
                    BuildWarning(
                        kind="external_extractor_invalid_json",
                        message=f"External extractor payload is not valid JSON: {exc}",
                        path=str(self.payload_path),
                    )
                )
                return None
            except OSError as exc:
                toolchain_status.status = "unavailable"
                toolchain_status.detail = str(exc)
                warnings.append(
                    BuildWarning(
                        kind="external_extractor_unavailable",
                        message=f"External extractor payload could not be read: {exc}",
                        path=str(self.payload_path),
                    )
                )
                return None

        if not self.command:
            warnings.append(
                BuildWarning(
                    kind="external_extractor_unavailable",
                    message=f"No external extractor command configured for {self.name}.",
                )
            )
            return None

        request = ExternalExtractorRequest.from_files(
            repo_root=self._repo_root_for(files),
            frontend=self.capabilities(),
            language=self.language,
            files=files,
        )
        try:
            result = subprocess.run(
                self.command,
                input=request.model_dump_json(),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                cwd=str(self._repo_root_for(files)),
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            toolchain_status.status = "timeout"
            warnings.append(
                BuildWarning(
                    kind="external_extractor_timeout",
                    message=(
                        f"External extractor timed out after "
                        f"{self.timeout_seconds:g}s: {self.name}"
                    ),
                )
            )
            return None

        stdout_size = len(result.stdout.encode("utf-8", errors="replace"))
        stderr_size = len(result.stderr.encode("utf-8", errors="replace"))
        if stdout_size > self.max_stdout_bytes:
            return self._output_too_large(
                warnings,
                toolchain_status,
                stream="stdout",
                size=stdout_size,
                limit=self.max_stdout_bytes,
            )
        if stdout_size > self.max_payload_bytes:
            return self._output_too_large(
                warnings,
                toolchain_status,
                stream="payload",
                size=stdout_size,
                limit=self.max_payload_bytes,
            )
        if stderr_size > self.max_stderr_bytes:
            return self._output_too_large(
                warnings,
                toolchain_status,
                stream="stderr",
                size=stderr_size,
                limit=self.max_stderr_bytes,
            )

        if result.returncode != 0:
            warnings.append(
                BuildWarning(
                    kind="external_extractor_unavailable",
                    message=(
                        f"External extractor failed with exit code "
                        f"{result.returncode}: "
                        f"{result.stderr.strip() or result.stdout.strip()}"
                    ),
                )
            )
            return None
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            toolchain_status.status = "invalid_output"
            warnings.append(
                BuildWarning(
                    kind="external_extractor_invalid_json",
                    message=f"External extractor returned invalid JSON: {exc}",
                )
            )
            return None
        if not isinstance(parsed, dict):
            warnings.append(
                BuildWarning(
                    kind="external_extractor_invalid_payload",
                    message="External extractor returned a non-object payload.",
                )
            )
            return None
        return parsed

    def _output_too_large(
        self,
        warnings: list[BuildWarning],
        toolchain_status: ToolchainStatus,
        *,
        stream: str,
        size: int,
        limit: int,
        path: str | None = None,
    ) -> None:
        toolchain_status.status = "invalid_output"
        toolchain_status.detail = (
            f"External extractor {stream} exceeded {limit} bytes: {size}"
        )
        warnings.append(
            BuildWarning(
                kind="external_extractor_output_too_large",
                message=toolchain_status.detail,
                path=path,
            )
        )
        return None

    def _unavailable_fragment(
        self,
        warnings: list[BuildWarning],
        *,
        toolchain_status: ToolchainStatus,
        started: float,
    ) -> FrontendGraphFragment:
        status_payload = toolchain_status.model_dump(mode="json")
        return FrontendGraphFragment(
            warnings=warnings,
            adapter_metrics={
                "status": "unavailable",
                "language": self.language,
                "tier": self.tier,
                "toolchain_status": status_payload,
            },
            extractor_metadata={
                "name": self.name,
                "version": self.version,
                "language": self.language,
                "tier": self.tier,
            },
            toolchain_status=status_payload,
            phase_timings={"external_extractor": time.monotonic() - started},
        )

    def _repo_root_for(self, files: list[FileRecord]) -> Path:
        if self.repo_root is not None:
            return self.repo_root
        if not files:
            return Path(".").resolve()
        first = Path(files[0].abs_path).resolve()
        for parent in [first.parent, *first.parents]:
            if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
                return parent
        return first.parent.parent if len(first.parents) > 1 else first.parent
