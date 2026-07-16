| ID | Source | Baseline | LemonCrow (full runtime) | Caveman |
|----|------|------------------:|------------------:|------------------:|
| react-rerender | benchmarks | 865 | 265 | 322 |
| auth-middleware-fix | benchmarks | 4509 | 356 | 668 |
| postgres-pool | benchmarks | 1769 | 919 | 1114 |
| git-rebase-merge | benchmarks | 1021 | 490 | 824 |
| async-refactor | benchmarks | 603 | 135 | 434 |
| microservices-monolith | benchmarks | 1306 | 654 | 1136 |
| pr-security-review | benchmarks | 969 | 232 | 395 |
| docker-multi-stage | benchmarks | 1466 | 692 | 797 |
| race-condition-debug | benchmarks | 1194 | 351 | 732 |
| error-boundary | benchmarks | 2461 | 4177 | 1945 |
| eval-01 | evals | 695 | 240 | 415 |
| eval-02 | evals | 1189 | 517 | 340 |
| eval-03 | evals | 811 | 142 | 326 |
| eval-04 | evals | 1228 | 477 | 646 |
| eval-05 | evals | 952 | 288 | 495 |
| eval-06 | evals | 929 | 177 | 446 |
| eval-07 | evals | 961 | 332 | 490 |
| eval-08 | evals | 736 | 150 | 323 |
| eval-09 | evals | 733 | 302 | 417 |
| eval-10 | evals | 866 | 276 | 402 |
| **Average** |  | **1263** | **559** | **633** |

_LemonCrow (full runtime) vs baseline: mean 60%, median 67%, range -70%-92%, stdev 32pp across 20 prompts._
_Caveman vs baseline: mean 47%, median 48%, range 13%-85%, stdev 17pp across 20 prompts._