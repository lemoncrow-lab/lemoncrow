# Licensing

The LemonCrow **local/open-source product** is published under the Apache
License, Version 2.0. The local runtime, CLI, MCP server, SDK, host integrations,
and engine (including the `lemoncrow.pro` package) are readable source under the
same license.

LemonCrow Enterprise and hosted service implementation are separate proprietary
components in the private development repository. They are not required to
build or run the complete single-developer local product.

- Full license text: [`/LICENSE`](../../LICENSE) and [`/LICENSE-APACHE`](../../LICENSE-APACHE)
- Attribution and third-party notices: [`/NOTICE`](../../NOTICE)

## No local account or entitlement gate

Every local capability is available without an account. The local engine has no
license check, entitlement lookup, usage/savings cap, or local plan tier. Hosted
and Enterprise services have their own server-side identity and commercial
policy, separate from local capability access.

Local LemonCrow has no account or login flow. When the CLI is configured for a
hosted LemonCrow server, `lc auth login|status|logout` manages that remote session;
it does not unlock or change any local capability.

## Optional performance build

The engine ships as readable Python source. Compiling it with mypyc is an
**optional performance build** — never required to run LemonCrow, and it changes
no behavior and unlocks no features.

## Network behavior

Indexing, search, edits, and memory all stay on your machine. Anonymous remote
telemetry is on by default — turn it off with `lc telemetry remote off`,
`DO_NOT_TRACK=1`, or `LEMONCROW_TELEMETRY=off`. For the full details
of what does and does not leave your machine, see
[Privacy & network behavior](../setup/privacy.md).
