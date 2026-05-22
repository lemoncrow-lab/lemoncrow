# atelier/frontend — Reasoning Dashboard

Vite + React + TypeScript + Tailwind dashboard for the Atelier reasoning
runtime. Reads from `atelier-api` (FastAPI HTTP wrapper) on port 8787.

## Run (native, recommended)

The dashboard is part of the native stack. From the repo root:

```bash
make start          # brings up atelier-api + atelier-frontend
open http://localhost:3125
```

## Run (manual)

```bash
cd atelier/frontend
npm install         # or bun install
VITE_API_URL=http://localhost:8787 npm run dev
```

## Pages

- **Overview** — token / cost / savings / counts (estimates)
- **Plans** — plan-related validation results per trace
- **Traces** — full observable trace list + detail view
- **Failures** — failure clusters from `FailureAnalyzer`
- **Environments** — Beseam environments + linked rubrics
- **Reason Blocks** — reusable procedures (the "memory")

All numbers under "tokens" and "cost" are **estimates** computed from
observable trace content (4 chars ≈ 1 token, $/1K rate via
`ATELIER_USD_PER_1K_TOKENS`). They are not provider billing data.
