| ID | Source | Baseline | Atelier (full runtime) | Atelier (telegraphic only) | Caveman |
|----|------|------------------:|------------------:|------------------:|------------------:|
| react-rerender | benchmarks | 897 | 777 | 673 | 238 |
| auth-middleware-fix | benchmarks | 3840 | 4381 | 4279 | 2209 |
| postgres-pool | benchmarks | 1689 | 1519 | 1313 | 1657 |
| git-rebase-merge | benchmarks | 981 | 883 | 889 | 665 |
| async-refactor | benchmarks | 785 | 153 | 192 | 193 |
| microservices-monolith | benchmarks | 1508 | 1085 | 1254 | 1220 |
| pr-security-review | benchmarks | 1127 | 692 | 516 | 446 |
| docker-multi-stage | benchmarks | 1901 | 961 | 1592 | 753 |
| race-condition-debug | benchmarks | 955 | 810 | 850 | 803 |
| error-boundary | benchmarks | 2506 | 1219 | 3012 | 3819 |
| eval-01 | evals | 791 | 679 | 608 | 433 |
| eval-02 | evals | 1258 | 1040 | 997 | 196 |
| eval-03 | evals | 723 | 448 | 445 | 297 |
| eval-04 | evals | 1219 | 947 | 895 | 708 |
| eval-05 | evals | 848 | 775 | 804 | 525 |
| eval-06 | evals | 854 | 430 | 705 | 585 |
| eval-07 | evals | 1042 | 683 | 831 | 645 |
| eval-08 | evals | 662 | 355 | 435 | 285 |
| eval-09 | evals | 751 | 628 | 640 | 410 |
| eval-10 | evals | 1018 | 459 | 696 | 370 |
| **Average** |  | **1268** | **946** | **1081** | **823** |

_Atelier (full runtime) vs baseline: mean 29%, median 25%, range -14%-81%, stdev 21pp across 20 prompts._
_Atelier (telegraphic only) vs baseline: mean 22%, median 20%, range -20%-76%, stdev 20pp across 20 prompts._
_Caveman vs baseline: mean 42%, median 44%, range -52%-84%, stdev 30pp across 20 prompts._