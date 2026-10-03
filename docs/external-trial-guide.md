# ArcGraph 0.1.0rc8 Local Installation Guide

Install a supplied, verified wheel in a dedicated environment.
Local installation does not publish packages or upload repository content. This guide covers
Python analysis through the installed CLI and local stdio MCP. TypeScript and
JavaScript require a separately resolvable compiler runtime; the Python wheel
does not contain that runtime. Without it, `typescript_frontend_unavailable`
reports the degradation. Availability of a local artifact does not establish
publication on PyPI or validation on every platform.

## 1. Verify The Bundle

Work from the bundle directory. Verify every listed file before installation.

On macOS:

```bash
shasum -a 256 -c SHA256SUMS
```

On Linux with GNU coreutils:

```bash
sha256sum -c SHA256SUMS
```

On Windows PowerShell:

```powershell
Get-Content SHA256SUMS | ForEach-Object {
    $parts = $_ -split '  ', 2
    $actual = (Get-FileHash -Algorithm SHA256 $parts[1]).Hash.ToLower()
    if ($actual -ne $parts[0]) { throw "SHA-256 mismatch: $($parts[1])" }
}
```

Do not install the bundle if any checksum fails. `provenance.json` records the
exact source commit/tree and the wheel/sdist hashes. `remote-ci.json` records
the normalized, successful GitHub Actions `push` run on `main` for that same
commit and owner/repository coordinates, including the `CI Gate` result.
`package-readiness.json` and
`release-candidate-smoke.json` record the local installed-wheel gates;
the latter also records that the wheel and sdist in this bundle are byte for
byte what a clean, isolated build of that commit produces, with the tool
versions it used. These files are field-allowlisted portable evidence, not a
publication signature.
Host paths, command arguments, captured output, and temporary workspace details
are intentionally omitted from the distributed copies.

## 2. Install The Local Wheel Once

Create one dedicated tool virtual environment outside the repositories you will
analyze. Reuse this installed executable for every trial project; do not create
one ArcGraph installation per project. Uninstall and rollback then stay
isolated from project dependencies. Replace `BUNDLE_DIR` with the absolute
bundle path.

On macOS or Linux:

```bash
python3 -m venv .arcgraph-trial-venv
source .arcgraph-trial-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install 'BUNDLE_DIR/artifacts/arcgraph-0.1.0rc8-py3-none-any.whl[mcp]'
arcgraph --version
arcgraph version --json
```

On Windows PowerShell:

```powershell
python -m venv .arcgraph-trial-venv
.\.arcgraph-trial-venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install 'BUNDLE_DIR\artifacts\arcgraph-0.1.0rc8-py3-none-any.whl[mcp]'
arcgraph --version
arcgraph version --json
```

The version command must print exactly:

```text
arcgraph 0.1.0rc8
```

In the JSON result, require `status = "available"`,
`execution_mode = "installed_distribution"`,
`provenance_status = "verified_artifact_hash"`, and an
`artifact_provenance.sha256` equal to the wheel entry in `SHA256SUMS` and
`provenance.json`. The version text alone does not identify unique bytes. Do not
substitute an editable checkout for this step; your local session exercises the packaged
product shape.

## 3. Prepare Each Python Repository

Use non-sensitive Python repositories that you are authorized to analyze. Keep
ArcGraph's generated output out of commits. For each project, first ask the
installed wheel to produce a preview-only setup plan:

```bash
arcgraph --repo-root PROJECT_ROOT trial setup --client claude --dry-run
```

Review every check, absolute path, and the complete `registration_command`.
The `arcgraph_executable` check includes `selection_source`. `invocation` and
`distribution_record` bind the command to the running installation. A scheme
or interpreter-sibling fallback is reported as `warn`/`review`; manually verify
that path against the installed wheel before registration rather than treating
its existence as package identity.
Dry-run mode must not create `.arcgraph-trial` or change `.git/info/exclude`.
`claude_config_modified` is a fixed product invariant, not evidence that can
fail; do not use that field as the verification. The command prints a manual
registration command but has no Claude-configuration write path. If independent
evidence is required, compare the client configuration before and after the dry
run. If the plan is acceptable, the operator may run the same command with
`--apply-local-files`; that creates only the private local state paths and
`.git/info/exclude` entry, and still does not edit Claude configuration. On
Windows, `status = "review"` is the expected final machine result because setup
cannot enforce POSIX `0700`/`0600` modes or validate ACL and reparse-point
policy. Record any separate platform security review outside this payload;
rerunning setup does not convert that boundary to `ready`.

If setup is not used, prefer adding the following entry to
`.git/info/exclude`, which does not modify tracked project files:

```gitignore
/.arcgraph-trial/
```

Use the repository's tracked `.gitignore` instead only when its owner explicitly
wants the rule shared by the project.

Choose a distinct absolute index path for each project, for example
`PROJECT_ROOT/.arcgraph-trial/index`. From that repository root, inspect each
command's JSON separately:

```bash
arcgraph --repo-root PROJECT_ROOT --output-dir PROJECT_ROOT/.arcgraph-trial/index doctor
arcgraph --repo-root PROJECT_ROOT --output-dir PROJECT_ROOT/.arcgraph-trial/index init --dry-run
```

`doctor` can return exit code 0 with `status = "warn"`; shell chaining is not a
health gate. Review `status`, every check, and the proposed source roots. A
first trial can use detected roots without changing `pyproject.toml`. Run
`arcgraph init` only when the repository owner approves a persistent
`[tool.arcgraph]` configuration.

Then execute the minimal trial journey:

```bash
arcgraph --repo-root PROJECT_ROOT --output-dir PROJECT_ROOT/.arcgraph-trial/index build
arcgraph --repo-root PROJECT_ROOT --output-dir PROJECT_ROOT/.arcgraph-trial/index current
arcgraph --repo-root PROJECT_ROOT --output-dir PROJECT_ROOT/.arcgraph-trial/index status
arcgraph --repo-root PROJECT_ROOT --output-dir PROJECT_ROOT/.arcgraph-trial/index context YOUR_SYMBOL --detail-level summary
```

These are the explicit multi-project-safe forms of `arcgraph build`, `arcgraph
current`, `arcgraph status`, and `arcgraph context YOUR_SYMBOL`.

Choose a real Python symbol such as `package.module.function` for
`YOUR_SYMBOL`. Inspect `schema_version`, `index_schema_version`, `status`,
`freshness`, `warnings`, `truncation`, and `source_snippets` before relying on a
read payload. `warnings` may contain strings or structured objects.

## 4. Handle A Stale Index

After source changes, `current`, `status`, and bounded reads can report
`freshness.status = "stale"` and a structured `stale_index` warning. Use the
stale-aware lifecycle command:

```bash
arcgraph --repo-root PROJECT_ROOT --output-dir PROJECT_ROOT/.arcgraph-trial/index sync --if-stale
arcgraph --repo-root PROJECT_ROOT --output-dir PROJECT_ROOT/.arcgraph-trial/index current
```

This is the multi-project-safe form of `arcgraph sync --if-stale`; retaining the
explicit repository and output paths keeps the refresh bound to your local session
index. The command publishes a new index only when required and keeps the old
`current.json` usable if a build fails. A direct `reindex --changed` is an
advanced compatible-edit operation, not the default stale recovery path.

Run a full `arcgraph build` instead after schema, resolver, source-root, or
frontend-contract changes. Do not treat results from a stale or unknown
freshness state as current.

### Keep The Index Fresh During A Session

Running the refresh by hand after every edit is easy to forget, and a forgotten
refresh is not a silent one: every affected tool response then carries
`freshness.status = "stale"`. An Agent that correctly distrusts a stale answer
falls back to reading or grepping files, which measures ArcGraph's operation
rather than its analysis. The MCP server is read-only and never refreshes the
index for you, so for an editing session prefer keeping an explicit debounced
synchronizer running in its own terminal:

```bash
arcgraph --repo-root PROJECT_ROOT --output-dir PROJECT_ROOT/.arcgraph-trial/index watch
```

This is the multi-project-safe form of `arcgraph watch`. It polls the published
freshness, waits for edits to settle, and then runs the same
`sync --if-stale` publication documented above; it introduces no new index
behavior. Index publication is atomic -- a new build directory is written first
and `current.json` is replaced only when it is complete -- so a concurrent
read-only MCP call sees either the previous complete index or the new one, never
a partial build.

Build, reindex, import, and Change Safety operations each hold the exclusive
operation lock only for the duration of that one operation, so two of them
cannot interleave. The watcher holds no lock while it is merely polling, so
this does not make a running watcher block anything: a manual `build` started
between synchronizations simply runs. Only an overlap is resolved, and it is
resolved asymmetrically, which matters more than it first appears:

- If a synchronization is already publishing, the manual `build` is refused
  with `ArcGraphOperationInProgress` and the watcher keeps running.
- If the manual `build` holds the lock first, the watcher's next
  synchronization fails and **the watcher stops** with
  `stopped = "sync_failed"`. The build itself succeeds, so nothing looks
  wrong -- but the index is no longer being kept fresh.

Run at most one synchronizer per trial index, and stop it deliberately before
running any command that needs the lock rather than relying on the overlap
being resolved in your favour.

Treat it as a session helper, not a daemon, and record these boundaries:

- It stops on the first failed synchronization and reports
  `status = "partial"` with `stopped = "sync_failed"`. It does not retry. After
  it stops, the index silently goes stale again, so check that the process is
  still alive before trusting a long session's freshness.
- It requires an index to already exist. With no `current.json` it stops
  immediately with `stopped = "index_unavailable"` and names `arcgraph build`.
- It only performs `sync --if-stale`, so it does not cover the schema,
  resolver, source-root, or frontend-contract changes that require a full
  `arcgraph build`.
- It returns at most the last 20 events and discloses the omitted count.
- It also stops if its own stdout is closed, reporting
  `stopped = "stdout_closed"`, so a watcher whose output was piped into a
  command that exited is no longer refreshing anything.

Stop it with Ctrl+C when the session ends. The CLI installs its own
SIGINT/SIGTERM handlers, so a normal stop is reported as
`stopped = "signal:SIGINT"` (or `"signal:SIGTERM"`). The library-level
`stopped = "keyboard_interrupt"` value applies only to embedders that call
`watch_index` without installing those handlers.

## 5. Configure MCP Manually

This legacy trial workflow does not auto-configure clients. The separate explicit
`arcgraph setup --client` workflow is documented in [client-setup.md](client-setup.md).
Client configuration formats differ, so adapt this generic stdio process description
to the client you are evaluating:

```json
{
  "command": "/absolute/path/to/.arcgraph-trial-venv/bin/arcgraph",
  "args": [
    "mcp",
    "serve",
    "--name",
    "ArcGraph Project A",
    "--repo-root",
    "/absolute/path/to/project",
    "--output-dir",
    "/absolute/path/to/project/.arcgraph-trial/index",
    "--metrics-log",
    "/absolute/path/to/project/.arcgraph-trial/metrics/mcp.jsonl",
    "--feedback-log",
    "/absolute/path/to/project/.arcgraph-trial/feedback/agent.jsonl"
  ]
}
```

Use the one tool virtual environment's absolute `arcgraph` executable. Create
one client entry and one stdio process per project. Give each entry a distinct
server name, repository root, output directory, metrics log, and feedback log.
A single-repository server intentionally keeps the default repo id, `default`,
so MCP tools work when the client omits their optional `repo_id` argument; do
not add a project-specific `--repo-id`.

The metrics and feedback options are explicit local opt-ins. Omit either one
when it is not wanted. Their paths must be distinct; ArcGraph rejects a shared
file before either subsystem writes to it. Keep source snippets disabled; do
not add `--expose-source-snippets` for this trial. The accepted transport is
local stdio only. ArcGraph installs or configures no network listener,
telemetry exporter, remote destination, or Agent client.

## 6. Optional Local Metrics And Feedback

Metrics are off by default. To opt in, add an operator-selected local path:

```bash
arcgraph mcp serve \
  --repo-root /absolute/path/to/project \
  --output-dir /absolute/path/to/project/.arcgraph-trial/index \
  --metrics-log /absolute/path/to/project/.arcgraph-trial/metrics/mcp.jsonl
```

The MCP JSONL allowlist contains only timestamp, tool name, status, duration,
payload bytes, estimated tokens, truncation, and an enum-only freshness status.
It does not contain tool arguments, repository ids, paths, source, returned
payload text, raw exceptions, prompts, or client identity. Use a separate log
for each project/client pair. A write failure disables further metrics writes
and must not change a tool result.

ArcGraph configures no automatic or remote telemetry and installs no exporter.
The MCP SDK exposes OpenTelemetry hooks; a global provider configured by the
host is host behavior and should be reviewed separately.

Feedback is a separate opt-in log. Adding an absolute `--feedback-log` registers
`arcgraph_record_trial_feedback`. Omitting it means the tool is not registered
and no feedback file is created. The tool is not read-only: it appends one
idempotent local record. It is non-destructive, does not modify the repository
or index, and has no network access. Input is restricted to documented enums
and bounded identifiers; it rejects free text, paths, targets, repository ids,
source, prompts, arbitrary metadata, and raw errors.

New POSIX metrics/feedback logs and newly created state directories use private
modes. Existing unsafe files and direct log directories are rejected without
automatic permission changes, and one log file cannot be shared through hard
links under different project paths. POSIX state paths also reject symbolic
links in parent components; use a canonical physical path (for example, derive
the project root with `pwd -P`) rather than a symlinked project alias. On
Windows, ArcGraph uses safe local file primitives and writer serialization but
does not claim POSIX mode bits describe Windows ACLs or reparse-point policy.

### Feedback Machine Contract

Feedback schema `1.0.0` accepts a `tool_name` of 1–64 safe identifier
characters and at most 16 unique values from the published `warning_kinds`
allowlist. One encoded JSONL record, including its newline, is limited to 4,096
UTF-8 bytes; one feedback log is limited to 10 MiB. Start a new private log when
the file limit is reached. Do not truncate or rewrite an existing log.
Historical warning kinds already persisted under record schema `1.0.0` remain
readable even after the current input allowlist stops accepting them. A
breaking record-shape or schema-version change requires a separate log; do not
mix record schema versions.

Every other command follows the same principle. JSON is the default output
mode, and a command that fails writes an error envelope to stdout rather than
leaving it empty:

```json
{
  "schema_version": "1.0.0",
  "command": "stats",
  "status": "error",
  "error_code": "ARCGRAPH_INPUT_NOT_FOUND",
  "error": {"code": "ARCGRAPH_INPUT_NOT_FOUND", "message": "..."}
}
```

Branch on `status` first, then on `error_code`; treat the code set as open.
The one-line `ArcGraph: <message>` on stderr is still written, and the exit
code still distinguishes the two kinds of failure: `2` is an expected
application error such as a missing or stale index, `1` is an internal defect,
which also prints a traceback to stderr and is worth reporting as feedback.
`--human` and `--raw` deliberately leave stdout empty on failure: the first
asked for prose, and the second promises stdout carries the query payload and
nothing else.

Agents and automation should branch on these stable machine codes instead
of matching message text:

| Error code | Meaning and recovery |
| --- | --- |
| `TRIAL_FEEDBACK_INPUT_INVALID` | The request violates the strict schema, enum, identifier, uniqueness, or size contract. Correct it before retrying. |
| `TRIAL_FEEDBACK_ID_CONFLICT` | The `client_event_id` already belongs to different feedback. Reuse an id only for an identical retry; use a new id for a genuinely new event. |
| `TRIAL_FEEDBACK_STORAGE_UNAVAILABLE` | ArcGraph could not prove a safe, durable local append. Inspect the structured message and the runbook; retry the same event id after recovery because the bytes may already exist. |
| `TRIAL_FEEDBACK_LIMIT_EXCEEDED` | A record or the 10 MiB log limit was exceeded. Reduce the bounded fields or select a new private log as appropriate. |
| `TRIAL_FEEDBACK_DISABLED` | The MCP feedback store is not enabled for that server. Use the authorized CLI fallback or ask the operator to restart with `--feedback-log`. |
| `METRICS_LOG_INVALID_ENCODING` | A metrics summary found non-UTF-8 bytes. Preserve the old log for diagnosis and start a new private metrics log. |
| `METRICS_LOG_UNREADABLE` | A metrics summary could not safely read the selected path. Verify its canonical path and private-state policy before retrying. |

Feedback storage messages distinguish an unreadable/replaced directory chain
from a filesystem that cannot make directory entries durable. Permission
changes do not fix unsupported directory `fsync`; use a private path on a
different supporting filesystem. This guide describes the public feedback contract for v0.1.0rc8.

Raw CLI metrics use a separate, older local-sensitive event contract and may
contain command failures. Do not share raw CLI or MCP metrics. Use
`arcgraph metrics PATH --trial-summary`, review the aggregate, and share only
the fields required for your local session.

## 7. Read through your agent

Use MCP `list_tools` as the authoritative inventory and `arcgraph_help` for
on-demand usage guidance. See [Agent Reading Guide](agent-reading-guide.md).
CLI agents can use `arcgraph help`; `arcgraph --help` is syntax help and
`arcgraph docs` is the reference. CLI and MCP are not feature-equivalent.
Keep each project's index, metrics and feedback paths distinct.

## 8. Watch Disk Use

Each full build keeps a versioned build directory under the selected output
directory, which is `PROJECT_ROOT/.arcgraph-trial/index` in this guide.
Inspect the repository's disk use with platform-appropriate filesystem tools.
ArcGraph's prune command is a dry run unless `--apply` is supplied:

```bash
arcgraph --repo-root PROJECT_ROOT --output-dir PROJECT_ROOT/.arcgraph-trial/index ops prune --keep-builds 1
```

This is the explicit-path form of `arcgraph ops prune --keep-builds 1`.
Review every proposed deletion. Do not add `--apply` during your local session unless
the repository owner separately authorizes cleanup and any active change-plan
pins have been reconciled. Pinned builds are intentionally protected.

## 9. Uninstall Or Roll Back

The safest uninstall is to deactivate and remove the dedicated virtual
environment after retaining any feedback evidence you intend to share:

```bash
deactivate
```

Alternatively, while the environment is active:

```bash
python -m pip uninstall arcgraph
```

For rollback, create a new virtual environment and install the previously
verified wheel. Do not overwrite this bundle or reuse its version label for
different bytes.

## 10. Send Safe Feedback

The Agent should use `arcgraph_record_trial_feedback` when enabled if ArcGraph
is inaccurate, unavailable, stale, truncated, slow, hard to discover, missing a
needed capability, or causes a fallback. CLI-only workflows can record the same
bounded event with `arcgraph feedback record --feedback-log ABSOLUTE_PATH ...`.
Neither surface accepts a free-form explanation; keep any human narrative
separate and redact it before sharing.

Before sharing feedback:

- remove secrets, tokens, credentials, private URLs, source snippets, prompts,
  and proprietary identifiers;
- replace absolute user/repository paths with neutral placeholders;
- do not attach the graph database, raw source, metrics log, or unreviewed JSON
  payloads;
- include the ArcGraph version, operating system, Python version, command name,
  redacted status/error code, and whether the index was fresh;
- describe expected versus observed behavior in plain language.

When local MCP metrics were enabled, generate a privacy-bounded aggregate:

```bash
arcgraph metrics /absolute/private/path/project-client.jsonl --trial-summary
```

Review the aggregate before sharing it. Do not attach the raw metrics JSONL.
Token values are JSON-size estimates, not model-tokenizer measurements, and
percentiles must be interpreted together with each tool's sample count.

For a local visual aggregate, use:

```bash
arcgraph report metrics-html /absolute/private/path/project-client.jsonl \
  --output /absolute/private/path/project-client-metrics.html
```

The dashboard omits the input-log path, first/last event timestamps, raw events,
raw error text, and warning text; it reports only the warning count. It can
still contain aggregate command and semantic statistics, so review the
generated HTML before sharing it.

Generate the separate privacy-bounded feedback aggregate with:

```bash
arcgraph feedback summarize /absolute/private/path/project-feedback.jsonl
```

Review both aggregates locally. Share only the fields needed for your local session;
never share the raw feedback or metrics JSONL merely because it is local. The
aggregate JSON is the intended review/export surface; ArcGraph performs no
automatic export or upload.


The trial does not authorize uploading repository content to any service. Stop
and ask the repository owner if useful evidence cannot be shared safely.

## Private-state filesystem checks

Use a restrictive umask. ArcGraph rejects symbolic links and unsafe parent
paths. It checks a bounded ancestor chain through the first pre-existing
non-private ancestor; POSIX mode checks do not establish Windows ACL safety.
See [the runbook](runbook.md) for permission and directory fsync recovery.
The persisted read vocabulary is append-only within a record schema version.
