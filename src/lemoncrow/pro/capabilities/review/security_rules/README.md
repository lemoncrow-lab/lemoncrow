# Default security rules for the pull request reviewer

These rules are what `security-scan: auto` runs when a repository brings none of its own. They are read by
[Opengrep](https://github.com/opengrep/opengrep), the open-source fork of the Semgrep engine, and their results become
`static_finding` evidence for the AI investigator (see `docs/setup/github-action.md`, "Security scan").

## Provenance and licence

Every rule here was written for LemonCrow and is licensed under Apache-2.0 like the rest of this repository
(SPDX-License-Identifier in each file). None of them is copied from, or adapted from, another rule set. That is on
purpose:

- The rules published in the Semgrep registry and the `semgrep-rules` repository are under the **Semgrep Rules
  License v1.0**, which does not allow redistributing them inside another product.
- The `opengrep-rules` repository is a fork of the older, LGPL-2.1 `semgrep-rules`, but its `LICENSE` adds the
  **Commons Clause** (no right to "Sell" a product whose value derives substantially from the software) and its
  README says the rules are for "research, testing & benchmarking". That does not allow shipping them in a product.

The pins of the Opengrep binary itself (LGPL-2.1, downloaded at run time, never vendored) are in
`pr_bot/opengrep_install.py`.

## What they cover

| File | Languages | Rules |
| --- | --- | --- |
| `python.yaml` | Python | command injection, `eval`/`exec`, SQL built from strings, pickle/marshal/YAML deserialization, weak hash and cipher, disabled TLS verification, SSRF and path traversal from request data (taint) |
| `javascript.yaml` | JavaScript, TypeScript | the same classes for Node |
| `go.yaml` | Go | the same classes for `net/http` and `database/sql` |
| `java.yaml` | Java | command injection, SQL built from strings, `ObjectInputStream`, weak digest and cipher |
| `secrets.yaml` | any text file | provider-format tokens, private keys, credential assignments |

Severity is chosen so the review bot's mapping stays conservative: `ERROR` is a P1 (should fix) until the AI
investigator confirms it, `WARNING` is a P2 (held back), and `metadata.confidence: LOW` lowers an `ERROR` by one
step. A rule's message never repeats the matched text, so a report never copies a secret.

Opengrep's default ignore list skips test directories, vendored and minified files; the secrets rules also exclude
tests, fixtures, examples and docs for the two noisiest patterns.

## Adding or changing a rule

1. Add the rule to the file of its language (or a new `*.yaml`). Use the id `lemoncrow.<language>.<name>`, a
   `message` that tells the author what to change, and `metadata` with `category: security`, `confidence` and `cwe`.
2. Add a `ruleid:` line it must report and an `ok:` line it must not, to the fixture with the same name in
   `tests/fixtures/opengrep/`.
3. Run the rule tests with the pinned Opengrep release:

   ```bash
   opengrep scan --test --config src/lemoncrow/pro/capabilities/review/security_rules tests/fixtures/opengrep
   ```

4. `tests/gateway/test_review_security_scan.py` checks the structure of every rule (id, severity, message, CWE) and
   that every rule has a positive and a negative fixture line. It does not run the binary.

A repository adds its own rules without editing this directory: list their paths under `security.rules` in
`.lemoncrow/review.yaml` on the default branch.
