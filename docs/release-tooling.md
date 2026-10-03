# Release tooling

These tools validate local artifacts and assemble a bundle; they do not
publish packages or change repository settings. The source version is `0.1.0rc8`.
A successful run is evidence only for its recorded source and platform.

## Local Validation Gate

Before any release candidate or package publication, run the gate in
`arcgraph docs release-checklist`. It is a fixed, ordered command sequence that
starts from a clean working tree and is the authoritative list of commands, so
this page describes the tools rather than repeating them.

`arcgraph_release_gate.py` and `arcgraph_package_readiness_smoke.py` reject dirty
source state and tracked symlinks, then recheck that the source did not mutate
during artifact generation. Their evidence records the Git commit/tree and
artifact size/SHA-256 values.

`arcgraph_release_candidate_check.py` validates an already-built wheel and sdist
and does not provide those checkout provenance guarantees: it cannot show that
the checkout stayed clean while the files were produced. It binds the bytes it
validated in two ways, which the release checklist describes; `--help`
documents its options. Its `wheel-source-provenance` check compares the
provenance embedded in the wheel with the repository head. Its
`clean-rebuild-identical` check clones that head into a fresh directory,
builds it in isolation and requires the wheel and sdist it was given to equal
the rebuilt files byte for byte. The package-content checks list what an
archive may contain, so they cannot vouch for a field nobody listed, such as a
dependency declaration; the rebuild comparison is what covers those. It needs
the same Python, the `build` frontend and the package index, and it records the
versions it used. The build backend is pinned to an exact version in
`pyproject.toml`; changing that pin changes the built bytes and is a reviewed
change like any other. The pin covers only that direct requirement, so drift in
a transitive build dependency or the environment can still make the comparison
fail, which is the safe direction. The exact file paths must match the version being
released: more than one wheel can carry one version label, so the recorded
commit and SHA-256, not the filename, identify what was validated.

## Local Candidate Bundle

`scripts/arcgraph_external_trial_bundle.py` assembles a local external-trial
bundle: the validated wheel and sdist plus provenance and checksums. It hands
the persisted wheel and sdist to the candidate check and accepts only a `pass`
report that carries the clean-rebuild comparison for those exact files. It
publishes, tags and pushes nothing. It requires saved evidence of a completed
successful remote CI push run on `main` for the exact candidate commit. Run it
with `--help` for its inputs; recipients
verify a bundle with the steps in the
[External Trial Guide](external-trial-guide.md).

## Published Releases

0.1.0rc7 was uploaded to PyPI on 2026-10-02 by a maintainer, from the validated
candidate built from commit `653f20412237f53658b34cbde813f6060bee64fc`; the two
files on PyPI are byte-identical to that candidate (wheel sha256
`51ebc958d43aa21062bab639a15d6fb4bd7fad9a13df876815fbe9fb4f9b6ad2`, sdist
sha256 `7b2ab2bcc61df4dbb099d1aab885cb718fcaaf5841dfd55bb0a707f0adaa95bc`). The
same files were rehearsed on TestPyPI first. Compare any download with those
hashes, and use `arcgraph version --json` to see the commit an installed copy
was built from.

A published file cannot be replaced. PyPI never lets a file name be reused, even
after the file or project is deleted, and deletion is permanent. To withdraw a
release, a maintainer can yank it (pip then skips it unless the exact version is
pinned) and, to fix it, publish a new version. Documentation changes that land
on `main` after an upload do not change the uploaded files and reach PyPI only
with the next version.
