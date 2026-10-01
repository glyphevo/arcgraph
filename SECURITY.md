# Security Policy

ArcGraph is currently alpha-stage local-first infrastructure. Please report
security issues privately: use GitHub private vulnerability reporting (the
"Report a vulnerability" button on this repository's Security tab) or email
[security@glyphevo.com](mailto:security@glyphevo.com). Do not open a GitHub
issue for vulnerabilities or credential exposure.

Private vulnerability reporting, Dependabot alerts, and secret scanning with
push protection are enabled on this repository. Dependabot alerts notify the
maintainers when a declared dependency matches a published advisory. These are
notifications, not a certification, and they carry no response-time
commitment; the dependency audits in the release checklist remain the check run
before a release. This repository does not currently run a live Actions
security job. The published mailbox domain has mail exchange records; that is
not proof the inbox is monitored. If you do not receive an acknowledgement,
assume the report still needs a maintainer follow-up.

## Supported Versions

ArcGraph has not made a public package release yet. Security fixes are handled on
the main repository line until a formal version support policy is published.

## Reporting A Vulnerability

When reporting a vulnerability, include:

- affected commit or version
- operating system and Python version
- command or workflow used
- minimal reproduction steps
- whether the issue involves path traversal, evidence import, snippet exposure,
  generated artifacts, dependency metadata, or the local visual server

Do not include live secrets, private repository contents, production tokens, or
credentials in the report. Use redacted examples whenever possible.

## Security Boundaries

ArcGraph default workflows are local-first. The CLI reads local repository files
and writes generated artifacts under `output/arcgraph` unless configured
otherwise. The project has no automatic or remote telemetry path, hosted
service dependency, or background collector in default CLI workflows. The
optional MCP SDK exposes OpenTelemetry API hooks, but ArcGraph does not install
or configure a telemetry exporter or collector; if an operator or host process
configures a global OpenTelemetry provider, any resulting spans are host
behavior, not something ArcGraph produces or controls.

Operator-selected local MCP metrics (`--metrics-log`) and structured Agent
feedback logs (`--feedback-log`) are disabled by default, never uploaded
automatically, and must not contain source,
paths, targets, repository ids, prompts, credentials, or raw errors. The two
paths must be distinct; ArcGraph rejects a shared file before either
subsystem writes. Metrics freshness is recorded only as the fixed enum
`fresh`/`stale`/`unknown`/`not_reported`, never as a timestamp or a path.
ArcGraph never uploads either log or creates a remote issue; the reviewed
aggregate from `arcgraph metrics PATH --trial-summary` or `arcgraph feedback
summarize PATH` is the intended export surface, and ArcGraph performs no
automatic export.

The default MCP analysis/change/help tools are read-only. Supplying an absolute
`--feedback-log` conditionally adds one non-destructive local append tool. New
POSIX logs and newly created state directories use private modes (`0600`/`0700`);
unsafe existing files or final-component symlinks fail closed rather than being
silently repaired, and concurrent appends are serialized. POSIX local-state
traversal also rejects symbolic parent components so distinct configured
project paths cannot alias one log through a directory symlink. ArcGraph does
not claim POSIX mode bits describe Windows ACL or reparse-point guarantees, and
it does not provide encryption at rest.

For multiple local projects, reuse one installed executable but run one stdio
process per project with distinct repository root, output directory, metrics
log, and feedback log. Every single-project server keeps `repo_id=default`;
this is local single-user process/path isolation, not a multi-tenant security
claim.

Evidence and protocol inputs are explicit: coverage, runtime trace, OpenAPI,
SCIP, OpenTelemetry, HAR, browser coverage, and external semantic extractor
payloads must be supplied by the user or by local tooling. Importers and
read-side surfaces enforce path containment before turning external evidence
into graph facts, and external semantic extractor payloads are additionally
validated for schema, extractor identity, tier, supported node/edge kinds,
semantic edge evidence, and resolution strategy before merge. Missing
toolchains, invalid payloads, oversized output, or repo-outside paths should
surface as unavailable/degraded status or warnings, never as false language
support.

Compact agent and MCP payloads sanitize path-like fields (including copies
embedded in structured warning messages), redact host-user paths and
well-known secret patterns in free text, and do not expose source snippets
unless explicitly allowed. HAR and runtime-related importers avoid persisting
sensitive request headers, cookies, authorization values, query values,
bodies, and raw browser metadata in compact edge evidence.

The local visual server should be used with loopback hosts such as `127.0.0.1`;
`arcgraph visual serve` rejects non-loopback hosts so the workbench is not
exposed on the LAN by accident. It is a local read-only inspection surface, not
a hosted service, and should not be exposed as a public service without a
separate security review.

Once ArcGraph is installed, `arcgraph docs security-model` is the
item-by-item, offline-reachable contract for these boundaries; treat it as
authoritative over this file for exact current wording, not as an identical
copy of it.

## Automated Security Evidence

When GitHub Actions is enabled, the workflow audits the resolved Python project
dependencies and Node development dependencies, scans runtime Python source, and
uploads a CycloneDX JSON Python environment SBOM plus machine-readable audit
results. Those artifacts are review evidence, not a certification or a signed
release attestation.

GitHub Actions is currently disabled on this repository, so this
candidate has no current remote CI run or uploaded SBOM as live evidence.

The Bandit configuration excludes tests and globally skips five specific check
IDs, reviewed at the time they were added: B110 (best-effort metadata-parsing
fallbacks), B404 and B603 (subprocess use through fixed-family argument
arrays), B607 (local operator PATH lookup for git/node/tool discovery), and
B608 (SQL assembled only from allowlisted clauses or parameter placeholder
counts). A `skips` entry silences that check ID everywhere in the scanned
tree, not only at the call sites reviewed when it was added — a verified,
reproducible check: an unrelated, unreviewed `subprocess.run(...)` call
elsewhere in the tree reports 0 issues under this configuration where it
would otherwise report B404/B603. New code using any of these five patterns
therefore needs manual review; Bandit will not flag it. The security workflow
still fails on any other Bandit finding outside these five skipped IDs.

## Non-Goals

ArcGraph does not provide an enterprise SLA, managed security monitoring,
certified vulnerability detection, guaranteed detection, a hosted scanning
service, or an encryption-at-rest guarantee.
