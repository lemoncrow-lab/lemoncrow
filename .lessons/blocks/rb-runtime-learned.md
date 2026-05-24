# Runtime Learned Recovery

- **id:** `rb-runtime-learned`
- **domain:** `Agent.shopify.publish`
- **status:** `active`

## Situation
When runtime traces show repeated publish failures

## Triggers
- publish
- shopify

## Dead ends
- Retry blindly without validating identifiers

## Procedure
1. Confirm Product GID
2. Run validation before retry
