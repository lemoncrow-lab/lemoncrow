# Deployment Modes

Atelier supports multiple deployment shapes without changing the host contract.

## Local

- SQLite store under `.atelier/`
- `atelier ...`
- best for local experimentation and single-user host integrations

## Service

- FastAPI service for shared runtime access
- best for remote MCP mode and dashboard access

## Postgres Production

- set `ATELIER_STORAGE_BACKEND=postgres`
- set `ATELIER_DATABASE_URL`
- use workers for queued jobs and shared state

## Docker

- use `docker-compose.yml` in the repo root
- pair with `.env.production.example` for environment variables

## Hosted-ready

- keep clients on MCP or HTTP contracts
- use API key auth and pack validation
- keep pack deployment local/internal for reproducible production and customer installs
