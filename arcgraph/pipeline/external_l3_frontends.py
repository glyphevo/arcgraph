"""Explicit L3 language frontends backed by external semantic extractor payloads."""

from __future__ import annotations

from pathlib import Path

from arcgraph.pipeline.external_frontend import ExternalSemanticExtractorFrontend
from arcgraph.pipeline.external_protocol import ToolchainRequirement

GO_EXTERNAL_FRONTEND_NAME = "go-semantic-external"
GO_EXTERNAL_FRONTEND_VERSION = "0.1.0"
CSHARP_EXTERNAL_FRONTEND_NAME = "csharp-semantic-external"
CSHARP_EXTERNAL_FRONTEND_VERSION = "0.1.0"
C_EXTERNAL_FRONTEND_NAME = "c-semantic-external"
C_EXTERNAL_FRONTEND_VERSION = "0.1.0"
CPP_EXTERNAL_FRONTEND_NAME = "cpp-semantic-external"
CPP_EXTERNAL_FRONTEND_VERSION = "0.1.0"
JAVA_EXTERNAL_FRONTEND_NAME = "java-semantic-external"
JAVA_EXTERNAL_FRONTEND_VERSION = "0.1.0"
RUST_EXTERNAL_FRONTEND_NAME = "rust-semantic-external"
RUST_EXTERNAL_FRONTEND_VERSION = "0.1.0"
SWIFT_EXTERNAL_FRONTEND_NAME = "swift-semantic-external"
SWIFT_EXTERNAL_FRONTEND_VERSION = "0.1.0"

_L3_NODE_KINDS = (
    "annotation",
    "attribute",
    "class",
    "constructor",
    "crate",
    "enum",
    "external_symbol",
    "field",
    "file",
    "function",
    "impl",
    "interface",
    "method",
    "module",
    "namespace",
    "package",
    "project",
    "property",
    "record",
    "struct",
    "trait",
)

_L3_EDGE_KINDS = (
    "calls",
    "contains",
    "defines",
    "has_annotation",
    "has_attribute",
    "has_field",
    "imports",
    "implements",
    "inherits",
    "references",
    "type_ref",
    "uses",
)

_C_L3_NODE_KINDS = (
    *_L3_NODE_KINDS,
    "global_variable",
    "macro",
    "translation_unit",
    "typedef",
    "union",
)

_C_L3_EDGE_KINDS = (
    *_L3_EDGE_KINDS,
    "reads",
    "writes",
)

_CPP_L3_NODE_KINDS = (
    *_C_L3_NODE_KINDS,
    "destructor",
    "template",
)

_CPP_L3_EDGE_KINDS = (
    *_C_L3_EDGE_KINDS,
    "overrides",
)

_SWIFT_L3_NODE_KINDS = (
    *_L3_NODE_KINDS,
    "extension",
    "protocol",
)

_SWIFT_L3_EDGE_KINDS = (
    *_L3_EDGE_KINDS,
    "conforms",
    "extends",
)


class GoExternalSemanticFrontend(ExternalSemanticExtractorFrontend):
    """Go L3 frontend wrapper for validated go/packages/go/types payloads."""

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        payload_path: str | Path | None = None,
        repo_root: str | Path | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        super().__init__(
            name=GO_EXTERNAL_FRONTEND_NAME,
            version=GO_EXTERNAL_FRONTEND_VERSION,
            language="go",
            language_ids=("go",),
            file_extensions=(".go",),
            command=command or ["arcgraph-go-extractor"],
            payload_path=payload_path,
            repo_root=repo_root,
            tier="L3",
            timeout_seconds=timeout_seconds,
            required_toolchains=(
                ToolchainRequirement(
                    name="arcgraph-go-extractor",
                    command="arcgraph-go-extractor",
                    required=False,
                ),
            ),
            supported_node_kinds=_L3_NODE_KINDS,
            supported_edge_kinds=_L3_EDGE_KINDS,
        )


class CSharpExternalSemanticFrontend(ExternalSemanticExtractorFrontend):
    """C# L3 frontend wrapper for validated Roslyn semantic payloads."""

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        payload_path: str | Path | None = None,
        repo_root: str | Path | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        super().__init__(
            name=CSHARP_EXTERNAL_FRONTEND_NAME,
            version=CSHARP_EXTERNAL_FRONTEND_VERSION,
            language="csharp",
            language_ids=("csharp",),
            file_extensions=(".cs",),
            command=command or ["arcgraph-csharp-extractor"],
            payload_path=payload_path,
            repo_root=repo_root,
            tier="L3",
            timeout_seconds=timeout_seconds,
            required_toolchains=(
                ToolchainRequirement(
                    name="arcgraph-csharp-extractor",
                    command="arcgraph-csharp-extractor",
                    required=False,
                ),
            ),
            supported_node_kinds=_L3_NODE_KINDS,
            supported_edge_kinds=_L3_EDGE_KINDS,
        )


class CExternalSemanticFrontend(ExternalSemanticExtractorFrontend):
    """C L3 wrapper for validated Clang/clangd semantic payloads."""

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        payload_path: str | Path | None = None,
        repo_root: str | Path | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        super().__init__(
            name=C_EXTERNAL_FRONTEND_NAME,
            version=C_EXTERNAL_FRONTEND_VERSION,
            language="c",
            language_ids=("c",),
            file_extensions=(".c", ".h"),
            command=command or ["arcgraph-c-extractor"],
            payload_path=payload_path,
            repo_root=repo_root,
            tier="L3",
            timeout_seconds=timeout_seconds,
            required_toolchains=(
                ToolchainRequirement(
                    name="arcgraph-c-extractor",
                    command="arcgraph-c-extractor",
                    required=False,
                ),
                ToolchainRequirement(
                    name="clang-or-clangd",
                    command="clang",
                    required=False,
                    version_args=("--version",),
                ),
            ),
            supported_node_kinds=_C_L3_NODE_KINDS,
            supported_edge_kinds=_C_L3_EDGE_KINDS,
        )


class CppExternalSemanticFrontend(ExternalSemanticExtractorFrontend):
    """C++ L3 wrapper for validated Clang/clangd semantic payloads."""

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        payload_path: str | Path | None = None,
        repo_root: str | Path | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        super().__init__(
            name=CPP_EXTERNAL_FRONTEND_NAME,
            version=CPP_EXTERNAL_FRONTEND_VERSION,
            language="cpp",
            language_ids=("cpp",),
            file_extensions=(".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"),
            command=command or ["arcgraph-cpp-extractor"],
            payload_path=payload_path,
            repo_root=repo_root,
            tier="L3",
            timeout_seconds=timeout_seconds,
            required_toolchains=(
                ToolchainRequirement(
                    name="arcgraph-cpp-extractor",
                    command="arcgraph-cpp-extractor",
                    required=False,
                ),
                ToolchainRequirement(
                    name="clang-or-clangd",
                    command="clang++",
                    required=False,
                    version_args=("--version",),
                ),
            ),
            supported_node_kinds=_CPP_L3_NODE_KINDS,
            supported_edge_kinds=_CPP_L3_EDGE_KINDS,
        )


class JavaExternalSemanticFrontend(ExternalSemanticExtractorFrontend):
    """Java L3 frontend wrapper for validated JDT/javac semantic payloads."""

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        payload_path: str | Path | None = None,
        repo_root: str | Path | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        super().__init__(
            name=JAVA_EXTERNAL_FRONTEND_NAME,
            version=JAVA_EXTERNAL_FRONTEND_VERSION,
            language="java",
            language_ids=("java",),
            file_extensions=(".java",),
            command=command or ["arcgraph-java-extractor"],
            payload_path=payload_path,
            repo_root=repo_root,
            tier="L3",
            timeout_seconds=timeout_seconds,
            required_toolchains=(
                ToolchainRequirement(
                    name="arcgraph-java-extractor",
                    command="arcgraph-java-extractor",
                    required=False,
                ),
                ToolchainRequirement(
                    name="javac-or-jdt",
                    command="javac",
                    required=False,
                    version_args=("-version",),
                ),
            ),
            supported_node_kinds=_L3_NODE_KINDS,
            supported_edge_kinds=_L3_EDGE_KINDS,
        )


class RustExternalSemanticFrontend(ExternalSemanticExtractorFrontend):
    """Rust L3 frontend wrapper for validated rust-analyzer semantic payloads."""

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        payload_path: str | Path | None = None,
        repo_root: str | Path | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        super().__init__(
            name=RUST_EXTERNAL_FRONTEND_NAME,
            version=RUST_EXTERNAL_FRONTEND_VERSION,
            language="rust",
            language_ids=("rust",),
            file_extensions=(".rs",),
            command=command or ["arcgraph-rust-extractor"],
            payload_path=payload_path,
            repo_root=repo_root,
            tier="L3",
            timeout_seconds=timeout_seconds,
            required_toolchains=(
                ToolchainRequirement(
                    name="arcgraph-rust-extractor",
                    command="arcgraph-rust-extractor",
                    required=False,
                ),
                ToolchainRequirement(
                    name="rust-analyzer-or-cargo",
                    command="cargo",
                    required=False,
                    version_args=("--version",),
                ),
            ),
            supported_node_kinds=_L3_NODE_KINDS,
            supported_edge_kinds=_L3_EDGE_KINDS,
        )


class SwiftExternalSemanticFrontend(ExternalSemanticExtractorFrontend):
    """Swift L3 wrapper for validated SourceKit/IndexStore semantic payloads."""

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        payload_path: str | Path | None = None,
        repo_root: str | Path | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        super().__init__(
            name=SWIFT_EXTERNAL_FRONTEND_NAME,
            version=SWIFT_EXTERNAL_FRONTEND_VERSION,
            language="swift",
            language_ids=("swift",),
            file_extensions=(".swift",),
            command=command or ["arcgraph-swift-extractor"],
            payload_path=payload_path,
            repo_root=repo_root,
            tier="L3",
            timeout_seconds=timeout_seconds,
            required_toolchains=(
                ToolchainRequirement(
                    name="arcgraph-swift-extractor",
                    command="arcgraph-swift-extractor",
                    required=False,
                ),
                ToolchainRequirement(
                    name="sourcekit-or-swift",
                    command="swift",
                    required=False,
                    version_args=("--version",),
                ),
            ),
            supported_node_kinds=_SWIFT_L3_NODE_KINDS,
            supported_edge_kinds=_SWIFT_L3_EDGE_KINDS,
        )
