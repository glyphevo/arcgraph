# ArcGraph Provenance

ArcGraph is a standalone code intelligence engine extracted from an earlier
TraceMind/CodeGraph code graph subsystem. The extraction preserved the ArcGraph
package, CLI, tests, release gate, and local indexing/query workflows while
removing host-application integration points.

ArcGraph is now independent from TraceMind at runtime. It must not require a
TraceMind service, route, frontend page, Docker build step, or application
wrapper in order to build an index or answer graph queries. TraceMind may use an
installed `arcgraph` CLI as an external developer tool, but ArcGraph itself does
not embed or depend on TraceMind.

The following legacy integration surfaces are outside the current ArcGraph
boundary and should not be restored in this repository:

- embedded `packages/arcgraph` host-application packages
- TraceMind-specific backend routes such as code graph API endpoints
- TraceMind-specific frontend code graph pages or stores
- TraceMind Docker copy steps or release gates
- pre-release TraceMind or CodeGraph command aliases

Current ArcGraph release readiness is based on the standalone package,
standalone CLI, local graph index, tests, CI checks, and release gate in this
repository.
