# Custom Domain Benchmarks

Atelier allows organizations to define their own internal benchmarks to evaluate agent performance on proprietary codebases or specific operational domains.

## Defining a Custom Benchmark

A custom benchmark is defined as a pack of tasks, initial states, and evaluation rubrics.

```yaml
# benchmark_custom.yaml
id: bench_acme_internal
tasks:
  - id: task_add_api_route
    prompt: "Add a new Fastapi route for /users/me"
    rubrics:
      - rubric_fastapi_auth_required
      - rubric_tests_included
  - id: task_db_migration
    prompt: "Add a column 'last_login' to the users table"
    rubrics:
      - rubric_alembic_down_revision_valid
```

## Execution

```bash
atelier benchmark run --suite ./benchmark_custom.yaml --agent custom-script.sh
```

## Evaluating Agent ROI
Custom benchmarks are critical for proving the ROI of AI agents to enterprise leadership. By using Atelier to track **Cost**, **Token Use**, and **Rubric Catches**, you can quantitatively demonstrate agent improvement over time.
