# Frontend

This doc is the live frontend guide for agent runs in this repository.

## Read before editing

- `frontend/src/api.ts`
- `docs/agent-os/validation-matrix.md`
- `docs/reliability/README.md`

## Current repo rules

- Verify API return types before using them.
- Do not assume raw arrays where the API returns wrapped response objects.
- Keep callback types explicit when TypeScript cannot infer them cleanly.

## Validation

```bash
cd frontend && npm run build
cd frontend && npm run test
```
