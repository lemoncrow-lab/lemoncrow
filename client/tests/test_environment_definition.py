"""The sections of ``.lemoncrow/environment.toml`` past ``checks``, the fingerprint and the verify record."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from lemoncrow_client.environment import (
    MAX_CHECK_TIMEOUT,
    VERIFY_RECORD_DIR,
    Check,
    DefinitionError,
    Fingerprint,
    Needs,
    check_digest,
    image_digest,
    is_attr_pathspec,
    parse_environment,
    read_verify_record,
    write_verify_record,
)

DIGEST = "sha256:" + "a" * 64
FULL = f"""\
version = 1

[toolchain]
image = "ghcr.io/example/toolchain-node22@{DIGEST}"

[[dependencies]]
lockfile = "package-lock.json"
install = "npm ci --no-audit --no-fund"

[[dependencies]]
lockfile = "api/uv.lock"
install = "uv sync --frozen"

[services.postgres]
image = "postgres:16@{DIGEST}"
ready = "pg_isready -U postgres"

[[checks]]
id = "typecheck"
paths = ["*.ts"]
kind = "typecheck"
run = "npm run -s typecheck"

[[checks]]
id = "tests"
paths = ["src", ":!docs"]
kind = "test"
timeout = 600
needs = ["postgres"]
run = "npm test"

[previews.web]
run = "npm run dev -- --port \\"$PORT\\""
ready = "/"

[needs]
network = ["api.example.com"]
secrets = ["EXAMPLE_API_KEY"]
"""


def test_every_section_parses_into_its_own_shape() -> None:
    env = parse_environment(FULL)
    assert env.toolchain is not None and env.toolchain.digest == DIGEST
    assert [(d.lockfile, d.install) for d in env.dependencies] == [
        ("package-lock.json", "npm ci --no-audit --no-fund"),
        ("api/uv.lock", "uv sync --frozen"),
    ]
    (postgres,) = env.services
    assert (postgres.name, postgres.ready, postgres.digest) == ("postgres", "pg_isready -U postgres", DIGEST)
    typecheck, tests = env.checks
    assert (typecheck.kind, typecheck.timeout, typecheck.needs) == ("typecheck", None, ())
    assert (tests.kind, tests.timeout, tests.needs) == ("test", 600, ("postgres",))
    (web,) = env.previews
    assert (web.name, web.run, web.ready) == ("web", 'npm run dev -- --port "$PORT"', "/")
    assert env.needs == Needs(network=("api.example.com",), secrets=("EXAMPLE_API_KEY",))
    assert env.unpinned_images() == () and env.service("postgres") is postgres and env.check("tests") is tests
    # Checks alone stay valid; the other sections default to empty.
    bare = parse_environment('version = 1\n[[checks]]\nid = "a"\npaths = ["src"]\nrun = "true"\n')
    assert (bare.toolchain, bare.dependencies, bare.services, bare.previews, bare.needs) == (None, (), (), (), Needs())


def test_an_unpinned_image_is_accepted_and_named() -> None:
    text = FULL.replace(f"toolchain-node22@{DIGEST}", "toolchain-node22:latest").replace(f"16@{DIGEST}", "16")
    env = parse_environment(text)
    assert env.unpinned_images() == ("ghcr.io/example/toolchain-node22:latest", "postgres:16")
    assert env.toolchain is not None and env.toolchain.digest == ""
    assert image_digest(f"localhost:5000/a/b:1@{DIGEST}") == DIGEST and image_digest("a@sha256:abc") == ""


CHECK = '[[checks]]\nid = "c"\npaths = ["src"]\nrun = "true"\n'


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[toolchain]\nimage = "x"\nos = "linux"\n', "toolchain: unknown key 'os'"),
        ("[toolchain]\n", "toolchain: image: must be an image reference"),
        ('[toolchain]\nimage = "has space"\n', "toolchain: image: must be an image reference"),
        ('[toolchain]\nimage = "example/UPPER"\n', "toolchain: image"),
        ('toolchain = "x"\n', "toolchain: must be a table"),
        ('[[dependencies]]\nlockfile = "../uv.lock"\ninstall = "x"\n', "dependencies[0]: lockfile must be"),
        ('[[dependencies]]\nlockfile = "/uv.lock"\ninstall = "x"\n', "dependencies[0]: lockfile must be"),
        ('[[dependencies]]\nlockfile = "./uv.lock"\ninstall = "x"\n', "dependencies[0]: lockfile must be"),
        ('[[dependencies]]\nlockfile = "uv.lock"\n', "dependencies[0] (uv.lock): install must be"),
        (
            '[[dependencies]]\nlockfile = "uv.lock"\ninstall = "x"\n[[dependencies]]\nlockfile = "uv.lock"\ninstall = "y"\n',
            "dependencies[1]: duplicate lockfile 'uv.lock'",
        ),
        ('[[dependencies]]\nlockfile = "uv.lock"\ninstall = "x"\nhash = "y"\n', "dependencies[0]: unknown key 'hash'"),
        ('[dependencies]\nlockfile = "uv.lock"\n', "dependencies must be an array of tables"),
        ('[services.db]\nimage = "postgres"\n', "services.db: ready must be"),
        ('[services.db]\nready = "true"\n', "services.db: image: must be"),
        ('[services.db]\nimage = "postgres"\nready = "true"\nports = [5432]\n', "services.db: unknown key 'ports'"),
        ('[services.DB]\nimage = "postgres"\nready = "true"\n', "services.DB: the name must match"),
        ('[[services]]\nimage = "postgres"\n', "services must be tables ([services.<name>])"),
        ('[previews.web]\nrun = "x"\nready = "health"\n', "previews.web: ready must be a path"),
        ('[previews.web]\nrun = "x"\nready = "//evil.example"\n', "previews.web: ready must be a path"),
        ('[previews.web]\nready = "/"\n', "previews.web: run must be"),
        ('[previews.web]\nrun = "x"\nready = "/"\nport = 3000\n', "previews.web: unknown key 'port'"),
        ('[needs]\nnetwork = ["https://api.example.com"]\n', "needs: network: 'https://api.example.com'"),
        ('[needs]\nnetwork = ["a.example", "a.example"]\n', "needs: network: 'a.example' is listed twice"),
        ('[needs]\nsecrets = ["api_key"]\n', "needs: secrets: 'api_key' is not an environment variable"),
        ('[needs]\nservices = ["db"]\n', "needs: unknown key 'services'"),
        ('needs = ["db"]\n', "needs: must be a table"),
        (CHECK + 'kind = "unit"\n', "checks[0] (c): kind must be one of test, lint"),
        (CHECK + "timeout = 0\n", "checks[0] (c): timeout must be a whole number"),
        (CHECK + f"timeout = {MAX_CHECK_TIMEOUT + 1}\n", "timeout must be a whole number of seconds from 1"),
        (CHECK + "timeout = 1.5\n", "timeout must be a whole number"),
        (CHECK + "timeout = true\n", "timeout must be a whole number"),
        (CHECK + 'needs = ["db"]\n', "checks[0] (c): needs names 'db', which is not declared under [services]"),
        (CHECK + 'needs = "db"\n', "checks[0] (c): needs: must be an array of service names"),
        ('colour = "red"\n', "unknown key 'colour'"),
    ],
)
def test_every_section_is_strict_and_names_the_file_and_key(text: str, message: str) -> None:
    with pytest.raises(DefinitionError, match=re.escape(message)) as raised:
        parse_environment("version = 1\n" + text, source="repo/.lemoncrow/environment.toml")
    assert str(raised.value).startswith("repo/.lemoncrow/environment.toml: ")


def test_the_bounds_hold_at_their_edges() -> None:
    edge = parse_environment(f"version = 1\n{CHECK}timeout = {MAX_CHECK_TIMEOUT}\n")
    assert edge.checks[0].timeout == MAX_CHECK_TIMEOUT
    assert parse_environment(f"version = 1\n{CHECK}timeout = 1\n").checks[0].timeout == 1
    long_host = ".".join(["a" * 63] * 3) + ".bcd"  # each label at its 63-character limit
    assert parse_environment(f'version = 1\n[needs]\nnetwork = ["{long_host}"]\n').needs.network == (long_host,)
    with pytest.raises(DefinitionError, match="is not a lowercase host name"):
        parse_environment(f'version = 1\n[needs]\nnetwork = ["{"a" * 64}.example"]\n')


def test_attr_magic_is_recognised_only_in_its_long_form() -> None:
    assert is_attr_pathspec(":(attr:generated)") and is_attr_pathspec(":(top,attr:!vendored)")
    assert not is_attr_pathspec("attr:generated") and not is_attr_pathspec(":!docs") and not is_attr_pathspec(":(top)x")


def test_the_fingerprint_covers_image_toolchain_lockfiles_and_check() -> None:
    env = parse_environment(FULL)
    tests = env.checks[1]
    files = {"package-lock.json": b"{}", "api/uv.lock": b"lock"}
    first = env.fingerprint(tests, files.get)
    assert first.image_digest == DIGEST and first.toolchain.startswith("ghcr.io/example/toolchain-node22@")
    assert first.reproducible and first.digest.startswith("envdef:sha256:")
    assert env.fingerprint(tests, files.get) == first  # deterministic
    # Each component moves the digest on its own.
    moved = env.fingerprint(tests, {**files, "api/uv.lock": b"other"}.get)
    assert moved.differences(first) == ("lockfile_digest",) and moved.digest != first.digest
    missing = env.fingerprint(tests, {"package-lock.json": b"{}"}.get)
    assert missing.lockfile_digest not in {"", first.lockfile_digest}
    retimed = parse_environment(FULL.replace("timeout = 600", "timeout = 601"))
    assert retimed.fingerprint(retimed.checks[1], files.get).differences(first) == ("check_digest",)
    reimaged = parse_environment(FULL.replace(f'"postgres:16@{DIGEST}"', f'"postgres:17@{DIGEST}"'))
    assert check_digest(reimaged, reimaged.checks[1]) != first.check_digest  # a needed service's image counts
    # The id is presentation; renaming a check keeps its digest.
    assert check_digest(env, Check(**{**tests.__dict__, "id": "renamed"})) == first.check_digest
    # No toolchain and no dependencies: empty components, still a fingerprint.
    bare = parse_environment(f"version = 1\n{CHECK}")
    plain = bare.fingerprint(bare.checks[0], lambda _path: None)
    assert (plain.image_digest, plain.toolchain, plain.lockfile_digest) == ("", "", "") and plain.reproducible
    assert Fingerprint.from_wire(first.to_wire()) == first
    assert Fingerprint.from_wire({**first.to_wire(), "check_digest": "sha256:forged"}) is None
    assert Fingerprint.from_wire({**first.to_wire(), "version": 2}) is None


def test_the_verify_record_round_trips_and_keeps_only_the_newest(tmp_path: Path) -> None:
    env = parse_environment(FULL)
    fingerprint = env.fingerprint(env.checks[0], lambda _path: b"x")
    commits = [f"{index:040x}" for index in range(5)]
    for index, commit in enumerate(commits):
        path = write_verify_record(tmp_path, commit, {"typecheck": (fingerprint, "passed")}, dirty=index == 4, keep=3)
        assert path is not None
        os.utime(path, ns=(index * 10**9, index * 10**9))
    kept = sorted(item.stem for item in (tmp_path / VERIFY_RECORD_DIR).glob("*.json"))
    assert kept == sorted(commits[2:])
    record = read_verify_record(tmp_path, commits[4])
    assert record == {
        "commit": commits[4],
        "dirty": True,
        "checks": {"typecheck": {"status": "passed", "fingerprint": fingerprint}},
    }
    assert read_verify_record(tmp_path, commits[0]) is None  # pruned
    assert write_verify_record(tmp_path, "not-a-commit", {}, dirty=False) is None
    assert read_verify_record(tmp_path, "../../etc/passwd") is None
    # A tampered fingerprint is left out, never trusted.
    stored = tmp_path / VERIFY_RECORD_DIR / f"{commits[3]}.json"
    document = json.loads(stored.read_text())
    document["checks"]["typecheck"]["fingerprint"]["lockfile_digest"] = "sha256:other"
    stored.write_text(json.dumps(document))
    assert read_verify_record(tmp_path, commits[3]) == {"commit": commits[3], "dirty": False, "checks": {}}
