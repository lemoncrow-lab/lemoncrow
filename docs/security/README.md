# Security

Security rules that agents should treat as durable:

- Do not write secrets into repo files, host configs, or generated artifacts.
- Keep install scripts idempotent and free of credential writes.
- Prefer explicit auth and redaction boundaries over silent defaults.
- Verify shell execution paths with the existing security tests and checks.

## Reference surfaces

- `docs/production-readiness.md`
- `tests/gateway/test_security.py`
- `tests/core/test_redaction.py`
