| ID | Source | Baseline | Atelier (full runtime) | Caveman |
|----|------|------------------:|------------------:|------------------:|
| react-rerender | benchmarks | 967 | 473 | 348 |
| auth-middleware-fix | benchmarks | 3746 | 2908 | 3374 |
| postgres-pool | benchmarks | 1890 | 1138 | 1136 |
| git-rebase-merge | benchmarks | 1000 | 622 | 675 |
| async-refactor | benchmarks | 673 | 165 | 357 |
| microservices-monolith | benchmarks | 1575 | 963 | 962 |
| pr-security-review | benchmarks | 1129 | 364 | 605 |
| docker-multi-stage | benchmarks | 1561 | 845 | 703 |
| race-condition-debug | benchmarks | 1061 | 480 | 530 |
| error-boundary | benchmarks | 2327 | 1096 | 2035 |
| eval-01 | evals | 738 | 429 | 349 |
| eval-02 | evals | 1267 | 721 | 233 |
| eval-03 | evals | 868 | 369 | 269 |
| eval-04 | evals | 1285 | 713 | 693 |
| eval-05 | evals | 900 | 472 | 497 |
| eval-06 | evals | 871 | 309 | 470 |
| eval-07 | evals | 876 | 603 | 539 |
| eval-08 | evals | 752 | 291 | 332 |
| eval-09 | evals | 775 | 437 | 403 |
| eval-10 | evals | 909 | 484 | 344 |
| **Average** |  | **1258** | **694** | **743** |

_Atelier (full runtime) vs baseline: mean 48%, median 46%, range 22%-75%, stdev 12pp across 20 prompts._
_Caveman vs baseline: mean 47%, median 47%, range 10%-82%, stdev 16pp across 20 prompts._