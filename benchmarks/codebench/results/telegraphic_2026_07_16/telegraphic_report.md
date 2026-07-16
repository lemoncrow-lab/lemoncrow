| ID | Source | Baseline | LemonCrow (full runtime) | Caveman |
|----|------|------------------:|------------------:|------------------:|
| react-rerender | benchmarks | 895 | 278 | 298 |
| auth-middleware-fix | benchmarks | 3025 | 261 | 1362 |
| postgres-pool | benchmarks | 1714 | 911 | 1068 |
| git-rebase-merge | benchmarks | 1063 | 484 | 605 |
| async-refactor | benchmarks | 507 | 131 | 467 |
| microservices-monolith | benchmarks | 1529 | 696 | 897 |
| pr-security-review | benchmarks | 1053 | 199 | 394 |
| docker-multi-stage | benchmarks | 1629 | 587 | 818 |
| race-condition-debug | benchmarks | 1118 | 384 | 526 |
| error-boundary | benchmarks | 2668 | 4567 | 2136 |
| eval-01 | evals | 775 | 315 | 320 |
| eval-02 | evals | 1326 | 542 | 296 |
| eval-03 | evals | 772 | 121 | 315 |
| eval-04 | evals | 1298 | 504 | 612 |
| eval-05 | evals | 963 | 321 | 508 |
| eval-06 | evals | 852 | 217 | 504 |
| eval-07 | evals | 967 | 358 | 455 |
| eval-08 | evals | 735 | 130 | 305 |
| eval-09 | evals | 736 | 290 | 371 |
| eval-10 | evals | 902 | 304 | 362 |
| **Average** |  | **1226** | **580** | **631** |

_LemonCrow (full runtime) vs baseline: mean 60%, median 65%, range -71%-91%, stdev 32pp across 20 prompts._
_Caveman vs baseline: mean 50%, median 53%, range 8%-78%, stdev 15pp across 20 prompts._