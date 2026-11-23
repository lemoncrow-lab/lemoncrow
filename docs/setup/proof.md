# Proof: reproduce findings and run the new tests on base

A review finding is a claim. `lc review proof` tries to settle it by running code. For each open P0 or P1 finding in the LemonCrow summary on the head commit, it asks the model for one minimal failing test that would demonstrate the bug, written for the repository's own test framework. It runs that test on the head commit and on the base commit. Separately, it runs the pull request's own added or changed tests on the base commit ("red-on-base"), which shows whether those tests would catch the change being undone.

The results are posted as a `LemonCrow Proof` check run on the head commit, with one reply on each finding's inline thread. Early access, Linux runners only.

## Add it to the review workflow

Add these three jobs to the workflow from [GitHub Action](github-action.md), next to its `review` job, replacing `<sha>` with the same release commit the review pins. Each job sets its own `permissions:`, which replace the workflow's for that job. Keep the order of the steps and the `head/` checkout: they are what keeps the pull request's files away from the steps that run before `lc` (see "Why three jobs").

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
      # The order is part of the design. The CLI and uv come first, while the workspace holds nothing of the pull
      # request. The pull request is checked out last, into head/, and `lc` is pointed at it with --repo, so no step
      # before `lc` has the pull request as its working directory or as a parent directory that uv would search.
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: lemoncrow-lab/lemoncrow
          ref: <sha>  # v0.8.1, the same release commit as the review job
          path: .lemoncrow-cli
          persist-credentials: false
      - uses: astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86  # v5.4.2
        with:
          version: "0.11.32"   # pinned, so a `required-version` in the pull request's uv.toml chooses nothing
          enable-cache: false  # the default (`auto`) runs uv and hashes files in the working directory on hosted runners
      - run: uv sync --frozen --project .lemoncrow-cli
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          path: head
          fetch-depth: 0
          persist-credentials: false
      - name: Plan one reproduction per open P0/P1 finding
        env:
          GH_TOKEN: ${{ github.token }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          LEMONCROW_REVIEW_MAX_USD: "3"  # the review's max-usd-per-pr; proof spends from what is left of it
        run: >-
          uv run --project .lemoncrow-cli lc review proof plan --repo head
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
      # The same order as proof-plan: the CLI and uv first, the pull request last and in head/.
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: lemoncrow-lab/lemoncrow
          ref: <sha>  # v0.8.1
          path: .lemoncrow-cli
          persist-credentials: false
      - uses: astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86  # v5.4.2
        with:
          version: "0.11.32"
          enable-cache: false
      - run: uv sync --frozen --project .lemoncrow-cli
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          path: head
          fetch-depth: 0
          persist-credentials: false
      - name: Bubblewrap (the sandbox)
        run: |
          sudo apt-get install -y bubblewrap
          # Ubuntu 24.04 blocks unprivileged user namespaces by default; bubblewrap needs them.
          sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
      - uses: actions/download-artifact@37930b1c2abaa49bbe596cd826c3c89aef350131 # v7.0.0
        with:
          name: lemoncrow-proof-plan
      # Your project's toolchain and test dependencies, from the BASE commit's requirements file, written to the
      # runner's temp directory (not read from the head checkout), wheels only. These steps read nothing of the
      # pull request but that one file of the base commit, build nothing from it and cannot put a program of its own on
      # PATH. Install the dependencies, not the project itself: an editable install would make the base run import
      # the head's code.
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5
        with:
          python-version: "3.12"
      - name: Test dependencies (from the base commit)
        working-directory: ${{ runner.temp }}
        run: |
          # The plan's base, not the base branch's tip: it is the commit `lc` diffs the head from, so the file
          # installed here is the file --deps-manifest compares.
          BASE_SHA=$(jq -r .base_sha "$GITHUB_WORKSPACE/lemoncrow-proof-plan.json")
          [[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "the plan names no base commit" >&2; exit 1; }
          git -C "$GITHUB_WORKSPACE/head" show "$BASE_SHA:requirements-dev.txt" > base-req.txt
          python -m venv venv
          venv/bin/pip install --only-binary=:all: -r base-req.txt
          echo "$RUNNER_TEMP/venv/bin" >> "$GITHUB_PATH"
      # --deps-manifest names the file installed above (a path relative to the repository root; the run refuses one
      # that is in neither commit). When the pull request changes it, this run still has the base's dependencies,
      # so a failure on head is reported as "could not run", and no reproduction reads supported or contradicted.
      - name: Run the reproductions and red-on-base
        run: >-
          uv run --project .lemoncrow-cli lc review proof run --repo head
          --plan lemoncrow-proof-plan.json --sandbox bubblewrap
          --toolchain "$RUNNER_TEMP/venv" --toolchain /opt/hostedtoolcache
          --deps-manifest requirements-dev.txt
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
      id-token: write        # GitHub's identity token for the report; this job only, never proof-run
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: lemoncrow-lab/lemoncrow
          ref: <sha>  # v0.8.1
          path: .lemoncrow-cli
          persist-credentials: false
      - uses: astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86  # v5.4.2
        with:
          version: "0.11.32"
          enable-cache: false
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
          --report-file lemoncrow-proof-report.json
      # A hosted Review reads the per-part proof from this file, not from the check run (see "Why three jobs").
      # `if: always()`: a report the step above refused to attest (it exits 2, with no token) still goes up, so a
      # hosted Review says why instead of saying the artifact is missing.
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        if: always()
        with:
          name: lemoncrow-proof-report
          path: lemoncrow-proof-report.json
          overwrite: true
          if-no-files-found: ignore
```

The dependency install takes the list from the base commit, not the head: the plan's `base_sha`, the same commit `lc` diffs the head from (the pull request's merge base, which can be older than the base branch's tip). The summary comment that names it is not signed, so `proof plan` refuses a base commit that is not in the base branch's history: it checks it against the branch's tip as GitHub reports it now and against the clone's `origin/<base>` when `origin` is the base repository (a head checked out from a fork has the fork as `origin`, whose branches the author sets, so they are not consulted; the example checks out with `fetch-depth: 0`), and a commit that only the pull request's own branch holds is refused with a message that says so. Under GitHub Actions `proof plan` also refuses a pull request that is not in the workflow's own repository (`GITHUB_REPOSITORY`, on `GITHUB_SERVER_URL`'s host), because `gh` resolves a bare `--pr` number from the clone's remotes: a workflow adapted to check the head out from the fork would otherwise read the fork's own pull request #N, whose base branch and summary the author controls. The message names both repositories and suggests `--pr OWNER/REPO#NUMBER`. A requirements file, a source distribution's build script or a package's console script from the pull request's own list would otherwise run as the job before the sandbox starts, with the job's permissions (see "Remaining limits"). The cost is plain: a pull request that adds or bumps a test dependency is tested against the base's dependencies, so a test that imports the new one cannot load, and it fails on head for a reason that is not its own. `--deps-manifest requirements-dev.txt` tells the run which file the install read (repeat it for each file). The path is relative to the repository root (the one `--repo` names), and the run refuses a path that is not a file in the base or the head commit (a misspelling, or a directory), so it cannot leave the guard off. When the pull request changes a listed file, nothing the run saw on head stands as a verdict. A changed test that fails on head reads `could_not_run` ("the pull request changes a dependency file this run did not install, so a head failure is not shown to be the test's own"; a hosted Review shows "Couldn't run: the pull request changes a dependency file the CI proof run does not install, so a failure on head may come from the base's dependencies"). A reproduction that would read `supported` or `contradicted` reads `inconclusive`: it cannot turn the check red, and a pass under the base's dependencies cannot answer the finding's thread with "Contradicted" or turn the check green. Neither reads "Fails" or "Supported". A changed test that passes on head is unaffected, because the base and head runs share the base's dependencies, and so is a run where no listed file changed. The list is yours to keep: a file the install includes with `-r`, or a lockfile you do not name, is not watched. Requirements that build head code (`-e .`, `./pkg`, a relative `-r other.txt`) fail on purpose: they resolve against the runner's temp directory, not the checkout. If the base keeps a hash-pinned file (`pip-compile --generate-hashes`), install that file and add `--require-hashes`. Replace `requirements-dev.txt` with your file's path, in the install step and in `--deps-manifest`. The example reads the plan with `jq`, which GitHub-hosted Ubuntu runners have.

The `proof-run` example is for a Python project. For another stack, replace the setup-python and venv steps with your own toolchain and a dependency install from the base commit's lockfile, outside the checkout and without build scripts of source packages, and pass each directory the tests need: `--toolchain DIR` for directories outside the checkout, such as an interpreter, an SDK or a venv, and `--deps node_modules` for dependency directories inside it. If your key is an OpenAI key, set `OPENAI_API_KEY` in `proof-plan` instead.

## Why three jobs

The pull request's code is untrusted. A test file, a `conftest.py` or a package `postinstall` script is code the author chose, and it runs as soon as tests run. So that code never runs in a process that holds the model key or a token that can write:

- **`proof-plan`** holds the model key and a read-only token. It reads the summary's hidden state, the diff and the code at head, all as data, and asks the model. It never imports, builds or tests anything from the checkout, which is in `head/`, not the workspace root.
- **`proof-run`** runs the pull request's code. It has no secrets and a `contents: read` token, and the checkout does not keep that token (`persist-credentials: false`). Each test runs in a child process whose environment is an allowlist (`PATH`, a scratch `HOME` and `TMPDIR`, `CI`, locale), so no inherited variable reaches it. With bubblewrap, that child also has no network, sees only the snapshot it runs on and read-only toolchains, and cannot write outside its scratch space. Under GitHub Actions (`GITHUB_ACTIONS=true`) the CLI runs every test that way, whatever `--sandbox` says: `auto` and `none` both need bubblewrap there, and with none working nothing of the pull request runs: every finding and every changed test reads `could_not_run` with the reason, in the check and in the results file. A hosted Review shows "Couldn't run: the CI proof job has no working bubblewrap sandbox, so it ran nothing" for every part, including a part the report has no row for (the check's `Sandbox:` line says `Nothing was run: the sandbox is missing.`, which is how the server tells those parts apart from tests nobody mapped). There the CLI also starts `bwrap`, `git`, `env` and `systemd-run` from the system path (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`), never from `PATH`, so a program a dependency puts first on `PATH` is not what it runs. The steps your workflow runs itself before `lc` run as the job and are outside that sandbox, so the example keeps the pull request away from them. The CLI is checked out and `uv` is installed (a pinned version, cache off) before the pull request is checked out. The pull request goes into `head/`, not the workspace root, and `lc` gets `--repo head`, so no earlier step has it as its working directory and `uv` never searches upward into its `.python-version`, `uv.toml` or `pyproject.toml`. The dependency install reads the base commit's file, as above.
- **`proof-report`** has `checks: write` and `pull-requests: write`, and checks out nothing from the pull request. It reads two JSON files and posts.

The report does not take the results file on trust. It refuses results for a different head commit or a different plan (the results carry the plan's digest), and it downgrades any outcome that lacks the run behind it to `could_not_run`. A `supported` needs a recorded failing run on head, and `contradicted` needs a recorded passing one. A run of the pull request's code outside a sandbox could have written its own results, so under GitHub Actions `proof-report` refuses to attest a results file that records one (`sandbox: none`): it requests no identity token, writes the report file with an empty token, prints why, posts the check as `neutral`, and exits 2. A hosted Review reads that report as "Couldn't run: the proof report could not be attested", because it has no token to verify. A report that does carry a valid token but whose `Sandbox:` line is not `bubblewrap` (an older CLI, or a custom workflow) reads "Couldn't run: the proof ran without a sandbox" instead, whichever CLI wrote it. That refusal only catches an honest misconfiguration: pull request code that did run plainly could also have forged the `sandbox` field, which is why the run job never runs it plainly in the first place. The check run still names the sandbox it ran in, and the example workflow passes `--sandbox bubblewrap` so that the intent is on the page; outside GitHub Actions `--sandbox auto` and `none` can still run plainly, and you should treat such results as advice.

A hosted Review does not take per-part proof from the check run, because any workflow with `checks: write` can post a check named `LemonCrow Proof` with any text, and GitHub files a check made with the workflow token under an existing check suite of the commit, so the suite does not say which workflow posted it. The check is only a pointer: its `external_id` names the workflow run (`<run id>:<attempt>`), and the report it carries is the one `proof-report` wrote to `lemoncrow-proof-report.json` and uploaded as the `lemoncrow-proof-report` artifact. The server reads GitHub's own record of that run and that run's artifact, and records the artifact's report, never the check's text.

The run record alone cannot say which pull request a run was for: another pull request of the same head commit (say the same branch opened against a branch the attacker controls, with an edited workflow) runs a workflow of the same name on the same commit. So `proof-report` also asks GitHub for an identity token (that is why it, and only it, has `id-token: write`). The token's audience is `lemoncrow-proof:` plus the SHA-256 of the head commit and the exact report, so it vouches for that report on that commit and nothing else. The token goes in the artifact next to the report and never in the check. The server verifies the token's RS256 signature against GitHub's published keys (`https://token.actions.githubusercontent.com`; a GitHub Enterprise Server host has its own issuer, set with `LEMONCROW_SERVER_GITHUB_REVIEW_OIDC_ISSUER` when it is not `https://HOST/_services/token`) and trusts the run only when all of these hold:

- the token's audience is the one recomputed from this Review's head commit and the artifact's report, and its repository id, `run_id` and `run_attempt` are this repository, run and attempt of the check;
- the token is for a `pull_request` run of this Review's own pull request: its `ref` is `refs/pull/<number>/merge` and its `base_ref` is this Review's base branch (GitHub's `pull_requests` field of the run is never used, it is not tied to the workflow);
- the workflow (`workflow_ref`) and the job's workflow (`job_workflow_ref`, which differs when the job comes from a reusable workflow) are files under `.github/workflows/` at that pull request's merge ref, the run record names the same workflow file, and the run is for this head commit;
- the pull request does not add or change that workflow, any reusable workflow of this repository that it calls, or anything under `.github/actions/`;
- the run has a non-expired `lemoncrow-proof-report` artifact for the attempt, holding one JSON file of at most 256 KiB whose report reads as a LemonCrow Proof report.

So keep the `checks: write` job in a workflow file of the base branch, and expect a pull request that edits that workflow (or a local action) to show "Couldn't run: untrusted workflow" for every part the check reported. The upload may finish after the check is posted: while the run is still going, the server waits and reads again rather than deciding, and it does the same when GitHub's key set cannot be read (nothing is believed or refused meanwhile). The hosted App needs the **Actions: Read** permission to read runs and artifacts (see [Hosted Review](hosted-review.md)).

A check the server does not believe never reads as proof, and every part it reported says why. The last two rows are the exceptions: those reports are believed, and what they say is that nothing ran, or that a failure may not be the test's own.

| Part says | Because |
| --- | --- |
| Couldn't run: untrusted workflow | The check names no run, or a run that is not one the pull request leaves as the base has it, or a run for another pull request or base branch, or one that does not exist. |
| Couldn't run: the GitHub App cannot read workflow runs; it needs Actions: Read | GitHub refused the read (401 or 403, or 404 when the App's token exchange did not report Actions: Read), usually because the App lacks Actions: Read. The hosted overview lists it as a readiness item. |
| Couldn't run: the proof report artifact is missing or expired | The run finished without a `lemoncrow-proof-report` artifact, or it has expired. |
| Couldn't run: the proof report artifact could not be read | The artifact is too large, is not a single JSON file in the report format, its report is not a LemonCrow Proof report, or the report download did not answer with a redirect. |
| Couldn't run: the proof report could not be attested | The artifact carries no identity token (`proof-report` lacks `id-token: write`, or it refused to attest a plain run), or the token is not GitHub's, or was not minted for this commit and this exact report. |
| Couldn't run: the proof ran without a sandbox | The report carries a token, but its `Sandbox:` line says the run was not in bubblewrap (or says nothing readable), so the pull request's code could have written whatever the report claims. No row of that report is believed. |
| Couldn't run: the CI proof job has no working bubblewrap sandbox, so it ran nothing | Under GitHub Actions the job's bubblewrap could not start (it is not installed, or the runner blocks unprivileged user namespaces), so no test of the pull request ran. The report is genuine, and unlike the rows above it keeps the "Reported by CI" line on a part it has a row for. A part it has no row for reads the same reason, without that line. Install bubblewrap and lift the restriction as in the example workflow. |
| Couldn't run: the pull request changes a dependency file the CI proof run does not install, so a failure on head may come from the base's dependencies | The pull request changes a file the run was given with `--deps-manifest`, and a changed test failed on head. The run installed the base's dependencies, so that failure is not shown to be the test's own. The report is genuine and keeps the "Reported by CI" line. A reproduction in the same position reads `inconclusive` whichever way it came out, in the check and on its thread. |

A re-run of the workflow posts a new check naming its own attempt, and the server reads that attempt's artifact and token. The newest check decides: a later check that cannot be believed replaces an earlier trusted report with "Couldn't run" until the next genuine run restores it. That is never a proof, but it is an insider ceiling: whoever can post a check or upload an artifact for a commit can make its parts read "Couldn't run".

Remaining limits:

- Any job of the trusted run could upload an artifact named `lemoncrow-proof-report`, and any job with `id-token: write` could mint a token for it. Only `proof-run` executes pull request code, and its test child has an allowlisted environment without the Actions runtime token it would need to upload or to ask for an identity token, so the pull request's code cannot do either. A job you add to the same workflow that runs pull request code with those tokens could. Give `id-token: write` to `proof-report` only.
- Whatever `proof-run` runs before the sandbox runs as the job, outside it. The example keeps the pull request's files out of every such step: the CLI and `uv` come first, `uv` pinned with its cache off, and the pull request is checked out afterwards, into `head/`. Checked out into the workspace root with `setup-uv` at its defaults, the pull request would have run as the job twice over. The action's cache is on by default (`auto`) on hosted runners, and its key comes from `uv python find` run in the working directory, which executes an interpreter path that the repository's `.python-version` names. `uv` also reads `required-version` from the repository's `uv.toml` or `pyproject.toml`, and it searches upward from the CLI's directory, so a CLI checked out inside the head checkout reaches the same files. That code would run before the sandbox, and a process it leaves behind could rewrite `lemoncrow-proof-results.json` before it is uploaded. In `proof-plan` it would run beside the model key. The example's dependency install reads the base commit's requirements file, outside the checkout, wheels only, so the pull request chooses nothing that builds or runs there: no source distribution's build script, no requirement pointing at its code, no console script from its own dependency list. What still runs there is what the base branch already holds: the wheels the requirements file names at a commit of that branch's history, and the actions the workflow names. The commit is the plan's `base_sha`, which `proof plan` takes from a summary comment that is not signed; it refuses one the base branch does not hold, so the comment cannot point the install at the pull request's own commits, but it could still name an older commit of the base branch, whose file the branch once accepted. That check needs `--pr`: a plan made with `--state-file` takes the state's base as it is, and the example does not use one. A workflow that installs the head's own list instead (`pip install -r requirements-dev.txt` from the checkout) runs a build script or a requirement that points at a repository as pull request code, with the job's permissions: a `contents: read` token the checkout does not keep, and the Actions runtime credential the job's upload step uses. It could leave a process behind that rewrites the results file or uploads its own `lemoncrow-proof-report` (it cannot mint the identity token: `proof-run` lacks `id-token: write`), and it could put a program named `uv`, or any tool a later step starts, ahead of the real one through the venv that the example adds to `GITHUB_PATH`. The CLI itself no longer starts `bwrap`, `git`, `env` or `systemd-run` from `PATH` under GitHub Actions, but the workflow's own steps (`uv run`, `lc`) still do. The sandbox protects the tests from each other and from the runner, not the results from the install.
- The base check trusts the history of whichever branch the pull request targets. Anyone who can push a branch can open a pull request into it, and that branch's history is theirs, so give the workflow's `pull_request` trigger a `branches:` filter that names your protected branches (for example `branches: [main]`, as in the [GitHub Action](github-action.md) page): a pull request into any other branch then runs no proof.
- A shared self-hosted runner has the same ceiling for anything an earlier job left behind. Keep untrusted self-hosted runners one job per machine, or bake the dependencies into the runner image.
- Outside GitHub Actions `--sandbox auto` still falls back to running unsandboxed when bubblewrap is unusable (the check says `sandbox: none`, and a hosted Review reads that as "Couldn't run"), and `--sandbox none` always runs plainly. Under GitHub Actions neither does.
- These setups are safe but refused, and read "Couldn't run: untrusted workflow": a reusable workflow from another repository; a same-repository reusable workflow pinned to `@main` (or any ref other than the pull request's merge ref); and a base branch that is renamed, or a pull request retargeted to another base, until the next run posts its check.

## What each outcome means

For each reproduction:

| Outcome | Meaning |
| --- | --- |
| `supported` | The test fails on head in the way the finding claims, and passes on base (or the code it calls does not exist on base yet). |
| `contradicted` | The test passes on head: the claimed failure does not happen. |
| `inconclusive` | The test fails for another reason (an import or setup error, or not the failure the finding names), or it fails on base too, or it would be `supported` or `contradicted` but the pull request changes a file listed with `--deps-manifest` (the run installed the base's dependencies, so a pass or a failure on head is not shown to be the head's own). |
| `could_not_run` | Nothing reliable ran: setup failed, it timed out, the sandbox was required (always, under GitHub Actions) and missing, no test framework was detected, the model declined, or the budget was spent. |

For each added or changed test, run on base with the head's test files on top:

| Outcome | Meaning |
| --- | --- |
| `fails_on_base` | It passes on head and fails on base: it can catch the change being undone. |
| `passes_on_base` | It passes on base too: it may not test this change. |
| `could_not_run` | No green run on head to compare with (a failure on head counts, and reads as the changed dependency file when the pull request changes one listed with `--deps-manifest`), no framework detected, or the run did not complete. |

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
