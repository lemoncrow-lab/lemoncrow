# Compact Tool Output

`compact` reduces oversized tool output before it is inserted into model context.

## Behavior

- Under 500 tokens: pass through unchanged.
- 500 to 2000 tokens: deterministic head/tail sampling with metadata.
- Over 2000 tokens: use the internal Ollama summarizer when available, then fall back to deterministic sampling.

The response includes the compaction strategy, original token estimate, compacted token estimate, and a recovery hint that tells the host how to request the full output if needed.

## MCP Shape

```json
&#123;
  "text": "...raw output...",
  "source": "pytest"
&#125;
```

Hosts should call this tool before injecting long command, search, SQL, or test output into the prompt.
