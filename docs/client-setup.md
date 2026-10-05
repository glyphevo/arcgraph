# Multi-client setup

Installed packages include a self-contained guide: `arcgraph docs client-setup`.

`arcgraph setup --client CLIENT` is an explicit local write operation. It builds
or refreshes the selected project index, checks it, and adds one client entry.
It is distinct from the legacy preview-first `trial setup` contract.
Install ArcGraph from PyPI with `[mcp]` for MCP clients (see Installation
availability); Pi's CLI skill does not require that extra. Use a persistent tool
installation or venv. A disposable `uvx` bootstrap is not yet an installation
manager: generated config binds the executable of the current installation.

From the target project directory, choose one:

```bash
arcgraph setup --client claude
arcgraph setup --client codex
arcgraph setup --client cursor
arcgraph setup --client hermes
arcgraph setup --client pi
```

Use `--dry-run` to inspect paths, the server entry and guidance without writes,
indexing or subprocesses. Global options precede the subcommand:

```bash
arcgraph --repo-root /path/to/project --output-dir /path/to/index setup --client codex --dry-run
```

The default index is `<project>/output/arcgraph`. The build/sync, MCP command and
CLI guidance all use the same absolute repo/output paths. Source-root detection
and project configuration are the ordinary build command's; no business tests,
services or repository code are executed for indexing. Review detected source
scope with `doctor` and configure it before setup if automatic detection is too
broad. Indexing can take time; progress is on stderr, the result is JSON stdout.

## Client matrix

| Client | Entry | Default destination | Remaining host action |
| --- | --- | --- | --- |
| Claude Code | Local stdio MCP | `<project>/.mcp.json` | Approve project MCP/trust and reconnect if required |
| Codex | Local stdio MCP | `<project>/.codex/config.toml` | Trust the project so project config is loaded; reconnect |
| Cursor editor | Local stdio MCP | `<project>/.cursor/mcp.json` | Open that project, approve/enable the server |
| Hermes Agent | Local stdio MCP | `$HERMES_HOME/config.yaml`, else `~/.hermes/config.yaml` | Start/reconnect in the chosen profile; choose the intended project's server |
| Pi | CLI skill | `<project>/.pi/skills/arcgraph/SKILL.md` | Trust project resources, reload, use `/skill:arcgraph` |

Each MCP server name includes a hash of the canonical project path. Hermes's
profile-level registration remains bound to that project even if a session
changes cwd. It does not magically follow the currently open folder. Select a
profile or explicit `--client-config PATH` for isolation; other clients also
accept an override for custom profiles. That override writes the specified file;
it does not configure the client to discover an arbitrary file automatically.
Pi expects the override to name the actual skill Markdown file.
Codex project trust must be recognized by the client's persisted configuration;
a temporary `-c` trust override alone did not load project MCP configuration in
the tested client. Setup does not write trust settings. Cursor may initially
show the discovered project server as disabled; enable it in Customize → MCPs.

Configuration edits preserve unrelated values. JSON is strict JSON (JSONC is
rejected rather than stripped); YAML uses safe loading with duplicate keys
rejected. JSON/YAML may be reformatted and YAML comments are not retained in the
new document. A private byte-for-byte backup is created beside every existing
file before an edit. TOML appends one table and preserves existing bytes;
unsupported inline-table layouts fail without changing the file. Conflicting
same-name server entries or existing different Pi skills are never overwritten.
Repeated identical setup is a no-op on config. Moving/upgrading an installation
may require manually reviewing and replacing its old entry; setup does not
silently redirect an existing entry to another executable.

Known symlink destinations/ancestors and concurrent config changes detected
before replacement are refused. This is not a hardened filesystem sandbox
against an actively racing adversary. Use canonical paths (on macOS `/tmp` and
`/var` aliases may need their resolved `/private/...` paths).
Project config/skill files may appear in git status; review them before sharing,
as their executable and index paths are machine-specific. Setup does not stage
files, alter credentials, edit client trust settings, start a model session,
install client applications, enable telemetry/feedback, or publish anything.

## What success means

- `status=planned`: dry-run validated a renderable plan, not a connection.
- `status=prepared`: index is fresh, an independent stdio probe found the exact
  default tool inventory and read index status (Pi: CLI help/current worked),
  and the client file was written or already matched.
- `client_connection_verified=false` and `model_call_verified=false` remain
  explicit: setup cannot prove an external application loaded the file or
  exposed tools to its model. It never bypasses that client's trust approval.
- Nonzero exit / `status=blocked`: no successful setup claim. A completed index
  build can remain after a later probe/config failure; setup is not an atomic
  transaction over indexing and client state. Fix the reported condition and
  rerun. An incompatible/corrupt existing index may need an explicit `build`.

Actual acceptance requires opening the client and asking it to call
`arcgraph_help`, then `arcgraph_index_status` for this project, and one targeted
`arcgraph_explain`. Pi should load the skill and run the corresponding CLI
commands. Record protocol success, client discovery and model use separately.
MCP reads never rebuild; after editing, use authorized CLI `sync --if-stale`
with the exact prefix returned by setup. Source search and tests remain required
for claims beyond indexed relationships.

## Verified hosts

Host verification keeps three layers apart: the protocol probe that `setup` runs
itself, client discovery (the host loaded the configuration), and model use (a
model in the host called the tools and reported values that only the tools can
return). Each host below was exercised once, on one macOS machine, in a
throwaway two-file Python project, by running the same prompt: call
`arcgraph_help`, call `arcgraph_index_status` and quote its `index_version` and
`commit_sha`, call `arcgraph_explain` on a function and list its callers.

| Host | Version observed | What was observed |
| --- | --- | --- |
| Claude Code (CLI) | 2.1.276 | The project `.mcp.json` entry was discovered, approved by the user and connected; the model called the three tools. |
| Cursor (desktop) | 3.23.12 | The server connected once the project was open; three MCP tool calls completed. |
| Codex (desktop) | core 0.159.2 (app version not recorded) | After the project was trusted in Codex (Codex recorded the trust entry itself), the project configuration loaded and three MCP tool calls ran. |
| Hermes Agent | CLI 0.21.5; desktop session (app version not recorded) | `hermes mcp test` connected and discovered 14 tools without a model; a desktop session then called the three tools. |
| Pi (CLI) | 0.85.1 | The project skill was loaded and the three CLI commands ran. The result is for one model: two other providers failed for reasons unrelated to ArcGraph. |

Limits: only macOS and only one session per host. The CI package matrix installs
the wheel and exercises MCP client handshakes on Ubuntu, Windows and macOS, but
no real host application was run on Windows or Linux. Larger or multi-project
setups, other client versions, and the exact wording of each host's approval or
enable prompts were not recorded.

What to expect, from those runs:

- Claude Code writes your approval to `.claude/settings.local.json` in the
  project. `claude mcp get NAME` reports `Pending approval` before you approve
  and `Connected` afterwards.
- Cursor connected as soon as the project was open in 3.23.12. Earlier guidance
  said the server may start disabled; if yours does, enable it in the MCP
  settings.
- Codex records the project trust in its own configuration
  (`trust_level = "trusted"`); `setup` does not write it.
- Hermes registers the server for the whole profile, so every Hermes session in
  that profile lists it. Remove it with `hermes mcp remove NAME`. Hermes may add
  its own default keys when it rewrites `config.yaml`. `hermes mcp test NAME`
  checks the connection and tool discovery without a model.
- `arcgraph_index_status` redacts host paths (`<redacted-user-path>`). Identify
  an index by its `commit_sha`, `index_version` and `repo_id`, not by a path.
- Any other client that can start a stdio MCP server can use the same entry:
  `command` is the installed `arcgraph` executable and `args` are `mcp serve
  --repo-root PROJECT --output-dir INDEX`. Only the five clients above have
  `setup` adapters; other clients were not verified.

## Installation availability

ArcGraph is published on PyPI, and [RELEASE_NOTES.md](../RELEASE_NOTES.md)
lists the published versions; 0.1.0 is a beta release. Install it into a persistent tool environment, for example `uv tool install --python
3.11 "arcgraph[mcp]"`, or with `python -m pip install "arcgraph[mcp]"` into a
dedicated virtual environment. Tested on macOS: with 0.1.0rc7, the
exact-version forms on Python 3.11 and the pip-style form on Python 3.12; with
0.1.0rc8, 0.1.0rc9 and 0.1.0rc10, pip installs of the exact version and of the
unpinned requirement on Python 3.11 and 3.12, and `uv tool install --python
3.11` of the exact version.
Installs of 0.1.0rc10 from PyPI were also tested on Windows and Linux (Ubuntu
24.04 under WSL) with Python 3.11 and 3.12. Pin the exact version you tested,
for example `arcgraph[mcp]==0.1.0`.
To install the development version from the repository instead, use
`"arcgraph[mcp] @ git+https://github.com/glyphevo/arcgraph.git"`. Then run `arcgraph setup --client CLIENT` in
each project. The five adapters share the runtime, index logic and protocol;
this does not require five packages or five MCP implementations. Unattended
installation, client-specific model calls and cloud/remote MCP remain separate.

## Support Matrix

| Surface | Status | Notes |
| --- | --- | --- |
| CLI subprocess | Supported (beta) | Start with `arcgraph help`; exact syntax remains in `arcgraph --help`. CLI has broader operational/query coverage than MCP. |
| MCP server | Supported (beta) | Stdio only. Protocol `list_tools` discovers the registered surface; analysis/change/help tools are read-only, while optional feedback is a disclosed local append. |
| Automatic agent configuration via explicit setup | Verified on one machine (see Verified hosts) | `setup --client` supports five client adapters; each was observed once in a real host on macOS with a throwaway project. |
| HTTP/network MCP transport | Deferred | Use local stdio transport only. |
| Local wheel candidate | External-trial path | The external-trial scope is Python analysis plus local stdio MCP; its sole optional write is the disclosed local feedback append. |
| PyPI install | Published | `RELEASE_NOTES.md` lists the versions on PyPI. The hosts above were observed with a 0.1.0rc7-era build from the GitHub repository (commit `cec768ab`). Later versions changed no MCP transport or tool definition, but fixed some tool internals (reading `current.json`, change previews); the hosts were not re-validated on them. |
| GitHub Release | Published | Each version published on PyPI has a Git tag and a GitHub release (a pre-release for 0.1.0rcN) carrying the same two files. |
| npm, Docker/GHCR | Deferred | Not published. |

CLI and MCP are not feature-equivalent: CLI agents use `arcgraph help`, while
MCP agents discover the registered surface through `list_tools` and call
`arcgraph_help` for usage guidance. Do not assume a task written for one
surface works unmodified on the other.

## Safety Checklist

- Run `arcgraph doctor` and `arcgraph build` before relying on MCP tools.
- Keep generated `output/arcgraph` artifacts out of commits.
- Keep one output, metrics, and feedback path per project; never point two
  project servers at the same local state.
- Keep source snippets disabled by default.
- Do not treat L2 protocol evidence as L3 language semantics.
- Do not perform public release, package publishing, tag, GitHub Release, or
  GitHub settings changes through an agent workflow.
