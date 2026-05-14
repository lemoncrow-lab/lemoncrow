# V2 to V3 Deprecation Matrix

| Area                | V2 behavior                                          | V3 behavior                                                                     | Operator action                                                  |
| ------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| Runtime embeddings  | `stub_embedding` could create 32-dimensional vectors | Runtime uses configured embedders and rejects legacy stub vectors on new writes | Run `atelier reembed` for legacy rows                            |
| Savings benchmark   | Fixed YAML lever constants used as proof             | 50-prompt replay harness is measurement source                                  | Run `make bench-savings-honest`                                  |
| Memory backend      | SQLite plus optional bridge or mirroring             | Exactly one primary backend, SQLite by default                                  | Set `[memory].backend` or `ATELIER_MEMORY_BACKEND` intentionally |
| Sleeptime summaries | Template grouping fallback                           | Ollama or Letta summarizer, otherwise unavailable                               | Install optional smart dependencies and Ollama if needed         |
| Tool output         | Large outputs could be passed through verbatim       | Compact output hook summarizes or samples oversized output                      | Use `compact` in host hooks                                      |
| Repo context        | Grep or manual reads                                 | Repo-map and code context through consolidated MCP tools                        | Use `search` with `mode="map"` or `code` with `op="context"`     |
| Memory updates      | Append-like behavior                                 | ADD, UPDATE, DELETE, and NOOP arbitration                                       | Inspect arbitration result in MCP response                       |
