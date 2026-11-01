# Proof: reproduce findings and run the new tests on base

A review finding is a claim. `lc review proof` tries to settle it by running code. For each open P0 or P1 finding in the LemonCrow summary on the head commit, it asks the model for one minimal failing test that would demonstrate the bug, written for the repository's own test framework. It runs that test on the head commit and on the base commit. Separately, it runs the pull request's own added or changed tests on the base commit ("red-on-base"), which shows whether those tests would catch the change being undone.

The results are posted as a `LemonCrow Proof` check run on the head commit, with one reply on each finding's inline thread. Early access, Linux runners only.

## Add it to the review workflow

Add these three jobs to the workflow from [GitHub Action](github-action.md), next to its `review` job, replacing `<sha>` with the same release commit the review pins. Each job sets its own `permissions:`, which replace the workflow's for that job.

```yaml
  proof-plan:
    # Holds the model key. Runs no pull request code: it reads files and asks the model.
    needs: review
    runs-on: ubuntu-latest
    timeout-minutes: 15
    permissions:
      contents: read
      pull-requests: read   # the LemonCrow summary and its hidden state
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: lemoncrow-lab/lemoncrow
          ref: <sha>  # v0.8.1, the same release commit as the review job
          path: .lemoncrow-cli
          persist-credentials: false
      - uses: astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86  # v5.4.2
      - run: uv sync --frozen --project .lemoncrow-cli
      - name: Plan one reproduction per open P0/P1 finding
        env:
          GH_TOKEN: ${{ github.token }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          LEMONCROW_REVIEW_MAX_USD: "3"  # the review's max-usd-per-pr; proof spends from what is left of it
        run: >-
          uv run --project .lemoncrow-cli lc review proof plan
          --pr ${{ github.event.pull_request.number }} --out lemoncrow-proof-plan.json
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with:
          name: lemoncrow-proof-plan
          path: lemoncrow-proof-plan.json

  proof-run:
    # Runs the pull request's code. No secrets, read-only token, nothing posted from here.
    needs: proof-plan
    runs-on: ubuntu-latest
    timeout-minutes: 30
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: lemoncrow-lab/lemoncrow
          ref: <sha>  # v0.8.1
          path: .lemoncrow-cli
          persist-credentials: false
      - uses: astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86  # v5.4.2
      - run: uv sync --frozen --project .lemoncrow-cli
      - name: Bubblewrap (the sandbox)
        run: |
          sudo apt-get install -y bubblewrap
          # Ubuntu 24.04 blocks unprivileged user namespaces by default; bubblewrap needs them.
          sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
      # Your project's toolchain and test dependencies. Install the dependencies, not the
      # project itself: an editable install would make the base run import the head's code.
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5
        with:
          python-version: "3.12"
      - run: |
          python -m venv "$RUNNER_TEMP/venv"
          "$RUNNER_TEMP/venv/bin/pip" install -r requirements-dev.txt
          echo "$RUNNER_TEMP/venv/bin" >> "$GITHUB_PATH"
      - uses: actions/download-artifact@37930b1c2abaa49bbe596cd826c3c89aef350131 # v7.0.0
        with:
          name: lemoncrow-proof-plan
      - name: Run the reproductions and red-on-base
        run: >-
          uv run --project .lemoncrow-cli lc review proof run
          --plan lemoncrow-proof-plan.json --sandbox auto
          --toolchain "$RUNNER_TEMP/venv" --toolchain /opt/hostedtoolcache
          --out lemoncrow-proof-results.json
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        if: always()
        with:
          name: lemoncrow-proof-results
          path: lemoncrow-proof-results.json
          if-no-files-found: ignore

  proof-report:
    # Posts the check run and the thread replies. Runs no pull request code.
    needs: [proof-plan, proof-run]
    if: always() && needs.proof-plan.result == 'success'
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      checks: write          # the "LemonCrow Proof" check run
      pull-requests: write   # one reply per finding thread
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: lemoncrow-lab/lemoncrow
          ref: <sha>  # v0.8.1
          path: .lemoncrow-cli
          persist-credentials: false
      - uses: astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86  # v5.4.2
      - run: uv sync --frozen --project .lemoncrow-cli
      - uses: actions/download-artifact@37930b1c2abaa49bbe596cd826c3c89aef350131 # v7.0.0
        with:
          pattern: lemoncrow-proof-*
          merge-multiple: true
      - name: Post LemonCrow Proof
        env:
          GH_TOKEN: ${{ github.token }}
        run: >-
          uv run --project .lemoncrow-cli lc review proof report
          --plan lemoncrow-proof-plan.json --results lemoncrow-proof-results.json
```

The `proof-run` example is for a Python project. For another stack, replace the setup-python and venv steps with your own toolchain and dependency install, and pass each directory the tests need: `--toolchain DIR` for directories outside the checkout, such as an interpreter, an SDK or a venv, and `--deps node_modules` for dependency directories inside it. If your key is an OpenAI key, set `OPENAI_API_KEY` in `proof-plan` instead.

## Why three jobs

The pull request's code is untrusted. A test file, a `conftest.py` or a package `postinstall` script is code the author chose, and it runs as soon as tests run. So that code never runs in a process that holds the model key or a token that can write:

- **`proof-plan`** holds the model key and a read-only token. It reads the summary's hidden state, the diff and the code at head, all as data, and asks the model. It never imports, builds or tests anything from the checkout.
- **`proof-run`** runs the pull request's code. It has no secrets and a `contents: read` token, and the checkout does not keep that token (`persist-credentials: false`). Each test runs in a child process whose environment is an allowlist (`PATH`, a scratch `HOME` and `TMPDIR`, `CI`, locale), so no inherited variable reaches it. With bubblewrap, that child also has no network, sees only the snapshot it runs on and read-only toolchains, and cannot write outside its scratch space.
- **`proof-report`** has `checks: write` and `pull-requests: write`, and checks out nothing from the pull request. It reads two JSON files and posts.

The report does not take the results file on trust. It refuses results for a different head commit or a different plan (the results carry the plan's digest), and it downgrades any outcome that lacks the run behind it to `could_not_run`. A `supported` needs a recorded failing run on head, and `contradicted` needs a recorded passing one. Without bubblewrap, though, the pull request's code ran in the same job that wrote the results, so it could have written its own. The check run names the sandbox it ran in for that reason, and you should treat `sandbox: none` results as advice.

## What each outcome means

For each reproduction:

| Outcome | Meaning |
| --- | --- |
| `supported` | The test fails on head in the way the finding claims, and passes on base (or the code it calls does not exist on base yet). |
| `contradicted` | The test passes on head: the claimed failure does not happen. |
| `inconclusive` | The test fails for another reason (an import or setup error, or not the failure the finding names), or it fails on base too. |
| `could_not_run` | Nothing reliable ran: setup failed, it timed out, the sandbox was required and missing, no test framework was detected, the model declined, or the budget was spent. |

For each added or changed test, run on base with the head's test files on top:

| Outcome | Meaning |
| --- | --- |
| `fails_on_base` | It passes on head and fails on base: it can catch the change being undone. |
| `passes_on_base` | It passes on base too: it may not test this change. |
| `could_not_run` | No green run on head to compare with, no framework detected, or the run did not complete. |

The check run's conclusion:

- It is `failure` when any finding is `supported`.
- It is `success` only when every finding is `contradicted` and every changed test is `fails_on_base`, all from recorded runs.
- It is `neutral` otherwise, including every `could_not_run` and a run job that never finished.

No outcome reads as a pass without a recorded run behind it. Each finding's thread reply gives the outcome, the commands that ran and their exit codes, the generated test and the tail of its output.

`proof-report` also writes one line into the LemonCrow summary, linking the check run, for as long as the summary is for the commit the proof ran on: `Proof on abc1234: 1 supported, 0 contradicted, 0 inconclusive, 0 could not run; new tests: 1 of 2 fail on base`. The next review of the same head keeps the line; a review of a newer head leaves it out, because a proof of an older push says nothing about the new one.

## Test frameworks

The frameworks come from the base commit, never from the pull request:

1. **Test recipes.** A test recipe in `check_recipes` of the base `.lemoncrow/review.yaml` comes first, if it takes a list of test files.
2. **Simple detection.** Otherwise, pytest (`pyproject.toml`, `pytest.ini`, `setup.cfg`, `tox.ini` or `conftest.py`), vitest or jest (`package.json`, run with `npx --no-install`), Go (`go.mod`) and Cargo (`Cargo.toml`) are detected.

A finding in a language with no framework is `could_not_run` and costs no model call. The sandbox has no network, so dependencies must already be installed on the runner. Go modules and Cargo crates must be vendored in the checkout and passed with `--deps vendor`.

## Cost

There is one model call per open P0/P1 finding, for at most 5 findings per run, P0 first. Set the limit with `--max-findings`, up to 20. The calls spend from the same `max-usd-per-pr` budget the review keeps in its summary state: once the budget is reached, the remaining findings are `could_not_run` with that reason, and no call is made. The model is given the finding, the code around it, its diff and one existing test file as an example, under the same content-access rules as the review. Red-on-base makes no model call. `proof-plan` has a read-only token, so `proof-report` writes the plan's spend into the summary's state (once per plan, so a re-run report does not count it twice). From then on, the review, chat answers and the next proof plan count it toward `max-usd-per-pr`.

## Run it on your machine

```bash
lc review proof plan --pr 123 --out plan.json          # needs a model key and gh
lc review proof run --local --plan plan.json --out results.json   # bubblewrap is required; never runs plainly
lc review proof run --local --base origin/main         # red-on-base only, no plan and no model
lc review proof run --local --plan plan.json --review REVIEW_ID   # also records it on that local Review
lc review proof report --plan plan.json --results results.json --dry-run
```

`--local` refuses to run without a working bubblewrap sandbox, and it is Linux-only. With `--review`, the outcome is recorded as a `LemonCrow Proof` check on that Review's Snapshot of the head commit.

## Not yet

- A pull request from a fork gets no proof, because its run has no model key and no write token, as with the review.
- macOS and Windows runners are not supported.
