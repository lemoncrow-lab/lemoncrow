| ID | Source | Baseline | LemonCrow (full runtime) | Caveman |
|----|------|------------------:|------------------:|------------------:|
| react-rerender | benchmarks | 873 | 254 | 268 |
| auth-middleware-fix | benchmarks | 1926 | 492 | 694 |
| postgres-pool | benchmarks | 1898 | 791 | 1044 |
| git-rebase-merge | benchmarks | 1044 | 452 | 675 |
| async-refactor | benchmarks | 553 | 133 | 393 |
| microservices-monolith | benchmarks | 1461 | 646 | 997 |
| pr-security-review | benchmarks | 1034 | 244 | 569 |
| docker-multi-stage | benchmarks | 1419 | 885 | 692 |
| race-condition-debug | benchmarks | 1277 | 369 | 641 |
| error-boundary | benchmarks | 2649 | 1246 | 2946 |
| eval-01 | evals | 779 | 305 | 364 |
| eval-02 | evals | 1340 | 434 | 246 |
| eval-03 | evals | 823 | 133 | 325 |
| eval-04 | evals | 1281 | 447 | 715 |
| eval-05 | evals | 963 | 266 | 477 |
| eval-06 | evals | 890 | 225 | 453 |
| eval-07 | evals | 884 | 326 | 545 |
| eval-08 | evals | 704 | 217 | 323 |
| eval-09 | evals | 780 | 182 | 384 |
| eval-10 | evals | 918 | 251 | 381 |
| **Average** |  | **1175** | **415** | **657** |

_LemonCrow (full runtime) vs baseline: mean 67%, median 70%, range 38%-84%, stdev 10pp across 20 prompts._
_Caveman vs baseline: mean 48%, median 50%, range -11%-82%, stdev 18pp across 20 prompts._