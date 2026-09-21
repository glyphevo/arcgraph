## Summary

- 

## Validation

- [ ] `python -m pytest arcgraph\tests scripts\tests -q`
- [ ] `python -m black arcgraph scripts --check`
- [ ] `python -m ruff check arcgraph scripts`
- [ ] `python scripts\arcgraph.py ci`

## Public-Safe Checklist

- [ ] No generated artifacts, `dist`, `node_modules`, caches, or smoke outputs
      are committed.
- [ ] No secrets, private repository contents, or local process notes are
      included.
- [ ] Documentation does not claim production readiness, stable APIs, public
      package publishing, or public MCP distribution unless separately approved.
