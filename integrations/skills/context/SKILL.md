---
description: Show the environment context (rules, forbidden phrases, required validations) for a domain.
argument-hint: "<domain>"
---

Resolve the Atelier environment context for the given domain.

1. If `$1` is empty, ask the user which domain (e.g.
   `beseam.shopify.publish`, `beseam.pdp.schema`).
2. Call `context({ domain: "$1" })`.
3. Render:
   - **Environment**: `<id>` — `<title>`
   - **Domain match**: `<domain prefix>`
   - **Required validations**: bullets.
   - **Forbidden phrases**: bullets.
   - **Top procedures**: titles of attached ReasonBlocks (read-only;
     do not paste full bodies).

If no environment matches, say so plainly. Do not fabricate one.
