| ID | Source | Baseline | LemonCrow (full runtime) | Caveman |
|----|------|------------------:|------------------:|------------------:|
| react-rerender | benchmarks | 865 | 248 | 322 |
| auth-middleware-fix | benchmarks | 4509 | 293 | 668 |
| postgres-pool | benchmarks | 1769 | 814 | 1114 |
| git-rebase-merge | benchmarks | 1021 | 383 | 824 |
| async-refactor | benchmarks | 603 | 136 | 434 |
| microservices-monolith | benchmarks | 1306 | 682 | 1136 |
| pr-security-review | benchmarks | 969 | 194 | 395 |
| docker-multi-stage | benchmarks | 1466 | 512 | 797 |
| race-condition-debug | benchmarks | 1194 | 350 | 732 |
| error-boundary | benchmarks | 2461 | 4382 | 1945 |
| eval-01 | evals | 695 | 229 | 415 |
| eval-02 | evals | 1189 | 501 | 340 |
| eval-03 | evals | 811 | 146 | 326 |
| eval-04 | evals | 1228 | 459 | 646 |
| eval-05 | evals | 952 | 272 | 495 |
| eval-06 | evals | 929 | 238 | 446 |
| eval-07 | evals | 961 | 315 | 490 |
| eval-08 | evals | 736 | 167 | 323 |
| eval-09 | evals | 733 | 274 | 417 |
| eval-10 | evals | 866 | 315 | 402 |
| **Average** |  | **1263** | **546** | **633** |

_LemonCrow (full runtime) vs baseline: mean 62%, median 67%, range -78%-94%, stdev 34pp across 20 prompts._
_Caveman vs baseline: mean 47%, median 48%, range 13%-85%, stdev 17pp across 20 prompts._