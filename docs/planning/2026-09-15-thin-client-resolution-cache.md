# Thin-client resolution cache

Status: implemented on `feat/thin-client-resolution-cache`
Date: 2026-09-15
Depends on: server-index single-authority (`38d87f962`)

## Invariant

The LemonCrow server remains the only code-index authority. The thin client may
cache complete responses and server-acknowledged content digests, but it never
derives or persists symbols, embeddings, relationship graphs, rankings or any
other searchable code index.

## Response reuse

`result_reuse.v1` is an additive negotiated capability. Only deterministic,
revision-bound resolution tools (`code_search`, `read`, `relations`, `search`)
participate.

A successful response carries an opaque server HMAC. A repeat call sends that
validator back. The request still authenticates, authorizes, binds the exact
view revision, takes the normal server gate and emits a `tool.invoke` audit
record. If the HMAC matches, the server returns a tiny `reuse` envelope instead
of materializing the workspace or executing the index query; the client then
uses the payload it already holds. A server restart/load-balancer hop rotates
the secret and simply turns the request into a normal cache miss.

The cache key includes session, view, view revision, tool and canonical
arguments. An edit or sync advances the revision and old entries are pruned.
Dynamic/user-state tools are intentionally excluded.

## Budget

The response cache is memory-only and allocates nothing up front. Its automatic
eviction ceiling is 10% of currently available RAM, respecting tighter Linux
cgroup headroom, with an 8 MiB low-memory floor and a 4 GiB automatic ceiling.
`LEMONCROW_RESOLUTION_CACHE_BYTES` can override it (`0` disables; K/M/G suffixes
are accepted). A 16 GiB machine with 16 GiB actually available therefore gets
about a 1.6 GiB ceiling.

## Digest acknowledgement cache

The client also remembers up to 16,384 content digests acknowledged by the
server during the current authenticated session. Edit/revert cycles can then
skip `/missing` when reverting to content the server already possesses. The
cache is soft: normal blob-miss recovery remains authoritative.
