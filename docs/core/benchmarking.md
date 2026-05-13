# Core Benchmarking

Atelier benchmarks runtime efficiency against baseline host behavior.

## CLI

- `atelier benchmark core`
- `atelier benchmark runtime`
- `atelier benchmark hosts`

## Runtime metrics

- total tool calls
- avoided tool calls
- token savings
- retries prevented
- loops prevented
- successful rescues
- validation catches
- context reduction
- task success rate

## Routing cost-quality metrics

Routing quality is measured by accepted outcomes, not raw token savings.

- cost per accepted patch
- premium call rate
- cheap success rate
- escalation success rate
- routing regression rate

Interpretation guidance:

- Failed cheap attempts must still count toward cost and cheap success denominator.
- Premium recovery after cheap failure should improve escalation success while preserving failure cost visibility.
- Regression rate tracks routed attempts that reduced acceptance quality versus baseline expectations.

## Notes

`benchmark runtime` emits capability-focused efficiency metrics.
`benchmark core` runs prompt/task benchmark rounds.
`benchmark hosts` verifies host integration health.
