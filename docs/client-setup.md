# Multi-client setup

Installed packages include a self-contained guide: `arcgraph docs client-setup`.

`arcgraph setup --client CLIENT` is an explicit local write operation. It builds
or refreshes the selected project index, checks it, and adds one client entry.
It is distinct from the legacy preview-first `trial setup` contract.
No PyPI publication is implied. Install the candidate wheel with `[mcp]` for
MCP clients; Pi's CLI skill does not require that extra. Use a persistent tool
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

## Installation availability

Until publication, use the validated local wheel. Once a version is actually
published, the intended persistent installation is `uv tool install --python
3.11 'arcgraph[mcp]==VERSION'`, followed by `arcgraph setup --client CLIENT` in
each project. The five adapters share the runtime, index logic and protocol;
this does not require five packages or five MCP implementations. Unattended
installation, client-specific model calls and cloud/remote MCP remain separate.

## Support Matrix

| Surface | Status | Notes |
| --- | --- | --- |
| CLI subprocess | Supported (alpha) | Start with `arcgraph help`; exact syntax remains in `arcgraph --help`. CLI has broader operational/query coverage than MCP. |
| MCP server | Supported (alpha) | Stdio only. Protocol `list_tools` discovers the registered surface; analysis/change/help tools are read-only, while optional feedback is a disclosed local append. |
| Automatic agent configuration via explicit setup | Source implementation | `setup --client` supports five client adapters; actual host/model acceptance is separate. |
| HTTP/network MCP transport | Deferred | Use local stdio transport only. |
| Local wheel candidate | External-trial path | The v0.1.0rc7 trial scope is Python analysis plus local stdio MCP; its sole optional write is the disclosed local feedback append. No public package publishing is implied. |
| Public package install | Deferred | PyPI/npm/Docker/GHCR publishing is not approved. |

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
