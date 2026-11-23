# Environment definition: `.lemoncrow/environment.toml`

A repository says how its changes are checked, and what they run against, in one committed file, `.lemoncrow/environment.toml`. `lc verify` runs its checks on your machine and as the pre-push hook. LemonCrow Review reads the same file from the trusted base of a review. `lc verify init` writes a first one from the checks the repository already runs. Status: preview, in the Workspaces client (`lc verify` is not in the Review-only client).

## The file

```toml
version = 1

[toolchain]
image = "ghcr.io/example/ci@sha256:<64 hex digits>"

[[dependencies]]
lockfile = "uv.lock"
install = "uv sync --frozen"

[services.db]
image = "postgres@sha256:<64 hex digits>"
ready = "pg_isready -h 127.0.0.1"

[previews.web]
run = "npm run dev -- --port $PORT"
ready = "/health"

[needs]
network = ["api.example.com"]
secrets = ["STRIPE_TEST_KEY"]

[[checks]]
id = "lint"
kind = "lint"
paths = ["*.py", ":!experiments"]
run = '''f=$(grep -E '\.py$' "$LC_CHANGED"); [ -z "$f" ] || ruff check $f'''

[[checks]]
id = "tests"
kind = "test"
timeout = 1200
paths = ["src", "tests", ":(attr:generated)"]
needs = ["db"]
run = "uv run pytest -q"
```

Every section but `version` is optional.

| Key | Type | Meaning |
| --- | --- | --- |
| `version` | integer | Must be `1`. |
| `[toolchain]` `image` | string | The base image checks and sandboxes are built from, pinned as `name@sha256:<digest>`. |
| `[[dependencies]]` | array of tables | One per lockfile. |
| `dependencies.lockfile` | string | A file path relative to the repository root. |
| `dependencies.install` | string | The command that installs what the lockfile pins. |
| `[services.<name>]` | table | A service checks can run against. The name follows the check id rule. |
| `services.<name>.image` | string | Its image, pinned by digest. |
| `services.<name>.ready` | string | A shell command that exits 0 once the service serves. |
| `[previews.<name>]` | table | A command that serves the application. |
| `previews.<name>.run` | string | A shell command that serves on `$PORT`, from the repository root. |
| `previews.<name>.ready` | string | The path that answers once it is up, such as `/` or `/health`. |
| `[needs]` `network` | array of strings | Lowercase host names the repository asks to reach. |
| `[needs]` `secrets` | array of strings | Environment variable names (`[A-Z_][A-Z0-9_]*`) the repository asks for. |
| `[[checks]]` | array of tables | The checks, run in the order written. |
| `checks.id` | string | Unique; lowercase letters, digits, `.`, `_` and `-`, starting with a letter or digit. |
| `checks.paths` | array of strings | Git pathspecs. The check runs when the change touches a path they match. `:!` excludes, and `:(attr:NAME)` matches by `.gitattributes`. A deleted file counts as touched. |
| `checks.run` | string | A shell command, run with `sh -c` from the repository root. |
| `checks.kind` | string | One of `test`, `lint`, `format`, `typecheck`, `build`, `precommit`. Without one, Review treats the check as a build. |
| `checks.timeout` | integer | Seconds, from 1 to 3600. Without one, `lc verify` sets no limit and Review stops the check after 900 seconds. |
| `checks.needs` | array of strings | Names of services under `[services]` the check runs against. |

Any other key, a duplicate `id` or lockfile, empty `paths`, an empty `run`, a `needs` entry that names no declared service, a timeout out of range or another `version` is an error that names the file and the key; nothing runs.

An image without a digest is accepted, but it makes the environment fingerprint unreproducible: the same name may mean another image later. Review refuses to start a service whose image is not pinned.

A name without a registry means what it means to Docker, on Podman as well: `python@sha256:...` is `docker.io/library/python@sha256:...`, and `acme/app@sha256:...` is `docker.io/acme/app@sha256:...`. Podman's own search registries are never consulted.

A check's command gets `LC_CHANGED`: the path of a file listing the changed paths that still exist, one per line, relative to the repository root. Use it to check only what changed, as `lint` does above. Standard input is empty.

## The environment fingerprint

Each check has a fingerprint built from:

- the toolchain image and its digest;
- the bytes of every declared lockfile;
- the check digest: its `run`, `paths`, `kind` and `timeout`, and the image and `ready` command of each service it needs. The `id` is left out.

`lc verify` and Review compute it with the same code. When a check passed for you and failed in Review, compare the fingerprints first.

## `lc verify init`

```bash
lc verify init                     # write .lemoncrow/environment.toml from the checks the repository already runs
lc verify init --print             # show it; write nothing
lc verify init --toolchain IMAGE   # also pin [toolchain] to IMAGE's digest; on an existing file, set only [toolchain]
```

`lc verify init` looks for checks in five places, strongest first:

1. Makefile targets named like checks: `test`, `lint`, `check`, `typecheck`, `format-check`, `pre-commit` and similar.
2. `package.json` scripts with those names, run with the package manager the repository uses (`packageManager`, else its lockfile, else npm).
3. `run:` steps of the GitHub Actions workflows in `.github/workflows` that start a known checker, such as pytest, ruff, mypy, eslint, tsc, `go test`, `cargo test` or `rake`.
4. The tools `pyproject.toml` configures or lists as dependencies (pytest, ruff, black, mypy, pyright), only for a kind of check no other source gave. They run through the project's runner: `uv run` with `uv.lock`, `poetry run`, `pdm run`, else `python3 -m`. With a lockfile, a tool runs only if a dependency list names it, since the locked environment has nothing else; `uv run` adds `--group G` or `--extra E` when only a group uv does not install by default, or an extra, names it.
5. The test command of the build manifest at the root, only when no other source gave a test: `go test ./...` for `go.mod`, `cargo test` for `Cargo.toml`, `./mvnw -B test` (`mvn` without the wrapper) for `pom.xml`, and `./gradlew check` (`gradle` without the wrapper) for a Gradle build.

A check that an earlier source already runs is not added twice, and a target, script or CI step that only runs checks already found is left out. A command that rewrites files (`--fix`, `--write`, a formatter without `--check`, or a target that runs one) is never a check, because `lc verify` runs in your working tree. A linter, formatter or type checker gets `paths` for its language's files and configuration; any other check gets `.`, so it runs on every change. A comment above each check names where it came from. Read the file before you commit it.

A root `package-lock.json` or `uv.lock` becomes a `[[dependencies]]` entry (`npm ci`, `uv sync --frozen`): a Workspaces server runs it before the checks, and Review provisions the lockfile itself. Any other lockfile (`pnpm-lock.yaml`, `yarn.lock`, `bun.lock`, `poetry.lock`, `pdm.lock`, `Gemfile.lock`) is only named in a note, because Review refuses a file that declares one. Such a repository's checks then find only what the toolchain image has. `lc verify` runs no install either way.

Workflows are read without a YAML parser, so only what can be read safely is used. A step is skipped when it branches or loops, runs in another shell, has `continue-on-error`, or has a working directory set by the workflow's defaults, by an expression, or to a folder that does not exist. Within a step, a command with an expression or `$` variable, a pipe, redirection or subshell is skipped. The command prints how many steps it skipped in each workflow.

It never rewrites an existing file. When `.lemoncrow/environment.toml` exists, `lc verify init` says so and exits 0. The one exception is `--toolchain IMAGE`: it adds or replaces only the `[toolchain]` image (a comment on that line stays), prints the change as a diff, and leaves every other byte as it was. With `--print` it prints the result instead.

`--toolchain IMAGE` asks IMAGE's registry for the digest of the manifest its tag names (`latest` without a tag) and writes `IMAGE@sha256:<digest>`. A name without a registry means Docker Hub, as above. It uses the anonymous token that Docker Hub, ghcr.io and other registries give for public images, and sends no credentials; for a private image, write the pinned reference under `[toolchain]` yourself. A registry on `localhost` or a `127.` address is reached over HTTP, any other over HTTPS. When the registry cannot be reached or does not have the image, the command says why and writes nothing; it never guesses a digest. Without `--toolchain` the file has no `[toolchain]`, and a comment in it says that a Workspaces server refuses a task without one.

Exit status: `0` when the file was written or printed, or already exists; `2` for a usage, Git or definition error, when no checks were found, or when the digest could not be resolved.

## `lc verify`

```bash
lc verify                 # checks touched by HEAD plus uncommitted and untracked work, against the merge base with origin/main
lc verify --base REF      # the same against the merge base with REF
lc verify --all           # every check, as if every tracked file changed
lc verify --list          # print the ids of the checks that would run; run nothing
lc verify --pre-push      # the pre-push hook: exactly the pushed commits, from Git's ref lines on stdin
```

Without `origin/main` the base is the root commit. Exit status: `0` when every selected check passed or nothing changed, `1` when a check failed or ran past its timeout, `2` for a usage, Git or definition error. A check that runs past its `timeout` is stopped with every process it started.

`lc verify` runs checks in your working tree with your own tools. It does not start the toolchain image or services, and it does not run `install`; it prints a note for a check that needs a service.

As the pre-push hook it skips tags and deleted branches, and a new branch is checked from where it leaves `origin/main`. Checks run in your working tree, so push from a clean tree for an exact result. A repository that uses Git LFS (`filter=lfs` in `.gitattributes`) keeps its LFS upload: `lc verify --pre-push` runs `git lfs pre-push` first.

### The verify record

After its checks run, `lc verify` writes `<git common dir>/lemoncrow/verify/<commit>.json`, where `<commit>` is `HEAD`. For each check it ran, the record holds how the check ended (`passed`, `failed` or `timed_out`) and the fingerprint it ran under. It also says whether the working tree had uncommitted changes. The file is replaced atomically, and only the newest 64 records are kept. `lc verify --list` writes nothing. A record that cannot be written is a note, never a failed verify.

## The pre-push hook

```bash
lc verify hook install           # write the hook (.git/hooks/pre-push, or wherever core.hooksPath points)
lc verify hook install --print   # show it; write nothing
lc verify hook install --force   # replace a pre-push hook that is not this one
lc verify hook uninstall         # remove it
```

The hook calls the absolute path of the `lc` that wrote it (the first `lc` on your PATH only if that file is gone) and runs `lc verify --pre-push`. It stops the push when a check fails, and also when `lc` cannot be found: it is the gate, so it fails closed and prints the file to delete to push without it. `git push --no-verify` skips it for one push.

Install refuses when the repository has no valid `environment.toml` (`lc verify init` writes one), and never overwrites another pre-push hook (for example the one `lc review hook install` writes) without `--force`. To keep another hook, add `lc verify --pre-push "$@"` to it. Installing twice writes nothing the second time. The hook's path is kept in `~/.lemoncrow/installed-hooks.txt`, and `lc uninstall all` removes the hook by its `# lemoncrow-verify-hook` marker line; `lc uninstall review` leaves it.

## How Review reads it

Review reads `.lemoncrow/environment.toml` from the trusted base of the review, never from the change under review: an edit to the file applies once it is merged. Local reviews read it from the base commit; hosted GitHub reviews capture it from the trusted base next to `.lemoncrow/review.yaml`.

- **Checks.** Each check becomes a check recipe that runs `sh -c <run>` on both sides, with `$LC_CHANGED`, only when the change touches its `paths`. Its `kind` files the result under that kind (`checks.test`, `checks.lint` and so on); a check without a kind is a build, addressable as `checks.recipe:<id>`. A check the change does not touch is shown as not run, with the reason.
- **`attr:` pathspecs.** Review matches them with the trusted base's `.gitattributes`, never your own Git configuration. When the change edits a `.gitattributes` file, or the attributes cannot be read, a check that uses `attr:` runs.
- **Dependencies.** Review does not run `install`. Its fetch stage downloads what each declared lockfile pins from the allowed registries; the build and run stages then have no network. Supported lockfiles are `package-lock.json`, `uv.lock` and hash-pinned `requirements*.txt`; any other lockfile, a lockfile outside the repository, a missing one or more than one Python project is refused with the reason. Python checks run with the installed environment and `UV_OFFLINE=1`, so `uv run` works offline. npm checks get the installed `node_modules` and `npm_config_offline=true`. They also get an npm cache (`npm_config_cache`) that holds every package in the lockfile, filed by its integrity hash, so npm finds each one without the network. In the toolchain image (below), `node_modules` is a writable copy for that one run, so `npm ci --offline` works. Without the toolchain image, `node_modules` is read-only and `npm ci` fails, because it must first empty `node_modules`.
- **Toolchain.** When `[toolchain]` is pinned by digest and the machine has rootless Podman or rootless Docker, a check runs inside that image. The reviewed files are at `/workspace`, the dependency trees and fixtures are read-only, and the check reaches no network but its own services. A rootful engine is used only when `LEMONCROW_OCI_ALLOW_ROOTFUL=1` says this is a development machine. Without a usable engine the check runs in the usual sandbox, and its result says the toolchain image was not used and why. Runtime observation and trusted sidecars still need the usual sandbox, so such a check is shown as not run in the toolchain image.
- **Services.** Review starts the services a check needs, in order, and waits up to 120 seconds for each `ready` command to exit 0. The services stay up while the check runs and are stopped afterwards, in reverse order, even when the check fails. A service image that is not pinned by digest is refused. Services run only with the toolchain image: each on the run's own internal network, which has no route out, under its name (`db`), and removed with the run. Without the toolchain image, a check that needs a service is shown as not run, with the reason.
- **Previews.** Every preview under `[previews]` can be the review's interactive preview. The reviewer picks one by name, and the first one in the file is the default. A preview is served on `$PORT` (3000 in the preview's own network namespace), and the review waits for `ready` to answer. One preview with an invalid `ready` stops all of them, with the reason.
- **Needs.** Review grants no network host and no secret. When the file asks for any, the check run says so: checks run offline and without secrets.
- **Fingerprints.** For each check, a local review compares its fingerprint with the verify record `lc verify` left for the same commit, and says whether they match, which parts differ and whether the record included uncommitted changes. Hosted reviews do not see local records.
- **An invalid file** runs no check. The plan entry shows the parse error, with the file and the key.

Without the file nothing changes, and `.lemoncrow/review.yaml` stays in charge. While repositories migrate, `review.yaml` sections still apply when `environment.toml` lacks the matching section:

| `environment.toml` | Sets aside in `review.yaml` |
| --- | --- |
| `[[checks]]` (or an invalid file) | `check_recipes` and recipe discovery; `check_policy` still applies |
| `[previews]` | `interactive_preview` and `runtimes` |

A Workspaces server (Enterprise's) builds each agent task's sandbox from the same file, from the task's own head.

There, `[[dependencies]]`, checks and previews reach the network only through the task's egress gateway: the organization's registries and the `needs.network` hosts it grants. The default registries are npm, PyPI, Go modules (`proxy.golang.org`, `sum.golang.org`), crates.io, Maven Central (`repo.maven.apache.org`, `repo1.maven.org`), Gradle (`plugins.gradle.org`, `services.gradle.org`) and RubyGems. `github.com` is not one of them, so a Gradle wrapper must find its distribution in the image. The proxy variables, `NODE_USE_ENV_PROXY=1` (for Node's own `fetch` and corepack), and a Maven `settings.xml` and Gradle `gradle.properties` under `HOME` point the usual tools at the gateway; a command that sets its own `MAVEN_OPTS`, `GRADLE_USER_HOME` or Maven settings file (`-s`) replaces them.

See [Workspaces and Review](../architecture/workspaces-and-review.md) for the design.
