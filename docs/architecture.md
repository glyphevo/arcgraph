# Architecture and contributor boundaries

ArcGraph separates extraction, graph storage, queries and interfaces. Frontends
and framework adapters contribute evidence; missing or ambiguous evidence must
not become guessed call edges. Query results disclose freshness, resolution,
confidence and truncation. An empty graph result does not establish runtime
absence.

## Dependency direction

The declared layer order in `pyproject.toml` is core, analyzers, adapters,
pipeline, change, providers, interfaces. Imports normally point inward.
Interfaces may depend on Change Safety; Change Safety may use GraphStoreReader,
QueryEngine, cleanup, operation_lock and evidence_manifest. QueryEngine must
not depend on Change Safety. GraphStore semantic tables must not depend on
Plan, Decision or Report records.

The declared core/cleanup.py exemption reads the Change Safety contract version
to validate pinned-build records before deletion. Unknown pin formats must fail
closed. Keep this exception explicit in the layer model; it is not permission
for other inward dependencies.

## Change Safety

Plans, approval decisions and verification reports have separate identities.
Index builds are immutable snapshots selected through atomic pointers. Active
plans pin their baseline builds; cleanup must respect those pins. Verification
uses source identity and evidence provenance, not a green graph alone.

Use `arcgraph docs change-safety` for CLI/MCP contracts and
[the worked example](change-safety.md) for evidence ordering,
attestation, verdicts and audit export. The implementation models and regression
tests define serialized fields; no development plan is required to use the API.
