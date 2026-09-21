# Example: Surgical Change Safety Workflow

This walks one plan from baseline capture to a verification verdict. ArcGraph
never edits code, runs a command supplied in evidence metadata, pushes Git state,
or changes remotes. You make the code change; ArcGraph records the contract and
checks what actually moved.

Every `arcgraph change` command requires an explicit `--repo-id`. There is no
implicit default repository identity.

## Preconditions

An executable baseline requires a clean working tree and a fresh index. Starting
from a dirty tree fails closed:

```json
{
  "error_code": "BASELINE_SOURCE_UNAVAILABLE",
  "error": { "message": "an executable baseline requires a clean working tree" },
  "status": "error"
}
```

Commit or stash first, then refresh the index:

```bash
git status --short
arcgraph build
```

On macOS/Linux:

```bash
git status --short
arcgraph build
```

## 1. Plan

Create and activate a plan from an explicit graph target. Targets may be a
symbol, path, route, resource, or contract:

```bash
arcgraph change --repo-id my-repo plan --task "Adjust the items route handler" --target symbol:my_pkg.api.ItemsHandler
```

The response captures a baseline, creates a durable pin, and returns a versioned
plan view with these fields:

| Field | Use |
| --- | --- |
| `data.plan_id` | Identifies the plan; pass it to later commands as `--plan-id`. |
| `data.plan_decision.plan_content_digest` | Binds decisions to one exact revision; pass it to later commands as `--plan-content-digest`. |
| `data.pin_projection.pin_id` | The durable build pin held for this plan. `plan` itself optionally accepts `--pin-id` to set it explicitly (it is auto-generated if you omit this); once the plan exists, you do not pass `--pin-id` to `approve`, `diff`, `evidence add`, or `verify` -- the system holds it internally for the life of the plan. |

Not every later command needs both `--plan-id` and `--plan-content-digest`
either; `show`, `list`, and `evidence list` accept a narrower set. Check each
command's own required options rather than assuming all three fields above
travel together.

The initial `data.plan_decision.status` is `pending`, and
`data.plan_revision.allowed_edit_scope` lists each in-scope symbol with its
baseline line range and source digests.

## 2. Approve

Approval requires the digest, so a decision cannot silently bind another
revision:

```bash
arcgraph change --repo-id my-repo approve --plan-id PLAN_ID --revision 1 --plan-content-digest DIGEST --actor alice --reason "scoped change approved"
```

`data.plan_decision.status` becomes `approved`. Use `reject` or `abandon` to stop
a revision without releasing its pin.

## 3. Make the change, reindex, then diff

Edit the code yourself, commit or stage it, and rebuild the index. Then compute
the graph delta for that exact revision:

```bash
arcgraph build
arcgraph change --repo-id my-repo diff --plan-id PLAN_ID --revision 1 --plan-content-digest DIGEST
```

`data` reports `changed_paths`, `changed_regions`, `identity_changes`,
`implementation_changes`, `evidence_changes`, and both the baseline and current
build identities.

## 4. Record evidence for every requirement

`verify` persists its result and closes the revision. **Run it exactly once, and
only after all evidence is recorded.** A `verify` call made before the evidence
is in place still returns a real `INSUFFICIENT_EVIDENCE` verdict, but it also
ends the revision: a later `evidence add` succeeds with exit `0` while the
following `verify` fails with `CHANGE_PLAN_STATE_INVALID`, leaving the revision
unverifiable and its pin still held. Record evidence first.

Each plan carries its own verification plan. Read the requirements from the plan
view rather than assuming there is exactly one:

```bash
arcgraph change --repo-id my-repo show --plan-id PLAN_ID
```

`data.plan_revision.verification_plan.requirements[]` lists each requirement. One
is generated per approved edit scope item, so an approvable `PLAN_READY` plan
always has at least one. **Zero requirements means the plan is blocked, not that
verification is free**: it indicates no target resolved to an allowed scope, for
example a `PLAN_BLOCKED_UNRESOLVED_TARGET` verdict. Stop and fix the planning
verdict rather than proceeding to `verify`.

For every entry, use its `requirement_id` and satisfy its
`minimum_attestation_level`:

```bash
arcgraph change --repo-id my-repo evidence add --plan-id PLAN_ID --revision 1 --plan-content-digest DIGEST --requirement-id REQUIREMENT_ID --result pass --attestation-level self_reported --producer-identity alice --artifact-digest ARTIFACT_SHA256
```

Repeat for each requirement. ArcGraph re-captures current code and build identity
and redacts sensitive metadata before publication; it does not execute the
command named in the evidence.

The command above covers `self_reported`. The two higher levels need more.

### `artifact_backed`

Submit the material with `--material-json`, and set `--artifact-digest` to its
**canonical** digest. ArcGraph hashes the canonical form of the value, with keys
sorted, no whitespace, and `ensure_ascii=False` — not the bytes of a JSON file as
you happen to have written it. Compute it the same way on any platform:

```bash
python -c "import hashlib,json;m=json.load(open('material.json',encoding='utf-8'));print(hashlib.sha256(json.dumps(m,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest())"
```

Hashing the file bytes directly matches only when the file already happens to be
in canonical form. Any indentation, trailing newline, or unsorted key makes the
two differ, and evidence is then rejected with
`EVIDENCE_ARTIFACT_DIGEST_MISMATCH`.

```bash
arcgraph change --repo-id my-repo evidence add --plan-id PLAN_ID --revision 1 --plan-content-digest DIGEST --requirement-id REQUIREMENT_ID --result pass --attestation-level artifact_backed --producer-identity local-runner --artifact-digest CANONICAL_DIGEST --material-json '{"passed":862,"skipped":1,"suite":"pytest"}'
```

### `trusted_runner`

Everything `artifact_backed` needs, plus `--runner-metadata-json` carrying a
non-empty `command`, plus an operator-controlled registry. The registry is passed
before the subcommand and must live **outside** the analyzed repository; ArcGraph
rejects a registry supplied from or resolving into the repo under review. With no
registry the default fails closed with `TRUSTED_RUNNER_UNAVAILABLE`.

A minimal registry:

```json
{
  "registry_version": "1.0",
  "runners": [
    {
      "runner_identity": "local-ci",
      "active": true,
      "allowed_repo_ids": ["my-repo"],
      "allowed_requirement_ids": ["REQUIREMENT_ID"],
      "allowed_commands": ["python -m pytest -q"]
    }
  ]
}
```

Every field is required and the entry must match on all four axes at once: its
`runner_identity` equals the evidence `--producer-identity`, `active` is `true`,
and the repo id, requirement id, and declared command each appear in the matching
allowlist. A single mismatch skips the entry, and if no entry matches the
evidence is rejected. An allowlist may hold `"*"` to accept any value, but list
the ids you actually intend to trust rather than widening a security boundary by
default.

Replace `PATH_OUTSIDE_REPO` with a path outside the analyzed repository:

```bash
arcgraph change --repo-id my-repo --trusted-runner-registry PATH_OUTSIDE_REPO evidence add --plan-id PLAN_ID --revision 1 --plan-content-digest DIGEST --requirement-id REQUIREMENT_ID --result pass --attestation-level trusted_runner --producer-identity local-ci --artifact-digest CANONICAL_DIGEST --material-json '{"passed":862,"skipped":1,"suite":"pytest"}' --runner-metadata-json '{"command":"python -m pytest -q"}'
```

Recording evidence above the required level is allowed; a `self_reported`
requirement accepts `artifact_backed` or `trusted_runner` evidence.

Confirm every requirement is covered before continuing:

```bash
arcgraph change --repo-id my-repo evidence list --plan-id PLAN_ID
```

## 5. Verify once

```bash
arcgraph change --repo-id my-repo verify --plan-id PLAN_ID --revision 1 --plan-content-digest DIGEST
```

Complete evidence does not by itself produce `SAFE_TO_PROCEED`. Evidence only
clears the evidence class of blockers, which sits lowest in the verdict
precedence. A revision whose changes left the approved scope, touched a
protected surface, ran against a stale index, or produced an incomplete or
unmapped graph delta still blocks with its own verdict even when every
requirement has passing evidence. Non-blocking findings return
`SAFE_WITH_KNOWN_RISKS`.

| Outcome | `status` | Exit |
| --- | --- | --- |
| Scope kept, no findings | `success`, `SAFE_TO_PROCEED` | `0` |
| Non-blocking findings only | `success`, `SAFE_WITH_KNOWN_RISKS` | `0` |
| Evidence missing or stale | `blocked`, `INSUFFICIENT_EVIDENCE` | `2` |
| Changes left the approved scope | `blocked`, `CHANGE_SCOPE_EXCEEDED` | `2` |
| Protected surface changed | `blocked`, `PROTECTED_SURFACE_CHANGED` | `2` |
| Index no longer matches source | `blocked`, `INDEX_STALE` | `2` |

The revision is closed in every one of these cases. Read `verdict` and
`findings` to see which blocker applied.

## 6. Archive

Only `archive` of a verified, failed, or abandoned revision releases the pin, and
only after the archive record is durable:

```bash
arcgraph change --repo-id my-repo archive --plan-id PLAN_ID --revision 1 --plan-content-digest DIGEST --actor alice --reason "change verified and closed"
```

## Exit codes

Change commands emit versioned JSON and use exit codes as a contract:

| Exit | Meaning |
| --- | --- |
| `0` | Non-blocking result. |
| `1` | Input, store, security, or runtime error; read `error_code`. |
| `2` | A valid blocked planning or verification verdict; read `verdict`. |

A blocked verdict is a real answer, not a crash. Agents should branch on the
exit code and then read `verdict` or `error_code`, never parse the message text.

## Expected result

- `plan` returns `status: success` with a pin and a pending decision.
- `approve` moves the decision to `approved`.
- `diff` returns `status: success` with a populated delta.
- `verify`, run once after every requirement has evidence, returns
  `status: success` at exit `0` when the change also stayed inside the approved
  scope: `SAFE_TO_PROCEED` with no findings, or `SAFE_WITH_KNOWN_RISKS` when
  only non-blocking findings remain. Scope, protected-surface, index-staleness,
  and incomplete-delta blockers still return `blocked` at exit `2` regardless of
  how complete the evidence is.
- All records are written under `output/arcgraph/change-safety/` and are local
  artifacts that should not be committed.
- Until a revision is archived it keeps its pin, which retains the pinned build.
  Archive finished revisions so old builds can be reclaimed.

## MCP scope

`arcgraph mcp serve` exposes `arcgraph_preview_change_plan`,
`arcgraph_get_change_plan`, `arcgraph_list_change_plans`,
`arcgraph_get_graph_delta`, and `arcgraph_verify_change`. These read, preview,
and compute only. Approval, reject, abandon, archive, evidence persistence and
purge, and audit export remain CLI-only. MCP verify computes without persisting a
report or lifecycle transition.

## Troubleshooting

- `BASELINE_SOURCE_UNAVAILABLE`: the working tree is dirty, or the baseline Git
  object is unavailable. Commit or stash, then rebuild.
- `CURRENT_CODE_IDENTITY_MISMATCH` or `CURRENT_BUILD_STALE`: the index no longer
  matches the source. Run `arcgraph build` before retrying.
- `CHANGE_PLAN_STATE_INVALID`: the revision is not in a state that allows this
  transition. The common cause is a `verify` that already ran, including a
  `verify` attempted before the evidence was recorded. The revision cannot be
  reverified; plan a new revision. Read the current state with
  `arcgraph change --repo-id my-repo show --plan-id PLAN_ID`.
- `TRUSTED_RUNNER_UNAVAILABLE`: trusted-runner evidence needs an
  operator-controlled registry outside the analyzed repository. The default has
  no trusted runners and fails closed.

For the full contract, run `arcgraph docs change-safety`.
