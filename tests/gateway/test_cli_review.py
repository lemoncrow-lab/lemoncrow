"""``lc review`` CLI surface: registration, JSON shape, and clean failure.

The error paths matter as much as the happy one here -- a review command that
dumps a traceback when it is run outside a repository is worse than useless in
a hook or a CI step.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import click
import pygit2
import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli

_WORKSPACE_ENV_VARS = (
    "CURSOR_WORKSPACE_ROOT",
    "LEMONCROW_WORKSPACE_ROOT",
    "CLAUDE_WORKSPACE_ROOT",
    "VSCODE_CWD",
)


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reject ast-grep discovery before it can reach the managed download path.

    ``lc review`` runs the impact detectors, and ast-grep's managed bootstrap
    would try to *download* a binary into each throwaway repo. ``sg`` is the one
    candidate name ``_reject_reason`` refuses outright.
    """

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


@pytest.fixture(autouse=True)
def _review_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Run one isolated real loopback server for each CLI Review test.

    These are HTTP integration tests, not unit tests: repeated invocations inside
    one test share durable Review state, while every test gets a fresh data plane.
    Service-process isolation itself is covered by the server suite, so the test
    server keeps Review/Dashboard in-process to avoid multiplying subprocesses.

    A tiny generated frontend fixture is sufficient here: these tests exercise the
    CLI/API boundary and do not need a production frontend build.
    """

    repo_root = Path(__file__).resolve().parents[2]
    python = Path(sys.executable)
    frontend = tmp_path / "frontend-dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html><title>Review test</title>", encoding="utf-8")
    assert python.is_file(), "test Python is missing"
    assert (frontend / "index.html").is_file(), "Review test frontend fixture is missing"

    log = (tmp_path / "server.log").open("w", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (
            str(repo_root / "server" / "src"),
            str(repo_root / "src"),
            str(repo_root / "client" / "src"),
            env.get("PYTHONPATH", ""),
        )
        if value
    )
    runtime_home = tmp_path / "client-home"
    runtime_home.mkdir()
    env["LEMONCROW_HOME"] = str(runtime_home)
    token_file = tmp_path / "machine-token"
    process = subprocess.Popen(
        [
            str(python),
            "-m",
            "lemoncrow_server_core",
            "up",
            "--directory",
            str(tmp_path / "server"),
            "--port",
            "0",
            "--token-file",
            str(token_file),
            "--frontend-dir",
            str(frontend),
            "--allow-local-fs",
            "--in-process-services",
        ],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    deadline = time.monotonic() + 10.0
    url = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.flush()
            raise AssertionError(f"Review test server exited early; see {log.name}")
        ready, _, _ = select.select([process.stdout], [], [], 0.2)
        if not ready:
            continue
        candidate = process.stdout.readline().strip()
        if re.fullmatch(r"http://127\.0\.0\.1:\d+", candidate):
            url = candidate
            log.write(candidate + "\n")
            log.flush()
            break
    else:
        process.terminate()
        raise AssertionError(f"Review test server did not publish its bound URL; see {log.name}")

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Review test server exited early; see {log.name}")
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=0.2) as response:
                if response.status != 200:
                    continue
            token = token_file.read_text(encoding="utf-8").strip()
            request = urllib.request.Request(f"{url}/api/reviews", headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(request, timeout=0.3) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            time.sleep(0.03)
    else:
        process.terminate()
        raise AssertionError(f"Review API did not become ready; see {log.name}")

    assert token_file.is_file(), "Review test token file is missing"
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", url)
    monkeypatch.delenv("LEMONCROW_TOKEN", raising=False)
    monkeypatch.setenv("LEMONCROW_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("LEMONCROW_LOCAL_FS", "1")
    monkeypatch.setenv("LEMONCROW_HOME", str(runtime_home))

    try:
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        log.close()


def _ctx() -> click.Context:
    return click.Context(cli, info_name="lc")


def _signature(offset: int) -> pygit2.Signature:
    return pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000 + offset, 0)


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = _signature(offset)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


def _fixture_repo(tmp_path: Path, *, identity_marker: str = "") -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.com"

    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    if identity_marker:
        (root / ".fixture-id").write_text(identity_marker + "\n", encoding="utf-8")
    _commit(repo, "seed", 0)

    (root / "src" / "app.py").write_text("def one():\n    return 2\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
    _commit(repo, "change the return value", 60)
    return root


def _invoke(tmp_path: Path, args: list[str], *, suppress_default_open: bool = True) -> Any:
    runner = CliRunner()
    command_args = list(args)
    if suppress_default_open and "--open" not in command_args and "--no-open" not in command_args:
        command_args.append("--no-open")
    # Some A/B tests intentionally create two independent fixture repositories
    # with byte-identical synthetic root commits. Production correctly treats
    # identical root history as the same repo when no SCM identity is supplied;
    # the test needs to state that these sandboxes are distinct repositories.
    fixture_repo_id = hashlib.sha256(str(tmp_path.resolve()).encode("utf-8")).hexdigest()[:24]
    return runner.invoke(
        cli,
        ["--root", str(tmp_path / "store"), "review", *command_args],
        catch_exceptions=False,
        env={"LEMONCROW_SCM_PROVIDER": "fixture", "LEMONCROW_SCM_REPO_ID": fixture_repo_id},
    )


def test_lcr_watch_uses_real_local_oss_review_server_without_hosted_pairing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3j: thin lcr --once uses the installed local Review HTTP boundary.

    Autouse fixture starts one disposable loopback Review server. There is no
    Hosted login or paired Review machine; the local server token remains local.
    """
    from io import StringIO

    from lemoncrow_client.review import review_main

    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    assert os.environ.get("LEMONCROW_INSTALL_MODE") == "local"
    assert os.environ.get("LEMONCROW_URL", "").startswith("http://127.0.0.1:")
    assert not os.environ.get("LEMONCROW_TOKEN")  # only local server token file

    first = StringIO()
    assert review_main(["watch", "--once", "--json"], sink=first) == 0, first.getvalue()
    assert json.loads(first.getvalue())["event"] == "idle"

    source = repo / "src" / "app.py"
    source.write_text("def one():\n    return 42\n", encoding="utf-8")
    result = StringIO()
    assert review_main(["watch", "--once", "--json"], sink=result) == 0, result.getvalue()
    captured = json.loads(result.getvalue())
    assert captured["event"] == "captured"
    assert captured["review_url"].startswith(os.environ["LEMONCROW_URL"] + "/r/")
    assert captured["source_fingerprint"]
    assert "machine_token" not in result.getvalue()


def test_review_server_error_is_actionable_without_misleading_repairs() -> None:
    from types import SimpleNamespace

    from lemoncrow_client.errors import ErrorCode

    from lemoncrow.gateway.cli.commands.review import _review_server_click_error

    error = _review_server_click_error(
        SimpleNamespace(message="internal error (correlation_id=test)", code=ErrorCode.INTERNAL),
        hosted=False,
    )
    assert str(error) == "Review server failed: internal error (correlation_id=test)"

    too_large = SimpleNamespace(
        message="request body exceeds the server limit",
        details={"limit": 4194304, "declared": 4804037},
        code=ErrorCode.REQUEST_TOO_LARGE,
    )
    assert str(_review_server_click_error(too_large, hosted=False)) == (
        "Review server failed: request body exceeds the server limit (4,804,037 bytes sent, limit 4,194,304)"
    )

    timeout = SimpleNamespace(
        message="cannot reach the LemonCrow server at http://127.0.0.1:7420: timed out after 10s",
        details={"url": "http://127.0.0.1:7420", "timed_out": True},
        code=ErrorCode.SERVER_UNREACHABLE,
    )
    timeout_message = str(_review_server_click_error(timeout, hosted=False))
    assert "may be busy rather than offline" in timeout_message
    assert "local_server.sh status" in timeout_message
    assert "local_server.sh restart" not in timeout_message

    offline = SimpleNamespace(message="connection refused", code=ErrorCode.SERVER_UNREACHABLE)
    local_message = str(_review_server_click_error(offline, hosted=False))
    assert "local_server.sh restart" in local_message
    assert "lc update --force" in local_message
    assert "local_server.sh restart" not in str(_review_server_click_error(offline, hosted=True))


def test_stale_review_base_error_tells_user_how_to_start_fresh() -> None:
    from types import SimpleNamespace

    from lemoncrow.gateway.cli.commands.review import _review_capture_value_error

    sha = "0efdeeb1ac1ba93e8415d0b2629729ffb1b3650c"
    rng = SimpleNamespace(mode="working_tree", base_sha=sha)
    error = _review_capture_value_error(
        ValueError(f"cannot resolve revision {sha!r}"),
        rng=rng,
        force_new_review=False,
    )
    message = str(error)
    assert "cannot continue the current Review" in message
    assert sha in message
    assert "lc review --new" in message

    explicit_range = SimpleNamespace(mode="commit_range", base_sha=sha)
    ordinary = _review_capture_value_error(
        ValueError(f"cannot resolve revision {sha!r}"),
        rng=explicit_range,
        force_new_review=False,
    )
    assert str(ordinary) == f"cannot resolve revision {sha!r}"

    forced_new = _review_capture_value_error(
        ValueError(f"cannot resolve revision {sha!r}"),
        rng=rng,
        force_new_review=True,
    )
    assert "lc review --new" not in str(forced_new)

    # --continue/--working-tree/--staged switch to the saved base inside the
    # capture, so the lost revision is not this range's own base.
    frozen = "a32150" + "0" * 34
    switched = _review_capture_value_error(
        ValueError(f"cannot resolve revision {frozen!r}"),
        rng=SimpleNamespace(mode="staged", base_sha=sha),
        force_new_review=False,
    )
    assert frozen in str(switched)
    assert "lc review --new" in str(switched)
    unborn = _review_capture_value_error(ValueError("cannot resolve revision 'HEAD'"), rng=rng, force_new_review=False)
    assert str(unborn) == "cannot resolve revision 'HEAD'"


@pytest.mark.parametrize("check", [False, True])
def test_a_lost_saved_base_points_to_a_new_review_from_every_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: bool
) -> None:
    from lemoncrow.pro.capabilities.review import hosted as hosted_mod

    repo_root = _fixture_repo(tmp_path)
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    frozen = "a32150" + "0" * 34

    def lost_base(*args: Any, **kwargs: Any) -> None:
        raise ValueError(f"cannot resolve revision {frozen!r}")

    monkeypatch.setattr(hosted_mod, "capture_server_review", lost_base)
    arguments = ["--repo-root", str(repo_root), "--working-tree"]
    arguments += ["--check", "lint", "--run", "true"] if check else ["--track"]
    result = _invoke(tmp_path, arguments)

    assert result.exit_code != 0
    assert f"saved base revision {frozen!r} is no longer available" in result.output
    assert "lc review --new" in result.output


def test_review_json_shape(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["schema_version"] == 2
    for key in (
        "generated_at",
        "repo_root",
        "range_mode",
        "base_rev",
        "head_rev",
        "base_sha",
        "head_sha",
        "files",
        "symbols",
        "impact",
        "order",
        "provenance",
        "evidence",
        "index_status",
        "degraded",
        "stats",
    ):
        assert key in payload, key
    assert isinstance(payload["files"], list)
    assert payload["provenance"]["status"] == "unknown"
    assert set(payload["stats"]) == {"files", "additions", "deletions", "hunks", "symbols", "impact_sites"}


def test_review_json_round_trips_through_to_dict(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.packet import build_review_packet

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--json"])
    payload = json.loads(result.output)

    packet = build_review_packet(
        repo_root,
        resolve_rev_range(repo_root, "HEAD~1"),
        store_root=tmp_path / "store",
    )
    expected = json.loads(json.dumps(packet.to_dict(), default=str))
    # `generated_at` is a wall-clock stamp; everything else must be identical.
    payload.pop("generated_at")
    expected.pop("generated_at")
    assert payload == expected


def test_review_outside_git_repo_errors_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    for name in _WORKSPACE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(outside))

    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / "store"), "review", "--repo-root", str(outside)])
    assert result.exit_code != 0
    assert "not a git repository" in result.output
    assert "Traceback" not in result.output


def test_review_explicit_repo_root_never_falls_back_to_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``--repo-root`` that is not a checkout must fail, not review elsewhere.

    Regression guard: the workspace fallback used to apply to an explicitly
    named directory too, so pointing ``--repo-root`` at a non-repo silently
    produced a packet for whatever repository the *workspace* resolved to --
    a confident answer to a question nobody asked.
    """

    repo_root = _fixture_repo(tmp_path)
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    for name in _WORKSPACE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # The workspace *is* a real repository here -- the fallback would succeed.
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(repo_root))

    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / "store"), "review", "--repo-root", str(outside)])
    assert result.exit_code != 0
    assert "not a git repository" in result.output
    assert str(repo_root) not in result.output


def test_review_no_changes_exits_zero_with_hint(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD..HEAD", "--no-color"])
    assert result.exit_code == 0, result.output
    assert "no changes" in result.output


def test_review_text_output_lists_files(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--no-color"])
    assert result.exit_code == 0, result.output
    assert "src/app.py" in result.output
    assert "tests/test_app.py" in result.output
    assert "START HERE" in result.output
    assert "Human review  REQUIRED" in result.output


def test_review_rich_output_still_names_every_file(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1"])
    assert result.exit_code == 0, result.output
    assert "src/app.py" in result.output
    assert "Human review  REQUIRED" in result.output


def test_bare_review_reviews_the_uncommitted_working_tree(tmp_path: Path) -> None:
    """The 80% moment is "the agent stopped -- is this safe to commit?".

    That question is about the working tree, not a pull request, and it is the
    only mode whose caller line numbers are exact by construction: the index and
    the tree agree, so nothing has to be re-anchored to a blob.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["range_mode"] == "working_tree"
    assert payload["head_rev"] == "WORKDIR"
    assert "src/app.py" in [item["path"] for item in payload["files"]]

    # `--working-tree` is what installed scripts and the docs say; it must keep
    # meaning exactly what the bare form now means.
    explicit = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--json"])
    assert explicit.exit_code == 0, explicit.output
    assert json.loads(explicit.output)["files"] == payload["files"]


def test_bare_review_on_a_clean_tree_reviews_the_last_commit_and_says_so(tmp_path: Path) -> None:
    """An empty packet is not what someone who just committed wanted to see.

    The substitution is stated rather than performed silently: the range label
    alone leaves the reader to work out which change they are looking at.
    """

    repo_root = _fixture_repo(tmp_path)

    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--no-color"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "working tree clean — reviewing the last commit instead"
    assert "HEAD~1..HEAD" in result.output
    assert "no changes in this range" not in result.output

    # An explicit range substituted nothing, so it must not claim it did.
    explicit = _invoke(tmp_path, ["--repo-root", str(repo_root), "--base", "HEAD~1", "--no-color"])
    assert explicit.exit_code == 0, explicit.output
    assert "working tree clean" not in explicit.output


def test_review_bad_revision_errors_cleanly(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "no-such-ref"])
    assert result.exit_code != 0
    assert "cannot resolve revision" in result.output
    assert "Traceback" not in result.output


def test_review_no_impact_and_no_provenance_are_recorded(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "HEAD~1", "--json", "--no-impact", "--no-provenance"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "impact_disabled" in payload["degraded"]
    assert "provenance_disabled" in payload["degraded"]


# ---------------------------------------------------------------------------
# --track: durable review state through the CLI
# ---------------------------------------------------------------------------


def test_mark_state_choices_match_the_model() -> None:
    """The CLI's restated choice list may never drift from the real vocabulary."""

    from lemoncrow.gateway.cli.commands.review import _MARK_STATE_CHOICES
    from lemoncrow.pro.capabilities.review.session_models import MARK_STATES

    assert _MARK_STATE_CHOICES == MARK_STATES


# The plain block a tracked capture prints: one headline in the Reader's words, then the URL.
_HEADLINE = re.compile(r"^Revision \d+ · \d+ files? · \d+ parts? · \d+ reviewed$", re.MULTILINE)
_REVIEW_URL = re.compile(r"^Review  (?P<url>\S+/(?P<ref>r/\S+))$", re.MULTILINE)


def test_bare_review_does_not_persist_anything(tmp_path: Path) -> None:
    """Constraint 6: ``lc review`` with no flags is what it always was."""

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1"])
    assert result.exit_code == 0, result.output
    assert _HEADLINE.search(result.output) is None
    assert not (tmp_path / "store" / "lemoncrow_reviews.db").exists()


def test_track_records_a_session_and_reopens_it_on_the_next_run(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    first = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--track"])
    assert first.exit_code == 0, first.output
    assert _HEADLINE.search(first.output), first.output

    found = _REVIEW_URL.search(first.output)
    assert found, first.output
    second = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--track"])
    assert found["ref"] in second.output
    assert "Revision 1 · " in second.output


def test_a_tracked_capture_prints_a_plain_summary_and_keeps_raw_details_in_json(tmp_path: Path) -> None:
    """One headline in the Reader's words ("parts", Revision counted from 1), then the URL.

    Raw unit counts, unit kinds, the Revision id and the range mode are internal: they stay in ``--json``.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    text = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--track", "--no-color"])
    assert text.exit_code == 0, text.output
    headline = _HEADLINE.search(text.output)
    assert headline, text.output
    assert headline.group(0).startswith("Revision 1 · 1 file · ")
    assert headline.group(0).endswith(" · 0 reviewed")
    after = text.output[headline.end() :]
    assert _REVIEW_URL.search(after), text.output
    for jargon in (
        "REVIEW SESSION",
        "checkpoint rr/",
        " raw",
        "document_section",
        "review areas",
        "changed target",
        "working_tree)",
    ):
        assert jargon not in text.output, jargon

    as_json = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--track", "--json"])
    state = json.loads(as_json.stderr.strip().splitlines()[-1])
    assert state["revision_number"] == 0
    assert state["revision_id"].startswith("rr/")
    assert state["range_mode"] == "working_tree"
    assert state["unit_count"] >= state["target_count"] > 1
    assert state["unit_kinds"]["file"] == state["file_count"] == 1


def test_review_help_fits_one_screen_and_help_all_shows_everything(tmp_path: Path) -> None:
    runner = CliRunner()
    short = runner.invoke(cli, ["review", "--help"], catch_exceptions=False)
    assert short.exit_code == 0, short.output
    lines = short.output.splitlines()
    assert len(lines) <= 45, short.output
    for shown in ("--staged", "--json", "--no-open", "--repo-root", "--ai", "--help-all", "status [--json]"):
        assert shown in short.output, shown
    for example in ("lc review main...HEAD", "lc review status --json", "lc review --feedback", "--on src/auth.py:L81"):
        assert example in short.output, example
    for advanced in ("--mark-state", "--since-my-review", "--max-usd-per-pr", "--reproduce", "verify-record"):
        assert advanced not in short.output, advanced

    full = runner.invoke(cli, ["review", "--help-all"], catch_exceptions=False)
    assert full.exit_code == 0, full.output
    for advanced in ("--mark-state", "--since-my-review", "--max-usd-per-pr", "--reproduce", "verify-record"):
        assert advanced in full.output, advanced
    assert "--serve-workspace" not in full.output
    # Showing everything once must not leave the options visible for the next --help.
    again = runner.invoke(cli, ["review", "--help"], catch_exceptions=False)
    assert again.output == short.output


def test_status_reports_the_current_review_with_documented_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _fixture_repo(tmp_path)
    none = _invoke(tmp_path, ["status", "--repo-root", str(repo_root)], suppress_default_open=False)
    assert none.exit_code == 2, none.output
    assert "No Review found" in none.output
    none_json = _invoke(tmp_path, ["status", "--repo-root", str(repo_root), "--json"], suppress_default_open=False)
    assert none_json.exit_code == 2
    assert json.loads(none_json.stdout)["state"] == "no_review"

    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    tracked = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--track"])
    assert tracked.exit_code == 0, tracked.output
    _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "why 3?", "--on", "src/app.py:L5"],
    )
    ref = _REVIEW_URL.search(tracked.output)
    assert ref, tracked.output

    text = _invoke(tmp_path, ["status", "--repo-root", str(repo_root)], suppress_default_open=False)
    assert text.exit_code == 1, text.output
    assert re.search(rf"^Review +{re.escape(ref['ref'])} · open$", text.output, re.MULTILINE), text.output
    assert re.search(r"^Revision +1$", text.output, re.MULTILINE), text.output
    assert re.search(r"^Reviewed +0 of \d+ parts$", text.output, re.MULTILINE), text.output
    # One Risk and one Understanding line, in the Brief's words.
    assert re.search(r"^Risk +(High|Medium|Low|Not measured) · \S", text.output, re.MULTILINE), text.output
    assert re.search(r"^Understanding +0 of \d+ (risky )?parts? reviewed$", text.output, re.MULTILINE), text.output
    assert re.search(r"^Comments +1 unresolved", text.output, re.MULTILINE), text.output
    assert re.split(r" +", text.output.splitlines()[-1], maxsplit=1) == ["Reader", ref["url"]]

    as_json = _invoke(tmp_path, ["status", "--repo-root", str(repo_root), "--json"], suppress_default_open=False)
    assert as_json.exit_code == 1
    status = json.loads(as_json.stdout)
    assert status["state"] == "not_ready" and status["exit_code"] == 1
    assert status["review"]["ref"] == ref["ref"]
    assert status["revision"]["number"] == 0 and status["revision"]["label"] == "Revision 1"
    assert status["targets"]["total"] > 1 and status["targets"]["reviewed"] == 0
    assert status["comments"]["unresolved"] == 1
    assert set(status["feedback"]) == {"unsent", "in_flight", "sent", "addressed"}
    assert status["work_remains"] is True
    assert status["reader_url"] == ref["url"]
    assert isinstance(status["readiness"]["actions"], list)
    assert status["risk"]["level"] in {"high", "medium", "low", "not_measured"}
    assert status["understanding"]["reviewed"] == 0 and status["understanding"]["text"].endswith("reviewed")

    named = _invoke(tmp_path, ["status", "--review", ref["ref"], "--json"], suppress_default_open=False)
    assert json.loads(named.stdout)["review"]["ref"] == ref["ref"]

    usage = _invoke(tmp_path, ["status", "--bogus"], suppress_default_open=False)
    assert usage.exit_code == 3, usage.output
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:9")
    down = _invoke(tmp_path, ["status", "--repo-root", str(repo_root), "--json"], suppress_default_open=False)
    assert down.exit_code == 3, down.output
    assert json.loads(down.stdout)["state"] == "error"


def test_status_exit_codes_and_vocabularies_are_one_list() -> None:
    from lemoncrow_client import review_terminal

    from lemoncrow.core.foundation.paths import AGENT_SESSION_ENVS
    from lemoncrow.gateway.cli.commands import review_status_cli
    from lemoncrow.pro.capabilities.review.session_models import ANNOTATION_KINDS, MARK_STATES

    assert review_status_cli._EXIT_ERROR == review_terminal.EXIT_ERROR
    assert (review_terminal.EXIT_READY, review_terminal.EXIT_NOT_READY, review_terminal.EXIT_NO_REVIEW) == (0, 1, 2)
    assert review_terminal.MARK_STATES == MARK_STATES
    assert review_terminal.ANNOTATION_KINDS == ANNOTATION_KINDS
    assert review_terminal.AGENT_SESSION_ENVS == AGENT_SESSION_ENVS


def test_the_installed_client_runs_the_mark_comment_feedback_loop_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stdlib client (the installed ``lc``) runs the scriptable loop against the same server as the full CLI."""

    from io import StringIO

    from lemoncrow_client.review import review_main

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    monkeypatch.chdir(repo_root)
    # The same repository identity `_invoke` gives the full CLI.
    monkeypatch.setenv("LEMONCROW_SCM_PROVIDER", "fixture")
    monkeypatch.setenv(
        "LEMONCROW_SCM_REPO_ID", hashlib.sha256(str(tmp_path.resolve()).encode("utf-8")).hexdigest()[:24]
    )
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)

    def thin(*arguments: str) -> tuple[int, str]:
        sink = StringIO()
        code = review_main(list(arguments), sink=sink)
        return code, sink.getvalue()

    code, out = thin("--working-tree", "--mark", "src/app.py::two", "--no-open")
    assert code == 0, out
    assert _HEADLINE.search(out) and " · 1 reviewed" in out, out
    code, out = thin(
        "--working-tree", "--comment", "why 3?", "--on", "src/app.py:L5", "--comment-kind", "request_change"
    )
    assert code == 0, out
    assert "COMMENT RECORDED" in out and "src/app.py:L5" in out
    code, out = thin("--working-tree", "--feedback")
    assert code == 0, out
    assert "FEEDBACK BUNDLE" in out and "why 3?" in out
    code, out = thin("--working-tree", "--marks", "--json")
    assert code == 0, out
    document = json.loads(out.splitlines()[-1])
    assert [mark["label"] for mark in document["state"]["marks"]] == ["src/app.py::two"]

    code, out = thin("status", "--json")
    assert code == 1, out
    status = json.loads(out)
    assert status["targets"]["reviewed"] == 1 and status["comments"]["unresolved"] == 1
    full = _invoke(tmp_path, ["status", "--json"], suppress_default_open=False)
    assert json.loads(full.stdout)["review"] == status["review"]

    # Human judgment stays the human's: the agent's own shell may capture and read, never mark or comment.
    monkeypatch.setenv("CLAUDECODE", "1")
    code, out = thin("--working-tree", "--mark", "src/app.py")
    assert code == 2 and "inside an agent session" in out, out
    code, out = thin("--working-tree", "--feedback", "--no-open")
    assert code == 0, out


def test_marks_made_in_one_invocation_are_visible_in_the_next(tmp_path: Path) -> None:
    """Phase 1 acceptance through the CLI: two processes, one review state."""

    repo_root = _fixture_repo(tmp_path)
    marked = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--mark", "src/app.py"])
    assert marked.exit_code == 0, marked.output

    listed = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--marks"])
    assert listed.exit_code == 0, listed.output
    assert "reviewed" in listed.output
    assert "src/app.py" in listed.output.split("MARKS")[1]


def test_a_mark_target_that_matches_nothing_fails_loudly(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--mark", "src/never.py"])
    assert result.exit_code != 0
    assert "no part matches" in result.output
    assert "Traceback" not in result.output


def test_units_lists_position_free_keys(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--units"])
    assert result.exit_code == 0, result.output
    body = result.output.split("UNITS")[1]
    assert "fil:" in body
    assert "hun:" in body
    assert "src/app.py" in body


def test_a_unit_key_can_be_marked_directly(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    listed = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--units"])
    key = next(
        line.split()[0]
        for line in listed.output.splitlines()
        if line.strip().startswith("fil:") and "src/app.py" in line
    )
    marked = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--mark", key, "--marks"])
    assert marked.exit_code == 0, marked.output
    # The listing prints the path once the key resolves, which is the proof the
    # key was accepted as a target in the first place.
    assert "src/app.py" in marked.output.split("MARKS")[1]
    assert "1 reviewed" in marked.output


# Two functions appended to the committed `def one(): return 2`, so one path
# carries more than one review target and the expansion has something to expand.
_TWO_MORE_SYMBOLS = "def one():\n    return 2\n\n\ndef two():\n    return 3\n\n\ndef three():\n    return 4\n"


def test_marking_by_path_moves_the_target_counters(tmp_path: Path) -> None:
    """The one mark ``--mark PATH`` creates has to be visible to the same numbers.

    ``resolve_unit`` answers a path with the file unit, but progress and closure
    are denominated in review *targets*, which are keyed to symbols or hunks.
    So the documented CLI action stored a verdict where nothing counted it and
    one block of output said ``marks 1 reviewed`` directly above
    ``targets 3 (0/3 reviewed)``.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    marked = _invoke(tmp_path, [*base, "--mark", "src/app.py", "--finish", "--json"])
    assert marked.exit_code == 0, marked.output
    state = json.loads(marked.stderr.strip().splitlines()[-1])

    progress = state["target_progress"]
    assert progress["target_count"] > 1, progress
    assert progress["reviewed"] == progress["target_count"], progress
    closure = state["closure"]
    assert closure["reviewed_targets"] == closure["target_count"], closure
    assert closure["unreviewed_targets"] == 0, closure


def test_marking_a_part_later_records_it_unjudged_and_says_it_is_left_for_later(tmp_path: Path) -> None:
    """`--mark-state later`: counted apart from reviewed, still open at finish, never "nothing outstanding"."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    marked = _invoke(tmp_path, [*base, "--mark", "src/app.py", "--mark-state", "later", "--finish", "--json"])
    assert marked.exit_code == 0, marked.output
    state = json.loads(marked.stderr.strip().splitlines()[-1])
    progress = state["target_progress"]
    assert progress["later"] == progress["target_count"] > 1, progress
    assert (progress["reviewed"], progress["unreviewed"], progress["needs_changes"]) == (0, 0, 0), progress
    assert state["stale_marks"] == 0
    assert set(state["mark_counts"]) == {"later"}
    closure = state["closure"]
    assert closure["later_targets"] == closure["target_count"], closure
    assert (closure["reviewed_targets"], closure["unreviewed_targets"]) == (0, 0), closure

    # The plain output says so in words, and does not claim the review is done.
    plain = _invoke(tmp_path, [*base, "--mark", "src/app.py", "--mark-state", "later", "--finish"])
    assert plain.exit_code == 0, plain.output
    assert " · 0 reviewed" in plain.output
    assert "left for later" in plain.output
    assert "nothing outstanding" not in plain.output

    # A later part is judged by marking it: the state moves out of `later`.
    judged = _invoke(tmp_path, [*base, "--mark", "src/app.py", "--json"])
    assert judged.exit_code == 0, judged.output
    after = json.loads(judged.stderr.strip().splitlines()[-1])["target_progress"]
    assert (after["later"], after["reviewed"]) == (0, after["target_count"])


def test_content_moving_under_a_part_left_for_later_makes_nothing_stale(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    marked = _invoke(tmp_path, [*base, "--mark", "src/app.py", "--mark-state", "later", "--json"])
    assert marked.exit_code == 0, marked.output
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS + "\n\ndef four():\n    return 5\n", encoding="utf-8")
    moved = _invoke(tmp_path, [*base, "--marks", "--json"])
    assert moved.exit_code == 0, moved.output
    state = json.loads(moved.stderr.strip().splitlines()[-1])
    assert state["stale_marks"] == 0
    assert state["mark_counts"] == {"later": state["mark_counts"]["later"]} and state["mark_counts"]["later"] > 0
    assert {mark["content"] for mark in state["marks"]} == {"same"}


def test_a_path_mark_names_the_targets_it_landed_on(tmp_path: Path) -> None:
    """Reader spec §29.3: a bare path must not silently mark the whole file.

    It no longer marks the whole file -- it marks every target on the path --
    but a reviewer who typed one word and had three verdicts recorded still has
    to be told which three, in the same labels every other surface uses.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    result = _invoke(tmp_path, [*base, "--mark", "src/app.py", "--json"])
    assert result.exit_code == 0, result.output
    assert "src/app.py: recorded on 3 parts" in result.output
    assert "src/app.py::two" in result.output
    assert "src/app.py::three" in result.output
    assert "src/app.py#0" in result.output

    state = json.loads(result.stderr.strip().splitlines()[-1])
    (row,) = state["path_marks"]
    assert row["target"] == "src/app.py"
    # The two added definitions, plus the hunk that owns the blank lines between
    # them -- every target on the path, named the way the browser names it.
    assert {item["label"] for item in row["covered"]} == {
        "src/app.py::two",
        "src/app.py::three",
        "src/app.py#0",
    }

    # A unit key already names one judgment, so it is recorded as typed and
    # reported as nothing -- there is no expansion to disclose.
    key = next(item["unit_key"] for item in row["covered"])
    by_key = _invoke(tmp_path, [*base, "--mark", key, "--json"])
    assert by_key.exit_code == 0, by_key.output
    assert "recorded on" not in by_key.output
    assert "path_marks" not in json.loads(by_key.stderr.strip().splitlines()[-1])


def test_json_tracking_keeps_stdout_parseable(tmp_path: Path) -> None:
    """The review-state block is a second document; stdout stays the packet."""

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--json", "--track"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 2

    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert state["review_id"].startswith("r/")
    assert state["revision_number"] == 0
    assert state["unit_count"] > 0


def test_a_file_saved_while_the_packet_is_built_is_not_marked_as_reviewed(tmp_path: Path) -> None:
    """End to end over the TOCTOU window the CLI used to leave open.

    The impact pass is slow enough for a developer to hit save inside it. What
    lands in the store must be the packet that was displayed, fingerprinted
    from the same bytes -- never a re-read of whatever is on disk by then.
    """

    from lemoncrow.pro.capabilities.review import packet as packet_module

    repo_root = _fixture_repo(tmp_path)
    target = repo_root / "src" / "app.py"
    target.write_text("def one():\n    return 3\n", encoding="utf-8")

    real_load = packet_module.load_blobs
    saved_after: list[str] = []

    def _save_during_the_build(root: Path, rng: Any, files: Any, **kwargs: Any) -> Any:
        blobs = real_load(root, rng, files, **kwargs)
        # The developer saves again while the build is still running.
        target.write_text("def one():\n    return 4242\n", encoding="utf-8")
        saved_after.append(target.read_text(encoding="utf-8"))
        return blobs

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(packet_module, "load_blobs", _save_during_the_build)
    try:
        result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--mark", "src/app.py", "--json"])
    finally:
        monkeypatched.undo()

    assert result.exit_code == 1
    assert saved_after, "the fixture never simulated a save"
    assert "source changed while Review source bytes were being captured; retry" in result.output
    assert not result.stdout.strip()

    # The failed capture must not bless bytes the reviewer never saw. Once the
    # source is stable, retrying succeeds and marks that exact snapshot.
    retry = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--mark", "src/app.py", "--json"])
    assert retry.exit_code == 0, retry.output
    packet = json.loads(retry.stdout)
    patches = "\n".join(hunk["patch"] for item in packet["files"] for hunk in item["hunks"])
    assert "4242" in patches

    state = json.loads(retry.stderr.strip().splitlines()[-1])
    assert state["mark_counts"] == {"reviewed": 1}

    again = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--marks"])
    assert again.exit_code == 0, again.output
    rows = [line.strip() for line in again.output.split("MARKS")[1].splitlines() if line.strip()]
    assert any(row.startswith("reviewed") and " same " in row for row in rows), rows


# --since-my-review
# --------------------------------------------------------------------------- #


def test_since_my_review_names_only_what_the_agent_rewrote(tmp_path: Path) -> None:
    """The terminal shape of the killer feature, exercised end to end.

    Two files reviewed, one of them rewritten. The summary must put *changed
    since my review* first and must not ask the human to re-read the file that
    did not move -- a tool that reopens both is a tool nobody trusts twice.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    (repo_root / "src" / "other.py").write_text("def two():\n    return 4\n", encoding="utf-8")

    marked = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--mark",
            "src/app.py",
            "--mark",
            "src/other.py",
        ],
    )
    assert marked.exit_code == 0, marked.output

    # `one` itself is rewritten, not merely joined by a neighbour: a mark typed
    # as a path is recorded on the targets that path covers, so appending an
    # unrelated definition would leave the reviewed one honestly still valid.
    (repo_root / "src" / "app.py").write_text(
        "def one():\n    return 5\n\n\ndef three():\n    return 3\n", encoding="utf-8"
    )
    after = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--since-my-review"])
    assert after.exit_code == 0, after.output

    assert "SINCE MY REVIEW" in after.output
    assert "CHANGED SINCE MY REVIEW" in after.output
    block = after.output.split("CHANGED SINCE MY REVIEW")[1]
    assert "src/app.py" in block.split("UNCHANGED REVIEWED")[0]
    assert "src/other.py" not in block.split("UNCHANGED REVIEWED")[0]
    assert "UNCHANGED REVIEWED" in after.output
    assert "SINCE MY REVIEW  Revision 2  (you last saw Revision 1)" in after.output
    assert "judgment still valid — nothing to re-read" in after.output
    assert "SINCE MY REVIEW  revision" not in after.output


def test_since_my_review_before_any_mark_lists_everything_as_not_reviewed_yet() -> None:
    """With no last review there is no "new since" it: one honest bucket, not "new 7 / not yet reviewed 0"."""

    from lemoncrow_client.review_terminal import render_frontier

    frontier = {"new": [{"label": "src/app.py"}, {"label": "src/app.py::one"}], "not_yet_reviewed": 1}
    text = "\n".join(render_frontier(frontier, {"revision_number": 0}))

    assert "SINCE MY REVIEW  Revision 1  (nothing reviewed yet)" in text
    assert "  not yet reviewed         3" in text
    assert "NOT REVIEWED YET  (2)" in text
    assert "NEW SINCE MY LAST REVIEW" not in text
    assert "changed since my review" not in text


def test_since_my_review_reaches_json_consumers_on_stderr(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--mark", "src/app.py"])
    (repo_root / "src" / "app.py").write_text("def one():\n    return 42\n", encoding="utf-8")

    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--since-my-review", "--json"])
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # stdout stays the packet
    state = json.loads(result.stderr.strip().splitlines()[-1])
    frontier = state["frontier"]
    assert [row["path"] for row in frontier["changed_since_review"]] == ["src/app.py"]
    assert frontier["previous_revision_number"] == 0
    # Every key here is rendered by a surface. `created` and `dropped_marks`
    # were shipped for a year and read by nothing; `removed_units` shipped as a
    # bare count of things it could not name.
    assert "created" not in frontier
    assert "dropped_marks" not in frontier
    assert frontier["removed_units"] == []


def test_repeated_working_tree_review_reuses_id_and_adds_revision(tmp_path: Path) -> None:
    """The canonical developer loop keeps one URL while the diff evolves."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    args = ["--repo-root", str(repo_root), "--working-tree", "--track", "--json"]

    first = _invoke(tmp_path, args)
    assert first.exit_code == 0, first.output
    first_state = json.loads(first.stderr.strip().splitlines()[-1])
    assert first_state["revision_number"] == 0

    (repo_root / "src" / "app.py").write_text("def one():\n    return 4\n", encoding="utf-8")
    second = _invoke(tmp_path, args)
    assert second.exit_code == 0, second.output
    second_state = json.loads(second.stderr.strip().splitlines()[-1])

    assert second_state["review_id"] == first_state["review_id"]
    assert second_state["revision_id"] != first_state["revision_id"]
    assert second_state["revision_number"] == 1


# --------------------------------------------------------------------------- #
# `--open`: the workspace is the surface, the static report is the fallback
# --------------------------------------------------------------------------- #


def _no_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Swallow the browser launch and record what would have been opened."""

    opened: list[str] = []
    from lemoncrow.gateway.cli.commands import review as review_command
    from lemoncrow.pro.capabilities.review import hosted as hosted_mod

    def _finish_in_background(path: Path) -> bool:
        path.unlink(missing_ok=True)
        return True

    monkeypatch.setattr(hosted_mod, "_launch_local_finalize_job", _finish_in_background)

    def _open(target: str) -> bool:
        opened.append(target)
        return True

    monkeypatch.setattr(review_command, "_launch_browser_url", _open)
    return opened


def test_browser_launcher_never_falls_back_to_links(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_NO_BROWSER", raising=False)
    import shutil
    import subprocess
    import sys

    from lemoncrow.gateway.cli.commands import review as review_command

    launched: list[list[str]] = []

    class _Proc:
        pass

    monkeypatch.setattr(sys, "platform", "linux")  # the xdg-open path, on any machine running the test
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("BROWSER", "links")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
    )
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **_kwargs: launched.append(list(argv)) or _Proc())

    url = "http://127.0.0.1:7420/r/deadbeef"
    assert review_command._launch_browser_url(url) is True
    assert launched == [["/usr/bin/xdg-open", url]]


def test_browser_launcher_uses_macos_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_NO_BROWSER", raising=False)
    import shutil
    import subprocess
    import sys

    from lemoncrow.gateway.cli.commands import review as review_command

    launched: list[list[str]] = []

    class _Proc:
        pass

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/open" if name == "open" else None)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **_kwargs: launched.append(list(argv)) or _Proc())

    url = "http://127.0.0.1:7420/r/deadbeef"
    assert review_command._launch_browser_url(url) is True
    assert launched == [["/usr/bin/open", url]]


def test_existing_review_can_be_listed_and_shown_by_id_without_recapturing(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    tracked = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--track", "--json"])
    assert tracked.exit_code == 0, tracked.output
    state = json.loads(tracked.stderr.strip().splitlines()[-1])
    review_id = state["review_id"]

    before = _invoke(tmp_path, ["--show-review", review_id, "--json"])
    assert before.exit_code == 0, before.output
    before_detail = json.loads(before.output)
    revision_count = before_detail["revision_count"]
    assert before_detail["revision"]["revision_number"] == 0

    listed = _invoke(tmp_path, ["--list-reviews", "--review-status", "all"])
    assert listed.exit_code == 0, listed.output
    assert review_id in listed.output
    assert before_detail["review"]["status"] in listed.output
    assert "REVISION" in listed.output

    shown = _invoke(tmp_path, ["--show-review", review_id])
    assert shown.exit_code == 0, shown.output
    assert review_id in shown.output
    assert "Revision 1" in shown.output
    assert f"Revisions   {revision_count}" in shown.output
    after = _invoke(tmp_path, ["--show-review", review_id, "--json"])
    assert json.loads(after.output)["revision_count"] == revision_count


def test_current_review_status_uses_the_workspace_live_binding_without_recapture(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    tracked = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--track", "--json"],
    )
    assert tracked.exit_code == 0, tracked.output
    tracked_state = json.loads(tracked.stderr.strip().splitlines()[-1])

    current = _invoke(tmp_path, ["--repo-root", str(repo_root), "--current", "--json"])
    assert current.exit_code == 0, current.output
    status = json.loads(current.output)

    tracked_review_ref = str(tracked_state["review_id"])
    tracked_revision_ref = str(tracked_state["revision_id"])
    assert status["review_ref"] == tracked_review_ref
    assert status["revision_ref"] == tracked_revision_ref
    assert status["review_id"]
    assert status["revision_id"]
    assert status["revision_number"] == tracked_state["revision_number"]
    assert status["reader_url"].endswith(f"/{tracked_review_ref}")
    assert status["feedback"] == {
        "open_total": 0,
        "unpublished": 0,
        "published": 0,
        "in_flight": 0,
        "addressed": 0,
    }


def test_resume_author_continues_the_exact_session_from_current_review_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from lemoncrow.pro.capabilities.review import delivery
    from lemoncrow.pro.capabilities.review.delivery import AgentDeliveryResult
    from lemoncrow.pro.capabilities.review.live_runtime import (
        load_live_review_binding,
        write_live_review_binding,
    )

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 4\n", encoding="utf-8")
    tracked = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--track", "--json"],
    )
    assert tracked.exit_code == 0, tracked.output
    binding = load_live_review_binding(
        tmp_path / "store",
        repo_root,
        server_url=os.environ["LEMONCROW_URL"],
    )
    assert binding is not None
    write_live_review_binding(
        tmp_path / "store",
        repo_root,
        replace(binding, author_host="codex", author_session_id="codex-exact-1"),
    )

    seen: list[tuple[str, str, Path, str]] = []

    def resume(host: str, session_id: str, root: Path, prompt: str) -> AgentDeliveryResult:
        seen.append((host, session_id, root, prompt))
        return AgentDeliveryResult(
            "queued",
            f"{host}:{session_id}",
            remote_ref=f"codex-thread:{session_id}",
            message="prompt queued to exact Codex session",
        )

    monkeypatch.setattr(delivery, "deliver_prompt_to_agent_session", resume)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--resume-author", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["author"]["host"] == "codex"
    assert payload["author"]["session_id"] == "codex-exact-1"
    assert payload["resume"]["state"] == "queued"
    assert seen and seen[0][:3] == ("codex", "codex-exact-1", repo_root.resolve())
    assert "Do not treat this continuity prompt as new reviewer feedback" in seen[0][3]


def test_current_review_requires_an_established_workspace_binding(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--current"])

    assert result.exit_code != 0
    assert "no current Review binding" in result.output
    assert "run `lcr`" in result.output


def test_finished_and_archived_review_ids_remain_discoverable_from_cli(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    base = ["--repo-root", str(repo_root), "--working-tree"]
    tracked = _invoke(tmp_path, [*base, "--track", "--json"])
    assert tracked.exit_code == 0, tracked.output
    review_id = json.loads(tracked.stderr.strip().splitlines()[-1])["review_id"]

    finished_result = _invoke(tmp_path, [*base, "--finish"])
    assert finished_result.exit_code == 0, finished_result.output
    finished = _invoke(tmp_path, ["--list-reviews", "--review-status", "finished"])
    assert finished.exit_code == 0, finished.output
    assert review_id in finished.output

    discarded_result = _invoke(tmp_path, [*base, "--discard-review"])
    assert discarded_result.exit_code == 0, discarded_result.output
    archived = _invoke(tmp_path, ["--list-reviews", "--review-status", "archived"])
    assert archived.exit_code == 0, archived.output
    assert review_id in archived.output
    all_rows = _invoke(tmp_path, ["--list-reviews", "--review-status", "all", "--json"])
    assert all_rows.exit_code == 0
    assert review_id in all_rows.output


def test_open_review_reuses_the_recorded_repository_and_review_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _fixture_repo(tmp_path)
    tracked = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--track", "--json"])
    assert tracked.exit_code == 0, tracked.output
    review_id = json.loads(tracked.stderr.strip().splitlines()[-1])["review_id"]
    opened = _no_browser(monkeypatch)

    result = _invoke(tmp_path, ["--open-review", review_id])
    assert result.exit_code == 0, result.output
    payload = review_id.removeprefix("r/")
    canonical = f"{os.environ['LEMONCROW_URL']}/r/{payload}"
    assert result.output.count(canonical) == 1
    expected = canonical
    assert opened == [expected]


def test_open_ignores_the_retired_workspace_bundle_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured server owns the Reader even if legacy workspace code sees no bundle."""

    from lemoncrow.pro.capabilities.review import workspace as workspace_mod

    monkeypatch.setattr(workspace_mod, "bundle_dir", lambda: None)
    opened = _no_browser(monkeypatch)

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--open"])

    assert result.exit_code == 0, result.output
    assert "no built frontend bundle found" not in result.stderr
    assert "Review workspace:" not in result.stderr
    assert not list((tmp_path / "store" / "review").glob("*.html"))
    assert opened and re.match(
        rf"^{re.escape(os.environ['LEMONCROW_URL'])}/reviews/preparing/[0-9a-f]{{32}}$", opened[0]
    )


def test_open_reports_meaningful_preparation_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _no_browser(monkeypatch)
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--open"])

    assert result.exit_code == 0, result.output
    assert "Preparing review…" in result.stderr
    assert "Reading changed files and diff" in result.stderr
    assert "Loading exact old/new source revisions" in result.stderr
    assert "Analyzing callers and change impact in background" not in result.stderr
    assert "Correlating author context in background" not in result.stderr
    assert "Remaining Review context continues in background" in result.stderr
    assert "Change packet ready" in result.stderr
    assert "Connecting to Review server" in result.stderr
    assert "Synchronizing current source" in result.stderr
    # A first open publishes a new Review (a later open reports "Creating new snapshot").
    assert "Publishing new review" in result.stderr
    assert "Opening Review Reader" in result.stderr
    assert "Freezing current revision" not in result.stderr
    assert "Freezing base revision in background" not in result.stderr
    assert "Base revision attached" not in result.stderr
    assert "Loading durable review state" not in result.stderr
    assert "Loading review overview" in result.stderr
    assert "Loading parts and marks" in result.stderr
    assert "Loading review comments" in result.stderr
    assert "Review Reader opened" in result.stderr
    assert result.stderr.index("Review Reader opened") < result.stderr.index(
        "Remaining Review context continues in background"
    )
    assert opened
    assert result.stderr.index("Review Reader opened") < result.stderr.index("Reading changed files and diff")
    from lemoncrow_client.config import load_config

    from lemoncrow.pro.capabilities.review.hosted import server_review_request

    job_id = opened[0].rsplit("/", 1)[-1]
    progress = server_review_request(load_config(cwd=repo_root), "GET", f"/api/review-preparations/{job_id}")
    assert progress["state"] == "ready"
    assert progress["review_url"].startswith("/r/")
    assert len(opened) == 1
    for key in ("state_overview", "state_units", "state_annotations"):
        assert next(step for step in progress["steps"] if step["key"] == key)["state"] == "done"
    assert all(step["key"] != "state" for step in progress["steps"])


@pytest.mark.parametrize("asset_size", [1024 * 1024, 1024 * 1024 + 1, 8 * 1024 * 1024, 8 * 1024 * 1024 + 1])
@pytest.mark.parametrize("changed", [False, True])
def test_working_tree_review_keeps_other_files_when_assets_are_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asset_size: int, changed: bool
) -> None:
    _no_browser(monkeypatch)
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "asset.bin").write_bytes(b"\0" * asset_size)
    _commit(pygit2.Repository(str(repo_root)), "add unchanged asset", 120)
    if changed:
        (repo_root / "asset.bin").write_bytes(b"\1" * asset_size)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--open", "--no-impact", "--no-provenance"],
    )

    assert result.exit_code == 0, result.output
    assert "Review Reader opened" in result.stderr
    assert "Remaining Review context continues in background" in result.stderr
    assert "Base revision attached" not in result.stderr


def test_warm_open_starts_progress_before_reusing_packet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _no_browser(monkeypatch)
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    first = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--open"])
    assert first.exit_code == 0, first.output
    assert opened
    first_preparation_url = opened[-1]
    from lemoncrow.pro.capabilities.review.live_runtime import load_live_review_binding

    first_binding = load_live_review_binding(
        tmp_path / "store",
        repo_root,
        server_url=os.environ["LEMONCROW_URL"],
    )
    assert first_binding is not None
    assert first_binding.claimed_at > 0

    opened.clear()
    second = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--open"])

    assert second.exit_code == 0, second.output
    assert opened and "/reviews/preparing/" in opened[0]
    assert opened[0] != first_preparation_url
    assert "Opening Review Reader" in second.stderr
    # The warm path reuses the prepared diff instead of re-reading it.
    reused = "Prepared diff, source revisions, and parts reused"
    assert reused in second.stderr
    assert "Reading changed files and diff" not in second.stderr
    assert second.stderr.index("Opening Review Reader") < second.stderr.index(reused)
    second_binding = load_live_review_binding(
        tmp_path / "store",
        repo_root,
        server_url=os.environ["LEMONCROW_URL"],
    )
    assert second_binding is not None
    assert second_binding.review_id == first_binding.review_id
    assert second_binding.claimed_at >= first_binding.claimed_at


def test_open_uses_the_configured_server_and_writes_no_second_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--open` is the canonical server Reader and does not also freeze HTML."""

    opened = _no_browser(monkeypatch)
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--open"])

    assert result.exit_code == 0, result.output
    assert f"{os.environ['LEMONCROW_URL']}/r/" in result.output
    assert f"{os.environ['LEMONCROW_URL']}/r/" not in result.stderr
    machine_token = Path(os.environ["LEMONCROW_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    assert machine_token not in result.output
    assert machine_token not in result.stderr
    assert "#t=" not in result.output
    assert "#t=" not in result.stderr
    assert opened and re.match(
        rf"^{re.escape(os.environ['LEMONCROW_URL'])}/reviews/preparing/[0-9a-f]{{32}}$", opened[0]
    )
    assert not list((tmp_path / "store" / "review").glob("*.html"))


def test_retired_workspace_launcher_cannot_affect_review_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Normal Review must never consult or start the retired per-repository launcher."""

    from lemoncrow.pro.capabilities.review import workspace as workspace_mod

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("retired workspace launcher was called")

    monkeypatch.setattr(workspace_mod, "ensure_workspace", _boom)
    opened = _no_browser(monkeypatch)

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--open"])

    assert result.exit_code == 0, result.output
    assert "could not start the review workspace" not in result.stderr
    assert opened and re.match(
        rf"^{re.escape(os.environ['LEMONCROW_URL'])}/reviews/preparing/[0-9a-f]{{32}}$", opened[0]
    )


def test_html_still_writes_a_report_on_its_own(tmp_path: Path) -> None:
    """`--html PATH` is a standalone surface and does not need `--open`."""

    repo_root = _fixture_repo(tmp_path)
    out = tmp_path / "report.html"
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--html", str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists()
    assert f"HTML report: {out}" in result.output


def test_review_title_is_passed_to_snapshot_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.cli.commands import review as review_mod

    repo_root = _fixture_repo(tmp_path)
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    titles: list[str] = []

    def capture(*args: Any, **kwargs: Any) -> None:
        titles.append(kwargs["title_override"])

    monkeypatch.setattr(review_mod, "_review_server_capture", capture)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--title", "Fix review labels"])
    assert result.exit_code == 0, result.output
    assert titles == ["Fix review labels"]


def test_review_intent_flags_record_linked_intent_after_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--intent``/``--description-file`` post bounded text for files and references for links."""

    from types import SimpleNamespace

    from lemoncrow.gateway.cli.commands import review as review_mod
    from lemoncrow.pro.capabilities.review import hosted as hosted_mod
    from lemoncrow.pro.capabilities.review import render as render_mod

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "docs").mkdir(exist_ok=True)
    (repo_root / "docs" / "spec.md").write_text("# Refresh\nExpired sessions report invalid.\n", encoding="utf-8")
    description = tmp_path / "pr.md"
    description.write_text("# Report invalid sessions\n\nCloses #12.\n", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    captured = SimpleNamespace(
        build=SimpleNamespace(packet=SimpleNamespace()),
        review_id="r1",
        review_url="http://127.0.0.1:7420/r/1",
        canonical_url="http://127.0.0.1:7420/r/1",
        pending=(),
        response={"review": {"title": "t"}, "revision": {"id": "rev1", "revision_number": 2}},
    )
    requests: list[tuple[str, str, Any]] = []

    def request(config: Any, method: str, path: str, *, body: Any = None, **kwargs: Any) -> dict[str, Any]:
        requests.append((method, path, body))
        return {}

    monkeypatch.setattr(hosted_mod, "capture_server_review", lambda *args, _captured=captured, **kwargs: _captured)
    monkeypatch.setattr(hosted_mod, "server_review_request", request)
    monkeypatch.setattr(render_mod, "render_review", lambda *args, **kwargs: "")
    monkeypatch.setattr(review_mod, "_server_terminal_state", lambda *args, **kwargs: {})
    monkeypatch.setattr(review_mod, "_render_state", lambda state: "")
    monkeypatch.setattr(review_mod, "_open_server_review", lambda *args, **kwargs: None)
    result = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--intent",
            "spec:docs/spec.md",
            "--intent",
            "incident:https://status.example.com/i/7",
            "--description-file",
            str(description),
        ],
    )
    assert result.exit_code == 0, result.output
    paths = [path for _method, path, _body in requests]
    assert paths == ["/api/reviews/r1/revisions/2/publish", "/api/reviews/r1/intent"]
    body = requests[1][2]
    assert body["expected_revision_id"] == "rev1"
    assert body["references"] == [
        {
            "kind": "spec",
            "ref": "docs/spec.md",
            "title": "docs/spec.md",
            "body": "# Refresh\nExpired sessions report invalid.\n",
        },
        {"kind": "incident", "ref": "https://status.example.com/i/7"},
    ]
    assert body["description"] == {"title": "Report invalid sessions", "body": "Closes #12."}

    for bad in (["--intent", "spec:../outside.md"], ["--intent", "story:#1"], ["--intent", "spec:docs/missing.md"]):
        refused = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", *bad])
        assert refused.exit_code != 0, bad
    combined = _invoke(tmp_path, ["--repo-root", str(repo_root), "--list-reviews", "--intent", "issue:#1"])
    assert combined.exit_code != 0 and "require a revision capture" in combined.output


def test_review_reports_background_passes_as_pending_in_text_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass handed to the background is pending, never `*_disabled` or `unknown`."""

    from types import SimpleNamespace

    from lemoncrow.gateway.cli.commands import review as review_mod
    from lemoncrow.pro.capabilities.review import hosted as hosted_mod
    from lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION, ChangedFile, ReviewPacket

    repo_root = _fixture_repo(tmp_path)
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    packet = ReviewPacket(
        schema_version=SCHEMA_VERSION,
        generated_at="2026-09-07T12:00:00+00:00",
        repo_root=str(repo_root),
        range_mode="working_tree",
        base_rev="HEAD",
        head_rev="WORKDIR",
        base_sha="a" * 40,
        head_sha="",
        title="working tree",
        files=(ChangedFile(path="lib/cache.ts", old_path=None, status="added"),),
        degraded=("impact_disabled", "non_definition_symbols", "provenance_disabled"),
    )
    captured = SimpleNamespace(
        build=SimpleNamespace(packet=packet),
        review_id="r1",
        review_url="http://127.0.0.1:7420/r/1",
        canonical_url="http://127.0.0.1:7420/r/1",
        response={"review": {"title": "t"}, "revision": {"id": "rev1", "revision_number": 1}},
        pending=("impact", "provenance"),
    )
    (repo_root / "cache.ts").write_text("export const ttl = 60;\n", encoding="utf-8")
    monkeypatch.setattr(hosted_mod, "capture_server_review", lambda *args, **kwargs: captured)
    monkeypatch.setattr(hosted_mod, "server_review_request", lambda *args, **kwargs: {})
    monkeypatch.setattr(review_mod, "_server_terminal_state", lambda *args, **kwargs: {})
    monkeypatch.setattr(review_mod, "_render_state", lambda state: "")
    monkeypatch.setattr(review_mod, "_open_server_review", lambda *args, **kwargs: None)

    text = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--track", "--no-color"])
    assert text.exit_code == 0, text.output
    assert "Generated with: still preparing, continues in the Reader" in text.stdout
    assert "Map and caller impact and author attribution: still preparing, continues in the Reader" in text.stdout
    assert "provenance not collected" not in text.stdout
    assert "_disabled" not in text.stdout

    as_json = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--track", "--json"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert payload["pending"] == ["impact", "provenance"]
    assert payload["degraded"] == ["non_definition_symbols"]


def test_review_title_that_the_server_did_not_apply_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for case_index, (review, check, noted) in enumerate(
        [
            ({"title": "Existing hosted Review"}, False, True),
            ({"title": "Existing hosted Review"}, True, True),
            ({"title": "Fix review labels"}, False, False),
            ({"title": "Manual name", "auto_title": "Fix review labels"}, False, False),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        with monkeypatch.context() as case_monkeypatch:
            from types import SimpleNamespace

            from lemoncrow.gateway.cli.commands import review as review_mod
            from lemoncrow.pro.capabilities.review import hosted as hosted_mod
            from lemoncrow.pro.capabilities.review import render as render_mod

            repo_root = _fixture_repo(case_tmp_path)
            case_monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
            case_monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
            captured = SimpleNamespace(
                build=SimpleNamespace(packet=SimpleNamespace()),
                review_id="r1",
                review_url="http://127.0.0.1:7420/r/1",
                canonical_url="http://127.0.0.1:7420/r/1",
                pending=(),
                response={"review": review, "revision": {"id": "rev1", "revision_number": 2}},
            )
            case_monkeypatch.setattr(
                hosted_mod, "capture_server_review", lambda *args, _captured=captured, **kwargs: _captured
            )
            case_monkeypatch.setattr(hosted_mod, "server_review_request", lambda *args, **kwargs: {})
            case_monkeypatch.setattr(render_mod, "render_review", lambda *args, **kwargs: "")
            case_monkeypatch.setattr(review_mod, "_server_terminal_state", lambda *args, **kwargs: {})
            case_monkeypatch.setattr(review_mod, "_render_state", lambda state: "")
            case_monkeypatch.setattr(review_mod, "_open_server_review", lambda *args, **kwargs: None)
            arguments = ["--repo-root", str(repo_root), "--working-tree", "--title", "Fix review labels"]
            if check:
                arguments += ["--check", "lint", "--run", "true"]
            result = _invoke(case_tmp_path, arguments)
            assert result.exit_code == 0, result.output
            note = f'''--title was not applied; the Review this revision joined keeps the title "{review["title"]}"'''
            assert (note in result.stderr) is noted


@pytest.mark.parametrize(
    ("mode", "url", "hosted"),
    [
        ("local", "http://127.0.0.1:7420", False),
        ("hosted", "https://lemoncrow.example", True),
    ],
)
def test_review_uses_configured_server_in_both_modes_without_workspace_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    url: str,
    hosted: bool,
) -> None:
    from lemoncrow.gateway.cli.commands import review as review_mod

    repo_root = _fixture_repo(tmp_path)
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", mode)
    monkeypatch.setenv("LEMONCROW_URL", url)
    monkeypatch.setenv("LEMONCROW_TOKEN", "review-test-token-0123456789")
    called: list[tuple[str, bool, str]] = []

    def _server(config: Any, root: Path, *args: Any, **kwargs: Any) -> None:
        called.append((str(root), bool(config.hosted), str(config.url)))

    monkeypatch.setattr(review_mod, "_review_server_capture", _server)

    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree"],
        suppress_default_open=False,
    )

    assert result.exit_code == 0, result.output
    assert called == [(str(repo_root.resolve()), hosted, url)]


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_both_modes_refuse_the_retired_workspace_server_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    repo_root = _fixture_repo(tmp_path)
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", mode)
    monkeypatch.setenv(
        "LEMONCROW_URL",
        "http://127.0.0.1:7420" if mode == "local" else "https://lemoncrow.example",
    )
    monkeypatch.setenv("LEMONCROW_TOKEN", "review-test-token-0123456789")

    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--serve-workspace"])

    assert result.exit_code != 0
    assert "per-repository Review workspace server is retired" in result.output


def test_local_server_review_url_is_credential_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from lemoncrow.gateway.cli.commands.review import _open_server_review
    from lemoncrow.pro.capabilities.review import hosted as review_server

    paired: list[str] = []
    monkeypatch.setattr(
        review_server,
        "pair_local_review_browser",
        lambda _config, path: paired.append(path) or {"state": "armed"},
    )
    opened = _no_browser(monkeypatch)
    config = SimpleNamespace(hosted=False, token="legacy-token-that-must-not-leak")
    _open_server_review(
        "http://127.0.0.1:7420/reviews/rev-one",
        config=config,
        review_id="rev-one",
        open_browser=True,
    )
    assert paired == ["/reviews/rev-one"]
    assert opened == ["http://127.0.0.1:7420/reviews/rev-one"]
    assert config.token not in opened[0]


def test_hosted_server_review_url_never_embeds_cli_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from lemoncrow.gateway.cli.commands.review import _open_server_review

    opened = _no_browser(monkeypatch)
    config = SimpleNamespace(hosted=True, token="hosted-secret-token")
    _open_server_review(
        "https://lemoncrow.example/reviews/rev-one",
        config=config,
        review_id="rev-one",
        open_browser=True,
    )
    assert opened == ["https://lemoncrow.example/reviews/rev-one"]


def test_default_open_announces_the_canonical_review_url_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _no_browser(monkeypatch)
    repo_root = _fixture_repo(tmp_path)
    from lemoncrow.gateway.cli.commands import review as review_mod

    resolve_active = review_mod._active_local_branch_review

    def resolve_after_open(*args: Any, **kwargs: Any) -> Any:
        assert opened, "browser must open before a potentially slow active-Review lookup"
        return resolve_active(*args, **kwargs)

    monkeypatch.setattr(review_mod, "_active_local_branch_review", resolve_after_open)

    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree"],
        suppress_default_open=False,
    )

    assert result.exit_code == 0, result.output
    urls = re.findall(r"Review  (https?://\S+/r/[0-9a-f]+)", result.output)
    assert len(urls) == 1
    assert len(opened) == 1
    assert re.fullmatch(rf"{re.escape(os.environ['LEMONCROW_URL'])}/reviews/preparing/[0-9a-f]{{32}}", opened[0])
    from lemoncrow_client.config import load_config

    from lemoncrow.pro.capabilities.review.hosted import server_review_request

    job_id = opened[0].rsplit("/", 1)[-1]
    job = server_review_request(load_config(cwd=repo_root), "GET", f"/api/review-preparations/{job_id}")
    assert os.environ["LEMONCROW_URL"] + job["review_url"].split("#", 1)[0] == urls[0]


def test_no_open_keeps_terminal_review_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _no_browser(monkeypatch)
    repo_root = _fixture_repo(tmp_path)

    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--no-open"],
        suppress_default_open=False,
    )

    assert result.exit_code == 0, result.output
    assert opened == []
    assert "Preparing review…" not in result.stderr
    assert "Review workspace:" not in result.output
    assert "HTML report:" not in result.output
    assert not list((tmp_path / "store" / "review").glob("*.html"))


def test_json_open_keeps_progress_on_stderr_and_json_on_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _no_browser(monkeypatch)
    repo_root = _fixture_repo(tmp_path)

    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--json", "--open"],
        suppress_default_open=False,
    )

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    assert "Preparing review…" in result.stderr
    assert "Opening Review Reader" in result.stderr
    assert opened


def test_json_does_not_open_unless_explicitly_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _no_browser(monkeypatch)
    repo_root = _fixture_repo(tmp_path)

    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--json"],
        suppress_default_open=False,
    )

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    assert opened == []
    assert "Review workspace:" not in result.stderr


# --------------------------------------------------------------------------- #
# comments: the terminal half of PR-R4
# --------------------------------------------------------------------------- #


def test_annotation_kind_choices_match_the_model() -> None:
    """The CLI's four dispositions are the model's four, not a parallel list.

    The choices are restated in the command module so ``lc --help`` never
    imports the review package; this pins the copy to the original, so a fifth
    kind cannot be introduced on one side alone.
    """

    from lemoncrow.gateway.cli.commands.review import _ANNOTATION_KIND_CHOICES
    from lemoncrow.pro.capabilities.review.session_models import ANNOTATION_KINDS

    assert _ANNOTATION_KIND_CHOICES == ANNOTATION_KINDS


def test_comment_records_an_anchored_comment_and_says_where(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    result = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "return 3 is not what the caller expects",
            "--on",
            "src/app.py:L2",
            "--comment-kind",
            "request_change",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "COMMENT RECORDED" in result.output
    assert "request_change" in result.output
    assert "src/app.py:L2" in result.output
    assert "Comments  1 open" in result.output
    assert "not anchored" not in result.output


def test_comments_lists_what_is_stored_with_its_anchor(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "why 3?", "--on", "src/app.py:L2"],
    )

    listed = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--comments"])
    assert listed.exit_code == 0, listed.output
    assert "COMMENTS" in listed.output
    assert "why 3?" in listed.output
    assert "src/app.py:L2" in listed.output


def test_comments_on_a_review_with_none_says_so(tmp_path: Path) -> None:
    """An empty list is a statement, not a missing section."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--comments"])

    assert result.exit_code == 0, result.output
    assert "COMMENTS" in result.output
    assert "none recorded" in result.output


def test_comment_without_a_target_is_refused_before_anything_is_stored(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--comment", "floating"])

    assert result.exit_code != 0
    assert "--comment needs --on" in result.output


def test_a_malformed_target_is_refused_with_the_spelling_that_works(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "x", "--on", "src/app.py"],
    )

    assert result.exit_code != 0
    assert "PATH:L10" in result.output


def test_a_comment_on_a_file_outside_the_review_is_refused(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "x", "--on", "README.md:L1"],
    )

    assert result.exit_code != 0
    assert "not a file in Revision" in result.output


def test_a_reply_threads_under_the_comment_it_answers(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    first = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "why 3?",
            "--on",
            "src/app.py:L2",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    created = json.loads(first.stderr.strip().splitlines()[-1])["created_annotation"]

    reply = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "because the fixture says so",
            "--reply-to",
            created["id"],
            "--comments",
        ],
    )
    assert reply.exit_code == 0, reply.output
    assert "↳" in reply.output
    assert "because the fixture says so" in reply.output


def test_comments_reach_json_consumers_on_stderr(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    result = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "why 3?",
            "--on",
            "src/app.py:L2",
            "--comments",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # stdout stays the packet
    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert state["annotation_counts"] == {"open": 1}
    assert state["created_annotation"]["location"] == "src/app.py:L2"
    assert state["created_annotation"]["anchored"] is True
    assert [row["body"] for row in state["annotations"]] == ["why 3?"]


# --------------------------------------------------------------------------- #
# anchor honesty: every surviving comment says how it was re-found
# --------------------------------------------------------------------------- #


def test_every_anchor_method_has_a_plain_language_label() -> None:
    """No surface may print a rung's identifier at a human.

    Total over the vocabulary, so adding a rung without a sentence for it fails
    here rather than shipping ``exact_text_context`` to a reviewer.
    """

    from lemoncrow.pro.capabilities.review.session_models import (
        ANCHOR_METHOD_LABELS,
        ANCHOR_METHODS,
        EXACT_ANCHOR_METHODS,
        HEURISTIC_ANCHOR_METHODS,
    )

    assert set(ANCHOR_METHOD_LABELS) == set(ANCHOR_METHODS)
    assert all(label and label != method for method, label in ANCHOR_METHOD_LABELS.items())
    # Only rung 1 may claim the line was never re-derived.
    assert EXACT_ANCHOR_METHODS == ("identical_blob",)
    assert set(EXACT_ANCHOR_METHODS).isdisjoint(HEURISTIC_ANCHOR_METHODS)


def test_a_heuristic_relocation_is_never_drawn_like_an_untouched_file(tmp_path: Path) -> None:
    """The judge's case: rename the function, rewrite the docstring, keep the line.

    Both context blocks change, so rung 2 finds nothing and rung 4 re-finds the
    comment by its text alone -- landing on the very line it started on. Before
    this, the reviewer was told **nothing**: the move was filtered out as
    ``unchanged`` and the comment card said only ``in compute_total``, a
    function that no longer exists. Plan SS4.4: *never silently move a comment
    to a different line after a revision.*
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "svc.py").write_text(
        "def compute_total(items):\n"
        '    """Add up the item prices and return the total."""\n'
        "    return sum(item.price for item in items)\n",
        encoding="utf-8",
    )
    first = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "prices can be None here",
            "--on",
            "src/svc.py:L3",
        ],
    )
    assert first.exit_code == 0, first.output

    (repo_root / "src" / "svc.py").write_text(
        "def total_price(items):\n"
        '    """Sum every price on the invoice, in minor units."""\n'
        "    return sum(item.price for item in items)\n",
        encoding="utf-8",
    )
    after = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--since-my-review", "--comments"],
    )

    assert after.exit_code == 0, after.output
    # The rung, in words, on the comment itself.
    assert "anchor: re-found by its text alone" in after.output
    # And in the audit block, which used to drop it for landing on the same line.
    assert "COMMENT ANCHORS  (1)" in after.output
    assert "relocated" in after.output
    assert "same line" in after.output
    # Never the stale definition the comment was written against.
    assert "compute_total" not in after.output
    assert "in total_price" in after.output


def test_the_anchor_rung_and_its_label_reach_json_consumers(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    result = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "why 3?",
            "--on",
            "src/app.py:L2",
            "--comments",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])
    row = state["annotations"][0]
    assert row["anchor_method"] == "identical_blob"
    assert row["anchor_method_label"] == "file unchanged since the comment"


# --------------------------------------------------------------------------- #
# recording a revision and reconciling it are one operation
# --------------------------------------------------------------------------- #

_ALPHA_BEFORE = '''def alpha_1(value):
    """Original docstring for alpha."""
    total = value * 2
    return total
'''

# Renamed, re-documented, one line left byte-identical -- so the relocation
# ladder can still find the commented line, and every honest surface has to
# report that it *searched* for it.
_ALPHA_AFTER = '''def renamed_alpha(value):
    """Completely rewritten prose describing the new behaviour."""
    total = value * 2
    return total + 1
'''


def _reviewed_then_rewritten(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A sandbox where the file was reviewed and commented, then rewritten.

    Returns ``(sandbox, repo_root)``; the sandbox is the ``--root`` store, so two
    of these share nothing at all.

    The verdict is recorded on the *hunk* unit, by key, because these tests are
    about a mark that survives the rewrite and goes stale. The hunk is the only
    unit here that does both: the rewrite renames the file's one definition, so
    a symbol mark -- and therefore a mark typed as the path or as the file's
    ``fil:`` key, both of which expand onto the targets the file covers -- is
    discarded outright, which is the scenario
    :func:`test_a_tree_that_went_backwards_says_so_and_calls_none_of_it_new`
    already owns.
    """

    sandbox = tmp_path / name
    repo_root = _fixture_repo(sandbox)
    (repo_root / "src" / "app.py").write_text(_ALPHA_BEFORE, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    commented = _invoke(sandbox, [*base, "--comment", "why times two?", "--on", "src/app.py:L3"])
    assert commented.exit_code == 0, commented.output
    marked = _invoke(sandbox, [*base, "--mark", _symbol_unit_key(sandbox, repo_root, "src/app.py#0")])
    assert marked.exit_code == 0, marked.output
    (repo_root / "src" / "app.py").write_text(_ALPHA_AFTER, encoding="utf-8")
    return sandbox, repo_root


def _without_session_id(output: str) -> str:
    """Blank durable object ids that are legitimately unique per sandbox."""

    normalized = re.sub(r"r/[0-9a-f]{8,64}", "r/<session>", output)
    return re.sub(r"(?m)^(  checkpoint )\S+$", r"\1<revision>", normalized)


# --------------------------------------------------------------------------- #
# the undo: content that changed and changed back
# --------------------------------------------------------------------------- #

# Line 3 carries the comment. Line 6 is `def beta_1(y):` -- named here because
# it is the line the tool used to point a comment at after an undo, and the
# assertions below check that number against this file rather than in the
# abstract.
_UNDO_BEFORE = """def alpha_1(x):
    y = x + 1
    return x * 2  # doubled


def beta_1(y):
    return y - 1


def gamma_1(z):
    return z * z
"""

# Renamed and three lines inserted, so the commented line moves 3 -> 6 and the
# owning definition is a name that only exists in this version of the file.
_UNDO_AFTER = """def renamed_alpha(x):
    extra = 0
    another = 1
    third = 2
    y = x + 1
    return x * 2  # doubled


def beta_1(y):
    return y - 1


def gamma_1(z):
    return z * z
"""


def _commented_then_rewritten(tmp_path: Path, name: str, *, on: str = "src/app.py:L3") -> tuple[Path, Path]:
    """A sandbox at revision 2: one comment written on revision 1, then a rewrite."""

    sandbox = tmp_path / name
    repo_root = _fixture_repo(sandbox)
    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    _invoke(sandbox, [*base, "--comment", "why doubled?", "--on", on])
    (repo_root / "src" / "app.py").write_text(_UNDO_AFTER, encoding="utf-8")
    assert _invoke(sandbox, [*base, "--since-my-review", "--comments"]).exit_code == 0
    return sandbox, repo_root


# --------------------------------------------------------------------------- #
# the frontier: durable state, not a shadow of the marks table
# --------------------------------------------------------------------------- #


def _symbol_unit_key(sandbox: Path, repo_root: Path, label: str) -> str:
    """The stored ``unit_key`` behind a printed unit label, via the JSON surface."""

    listed = _invoke(sandbox, ["--repo-root", str(repo_root), "--working-tree", "--units", "--json"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.stderr.strip().splitlines()[-1])
    for unit in payload["units"]:
        if unit["label"] == label:
            return str(unit["unit_key"])
    raise AssertionError(f"no unit labelled {label!r} in {[u['label'] for u in payload['units']]}")


def _symbol_reviewed_then_renamed(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A sandbox whose only verdict is on a symbol the agent then renames away.

    The mark is on the *symbol*, not the file, and that is the whole scenario:
    a symbol mark's unit leaves the review outright when the symbol is renamed,
    so reconciliation deletes the row -- taking with it the only evidence a
    marks-derived frontier had that this reviewer had ever looked at anything.
    """

    sandbox = tmp_path / name
    repo_root = _fixture_repo(sandbox)
    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    key = _symbol_unit_key(sandbox, repo_root, "src/app.py::alpha_1")
    marked = _invoke(sandbox, ["--repo-root", str(repo_root), "--working-tree", "--mark", key])
    assert marked.exit_code == 0, marked.output
    (repo_root / "src" / "app.py").write_text(_UNDO_AFTER, encoding="utf-8")
    return sandbox, repo_root


# --------------------------------------------------------------------------- #
# same-named symbols in one file
# --------------------------------------------------------------------------- #


def test_two_definitions_nesting_cannot_separate_get_a_line_disambiguator(tmp_path: Path) -> None:
    """Containment is not always enough; the label still has to tell them apart."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "svc.py").write_text(
        "def run():\n    return 1\n\n\ndef run():\n    return 2\n",
        encoding="utf-8",
    )
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--units", "--json"])

    assert result.exit_code == 0, result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])
    labels = [row["label"] for row in state["units"] if row["kind"] == "symbol"]
    assert labels == ["src/svc.py::run@L1", "src/svc.py::run@L5"]


# --------------------------------------------------------------------------- #
# --feedback and --finish: plan SS5.5's last two actions
# --------------------------------------------------------------------------- #


def test_feedback_renders_the_bundle_and_says_it_delivered_nothing(tmp_path: Path) -> None:
    """Plan SS10.1's bundle, reachable from the terminal.

    The disclaimer is load-bearing: this renders Markdown and stops, and a
    reviewer who read the header and assumed the agent had been told would stop
    watching for a reply that is never coming.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "return 3 is unexplained",
            "--on",
            "src/app.py:L2",
            "--comment-kind",
            "request_change",
        ],
    )
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--feedback", "--no-color"])

    assert result.exit_code == 0, result.output
    assert "FEEDBACK BUNDLE  (1 open · 0 orphaned · 0 resolved)" in result.output
    assert "nothing was sent to any agent, host or pull request" in result.output
    assert "## Review feedback" in result.output
    assert "### src/app.py:L2 — request change" in result.output
    assert "return 3 is unexplained" in result.output
    # Even here, the comment says how it is anchored.
    assert "file unchanged since the comment" in result.output
    assert "Human review REQUIRED" in result.output


def test_feedback_reaches_json_consumers_on_stderr(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--feedback", "--json"])

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert state["feedback"]["open"] == 0
    assert state["feedback"]["markdown"].startswith("## Review feedback")


def test_finish_records_the_decision_and_what_was_still_outstanding(tmp_path: Path) -> None:
    """Plan SS4.1: the human closes the review; the tool reports, never refuses.

    Nothing here reads as ``approved`` and nothing blocks a close over an open
    comment. What the reviewer is owed is the tally next to their own decision.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "why 3?", "--on", "src/app.py:L2"],
    )
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--finish", "--no-color"])

    assert result.exit_code == 0, result.output
    assert "REVIEW FINISHED" in result.output
    assert "comments still open      1" in result.output
    assert "APPROVED" not in result.output

    reopened = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--reopen-review", "--no-color"],
    )
    assert reopened.exit_code == 0, reopened.output
    assert "REVIEW REOPENED" in reopened.output


def test_finish_and_reopen_together_are_refused(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--finish", "--reopen-review"],
    )
    assert result.exit_code != 0
    assert "opposite things" in result.output


def test_discard_and_finish_together_are_refused(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--finish", "--discard-review"],
    )
    assert result.exit_code != 0
    assert "opposite things" in result.output


def test_discard_review_is_read_only_until_explicit_restore(tmp_path: Path) -> None:
    """Discard says deletion plainly and cannot be mutated through a normal reopen."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--discard-review", "--no-color"],
    )
    assert result.exit_code == 0, result.output
    assert "REVIEW DISCARDED" in result.output
    assert "retention may permanently delete this review and its evidence" in result.output

    blocked = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--marks"])
    assert blocked.exit_code != 0
    assert "discarded and read-only" in blocked.output
    assert "--reopen-review" in blocked.output

    restored = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--reopen-review", "--no-color"],
    )
    assert restored.exit_code == 0, restored.output
    assert "REVIEW REOPENED" in restored.output

    from lemoncrow.pro.capabilities.review.store import ReviewStore

    databases = list((tmp_path / "server" / "reviews").glob("*/lemoncrow_reviews.db"))
    assert len(databases) == 1
    store = ReviewStore(databases[0].parent)
    open_sessions = [session for session in store.list_sessions(status="open")]
    assert len(open_sessions) == 1
    assert store.prune(older_than_days=0) == 0


def test_finish_survives_into_the_stored_session(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--finish", "--json"])
    assert result.exit_code == 0, result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert state["closure"]["status"] == "finished"

    stored = _invoke(tmp_path, ["--show-review", state["review_id"], "--json"])
    assert stored.exit_code == 0, stored.output
    detail = json.loads(stored.output)
    assert detail["review"]["ref"] == state["review_id"]
    assert detail["review"]["short_id"] == state["review_id"].removeprefix("r/")
    assert detail["review"]["id"]
    assert detail["review"]["status"] == "finished"


def test_review_setup_reports_detected_surfaces_without_opening_review(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    frontend = root / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(json.dumps({"dependencies": {"vite": "8", "react": "19"}}))

    result = _invoke(tmp_path, ["--repo-root", str(root), "--setup", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    web = [item for item in payload["detected"] if item["provider"] == "web"]
    assert len(web) == 1
    assert web[0]["root"] == "frontend"
    assert web[0]["framework"] == "vite"
    assert not (root / ".lemoncrow" / "review.yaml").exists()


def test_review_setup_can_write_high_confidence_config(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    (root / "package.json").write_text(json.dumps({"dependencies": {"astro": "5"}}))

    result = _invoke(
        tmp_path,
        ["--repo-root", str(root), "--setup", "--write-review-config"],
    )

    assert result.exit_code == 0, result.output
    config = root / ".lemoncrow" / "review.yaml"
    assert config.is_file()
    text = config.read_text(encoding="utf-8")
    assert "provider: web" in text
    assert "framework: astro" in text


def _python_command(code: str) -> str:
    import shlex
    import sys

    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _recorded_checks(repo_root: Path, review_id: str, revision_id: str) -> dict[str, dict[str, Any]]:
    from urllib.parse import quote

    from lemoncrow_client.config import load_config

    from lemoncrow.pro.capabilities.review.hosted import server_review_request

    payload = server_review_request(
        load_config(cwd=repo_root),
        "GET",
        f"/api/reviews/{quote(review_id, safe='')}/verification?revision_id={quote(revision_id, safe='')}",
    )
    assert payload["snapshot_id"] == revision_id
    return {str(row["name"]): row for row in payload["checks"]}


def _check(tmp_path: Path, repo_root: Path, command: str, *extra: str) -> Any:
    return _invoke(tmp_path, ["--repo-root", str(repo_root), *extra, "--check", "unit", "--run", command, "--json"])


def test_a_local_check_publishes_its_result_to_the_exact_working_tree_snapshot(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    result = _check(tmp_path, repo_root, _python_command("raise SystemExit(0)"))

    assert result.exit_code == 0, result.output
    published = json.loads(result.stdout)
    assert published["status"] == "PASS"
    row = _recorded_checks(repo_root, published["review_id"], published["revision_id"])["unit"]
    assert (row["status"], row["state"]) == ("PASS", "current")


def test_a_failing_local_check_is_recorded_and_fails_the_hook(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    result = _check(tmp_path, repo_root, _python_command("raise SystemExit(3)"))

    assert result.exit_code == 3, result.output
    published = json.loads(result.stdout)
    row = _recorded_checks(repo_root, published["review_id"], published["revision_id"])["unit"]
    assert (row["status"], row["state"]) == ("FAIL", "current")


def test_a_check_that_edits_the_checkout_is_never_bound_as_pass_or_fail(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    target = repo_root / "src" / "app.py"
    target.write_text("def one():\n    return 3\n", encoding="utf-8")
    edit = f"import pathlib; pathlib.Path({str(target)!r}).write_text('def one():\\n    return 4\\n')"

    result = _check(tmp_path, repo_root, _python_command(edit))

    assert result.exit_code == 1, result.output
    published = json.loads(result.stdout)
    assert published["status"] == "UNKNOWN"
    row = _recorded_checks(repo_root, published["review_id"], published["revision_id"])["unit"]
    assert row["status"] == "UNKNOWN"


def test_a_check_after_commit_binds_to_the_checked_out_commit(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)  # clean tree: the Review is the commit HEAD checks out

    result = _check(tmp_path, repo_root, _python_command("raise SystemExit(0)"))

    assert result.exit_code == 0, result.output
    published = json.loads(result.stdout)
    assert _recorded_checks(repo_root, published["review_id"], published["revision_id"])["unit"]["status"] == "PASS"


def test_a_check_refuses_a_snapshot_the_checkout_does_not_hold(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    ran = tmp_path / "ran"
    command = _python_command(f"import pathlib; pathlib.Path({str(ran)!r}).touch()")

    for extra in (["HEAD~1"], ["--staged"]):
        result = _check(tmp_path, repo_root, command, *extra)
        assert result.exit_code != 0, result.output
        assert "--check can only bind" in result.output

    assert not ran.exists()


def test_human_judgment_is_refused_from_inside_an_agent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    # The coding agent's own shell: it may capture, read and run checks, but a
    # mark, comment, finish or GitHub verdict recorded from here would be the
    # agent speaking as the human reviewer.
    monkeypatch.setenv("CLAUDECODE", "1")

    for judgment in (
        ["--mark", "src/app.py"],
        ["--comment", "looks fine", "--on", "src/app.py:L1"],
        ["--finish"],
        ["--discard-review"],
        ["--reopen-review"],
    ):
        refused = _invoke(tmp_path, ["--repo-root", str(repo_root), *judgment])
        assert refused.exit_code != 0, (judgment, refused.output)
        assert "inside an agent session" in refused.output, (judgment, refused.output)

    captured = _invoke(tmp_path, ["--repo-root", str(repo_root), "--track", "--json"])
    assert captured.exit_code == 0, captured.output


def test_a_check_needs_its_command_and_runs_on_its_own(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)

    alone = _invoke(tmp_path, ["--repo-root", str(repo_root), "--check", "unit"])
    combined = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--check", "unit", "--run", "true", "--mark", "src/app.py"],
    )

    assert alone.exit_code != 0 and "--check NAME and --run COMMAND go together" in alone.output
    assert combined.exit_code != 0 and "--check runs on its own; drop --mark" in combined.output


def test_review_new_and_continue_forward_explicit_lifecycle_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.gateway.cli.commands import review as review_mod

    repo_root = _fixture_repo(tmp_path)
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    seen: list[dict[str, Any]] = []

    def _server(_config: Any, _root: Path, _rng: Any, **kwargs: Any) -> None:
        seen.append(kwargs)

    monkeypatch.setattr(review_mod, "_review_server_capture", _server)

    explicit = _invoke(tmp_path, ["--repo-root", str(repo_root), "--new", "--base", "HEAD"])
    assert explicit.exit_code == 0, explicit.output
    assert seen[-1]["force_new_review"] is True

    created = _invoke(tmp_path, ["--repo-root", str(repo_root), "--new"])
    assert created.exit_code == 0, created.output
    assert seen[-1]["force_new_review"] is True
    assert seen[-1]["continue_review_id"] == ""

    continued = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--continue", "r/12345678"],
    )
    assert continued.exit_code == 0, continued.output
    assert seen[-1]["force_new_review"] is False
    assert seen[-1]["continue_review_id"] == "r/12345678"

    invalid = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--new", "--continue", "r/12345678"],
    )
    assert invalid.exit_code != 0
    assert "mutually exclusive" in invalid.output


def test_no_browser_mode_takes_the_paired_hand_open_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    from lemoncrow.gateway.cli.commands import review as review_command
    from lemoncrow.pro.capabilities.review import hosted as hosted_mod

    monkeypatch.setenv("LEMONCROW_NO_BROWSER", "1")
    url = "http://127.0.0.1:7420/r/deadbeef"
    assert review_command._launch_browser_url(url) is False

    monkeypatch.setattr(hosted_mod, "pair_local_review_browser", lambda _config, _path: {"expires_in": 60})
    config = SimpleNamespace(hosted=False, token="machine-token")
    assert review_command._open_server_review(url, config=config, review_id="deadbeef", open_browser=True) is False

    captured = capsys.readouterr()
    assert f"Open {url} within 60 seconds" in captured.err
    assert url not in captured.out
