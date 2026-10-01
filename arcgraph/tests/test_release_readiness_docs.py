from __future__ import annotations

import argparse
import json
import py_compile
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tomllib

import pytest

from arcgraph.core.payload_policy import (
    TARGET_SCOPED_CLI_COMMANDS,
    TARGET_SCOPED_MCP_TOOLS,
)
from arcgraph.interfaces.agent_capabilities import mcp_capability_names
from arcgraph.interfaces.cli import _ArgumentParserError, build_parser, main
from arcgraph.interfaces.docs import DOC_TOPICS, render_docs
from arcgraph.interfaces.mcp_server import build_parser as build_mcp_parser
from arcgraph.interfaces.metrics import (
    METRICS_LOG_INVALID_ENCODING_ERROR_CODE,
    METRICS_LOG_UNREADABLE_ERROR_CODE,
)
from arcgraph.interfaces.trial_feedback import (
    MAX_FEEDBACK_FILE_BYTES,
    MAX_FEEDBACK_RECORD_BYTES,
    MAX_WARNING_KINDS,
    TrialFeedbackConflictError,
    TrialFeedbackDisabledError,
    TrialFeedbackInputError,
    TrialFeedbackLimitError,
    TrialFeedbackRequest,
    TrialFeedbackStorageError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_release_readiness_docs_topics_are_renderable() -> None:
    assert main(["docs", "quickstart"]) == 0
    assert main(["docs", "security-model"]) == 0
    assert main(["docs", "release-checklist"]) == 0
    assert main(["docs", "schema-governance"]) == 0
    assert main(["docs", "frontend-contract"]) == 0
    assert main(["docs", "agent-cli-contract"]) == 0
    assert main(["docs", "mcp-server"]) == 0
    assert main(["docs", "source-checkout-smoke"]) == 0
    assert main(["docs", "package-readiness"]) == 0


@pytest.mark.parametrize("topic", DOC_TOPICS)
def test_all_docs_topics_render_markdown_json_and_cli(topic: str) -> None:
    markdown = render_docs(topic)
    payload = render_docs(topic, as_json=True)

    assert isinstance(markdown, str)
    assert markdown.startswith("# ")
    assert isinstance(payload, dict)
    assert payload["topic"] == topic
    assert payload["title"]
    assert payload["sections"]
    assert main(["docs", topic]) == 0
    assert main(["docs", topic, "--json"]) == 0


def test_cli_docs_topic_choices_match_doc_topics() -> None:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if getattr(action, "dest", None) == "command"
    )
    docs_parser = subparsers.choices["docs"]
    topic_action = next(
        action for action in docs_parser._actions if action.dest == "topic"
    )

    assert tuple(topic_action.choices) == DOC_TOPICS


def test_readme_lists_every_built_in_docs_topic() -> None:
    """A built-in topic that README never names is undiscoverable in practice."""

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Built-In Documentation", 1)[1].split("\n## ", 1)[0]
    missing = [
        topic for topic in DOC_TOPICS if f"| `arcgraph docs {topic}` |" not in section
    ]

    assert not missing, (
        "README.md must list every built-in docs topic in its Built-In "
        f"Documentation table so it is discoverable; missing: {', '.join(missing)}"
    )


def test_cli_reference_names_every_top_level_command() -> None:
    """`cli-reference` is presented as the full command surface, so prove it.

    The topic previously omitted ten commands, including the whole `change`
    group, while README described it as complete. Pinning the coverage keeps the
    claim and the content from drifting apart again.
    """

    parser = build_parser()
    commands = next(
        action
        for action in parser._actions
        if getattr(action, "dest", None) == "command"
    ).choices
    reference = render_docs("cli-reference")
    missing = [name for name in sorted(commands) if f"arcgraph {name}" not in reference]

    assert not missing, (
        "`arcgraph docs cli-reference` must name every top-level command; "
        f"missing: {', '.join(missing)}"
    )


def _change_leaf_requirements(
    subcommands: dict[str, object],
) -> dict[tuple[str, ...], set[str]]:
    """Map each runnable `change` leaf path to the options argparse requires.

    `evidence`, `report`, and `audit` are groups, so their requirements live one
    level down and only the leaves are directly invocable.
    """

    leaves: dict[tuple[str, ...], set[str]] = {}

    def walk(node: object, path: tuple[str, ...]) -> None:
        nested = next(
            (
                action.choices
                for action in node._actions  # type: ignore[attr-defined]
                if isinstance(getattr(action, "choices", None), dict)
            ),
            None,
        )
        if nested:
            for name, child in nested.items():
                walk(child, path + (name,))
            return
        leaves[path] = {
            option
            for action in node._actions  # type: ignore[attr-defined]
            if action.required
            for option in action.option_strings
            if option.startswith("--")
        }

    for name, subparser in subcommands.items():
        walk(subparser, (name,))
    return leaves


def test_cli_reference_covers_the_change_subcommand_tree() -> None:
    """The change group carries most of its contract in its subcommands."""

    parser = build_parser()
    change_parser = next(
        action
        for action in parser._actions
        if getattr(action, "dest", None) == "command"
    ).choices["change"]
    subcommands = next(
        action
        for action in change_parser._actions
        if getattr(action, "dest", None) == "change_command"
    ).choices
    reference = render_docs("cli-reference")

    # cli-reference uses usage notation, with brackets for optional arguments, so
    # it cannot be fed to the parser the way the runnable examples are. Assert
    # the required options instead — but bind them to the entry that documents
    # that exact leaf. Searching the whole topic gives false coverage: `--plan-id`
    # appears under a dozen commands, so `show` could drop its own and still pass.
    items = [
        item
        for section in render_docs("cli-reference", as_json=True)["sections"]
        for item in section["items"]
    ]

    for path, required in sorted(_change_leaf_requirements(subcommands).items()):
        label = " ".join(path)
        entry = f"change --repo-id REPO {label}"
        documenting = [item for item in items if entry in item]
        assert documenting, f"cli-reference does not document `arcgraph {entry}`"
        assert any(
            all(option in item for option in required) for item in documenting
        ), (
            f"no cli-reference entry for `arcgraph {entry}` carries all of its "
            f"required options: {', '.join(sorted(required))}"
        )

    # The three facts an operator cannot recover from a usage string alone.
    assert "no implicit default repository identity" in reference
    assert "CHANGE_PLAN_STATE_INVALID" in reference
    assert "Complete evidence does not imply `SAFE_TO_PROCEED`" in reference


def test_change_safety_surface_is_documented_for_users() -> None:
    """The change subsystem must stay reachable from user-facing docs."""

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    mcp_usage = (REPO_ROOT / "docs" / "mcp-usage.md").read_text(encoding="utf-8")

    assert "## Change Safety" in readme
    assert "arcgraph change --repo-id" in readme
    assert "docs/change-safety.md" in readme

    for tool in (
        "arcgraph_preview_change_plan",
        "arcgraph_get_change_plan",
        "arcgraph_list_change_plans",
        "arcgraph_get_graph_delta",
        "arcgraph_verify_change",
    ):
        assert tool in mcp_usage

    # `plan` creates and activates a plan and writes a durable pin, so it must
    # never be presented as the read-only equivalent of the MCP preview tool.
    assert "has no read-only CLI equivalent" in mcp_usage
    assert "Change plan preview" not in mcp_usage


def test_change_safety_example_documents_the_verify_ordering_constraint() -> None:
    """The example must not walk an operator into an unverifiable revision.

    ``verify`` persists its result and closes the revision, so evidence has to be
    recorded first. ``test_change_verify_closes_the_revision_even_when_it_blocks``
    in ``test_change_cli.py`` pins the behavior this text describes.
    """

    example = (REPO_ROOT / "docs" / "change-safety.md").read_text(encoding="utf-8")

    record_evidence = example.index("## 4. Record evidence")
    run_verify = example.index("## 5. Verify once")

    assert record_evidence < run_verify
    assert "Run it exactly once" in example
    assert "CHANGE_PLAN_STATE_INVALID" in example
    # Requirements vary per plan, so the example must read them from the plan
    # view instead of hard-coding a single self-reported requirement.
    assert "verification_plan.requirements[]" in example
    assert "minimum_attestation_level" in example


def test_change_safety_example_does_not_promise_a_safe_verdict() -> None:
    """Complete evidence clears only the lowest-precedence class of blockers.

    ``test_e2e_unapproved_and_out_of_scope_changes_never_pass`` records passing
    evidence for every requirement and still gets ``CHANGE_SCOPE_EXCEEDED``, so
    the example must not present ``SAFE_TO_PROCEED`` as the guaranteed outcome.
    """

    example = (REPO_ROOT / "docs" / "change-safety.md").read_text(encoding="utf-8")

    assert "Complete evidence does not by itself produce `SAFE_TO_PROCEED`" in example
    assert "SAFE_WITH_KNOWN_RISKS" in example
    for blocker in (
        "CHANGE_SCOPE_EXCEEDED",
        "PROTECTED_SURFACE_CHANGED",
        "INDEX_STALE",
    ):
        assert blocker in example

    # A plan with no requirements is blocked, not trivially verifiable.
    assert "PLAN_BLOCKED_UNRESOLVED_TARGET" in example
    assert "Zero requirements means the plan is blocked" in example

    # artifact_backed and trusted_runner need inputs beyond --artifact-digest.
    assert "--material-json" in example
    assert "--runner-metadata-json" in example
    assert "--trusted-runner-registry" in example


def _documented_arcgraph_commands(text: str) -> list[str]:
    """Return every ``arcgraph ...`` invocation the docs present as runnable.

    Both fenced blocks and inline code count. The CLI-equivalents table in
    ``docs/mcp-usage.md`` keeps its commands in inline code, and that table is
    exactly where a required option went missing before, so a fenced-only
    extractor would leave the original regression site unguarded.
    """

    commands: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            if line.strip().startswith("arcgraph "):
                commands.append(line.strip())
            continue
        commands.extend(
            span for span in re.findall(r"`(arcgraph [^`]+)`", line) if span.strip()
        )
    return commands


def _parseable(command: str) -> list[str]:
    """Drop the program name and resolve documentation placeholders.

    ``--revision N`` reads naturally in prose but is not an int, so normalize it
    rather than forcing the docs to print a less readable literal.
    """

    tokens = shlex.split(command)[1:]
    return ["1" if token == "N" else token for token in tokens]


def test_documented_change_commands_parse() -> None:
    """Every documented `arcgraph change` invocation must survive the parser.

    Checking that required option strings appear somewhere in the line is not
    enough: it cannot tell a missing global `--repo-id` from a line that simply
    was not matched, so a malformed command reads as "nothing to check". Feeding
    the real parser removes that blind spot.
    """

    parser = build_parser()
    sources = {
        "README.md": (REPO_ROOT / "README.md"),
        "docs/mcp-usage.md": (REPO_ROOT / "docs" / "mcp-usage.md"),
        "docs/change-safety.md": (REPO_ROOT / "docs" / "change-safety.md"),
    }

    observed: dict[str, set[str]] = {}
    for label, path in sources.items():
        for command in _documented_arcgraph_commands(path.read_text(encoding="utf-8")):
            if " change " not in f" {command} ":
                continue
            tokens = _parseable(command)
            if tokens == ["change"]:
                # Prose naming the command group, and nothing else. Exempting
                # anything wider lets a real invocation that dropped the global
                # `--repo-id`, such as `arcgraph change list`, read as prose.
                continue
            try:
                args = parser.parse_args(tokens)
            except (_ArgumentParserError, SystemExit) as exc:
                message = getattr(exc, "message", str(exc))
                raise AssertionError(
                    f"{label} documents an unparseable command: {command}\n{message}"
                ) from exc
            observed.setdefault(label, set()).add(args.change_command)

    # Assert per source, not in total: a total would let one rich source mask a
    # source the extractor stopped reaching, which is how the mcp-usage table
    # went unchecked before. Naming the expected subcommands rather than a count
    # also catches a single table row being deleted.
    assert observed.get("docs/mcp-usage.md") == {"show", "list", "diff"}
    for label in sources:
        assert observed.get(label), f"no change command was extracted from {label}"


def test_documented_change_commands_include_the_advanced_attestation_paths() -> None:
    """`self_reported` alone does not make the higher levels reproducible."""

    example = (REPO_ROOT / "docs" / "change-safety.md").read_text(encoding="utf-8")
    commands = _documented_arcgraph_commands(example)
    levels = {
        level
        for level in ("self_reported", "artifact_backed", "trusted_runner")
        for command in commands
        if f"--attestation-level {level}" in command
    }

    assert levels == {"self_reported", "artifact_backed", "trusted_runner"}

    # The canonical digest is sorted-key, whitespace-free JSON, so a plain file
    # hash is rejected. The example has to show how to compute the right one.
    assert "sort_keys=True" in example
    assert "separators=(',',':')" in example
    assert "EVIDENCE_ARTIFACT_DIGEST_MISMATCH" in example

    # A trusted-runner entry must match on identity, repo, requirement, and
    # command at once, so the schema itself has to be documented.
    for field in (
        "registry_version",
        "runner_identity",
        "allowed_repo_ids",
        "allowed_requirement_ids",
        "allowed_commands",
    ):
        assert field in example
    assert "TRUSTED_RUNNER_UNAVAILABLE" in example


def test_docs_markdown_tables_are_not_split_by_prose() -> None:
    """An orphaned table row renders as literal pipes instead of a table."""

    for relative in (
        "mcp-usage.md",
        "package-readiness.md",
        "change-safety.md",
    ):
        lines = (REPO_ROOT / "docs" / relative).read_text(encoding="utf-8").splitlines()
        block: list[str] = []
        for line in lines + [""]:
            if line.startswith("|"):
                block.append(line)
                continue
            if block:
                assert len(block) >= 2 and set(block[1].replace("|", "").strip()) <= {
                    "-",
                    " ",
                    ":",
                }, f"{relative} has a table row detached from its header: {block[0]}"
                block = []


def test_quickstart_docs_cover_clone_to_confidence_workflow() -> None:
    quickstart = render_docs("quickstart")

    assert 'python -m pip install -e ".[dev]"' in quickstart
    assert "npm ci" in quickstart
    assert "arcgraph build" in quickstart
    assert "arcgraph context" in quickstart
    assert "arcgraph explain" in quickstart
    assert "arcgraph evidence status" in quickstart
    assert "arcgraph benchmark suite" in quickstart
    assert "arcgraph visual workbench" in quickstart
    assert "arcgraph visual serve" in quickstart
    assert "arcgraph docs release-checklist" in quickstart
    assert "arcgraph docs agent-cli-contract" in quickstart
    assert "arcgraph docs mcp-server" in quickstart
    assert "arcgraph docs source-checkout-smoke" in quickstart
    assert "arcgraph docs schema-governance" in quickstart
    assert "arcgraph docs security-model" in quickstart
    assert "arcgraph docs frontend-contract" in quickstart
    assert "full program correctness" not in quickstart.lower()


def test_agent_cli_contract_docs_cover_subprocess_json_boundary() -> None:
    contract = render_docs("agent-cli-contract")

    assert "local subprocess" in contract
    assert "`--repo-root PATH` before the subcommand" in contract
    assert "JSON to stdout by default" in contract
    assert "Global `--human`" in contract
    assert "do not gate JSON parsing on exit code zero" in contract
    assert "for any non-`change` command, an argument-parsing failure" in contract
    assert "exits 2 with empty stdout" in contract
    assert "`arcgraph help --tool UNKNOWN_TOOL`" in contract
    assert "not empty and not this error envelope either" in contract
    assert "own, different argument-parsing contract" in contract
    assert "`error_code: CHANGE_CLI_INPUT_INVALID`" in contract
    assert "even under `--human`, which does not suppress this one" in contract
    assert (
        "do suppress the error envelope specifically for the unhandled-exception case"
        in contract
    )
    assert "`ArcGraph: ...` to stderr" in contract
    assert "commonly return exit code 2" in contract
    assert "`arcgraph ci` can return nonzero" in contract
    assert "`--fail-on-warnings`" in contract
    assert "schema_version" in contract
    assert "rebuilding the index does not change the read contract" in contract
    assert "`status`, `freshness`, `warnings`, and `truncation`" in contract
    assert "Avoid `--include-source` by default" in contract
    assert "`arcgraph current` or `arcgraph status`" in contract
    assert "`arcgraph doctor`" in contract
    assert "`arcgraph build`" in contract
    assert "`arcgraph reindex --changed`" in contract
    assert "`arcgraph context TARGET --detail-level summary`" in contract
    assert "`arcgraph explain TARGET --detail-level summary`" in contract
    assert "`arcgraph impact TARGET --profile review_default`" in contract
    assert "`arcgraph evidence status` and `arcgraph evidence plan`" in contract
    assert "`arcgraph benchmark agent-startup`" in contract
    assert "default MCP analysis/change/help surface is read-only" in contract
    assert "feedback append tool" in contract
    assert "arcgraph mcp serve --repo-root . --output-dir output/arcgraph" in contract
    assert "docs/examples/mcp_readonly_host.py" in contract
    assert "public package publication remains deferred" in contract
    assert "ArcGraph does not auto-configure" in contract
    assert "Public product release and package publishing remain unapproved" in contract
    assert "No language is claimed as L4" in contract


def test_mcp_server_docs_cover_alpha_server_boundary() -> None:
    docs = render_docs("mcp-server")

    assert "arcgraph mcp serve --repo-root . --output-dir output/arcgraph" in docs
    assert "python -m arcgraph.interfaces.mcp_server" in docs
    # Quoted: `.[mcp]` is a glob in zsh and fails before pip runs.
    assert "python -m pip install -e '.[mcp]'" in docs
    assert "stdio is the only supported transport" in docs
    assert "`--allowed-root`" in docs
    assert "`--expose-source-snippets` is off by default" in docs
    assert "`arcgraph_record_learning` is proposal-only" in docs
    assert "does not auto-build" in docs
    assert "does not auto-modify Claude" in docs
    assert "HTTP/network MCP transport remain deferred" in docs


def test_agent_workflows_doc_includes_subprocess_example() -> None:
    workflows = (REPO_ROOT / "docs" / "agent-reading-guide.md").read_text(
        encoding="utf-8"
    )
    normalized_workflows = " ".join(workflows.split())

    assert "## Alpha CLI Contract" in workflows
    assert '["arcgraph", "--repo-root", str(repo), *args]' in workflows
    assert "subprocess.run" in workflows
    assert "json.loads" in workflows
    assert "SCHEMA_VERSION" in workflows
    assert "arcgraph build" in workflows
    assert "Avoid `--include-source` by" in workflows
    assert "Generated indexes" in workflows
    assert "The commands above are the recommended alpha" in workflows
    assert "## Alpha MCP Server" in workflows
    assert "arcgraph mcp serve --repo-root . --output-dir output/arcgraph" in workflows
    assert "does not auto-build indexes" in normalized_workflows
    assert "## Client Integration Examples" in workflows


def test_source_checkout_smoke_docs_cover_source_checkout_and_package_boundary() -> (
    None
):
    smoke = render_docs("source-checkout-smoke")
    example = (REPO_ROOT / "docs" / "examples" / "source-checkout-smoke.md").read_text(
        encoding="utf-8"
    )
    combined = smoke + "\n" + example

    assert "source checkout" in combined
    assert 'python -m pip install -e ".[dev]"' in combined
    assert "arcgraph --help" in combined
    assert "python -m pip show arcgraph" in combined
    assert "arcgraph doctor" in combined
    assert "arcgraph init --dry-run" in combined
    assert "arcgraph build" in combined
    assert "arcgraph current" in combined
    assert "arcgraph status" in combined
    assert "arcgraph stats" in combined
    assert "arcgraph context arcgraph.pipeline.indexer.ArcGraphIndexer" in combined
    assert "arcgraph explain arcgraph.pipeline.indexer.ArcGraphIndexer" in combined
    assert "arcgraph ci" in combined
    assert "python scripts/arcgraph_source_checkout_smoke.py" in combined
    assert "python scripts/arcgraph_clean_checkout_smoke.py" in combined
    assert "docs/clean-checkout-smoke.md" in combined
    assert "arcgraph mcp serve --help" in combined
    assert "arcgraph docs mcp-server" in combined
    assert "arcgraph docs package-readiness" in combined
    assert "output/arcgraph" in combined
    assert "PyPI publishing remains unapproved" in combined
    assert "npm package publishing remains private/dev-only" in combined
    assert "Docker/GHCR publishing remains unapproved" in combined
    assert "GitHub Release and tag creation remain unapproved" in combined
    assert "separate package readiness gate" in combined
    assert "Automatic agent config installers" in combined


def test_cli_reference_documents_the_global_product_version() -> None:
    cli_reference = render_docs("cli-reference")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Global `arcgraph --version`" in cli_reference
    assert "arcgraph --version" in readme


def test_package_readiness_docs_cover_local_package_gate_boundaries() -> None:
    docs_topic = render_docs("package-readiness")
    package_doc = (REPO_ROOT / "docs" / "package-readiness.md").read_text(
        encoding="utf-8"
    )
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package_json = (REPO_ROOT / "package.json").read_text(encoding="utf-8")
    combined = "\n".join([docs_topic, package_doc, readme])
    combined_lower = combined.lower()

    assert "python scripts/arcgraph_package_readiness_smoke.py" in combined
    assert "wheel/sdist" in combined
    assert "built wheel" in combined
    assert "temporary virtual environment" in combined
    assert "sample repo" in combined_lower
    assert "package.json" in combined
    assert "npm remains private/dev-only" in combined
    assert "This does not publish PyPI" in combined
    assert "This does not publish npm" in combined
    assert "This does not publish Docker/GHCR" in combined
    assert "This does not create GitHub Releases or tags" in combined
    assert "This does not make the repo public" in combined
    assert "This does not approve package publishing" in combined
    assert "This does not approve public/packaged MCP distribution" in combined
    assert "This does not replace public release/cutover approval" in combined
    # The built-in topic is the single source for what the smoke verifies.
    assert "real v1.28.1 client" in docs_topic
    assert "exact registered default surface" in docs_topic
    assert "two named feedback-enabled single-project stdio servers" in docs_topic
    assert "from one environment" in docs_topic
    assert "bounded Agent help" in docs_topic
    assert "exact registered tool contract" in docs_topic
    assert "CLI and MCP feedback record/summarize" in docs_topic
    assert "clean process shutdown" in docs_topic
    assert "fails before building when tracked, staged or untracked" in docs_topic
    assert "Every regular file in the sdist must be a file Git tracks" in docs_topic
    assert "directories are structural only" in docs_topic
    assert "Wheel members may sit only under `arcgraph/`" in docs_topic
    assert "Symbolic links, hard links, device files, FIFOs" in docs_topic
    assert "no member name may repeat" in docs_topic
    assert "RECORD must list every non-directory member exactly once" in docs_topic
    assert "an empty directory entry needs no row" in docs_topic
    assert "must name the project and version in pyproject.toml" in docs_topic
    assert "identical to the tracked file" in docs_topic
    assert "clean-status digest" in docs_topic
    assert "must leave Claude configuration and local trial state unchanged" in (
        docs_topic
    )
    assert "--artifact-dir" in package_doc
    assert "package-matrix" in package_doc
    assert "full external MCP protocol client handshake remain deferred" not in (
        docs_topic
    )
    assert 'arcgraph = "arcgraph.interfaces.cli:main"' in pyproject
    assert "[tool.hatch.build.targets.sdist]" in pyproject
    assert '"/arcgraph/tests/**"' in pyproject
    assert '"/docs/_internal/**"' in pyproject
    assert '"mcp>=2.0.0,<3.0.0"' in pyproject
    assert '"private": true' in package_json
    assert "package publishing is approved" not in combined_lower
    assert "public release is approved" not in combined_lower
    assert "pypi has been published" not in combined_lower
    assert "docker image has been published" not in combined_lower


def test_mcp_readonly_host_example_is_public_safe_and_syntax_valid() -> None:
    example_path = REPO_ROOT / "docs" / "examples" / "mcp_readonly_host.py"
    source = example_path.read_text(encoding="utf-8")
    mcp_usage = (REPO_ROOT / "docs" / "mcp-usage.md").read_text(encoding="utf-8")

    py_compile.compile(str(example_path), doraise=True)
    assert "create_mcp_app" in source
    assert "create_tool_group" in source
    assert "expose_source_snippets=False" in source
    assert "allowed_roots" in source
    assert "preferred alpha command" in source
    assert "arcgraph mcp serve" in source
    assert "docs/examples/mcp_readonly_host.py" in mcp_usage
    assert "v0.1.0rc7 external trial" in mcp_usage
    assert "installed wheel" in mcp_usage
    assert "stdio transport only" in mcp_usage
    assert "`arcgraph_record_learning` is proposal-only" in mcp_usage
    assert "`--expose-source-snippets`" in mcp_usage


def test_mcp_metrics_docs_bind_opt_in_and_privacy_boundary() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    mcp_usage = (REPO_ROOT / "docs" / "mcp-usage.md").read_text(encoding="utf-8")
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    built_in = render_docs("mcp-server") + render_docs("security-model")

    for document in (readme, mcp_usage, security, built_in):
        assert "--metrics-log" in document
        assert "local" in document.lower()
        assert "--trial-summary" in document
    assert "disabled by default" in readme
    assert "disabled unless `--metrics-log` is supplied" in mcp_usage
    assert "one event per completed tool call" in mcp_usage
    normalized_usage = " ".join(mcp_usage.split())
    assert "at most one bounded diagnostic to stderr" in normalized_usage
    normalized_security = " ".join(security.lower().split())
    assert "does not install or configure a telemetry exporter" in normalized_security
    for forbidden_field in (
        "arguments",
        "targets",
        "repository ids",
        "filesystem paths",
        "source",
        "returned payload text",
        "raw exception text",
        "client identity",
        "prompts",
    ):
        assert forbidden_field in normalized_usage


def test_metrics_html_docs_bind_the_local_privacy_boundary() -> None:
    guide = (REPO_ROOT / "docs" / "external-trial-guide.md").read_text(encoding="utf-8")
    runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    built_in = render_docs("cli-reference")

    for document in (guide, runbook, built_in):
        normalized = " ".join(document.split()).lower()
        assert "report metrics-html" in normalized
        assert "input-log path" in normalized
        assert "event timestamps" in normalized
        assert "raw error" in normalized
        assert "warning text" in normalized
        assert "warning count" in normalized
        assert "review" in normalized


def test_external_trial_single_repo_config_matches_mcp_tool_defaults() -> None:
    guide = (REPO_ROOT / "docs" / "external-trial-guide.md").read_text(encoding="utf-8")
    instructions = (REPO_ROOT / "docs" / "agent-reading-guide.md").read_text(
        encoding="utf-8"
    )
    bundle_script = (
        REPO_ROOT / "scripts" / "arcgraph_external_trial_bundle.py"
    ).read_text(encoding="utf-8")

    config_match = re.search(
        r"## 5\. Configure MCP Manually.*?```json\n(.*?)\n```",
        guide,
        flags=re.DOTALL,
    )
    assert config_match is not None
    config = json.loads(config_match.group(1))
    args = config["args"]

    assert config["command"] == "/absolute/path/to/.arcgraph-trial-venv/bin/arcgraph"
    assert args[:2] == ["mcp", "serve"]
    assert "--repo-id" not in args
    assert build_mcp_parser().parse_args([]).repo_id == "default"
    assert "arcgraph.interfaces.mcp_server" not in guide
    assert "default repo id, `default`" in guide
    assert ".git/info/exclude" in guide
    assert 'exit code 0 with `status = "warn"`' in guide
    assert "agent-reading-guide.md" in guide
    assert "TRIAL_AGENT_GUIDE" in bundle_script

    assert "MCP surface does not expose" in instructions
    for command in ("`symbol`", "`callers`", "`callees`", "`tests`", "`impact`"):
        assert command in instructions
    assert "Do not silently open a shell" in instructions


def test_current_mcp_tool_documentation_is_bound_to_capability_registry() -> None:
    usage = (REPO_ROOT / "docs" / "mcp-usage.md").read_text(encoding="utf-8")
    tool_section = usage.split("## Tool Surface", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"\| `(arcgraph_[a-z0-9_]+)` \|", tool_section))

    assert documented == set(mcp_capability_names(feedback_enabled=True))
    assert "Protocol `list_tools` is the authoritative inventory" in usage
    assert "MCP intentionally does not expose every CLI" in usage
    assert "CLI has no automatic protocol inventory" in " ".join(usage.split())


def test_agent_trial_docs_bind_multi_project_discovery_and_feedback() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "external-trial-guide.md").read_text(encoding="utf-8")
    instructions = (REPO_ROOT / "docs" / "agent-reading-guide.md").read_text(
        encoding="utf-8"
    )
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    built_in = render_docs("mcp-server") + render_docs("security-model")
    combined = "\n".join([readme, guide, instructions, security, built_in])

    for required in (
        "one installed",
        "one stdio",
        "repo_id=default",
        "--metrics-log",
        "--feedback-log",
        "arcgraph_help",
        "arcgraph_record_trial_feedback",
        "arcgraph help",
        "not feature-equivalent",
        "no network access",
        "free text",
        "Windows",
        "ACL",
    ):
        assert required.lower() in combined.lower()

    assert "list_tools" in combined
    assert "authoritative" in combined
    assert "client may filter" in combined or "client may choose" in combined
    assert "arcgraph feedback summarize" in combined
    assert "never share the raw" in combined.lower()
    usage = (REPO_ROOT / "docs" / "mcp-usage.md").read_text(encoding="utf-8")
    for document in (guide, usage):
        assert "paths must be distinct" in document
    assert "The tool is not read-only" in guide
    assert "sole optional write" in usage
    assert "not read-only" in built_in
    assert "intended review/export surface" in guide
    assert "intended export surface" in " ".join(security.split())
    assert "arcgraph_help" in guide
    assert "Agent Reading Guide" in guide


def test_current_product_docs_do_not_embed_drifting_mcp_tool_counts() -> None:
    documents = [
        (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        (REPO_ROOT / "docs" / "release_notes" / "v0.1.0-rc7.md").read_text(
            encoding="utf-8"
        ),
        (REPO_ROOT / "docs" / "mcp-usage.md").read_text(encoding="utf-8"),
        (REPO_ROOT / "docs" / "external-trial-guide.md").read_text(encoding="utf-8"),
        (REPO_ROOT / "docs" / "agent-reading-guide.md").read_text(encoding="utf-8"),
        (REPO_ROOT / "docs" / "package-readiness.md").read_text(encoding="utf-8"),
        render_docs("mcp-server"),
        render_docs("package-readiness"),
    ]
    drifting_count = re.compile(
        r"\b(?:13|14|15|thirteen|fourteen|fifteen)[ -]tool",
        flags=re.IGNORECASE,
    )

    assert all(drifting_count.search(document) is None for document in documents)


def test_alpha_operational_docs_cover_agent_boundaries() -> None:
    integrations = (REPO_ROOT / "docs" / "agent-reading-guide.md").read_text(
        encoding="utf-8"
    )
    client_setup = (REPO_ROOT / "docs" / "client-setup.md").read_text(encoding="utf-8")
    runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    guardrails = integrations

    assert "Claude Code" in integrations
    assert "Codex Or Generic MCP Client" in integrations
    assert "Cursor Or Generic MCP JSON Client" in integrations
    assert "Aider Or CLI Fallback" in integrations
    assert "Automatic agent config" in client_setup
    assert "Deferred" in client_setup
    assert "arcgraph mcp serve" in integrations
    assert "does not auto-edit" in integrations
    assert "Local wheel candidate" in client_setup
    assert "whl[mcp]" in integrations

    assert "Missing Index" in runbook
    assert "Stale Index" in runbook
    assert "Schema Mismatch" in runbook
    assert "MCP Server Starts But Index Is Unavailable" in runbook
    assert "Clean-Checkout Smoke" in runbook
    assert "Source Snippet Policy" in runbook
    assert "Windows Path, venv, And PATH Issues" in runbook
    assert "Do not commit generated `output/arcgraph` files" in runbook
    assert "node_modules/typescript" in runbook
    assert "diagnostics.jsonl" in runbook

    assert "MCP analysis, Change Safety preview/read/compute, and Agent-help" in (
        guardrails
    )
    assert "must not be described as read-only" in guardrails
    assert "Keep source snippets disabled by default" in guardrails
    assert "Do not claim any language is L4" in guardrails
    assert "Do not treat SCIP, OpenAPI, tree-sitter, or syntax-only facts as L3" in (
        guardrails
    )
    assert "Public visibility" in guardrails or "public visibility" in guardrails


def test_external_trial_docs_bind_python_scope_and_typescript_degradation() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    language_support = (REPO_ROOT / "docs" / "language-support.md").read_text(
        encoding="utf-8"
    )
    integrations = (REPO_ROOT / "docs" / "agent-reading-guide.md").read_text(
        encoding="utf-8"
    )
    client_setup = (REPO_ROOT / "docs" / "client-setup.md").read_text(encoding="utf-8")
    cli_reference = render_docs("cli-reference")
    quickstart = render_docs("quickstart")
    troubleshooting = render_docs("troubleshooting")
    built_in_package_readiness = render_docs("package-readiness")
    normalized_readme = " ".join(readme.split())

    assert "v0.1.0rc7 external-trial scope" in readme
    assert (
        "Python analysis through the installed CLI plus local stdio MCP"
        in normalized_readme
    )
    assert "outside that trial's acceptance scope" in readme
    assert (
        "Node.js and npm only when you need TypeScript/JavaScript analysis tests"
        not in readme
    )
    assert "must be resolvable at runtime" in quickstart
    assert "node_modules/typescript" in language_support
    assert "typescript_frontend_unavailable" in language_support
    assert "summary.json" in language_support
    assert "diagnostics.jsonl" in language_support
    assert "explicitly Python-only scope" in quickstart
    assert "acceptable only for the Python-only v0.1.0rc7 trial scope" in (
        troubleshooting
    )
    assert "outside that trial's acceptance scope" in cli_reference
    assert "clears `PYTHONPATH` and `NODE_PATH`" in built_in_package_readiness
    assert "`summary.json`" in built_in_package_readiness
    assert "`diagnostics.jsonl`" in built_in_package_readiness
    assert "does not auto-edit" in integrations
    assert "The examples below are manual templates only" in integrations
    assert "Local wheel candidate" in client_setup
    assert "whl[mcp]" in integrations


def test_source_checkout_smoke_script_is_syntax_valid() -> None:
    py_compile.compile(
        str(REPO_ROOT / "scripts" / "arcgraph_source_checkout_smoke.py"),
        doraise=True,
    )


def test_clean_checkout_smoke_docs_cover_matrix_and_release_boundaries() -> None:
    clean_smoke = (REPO_ROOT / "docs" / "clean-checkout-smoke.md").read_text(
        encoding="utf-8"
    )
    runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    notes = (REPO_ROOT / "docs" / "package-readiness.md").read_text(encoding="utf-8")
    combined = "\n".join([clean_smoke, runbook, notes])
    combined_lower = combined.lower()

    assert "python scripts/arcgraph_clean_checkout_smoke.py" in combined
    assert "source-checkout editable install" in combined_lower
    assert "temporary Git checkout" in combined
    assert "concrete Git commit" in combined
    assert "CLI help" in combined
    assert "MCP help" in combined
    assert "source-checkout smoke" in combined
    assert "does not authorize package publishing" in combined_lower
    assert "does not authorize public release" in combined_lower
    assert "does not authorize public/packaged mcp distribution" in combined_lower
    assert "does not auto-configure" in combined
    assert "does not replace the final public pre-cutover gate" in combined
    assert "wheel or sdist package installation readiness" in combined_lower
    assert "package readiness smoke for local wheel/sdist build" in combined_lower
    assert "real v1.28.1 client handshakes" in combined
    assert "HTTP/network MCP transport" in combined
    assert "public release is approved" not in combined_lower


def test_clean_checkout_smoke_script_is_syntax_valid() -> None:
    py_compile.compile(
        str(REPO_ROOT / "scripts" / "arcgraph_clean_checkout_smoke.py"),
        doraise=True,
    )


def test_security_model_docs_cover_local_safety_boundaries() -> None:
    security = render_docs("security-model")

    assert "local-only" in security
    assert "no telemetry" in security
    assert "path containment" in security
    assert "snippet redaction" in security
    assert "loopback-only" in security
    assert "visual serve" in security
    assert "headers" in security
    assert "cookies" in security
    assert "request/response bodies" in security
    assert "runtime-only" in security


def test_release_checklist_docs_cover_full_gate() -> None:
    checklist = render_docs("release-checklist")

    assert "git status --porcelain=v1 --untracked-files=all" in checklist
    assert "ARCGRAPH_REQUIRE_TS" in checklist
    assert "python -m pytest arcgraph/tests scripts/tests -q" in checklist
    assert "python -m black arcgraph scripts --check" in checklist
    assert "python -m ruff check arcgraph scripts" in checklist
    assert "python -m bandit -c pyproject.toml" in checklist
    assert "npx --yes npm@11.12.1 audit --audit-level=high --json" in checklist
    assert "python -m pip install --upgrade pip setuptools wheel" in checklist
    assert "python -m pip freeze --exclude-editable" in checklist
    assert (
        "python -m pip_audit --strict --disable-pip --no-deps --requirement"
        in checklist
    )
    assert "python -m build" in checklist
    assert "python scripts/arcgraph.py build" in checklist
    assert "python scripts/arcgraph.py ci" in checklist
    assert "python scripts/arcgraph_release_gate.py" in checklist
    assert "python scripts/arcgraph_release_candidate_check.py" in checklist
    assert "v0.1-rc7-smoke.json" in checklist
    assert "--sdist dist/arcgraph-0.1.0rc7.tar.gz" in checklist
    assert "`clean-rebuild-identical` check" in checklist
    assert "clones that HEAD into a fresh directory" in checklist
    assert "identical bytes as the two files given" in checklist
    assert "pinned to an exact version in `pyproject.toml`" in checklist
    assert "the bundle assembler accepts only `pass`" in checklist
    assert "arcgraph_external_trial_bundle.py" in checklist
    assert "cannot run while GitHub Actions is disabled" in checklist

    # docs and README call the checklist a fixed, ordered sequence, so it must
    # actually run top to bottom: dependencies first, then checks that need them.
    ordered = [
        "npm ci",
        "python -m pip install --upgrade",
        "python -m pip install -e",
        "python -m pytest",
        "python -m black",
        "python -m ruff",
        "python -m bandit",
        "python -m pip_audit",
        "npm@11.12.1 audit",
        "python -m build",
        "python scripts/arcgraph_package_readiness_smoke.py",
        "python scripts/arcgraph.py build",
        "python scripts/arcgraph.py ci",
        "python scripts/arcgraph_release_gate.py",
        "python scripts/arcgraph_release_candidate_check.py",
    ]
    positions = [checklist.index(step) for step in ordered]
    assert positions == sorted(positions), "release-checklist steps are out of order"
    assert "arcgraph benchmark suite" in checklist
    assert "npm ci" in checklist
    assert "arcgraph docs schema-governance" in checklist
    assert "arcgraph docs frontend-contract" in checklist
    assert "arcgraph docs security-model" in checklist
    assert "full `arcgraph build`" in checklist
    assert "incremental evidence imports" in checklist
    assert "automatic plugin discovery" in checklist


def test_schema_governance_docs_cover_rebuild_policy() -> None:
    governance = render_docs("schema-governance")

    assert "Index schema `1.0.0`" in governance
    assert "read schema `1.4.0`" in governance
    assert "`index_schema_version`" in governance
    assert "schema_compatibility" in governance
    assert "0.6.0 -> 1.0.0" in governance
    assert "full-rebuild" in governance
    assert "full `arcgraph build`" in governance
    assert "incremental" in governance
    assert "runtime-only" in governance
    assert "schema_compatibility_steps" in governance
    assert "schema_direct_compatibility_step" in governance


@pytest.mark.parametrize(
    "topic",
    [
        "agent-cli-contract",
        "mcp-server",
        "schema-governance",
        "migration-notes",
    ],
)
def test_read_contract_topics_document_structured_warning_elements(
    topic: str,
) -> None:
    """The 1.1 shape change must be actionable to existing JSON consumers."""

    rendered = render_docs(topic)

    assert "read schema `1.4.0`" in rendered
    assert "heterogeneous JSON array" in rendered
    assert "string" in rendered
    assert "structured object" in rendered
    assert "join(warnings)" in rendered


@pytest.mark.parametrize(
    "relative_path",
    [
        "README.md",
        "docs/agent-reading-guide.md",
        "docs/mcp-usage.md",
    ],
)
def test_user_entrypoints_document_structured_warning_elements(
    relative_path: str,
) -> None:
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

    assert "read schema `1.4.0`" in text
    assert "heterogeneous JSON array" in text
    assert "string" in text
    assert "structured object" in text
    assert "join(warnings)" in text


def test_frontend_contract_docs_cover_inventory_and_extension_boundary() -> None:
    contract = render_docs("frontend-contract")

    assert "LanguageFrontend" in contract
    assert "FrontendGraphFragment" in contract
    assert "python-v1-compat-shim" in contract
    assert "typescript-static" in contract
    assert "scip-protocol" in contract
    assert "--scip-graph-index" in contract
    assert "--scip-index" in contract
    assert "automatic plugin discovery" in contract
    assert "package entry point loading" in contract
    assert "remote plugin" in contract
    assert "plugin sandbox" in contract
    assert "confirmed" in contract
    assert "runtime-only" in contract
    assert "golden matrix" in contract
    assert "should not claim complete language semantics" in contract


def test_readme_points_to_current_release_readiness_workflows() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "npm ci" in readme
    assert "arcgraph context" in readme
    assert "arcgraph explain" in readme
    assert "arcgraph evidence status" in readme
    assert "arcgraph benchmark suite" in readme
    assert "arcgraph docs quickstart" in readme
    assert "arcgraph docs security-model" in readme
    assert "arcgraph docs release-checklist" in readme
    assert "arcgraph docs frontend-contract" in readme
    assert "arcgraph docs schema-governance" in readme
    assert "arcgraph docs agent-cli-contract" in readme
    assert "arcgraph docs mcp-server" in readme
    assert "arcgraph docs source-checkout-smoke" in readme
    assert "docs/agent-reading-guide.md" in readme
    assert "docs/runbook.md" in readme
    assert "docs/clean-checkout-smoke.md" in readme
    assert "docs/examples/mcp_readonly_host.py" in readme
    assert "python scripts/arcgraph_clean_checkout_smoke.py" in readme
    assert "arcgraph mcp serve --repo-root . --output-dir output/arcgraph" in readme
    assert "Public package publication" in readme
    assert "automatic plugin ecosystem" not in readme.lower()
    assert "full program correctness" not in readme.lower()


def test_release_notes_describe_current_public_safe_scope() -> None:
    release_notes = (REPO_ROOT / "docs" / "release_notes" / "v0.1.0-rc7.md").read_text(
        encoding="utf-8"
    )
    stale_python_baseline = "392" + "698b"

    assert stale_python_baseline not in release_notes
    assert "Python V2 Second-Phase Closeout" not in release_notes
    assert "Non-Python frontend proof of concept" not in release_notes
    assert "local-first code semantic graph engine" in release_notes
    assert "Python is a native L3 semantic static frontend" in release_notes
    assert (
        "TypeScript and JavaScript are native L3 semantic static frontends"
        in release_notes
    )
    assert "validated" in release_notes
    assert "external semantic extractor payloads" in release_notes
    assert "arcgraph build --scip-graph-index PATH" in release_notes
    assert "arcgraph build --openapi-spec PATH" in release_notes
    assert "not default" in release_notes
    assert "live compiler-backed extraction" in release_notes
    assert "No language is claimed as L4" in release_notes
    assert "read schema `1.3.0`" in release_notes
    assert "blast_radius" in release_notes
    assert "stable-ID-only" in release_notes
    assert "name`, `qualname`, `path`, `start_line`, and `end_line" in release_notes


def test_governance_docs_do_not_make_release_overclaims() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    corpus = "\n".join([readme, *(render_docs(topic) for topic in DOC_TOPICS)]).lower()

    forbidden_positive_claims = [
        "guarantees full program correctness",
        "guarantee full program correctness",
        "complete language semantics are supported",
        "supports every language",
        "supports all languages",
        "automatic plugin ecosystem",
        "automatic plugin discovery is supported",
        "remote plugin execution is supported",
        "telemetry enabled",
        "sends telemetry",
        "hosted telemetry",
    ]

    for claim in forbidden_positive_claims:
        assert claim not in corpus
    assert "not a compiler" in corpus
    assert "no telemetry" in corpus
    assert "should not claim complete language semantics" in corpus


def test_current_migration_notes_preserve_payload_and_index_disclosures() -> None:
    """Public migration guidance preserves observable payload/index changes."""

    notes = (REPO_ROOT / "docs" / "release_notes" / "v0.1.0-rc7.md").read_text(
        encoding="utf-8"
    )
    notes_flat = " ".join(notes.split())

    assert "--trial-summary" in notes
    assert "`arcgraph_help`" in notes
    assert "`arcgraph_record_trial_feedback`" in notes
    assert "read schema `1.3.0`" in notes
    assert "blast_radius" in notes
    assert "stable-ID-only" in notes
    assert "token_jaccard=" in notes
    assert "token_overlap=" in notes
    assert "not byte/behavior compatible" in notes_flat
    notes_lower = notes_flat.lower()
    assert "`.mjs`" in notes_flat
    assert "`route`" in notes_flat
    assert "`similar_to`" in notes_flat
    assert "`BuildWarning.frontend_name`" in notes_flat
    assert "fails closed" in notes_flat or "fail closed" in notes_flat
    assert "unchanged local module" in notes_flat
    assert "persisted module identities" in notes_flat
    assert "declaration context" in notes_flat
    assert "complete import topology" in notes_flat
    assert "Same-repository linked Git worktrees" in notes_flat
    assert "submodules" in notes_flat
    assert "coexisting `.jsx`, `.tsx`, and `.js` files" in notes_lower
    assert "changes affected node ids" in notes_lower
    assert "compiler provenance" in notes_lower
    assert "sourcemappingurl" in notes_lower


def test_current_candidate_identity_notes_and_trial_guide_are_consistent() -> None:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    notes_index = (REPO_ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    strategy = (REPO_ROOT / "docs" / "release-tooling.md").read_text(encoding="utf-8")
    notes = (REPO_ROOT / "docs" / "release_notes" / "v0.1.0-rc7.md").read_text(
        encoding="utf-8"
    )
    guide = (REPO_ROOT / "docs" / "external-trial-guide.md").read_text(encoding="utf-8")
    agent_workflows = (REPO_ROOT / "docs" / "agent-reading-guide.md").read_text(
        encoding="utf-8"
    )
    guide_flat = " ".join(guide.split())
    guide_lower = guide_flat.lower()
    package_docs = (REPO_ROOT / "docs" / "package-readiness.md").read_text(
        encoding="utf-8"
    )
    built_in = render_docs("release-checklist") + render_docs("package-readiness")

    assert pyproject["project"]["version"] == "0.1.0rc7"
    for text in (readme, notes_index, strategy, notes, guide, built_in):
        assert "0.1.0rc7" in text
    assert "arcgraph_external_trial_bundle.py" in readme
    assert "arcgraph_external_trial_bundle.py" in strategy
    assert "--artifact-dir" in package_docs
    assert "--artifact-dir" in render_docs("package-readiness")
    assert "Change Preflight" in notes
    assert "comparison projection to `1.2`" in notes
    assert guide.count("arcgraph-0.1.0rc7-py3-none-any.whl[mcp]") == 2
    assert "arcgraph 0.1.0rc7" in guide
    assert "arcgraph version --json" in guide
    assert "trial setup --client claude --dry-run" in guide
    assert "remote-ci.json" in guide
    assert "`push` run on `main`" in guide
    for text in (readme, strategy, guide, package_docs, built_in):
        assert "arcgraph-0.1.0rc5" not in text
        assert "arcgraph-v0.1.0rc5-COMMIT" not in text

    for required in (
        "Python analysis through the installed CLI",
        "local stdio MCP",
        "TypeScript and JavaScript",
        "SHA256SUMS",
        "arcgraph --version",
        ".arcgraph-trial/",
        "arcgraph build",
        "arcgraph current",
        "arcgraph status",
        "arcgraph context YOUR_SYMBOL",
        "arcgraph sync --if-stale",
        "arcgraph ops prune --keep-builds 1",
        "--metrics-log",
        "python -m pip uninstall arcgraph",
        "manual",
        "secrets",
        "absolute user/repository paths",
    ):
        assert (
            required in guide
            or required in guide_flat
            or required.lower() in guide_lower
        )
    assert "does not auto-configure" in guide
    assert "does not publish" in guide_flat
    assert "fixed product invariant, not evidence" in guide_flat
    assert "must report `claude_config_modified = false`" not in guide_flat
    assert "selection_source" in guide
    assert "distribution_record" in guide
    assert "warn" in guide and "review" in guide
    for text in (notes, agent_workflows):
        lowered = " ".join(text.lower().split())
        assert "emit an explicit unresolved dynamic edge" not in lowered
        assert "represented as unresolved dynamic edges" not in lowered
        assert "edge" in lowered
        assert "manufactur" in lowered or "does not create" in lowered
    assembler = (REPO_ROOT / "scripts" / "arcgraph_external_trial_bundle.py").read_text(
        encoding="utf-8"
    )
    assert '"scope": "bundle_assembler_execution"' in assembler
    assert "_load_remote_ci_evidence" in assembler
    assert '"push commits"' not in assembler


def test_read_schema_docs_disclose_both_truncation_unknown_kinds() -> None:
    current_contract_docs = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "change-preflight.md",
        REPO_ROOT / "docs" / "mcp-usage.md",
        REPO_ROOT / "docs" / "agent-reading-guide.md",
        REPO_ROOT / "docs" / "release_notes" / "v0.1.0-rc7.md",
    ]

    for path in current_contract_docs:
        text = path.read_text(encoding="utf-8")
        assert "truncated_scope" in text, path
        assert "analysis_truncated_scope" in text, path


def test_current_mcp_docs_do_not_claim_the_rc7_server_is_source_checkout_only() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    usage = (REPO_ROOT / "docs" / "mcp-usage.md").read_text(encoding="utf-8")
    built_in = render_docs("cli-reference") + render_docs("mcp-server")

    assert "source-checkout only" not in built_in
    assert "source-checkout users who manually" not in usage
    assert "installed-wheel local stdio server" in built_in
    assert "installed wheel" in usage
    assert "installed wheel" in readme


def test_external_trial_commands_keep_one_explicit_index_path() -> None:
    guide = (REPO_ROOT / "docs" / "external-trial-guide.md").read_text(encoding="utf-8")
    explicit_prefix = (
        "arcgraph --repo-root PROJECT_ROOT "
        "--output-dir PROJECT_ROOT/.arcgraph-trial/index"
    )

    assert f"{explicit_prefix} sync --if-stale" in guide
    assert f"{explicit_prefix} current" in guide
    assert f"{explicit_prefix} ops prune --keep-builds 1" in guide
    assert "/absolute/path/to/project/output/arcgraph" not in guide
    assert "--output-dir /absolute/path/to/project/.arcgraph-trial/index" in guide


def test_rc5_docs_disclose_private_state_recovery_and_alias_policy() -> None:
    runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "external-trial-guide.md").read_text(encoding="utf-8")
    release_notes = (REPO_ROOT / "docs" / "release_notes" / "v0.1.0-rc7.md").read_text(
        encoding="utf-8"
    )
    contract = (REPO_ROOT / "docs" / "external-trial-guide.md").read_text(
        encoding="utf-8"
    )

    for text in (runbook, guide, contract):
        assert "symbolic" in text
        assert "parent" in text
    assert "`0700`" in runbook
    assert "`0600`" in runbook
    assert "`0400`" in runbook
    assert "canonical" in guide
    assert "restrictive umask" in contract
    assert "bounded ancestor chain" in guide
    assert "first pre-existing non-private ancestor" in " ".join(guide.split())
    assert "complete parent chain" not in release_notes


def test_rc5_feedback_machine_contract_tracks_runtime_codes_and_limits() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    integrations = (REPO_ROOT / "docs" / "agent-reading-guide.md").read_text(
        encoding="utf-8"
    )
    runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "external-trial-guide.md").read_text(encoding="utf-8")
    contract = (REPO_ROOT / "docs" / "external-trial-guide.md").read_text(
        encoding="utf-8"
    )
    error_codes = {
        TrialFeedbackInputError.error_code,
        TrialFeedbackConflictError.error_code,
        TrialFeedbackStorageError.error_code,
        TrialFeedbackLimitError.error_code,
        TrialFeedbackDisabledError.error_code,
        METRICS_LOG_INVALID_ENCODING_ERROR_CODE,
        METRICS_LOG_UNREADABLE_ERROR_CODE,
    }
    request_schema = TrialFeedbackRequest.model_json_schema()
    identifier_limit = request_schema["properties"]["tool_name"]["maxLength"]
    warning_limit = request_schema["properties"]["warning_kinds"]["maxItems"]
    documented_limits = {
        f"1–{identifier_limit}",
        f"at most {warning_limit} unique",
        f"{MAX_FEEDBACK_RECORD_BYTES:,} UTF-8 bytes",
        f"{MAX_FEEDBACK_FILE_BYTES // (1024 * 1024)} MiB",
    }

    assert warning_limit == MAX_WARNING_KINDS
    for text in (guide, contract):
        normalized = " ".join(text.split())
        missing_codes = {code for code in error_codes if code not in text}
        missing_limits = {
            limit for limit in documented_limits if limit not in normalized
        }
        assert not missing_codes
        assert not missing_limits
    assert "feedback-machine-contract" in readme
    assert "feedback-machine-contract" in integrations
    assert "could not be inspected" in runbook
    assert "directory `fsync`" in runbook
    assert "record schema `1.0.0`" in guide
    assert "persisted read vocabulary is append-only" in " ".join(contract.split())
    assert "remain readable" in runbook


def _markdown_docs() -> list[Path]:
    return [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        *sorted(
            path
            for path in (REPO_ROOT / "docs").rglob("*.md")
            if not any(
                part.startswith("_")
                for part in path.relative_to(REPO_ROOT / "docs").parts
            )
        ),
    ]


def _doc_sources() -> list[tuple[str, str]]:
    """Every document a user can be told to copy commands out of.

    Returns ``(source_name, text)``. The built-in ``arcgraph docs`` topics are
    included deliberately: an earlier version of this guard read only the
    markdown tree, so 20 Windows-path commands across 7 topics stayed shipped
    and unguarded even while the markdown side was clean.
    """
    sources = [
        (path.relative_to(REPO_ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for path in _markdown_docs()
    ]
    sources += [(f"arcgraph docs {topic}", render_docs(topic)) for topic in DOC_TOPICS]
    return sources


def _fence_language(info_string: str) -> str:
    """Normalise a fence info string to its language token.

    CommonMark allows attributes after the language (``pwsh title="Windows"``)
    and imposes no casing. Comparing the raw string against a set let both
    ```` ```PowerShell ```` and ```` ```pwsh title="Windows" ```` slip past
    every check, since neither matched an entry exactly.
    """
    return (info_string.strip().split() or ["text"])[0].lower()


#: A top-level CommonMark fence delimiter: up to three leading spaces followed
#: by three or more backticks or tildes. Matching only a column-zero delimiter
#: made an indented ``~~~PowerShell`` block invisible to every documentation
#: guard.
_FENCE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")


def _fence_open(line: str) -> tuple[str, str] | None:
    """Return ``(marker, raw_info)`` when ``line`` is a fence delimiter.

    The info string is returned raw: a closing fence is defined by carrying no
    info at all, and ``_fence_language`` maps an empty string to ``"text"``,
    which would make every closing fence look like an opening one.
    """
    match = _FENCE.match(line)
    if match is None:
        return None
    marker, info = match.group("marker"), match.group("info")
    # CommonMark forbids a backtick anywhere in a backtick fence's info
    # string. Treating such a line as an opener hides inline code that
    # CommonMark renders as ordinary document content.
    if marker[0] == "`" and "`" in info:
        return None
    return marker, info


def _fence_closes(line: str, marker: str) -> bool:
    """A fence closes on a run of the same character, at least as long."""
    delimiter = _fence_open(line)
    if delimiter is None:
        return False
    closing, info = delimiter
    return closing[0] == marker[0] and len(closing) >= len(marker) and not info.strip()


def _fence_blocks_with_lines(
    text: str,
) -> tuple[list[tuple[str, list[tuple[int, str]]]], set[int]]:
    """Return fenced bodies with source lines and every delimiter line.

    CommonMark treats an unclosed fence as extending to EOF, so an active block
    is preserved at the end instead of being dropped. All documentation guards
    consume this scan to avoid subtly different notions of whether a line is
    inside a fence.
    """
    blocks: list[tuple[str, list[tuple[int, str]]]] = []
    delimiter_lines: set[int] = set()
    marker: str | None = None
    lang = ""
    body: list[tuple[int, str]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if marker is None:
            delimiter = _fence_open(line)
            if delimiter is not None:
                delimiter_lines.add(number)
                marker, lang = delimiter[0], _fence_language(delimiter[1])
                body = []
            continue
        if _fence_closes(line, marker):
            delimiter_lines.add(number)
            blocks.append((lang, body))
            marker = None
            lang = ""
            body = []
            continue
        body.append((number, line))
    if marker is not None:
        blocks.append((lang, body))
    return blocks, delimiter_lines


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Return (language, body) for every fenced block in a markdown document."""
    blocks, _ = _fence_blocks_with_lines(text)
    return [(lang, "\n".join(line for _, line in body)) for lang, body in blocks]


# A Windows-style path: two path segments joined by a backslash. Matches
# ``output\arcgraph`` and ``C:\path`` but not a lone trailing backslash.
_WINDOWS_PATH = re.compile(r"[\w.$*-]\\[\w.$*-]")

# Documentation placeholders such as ``<target>``. They are not shell syntax,
# so they are normalised away before a block is handed to ``bash -n``.
_PLACEHOLDER = re.compile(r"<[a-zA-Z][\w-]*>")

# PowerShell constructs that must never appear inside a ```bash block.
_POWERSHELL_ONLY = (
    "$env:",
    "New-Item",
    "Activate.ps1",
    "-ExecutionPolicy",
    "| Out-Null",
)

# The only ```powershell blocks allowed, as (doc path, required marker).
# Each entry must be a Windows-specific instruction that has a POSIX
# counterpart elsewhere in the same document.
_ALLOWED_POWERSHELL_BLOCKS = {
    ("README.md", ".\\.venv\\Scripts\\Activate.ps1"),
    ("README.md", "install-arcgraph-precision-tools.ps1"),
    ("docs/external-trial-guide.md", "Get-Content SHA256SUMS"),
    (
        "docs/external-trial-guide.md",
        ".\\.arcgraph-trial-venv\\Scripts\\Activate.ps1",
    ),
    ("docs/examples/source-checkout-smoke.md", ".\\.venv\\Scripts\\Activate.ps1"),
}


def test_doc_source_corpus_covers_markdown_and_built_in_topics() -> None:
    """Guard the corpus itself, by identity, not by count.

    Every check below is only as good as what it is fed. The original miss was
    not a weak assertion but a corpus that silently excluded the built-in
    topics, so this test names both source classes explicitly.
    """
    names = {name for name, _ in _doc_sources()}

    assert "README.md" in names
    assert {f"arcgraph docs {topic}" for topic in DOC_TOPICS} <= names
    assert {
        path.relative_to(REPO_ROOT).as_posix() for path in _markdown_docs()
    } <= names
    assert all(text.strip() for _, text in _doc_sources())


# A claim about whether the repository is private or public is stale the moment
# its visibility changes, so shipped documents must not make one.
_VISIBILITY_STATE_CLAIMS = re.compile(
    r"pre-public|not a public github|while the repository remains private"
    r"|this private repository|repository is private"
    r"|public repository visibility remains unapproved",
    flags=re.IGNORECASE,
)


def test_public_docs_do_not_state_repository_visibility() -> None:
    sources = _doc_sources() + [
        (name, (REPO_ROOT / name).read_text(encoding="utf-8"))
        for name in ("SUPPORT.md", "SECURITY.md", "RELEASE_NOTES.md")
    ]
    offenders = [
        f"{name}: {match.group(0)!r}"
        for name, text in sources
        for match in _VISIBILITY_STATE_CLAIMS.finditer(text)
    ]

    assert offenders == [], "\n".join(offenders)


def test_shell_examples_are_posix_first() -> None:
    """Every runnable shell example must work when pasted into a POSIX shell.

    A Windows path pasted into bash does not fail loudly: ``output\\arcgraph\\x``
    is read as ``outputarcgraphx``, so the command exits 0 and writes the wrong
    file. That silent-wrong-result mode is what this test exists to prevent.
    """
    offenders: list[str] = []
    powershell_blocks: set[tuple[str, str]] = set()

    for name, text in _doc_sources():
        for lang, body in _fenced_blocks(text):
            if lang in _POWERSHELL_FENCES:
                matched = {
                    entry
                    for entry in _ALLOWED_POWERSHELL_BLOCKS
                    if entry[0] == name and entry[1] in body
                }
                if not matched:
                    offenders.append(f"{name}: unapproved ```powershell block")
                powershell_blocks |= matched
                continue
            if lang not in {"bash", "sh", "shell"}:
                continue
            for marker in _POWERSHELL_ONLY:
                if marker in body:
                    offenders.append(f"{name}: ```{lang} block contains {marker!r}")
            for line in body.split("\n"):
                if _WINDOWS_PATH.search(line):
                    offenders.append(
                        f"{name}: ```{lang} block has Windows path: {line.strip()!r}"
                    )

    assert offenders == [], "POSIX-unsafe shell examples:\n" + "\n".join(offenders)
    # Assert the exempted set by identity, so deleting a Windows fallback is
    # caught just as loudly as adding an unapproved one.
    assert powershell_blocks == _ALLOWED_POWERSHELL_BLOCKS


def test_no_windows_paths_anywhere_a_user_can_copy_from() -> None:
    """Windows paths must not leak into prose, inline code, or built-in docs.

    Built-in topics render as prose bullets with inline code rather than fenced
    blocks, so a fence-only check never inspected them.
    """
    offenders: list[str] = []
    for name, text in _doc_sources():
        blocks, delimiter_lines = _fence_blocks_with_lines(text)
        powershell_body_lines = {
            number
            for language, body in blocks
            if language in _POWERSHELL_FENCES
            for number, _ in body
        }
        for number, line in enumerate(text.split("\n"), start=1):
            if number in delimiter_lines or number in powershell_body_lines:
                continue
            if _WINDOWS_PATH.search(line):
                offenders.append(f"{name}:{number}: {line.strip()!r}")

    assert offenders == [], "Windows paths outside PowerShell blocks:\n" + "\n".join(
        offenders
    )


def test_bash_blocks_are_valid_bash() -> None:
    """A ```bash block must actually parse as bash.

    Relabelling a PowerShell block as ``bash`` passes every substring check
    while still failing for the reader. ``bash -n`` parses without executing,
    which catches the syntax the marker list does not enumerate.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip(
            "bash syntax validation runs in the required Linux quality lane; "
            "this host has no bash executable"
        )

    offenders: list[str] = []
    checked = 0
    for name, text in _doc_sources():
        for lang, body in _fenced_blocks(text):
            if lang not in {"bash", "sh", "shell"} or not body.strip():
                continue
            checked += 1
            result = subprocess.run(
                [bash, "-n"],
                input=_PLACEHOLDER.sub("PLACEHOLDER", body),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                first = result.stderr.strip().splitlines()
                offenders.append(f"{name}: {first[0] if first else 'bash -n failed'}")

    assert checked > 50, f"only {checked} bash blocks inspected; extractor is broken"
    assert (
        offenders == []
    ), "blocks labelled bash that bash cannot parse:\n" + "\n".join(offenders)


def test_bash_syntax_contract_skips_cleanly_when_bash_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(pytest.skip.Exception, match="required Linux quality lane"):
        test_bash_blocks_are_valid_bash()


def test_pip_extras_are_quoted_for_zsh() -> None:
    """``pip install -e .[mcp]`` is a glob in zsh, the macOS default shell.

    Unquoted, it fails with ``no matches found`` before pip ever runs.
    """
    offenders: list[str] = []
    for name, text in _doc_sources():
        for number, line in enumerate(text.split("\n"), start=1):
            if re.search(r"install -e \.\[", line):
                offenders.append(f"{name}:{number}: {line.strip()!r}")

    assert offenders == [], "unquoted pip extras:\n" + "\n".join(offenders)


def test_migration_notes_document_the_bounded_relation_default() -> None:
    """The behaviour change must be discoverable by an existing caller."""
    notes = render_docs("migration-notes")

    assert "--raw" in notes
    assert "`--compact` is still accepted" in notes
    assert "bounded agent payload by default" in notes


def test_migration_notes_document_lifecycle_keying_and_ci_field_rename() -> None:
    """Caller-visible identity and field changes must be discoverable."""

    notes = render_docs("migration-notes")

    assert "stable callsite subject" in notes
    assert "diagnostic_lifecycle.dedupe_key" in notes
    assert "derived when diagnostics are read" in notes
    assert "upgrades with no re-key" in notes
    assert "reports every unresolved diagnostic as `new` once" in notes
    # The unconditional claim is wrong: the key is derived at read time, so an
    # index that already carries stable subjects re-keys nothing. Keep the
    # phrasing that asserted it out of the notes.
    assert "This is a one-time re-key" not in notes
    assert "exceptions_present" in notes
    assert "exception_budget_declared" in notes


@pytest.mark.parametrize("command", ["callers", "callees", "impact"])
def test_relation_commands_expose_raw_and_treat_compact_as_optional(
    command: str,
) -> None:
    """Bind the documented contract to the parser instead of to prose.

    Asserting against ``build_parser()`` means a future edit that removes
    ``--raw``, or that makes ``--compact`` required again, fails here rather
    than leaving the migration note describing a CLI that no longer exists.
    """
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    sub = subparsers.choices[command]
    options = {
        option: action for action in sub._actions for option in action.option_strings
    }

    assert "--raw" in options
    assert "--compact" in options
    # Defaulting to None is what lets the handler tell an explicit option apart
    # from an argparse fill-in, which is how --raw refuses shaping it cannot do.
    for flag in ("--max-results", "--detail-level", "--include-source"):
        assert options[flag].default is None, flag
    assert options["--raw"].default is False
    assert options["--compact"].default is False


CLI_MODULE_LINE_BUDGET = 2800


def test_cli_modules_stay_within_their_split() -> None:
    """Keep ``cli.py`` from growing back into a single 3900-line module.

    The budget is deliberately close to the current size: it should force a
    new command group into its own module rather than quietly accumulate.
    """
    cli = REPO_ROOT / "arcgraph" / "interfaces" / "cli.py"
    line_count = len(cli.read_text(encoding="utf-8").splitlines())

    assert line_count <= CLI_MODULE_LINE_BUDGET, (
        f"cli.py is {line_count} lines (budget {CLI_MODULE_LINE_BUDGET}). "
        "Extract the new command group into its own arcgraph/interfaces/cli_*.py."
    )


def test_cli_group_modules_do_not_import_the_dispatch_module() -> None:
    """Dependencies run cli_support -> cli_<group> -> cli, never back.

    A group module importing ``cli`` would make the split circular and would
    reintroduce the import cycle the shared-helper module exists to avoid.
    """
    interfaces = REPO_ROOT / "arcgraph" / "interfaces"
    offenders = []
    for module in sorted(interfaces.glob("cli_*.py")):
        source = module.read_text(encoding="utf-8")
        for number, line in enumerate(source.split("\n"), start=1):
            stripped = line.strip()
            if stripped.startswith(
                (
                    "from arcgraph.interfaces.cli import",
                    "from arcgraph.interfaces import cli",
                )
            ) and not stripped.startswith("from arcgraph.interfaces.cli_"):
                offenders.append(f"{module.name}:{number}: {stripped}")

    assert offenders == [], "cli_* modules must not import cli:\n" + "\n".join(
        offenders
    )


def test_every_top_level_command_survives_the_split() -> None:
    """The parser must still register every command the docs promise."""
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    documented = _documented_arcgraph_commands(render_docs("cli-reference"))

    registered = set(subparsers.choices)
    # Commands are documented as full `arcgraph <command> ...` invocations;
    # the token after the program name is the subcommand.
    named = {
        parts[1]
        for command in documented
        if len(parts := command.split()) > 1 and not parts[1].startswith("-")
    }
    assert named
    assert named == registered


# A PowerShell cmdlet is ``Verb-Noun`` with both halves capitalised, which no
# POSIX command uses. Matching the *shape* rather than a verb list is the
# point: an enumerated list of 20 verbs let ``ConvertTo-Json``, ``Export-Csv``,
# ``Import-Module``, ``Join-Path`` and ``Split-Path`` straight through.
_POWERSHELL_CMDLET = re.compile(r"\b[A-Z][A-Za-z]{2,}-[A-Z][A-Za-z]+\b")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

#: Fence languages that mean PowerShell. ``pwsh`` and ``ps1`` were neither
#: scanned as shell nor matched by the ```powershell allowlist, so a block in
#: either language was invisible to every check.
_POWERSHELL_FENCES = {"powershell", "pwsh", "ps1"}
_POSIX_FENCES = {"bash", "sh", "shell"}
_WINDOWS_LABEL = "Windows PowerShell"


def _command_spans(text: str) -> list[tuple[int, int, str]]:
    """Inline code and POSIX shell bodies — the places a reader copies from.

    Returns ``(line_number, column_offset, span)``. The column matters: a
    line-wide exemption let ``Run `ConvertTo-Json`; Windows PowerShell
    alternative: `Get-Command`.`` pass, because the label anywhere on the line
    excused every span on it.
    """
    spans: list[tuple[int, int, str]] = []
    blocks, delimiter_lines = _fence_blocks_with_lines(text)
    fenced_lines = {number: language for language, body in blocks for number, _ in body}
    for number, line in enumerate(text.split("\n"), start=1):
        if number in delimiter_lines:
            continue
        fence = fenced_lines.get(number)
        if fence is not None:
            if fence in _POSIX_FENCES:
                spans.append((number, 0, line))
            continue
        for match in _INLINE_CODE.finditer(line):
            spans.append((number, match.start(1), match.group(1)))
    return spans


def _powershell_offenders(name: str, text: str) -> list[str]:
    """Cmdlets a POSIX reader could copy, plus PowerShell fences off the allowlist.

    Extracted as a function so the extractor itself can be tested with positive
    samples. While the checks lived inline, replacing the extractor with a
    constant empty list still passed every PowerShell test.
    """
    offenders: list[str] = []
    lines = text.split("\n")
    for number, column, span in _command_spans(text):
        line = lines[number - 1]
        label = line.find(_WINDOWS_LABEL)
        for match in _POWERSHELL_CMDLET.finditer(span):
            # Exempt only spans that follow the Windows label on that line, so
            # the label cannot excuse a cmdlet presented as the POSIX form.
            if 0 <= label < column:
                continue
            offenders.append(f"{name}:{number}: {match.group(0)} in {span!r}")

    for lang, body in _fenced_blocks(text):
        if lang not in _POWERSHELL_FENCES or lang == "powershell":
            continue
        offenders.append(f"{name}: ```{lang} block is PowerShell outside the allowlist")
    return offenders


def test_no_powershell_cmdlets_outside_windows_fallback_blocks() -> None:
    """PowerShell must not reach a POSIX reader through inline code either.

    The fenced-block check never inspected inline code, so
    ``arcgraph docs migration-notes`` kept recommending ``Get-Command arcgraph``
    — ``command not found`` in zsh. Built-in topics render almost entirely as
    prose bullets with inline code, so for them the fenced check saw nothing.
    """
    offenders = [
        offender
        for name, text in _doc_sources()
        for offender in _powershell_offenders(name, text)
    ]

    assert (
        offenders == []
    ), "PowerShell cmdlets reachable by POSIX readers:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "label,sample",
    [
        ("inline", "Run `ConvertTo-Json payload` to export."),
        ("bash_fence", "```bash\nJoin-Path a b\n```"),
        ("pwsh_fence", "```pwsh\nGet-ChildItem -Path .\n```"),
        ("ps1_fence", "```ps1\nRemove-Item x\n```"),
        (
            "label_does_not_excuse_earlier_span",
            "Run `ConvertTo-Json`; Windows PowerShell alternative: `Get-Command`.",
        ),
        ("pwsh_fence_with_info_string", '```pwsh title="Windows"\nJoin-Path a b\n```'),
        ("uppercase_pwsh_fence", "```PWSH\nExport-Csv out\n```"),
        ("mixed_case_ps1_fence", "```Ps1\nImport-Module x\n```"),
    ],
    ids=lambda value: value if isinstance(value, str) and " " not in value else "",
)
def test_powershell_detector_flags_every_reachable_shape(
    label: str, sample: str
) -> None:
    """Positive coverage for the extractor, not just the regex.

    Every one of these samples passed at some point: the extractor skipped
    ``pwsh``/``ps1`` fences entirely, and a ``Windows PowerShell`` mention
    anywhere on a line exempted the whole line including spans before it.
    """
    assert _powershell_offenders("sample", sample), label


def test_powershell_detector_allows_a_properly_labelled_windows_fallback() -> None:
    """The real documentation pattern must still pass."""
    sample = (
        "Use `command -v arcgraph` (Windows PowerShell: `Get-Command arcgraph`) "
        "to confirm."
    )

    assert _powershell_offenders("sample", sample) == []


def test_powershell_guard_matches_shape_not_a_verb_list() -> None:
    """Pin the guard's own reach, so the docstring above stays true."""
    for cmdlet in (
        "Get-Command",
        "ConvertTo-Json",
        "Export-Csv",
        "Import-Module",
        "Join-Path",
        "Split-Path",
        "New-ItemProperty",
    ):
        assert _POWERSHELL_CMDLET.search(cmdlet), cmdlet
    # Hyphenated prose is why the scan is limited to command spans.
    assert _POWERSHELL_CMDLET.search("Read-Only")
    assert _command_spans("Read-Only mode is the default.") == []


#: The exact opening of the scope bullet. The whole item is derived from the
#: authoritative command set below; no separately maintained count is allowed.
_SCOPE_CLAIM = "This applies to all target-scoped commands:"


def _markdown_series(values: set[str] | frozenset[str]) -> str:
    """Format a deterministic Markdown code-span series."""
    names = [f"`{value}`" for value in sorted(values)]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _expected_scope_item(commands: set[str] | frozenset[str]) -> str:
    """Build the count-free scope contract from the authoritative command set."""
    return (
        f"{_SCOPE_CLAIM} {_markdown_series(commands)}. It also applies to the MCP "
        f"{_markdown_series(TARGET_SCOPED_MCP_TOOLS)} payloads. Index-level views "
        "(`current`, `status`, `stats`, `architecture`, `doctor`, `ci`) and the MCP "
        "`arcgraph_index_status` tool keep the complete table."
    )


def _scope_item_contract_errors(
    item: str, commands: set[str] | frozenset[str]
) -> list[str]:
    """Return an error when prose diverges from the generated scope contract."""
    expected = _expected_scope_item(commands)
    if item == expected:
        return []
    return [
        "scope bullet must be generated from the authoritative command set "
        "without a separately maintained count"
    ]


def _migration_scope_item() -> str:
    """The one migration bullet that enumerates the changed command surface."""
    payload = render_docs("migration-notes", as_json=True)
    section = next(
        section
        for section in payload["sections"]
        if section["title"] == "Bounded Relation Payloads Are Now The Default"
    )
    return next(item for item in section["items"] if "target-scoped commands" in item)


def test_migration_notes_name_every_command_whose_payload_changed() -> None:
    """Assert inside the scope bullet, not against the whole topic.

    A whole-document check passed even after deleting ``callers`` from the
    scope list, because the topic mentions ``callers`` in three other bullets.
    Binding the assertion to the item that makes the claim is the only way the
    claim is actually tested.
    """
    item = _migration_scope_item()
    named = {
        command for command in TARGET_SCOPED_CLI_COMMANDS if f"`{command}`" in item
    }
    named_mcp = {tool for tool in TARGET_SCOPED_MCP_TOOLS if f"`{tool}`" in item}

    assert named == TARGET_SCOPED_CLI_COMMANDS, (
        "scope bullet omits: " f"{sorted(TARGET_SCOPED_CLI_COMMANDS - named)}"
    )
    assert named_mcp == TARGET_SCOPED_MCP_TOOLS, (
        "MCP scope bullet omits: " f"{sorted(TARGET_SCOPED_MCP_TOOLS - named_mcp)}"
    )
    errors = _scope_item_contract_errors(item, TARGET_SCOPED_CLI_COMMANDS)
    assert errors == [], "; ".join(errors)
    assert "arcgraph_index_status" in item


@pytest.mark.parametrize(
    "info_string",
    ["powershell", "PowerShell", "POWERSHELL", 'powershell title="Windows"'],
)
def test_powershell_fences_are_recognised_regardless_of_spelling(
    info_string: str,
) -> None:
    """A fence label is free-form text; classification must normalise it.

    CommonMark allows attributes after the language and imposes no casing, so
    comparing the raw info string against a set let ```` ```PowerShell ```` and
    ```` ```pwsh title="Windows" ```` past both documentation guards at once.
    """
    assert _fence_language(info_string) in _POWERSHELL_FENCES


_MIGRATION_SENTINEL_CONTRACT = (
    "Only the definition-path failure adds a warning:",
    (
        "The no-target and unresolved-target cases stop folding too, but add no "
        "sentinel because nothing failed."
    ),
)
_README_SENTINEL_CONTRACT = (
    "Only that last case adds a `warning_scope_unavailable` warning",
    "the other two are ordinary states already visible in the payload.",
)


def _missing_prose_clauses(text: str, clauses: tuple[str, ...]) -> list[str]:
    """Return required semantic clauses missing from whitespace-normalised prose."""
    normalized = " ".join(text.split())
    return [clause for clause in clauses if clause not in normalized]


def test_migration_notes_document_the_scope_failure_sentinel() -> None:
    """A payload can now say "scope unknown"; consumers must be told what that means.

    ``warning_scope_unavailable`` appeared in payloads while every user-facing
    document still described only two reasons folding stops, so a caller had no
    way to tell an unfiltered payload from a filtered one.
    """
    notes = render_docs("migration-notes")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for text in (notes, readme):
        assert "warning_scope_unavailable" in text
    assert "could not be read" in notes
    assert "unfiltered" in notes and "unfiltered" in readme

    assert _missing_prose_clauses(notes, _MIGRATION_SENTINEL_CONTRACT) == []
    assert _missing_prose_clauses(readme, _README_SENTINEL_CONTRACT) == []


def test_scope_failure_doc_contract_rejects_the_original_false_guarantee() -> None:
    """The exact safety overclaim that prompted the fix must stay impossible."""
    false_guarantee = (
        "All three cases add a `warning_scope_unavailable` warning and report "
        "every warning unfiltered."
    )

    assert _missing_prose_clauses(false_guarantee, _README_SENTINEL_CONTRACT) == list(
        _README_SENTINEL_CONTRACT
    )


@pytest.mark.parametrize(
    "label,document,expected",
    [
        (
            "tilde",
            "~~~PowerShell\nConvertTo-Json $x\n~~~",
            ("powershell", "ConvertTo-Json $x"),
        ),
        (
            "four_backticks",
            "````powershell\nGet-Command x\n````",
            ("powershell", "Get-Command x"),
        ),
        (
            "three_space_indent",
            "   ~~~PowerShell\nConvertTo-Json $x\n   ~~~",
            ("powershell", "ConvertTo-Json $x"),
        ),
        ("five_tildes", "~~~~~pwsh\nExport-Csv out\n~~~~~", ("pwsh", "Export-Csv out")),
        ("longer_close", "```bash\necho hi\n`````", ("bash", "echo hi")),
        (
            "unclosed_to_eof",
            "~~~PowerShell\nConvertTo-Json $x",
            ("powershell", "ConvertTo-Json $x"),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and "\n" not in value else "",
)
def test_fence_parser_handles_commonmark_delimiters_indentation_and_eof(
    label: str, document: str, expected: tuple[str, str]
) -> None:
    """Pin the top-level forms the documentation scanner promises to handle.

    CommonMark allows tildes and runs longer than three. Matching only an exact
    three-backtick prefix made ``~~~PowerShell`` invisible and read
    ```` ````powershell ```` as the language "`powershell", so both bypassed
    every documentation guard at once.
    """
    assert _fenced_blocks(document) == [expected], label


def test_fence_parser_does_not_close_on_a_shorter_or_different_run() -> None:
    """A closing fence must match the opener's character and length."""
    assert _fenced_blocks("````bash\n```\necho hi\n````") == [("bash", "```\necho hi")]
    assert _fenced_blocks("~~~bash\n```\necho hi\n~~~") == [("bash", "```\necho hi")]


def test_backtick_in_info_string_cannot_hide_inline_commands() -> None:
    """An invalid backtick opener is prose, so its following inline code is visible."""
    document = "```text`not-a-fence\n`ConvertTo-Json $x`\n```"

    spans = [span for _, _, span in _command_spans(document)]

    assert "ConvertTo-Json $x" in spans
    assert _powershell_offenders("sample", document)


@pytest.mark.parametrize(
    "replacement,suffix",
    [
        ("all fifteen target-scoped commands", ""),
        ("all of the fifteen target-scoped commands", ""),
        (None, " There are thirty commands."),
        (None, " There are `15` commands."),
    ],
)
def test_scope_contract_rejects_every_separately_maintained_count(
    replacement: str | None, suffix: str
) -> None:
    """Counts fail regardless of wording, position, or Markdown formatting."""
    expected = _expected_scope_item(TARGET_SCOPED_CLI_COMMANDS)
    mutated = (
        expected.replace("all target-scoped commands", replacement)
        if replacement is not None
        else f"{expected}{suffix}"
    )

    assert _scope_item_contract_errors(mutated, TARGET_SCOPED_CLI_COMMANDS)


def test_scope_contract_accepts_the_generated_count_free_item() -> None:
    """The generated item is the sole accepted scope wording."""
    expected = _expected_scope_item(TARGET_SCOPED_CLI_COMMANDS)

    assert _scope_item_contract_errors(expected, TARGET_SCOPED_CLI_COMMANDS) == []
