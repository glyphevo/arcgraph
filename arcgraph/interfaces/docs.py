"""Built-in user-facing ArcGraph reference docs."""

from __future__ import annotations

from typing import Any

DOC_TOPICS = (
    "cli-reference",
    "client-setup",
    "change-preflight",
    "change-safety",
    "agent-cli-contract",
    "mcp-server",
    "source-checkout-smoke",
    "package-readiness",
    "capabilities",
    "visualization",
    "quickstart",
    "security-model",
    "release-checklist",
    "schema-governance",
    "frontend-contract",
    "limitations",
    "evidence-cookbook",
    "troubleshooting",
    "migration-notes",
)


def render_docs(
    topic: str = "cli-reference", *, as_json: bool = False
) -> str | dict[str, Any]:
    normalized = topic.strip().lower()
    if normalized not in DOC_TOPICS:
        raise RuntimeError(
            "Unknown docs topic "
            f"{topic!r}. Available topics: {', '.join(DOC_TOPICS)}."
        )
    payload = _docs_payload(normalized)
    if as_json:
        return payload
    return _docs_markdown(payload)


def _docs_payload(topic: str) -> dict[str, Any]:
    if topic == "client-setup":
        return {
            "topic": topic,
            "title": "ArcGraph Client Setup",
            "sections": [
                {
                    "title": "Prepare One Project",
                    "items": [
                        'Install ArcGraph in a persistent tool environment or venv; MCP clients require the mcp extra. 0.1.0 is a beta release. ArcGraph is published on PyPI: install it with `uv tool install --python 3.11 "arcgraph[mcp]"` or `python -m pip install "arcgraph[mcp]"` (pip installs of 0.1.0 from PyPI were tested on macOS, Windows and Linux with Python 3.11 and 3.12; pin the exact version you tested, for example `arcgraph[mcp]==0.1.0`), or from the GitHub repository with `"arcgraph[mcp] @ git+https://github.com/glyphevo/arcgraph.git"`. A disposable uvx environment is not managed by setup. Git tags and GitHub releases carry the same files as the matching PyPI version; npm and Docker/GHCR are not published.',
                        "From the target project, run `arcgraph setup --client claude|codex|cursor|hermes|pi`, selecting one client. It explicitly builds or refreshes the index, probes the runtime, then writes one client entry.",
                        "Use `--dry-run` for a plan without writes, indexing or subprocesses; `--client-config PATH` overrides the destination, not client discovery; `--timeout SECONDS` sets a positive per-step timeout.",
                        "Global options precede setup: `arcgraph --repo-root /path/to/project --output-dir /path/to/index setup --client codex`. Default output is project/output/arcgraph. Indexing, MCP and CLI guidance share the same absolute paths. Source scope follows ordinary project configuration and detection; inspect it with doctor.",
                    ],
                },
                {
                    "title": "Client Destinations",
                    "items": [
                        "Claude Code: project/.mcp.json, stdio MCP; approve project MCP/trust in the client when requested.",
                        "Codex: project/.codex/config.toml, stdio MCP; the client must trust the project to load its configuration. Setup does not write trust settings.",
                        "Cursor: project/.cursor/mcp.json, stdio MCP; open that project and enable the server if the client initially disables it.",
                        "Hermes Agent: HERMES_HOME/config.yaml, otherwise ~/.hermes/config.yaml, stdio MCP. Each hashed server name remains bound to one project even when cwd changes and is listed by every session of that profile; remove it with `hermes mcp remove NAME`, check it without a model with `hermes mcp test NAME`, and use isolated profiles where needed. Hermes may add its own default keys when it rewrites the file.",
                        "Pi: project/.pi/skills/arcgraph/SKILL.md, CLI skill rather than native MCP; reload project resources and use /skill:arcgraph. An override must name the skill file.",
                    ],
                },
                {
                    "title": "Preservation And Failure",
                    "items": [
                        "Existing files receive a byte-for-byte backup before editing. Unrelated configuration values are retained. JSON is strict (not JSONC); YAML is safely parsed, but rewriting may remove comments. TOML appends a table; unsupported layouts and duplicate keys are refused.",
                        "Conflicting same-name entries, different existing Pi skills and symlinked destinations/ancestors are refused; there is no force option. Identical setup leaves configuration unchanged. Installation moves or upgrades can require manual entry review.",
                        "Detected concurrent config changes are refused, but this is not an adversarial filesystem sandbox. Use canonical paths. Machine-specific config/skill paths may appear in git status; setup never stages them.",
                        "Blocked returns nonzero. Indexing and config are not one transaction: a completed index may remain after a probe or config failure. An incompatible/corrupt index may require an explicit build; a different repository requires a separate output directory.",
                    ],
                },
                {
                    "title": "Verified Hosts",
                    "items": [
                        "Three layers are recorded separately: setup's own protocol probe, client discovery, and model use. Each host was exercised once on one macOS machine in a throwaway two-file Python project with the same prompt (arcgraph_help, arcgraph_index_status quoting index_version and commit_sha, arcgraph_explain with callers).",
                        "Observed: Claude Code CLI 2.1.276 (project entry discovered, approved, connected, three tool calls); Cursor 3.23.12 (connected once the project was open, three tool calls); Codex desktop with core 0.159.2 (project configuration loaded after the project was trusted in Codex, which recorded the trust entry itself; three tool calls); Hermes Agent CLI 0.21.5 (hermes mcp test connected and found 14 tools without a model) and a desktop session (three tool calls); Pi 0.85.1 (skill loaded, three CLI commands) with one model, two other providers having failed for reasons unrelated to ArcGraph. App versions of the Codex and Hermes desktop apps were not recorded.",
                        "Limits: macOS only and one session per host. The CI package matrix exercises MCP client handshakes on Ubuntu, Windows and macOS but no real host application ran on Windows or Linux. Larger or multi-project setups, other client versions and the exact wording of each host's approval or enable prompts were not recorded. Claude Code writes the approval to .claude/settings.local.json; Codex records project trust in its own configuration; Cursor may start with the server disabled in other versions.",
                        "arcgraph_index_status redacts host paths; identify an index by commit_sha, index_version and repo_id. Any other client that can start a stdio MCP server can use the same entry (command: the installed arcgraph executable; args: mcp serve --repo-root PROJECT --output-dir INDEX), but only the five clients above have setup adapters and other clients were not verified.",
                    ],
                },
                {
                    "title": "Verify In The Client",
                    "items": [
                        "planned is only a dry-run plan. prepared means the index and independent protocol probe passed and configuration matched or was written; Pi checks CLI help/current instead.",
                        "client_connection_verified and model_call_verified remain false. Registration and a protocol probe do not prove host approval or model-visible tools. Open/reconnect the client and request arcgraph_help, arcgraph_index_status and one targeted arcgraph_explain; Pi uses the skill and equivalent CLI commands.",
                        "Setup never changes credentials, client trust, telemetry or feedback settings, starts a model session, installs a client, or publishes a package. Legacy trial setup remains preview-first and does not edit Claude configuration.",
                        "MCP reads never refresh indexes. After edits, use authorized CLI sync --if-stale with the exact prefix returned by setup. Empty/unresolved or truncated graph answers do not prove absence; confirm with source and tests. CLI shares MCP analysis limitations.",
                    ],
                },
            ],
        }
    if topic == "change-preflight":
        return {
            "topic": topic,
            "title": "ArcGraph Change Preflight",
            "sections": [
                {
                    "title": "Default Read-Only Workflow",
                    "items": [
                        "Call MCP `arcgraph_get_risk` before editing a symbol, file, or route. It returns deterministic target resolution, blast radius, related tests, similar siblings, entrypoints, unknowns, assurance, and recommended reads in one bounded response.",
                        "Set `verify_references=true` for target-scoped SCIP or TypeScript Language Service confirmation when the change is high risk. Exact references do not prove reflection, runtime plugin, or cross-repository callers.",
                        "For ordinary symbol/path targets, precedence is stable id, exact qualified name, a normalized-id retry (bare qualname missing its stable-id kind prefix), exact path, then a unique bare name or qualified-name suffix. Ambiguity returns candidates and resolves nothing. Route-shaped and entrypoint/worker-flow targets are matched separately before or instead of this chain.",
                    ],
                },
                {
                    "title": "Focused Follow-Up",
                    "items": [
                        "Use `arcgraph impact TARGET`, `arcgraph tests TARGET`, `arcgraph references TARGET`, `arcgraph similar TARGET`, and `arcgraph route METHOD PATH` for deeper evidence.",
                        "Similarity reports pattern families, low-information members, and whether only one family member changed. Route reports static mount context, ordered middleware roles, heuristic authorization-name hints, and dynamic registration gaps.",
                    ],
                },
                {
                    "title": "Explicit Index Lifecycle",
                    "items": [
                        "Queries and MCP readers never rebuild. Run `arcgraph sync --if-stale` for one atomic incremental publication or `arcgraph watch` for explicit debounced synchronization.",
                        "A failed sync preserves the prior `current.json`; stale payloads return a machine-readable `recovery_action`.",
                        "Index status reports build identity and read-only storage lifecycle data. The suggested prune command is a dry run; deletion still requires explicit `--apply`.",
                    ],
                },
            ],
        }
    if topic == "change-safety":
        return {
            "topic": topic,
            "title": "ArcGraph Surgical Change Safety",
            "sections": [
                {
                    "title": "Local Contract Workflow",
                    "items": [
                        "Surgical Change Safety is a local, fail-closed workflow under `arcgraph change`; it does not edit code, run user-provided commands, push Git state, or change remotes.",
                        "Start from a clean, indexed Git checkout, then use `arcgraph change --repo-id REPO plan --task TEXT --target symbol:ID` (or a path, route, resource, or contract target). The plan command captures a baseline, creates a durable pin, and returns a versioned JSON plan view.",
                        "Approve an exact revision with `arcgraph change --repo-id REPO approve --plan-id ID --revision N --plan-content-digest DIGEST --actor ACTOR --reason TEXT`; the digest is required so a decision cannot silently bind another revision.",
                        "After code changes are independently made and the index is refreshed, use `arcgraph change ... evidence add` to record already-produced evidence, then `arcgraph change ... verify` to persist the verification result. The CLI never executes a test command supplied in evidence metadata.",
                        "Use `reject` or `abandon` to stop a revision without releasing its pin. Only `archive` of a verified, failed, or abandoned revision can safely release the pin after the archive record is durable.",
                    ],
                },
                {
                    "title": "Evidence, Retention, And Audit",
                    "items": [
                        "`evidence add` requires the exact plan id, revision, content digest, verification requirement, result, attestation level, producer identity, and artifact digest. ArcGraph re-captures current code/build identity and redacts sensitive metadata before publication.",
                        "`evidence list` and `evidence show` return summary projections, not controlled material. `evidence purge` can only remove or replace controlled material; immutable evidence records, purge events, and audit events remain retained.",
                        "`arcgraph change ... audit export --export-id ID` writes a non-authoritative, default-redacted export only below `output/arcgraph/change-safety/exports/`. It does not change the plan, pointer, pin, or verdict.",
                        "Trusted-runner evidence requires an operator-controlled registry outside the analyzed repository. The default has no trusted runners, and an unavailable, corrupt, or mismatched registry fails closed.",
                    ],
                },
                {
                    "title": "Machine Results And MCP",
                    "items": [
                        "Change commands emit versioned JSON with `schema_version`, `change_contract_version`, `repo_id`, `status`, and an applicable `verdict` or `error_code`. Exit 0 means a non-blocking result, exit 1 an input/store/security/runtime error, and exit 2 a valid blocked planning or verification verdict.",
                        "`arcgraph mcp serve` exposes only read/preview/compute change tools: `arcgraph_preview_change_plan`, `arcgraph_get_change_plan`, `arcgraph_list_change_plans`, `arcgraph_get_graph_delta`, and `arcgraph_verify_change`.",
                        "MCP verify computes without persisting a report or lifecycle transition. MCP does not expose approval, reject, abandon, archive, evidence persistence/purge, audit export, test execution, code editing, or Git/remote writes.",
                    ],
                },
            ],
        }
    if topic == "cli-reference":
        return {
            "topic": topic,
            "title": "ArcGraph CLI Reference",
            "sections": [
                {
                    "title": "Indexing",
                    "items": [
                        "`arcgraph setup --client claude|codex|cursor|hermes|pi` explicitly builds/refreshes the selected index, probes the local runtime, and writes one client entry. `--dry-run` previews without writes or subprocesses; `--client-config PATH` selects a config destination, not automatic client discovery. Use a persistent installation with the mcp extra for MCP clients. Global repo/output options precede setup. Conflicting entries are refused and existing config files backed up. Claude/Codex/Cursor use project configs, Hermes uses its selected profile, and Pi gets a project CLI skill. Prepared does not mean client approval, connection or model invocation; complete those checks in the host. Legacy trial setup keeps its preview-first behavior.",
                        "`arcgraph init` detects source roots and adds a `[tool.arcgraph]` section to an existing `pyproject.toml`; use `--dry-run` to preview the config.",
                        "`arcgraph doctor` runs environment and index health checks (project config, source roots, index freshness, precision tools, Python version).",
                        "`arcgraph build [--root PATH] [--coverage PATH] [--runtime-trace PATH] [--scip-index PATH] [--pyright-export PATH] [--scip-graph-index PATH]` builds a fresh schema 1.0 index.",
                        "Global `arcgraph --version` reports the installed ArcGraph package version and exits without requiring a subcommand.",
                        "`arcgraph version --json` reports the exact running source-checkout or installed-wheel identity; compare an installed `artifact_provenance.sha256` with the release manifest instead of treating the product version as a unique candidate id.",
                        "Pass global `--human` before a subcommand, for example `arcgraph --human build`, to print a readable summary instead of JSON.",
                        "`--semantic-call-resolution` is retained for older scripts as a compatibility no-op; use `--legacy-call-resolution` only as an explicit escape hatch.",
                        "`arcgraph build --scip-index PATH --pyright-export PATH` imports SCIP and Pyright precision evidence generated before the build.",
                        "`arcgraph build --scip-graph-index PATH` ingests an existing `scip print --json` payload as a language-neutral protocol graph fragment. This is distinct from `--scip-index` precision evidence.",
                        "Source roots are auto-detected from project configuration (`pyproject.toml`, `setup.cfg`, or package structure). Use `--root PATH` to override, or `--legacy-roots` for the legacy source-root layout.",
                        '`[tool.arcgraph] exclude = ["**/generated/**", "legacy_app/**"]` adds fnmatch patterns to skip files during scanning. Patterns are merged with built-in ignore rules (e.g. `__pycache__`, `.venv`).',
                        "`arcgraph reindex --changed` refreshes changed files against the current index; `arcgraph sync --if-stale` is the normal explicit stale-aware lifecycle command and `arcgraph watch` debounces repeated syncs.",
                        "`arcgraph current` or `arcgraph status` returns schema, freshness, capabilities, and precision/runtime status.",
                        "`arcgraph help [--topic TOPIC] [--tool MCP_TOOL] [--surface cli|mcp|all]` returns bounded structured Agent guidance for discovery, tool selection, result interpretation, recovery, and optional local trial feedback; `arcgraph --help` remains syntax help.",
                        "`arcgraph feedback record --feedback-log ABSOLUTE_PATH ...` appends one strict privacy-bounded trial event; `arcgraph feedback summarize ABSOLUTE_PATH` returns aggregates without raw events, ids, timestamps, or local paths.",
                        "`arcgraph trial setup --client claude --dry-run` checks local prerequisites and returns a complete registration command without changing Claude configuration; `--apply-local-files` creates the local state paths and a Git exclude entry. POSIX private modes are enforced where available; Windows reports review because setup does not set ACLs or claim reparse-point protection.",
                        "`arcgraph docs [TOPIC] [--json]` renders any built-in reference topic; run `arcgraph docs` with no topic for this CLI reference.",
                        "`arcgraph workspace status --config arcgraph.workspace.toml` reads an explicit multi-repo workspace manifest and summarizes each repo's current index, freshness, capabilities, CI summary, warnings, catalog, and shallow dependency hints without building or merging indexes.",
                        "`arcgraph workspace status --no-dependency-hints` keeps the workspace catalog but omits shallow cross-repo dependency candidates. Catalog and hints are advisory; they are not cross-repo impact analysis and do not create graph edges.",
                        "`arcgraph workspace resolve TARGET --config arcgraph.workspace.toml` routes a repo id, project/package/module/import/path/dependency target to candidate repositories so an agent can then run single-repo `symbol`, `context`, or `explain` commands in the chosen repo.",
                        "`arcgraph workspace resolve` is an advisory routing layer over the workspace catalog and dependency hints; it does not perform cross-repo symbol identity, impact analysis, or graph edge creation.",
                        "`arcgraph workspace context TARGET --config arcgraph.workspace.toml` runs workspace resolve, then returns summary/standard single-repo `context` payloads for the matched repositories. It is an agent convenience pack, not cross-repo impact analysis.",
                        "`arcgraph build` and successful `arcgraph reindex --changed` runs automatically prune stale build history after publishing `current.json`.",
                        "`arcgraph ops prune --keep-builds 3` dry-runs safe cleanup of stale or incomplete build directories; add `--apply` to delete them.",
                        "`arcgraph ops prune --include-input-artifacts --apply` removes generated root-level precision, coverage, and runtime input artifacts, but never deletes `output/arcgraph/evidence/manifest.json`.",
                    ],
                },
                {
                    "title": "Local Install And Upgrade",
                    "items": [
                        "ArcGraph is on PyPI and can also be installed from a source checkout; 0.1.0 is a beta release. Git tags (`v` followed by the version) and GitHub releases carry the same two files as the matching version on PyPI. npm and Docker/GHCR package paths are not published.",
                        "ArcGraph requires Python 3.11 or 3.12 (`requires-python >=3.11,<3.13`). Create and activate a virtual environment before installing when you want an isolated local tool.",
                        "From a source checkout, run `python -m pip install --upgrade -e .` in the ArcGraph project directory to install or refresh the `arcgraph` CLI.",
                        'For development and local validation, run `python -m pip install -e ".[dev]"`; use the analyzed project\'s locked npm install when TypeScript/JavaScript analysis is required.',
                        "Node.js and a resolvable TypeScript compiler API are runtime dependencies for TypeScript/JavaScript analysis, not merely test dependencies. The Python wheel includes the `.mjs` extractor but not `node_modules/typescript`.",
                        "The external-trial acceptance scope is Python analysis through the installed CLI plus local stdio MCP. Its default analysis/change/help surface is read-only; optional local feedback is a separate disclosed append. TS/JS is outside that guarantee.",
                        "If `arcgraph` is not on PATH, run `python scripts/arcgraph.py <command>` from the source checkout.",
                        "If an older wheel shadows the checkout, run `python -m pip uninstall arcgraph` and then reinstall with `python -m pip install -e .`.",
                        "After upgrading across schema or resolver changes, run `arcgraph build --coverage output/arcgraph/coverage.xml` and then `arcgraph ci`.",
                    ],
                },
                {
                    "title": "Precision Inputs",
                    "items": [
                        "`arcgraph precision scip-python --output output/arcgraph/scip-index.json --index-file output/arcgraph/index.scip --project-name NAME` runs scip-python, then converts the generated binary SCIP index to JSON.",
                        "`arcgraph precision scip-json --input index.scip --output output/arcgraph/scip-index.json` converts an existing binary SCIP index with `scip print --json`.",
                        "`arcgraph precision pyright --output output/arcgraph/pyright-export.json --python-version 3.11` generates the Pyright type evidence contract using `pyright-langserver` LSP `didOpen`, `typeDefinition`, `definition`, and `hover` requests.",
                        "`arcgraph evidence status` compares the live manifest with the current index snapshot and summarizes evidence-related capabilities; it does not suggest actions.",
                        "`arcgraph evidence plan [--profile default|python_full]` reads the live evidence manifest, reports missing/stale/partial/invalid/out-of-scope artifacts, and suggests generation commands without generating evidence.",
                        "`arcgraph evidence stamp --kind coverage --path output/arcgraph/coverage.xml --tool-name coverage` writes an ArcGraph sidecar with commit/source_roots metadata for externally generated evidence artifacts.",
                        "`evidence status` is for live-vs-snapshot accounting, `evidence plan` is for next actions, `current` is the machine-readable snapshot, and `doctor` is the health check entrypoint.",
                        "`doctor`, `evidence status`, and `evidence plan` read the live manifest at `output/arcgraph/evidence/manifest.json`; `current`, `ci`, and release gates read the build snapshot stored in index metadata.",
                        "Pyright export metrics include total probes, requestable probes, skipped unmappable probes, request count, resolved type info, and unresolved probes.",
                        "The Windows precision-tools install script pins `pyright@1.1.409`; local full evidence profiles should use the same version unless intentionally testing compatibility. GitHub Actions does not currently install or run Pyright.",
                        "Pass the resulting JSON to `arcgraph build --scip-index output/arcgraph/scip-index.json --pyright-export output/arcgraph/pyright-export.json`; missing or invalid precision input remains explicit as `precision=ast_fallback_only` or `precision_partial`.",
                        "On Windows, run `scripts/install-arcgraph-precision-tools.ps1` to install `@sourcegraph/scip-python@0.6.6`, `pyright@1.1.409`, and build `scip.exe v0.7.1` with Go. WSL remains a fallback (use the Linux install steps there); GitHub Actions is not currently a working fallback for this, since it neither installs these tools nor runs at all while disabled.",
                    ],
                },
                {
                    "title": "Semantic Queries",
                    "items": [
                        "`arcgraph stats` reports file, node, and edge counts for the current index; `arcgraph semantic-stats` records callsite, binding, and TypeRef metrics.",
                        "For ordinary symbol/path targets, commands resolve stable id, exact qualified name, a normalized-id retry (bare qualname missing its stable-id kind prefix), exact path, then a unique bare name or qualified-name suffix; ambiguous inputs return candidates instead of selecting the first match. Route-shaped and entrypoint/worker-flow targets are matched separately before or instead of this chain.",
                        "`arcgraph symbol QUALNAME` resolves one symbol, and `arcgraph similar TARGET [--max-results N] [--detail-level summary|standard|detailed]` reports similar implementations plus pattern-family and low-information signals.",
                        "`arcgraph imports MODULE` lists the import edges recorded for a module.",
                        "`arcgraph route METHOD PATH` traces a route's static mount and ordered middleware/handler chain; `arcgraph references TARGET` performs target-scoped SCIP or TypeScript Language Service reference verification; `arcgraph worker TARGET` traces an ARQ worker task or queue.",
                        "`arcgraph tests TARGET` recommends related tests and reports coverage gaps.",
                        "`arcgraph architecture` returns a basic architecture report over the current index.",
                        "`arcgraph unresolved [target|path] [--category CATEGORY] [--release-blocking-only]` shows unresolved callsites with category, risk level, python_full blocking status, and suggested next steps.",
                        "`arcgraph bindings TARGET`, `arcgraph types TARGET`, and `arcgraph callsites TARGET` expose V2 debug facts.",
                        '`arcgraph context TARGET... --task "review change" --detail-level summary|standard|detailed` returns the canonical agent context payload; source snippets stay disabled unless `--include-source` is passed.',
                        "`arcgraph context --max-results N` is an upper bound; `--detail-level` also applies an agent payload density limit: `summary` caps at 6, `standard` caps at 8, and `detailed` uses `--max-results` directly with no separate density cap (CLI defaults `--max-results` to 30; MCP callers may raise it up to the server's own ceiling of 100). The applied limit is reported as `context_limit` in `truncation`.",
                        '`arcgraph explain TARGET... --task "review change" --detail-level summary|standard|detailed` returns compact target explanations with direct edge evidence, resolution strategy/fallbacks, and confidence sources; use `context` for a broader task context package.',
                        "`arcgraph callers TARGET [--detail-level summary|standard|detailed]` returns agent-safe incoming edges with capped evidence, resolution, and confidence sources. This bounded payload is the default; `--raw` returns the unbounded debug payload and rejects `--max-results`, `--detail-level`, and `--include-source`.",
                        "`arcgraph callees TARGET [--detail-level summary|standard|detailed]` returns agent-safe outgoing edges with capped evidence, resolution, and confidence sources. This bounded payload is the default; `--raw` returns the unbounded debug payload.",
                        "`arcgraph impact TARGET [--profile review_default]` returns compact call/import/entrypoint/resource impact sections with provenance edges. This bounded payload is the default; `impact --raw` remains the compatibility/debug view and is not size-bounded.",
                        "`explain` is the single-target resolution and provenance package; `callers`/`callees` are directional traversal views; `impact` is the blast-radius provenance view. `--compact` is still accepted on all three and is now a no-op.",
                    ],
                },
                {
                    "title": "MCP Tool Facade",
                    "items": [
                        "`arcgraph mcp serve --repo-root . --output-dir output/arcgraph` runs the local stdio MCP server. Its default analysis/change/help surface is read-only, and this path is included in the external-trial scope; the optional MCP runtime extra is required only when serving.",
                        "`python -m arcgraph.interfaces.mcp_server --repo-root . --output-dir output/arcgraph` exposes the same server entrypoint for source-checkout environments.",
                        "`arcgraph_get_context` returns the compact task context package for editing and review agents.",
                        "`arcgraph_get_risk` is the default Change Preflight. It returns resolution, impact, tests, similar siblings, entrypoints, unknowns, assurance, next reads, and optional exact references without updating the index.",
                        "`arcgraph_explain` returns compact target explanations with edge evidence, resolution strategy/fallbacks, and confidence sources.",
                        "`arcgraph_get_why` returns structural reasons plus optional external memory context; use it for historical decisions and lessons, not as a replacement for `arcgraph_explain` provenance.",
                        "`arcgraph_record_learning` is proposal-only and does not persist external memory by default.",
                        "MCP source snippets are disabled by default, allowed roots default to the resolved repo root, and server startup does not build indexes or edit agent configuration.",
                        "The MCP server requires MCP Python SDK `>=2.0.0,<3.0.0` and uses the public `MCPServer` API. ArcGraph installs no telemetry exporter and configures no automatic or remote telemetry; an operator-configured global OpenTelemetry provider remains host behavior.",
                        "`arcgraph mcp serve --metrics-log PATH` explicitly enables local per-tool JSONL metrics. It is off by default and records only tool name, status, duration, payload size/token estimates, truncation, and enum-only freshness status; it never records arguments, repo ids, paths, source, returned payload text, raw exceptions, prompts, or client identity.",
                        "The external-trial scope includes the installed-wheel stdio server. Explicit `arcgraph setup --client` prepares client configuration; HTTP/network MCP transport remains deferred.",
                    ],
                },
                {
                    "title": "Surgical Change Safety",
                    "items": [
                        "Every command takes a required global `arcgraph change --repo-id REPO ...` before the subcommand; there is no implicit default repository identity.",
                        "`arcgraph change --repo-id REPO plan --task TEXT [--target KIND:VALUE] [--target-policy must_resolve|allow_manual_review|informational] [--intent-id ID] [--plan-id ID] [--revision N] [--pin-id ID] [--acceptance-criterion TEXT] [--constraint TEXT] [--allow-surface-change SURFACE] [--forbid-surface-change SURFACE] [--protect SURFACE] [--openapi-input PATH]` captures a baseline, creates a durable pin, and activates a versioned plan. It is not a preview: it writes state.",
                        "`arcgraph change --repo-id REPO show --plan-id ID` reads one current plan view, and `arcgraph change --repo-id REPO list` lists them, neither mutating state.",
                        "`arcgraph change --repo-id REPO approve --plan-id ID --revision N --plan-content-digest DIGEST --actor ACTOR --reason TEXT` approves one exact revision; the digest is required so a decision cannot silently bind another revision.",
                        "`arcgraph change --repo-id REPO reject --plan-id ID --revision N --plan-content-digest DIGEST --actor ACTOR --reason TEXT` and `arcgraph change --repo-id REPO abandon --plan-id ID --revision N --plan-content-digest DIGEST --actor ACTOR --reason TEXT` stop a revision without releasing its pin.",
                        "`arcgraph change --repo-id REPO archive --plan-id ID --revision N --plan-content-digest DIGEST --actor ACTOR --reason TEXT` archives a verified, failed, or abandoned revision and is the only path that safely releases its pin.",
                        "`arcgraph change --repo-id REPO diff --plan-id ID --revision N --plan-content-digest DIGEST` computes the current graph delta for one exact revision.",
                        "`arcgraph change --repo-id REPO evidence add --plan-id ID --revision N --plan-content-digest DIGEST --requirement-id ID --result pass|fail|unknown|not_run --attestation-level self_reported|artifact_backed|trusted_runner --producer-identity NAME --artifact-digest DIGEST [--evidence-id ID] [--stdout-digest DIGEST] [--stderr-digest DIGEST] [--runner-metadata-json JSON] [--material-json JSON]` records already-produced evidence. ArcGraph never executes the command named in the evidence.",
                        "For `artifact_backed` and `trusted_runner`, `--artifact-digest` must equal the canonical digest of `--material-json`, which is the SHA-256 of sorted-key, whitespace-free JSON rather than the file bytes.",
                        "`trusted_runner` evidence additionally needs `--runner-metadata-json` carrying a non-empty `command` and an operator registry supplied as `arcgraph change --repo-id REPO --trusted-runner-registry PATH ...`. The registry must resolve outside the analyzed repository, and the default of no registry fails closed.",
                        "`arcgraph change --repo-id REPO evidence list --plan-id ID` and `arcgraph change --repo-id REPO evidence show --plan-id ID --evidence-id ID` return summary projections rather than controlled material.",
                        "`arcgraph change --repo-id REPO evidence purge --plan-id ID --revision N --evidence-id ID --actor ACTOR --reason TEXT --purge-mode remove_material|replace_with_redacted` removes or replaces controlled material only; immutable evidence records, purge events, and audit events are retained.",
                        "`arcgraph change --repo-id REPO verify --plan-id ID --revision N --plan-content-digest DIGEST` persists verification and closes the revision. Run it once, after every requirement has evidence: a verify attempted earlier still closes the revision, and the following attempt returns `CHANGE_PLAN_STATE_INVALID`.",
                        "Complete evidence does not imply `SAFE_TO_PROCEED`. Evidence clears the lowest-precedence blockers; out-of-scope changes, protected surfaces, a stale index, and incomplete graph deltas each keep their own blocking verdict, and non-blocking findings return `SAFE_WITH_KNOWN_RISKS`.",
                        "`arcgraph change --repo-id REPO report show --plan-id ID --verification-id ID` reads an immutable verification report with current evidence availability.",
                        "`arcgraph change --repo-id REPO audit export --export-id ID` writes a default-redacted export below `output/arcgraph/change-safety/exports/`; redaction is reported as `redaction_assurance=best_effort_pattern_based`.",
                        "`arcgraph change --repo-id REPO reconcile-pins` reports pin/store divergence without auto-releasing anything.",
                        "Change commands emit versioned JSON. Exit 0 is a non-blocking result, exit 1 an input/store/security/runtime error carrying `error_code`, and exit 2 a valid blocked planning or verification verdict carrying `verdict`.",
                        "See `arcgraph docs change-safety` for the contract and `docs/change-safety.md` for a worked end-to-end run.",
                    ],
                },
                {
                    "title": "Reports And Gates",
                    "items": [
                        "`arcgraph ci` runs compatibility, stale, adapter, precision, runtime, performance, release, architecture, and semantic checks.",
                        "CI profiles should generate SCIP, Pyright, coverage, and runtime artifacts before indexing, then run one schema 1.0 default-V2 build.",
                        "`arcgraph ci --python-full` fails unless SCIP/Pyright precision, coverage, runtime trace, evidence commit consistency, type precision, resolver mode, quality targets, and release-blocking unresolved classification gates pass.",
                        "After committing, regenerate coverage, SCIP/Pyright, runtime trace, and the index before relying on local `ci --python-full`. Remote CI on GitHub Actions is the canonical fresh-HEAD evidence path for a pushed commit; locally regenerating and recording these artifacts is how to produce fresh-HEAD evidence before pushing -- running the commands above does not by itself constitute evidence until their output is actually captured.",
                        "`arcgraph report pr --output output/arcgraph/reports/pr-impact.md` writes the PR impact summary.",
                        "`arcgraph report html TARGET --output output/arcgraph/reports/report.html` writes the static VisualSlice HTML report.",
                        "`arcgraph visual force --output output/arcgraph/reports/force-graph.json` exports a production-filtered semantic force graph JSON payload.",
                        "`arcgraph visual workbench --output-dir output/arcgraph/reports/workbench [--open]` writes a local ArcGraph Explorer workbench with index.html, graph_data.json, and status_data.json.",
                        "`arcgraph visual serve --host 127.0.0.1 --port 8765 [--open]` starts a blocking loopback-only read-only workbench server with on-demand search, focus, node detail, and audit APIs.",
                        "`arcgraph visual smoke --output-dir output/arcgraph/reports/visual-smoke` runs an optional maintainer browser smoke against the local workbench and writes screenshots plus result.json when Playwright CLI is available.",
                        "`arcgraph report unresolved --output output/arcgraph/reports/unresolved-classification.md --json-output output/arcgraph/reports/unresolved-classification.json` writes Markdown and machine-readable unresolved classification reports.",
                        "`arcgraph metrics PATH` summarizes CLI P50/P95 budgets for current/status, query, reindex, report, and context commands. Add `--trial-summary` for a path- and timestamp-free aggregate of MCP counts, latency, payload size, estimated tokens, truncation, and freshness.",
                        "`arcgraph report metrics-html PATH --output REPORT.html` writes a local visual aggregate that omits the input-log path, first/last event timestamps, raw events, raw errors, and warning text while retaining a warning count. Review aggregate command and semantic statistics before sharing it.",
                        "`arcgraph benchmark agent-startup --iterations 5 --warmups 1` measures CLI-per-call cold-start latency for `current`, `context`, and `explain`; use it to decide whether a future turnkey MCP server is justified.",
                        "`arcgraph benchmark suite --iterations 5 --warmups 1 [--include-build]` writes a local platform quality snapshot with agent-startup timings, query probes, semantic quality, and an MCP serve recommendation.",
                        "`output/arcgraph/*` artifacts are generated evidence/index files and should not be committed.",
                    ],
                },
                {
                    "title": "Current Public Capability Boundaries",
                    "items": [
                        "Current public-safe language tier wording lives in `docs/language-support.md`.",
                        "Python is the guaranteed native L3 frontend for the external trial. TypeScript/JavaScript is L3 when a TypeScript compiler API is resolvable, but is outside that trial's acceptance scope.",
                        "Next.js and Vue are framework semantics layered over TypeScript/JavaScript.",
                        "Go, C#, Java, Rust, C, C++, and Swift are L3 only through validated external semantic extractor payloads; ArcGraph does not ship default live compiler-backed extractors for those languages.",
                        "SCIP protocol graph input is explicit L2 protocol evidence through `arcgraph build --scip-graph-index PATH`.",
                        "OpenAPI input is explicit L2 protocol evidence through `arcgraph build --openapi-spec PATH`.",
                        "No language is claimed as L4, and public visibility, package publishing, tags, and GitHub Releases require separate human approval.",
                    ],
                },
                {
                    "title": "Runtime Trace",
                    "items": [
                        "`arcgraph trace run --output TRACE -- TESTS...` records optional pytest runtime evidence with event and wall-time limits.",
                        "`arcgraph trace import TRACE --max-events N --max-file-bytes BYTES --max-seconds SECONDS` imports runtime-only facts with hard import limits.",
                        "`arcgraph trace import-otel SPANS --max-spans N --max-file-bytes BYTES --max-seconds SECONDS` imports explicit offline OpenTelemetry span JSON/JSONL when spans carry mappable ArcGraph source/target attributes.",
                        "`arcgraph trace import-har HAR --max-entries N --max-file-bytes BYTES --max-seconds SECONDS` imports explicit offline HAR 1.2 browser network observations when each request uniquely matches an indexed route.",
                        "Minimal flow: run `arcgraph trace run --output output/arcgraph/runtime-trace.json -- <pytest target>`, then run `arcgraph trace import output/arcgraph/runtime-trace.json` or pass `--runtime-trace output/arcgraph/runtime-trace.json` to `arcgraph ci`.",
                        "`arcgraph ci --python-full` treats runtime trace as an explicit evidence input; use `evidence status` and `evidence plan --profile python_full` to audit whether the trace is available, stale, or out of scope.",
                        "Runtime trace facts are additive `runtime-only` evidence. They do not overwrite static confidence, do not prove full runtime coverage, and do not become an ArcGraph runtime dependency.",
                        "The current baseline covers pytest trace run/import, a FastAPI/TestClient request smoke, offline OpenTelemetry span import, and offline HAR network import. Specific route/helper edges inside TestClient worker threads are best-effort because Python profiling hooks depend on thread creation timing. Playwright automation and browser coverage importers are later evidence tracks.",
                    ],
                },
            ],
        }
    if topic == "agent-cli-contract":
        return {
            "topic": topic,
            "title": "ArcGraph Agent CLI Subprocess Contract",
            "sections": [
                {
                    "title": "Subprocess Boundary",
                    "items": [
                        "Agents should call ArcGraph as a local subprocess from the repository root or pass global `--repo-root PATH` before the subcommand.",
                        "Dictionary payloads print JSON to stdout by default. Global `--human` must appear before the subcommand and is for humans, not JSON-parsing agents.",
                        "In the default JSON output mode, stdout carries a JSON payload on success (exit 0) and also when a command handler raises an unhandled runtime/file/schema exception (exit 1 or 2): that case writes a machine-readable error envelope to stdout in addition to the human-readable stderr line, so do not gate JSON parsing on exit code zero for it. Two other nonzero-exit cases behave differently and are not this same envelope: for any non-`change` command, an argument-parsing failure (an unrecognized flag or invalid subcommand) exits 2 with empty stdout and only an argparse usage message on stderr, before any command handler runs; and a command whose handler completes normally but reports a nonzero status through its own payload (for example `arcgraph help --tool UNKNOWN_TOOL`, or `ci --fail-on-warnings`) still prints that command's normal payload to stdout in whatever mode is active, JSON or `--human` alike -- it is not empty and not this error envelope either.",
                        "`arcgraph change` has its own, different argument-parsing contract: an argument-parsing failure under `change` (for example a missing `--repo-id`) exits 1, not 2, and always prints a versioned `change_contract_version`/`schema_version` JSON error envelope with `error_code: CHANGE_CLI_INPUT_INVALID` to stdout, with empty stderr -- even under `--human`, which does not suppress this one. Non-`change` commands do not follow this contract.",
                        "`--human` and `--raw` do suppress the error envelope specifically for the unhandled-exception case above: there, stdout is left empty and only the exit code and the stderr line are informative. They do not suppress a command's own normal nonzero-status payload.",
                        "Treat nonzero exit codes as failures regardless of whether stdout parsed. Handled runtime, file, and schema errors currently print `ArcGraph: ...` to stderr and commonly return exit code 2; an internal defect returns exit code 1 and also prints a traceback to stderr.",
                        "`arcgraph ci` can return nonzero through its payload status, including when `--fail-on-warnings` turns warnings into a failing gate.",
                        "Caller code should capture stdout and stderr separately, use a subprocess timeout, and report JSON parse failures as incompatible or unexpected CLI output.",
                    ],
                },
                {
                    "title": "Payload Checks",
                    "items": [
                        "Whole-index and raw QueryEngine payloads use index schema `1.0.0`. Bounded target-scoped payloads use read schema `1.4.0` and report the underlying storage contract separately as `index_schema_version`.",
                        "Check `schema_version` before relying on payload fields; an unsupported read schema requires a client upgrade, not an index rebuild.",
                        "Check `status`, `freshness`, `warnings`, and `truncation` before using returned context.",
                        'Since read schema `1.1.0`, `warnings` is a heterogeneous JSON array: each element may be a string or a structured object. Do not pass it directly to string-only operations such as `"\\n".join(warnings)`; branch on the element type and read an object\'s `message`, `kind`, and optional `path` fields.',
                        "Use `--detail-level summary|standard|detailed` and `--max-results N` deliberately to control payload size.",
                        "Avoid `--include-source` by default; compact agent payloads keep source snippets off unless explicitly requested.",
                        "Generated indexes, evidence manifests, reports, and workbench files under `output/arcgraph` are local ignored artifacts and should not be committed.",
                    ],
                },
                {
                    "title": "Recommended Agent Command Set",
                    "items": [
                        "`arcgraph current` or `arcgraph status` checks index state, schema, capabilities, language tiers, freshness, warnings, and evidence snapshot state.",
                        "`arcgraph doctor` is the troubleshooting entrypoint for environment, project config, source roots, and stale or missing index state.",
                        "`arcgraph build` creates a fresh index; `arcgraph reindex --changed` is the smaller refresh path for compatible source edits.",
                        "`arcgraph context TARGET --detail-level summary` returns a bounded task package for coding agents.",
                        "`arcgraph explain TARGET --detail-level summary` returns target-specific provenance, resolution strategy, confidence sources, and direct edge evidence.",
                        "`arcgraph impact TARGET --profile review_default` returns review blast radius; `callers` and `callees` return bounded directional traversal by default. Never pass `--raw` from an agent: it is unbounded and reaches tens of megabytes.",
                        "`arcgraph tests TARGET` returns related tests and coverage gaps.",
                        "`arcgraph evidence status` and `arcgraph evidence plan` expose optional evidence state and next actions without generating evidence.",
                        "`arcgraph ci` is the local graph health gate; `arcgraph benchmark agent-startup` and `arcgraph benchmark suite` measure CLI-per-call latency when deciding whether an MCP server is justified.",
                    ],
                },
                {
                    "title": "Index And Schema Recovery",
                    "items": [
                        "If `current` is unavailable, stale, or schema-incompatible, run `arcgraph doctor` first.",
                        "Use `arcgraph build` for missing or incompatible indexes and `arcgraph sync --if-stale` for the normal stale-index recovery path.",
                        "If a bounded payload's read schema is unsupported, upgrade the client or installed ArcGraph checkout; rebuilding the index does not change the read contract. Rebuild only for an actual index-schema mismatch.",
                        "If JSON parsing fails after an exit code 0, do not guess an envelope; treat it as unexpected output and fall back to `doctor` plus a fresh build.",
                    ],
                },
                {
                    "title": "MCP And Release Boundaries",
                    "items": [
                        "The external-trial surface includes an installed-wheel local stdio server: `arcgraph mcp serve --repo-root . --output-dir output/arcgraph`.",
                        "The default MCP analysis/change/help surface is read-only. An operator can separately enable one privacy-bounded local feedback append tool; npm and Docker/GHCR distribution remain deferred.",
                        "`docs/examples/mcp_readonly_host.py` remains a lower-level read-only host example; it is not the feedback-enabled trial configuration.",
                        "Outside explicit `arcgraph setup --client`, ArcGraph does not auto-configure agent clients. Setup supports Claude Code, Codex, Cursor, Hermes and a Pi CLI skill; client approval and model use remain separate checks. Use `arcgraph docs mcp-server` and the Client Integration Examples section of `docs/agent-reading-guide.md` for manual examples.",
                        "Each PyPI upload, including 0.1.0, is made by a maintainer after separate human approval; npm, Docker/GHCR, GitHub Releases, tags, and repository visibility changes also require separate human approval.",
                        "No language is claimed as L4. Python and TypeScript/JavaScript are native L3, Next.js and Vue are framework semantics over TS/JS, Go/C#/Java/Rust/C/C++/Swift are external payload-backed L3, and SCIP/OpenAPI are explicit L2 protocol evidence.",
                    ],
                },
            ],
        }
    if topic == "mcp-server":
        return {
            "topic": topic,
            "title": "ArcGraph MCP Server",
            "sections": [
                {
                    "title": "Local Stdio Server Command",
                    "items": [
                        "The external-trial scope includes the installed-wheel local stdio server; source-checkout use remains available for maintainers.",
                        "`arcgraph mcp serve --repo-root . --output-dir output/arcgraph` starts the local stdio MCP server. Its default analysis/change/help surface is read-only.",
                        "`python -m arcgraph.interfaces.mcp_server --repo-root . --output-dir output/arcgraph` exposes the same source-checkout entrypoint.",
                        "Install an external-trial wheel with its MCP extra, or use `python -m pip install -e '.[mcp]'` in a source checkout. CLI help and docs commands work without that dependency.",
                        "Add `--metrics-log PATH` only when explicitly opting in to privacy-bounded local per-tool JSONL metrics; without it the server writes no metrics log.",
                        "Add an absolute `--feedback-log PATH` only when explicitly enabling the strict local Agent feedback append tool; without it the tool is not registered and no feedback log is created.",
                        "Server startup does not run `arcgraph build`, mutate the target repo, write user agent configuration, publish packages, change GitHub settings, or create releases.",
                        "HTTP/network MCP transport is not supported in the external trial; stdio is the only supported transport.",
                    ],
                },
                {
                    "title": "Repository Scope",
                    "items": [
                        "`--repo-root` is the repository exposed to MCP tools.",
                        "`--output-dir` points to the existing ArcGraph index directory. Relative output dirs resolve under the repo root.",
                        "`--repo-id` defaults to `default` and identifies the registered repository in tool calls.",
                        "`--allowed-root` can be repeated; when omitted, allowed roots default to the resolved repo root.",
                        "`--expose-source-snippets` is off by default. Even when a tool request asks for source, snippets stay disabled unless the server was explicitly started with this flag.",
                        "`--metrics-log` is off by default. Enabled events contain only timestamp, tool name, status, duration, payload byte/token estimates, truncation, and enum-only freshness status; they exclude arguments, repo ids, paths, source, returned payload text, raw exceptions, prompts, and client identity. Use `arcgraph metrics PATH --trial-summary` for a shareable aggregate and do not share raw JSONL.",
                        "Reuse one installed executable across projects but start one named process per project with distinct output, metrics, and feedback paths. Each single-project server keeps `repo_id=default`; this is local single-user process/path isolation, not a multi-tenant claim.",
                    ],
                },
                {
                    "title": "Agent Tool Surface",
                    "items": [
                        "`arcgraph_index_status` reports index freshness, counts, and capabilities.",
                        "`arcgraph_get_context`, `arcgraph_explain`, `arcgraph_get_risk`, `arcgraph_entrypoint_flow`, and `arcgraph_find_similar` return bounded read-side graph context.",
                        "`arcgraph_get_why` can include external historical memory only when an external connector is configured by the host.",
                        "`arcgraph_record_learning` is proposal-only and does not persist memory by default; it is documented because the name sounds write-like.",
                        "`arcgraph_help` is always registered and returns bounded workflow, selection, result, recovery, CLI-fallback, and feedback guidance. Protocol `list_tools` remains the authoritative inventory for one process.",
                        "`arcgraph_record_trial_feedback` is registered only with `--feedback-log`. It appends one idempotent privacy-bounded local event, is non-destructive, and has no network access, but is not read-only.",
                        "Path-like targets are checked against the registered repo root and outside-repo paths are rejected or sanitized.",
                    ],
                },
                {
                    "title": "Index State Behavior",
                    "items": [
                        "The server reads an existing index; it does not auto-build during startup.",
                        "Missing, stale, unavailable, or schema-incompatible indexes are returned as status/warning payloads rather than hidden by a background mutation.",
                        "Run `arcgraph doctor` when index state is unclear, `arcgraph build` when it is missing or incompatible, or `arcgraph sync --if-stale` when it is stale.",
                        "`arcgraph_index_status` uses index schema `1.0.0`. Bounded target-scoped MCP tools use read schema `1.4.0` and expose the storage contract separately as `index_schema_version`.",
                        "Agents should inspect `schema_version`, `index_schema_version`, `status`, `freshness`, `warnings`, `truncation`, and `source_snippets` before trusting a payload.",
                        'Since read schema `1.1.0`, `warnings` is a heterogeneous JSON array: each element may be a string or a structured object. Do not pass it directly to string-only operations such as `"\\n".join(warnings)`; branch on the element type and read an object\'s `message`, `kind`, and optional `path` fields.',
                    ],
                },
                {
                    "title": "Client Integration",
                    "items": [
                        "Manual examples for Claude Code, Codex, Cursor/generic MCP JSON clients, and Aider CLI fallback live in the Client Integration Examples section of `docs/agent-reading-guide.md`.",
                        "MCP server startup does not auto-modify Claude, Codex, Cursor, Hermes, Pi or other user configuration files; read queries never do so either. Explicit `arcgraph setup --client` writes the selected client config; see `arcgraph setup --help` and docs/client-setup.md.",
                        "MCP and CLI are not feature-equivalent. CLI Agents use `arcgraph help`; MCP Agents discover the registered surface through `list_tools` and call `arcgraph_help` for usage guidance.",
                        "Use `docs/runbook.md` for operational troubleshooting and the Rules And Boundaries section of `docs/agent-reading-guide.md` for human/agent safety boundaries.",
                        "A stable release, npm and Docker/GHCR publishing, full unattended installation, and HTTP/network MCP transport remain deferred.",
                    ],
                },
            ],
        }
    if topic == "package-readiness":
        return {
            "topic": topic,
            "title": "ArcGraph Package Readiness Gate",
            "sections": [
                {
                    "title": "Local Evidence Gate",
                    "items": [
                        "`python scripts/arcgraph_package_readiness_smoke.py` builds local wheel/sdist artifacts in a temporary directory and installs the built wheel into a fresh temporary virtual environment.",
                        "The installed `arcgraph` command is used for syntax help, bounded Agent help, docs, MCP help, sample-repo build, current/status, context, explain, feedback summarize, and optional `ci`; the source checkout wrapper is not used for installed-package validation.",
                        "The smoke checks required package files, workbench assets, the MCP server module, the TypeScript extractor helper, package metadata, and `package.json` private status (npm remains private/dev-only).",
                        "The smoke verifies that local-only internal docs, generated output, caches, node_modules, dist artifacts, and test suites are not included in package archives.",
                        "Every regular file in the sdist must be a file Git tracks or that the build generates (the package metadata file and `arcgraph/_build_provenance.json`); directories are structural only, everything sits under one top-level directory named after the sdist file, and no member name may repeat. Wheel members may sit only under `arcgraph/` and the wheel's dist-info directory named after the wheel file, and the wheel's RECORD must list every non-directory member exactly once with the SHA-256 and size of the bytes the wheel holds (an empty directory entry needs no row). A member's name and type must agree, and a directory entry carries no content. The archive file names and the embedded METADATA and PKG-INFO must name the project and version in pyproject.toml, and every license file they declare must be present in both archives and identical to the tracked file. Symbolic links, hard links, device files, FIFOs, absolute or non-canonical paths, duplicate names and members outside those locations are rejected in both archives, so a local file that only a developer's global ignore rules hide cannot ship.",
                        "The installed-wheel smoke clears `PYTHONPATH` and `NODE_PATH`, runs outside the checkout, proves a Python project succeeds, and requires `typescript_frontend_unavailable` in both a TS build's `summary.json` and `diagnostics.jsonl` when no compiler runtime is available.",
                        "With the MCP extra enabled, the smoke verifies `arcgraph --version`, runs MCP v2 auto and legacy handshakes plus a separate real v1.28.1 client, calls the exact registered default surface, rejects unexpected server stderr, and requires clean process shutdown.",
                        "The installed-wheel gate also starts two named feedback-enabled single-project stdio servers from one environment, checks their exact registered tool contract, keeps `repo_id=default`, exercises both CLI and MCP feedback record/summarize, and proves graph/current state, path authorization, metrics, feedback, and process lifecycle stay isolated.",
                        "The script fails before building when tracked, staged or untracked source state is dirty; `--dry-run` reports dirty state and builds nothing. Its JSON evidence binds the Git commit, source tree, clean-status digest, and wheel and sdist SHA-256 values, and the source is rechecked after the run, so a dirty or source-mutating run is invalid evidence.",
                        "The installed `arcgraph version --json` must identify an installed distribution, bind the embedded source commit, and report the wheel SHA-256 that the gate built. The installed wheel also runs `arcgraph trial setup --client claude --dry-run` against a sample repository and must leave Claude configuration and local trial state unchanged.",
                        "`--artifact-dir` atomically preserves the exact validated wheel and sdist in a new Git-ignored or external directory; it never overwrites an existing destination.",
                    ],
                },
                {
                    "title": "What It Does Not Approve",
                    "items": [
                        "This does not publish PyPI.",
                        "This does not publish npm.",
                        "This does not publish Docker/GHCR.",
                        "This does not create GitHub Releases or tags.",
                        "This does not make the repo public.",
                        "This does not approve package publishing.",
                        "This does not approve public/packaged MCP distribution.",
                        "This does not replace public release/cutover approval.",
                    ],
                },
                {
                    "title": "Deferred Channels",
                    "items": [
                        "Python sdist install is deferred unless a separate sdist install smoke is run.",
                        "Homebrew, Scoop, Winget, Docker/GHCR, npm package publishing, GitHub Releases, and tags remain separate gates; this smoke does not test the PyPI release.",
                        "A local run proves only the current host; the configured remote matrix is required for each claimed operating system and Python line. The `CI` workflow on GitHub Actions runs that matrix; a passing run is evidence only for the commit it ran on.",
                        "Distribution of the MCP server through channels other than the PyPI package (MCP registries, npm, Docker/GHCR) remains deferred and requires separate approval; installed-wheel protocol evidence does not grant publication approval.",
                    ],
                },
            ],
        }
    if topic == "source-checkout-smoke":
        return {
            "topic": topic,
            "title": "ArcGraph Source-Checkout Smoke",
            "sections": [
                {
                    "title": "Source Checkout Assumption",
                    "items": [
                        'The product path checked here is a source checkout installed with `python -m pip install -e .` or `python -m pip install -e ".[dev]"`.',
                        "Node.js and a resolvable TypeScript compiler API are required at analysis runtime for TypeScript/JavaScript projects. The Python wheel does not bundle `node_modules/typescript`; npm remains a dependency-install mechanism, not an approved product distribution channel.",
                        "The external-trial guarantee is Python analysis plus local stdio MCP with a default read-only analysis/change/help surface and optional disclosed local feedback. TS/JS is outside the trial acceptance even when a local runtime makes it available.",
                        "If `arcgraph` is not on PATH, use `python scripts/arcgraph.py <command>` from the checkout.",
                        "This smoke does not publish to PyPI, npm or Docker/GHCR and creates no GitHub Release or tag; the PyPI release is not exercised by it.",
                    ],
                },
                {
                    "title": "Smoke Commands",
                    "items": [
                        "`python scripts/arcgraph_source_checkout_smoke.py` runs an executable local smoke in a temporary project and cleans up generated output by default.",
                        "`arcgraph --help` and `python -m pip show arcgraph` verify the installed CLI surface.",
                        "`arcgraph doctor` checks environment, project config, source roots, index state, optional evidence, precision tools, and Python version.",
                        "`arcgraph init --dry-run` previews `[tool.arcgraph]` source-root config without writing project files.",
                        "`arcgraph build` creates the local index under `output/arcgraph`.",
                        "`arcgraph current`, `arcgraph status`, and `arcgraph stats` verify schema, freshness, capabilities, warnings, language tiers, evidence state, and graph counts.",
                        "`arcgraph context arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level summary` and `arcgraph explain arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level summary` verify agent payloads for a known target.",
                        "`arcgraph ci` verifies local graph health and returns nonzero on failures.",
                        "`arcgraph docs agent-cli-contract` verifies the local subprocess JSON contract for coding agents.",
                        "`arcgraph docs mcp-server` documents the local stdio MCP server, its default read-only tools, and optional feedback.",
                        "`arcgraph mcp serve --help` verifies MCP server CLI wiring without requiring the optional MCP runtime.",
                        "`python docs/examples/mcp_readonly_host.py --repo-root . --output-dir output/arcgraph` exercises the lower-level example host path when a compatible MCP host framework is installed separately.",
                    ],
                },
                {
                    "title": "Expected Warnings And Blockers",
                    "items": [
                        "Warnings about missing optional evidence, precision tools, runtime trace, coverage, or optional TypeScript toolchain can be expected on a default static checkout.",
                        "Blockers for agent use include missing source roots, build failures, stale or schema-incompatible indexes, JSON parse failures, `ci` failures, and command output that does not match the documented agent contract.",
                        "Generated indexes, evidence manifests, reports, workbench files, and smoke outputs under `output/arcgraph` are local ignored artifacts and should not be committed.",
                    ],
                },
                {
                    "title": "Package And Release Decisions",
                    "items": [
                        "PyPI: the published versions are listed in RELEASE_NOTES.md; this smoke neither publishes nor tests them.",
                        "npm package publishing remains private/dev-only unless separately authorized.",
                        "Docker/GHCR publishing remains unapproved.",
                        "GitHub Releases and tags mirror published PyPI versions; this smoke creates neither, and each new one requires separate human approval.",
                        "A future package channel requires a separate package readiness gate.",
                        "Run `arcgraph docs package-readiness` for the local wheel/sdist readiness gate that does not publish anything.",
                        "The external-trial scope includes the installed-wheel local stdio server; npm and Docker/GHCR distribution remain separately unapproved.",
                    ],
                },
            ],
        }
    if topic == "limitations":
        return {
            "topic": topic,
            "title": "ArcGraph Limitations",
            "sections": [
                {
                    "title": "General Contract",
                    "items": [
                        "ArcGraph is a local-first semantic graph and evidence platform for agents; it is not a compiler, IDE, hosted SaaS, or proof of full program correctness.",
                        "Confidence values are evidence labels (`confirmed`, `inferred`, `heuristic`, `runtime-only`, `unresolved`), not guarantees that every possible runtime behavior has been observed.",
                        "Unsupported or dynamic behavior should remain visible as warnings, diagnostics, unresolved records, or lower-confidence edges instead of being silently resolved.",
                        "`arcgraph benchmark agent-startup` and `arcgraph benchmark suite` report local-machine performance baselines; they are not universal latency guarantees for every repo, shell, filesystem, or host.",
                    ],
                },
                {
                    "title": "Python",
                    "items": [
                        "Python analysis is static and conservative; dynamic dispatch, reflection, metaprogramming, monkeypatching, and framework magic may stay unresolved.",
                        "Release blocking is limited to clear static candidates and missing type context; expected dynamic boundaries should remain visible without blocking release gates.",
                        "SCIP/Pyright, coverage, and runtime trace improve evidence but do not imply complete type, coverage, or runtime behavior coverage.",
                    ],
                },
                {
                    "title": "TypeScript And SCIP",
                    "items": [
                        "The TypeScript frontend uses the compiler API for committed static scenarios and includes checker-backed receiver call resolution where a local declaration can be mapped deterministically; it is not complete TypeScript program, bundler, or package-manager proof.",
                        "TypeScript module resolution supports common `tsconfig` aliases, static `package.json` `exports` / `imports` mappings including conservative nested condition objects, repo-local workspace package hints, static Vite/Webpack `resolve.alias` hints, and literal dynamic import hints when they uniquely resolve to repo-local files.",
                        "`arcgraph reindex --changed` parses changed TypeScript/JavaScript files while reusing persisted module identities and declaration context. Supported static imports to unchanged local modules and deterministically mapped direct `calls` remain resolved; dynamic, ambiguous, or otherwise unmappable relationships remain best-effort, so run a full `arcgraph build` before relying on complete import topology.",
                        "Cross-file Express mount relationships currently require a full TypeScript/JavaScript view. If the current index contains Express, `arcgraph reindex --changed` fails closed and asks for a full `arcgraph build` instead of publishing routes from incomplete mount context.",
                        "Adding or removing a coexisting TypeScript/JavaScript module variant can change an existing file's module identity. Incremental reindex fails closed in that case because retained import or call edges could otherwise retarget silently; run a full `arcgraph build`.",
                        "Large TypeScript similarity buckets use bounded approximate candidate generation and a strongest-neighbour edge cap. `similarity_bucket_approximated` and `similarity_edges_capped` warnings disclose when exhaustive pairs or lower-ranked edges were omitted; dense repositories can still produce sizeable linear output.",
                        "Unsupported TypeScript behavior includes expression-based dynamic imports, dynamic/function/regex bundler aliases, plugin-provided aliases, complete Node/package-manager condition evaluation, package-manager linking semantics, cross-repo package identity, and highly dynamic framework registration.",
                        "`--scip-graph-index` consumes existing SCIP JSON as protocol facts; ArcGraph does not run language indexers, infer calls from references, or claim complete Go/C#/Java/Rust/C/C++/Swift semantics from SCIP.",
                    ],
                },
                {
                    "title": "Frontend Extension",
                    "items": [
                        "ArcGraph supports Python and TypeScript/JavaScript frontends by default, plus explicit opt-in SCIP and OpenAPI protocol graph ingestion through `--scip-graph-index` and `--openapi-spec`.",
                        "Go, C#, Java, Rust, C, C++, and Swift L3 support is explicit and payload-backed through validated external semantic extractor frontends; live extractor execution is not part of the default public workflow.",
                        "Frontend Plugin Contract v1 is a build-time in-process contract; ArcGraph does not yet provide automatic plugin discovery, package entry point loading, remote plugins, or plugin sandboxing.",
                        "New language support should first add a contract-compliant frontend and golden matrix; it should not claim complete language semantics without protocol evidence such as SCIP, LSP, or compiler APIs.",
                    ],
                },
                {
                    "title": "Runtime, Workspace, And Visuals",
                    "items": [
                        "Runtime trace evidence is explicit and advisory; it adds `runtime-only` facts from a bounded pytest trace, including the committed FastAPI/TestClient request smoke, and never becomes a runtime dependency.",
                        "OpenTelemetry support is offline span import only: ArcGraph does not install an SDK, run a collector, instrument services, or infer edges from unmappable spans.",
                        "HAR support is offline browser network import only: ArcGraph does not start a browser, collect Playwright traces, infer frontend source call chains, or treat network observations as browser coverage.",
                        "Browser coverage support is offline coverage import only: ArcGraph consumes exported Istanbul or Playwright/Chrome JSON as coverage evidence, without source-map reconstruction, browser automation, or frontend call-chain proof.",
                        "Phase 6 evidence support is explicit offline import only: runtime trace, OpenTelemetry spans, HAR network observations, and browser coverage require user-provided artifacts and do not provide browser call-chain proof.",
                        "`runtime-only` evidence does not overwrite static confidence. Inspect it with `explain`, `context`, compact relation queries, or `impact --compact` when you need to audit which observed edge came from a trace.",
                        "FastAPI/TestClient route/helper calls that run inside framework worker threads are best-effort observations because Python profiling hooks depend on thread creation timing.",
                        "Workspace commands are opt-in routing and context aggregation over independent repos; they do not merge SQLite indexes or create cross-repo graph edges.",
                        "The workbench is a human audit surface over exported/queryable facts. It does not replace CLI/MCP agent payloads or expose source snippets by default.",
                    ],
                },
            ],
        }
    if topic == "evidence-cookbook":
        return {
            "topic": topic,
            "title": "ArcGraph Evidence Cookbook",
            "sections": [
                {
                    "title": "Precision Evidence",
                    "items": [
                        "Generate SCIP JSON with `arcgraph precision scip-python --output output/arcgraph/scip-index.json --index-file output/arcgraph/index.scip --project-name NAME` or convert an existing SCIP index with `arcgraph precision scip-json --input index.scip --output output/arcgraph/scip-index.json`.",
                        "Generate Pyright evidence with `arcgraph precision pyright --output output/arcgraph/pyright-export.json --python-version 3.11`.",
                        "Import precision evidence with `arcgraph build --scip-index output/arcgraph/scip-index.json --pyright-export output/arcgraph/pyright-export.json`.",
                    ],
                },
                {
                    "title": "Coverage And Runtime Trace",
                    "items": [
                        "Generate coverage XML with your test command, for example `python -m pytest --cov=. --cov-report=xml:output/arcgraph/coverage.xml`, then stamp external scope metadata with `arcgraph evidence stamp --kind coverage --path output/arcgraph/coverage.xml --tool-name coverage`.",
                        "Import offline browser coverage by exporting Istanbul or Playwright/Chrome coverage JSON, then running `arcgraph build --coverage output/arcgraph/browser-coverage.json`; browser coverage creates `runtime-only` `coverage_run -> module/symbol` `covers` edges when files map uniquely.",
                        "Record runtime evidence with `arcgraph trace run --output output/arcgraph/runtime-trace.json -- <pytest target>`; committed baselines cover direct pytest calls and a FastAPI/TestClient request smoke. Framework worker-thread calls are best-effort.",
                        "Import runtime evidence with `arcgraph trace import output/arcgraph/runtime-trace.json` or pass `--runtime-trace output/arcgraph/runtime-trace.json` to `arcgraph build` or `arcgraph ci`.",
                        "Import externally captured OpenTelemetry spans with `arcgraph trace import-otel output/arcgraph/otel-spans.json`; only spans with explicit ArcGraph source/target attributes become `runtime-only` edges.",
                        "Import browser or Playwright-exported HAR 1.2 network observations with `arcgraph trace import-har output/arcgraph/network.har`; only HTTP(S) requests that uniquely match indexed routes become `runtime-only` `runtime_session -> route` edges.",
                        'After import, audit runtime evidence with `arcgraph explain TARGET --detail-level detailed`, `arcgraph context TARGET --task "review runtime evidence"`, `arcgraph callers TARGET --compact`, or `arcgraph impact TARGET --compact`; runtime-derived edges are labeled `runtime-only` with `resolution.strategy=runtime_trace`, `otel_span_import`, or `har_network_import`.',
                        "Evidence import matrix: `trace import`, `trace import-otel`, `trace import-har`, and `build --coverage` cover runtime trace, OpenTelemetry, HAR, Cobertura/custom coverage, Istanbul, and Playwright/Chrome artifacts; unsupported, ambiguous, stale, or repo-outside inputs remain visible as warnings/partial metrics.",
                    ],
                },
                {
                    "title": "Protocol Graph Evidence",
                    "items": [
                        "Pass an existing `scip print --json` payload as a language-neutral graph input with `arcgraph build --scip-graph-index path/to/scip-index.json`.",
                        "`--scip-graph-index` is separate from `--scip-index`: protocol graph input creates document/module/definition/reference facts, while precision SCIP remains Python precision evidence.",
                        "ArcGraph does not install or run external SCIP indexers; generate SCIP payloads in your language-specific toolchain first.",
                    ],
                },
                {
                    "title": "Auditing Evidence Health",
                    "items": [
                        "Run `arcgraph evidence status` to compare live evidence artifacts with the current index snapshot.",
                        "Run `arcgraph evidence plan --profile python_full` to see missing, stale, invalid, or out-of-scope evidence and suggested generation commands.",
                        "`current`, `ci`, and release gates read the build snapshot; `doctor`, `evidence status`, and `evidence plan` read the live manifest at `output/arcgraph/evidence/manifest.json`.",
                    ],
                },
            ],
        }
    if topic == "capabilities":
        return {
            "topic": topic,
            "title": "Frontend Capability Reference",
            "sections": [
                {
                    "title": "Core Capabilities",
                    "items": [
                        "`semantic_facts`, `diagnostics`, and `merge_metrics` mean platform staging and merge artifacts are persisted.",
                        "`semantic_stats`, `unresolved`, `bindings`, and `types` mean selected Python V2 queries are enabled.",
                        "`receiver_resolution=available` means the current index has Python V2 receiver-aware call resolution enabled.",
                        "V2 receiver resolution is the schema 1.0 default; `receiver_resolution=legacy` means the explicit escape hatch was used.",
                        "The receiver-resolution fallback window is closed at schema `1.0.0`; legacy resolution is not accepted by `ci --python-full`.",
                        "`semantic_quality_targets=pass|warn|fail` reports whether overall, attribute, chain, and framework registration quality targets are met for the current index.",
                        "`tool.arcgraph.ci.semantic_quality_scope` can include or exclude path globs for CI quality targets; ordinary `semantic-stats` and graph queries remain full-index.",
                    ],
                },
                {
                    "title": "Framework Adapters",
                    "items": [
                        "`framework_adapters=available|partial|unavailable` describes whether framework-specific extraction ran successfully.",
                        "FastAPI, Django, MCP, ARQ, Celery, service-container, SQLAlchemy, logging, Pydantic, pytest, and Typer/Click adapters add framework semantics beyond raw Python calls.",
                        "Django extraction adds URL route entrypoints, ORM model-to-table mappings, admin registrations, and best-effort ORM read/write edges.",
                        "Celery extraction adds worker task entrypoints, queue resources, task-to-queue consume edges, and enqueue edges for task dispatch calls.",
                        "Adapter metrics are reported under `current.adapter_metrics`; `arcgraph ci` checks adapter diagnostics and framework registration quality.",
                        "The framework adapter matrix fixture protects committed Django, Celery, ARQ, FastAPI, Pydantic, pytest, SQLAlchemy, and Typer/Click scenarios; it is a regression floor, not a claim that arbitrary framework metaprogramming is fully resolved.",
                    ],
                },
                {
                    "title": "TypeScript Frontend",
                    "items": [
                        "The TypeScript/TSX frontend is a compiler-AST baseline for imports, exports, functions, classes, interfaces, type aliases, enums, React components, hooks, JSX render edges, route/API calls, and test files.",
                        "TypeScript module resolution handles common `tsconfig.json`/`jsconfig.json` `baseUrl` and `paths` aliases, including `extends` inheritance, for patterns such as `@/*`, `@app/*`, exact aliases, and baseUrl-relative imports; ambiguous or missing alias matches stay visible as warnings instead of being forced into local edges.",
                        "Static `package.json` `exports` / `imports` resolution covers package self-reference, subpath exports, and `#imports` exact or single-wildcard mappings when they uniquely map to repo-local files; ambiguous or unsupported package targets stay visible as import warnings.",
                        "TypeScript config/resource extraction records explicit static `process.env`, `import.meta.env`, `window/globalThis.localStorage`, and `window/globalThis.sessionStorage` accesses as advisory resource-flow hints; dynamic keys stay visible as `typescript_config_dynamic` warnings.",
                        'These config/resource hints cover literal keys only, including `process.env["KEY"]` and `import.meta.env["KEY"]`; they are not secret scanning, TypeScript data-flow, or runtime proof.',
                        "The TypeScript baseline golden fixture is a regression floor for committed scenarios; it is not a claim that arbitrary JavaScript or TypeScript projects are fully resolved.",
                        "When Node.js or the TypeScript compiler API is unavailable, builds should report `typescript_frontend_unavailable` as a warning rather than crashing.",
                        "Cross-language strategy remains protocol-first: prefer TS compiler API, SCIP, LSP, or tree-sitter evidence over hand-written deep language server behavior.",
                    ],
                },
                {
                    "title": "Frontend Plugin Contract",
                    "items": [
                        "The frontend contract v1 is the local build-time `LanguageFrontend` interface: declare name, language IDs, version, file extensions, capabilities, detection, file acceptance, and `analyze_to_graph()`.",
                        "`analyze_to_graph()` must return a `FrontendGraphFragment` containing `Node`, `Edge`, and `BuildWarning` objects; the indexer validates this contract before merging graph data.",
                        "`adapter_metrics` must be compact JSON-serializable metadata, and `phase_timings` values must be numeric seconds.",
                        "ArcGraph currently has no automatic third-party frontend discovery or installation mechanism; future languages should first implement this contract plus a golden matrix before adding deeper semantics.",
                        "Frontends must use the canonical confidence vocabulary (`confirmed`, `inferred`, `heuristic`, `runtime-only`, `unresolved`) and should surface uncertainty as warnings or diagnostics instead of inventing new confidence levels.",
                    ],
                },
                {
                    "title": "SCIP Protocol Graph Input",
                    "items": [
                        "`arcgraph build --scip-graph-index PATH` consumes an existing `scip print --json` style payload as an explicit build-time frontend.",
                        "SCIP protocol ingestion emits confirmed document/module, definition, and reference facts from the payload, but it does not infer calls, imports, cross-repo identity, or language-specific deep semantics.",
                        "`--scip-graph-index` is separate from `--scip-index`: the former adds language-neutral graph structure; the latter remains Python precision evidence for confirmed references.",
                        "Malformed, missing, duplicate, or unmapped SCIP payload details stay visible as warnings. ArcGraph does not run external SCIP indexers or install language toolchains for this input.",
                    ],
                },
                {
                    "title": "External Evidence",
                    "items": [
                        "`precision=precision_available|precision_partial|ast_fallback_only` describes SCIP/Pyright input status.",
                        "`precision_available` means reference precision exists; check `type_precision_complete`, `pyright_status`, `pyright_lsp_type_info_total`, `pyright_lsp_requestable_probe_total`, `pyright_lsp_skipped_unmappable_total`, `scip_type_occurrences`, and `type_occurrences` before claiming type precision is complete.",
                        "Even when `type_precision_complete=true`, unresolved and skipped Pyright LSP probe counts remain part of the precision boundary and must not be described as full-project type coverage.",
                        "`arcgraph precision scip-python`, `arcgraph precision scip-json`, and `arcgraph precision pyright` generate JSON artifacts accepted by `arcgraph build --scip-index ... --pyright-export ...`.",
                        "`runtime_trace=available|partial|unavailable` describes explicit runtime trace import status.",
                        "A small CI smoke trace proves the runtime evidence chain and stale detection only; broad dynamic behavior coverage requires an intentionally broader trace profile.",
                        "`coverage=available|partial|unavailable` describes test coverage input status; `partial` means the file was stale, invalid, or produced only partial evidence.",
                    ],
                },
                {
                    "title": "Release Scope Boundaries",
                    "items": [
                        "Current release readiness is gated by a clean worktree, fresh index, CI success, release gate success, package smoke when applicable, public-safe docs, and explicit human approval for any public cutover.",
                        "The Python precision benchmark fixture protects committed analyzer and adapter scenarios; it is a regression floor, not a claim that arbitrary Python projects are fully resolved.",
                        "The framework adapter matrix fixture protects committed framework extraction scenarios separately from the Python precision benchmark; dynamic framework magic should remain visible unless adapter evidence confirms it.",
                        "The TypeScript frontend baseline protects committed compiler API extraction scenarios separately from Python quality gates; unsupported JS/TS dynamic behavior, including dynamic config/resource keys, non-static bundler alias resolution, complete package-manager condition evaluation, package-manager linking semantics, and cross-repo package identity, should stay visible as warnings or future diagnostics.",
                        "Dynamic boundary triage is an agent decision aid, not a compiler proof: external service, framework magic, generated/reflection, and true-dynamic diagnostics stay visible but do not block release unless they are classified as release-blocking.",
                        "Unresolved classification follows recorded evidence rather than spelling or directory location: a direct call through a visible runtime local binding (such as a parameter, loop target, or unresolved local alias) is `true_dynamic_call`, explicit mock/assertion helper syntax is `mock_or_assertion`, and an otherwise ordinary unresolved call remains a release-blocking `static_candidate` even under `tests/`.",
                        "Remaining `true_dynamic_call`, `mock_or_assertion`, `framework_magic`, `generated_or_reflection`, and `external_service_boundary` diagnostics are expected to stay visible and should be reviewed when future business changes touch those paths.",
                        "Live external extractor binaries, MCP server distribution beyond the PyPI package, npm and Docker/GHCR publishing, and L4 language claims remain deferred productization items on the platform P2 roadmap.",
                    ],
                },
            ],
        }
    if topic == "visualization":
        return {
            "topic": topic,
            "title": "ArcGraph Visualization Guide",
            "sections": [
                {
                    "title": "Quick Start",
                    "items": [
                        "Build an index first: `arcgraph build`.",
                        "Generate the local ArcGraph Explorer workbench: `arcgraph visual workbench --output-dir output/arcgraph/reports/workbench --open`.",
                        "For large repositories or on-demand audit, run the local read-only server: `arcgraph visual serve --host 127.0.0.1 --port 8765 --open`.",
                        "Without `--open`, open `output/arcgraph/reports/workbench/index.html` in a browser (a local file server like `python -m http.server 8080 -d output/arcgraph/reports/workbench` also works).",
                        "Open the serve URL printed by `visual serve` when you want remote-mode search, focus, node detail, and audit APIs without shipping full static indexes.",
                        "Use `arcgraph visual force --output viz/graph_data.json` only when you need the raw graph payload for another consumer.",
                        "Maintainers can run `arcgraph visual smoke --output-dir output/arcgraph/reports/visual-smoke` to exercise Overview, Focus, SVG/Canvas, Density, Labels, and audit drawer in a real browser when Playwright CLI is locally available.",
                        "The one-click Architecture Overview button sets Architecture layout + Entry→Resource lens + Collapse All for an instant bird's-eye view.",
                        "Use `--initial-focus TARGET --focus-depth 1 --focus-direction both` to open a static ego-graph around a target.",
                    ],
                },
                {
                    "title": "Dimensions And Controls",
                    "items": [
                        "Granularity (Modules | Symbols): choose module-level or symbol-level graph. Double-click a module to expand its symbols in place.",
                        "Layout (Package | Architecture): Package groups nodes by package; Architecture arranges nodes into semantic tiers — Entry Points (top), Logic, Data Models, Resources (bottom).",
                        "Lens (All | Call Flow | Data Flow | Entry→Resource): filters edges by relationship type. Call Flow keeps `calls/invokes/constructs/initializes`; Data Flow keeps `reads/writes/enqueues/consumes/configures/provides`; Entry→Resource shows only paths from entry points to resources via BFS.",
                        "Confidence slider: from All to Confirmed only. Default is ≥Runtime, which hides heuristic and unresolved edges.",
                        "Min Strength slider: hides low-weight edges in the module graph.",
                        "Collapse All / Expand All (Modules view only): right-click any module to collapse its `sub_package` into a single synthetic node. Collapsed nodes show a dashed ring and member count.",
                        "Mode (Overview | Focus): Overview shows modules or the capped symbol overview; Focus renders a local symbol ego-graph from the static `focus_index`.",
                        "Renderer (Auto | SVG | Canvas): Auto uses SVG for small rendered subgraphs and switches to 2D Canvas for larger rendered node/edge counts; SVG remains the compatibility/debug path.",
                        "Density (Auto | Full | Balanced | Sparse): controls display-only edge reduction. Full preserves all rendered edges; Balanced and Sparse reduce low-signal edges while keeping selected and focus-seed neighborhoods visible.",
                        "Labels (Auto | Key | All | None): controls label level of detail without changing search, tooltip, drawer, or graph data.",
                        "The top status strip shows static/remote mode, index freshness, CI, evidence health, payload warnings, renderer, density, labels, and rendered counts.",
                        "Focus depth is intentionally limited to 1-2 in the static workbench to keep the graph readable.",
                        "Architecture layout consumes the backend `visual_contract` and exported `semantic_tier` fields instead of guessing tiers from path or module-name regexes.",
                    ],
                },
                {
                    "title": "Export Options",
                    "items": [
                        "`--include-tests` adds test files and test-only nodes to the export.",
                        "`--include-generated` adds generated, coverage, backup, and build-output files.",
                        "`--include-external` adds `external_symbol` and `protocol_symbol` nodes to the symbol graph.",
                        "`--include-structural-edges` adds `references/uses/contains/defines/declares` edges that are hidden by default.",
                        "`--include-edge-kind KIND` restricts the export to specific edge kinds (repeatable).",
                        "`--exclude-edge-kind KIND` removes specific edge kinds (repeatable).",
                        "`--max-symbol-nodes N` caps symbol-graph nodes (default 400); `--min-symbol-degree N` sets the minimum degree threshold (default 5).",
                        "`--initial-focus TARGET` seeds the Focus view; `--focus-depth 1|2` and `--focus-direction incoming|outgoing|both` control the exported initial ego-graph.",
                        "The static `focus_index` carries all focusable symbols under the current export filters, while `symbol_graph` remains a capped overview rendering set.",
                        "`--include-audit-index` is enabled by default and adds compact node audit summaries for the drawer; the default audits a lightweight 150-node sample. Use `--audit-max-nodes N` to opt into larger static summaries or `--no-audit-index` to skip it.",
                        "`audit_index` is a static summary for human inspection, not the full CLI/MCP agent payload. It omits source snippets and raw properties.",
                        "Every graph payload includes `payload_budget` metadata with estimated section bytes and non-fatal static-package warnings; large repositories should use this to decide when an on-demand serve workflow is needed.",
                        "`visual serve` is loopback-only and read-only; it serves the packaged workbench and `/api/status`, `/api/graph`, `/api/search`, `/api/focus`, `/api/node`, and `/api/audit`.",
                        "`visual smoke` starts a temporary loopback workbench server, drives it with Playwright CLI when available, and records non-git-tracked QA artifacts under `output/arcgraph/reports/visual-smoke/` by default.",
                    ],
                },
                {
                    "title": "Interaction",
                    "items": [
                        "Click a node to open the detail drawer with path, package, outgoing/incoming connections, and static audit sections when the node is present in `audit_index`.",
                        "Drawer audit sections summarize provenance, impact, tests, unresolved triage, and recommended reads without source snippets.",
                        "If a node has no static audit entry, regenerate with a higher `--audit-max-nodes` or use `arcgraph visual serve` for on-demand audit in large repositories.",
                        "In `visual serve` remote mode, search, focus, node detail, and audit summaries are loaded on demand from `/api/*`; the default overview payload avoids eager full `focus_index` and `audit_index` data.",
                        "Double-click a module node to expand its symbols inline.",
                        "Right-click a module to collapse its sub-package; right-click the collapsed node to expand.",
                        "Drag nodes to reposition them; scroll to zoom; click the fit-view button to reset the viewport.",
                        "Click a legend item to toggle visibility of that node kind.",
                        "Search by name, qualname, or path in the search box; click a result to switch into Focus mode for that symbol.",
                        "The drawer shifts zoom/fit controls left while open, so navigation remains reachable in SVG and Canvas modes.",
                        "If a static package size warning appears, the workbench remains usable; reduce `--audit-max-nodes`, narrow export filters, or use `arcgraph visual serve` for very large repositories.",
                        "Canvas improves 2D rendering scale after focus/aggregation; 3D remains optional and is not the primary fix for dense code graphs.",
                        "Visual smoke is a maintainer QA baseline, not a required runtime dependency; when npx/Playwright is unavailable it records a skipped result instead of changing normal validation.",
                    ],
                },
            ],
        }
    if topic == "quickstart":
        return {
            "topic": topic,
            "title": "ArcGraph Quickstart",
            "sections": [
                {
                    "title": "Install From A Checkout",
                    "items": [
                        "ArcGraph requires Python 3.11 or 3.12 (`requires-python >=3.11,<3.13`) and is currently installed from a source checkout.",
                        "Create and activate a virtual environment, then run `python -m pip install --upgrade pip`.",
                        "Run `python -m pip install -e .` for the normal CLI install.",
                        'Run `python -m pip install -e ".[dev]"` when you need development dependencies or local validation.',
                        "For TypeScript/JavaScript analysis, Node.js and a TypeScript compiler API must be resolvable at runtime. Use the analyzed project's locked npm install; the Python wheel includes the `.mjs` extractor but not `node_modules/typescript`.",
                        "For ArcGraph's own TypeScript golden tests from a source checkout, run `npm ci` in the ArcGraph checkout; that is development setup, not the analyzed project's product runtime.",
                        "The external-trial guarantee is Python analysis through the installed CLI plus local stdio MCP with default read-only analysis/change/help tools and optional disclosed local feedback; TS/JS remains outside its acceptance scope.",
                        "Run `python -m pip install -e '.[mcp]'` only when you need to start the local MCP server.",
                        "If the `arcgraph` executable is not on PATH, use `python scripts/arcgraph.py <command>` from the source checkout.",
                        "ArcGraph is on PyPI; 0.1.0 is a beta release. Git tags (`v` followed by the version) and GitHub releases carry the same two files as the matching version on PyPI. npm and Docker/GHCR package paths are not published.",
                    ],
                },
                {
                    "title": "First Run",
                    "items": [
                        "Run `arcgraph --help` and `python -m pip show arcgraph` to confirm the CLI install.",
                        "Run `arcgraph help` for bounded Agent-oriented discovery; `arcgraph --help` remains exact syntax and `arcgraph docs` remains the long-form reference.",
                        "Run `arcgraph doctor` before the first build to check project config, source roots, index state, optional evidence, precision tools, and Python version.",
                        "Run `arcgraph init --dry-run` to preview `[tool.arcgraph]` source-root config before writing project config.",
                        "Run `arcgraph build` to create the local index, then `arcgraph current` or `arcgraph status` to confirm freshness and capabilities.",
                        "Run `arcgraph stats` to inspect file, node, and edge counts.",
                        "Use `arcgraph context arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level summary` for a known first agent task context package.",
                        "Use `arcgraph explain arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level summary` to verify provenance, resolution strategy, and confidence sources for a known target.",
                        "Run `arcgraph ci` after build to check local graph health.",
                        "Run `arcgraph evidence status` to inspect the evidence manifest snapshot and `arcgraph evidence plan --profile python_full` to see missing optional evidence actions.",
                        "Generated indexes, evidence manifests, reports, and visual workbench files are written under `output/arcgraph` and should not be committed.",
                    ],
                },
                {
                    "title": "Human Audit Surfaces",
                    "items": [
                        "Run `arcgraph visual workbench --output-dir output/arcgraph/reports/workbench --open` for the static local ArcGraph Explorer.",
                        "Run `arcgraph visual serve --host 127.0.0.1 --port 8765 --open` when large repositories need loopback-only on-demand search, focus, node detail, and audit summaries.",
                        "Run `arcgraph mcp serve --repo-root . --output-dir output/arcgraph` when a manually configured MCP client needs the local stdio server; omit `--feedback-log` for the default read-only surface.",
                        "Run `arcgraph benchmark suite --iterations 5 --warmups 1 --output output/arcgraph/reports/benchmark-suite.json` to record local CLI-per-call and query latency baselines.",
                    ],
                },
                {
                    "title": "Common First-Run Issues",
                    "items": [
                        "If `arcgraph` is not found, activate the virtual environment or use `python scripts/arcgraph.py <command>` from the checkout.",
                        "If Python is outside 3.11 or 3.12, install Python 3.11 or 3.12 and recreate the virtual environment.",
                        "If Node.js or the TypeScript compiler API is missing, provide the analyzed project's locked runtime when TypeScript/JavaScript analysis is required. Otherwise confirm `typescript_frontend_unavailable` in the build summary and diagnostics and continue only under an explicitly Python-only scope.",
                        "If `pyproject.toml` or source roots are missing, run `arcgraph init --dry-run`, then `arcgraph init` only after reviewing the proposed config.",
                        "If the index is missing or stale, run `arcgraph doctor`, then use `arcgraph build` for a missing or incompatible index or `arcgraph sync --if-stale` for a stale one.",
                        "If optional precision tools are missing, default static builds still work; run `arcgraph evidence plan --profile python_full` when a precision profile is required.",
                    ],
                },
                {
                    "title": "Governance References",
                    "items": [
                        "Run `arcgraph docs agent-cli-contract` before wiring a coding agent to ArcGraph through a subprocess JSON contract.",
                        "Run `arcgraph docs mcp-server` before wiring a coding agent to ArcGraph through the local MCP server.",
                        "Run `arcgraph docs source-checkout-smoke` to validate the source-checkout product path without public publishing.",
                        "Run `arcgraph docs package-readiness` before validating local wheel/sdist readiness without publishing.",
                        "Run `arcgraph docs release-checklist` before publishing or sharing a release candidate.",
                        "Run `arcgraph docs schema-governance` before changing schema, resolver defaults, graph storage, or evidence import compatibility.",
                        "Run `arcgraph docs security-model` to review local-only defaults, no telemetry, path containment, snippet redaction, loopback-only serve, and evidence redaction boundaries.",
                        "Run `arcgraph docs frontend-contract` before adding or changing a language frontend; it documents the contract, current built-in frontend inventory, and required golden-test path.",
                    ],
                },
            ],
        }
    if topic == "security-model":
        return {
            "topic": topic,
            "title": "ArcGraph Security Model",
            "sections": [
                {
                    "title": "Local Execution Boundary",
                    "items": [
                        "ArcGraph is local-only by default: it indexes files on disk and writes generated artifacts under `output/arcgraph` unless explicitly configured otherwise.",
                        "ArcGraph has no telemetry exporter, hosted service dependency, or background collector in default CLI workflows and configures no automatic or remote telemetry. The optional MCP SDK exposes OpenTelemetry API hooks, but an operator-configured global provider remains host behavior.",
                        "The MCP server's explicit local `--metrics-log` option is disabled by default, records privacy-bounded per-tool JSONL only at the operator-selected path, and installs or configures no exporter. `arcgraph metrics PATH --trial-summary` omits the input path, timestamps, arguments, payloads, and raw errors from its aggregate.",
                        "The separate absolute `--feedback-log` option conditionally registers one privacy-bounded local append tool. Metrics and feedback paths must be distinct and a shared file is rejected before either subsystem writes. Feedback accepts no free text, source, paths, targets, repo ids, prompts, arbitrary metadata, or raw errors; `arcgraph feedback summarize PATH` omits ids, timestamps, raw events, and the input path.",
                        "On POSIX, new local metrics/feedback files and newly created state directories use private modes; unsafe existing files are rejected without automatic chmod. Windows behavior does not claim POSIX modes as an ACL guarantee.",
                        "`visual serve` is loopback-only; non-loopback hosts are rejected so the workbench server is not exposed on the LAN by accident.",
                    ],
                },
                {
                    "title": "Path And Source Safety",
                    "items": [
                        "Importers and visual/read-side surfaces enforce path containment before turning external evidence into graph facts.",
                        "Source snippets are disabled by default in agent payloads, and compact surfaces apply snippet redaction plus raw-properties stripping.",
                        "Repo-outside paths, malformed artifacts, ambiguous route matches, and unsupported evidence records should stay visible as warnings or partial metrics instead of becoming graph edges.",
                        "For multiple projects, reuse one installed executable but use one stdio process and distinct repo/output/metrics/feedback paths per project. Every single-project server keeps `repo_id=default`; this is not a multi-tenant security claim.",
                    ],
                },
                {
                    "title": "Evidence Redaction",
                    "items": [
                        "Runtime, OpenTelemetry, HAR, and browser coverage importers keep `runtime-only` or coverage evidence advisory; they do not overwrite static confidence.",
                        "Sensitive headers, cookies, authorization values, query values, request/response bodies, JavaScript source text, and browser metadata are not persisted in compact edge evidence.",
                        "Use `arcgraph explain`, `arcgraph context`, compact relation queries, and `impact --compact` to audit imported evidence without exposing raw runtime artifacts.",
                    ],
                },
            ],
        }
    if topic == "release-checklist":
        return {
            "topic": topic,
            "title": "ArcGraph Release Checklist",
            "sections": [
                {
                    "title": "Required Local Gate",
                    "items": [
                        "Start from a clean working tree: `git status --porcelain=v1 --untracked-files=all` must print nothing, because the release gate and package smoke reject dirty source.",
                        'Export `ARCGRAPH_REQUIRE_TS=1` (Windows PowerShell: `$env:ARCGRAPH_REQUIRE_TS = "1"`) so TypeScript golden tests fail instead of skipping when Node or the compiler API is unavailable.',
                        "Run `npm ci` before the test suite on a fresh clone.",
                        "Upgrade the build tools with `python -m pip install --upgrade pip setuptools wheel`, then install the full environment with `python -m pip install -e '.[dev,mcp,security]'`, so the checks below run against the resolved dependencies.",
                        "Run `python -m pytest arcgraph/tests scripts/tests -q`.",
                        "Run `python -m black arcgraph scripts --check`.",
                        "Run `python -m ruff check arcgraph scripts`.",
                        "Run `python -m bandit -c pyproject.toml -r arcgraph scripts -x arcgraph/tests,scripts/tests` and require no issue.",
                        "Run `mkdir -p output/security`, freeze the environment with `python -m pip freeze --exclude-editable > output/security/python-audit-requirements.txt`, and run `python -m pip_audit --strict --disable-pip --no-deps --requirement output/security/python-audit-requirements.txt`; require no known vulnerability in the resolved Python environment. `--disable-pip` matters: without it pip-audit builds a temporary environment and silently leaves out a pinned package that environment already contains, and `--strict` does not report the omission, so confirm the report lists every line of the requirements file. This is the Python dependency audit step of the CI security job; the npm audit and Bandit items in this checklist are the local counterparts of two other steps of that job.",
                        "Run `npx --yes npm@11.12.1 audit --audit-level=high --json` and require no high-or-critical Node development dependency advisory; the exact npm CLI version avoids retired legacy audit endpoints.",
                        "Run `python -m build`.",
                        "Run `python scripts/arcgraph_package_readiness_smoke.py` to validate local wheel/sdist build, wheel install, installed CLI/docs/MCP help, installed trial-setup dry run, and sample repo smoke without publishing.",
                        "Run `python scripts/arcgraph.py build` to rebuild the self-index at the current commit.",
                        "Run `python scripts/arcgraph.py ci` and require 0 fail / 0 warn.",
                        "Run `python scripts/arcgraph_release_gate.py` to validate freshness, CI status, wheel buildability, and packaged release assets.",
                        "Run `python scripts/arcgraph_release_candidate_check.py --wheel dist/arcgraph-0.1.0-py3-none-any.whl --sdist dist/arcgraph-0.1.0.tar.gz --repo-root . --output output/arcgraph/reports/v0.1.0-smoke.json` to smoke the installed wheel before tagging or publishing. Its `wheel-source-provenance` check records the wheel SHA-256 and passes only when the wheel's embedded build provenance names the same commit and tree as `--repo-root` HEAD and records a clean build working tree, so neither a stale `dist/` artifact from an earlier commit nor a wheel built over uncommitted edits can be validated under the current version label. Its `clean-rebuild-identical` check then clones that HEAD into a fresh directory, runs an isolated `python -m build` there, and requires the rebuilt wheel and sdist to carry the same file names and identical bytes as the two files given, so an archive edited after the build (a changed dependency declaration, entry point or script) is rejected even when its RECORD was regenerated to match. The build backend is pinned to an exact version in `pyproject.toml` to reduce drift between the two builds; the pin covers only that direct requirement, so a change in a transitive build dependency or in the environment can still make the comparison fail, which is the safe direction, and the check records the Python, build frontend and backend versions it used. A side carrying no provenance at all, or no repository head to rebuild, is reported as `warn` rather than claimed as bound, and `--allow-source-mismatch` downgrades a real disagreement to a disclosed warning; report `status` is then `warn`, never `pass`, and the bundle assembler accepts only `pass`.",
                        "Maintainers can assemble a local external-trial bundle with `python scripts/arcgraph_external_trial_bundle.py`; run it with `--help` for its inputs. It publishes, tags and pushes nothing, and it requires saved evidence of a completed successful remote CI push run on `main` for the exact candidate commit.",
                        "Review `arcgraph docs schema-governance` before changing `SCHEMA_VERSION`, graph storage, resolver defaults, or evidence import schemas.",
                        "Review `arcgraph docs frontend-contract` before adding, removing, or changing a language frontend.",
                        "Review `arcgraph docs security-model` before changing evidence importers, visual serving, source snippet handling, or path containment rules.",
                    ],
                },
                {
                    "title": "Schema And Rebuild Governance",
                    "items": [
                        "Schema or resolver changes require a full `arcgraph build` before release; compatible source edits can use `arcgraph reindex --changed` during development.",
                        "Runtime trace, OpenTelemetry, HAR, and coverage imports are incremental evidence imports only when the current schema compatibility checks pass.",
                        "A release that changes schema policy must update schema compatibility tests, `arcgraph ci` schema readiness output, `arcgraph docs schema-governance`, and this checklist.",
                    ],
                },
                {
                    "title": "Frontend Governance",
                    "items": [
                        "Frontend changes must pass contract validation, retain stable default registry inventory, and include a golden matrix for any new language or protocol frontend.",
                        "Do not add automatic plugin discovery, third-party package entry point loading, remote plugins, or sandbox claims without a separate security model and release gate update.",
                    ],
                },
                {
                    "title": "Optional Maintainer Baselines",
                    "items": [
                        "Run `arcgraph benchmark suite --iterations 5 --warmups 1 --output output/arcgraph/reports/benchmark-suite.json` to capture local CLI and query latency before changing MCP/server strategy.",
                        "Run `arcgraph visual smoke --output-dir output/arcgraph/reports/visual-smoke` when workbench templates or visual assets changed and Playwright CLI is locally available.",
                        "Generated files under `output/arcgraph` and reports under `output/arcgraph/reports` are local artifacts and should not be committed.",
                    ],
                },
            ],
        }
    if topic == "schema-governance":
        return {
            "topic": topic,
            "title": "ArcGraph Schema And Rebuild Governance",
            "sections": [
                {
                    "title": "Stable Runtime Contract",
                    "items": [
                        "Index schema `1.0.0` is the current stable contract for graph storage, raw `QueryEngine` payloads, and release gate compatibility checks.",
                        "Bounded target-scoped CLI, native, and MCP payloads use read schema `1.4.0` as an independent contract; their `index_schema_version` remains `1.0.0`, so clients can negotiate response shape without mistaking it for a rebuild requirement.",
                        'Since read schema `1.1.0`, `warnings` is a heterogeneous JSON array: each element may be a string or a structured object. Consumers must branch on the element type instead of assuming string-only operations such as `"\\n".join(warnings)`; changing the permitted element shapes again requires a read-contract version change.',
                        "`arcgraph ci` always reports `schema_compatibility` with the current schema, stable target, legacy 0.6.0 upgrade route, and receiver-resolution default schema version.",
                        '`ContextResponse`, `ExplainResponse`, `RiskReport`, and `WhyReport` are canonical agent-facing schemas; new read-side fields require a read-contract version change and tests that preserve `extra="forbid"` validation.',
                    ],
                },
                {
                    "title": "When Full Rebuild Is Required",
                    "items": [
                        "Run a full `arcgraph build` after changing `SCHEMA_VERSION`, graph storage tables, node/edge serialization, source-root semantics, analyzer identity rules, or resolver defaults.",
                        "Direct 0.6.0 -> 1.0.0 upgrades use the recorded `full-rebuild` strategy because intermediate 0.7.0, 0.8.0, and 0.9.0 entries were readiness checkpoints, not independently published stable runtime schemas.",
                        "Release validation must use a fresh self-index at the current commit, then `arcgraph ci` and `scripts/arcgraph_release_gate.py`.",
                    ],
                },
                {
                    "title": "When Incremental Import Is Allowed",
                    "items": [
                        "Runtime trace, OpenTelemetry, HAR, and coverage evidence may be imported incrementally into a compatible current index; they remain advisory `runtime-only` or coverage evidence.",
                        "`arcgraph reindex --changed` is acceptable for compatible source edits during development, but release readiness still requires a fresh self-index at HEAD.",
                        "If compatibility checks fail or the fallback window changes, rebuild instead of importing evidence into an old index.",
                    ],
                },
                {
                    "title": "Required Test Updates For Schema Changes",
                    "items": [
                        "Update schema compatibility tests for `schema_compatibility_steps`, `schema_direct_compatibility_step`, and receiver-resolution default gates.",
                        "Update `arcgraph ci` schema readiness expectations so release gate consumers see the same migration policy.",
                        "Update `arcgraph docs schema-governance`, `arcgraph docs release-checklist`, and any golden fixtures affected by new node, edge, evidence, or confidence semantics.",
                    ],
                },
            ],
        }
    if topic == "frontend-contract":
        return {
            "topic": topic,
            "title": "ArcGraph Frontend Contract",
            "sections": [
                {
                    "title": "Current Inventory",
                    "items": [
                        "Default builds register the Python compatibility frontend (`python-v1-compat-shim`) and the TypeScript compiler API frontend (`typescript-static`) in deterministic order.",
                        "SCIP protocol graph ingestion (`scip-protocol`) is available only when the user passes `arcgraph build --scip-graph-index PATH`; it is not part of the default frontend registry.",
                        "`arcgraph build --scip-index PATH` remains precision evidence input and does not enable the SCIP protocol graph frontend.",
                    ],
                },
                {
                    "title": "Contract V1",
                    "items": [
                        "A frontend implements `LanguageFrontend` with `name`, `language_ids`, `version`, `file_extensions`, `capabilities()`, `detect()`, `accepts()`, and `analyze_to_graph()`.",
                        "`analyze_to_graph()` must return `FrontendGraphFragment`; its `nodes`, `edges`, and `warnings` must contain `Node`, `Edge`, and `BuildWarning` instances.",
                        "`phase_timings` must contain numeric seconds and `adapter_metrics` must be compact JSON-serializable data.",
                        "If `fragment.frontend` is empty, the indexer fills it from `frontend.capabilities()`; if present, it must match the registered frontend name and version.",
                        "Frontend output is validated before graph merge and fails loudly when the contract is violated.",
                    ],
                },
                {
                    "title": "Extension Boundary",
                    "items": [
                        "ArcGraph currently has no automatic plugin discovery, package entry point loading, plugin installation command, remote plugin execution, or plugin sandbox.",
                        "The built-in TypeScript frontend may read static local configuration facts such as `tsconfig` paths, package exports/imports with conservative static condition selection, repo-local workspace package manifests, literal Vite/Webpack aliases, and literal dynamic import specifiers, but it never executes project configuration, plugin code, package manager logic, or imported modules.",
                        "Frontends must use the existing confidence vocabulary: `confirmed`, `inferred`, `heuristic`, `runtime-only`, and `unresolved`; they must not invent public confidence values or payload fields.",
                        "A new frontend should include contract tests, a golden matrix, limitations docs, and release gate coverage before it is considered part of the supported surface; it should not claim complete language semantics without protocol or compiler evidence.",
                        "Protocol-first inputs such as SCIP, LSP, compiler APIs, or tree-sitter are preferred over hand-written deep analyzers for additional languages.",
                    ],
                },
            ],
        }
    if topic == "troubleshooting":
        return {
            "topic": topic,
            "title": "ArcGraph Troubleshooting",
            "sections": [
                {
                    "title": "Install Or CLI Startup",
                    "items": [
                        "If `arcgraph` is not found, activate the virtual environment that installed ArcGraph or run `python scripts/arcgraph.py <command>` from the source checkout.",
                        "If `python -m pip show arcgraph` points to an old wheel or another checkout, uninstall it and reinstall from the intended checkout with `python -m pip install -e .`.",
                        "If Python is outside 3.11 or 3.12, install Python 3.11 or 3.12, recreate the virtual environment, and reinstall ArcGraph.",
                        "TypeScript/JavaScript analysis requires Node.js and a resolvable TypeScript compiler API at runtime. The Python wheel includes the `.mjs` extractor but not `node_modules/typescript`; use the analyzed project's locked npm install when TS/JS is required.",
                        "Without that runtime, a build should succeed while recording `typescript_frontend_unavailable` in its `summary.json` and `diagnostics.jsonl`. This is acceptable only for the Python-only external-trial scope.",
                        "npm package publishing remains unapproved; installing a project-local compiler runtime does not turn npm into an ArcGraph distribution channel.",
                        "ArcGraph can be installed from PyPI; 0.1.0 is a beta release. Git tags (`v` followed by the version) and GitHub releases carry the same two files as the matching version on PyPI. npm and Docker/GHCR installation paths are not published; use a source checkout for development.",
                    ],
                },
                {
                    "title": "Stale Or Incompatible Index",
                    "items": [
                        "Run `arcgraph doctor` first when a new checkout does not build or query as expected.",
                        "Run `arcgraph current` first; if freshness is stale, run `arcgraph sync --if-stale`.",
                        "If schema versions differ, run `arcgraph build`; 0.5.x and incompatible future schemas require full rebuild.",
                        "If project config or source roots are missing, run `arcgraph init --dry-run` to preview the `[tool.arcgraph]` config and then `arcgraph init` only after reviewing it.",
                    ],
                },
                {
                    "title": "Partial Semantic Results",
                    "items": [
                        "When precision inputs are missing, `precision=ast_fallback_only` is expected and should be reported.",
                        "If `arcgraph precision scip-python` fails on Windows because tools are missing, run `scripts/install-arcgraph-precision-tools.ps1`; if Go is missing, install it with `winget install --id GoLang.Go -e` or pass `-InstallGoWithWinget`.",
                        "If local `scip-python`, `scip`, or `pyright-langserver` is unavailable and cannot be installed, use WSL/Linux/CI for evidence generation or run fast `arcgraph ci` while reporting `precision=ast_fallback_only`, `precision_partial`, or `type_precision_complete=false` explicitly.",
                        "Unresolved callsites are diagnostics, not silent drops; inspect `arcgraph unresolved TARGET`, filter with `--category`, or use `--release-blocking-only` before a python_full release.",
                        "Do not infer unresolved risk from a callback-like name or a test path. `true_dynamic_call` requires analyzer-recorded dynamic dispatch (including a visible runtime local binding), while ordinary unresolved test calls remain `static_candidate`.",
                        "Use `arcgraph report unresolved --output output/arcgraph/reports/unresolved-classification.md` to classify unresolved diagnostics without committing generated output.",
                        "Runtime traces are never imported implicitly; use `arcgraph trace run` and `arcgraph trace import` explicitly.",
                        "Coverage files are freshness-checked against covered source paths; stale coverage is reported as `coverage=partial` in `arcgraph current` and `coverage_inputs=warn` in CI.",
                        "Large `output/arcgraph/builds` directories are historical index snapshots; build and reindex automatically retain only recent successful builds after publishing `current.json`, and incomplete or unpublished builds are cleanup candidates. Run `arcgraph ops prune` first as a dry run when manual cleanup is needed, then rerun with `--apply` when the candidate list is expected.",
                    ],
                },
                {
                    "title": "Framework Adapter Gaps",
                    "items": [
                        "If expected Django routes, models, admin registrations, or Celery tasks are missing, first confirm the files are inside detected `source_roots` and not matched by `[tool.arcgraph] exclude`.",
                        "Django URL extraction is static and best-effort; dynamic URLConf mutation, settings-driven imports, or custom routers may need source review or future adapter support.",
                        "Celery extraction recognizes common `Celery()`, `@app.task`, `@shared_task`, `delay`, `apply_async`, and `send_task` patterns; highly dynamic task naming or dispatch wrappers may remain unresolved.",
                        "Run `arcgraph current` to inspect adapter metrics and `arcgraph unresolved TARGET` when a framework edge depends on unresolved Python callsites.",
                    ],
                },
            ],
        }
    return {
        "topic": topic,
        "title": "Schema Migration Notes",
        "sections": [
            {
                "title": "Read Contract 1.4.0",
                "items": [
                    "Bounded payloads now declare read schema `1.4.0`; index schema remains `1.0.0`. Update strict read clients before adopting this candidate; no storage migration is implied.",
                    "Explain adds separately bounded resource/queue relations and call_scope. Risk adds entrypoint_summary, entrypoint categories and diagnostic inclusion_scope/source_scope; callers keep their existing semantics.",
                    "The 32 KiB budget applies per explain relations section; the 64 KiB entrypoint_flow budget is unchanged. Neither is a global get_risk or explain payload budget.",
                ],
            },
            {
                "title": "Read Contract 1.3.0",
                "items": [
                    "Read contract 1.3.0 introduced its independently versioned bounded payloads; the underlying graph/index schema remains `1.0.0`, so this requires a client update rather than an index migration.",
                    "Read schema `1.3.0` adds ambiguity-safe `target_resolution`, structured `assurance`, and separate analysis and response-presentation truncation signals. Ambiguous candidates and unresolved suggestions are never auto-selected.",
                    "`arcgraph_find_similar` now reports `token_jaccard=` instead of `token_overlap=` and uses a monotone structural score. Call/resource agreement can raise the score; disagreement remains visible in `reasons` but no longer vetoes an otherwise identical structure.",
                    'Since read schema `1.1.0`, `warnings` is a heterogeneous JSON array: each element may be a string or a structured object. Consumers must branch on the element type instead of using string-only operations such as `"\\n".join(warnings)`.',
                ],
            },
            {
                "title": "CLI Errors Are Machine-Readable On stdout",
                "items": [
                    'A failing command other than `change` now writes a JSON error envelope to stdout: `{schema_version, command, status: "error", error_code, error: {code, message}}`. Before this, stdout was empty on failure while JSON is the default output mode, so a caller parsing stdout received nothing and had to recover the reason from prose on stderr.',
                    "Nothing a caller already reads has moved. The `ArcGraph: <message>` line on stderr is unchanged, and so are the exit codes: 2 is an expected application error such as a missing index or an invalid option, 1 is an internal defect, which also still prints its traceback to stderr. A caller that treats any non-zero exit as failure is unaffected; a caller that parsed stdout gains a document where it previously got a parse error on empty input.",
                    "`--human` and `--raw` deliberately keep stdout empty specifically for this envelope, not for every failure: `--human` asked for prose and receives the stderr line only, and `--raw` promises that stdout carries the query engine payload and nothing else, so a refusal there stays an exit code plus a stderr line rather than a second JSON shape in the same stream. Neither suppresses a command's own normal payload when it reports a nonzero status on its own (for example `arcgraph help --tool UNKNOWN_TOOL`), and neither applies to `change`, whose argument-parsing failures print a versioned JSON envelope unconditionally -- see `arcgraph docs agent-cli-contract` for the exact boundaries.",
                    "`change` commands are unchanged: they already emitted a structured payload, they exit 1 rather than 2, and their messages are redacted because those payloads are contract artifacts that get stored and handed on. The general envelope carries the same text as the stderr line, paths included, because it is local diagnostic output for the operator who ran the command.",
                    "Error codes are `ARCGRAPH_INPUT_NOT_FOUND`, `ARCGRAPH_SCHEMA_VERSION_UNSUPPORTED`, `ARCGRAPH_RUNTIME_ERROR`, and `ARCGRAPH_INTERNAL_ERROR`; a `ChangeSafetyError` surfacing outside a `change` command carries its own code. Treat the set as open: match on `status` first.",
                ],
            },
            {
                "title": "Supported Python Versions Narrowed",
                "items": [
                    "`requires-python` is now `>=3.11,<3.13`. It was `>=3.11`, which pip accepted on 3.13 and later on the strength of a declaration no CI lane exercised; the test and package matrices run 3.11 and 3.12 only. Installing on 3.13 or later now fails at resolution time with a clear message instead of succeeding into untested behavior.",
                    "The `Programming Language :: Python` classifiers already named 3.11 and 3.12 only, so this removes a disagreement between two declarations in the same file rather than dropping a version that was ever claimed consistently.",
                    "No supported interpreter loses support and no runtime behavior changes. A 3.13+ user who was relying on the wider bound should either stay on an installed copy or ask for the matrix to be widened; the declaration and the matrix are now checked against each other, so widening one without the other fails the suite.",
                ],
            },
            {
                "title": "CI Gate Configuration And Scope Reporting",
                "items": [
                    "`arcgraph ci` reads three new optional sections from the analyzed repository's `pyproject.toml`. `[[tool.arcgraph.ci.layers]]` entries, innermost first, each with a `name` and `paths` prefixes, replace the built-in model/repository/service/api layering for the `layer_violations` check. `[[tool.arcgraph.ci.layer_exemptions]]` entries name a `from` layer, a `to` layer, and the `reason` an inward import is accepted; an entry without a non-empty reason exempts nothing. `[tool.arcgraph.ci.semantic_resolution_baseline]` supplies `resolution_rate` for the `semantic_resolution_trend` check when `--semantic-baseline` is not passed.",
                    "Checks that inspected nothing no longer report that nothing was wrong. `layer_violations` reports `applicable`, `classified_edges`, `model_source`, and `exempted` entries with their reasons, and says when no layer model placed any file. `high_risk_changes_without_tests` and `high_risk_unresolved_callsites` report `scoped` and `target_count`, and say when no `--target` or `--changed-file` gave them a change to examine. All three still report `pass` in that state: a repository may legitimately have no layer model and a run may legitimately have no change scope.",
                    "`pr_summary.high_risk_unresolved` carries the same fields as the matching check details, so the two cannot disagree about whether a change was examined.",
                ],
            },
            {
                "title": "Removed Compatibility Import Paths",
                "items": [
                    "`arcgraph.core.indexer`, `arcgraph.core.frontends`, `arcgraph.core.python_frontend`, `arcgraph.core.reindexer`, and `arcgraph.core.typescript_frontend` are removed. They re-exported the pipeline modules and raised `DeprecationWarning` on import.",
                    "Import from `arcgraph.pipeline.indexer`, `arcgraph.pipeline.frontends`, `arcgraph.pipeline.contracts`, `arcgraph.pipeline.python_frontend`, `arcgraph.pipeline.reindexer`, and `arcgraph.pipeline.typescript_frontend` instead. The objects are the same ones the shims returned, so only the import line changes.",
                    "The shims shipped in the wheel, so this removes import paths an installed copy accepted. Nothing in this repository imported them outside their own deprecation test, and no package has been published from it, so no released artifact depended on them.",
                ],
            },
            {
                "title": "Diagnostic Lifecycle Identity",
                "items": [
                    "Unresolved-callsite diagnostics are now keyed for lifecycle purposes by their stable callsite subject and per-subject occurrence instead of path and start line. Moving a callsite without changing it no longer reports it as newly discovered.",
                    "`diagnostic_lifecycle.dedupe_key` now states that preference: the first available of `fact_id`, a stable callsite subject and occurrence, or path and start line. Consumers that matched the previous wording must update; the lifecycle counts themselves keep their meaning.",
                    "The lifecycle key is derived when diagnostics are read, not stored alongside them. An index whose diagnostics already carry a stable callsite subject therefore upgrades with no re-key, because the previous and the current side derive the same key.",
                    "An index built before stable callsite subjects were persisted in diagnostic properties carries none, so its diagnostics key by path and start line while current ones key by subject. Upgrading from such an index reports every unresolved diagnostic as `new` once and returns to zero `new` on the next build. Run `arcgraph build` once and confirm that the next build reports zero `new` before relying on the counts, rather than reading that single build as a regression.",
                    "`diagnostic.fact_id` is deliberately unchanged: it still means a row in `semantic_facts`, and Change Safety still reads it as the diagnostic identity subject. The stable subject is used only by the lifecycle key, so `stable_callsite_v1` identities and `DIAGNOSTIC_IDENTITY_PROFILE_VERSION` are unaffected.",
                    "A removal plus an addition inside one repeated callsite group can reproduce the same identity set exactly, so identity-set difference alone cannot detect that addition. Compare per-subject counts when judging newly introduced unresolved calls.",
                ],
            },
            {
                "title": "Semantic Quality CI Field Names",
                "items": [
                    "The `arcgraph ci` `semantic_quality_targets` details field `exception_budget_declared` is renamed to `exceptions_present`.",
                    "No declared-budget mechanism exists: the boolean only reports whether any metric is below its target. The sibling `exceptions` list already names each metric, its current value, and its target, so no accepted allowance can be read from it.",
                ],
            },
            {
                "title": "0.6.0 To 1.0.0 Readiness",
                "items": [
                    "0.6.0 -> 0.7.0: full rebuild for bindings, callsites, TypeRefs, and semantic metrics.",
                    "0.7.0 -> 0.8.0: full rebuild preferred when adding precision facts and refreshing source roots.",
                    "0.8.0 -> 0.9.0: runtime trace facts are additive and can be imported incrementally.",
                    "0.9.0 -> 1.0.0: compatibility check first; rebuild only when the check fails.",
                    "The 0.7.0, 0.8.0, and 0.9.0 entries are readiness checkpoints from the rollout plan, not separately published stable runtime schemas; direct 0.6.0 -> 1.0.0 upgrades should use a full rebuild.",
                    "Index schema `1.0.0` is the stable storage contract; old 0.6.x indexes should follow this route and rebuild before Python full CI. Bounded read payloads are versioned independently.",
                    "Stable CI requires `ci --python-full` 0 warn/0 fail, semantic quality targets pass, stale trace negative tests pass, type precision complete, and `release_blocking_count=0` in unresolved classification.",
                ],
            },
            {
                "title": "Bounded Relation Payloads Are Now The Default",
                "items": [
                    "`callers`, `callees`, and `impact` now return the bounded agent payload by default. Previously the default was the unbounded QueryEngine debug payload, which on the ArcGraph repository itself is about 3 MB for `callers` and 10 MB for `impact`.",
                    "`--compact` is still accepted on all three commands and is now a no-op. Existing agent scripts that pass it keep working and keep receiving the same payload.",
                    "Scripts that relied on the old unbounded default must add `--raw`. Only add it for human debugging: `--raw` has no size bound and is not intended for agent consumption.",
                    "`--raw` now rejects `--max-results`, `--detail-level`, and `--include-source` with exit code 2 instead of accepting and discarding them. Previously `callers TARGET --max-results 5` parsed successfully and still returned every caller in the graph.",
                    "Bounded payloads report what they dropped in `truncation`, including `reason`, the applied `context_limit`, and per-section `truncated_counts`. Check it before treating a result set as complete.",
                    "Target-scoped payloads now report only the capabilities that qualify the answer. A capability whose value is exactly `available` places no caveat on a result and is omitted; every other value (`basic`, `heuristic`, `ast-fallback`, `partial`, `unavailable`) is still reported.",
                    "This applies to all target-scoped commands: `bindings`, `callees`, `callers`, `callsites`, `context`, `explain`, `impact`, `imports`, `route`, `similar`, `symbol`, `tests`, `types`, `unresolved`, and `worker`. It also applies to the MCP `arcgraph_entrypoint_flow`, `arcgraph_explain`, `arcgraph_find_similar`, `arcgraph_get_context`, `arcgraph_get_risk`, `arcgraph_get_why`, and `arcgraph_record_learning` payloads. Index-level views (`current`, `status`, `stats`, `architecture`, `doctor`, `ci`) and the MCP `arcgraph_index_status` tool keep the complete table.",
                    "The new `capabilities_summary` field on those payloads states `reported`, `total`, and the single `omitted_value`, so an absent key can never be read as an unknown one. `arcgraph current`, `arcgraph status`, and the MCP `arcgraph_index_status` tool still return the complete capability table.",
                    "Those bounded payloads now declare read schema `1.1.0` and expose the underlying graph/index schema separately as `index_schema_version`. Raw `--raw` payloads remain index-schema `1.0.0` and are exempt from read-side shaping.",
                    '`warnings` changed from a string-only list to a heterogeneous JSON array in read schema `1.1.0`: each element may be a string or a structured object. Existing consumers must replace string-only operations such as `"\\n".join(warnings)` with element-type handling and use an object\'s `message`, `kind`, and optional `path` fields.',
                    "Warning scoping covers `context`, `explain`, `callers`, `callees`, `impact`, and the MCP risk payload. Those payloads no longer copy the whole index warning list. Warnings whose `path` is outside the files the answer covers are folded into one `index_warnings_omitted` entry carrying per-kind counts; path-less capability caveats are always kept.",
                    "Folding is skipped entirely when the payload cannot vouch for its own file set: no targets were requested, a requested target did not resolve, or the definition paths for the resolved targets could not be read. A file that fails to parse contributes no paths, so folding there would have hidden the `parse_error` explaining why the target is missing.",
                    "Only the definition-path failure adds a warning: when that read raises or comes back short, the payload emits `warning_scope_unavailable` and reports every index warning unfiltered. The no-target and unresolved-target cases stop folding too, but add no sentinel because nothing failed. Treat that sentinel as 'scope unknown, nothing was filtered' rather than as a normal warning: the index store may be unavailable, and no `index_warnings_omitted` entry will be present.",
                    "`--raw` is exempt from every payload transform, including capability scoping. `callers TARGET --raw` returns exactly what `QueryEngine.callers()` returns.",
                ],
            },
            {
                "title": "Fallback Window",
                "items": [
                    "The fallback window is closed at schema `1.0.0`.",
                    "Use `--semantic-call-resolution` only for compatibility with older scripts; it no longer changes the default resolver mode.",
                    "Use `--legacy-call-resolution` only as an explicit escape hatch; `ci --python-full` treats legacy receiver mode as a failure.",
                ],
            },
            {
                "title": "Local CLI Upgrade",
                "items": [
                    "Editable installs should be refreshed with `python -m pip install --upgrade -e .` from the ArcGraph project directory.",
                    "Use `command -v arcgraph` (Windows PowerShell: `Get-Command arcgraph`) and `python -m pip show arcgraph` to confirm which executable and package location are active.",
                    "A schema or resolver upgrade should be followed by `arcgraph build`; compatible source edits can use `arcgraph reindex --changed`.",
                ],
            },
        ],
    }


def _docs_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# {payload['title']}", ""]
    for section in payload["sections"]:
        lines.extend([f"## {section['title']}", ""])
        for item in section["items"]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
