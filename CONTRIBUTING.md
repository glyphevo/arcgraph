# Contributing To ArcGraph

ArcGraph is an alpha-stage, local-first code semantic graph engine.
Contributions should preserve the current public-safe boundaries:
source-checkout install, local execution, no public package publishing claims,
and no public MCP distribution claims.

## Before Opening A Pull Request

- Keep changes focused and explain the user-facing behavior or evidence improved.
- Run the [local checks](#local-checks) before requesting review.
- On a clean committed branch, run
  `python scripts/arcgraph_release_gate.py` and
  `python scripts/arcgraph_package_readiness_smoke.py`; dirty source is rejected
  so artifact evidence cannot be confused with uncommitted work.
- GitHub Actions is currently disabled. Do not treat a missing remote run as a
  passing Ubuntu/Windows/macOS matrix.
- Do not commit generated output, `dist`, `node_modules`, caches, smoke outputs,
  credentials, private repository contents, or local process notes.
- Keep docs honest about current maturity. Describe ArcGraph as an alpha
  developer preview until its release status actually changes.

## Local Checks

These are the contributor-level part of the required local gate. Run them from
a source checkout. `arcgraph docs release-checklist` is the authoritative,
current list and also covers the steps only a release needs.

```bash
export ARCGRAPH_REQUIRE_TS=1
npm ci
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[dev,mcp,security]"
python -m pytest arcgraph/tests scripts/tests -q
python -m black arcgraph scripts --check
python -m ruff check arcgraph scripts
python -m bandit -c pyproject.toml -r arcgraph scripts -x arcgraph/tests,scripts/tests
mkdir -p output/security
python -m pip freeze --exclude-editable > output/security/python-audit-requirements.txt
python -m pip_audit --strict --disable-pip --no-deps --requirement output/security/python-audit-requirements.txt
npx --yes npm@11.12.1 audit --audit-level=high --json
python scripts/arcgraph.py build
python scripts/arcgraph.py ci
```

`ARCGRAPH_REQUIRE_TS=1` makes the TypeScript golden tests fail instead of
skipping when Node or the compiler API is unavailable. The commands are
POSIX-shell syntax; on Windows PowerShell set it with
`$env:ARCGRAPH_REQUIRE_TS = "1"` and translate `export`, `mkdir -p` and the `>`
redirect.

## Security

Do not open public issues for vulnerabilities or sensitive data exposure. Follow
the private reporting path in `SECURITY.md`.

## Code Of Conduct

Participation in this project is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
