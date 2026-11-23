"""``lc verify init`` against real Git repositories, and digest pinning against a fake local registry."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import socket
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from lemoncrow_client.environment import ENVIRONMENT_FILE, Dependency, load_environment, parse_environment
from lemoncrow_client.image_registry import RegistryError, parse_reference, resolve_digest
from lemoncrow_client.verify import verify_main
from lemoncrow_client.verify_init import detect, init_main

MANIFEST = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[]}'
DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()
OLD = "sha256:" + "1" * 64
PYTHON_FILES = ("*.py", "*.pyi", "pyproject.toml", "setup.cfg")


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "gitconfig"
    config.write_text("[user]\n\tname = Tester\n\temail = <redacted-email>\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def make_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def checks(top: Path) -> list[tuple[str, str, str, tuple[str, ...]]]:
    return [(check.id, check.kind, check.run, check.paths) for check in detect(top).checks]


def init(top: Path, *arguments: str) -> tuple[int, str]:
    sink = io.StringIO()
    return init_main(list(arguments), sink, cwd=top), sink.getvalue()


MAKEFILE = """\
.PHONY: test lint check format pre-commit
test: ## run the tests
\tuv run pytest -q
lint:
\tuv run ruff check .
format:
\tuv run ruff format .
check: lint test
pre-commit:
\t@$(MAKE) --no-print-directory format
\t@$(MAKE) --no-print-directory test
"""

PYPROJECT = """\
[project]
name = "demo"

[tool.pytest.ini_options]
addopts = "-q"

[tool.ruff]
line-length = 100

[tool.mypy]
strict = true

[dependency-groups]
dev = ["mypy", "pytest", "ruff"]
"""


def test_makefile_targets_come_first_and_pyproject_only_fills_a_missing_kind(tmp_path: Path) -> None:
    top = make_repo(tmp_path / "repo", {"Makefile": MAKEFILE, "pyproject.toml": PYPROJECT, "uv.lock": ""})

    assert checks(top) == [
        ("lint", "lint", "make lint", (".",)),
        ("mypy", "typecheck", "uv run mypy .", (*PYTHON_FILES, "mypy.ini", ".mypy.ini")),
        ("test", "test", "make test", (".",)),
    ]  # `check` only runs lint and test; `pre-commit` runs `format`, which rewrites files; pytest and ruff are in


def test_package_json_scripts_run_with_the_package_manager_the_repository_uses(tmp_path: Path) -> None:
    scripts = {
        "test": "vitest run",
        "test:watch": "vitest --watch",
        "lint": "eslint . --fix",
        "typecheck": "tsc --noEmit",
        "format:check": "prettier --check .",
        "dev": "vite",
    }
    top = make_repo(tmp_path / "repo", {"package.json": json.dumps({"scripts": scripts}), "pnpm-lock.yaml": ""})

    assert [(check_id, kind, run) for check_id, kind, run, _paths in checks(top)] == [
        ("format-check", "format", "pnpm run format:check"),
        ("typecheck", "typecheck", "pnpm run typecheck"),
        ("test", "test", "pnpm run test"),
    ]  # `lint` rewrites files (--fix), `test:watch` never exits, `dev` is no check

    placeholder = {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}
    assert checks(make_repo(tmp_path / "empty", {"package.json": json.dumps(placeholder)})) == []


WORKFLOW = """\
name: ci
on: [push]
defaults:
  run:
    shell: bash
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: uv run ruff check .
      - run: |
          cd web
          npm ci
          npm test
      - name: Types
        working-directory: web
        run: npx tsc --noEmit
      - run: pytest ${{ matrix.args }}
      - run: go test ./... | tee out.txt
      - run: |
          if [ "$RUNNER_OS" = Linux ]; then
            cargo test
          fi
      - shell: pwsh
        run: cargo test
      - continue-on-error: true
        run: cargo clippy
      - working-directory: missing
        run: cargo check
"""


def test_workflow_run_steps_are_read_only_where_that_is_safe(tmp_path: Path) -> None:
    top = make_repo(tmp_path / "repo", {".github/workflows/ci.yml": WORKFLOW, "web/package.json": "{}"})

    detection = detect(top)

    assert [(check.id, check.kind, check.run, check.paths[:2]) for check in detection.checks] == [
        ("ruff", "lint", "uv run ruff check .", ("*.py", "*.pyi")),
        ("web-tsc", "typecheck", "cd web && npx tsc --noEmit", ("web/*.js", "web/*.jsx")),
        ("web-test", "test", "cd web && npm test", ("web",)),
    ]  # an expression, a pipe: that command is left out; a branch, pwsh, continue-on-error, no such folder: the step
    assert detection.notes == [
        ".github/workflows/ci.yml: 4 run step(s) not read (a branch or loop, another shell, an expression, "
        "continue-on-error, or a working directory that is unclear or missing)"
    ]


def test_pyproject_tools_run_through_the_projects_runner(tmp_path: Path) -> None:
    pyproject = '[project]\nname = "demo"\n\n[dependency-groups]\ndev = ["pytest>=8", "ruff"]\n'
    top = make_repo(tmp_path / "repo", {"pyproject.toml": pyproject, "poetry.lock": ""})

    assert [(check_id, kind, run) for check_id, kind, run, _paths in checks(top)] == [
        ("ruff", "lint", "poetry run ruff check ."),
        ("pytest", "test", "poetry run pytest"),
    ]


def test_with_a_lockfile_a_tool_runs_only_where_the_project_installs_it(tmp_path: Path) -> None:
    pyproject = """\
[project]
name = "demo"
optional-dependencies.types = ["pyright"]

[dependency-groups]
tests = ["pytest>9"]
lint = ["ruff"]
dev = [{ include-group = "lint" }]

[tool.mypy]
strict = true
"""
    top = make_repo(tmp_path / "repo", {"pyproject.toml": pyproject, "uv.lock": ""})

    assert [(check_id, kind, run) for check_id, kind, run, _paths in checks(top)] == [
        ("ruff", "lint", "uv run ruff check ."),
        ("pyright", "typecheck", "uv run --extra types pyright"),
        ("pytest", "test", "uv run --group tests pytest"),
    ]  # dev, which uv installs by default, includes lint; mypy is configured, but nothing installs it


def test_a_build_manifest_gives_a_test_only_when_nothing_else_did(tmp_path: Path) -> None:
    def found(top: Path) -> list[tuple[str, str, str]]:
        return [(check_id, kind, run) for check_id, kind, run, _paths in checks(top)]

    rust_ci = CI_STEPS + "      - run: cargo fmt -- --check\n      - run: cargo test --locked ${{ env.FEATURES }}\n"
    rust = make_repo(tmp_path / "rust", {"Cargo.toml": "[package]\n", ".github/workflows/ci.yml": rust_ci})
    assert found(rust) == [("cargo-fmt", "format", "cargo fmt -- --check"), ("cargo-test", "test", "cargo test")]

    assert found(make_repo(tmp_path / "go", {"go.mod": "module x\n"})) == [("go-test", "test", "go test ./...")]
    maven = make_repo(tmp_path / "maven", {"pom.xml": "<project/>\n", "mvnw": ""})
    assert found(maven) == [("maven", "test", "./mvnw -B test")]
    gradle_ci = CI_STEPS + "      - run: ./gradlew build\n"
    gradle = make_repo(
        tmp_path / "gradle", {"build.gradle.kts": "", "gradlew": "", ".github/workflows/ci.yml": gradle_ci}
    )
    assert found(gradle) == [("gradle", "build", "./gradlew build")]  # it runs Gradle already
    made = make_repo(tmp_path / "made", {"go.mod": "module x\n", "Makefile": "test:\n\tgo test -race ./...\n"})
    assert found(made) == [("test", "test", "make test")]


CI_STEPS = "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"


def test_make_options_and_rake_in_ci_are_read_as_they_run(tmp_path: Path) -> None:
    steps = [
        "make -j -C tests test-lz4c32 V=1",
        "make -j 4 -C tests test",
        "bundle exec rake",
        "bundle exec rake release",
    ]
    workflow = CI_STEPS + "".join(f"      - run: {step}\n" for step in steps)
    top = make_repo(tmp_path / "repo", {".github/workflows/ci.yml": workflow, "tests/Makefile": ""})

    assert [(check_id, kind, run) for check_id, kind, run, _paths in checks(top)] == [
        ("tests-test", "test", "make -j 4 -C tests test"),
        ("rake", "test", "bundle exec rake"),
    ]  # `-j` takes no count before `-C`: `test-lz4c32`, no check by its name, runs in tests; `rake release` is none


def test_a_ci_step_that_runs_only_scripts_already_in_is_left_out(tmp_path: Path) -> None:
    scripts = {
        "check": "npm run check:type && npm run check:lint",
        "check:type": "tsc --noEmit",
        "check:lint": "eslint .",
    }
    workflow = CI_STEPS + "      - run: npm run check\n"
    top = make_repo(
        tmp_path / "repo", {".github/workflows/ci.yml": workflow, "package.json": json.dumps({"scripts": scripts})}
    )

    assert [(check_id, kind, run) for check_id, kind, run, _paths in checks(top)] == [
        ("check-lint", "lint", "npm run check:lint"),
        ("check-type", "typecheck", "npm run check:type"),
    ]  # the `check` script, and the CI step that runs it, only run these two


def test_root_lockfiles_review_provisions_become_dependencies(tmp_path: Path, registry: Registry) -> None:
    files = {"Makefile": MAKEFILE, "package-lock.json": "{}", "uv.lock": "", "pnpm-lock.yaml": ""}
    top = make_repo(tmp_path / "repo", files)

    code, output = init(top)

    assert code == 0, output
    environment = load_environment(top)
    assert environment is not None
    assert environment.dependencies == (
        Dependency("package-lock.json", "npm ci"),
        Dependency("uv.lock", "uv sync --frozen"),
    )
    assert "a server installs dependencies first: npm ci (package-lock.json)\n" in output
    assert "note: pnpm-lock.yaml: not written as [[dependencies]]: Review provisions only" in output

    code, output = init(top, "--toolchain", f"{registry.host}/team/python:3.12")

    assert code == 0, output
    after = load_environment(top)
    assert after is not None and after.toolchain is not None
    assert (after.dependencies, after.checks) == (environment.dependencies, environment.checks)


def test_init_writes_a_definition_the_loader_reads(tmp_path: Path) -> None:
    top = make_repo(tmp_path / "repo", {"Makefile": MAKEFILE})

    code, output = init(top)

    assert code == 0, output
    environment = load_environment(top)
    assert environment is not None and environment.toolchain is None
    assert [(check.id, check.run) for check in environment.checks] == [("lint", "make lint"), ("test", "make test")]
    assert "lc verify init --toolchain IMAGE" in (top / ENVIRONMENT_FILE).read_text()
    assert "commit the file, then run `lc verify hook install`" in output


def test_an_existing_definition_is_left_alone(tmp_path: Path) -> None:
    top = make_repo(tmp_path / "repo", {"Makefile": MAKEFILE, ENVIRONMENT_FILE: "# mine\nversion = 1\n"})

    for arguments in ((), ("--print",)):
        code, output = init(top, *arguments)
        assert code == 0 and "already exists, so it was left alone" in output

    assert (top / ENVIRONMENT_FILE).read_bytes() == b"# mine\nversion = 1\n"


def test_print_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    top = make_repo(tmp_path / "repo", {"Makefile": MAKEFILE})
    monkeypatch.chdir(top)

    sink = io.StringIO()
    assert verify_main(["init", "--print"], sink) == 0  # through `lc verify`

    assert [check.id for check in parse_environment(sink.getvalue()).checks] == ["lint", "test"]
    assert not (top / ".lemoncrow").exists()


# --------------------------------------------------------------------------- #
# A registry on 127.0.0.1 that wants an anonymous Bearer token, as Docker Hub and ghcr.io do
# --------------------------------------------------------------------------- #


class Registry:
    def __init__(self, server: http.server.ThreadingHTTPServer) -> None:
        self.server = server
        self.host = f"127.0.0.1:{server.server_address[1]}"
        self.head_digest = True
        self.requests: list[str] = []


@pytest.fixture
def registry() -> Iterator[Registry]:
    state: list[Registry] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return None

        def answer(self, body: bool) -> None:
            fake = state[0]
            fake.requests.append(f"{self.command} {self.path}")
            if self.path.startswith("/token?"):
                payload = json.dumps({"token": "t0k"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.headers.get("Authorization") != "Bearer t0k":
                self.send_response(401)
                realm = f"http://{fake.host}/token"
                challenge = f'Bearer realm="{realm}",service="fake",scope="repository:team/python:pull"'
                self.send_header("WWW-Authenticate", challenge)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path not in {"/v2/team/python/manifests/3.12", f"/v2/team/python/manifests/{DIGEST}"}:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.oci.image.index.v1+json")
            self.send_header("Content-Length", str(len(MANIFEST)))
            if fake.head_digest or body:
                self.send_header("Docker-Content-Digest", DIGEST)
            self.end_headers()
            if body:
                self.wfile.write(MANIFEST)

        def do_HEAD(self) -> None:
            self.answer(body=False)

        def do_GET(self) -> None:
            self.answer(body=True)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.append(Registry(server))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state[0]
    finally:
        server.shutdown()
        server.server_close()


def test_digest_comes_from_the_registry_through_an_anonymous_token(registry: Registry) -> None:
    assert resolve_digest(parse_reference(f"{registry.host}/team/python:3.12"), timeout=5) == DIGEST
    assert registry.requests == [
        "HEAD /v2/team/python/manifests/3.12",
        "GET /token?service=fake&scope=repository%3Ateam%2Fpython%3Apull",
        "HEAD /v2/team/python/manifests/3.12",
    ]

    registry.head_digest = False  # some registries name the digest only with the manifest: it is hashed
    assert resolve_digest(parse_reference(f"{registry.host}/team/python:3.12"), timeout=5) == DIGEST
    assert registry.requests[-1] == "GET /v2/team/python/manifests/3.12"

    with pytest.raises(RegistryError, match=r"no manifest for 3\.11 "):
        resolve_digest(parse_reference(f"{registry.host}/team/python:3.11"), timeout=5)
    assert resolve_digest(parse_reference(f"{registry.host}/team/python:3.12@{DIGEST}"), timeout=5) == DIGEST
    with pytest.raises(RegistryError, match="no manifest for sha256:1111"):
        resolve_digest(parse_reference(f"{registry.host}/team/python:3.12@{OLD}"), timeout=5)


DEFINED = f"""\
# Ours.
version = 1

[toolchain]
image = "python:3.11@{OLD}"  # the old one

# Lint the code.
[[checks]]
id = "lint"
paths = ["*.py"]
run = "make lint"
"""


def test_toolchain_replaces_only_the_toolchain_and_shows_the_change(tmp_path: Path, registry: Registry) -> None:
    image = f"{registry.host}/team/python:3.12"
    top = make_repo(tmp_path / "repo", {ENVIRONMENT_FILE: DEFINED})
    path = top / ENVIRONMENT_FILE
    replaced = DEFINED.replace(f'"python:3.11@{OLD}"', f'"{image}@{DIGEST}"')  # the comment stays

    code, output = init(top, "--toolchain", image, "--print")
    assert (code, output) == (0, replaced)
    assert path.read_text() == DEFINED  # --print wrote nothing

    code, output = init(top, "--toolchain", image)
    assert code == 0, output
    assert path.read_text() == replaced
    assert f'-image = "python:3.11@{OLD}"  # the old one\n+image = "{image}@{DIGEST}"  # the old one\n' in output
    environment = load_environment(top)
    assert environment is not None and environment.toolchain is not None
    assert environment.toolchain.image == f"{image}@{DIGEST}"


def test_toolchain_takes_the_place_of_the_comment_init_wrote(tmp_path: Path, registry: Registry) -> None:
    top = make_repo(tmp_path / "repo", {"Makefile": MAKEFILE})
    assert init(top)[0] == 0
    path = top / ENVIRONMENT_FILE
    before = path.read_text()

    code, output = init(top, "--toolchain", f"{registry.host}/team/python:3.12")

    assert code == 0, output
    after = path.read_text()
    table = f'[toolchain]\nimage = "{registry.host}/team/python:3.12@{DIGEST}"\n'
    assert table in after and "# No [toolchain]" not in after
    assert parse_environment(after).checks == parse_environment(before).checks
    start = before.index("# No [toolchain]")
    end = before.index("--toolchain IMAGE\n", start) + len("--toolchain IMAGE\n")
    assert after == before[:start] + table + before[end:]


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.parametrize("missing", ["tag", "registry"])
def test_a_digest_that_cannot_be_resolved_writes_nothing(tmp_path: Path, registry: Registry, missing: str) -> None:
    image = f"{registry.host}/team/python:3.11" if missing == "tag" else f"127.0.0.1:{closed_port()}/team/python:3.12"
    top = make_repo(tmp_path / "repo", {"Makefile": MAKEFILE})

    code, output = init(top, "--toolchain", image)

    assert code == 2
    assert f"could not pin {image}: " in output and output.endswith("Nothing was written.\n")
    assert not (top / ".lemoncrow").exists()
