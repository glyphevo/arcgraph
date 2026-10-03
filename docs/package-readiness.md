# Package Readiness Gate

ArcGraph package readiness is a local evidence gate for a future human release
decision. It builds and installs local artifacts for source version `0.1.0rc8`,
but it does not publish anything.

Package readiness smoke for local wheel/sdist build checks that a wheel and
sdist can be built from a clean checkout, that the built wheel installs into a
fresh temporary virtual environment, and that the installed `arcgraph` command,
its built-in docs, the MCP server and a sample-repository workflow work without
the source checkout. `arcgraph docs package-readiness` is the authoritative,
current list of what it checks and what it does not approve.

## Run It

```bash
python scripts/arcgraph_package_readiness_smoke.py
```

The script fails before building when tracked, staged or untracked source state
is dirty. It emits JSON evidence, uses temporary directories, and cleans them
up on exit. Common variants:

```bash
python scripts/arcgraph_package_readiness_smoke.py --dry-run
python scripts/arcgraph_package_readiness_smoke.py --artifact-dir output/release/validated-artifacts
```

`--dry-run` reports dirty state and performs no build. `--artifact-dir`
atomically preserves the exact validated wheel and sdist in a new destination;
it refuses to overwrite an existing directory, and a destination inside the
source repository must be Git-ignored. Run the script with `--help` for the
remaining options.

## Boundaries

- Package readiness is evidence for a future human decision, not the decision
  itself.
- A dirty or source-mutating run is invalid evidence and fails closed.
- A single local run covers only the current host. The GitHub Actions
  `package-matrix` job runs it on Ubuntu, Windows and macOS; a passing run is
  evidence only for the commit it ran on.
- Built wheel, sdist, temporary virtual environments, generated indexes and
  smoke output must not be committed.
- Nothing here publishes packages, creates tags or releases, changes GitHub
  settings or repository visibility, or approves public or packaged MCP
  distribution. The complete non-approval list is in the built-in topic.
