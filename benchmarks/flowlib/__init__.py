"""Wire-level token-savings measurement for Claude Code.

Capture the real HTTP traffic between Claude Code and its model provider
(Amazon Bedrock or Anthropic-direct) with mitmproxy, then compare token usage
between two runs (e.g. LemonCrow MCP enabled vs disabled). Unlike LemonCrow's
internal counters, this reads the *actual* ``usage`` blocks billed by the
provider, so "savings" are ground truth rather than counterfactual estimates.

No Anthropic API key required: works with a Bedrock key or a Claude Pro/Max
subscription (token counts are present in responses regardless of auth mode).
"""
