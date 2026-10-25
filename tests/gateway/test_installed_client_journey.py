"""The first-time journey of the installed client, from the built artifacts and no source checkout.

``curl ... | bash``, then ``lc review``, then an AI review in the terminal. This builds the two artifacts the installer
ships (the Review client and the Review engine), lays them out as the installer does (the client at
``$LEMONCROW_HOME/review/client`` with ``lc`` and ``lcr`` in ``bin``, the engine at ``$LEMONCROW_HOME/review/engine``),
starts the engine's loopback server, and then types what a new developer types into a temporary HOME:

* ``lc --help`` and ``lc review --help`` name ``--ai``, ``hook``, ``init-action`` and ``config``;
* ``lc review`` captures (and ``--open`` arms the browser pairing: the command that once ended in
  ``ModuleNotFoundError: lemoncrow_client.session``);
* ``lc review --ai`` with no key says what to do in three lines, exits 3 and spends nothing; a dummy ``claude`` on the PATH
  is suggested by name and never run unless ``--agent claude`` names it; with a key it reviews, here against a loopback
  stand-in for the Anthropic API, through the SDK-free route the installed engine uses; a key the provider refuses is one
  line; ``lc settings set`` saves a key owner-only and the next review uses it;
* a ``.lemoncrow/review.yaml`` you have not committed is the config the review uses, and says so;
* ``lc review hook install`` and a real ``git push``: a finding stops it, no key and a crashing ``lc`` never do;
* ``lc review init-action`` and ``lc review config``;
* ``lc uninstall`` lists, then removes what the installer added, keeps what is the person's, and runs no ``npm``.

Run it (``slow``: excluded from the default run)::

    uv run pytest -m slow tests/gateway/test_installed_client_journey.py -q

Network: none with a warm uv cache (see ``test_review_engine_smoke``). No model key exists or is used.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.gateway.pr_bot_support import AUTH_BUGGY, BASE, RULE
from tests.gateway.test_review_engine_smoke import (
    _build,
    _build_client,
    _engine,
    _Env,
    _git,
    _install_client,
    _isolated_env,
    _repository,
    _run,
    _tool_env,
)

pytestmark = pytest.mark.slow


class FakeAnthropic:
    """A loopback stand-in for the Anthropic Messages API that reads the review's prompt the way the fake model of the
    local-review tests does: a line ``BUG: <title>`` in the changed code is a confirmed finding."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.url = ""
        self._server: ThreadingHTTPServer | None = None
        #: ``(status, message)``: answer every call with the API's error body instead of a review.
        self.refuse: tuple[int, str] | None = None

    @staticmethod
    def _reply(content: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "msg_journey",
            "type": "message",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": json.dumps(content)}],
            "usage": {"input_tokens": 1200, "output_tokens": 180},
        }

    def answer(self, body: dict[str, Any]) -> dict[str, Any]:
        last = body["messages"][-1]["content"]
        try:
            prompt = json.loads(last if isinstance(last, str) else "".join(block.get("text", "") for block in last))
        except ValueError:
            return self._reply({"answer": "ok"})
        if "trusted_repository_rule" in prompt:
            source = prompt["snapshot_source"]["value"]
            hits = [target for target, row in source.items() if "BUG: " in str(row.get("text", ""))]
            if not hits:
                return self._reply(
                    {
                        "status": "satisfied",
                        "target_ids": [],
                        "title": "",
                        "explanation": "",
                        "consequence": "",
                        "evidence_refs": [],
                    }
                )
            title = re.search(r"BUG: (.+)", str(source[hits[0]]["text"]))
            assert title is not None
            return self._reply(
                {
                    "status": "violated",
                    "target_ids": [hits[0]],
                    "title": title.group(1).strip(),
                    "explanation": f"The change in {source[hits[0]]['path']} {title.group(1).strip()}.",
                    "consequence": "The rule no longer holds for this code.",
                    "evidence_refs": [f"target:{hits[0]}"],
                }
            )
        if "candidates" in prompt:
            rows = [
                {
                    "category": candidate["category"],
                    "target_ids": candidate["target_ids"],
                    "failure_mode": f"The changed {candidate['category']} path may be unsound.",
                    "investigation_rationale": "The deterministic triage flagged it.",
                    "priority_rationale": "It is on a consequential path.",
                    "evidence_needed": "Trace the changed lines.",
                    "rule_refs": candidate["rule_refs"],
                }
                for candidate in prompt["candidates"]["value"]
            ]
            return self._reply({"hypotheses": rows})
        if "hypothesis" in prompt:
            hypothesis = prompt["hypothesis"]["value"]
            targets = hypothesis["target_ids"]
            source = prompt["snapshot_source"]["value"]
            text = "\n".join(str(source[target].get("text", "")) for target in targets if target in source)
            found = re.search(r"BUG: (.+)", text)
            result: dict[str, Any] = {
                "status": "not_confirmed",
                "category": hypothesis["category"],
                "target_ids": targets,
                "title": "",
                "explanation": "",
                "consequence": "",
                "evidence_refs": [],
                "rule_refs": [],
            }
            if found is not None:
                result.update(status="confirmed", title=found.group(1).strip())
            return self._reply(result)
        return self._reply({"answer": "ok"})

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "key": self.headers.get("x-api-key"), "body": body})
                status = 200 if outer.refuse is None else outer.refuse[0]
                payload = outer.answer(body) if outer.refuse is None else {"error": {"message": outer.refuse[1]}}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@dataclass
class Journey:
    """What the installer leaves: ``lc`` in ``bin``, the client and the engine under ``home``, and a server."""

    home: Path
    user: Path
    env: _Env
    engine_url: str
    log: Path

    @property
    def lc(self) -> Path:
        return self.home / "bin" / "lc"

    def run(
        self, *arguments: str, cwd: Path, extra: dict[str, str] | None = None, ok: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess[str]:
        env = _Env({**self.env, **(extra or {})})
        done = subprocess.run(
            [str(self.lc), *arguments], cwd=cwd, env=env, capture_output=True, text=True, timeout=300, check=False
        )
        assert (
            done.returncode in ok
        ), f"lc {' '.join(arguments)} exited {done.returncode}:\n{done.stdout}\n{done.stderr}"
        return done


@pytest.fixture(scope="module")
def journey(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Journey]:
    tmp = tmp_path_factory.mktemp("journey")
    wheel, frontend = _build(tmp)
    client_archive = _build_client(tmp)
    home, user = tmp / "review-home", tmp / "review-user"  # where _install_client lays the client out
    # The engine, where the installer puts it (scripts/review_engine.sh: $LEMONCROW_HOME/review/engine).
    engine = home / "review" / "engine"
    tool = _tool_env(tmp)
    _run(["uv", "venv", "--python", f"{sys.version_info.major}.{sys.version_info.minor}", str(engine)], env=tool)
    _run(["uv", "pip", "install", "--python", str(engine / "bin" / "python"), str(wheel)], env=tool)
    log = tmp / "engine.log"
    with _engine(engine, tmp / "state", frontend, home, log) as server:
        _install_client(tmp, client_archive, server.url, server.token)
        env = _isolated_env(
            user,
            LEMONCROW_HOME=str(home),
            LEMONCROW_BIN_DIR=str(home / "bin"),
            LEMONCROW_NO_LAZY_INSTALL="1",
            BROWSER="true",
            NO_COLOR="1",
        )
        yield Journey(home=home, user=user, env=env, engine_url=server.url, log=log)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return _repository(tmp_path)


def test_help_names_the_ai_review_hook_init_action_and_config(journey: Journey, repository: Path) -> None:
    root = journey.run("--help", cwd=repository).stdout
    review = journey.run("review", "--help", cwd=repository).stdout
    for text in (root, review):
        for wanted in ("--ai", "hook", "init-action", "config"):
            assert wanted in text, f"{wanted!r} missing from:\n{text}"
    assert "No AI, no model call" in review  # a plain `lc review` says it spends nothing
    assert "Traceback" not in root + review


def _claim_browser(journey: Journey, review_url: str) -> int:
    """What the Reader does on first load: claim a Review-only capability for the review's path (status code)."""

    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        journey.engine_url + "/v1/auth/local-browser/claim",
        data=json.dumps({"path": "/" + review_url.split("/", 3)[3]}).encode(),
        headers={"Content-Type": "application/json", "Origin": journey.engine_url},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as claimed:
            assert json.loads(claimed.read())["token"]
            return claimed.status
    except urllib.error.HTTPError as refused:
        return refused.code


def test_lc_review_captures_and_open_arms_the_browser_without_crashing(journey: Journey, repository: Path) -> None:
    """``lc review``, ``--open``, ``--json`` and ``lcr`` crashed with ModuleNotFoundError after capture."""

    plain = journey.run("review", cwd=repository)  # --open is the default; BROWSER=true makes opening a no-op
    found = re.search(r"Review  (http://127\.0\.0\.1:\d+/r/\S+)", plain.stdout)
    assert found, plain.stdout
    assert "Traceback" not in plain.stderr and "ModuleNotFoundError" not in plain.stderr
    # It armed the pairing: a browser that opens that review's path may claim a Review-only capability, once.
    assert _claim_browser(journey, found.group(1)) == 200
    assert _claim_browser(journey, found.group(1)) != 200
    # A review nothing armed (--no-open) is refused: the Reader says "this browser isn't connected" for it.
    unarmed = journey.run("review", "--no-open", cwd=repository)
    assert _claim_browser(journey, re.search(r"Review  (\S+)", unarmed.stdout).group(1)) != 200  # type: ignore[union-attr]
    as_json = journey.run("review", "--open", "--json", cwd=repository)
    captured = json.loads(as_json.stdout.strip().splitlines()[-1])
    assert captured["review"]["id"] and captured["review_url"].startswith(journey.engine_url)
    lcr = subprocess.run(
        [str(journey.home / "bin" / "lcr"), "--no-open"],
        cwd=repository,
        env=journey.env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert lcr.returncode == 0 and "Traceback" not in lcr.stderr, lcr.stderr
    log = journey.log.read_text(encoding="utf-8")
    assert "ModuleNotFoundError" not in log and "Traceback" not in log, log[-4000:]


NO_KEY_LINES = (
    "No AI model is set up. Set a key:  export ANTHROPIC_API_KEY=sk-ant-...   (or OPENAI_API_KEY=sk-...)",
    "Or use an agent you are signed in to:  lc review --ai --agent claude   (also codex, opencode)",
    "Details: https://docs.lemoncrow.com/setup/ai-review",
)


def _agent_on_path(directory: Path, *names: str) -> tuple[dict[str, str], Path]:
    """A PATH with dummy agent CLIs that only print and record that they ran. Returns (environment, the record)."""

    directory.mkdir()
    ran = directory / "ran.txt"
    for name in names:
        script = directory / name
        script.write_text(f'#!/bin/sh\necho {name} "$@" >> {ran}\necho dummy {name} output\n', encoding="utf-8")
        script.chmod(0o755)
    return {"PATH": f"{directory}:/usr/bin:/bin"}, ran


def test_ai_review_with_no_key_is_three_lines_exit_3_and_spends_nothing(journey: Journey, repository: Path) -> None:
    clean = {"PATH": "/usr/bin:/bin"}  # no agent CLI on it, whatever this machine has
    done = journey.run("review", "--ai", cwd=repository, extra=clean, ok=(3,))
    assert done.stdout == "" and done.stderr.strip().splitlines() == list(NO_KEY_LINES)
    for jargon in ("provider", "driver", "Review Intelligence", "route", "Traceback"):
        assert jargon not in done.stderr
    strict = journey.run("review", "--ai", "--strict", cwd=repository, extra=clean, ok=(2,))
    assert strict.stderr.strip().splitlines() == list(NO_KEY_LINES)
    as_json = journey.run("review", "--ai", "--json", cwd=repository, extra=clean, ok=(3,))
    assert json.loads(as_json.stdout)["exit"]["code"] == 3
    # A flag that merely sounds like AI never starts a model.
    only_agent = journey.run("review", "--agent", "claude", cwd=repository, ok=(2,))
    assert "add --ai" in only_agent.stdout


def test_one_agent_on_the_path_is_suggested_by_name_and_only_run_when_named(
    journey: Journey, pushed: tuple[Path, Path], tmp_path: Path
) -> None:
    repo, _origin = pushed  # auth code: Auto depth sends a risk signal to the model, so a named agent really is asked
    environment, ran = _agent_on_path(tmp_path / "agents", "claude")
    found = journey.run("review", "--ai", "--base", "main", cwd=repo, extra=environment, ok=(3,))
    lines = found.stderr.strip().splitlines()
    assert lines[1].startswith("Found Claude Code:  lc review --ai --agent claude") and "--remember" in lines[1]
    assert not ran.exists()  # suggested, never started
    journey.run("review", "--ai", "--agent", "claude", "--base", "main", cwd=repo, extra=environment, ok=(0, 1, 2, 3))
    assert ran.exists() and ran.read_text().startswith("claude")  # named: now it may run (the dummy only prints)
    forgot = journey.run("review", "--ai", "--remember", cwd=repo, extra=environment, ok=(3,))
    assert "add --agent claude" in forgot.stderr  # --remember saves what you name; it never guesses


def test_a_key_the_provider_refuses_is_one_line_and_exit_3(journey: Journey, pushed: tuple[Path, Path]) -> None:
    repo, _origin = pushed
    provider = FakeAnthropic()
    provider.start()
    try:
        extra = {"ANTHROPIC_API_KEY": "sk-ant-wrong", "ANTHROPIC_BASE_URL": provider.url}
        for status, message, line in (
            (401, "invalid x-api-key", "Anthropic rejected the API key (HTTP 401): check ANTHROPIC_API_KEY."),
            (
                400,
                "Your credit balance is too low to access the Anthropic API.",
                "Anthropic says your account has no credit left (HTTP 400): add credit at console.anthropic.com/settings/billing.",
            ),
            (429, "slow down", "Anthropic rate limit reached (HTTP 429): wait a minute and run it again."),
        ):
            provider.refuse = (status, message)
            done = journey.run("review", "--ai", "--base", "main", "--fresh", cwd=repo, extra=extra, ok=(3,))
            shown = done.stderr.strip().splitlines()
            assert shown[-1].startswith(line), shown  # the provider's own words may follow it
            assert done.stdout == "" and "Traceback" not in done.stderr
            assert "Give Feedback" not in done.stdout + done.stderr and "\x1b" not in done.stderr
    finally:
        provider.stop()


def test_lc_settings_saves_a_key_owner_only_and_the_next_review_uses_it(
    journey: Journey, pushed: tuple[Path, Path]
) -> None:
    import stat

    repo, _origin = pushed
    done = subprocess.run(
        [str(journey.lc), "settings", "set", "llm.anthropic_api_key", "-"],  # the key is read from standard input
        input="sk-ant-saved-by-settings\n",
        cwd=repo,
        env=journey.env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "sk-ant-saved-by-settings" not in done.stdout + done.stderr
    assert "✓ llm.anthropic_api_key saved." in done.stdout
    settings_file = journey.home / "plugin_settings.json"
    assert f"Stored as plain text in {settings_file}, readable only by you (mode 600)." in done.stdout
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600
    shown = journey.run("settings", cwd=repo).stdout
    assert "llm.anthropic_api_key" in shown and "saved (hidden)" in shown and "sk-ant-saved" not in shown
    # The next review, with no exported key, uses the saved one.
    provider = FakeAnthropic()
    provider.start()
    try:
        review = journey.run(
            "review", "--ai", "--base", "main", cwd=repo, extra={"ANTHROPIC_BASE_URL": provider.url}, ok=(0, 1)
        )
        assert provider.requests, review.stderr
        assert all(call["key"] == "sk-ant-saved-by-settings" for call in provider.requests)
    finally:
        provider.stop()
    journey.run("settings", "reset", "llm.anthropic_api_key", cwd=repo)
    assert "sk-ant-saved" not in settings_file.read_text(encoding="utf-8")


def test_an_uncommitted_review_yaml_is_the_config_the_review_uses_and_it_says_so(
    journey: Journey, pushed: tuple[Path, Path]
) -> None:
    repo, _origin = pushed
    _git(repo, "checkout", "-q", "main")
    (repo / ".lemoncrow" / "review.yaml").write_text(
        RULE + "review:\n  strictness: 3\n  ignore: ['docs/**']\n  max_files: 40\n", encoding="utf-8"
    )  # edited, not committed
    provider = FakeAnthropic()
    provider.start()
    try:
        extra = {"ANTHROPIC_API_KEY": "sk-ant-journey", "ANTHROPIC_BASE_URL": provider.url}
        review = journey.run("review", "--ai", cwd=repo, extra=extra, ok=(0, 1))
        assert "Config: .lemoncrow/review.yaml from your working tree (not committed yet" in review.stdout
    finally:
        provider.stop()
    config = journey.run("review", "config", "--effective", cwd=repo).stdout
    assert "read from:    your working tree" in config
    for wanted in ("strictness", "3 (threshold high)", "ignore", "docs/**", "inline_comments", "max_files", "40"):
        assert wanted in config, config
    assert "from .lemoncrow/review.yaml" in config
    committed = journey.run("review", "config", "--effective", "--base", "HEAD", cwd=repo).stdout
    assert (
        "trusted base: HEAD" in committed and "3 (threshold high)" not in committed
    )  # HEAD's copy has no review: block


def test_an_unreachable_provider_is_a_sentence_and_not_a_banner_or_a_traceback(
    journey: Journey, pushed: tuple[Path, Path]
) -> None:
    repo, _origin = pushed  # auth code: Auto depth sends a risk signal to the model; a tidy change sends nothing
    done = journey.run(
        "review",
        "--ai",
        "--base",
        "main",
        cwd=repo,
        extra={"ANTHROPIC_API_KEY": "sk-ant-not-a-key", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"},
    )
    assert "could not reach Anthropic" in done.stdout, done.stdout
    assert "Traceback" not in done.stdout + done.stderr
    assert "Give Feedback" not in done.stdout + done.stderr  # the LiteLLM banner is not in this install


@pytest.fixture
def pushed(tmp_path: Path) -> tuple[Path, Path]:
    """``(clone, origin)``: ``main`` is on the origin with a critical rule; ``feature`` has a defect the rule forbids."""

    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "A"), ("user.email", "a@example.invalid"), ("commit.gpgsign", "false")):
        _git(repo, "config", key, value)
    for name, text in {**BASE, ".lemoncrow/review.yaml": RULE}.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "remote", "set-head", "origin", "main")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "src" / "auth.py").write_text(AUTH_BUGGY, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "buggy auth")
    return repo, origin


def _push(repo: Path, env: _Env) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "push", "origin", "feature"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _on_origin(origin: Path) -> bool:
    done = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "--verify", "-q", "refs/heads/feature"],
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def test_the_pre_push_hook_stops_on_a_finding_and_never_because_lemoncrow_broke(
    journey: Journey, pushed: tuple[Path, Path]
) -> None:
    repo, origin = pushed
    installed = journey.run("review", "hook", "install", cwd=repo)
    assert "calls " + str(journey.lc) in installed.stdout  # the hook names the lc that wrote it
    hook = repo / ".git" / "hooks" / "pre-push"
    assert f"LC={journey.lc}" in hook.read_text()

    # No key: the review cannot run, and the push goes through (the v0.8.0 hook refused it with a traceback).
    done = _push(repo, journey.env)
    assert done.returncode == 0 and _on_origin(origin), done.stderr
    assert "No AI model is set up" in done.stderr and "Traceback" not in done.stderr
    _git(origin, "update-ref", "-d", "refs/heads/feature")

    # A finding: the fake Anthropic API confirms the defect the repository's critical rule forbids.
    provider = FakeAnthropic()
    provider.start()
    try:
        with_key = _Env({**journey.env, "ANTHROPIC_API_KEY": "sk-ant-journey", "ANTHROPIC_BASE_URL": provider.url})
        stopped = _push(repo, with_key)
        assert stopped.returncode != 0 and not _on_origin(origin), stopped.stderr
        assert "P0 · must fix  src/auth.py" in stopped.stderr
        assert "stopped by an open finding at or above p0" in stopped.stderr
        assert provider.requests and all(call["key"] == "sk-ant-journey" for call in provider.requests)
        assert all(call["path"] == "/v1/messages" for call in provider.requests)
        # The same review as a command: exit 1 for the finding, with the findings printed.
        review = journey.run(
            "review",
            "--ai",
            "--base",
            "main",
            cwd=repo,
            extra={"ANTHROPIC_API_KEY": "sk-ant-journey", "ANTHROPIC_BASE_URL": provider.url},
            ok=(1,),
        )
        assert "P0 · must fix  src/auth.py" in review.stdout
    finally:
        provider.stop()

    # A lc that crashes (here: a module the install lost) never stops a push.
    client = next((journey.home / "review" / "client").glob("lib/python*/site-packages/lemoncrow_client"))
    lost = client / "review_engine.py"
    lost.rename(lost.with_suffix(".off"))
    try:
        crashed = _push(repo, journey.env)
    finally:
        lost.with_suffix(".off").rename(lost)
    assert crashed.returncode == 0 and _on_origin(origin), crashed.stderr
    assert "ModuleNotFoundError" in crashed.stderr and "not blocking this push" in crashed.stderr


def test_init_action_and_config_run_from_the_installed_lc(journey: Journey, pushed: tuple[Path, Path]) -> None:
    repo, _origin = pushed
    wrote = journey.run("review", "init-action", cwd=repo)
    assert (
        "Wrote .github/workflows/lemoncrow-review.yml" in wrote.stdout
        and "gh secret set ANTHROPIC_API_KEY" in wrote.stdout
    )
    assert (repo / ".github" / "workflows" / "lemoncrow-review.yml").is_file()
    config = journey.run("review", "config", "--effective", cwd=repo)
    assert "Effective Review configuration" in config.stdout and "no-bug-markers" in config.stdout


def test_zz_uninstall_lists_then_removes_what_the_installer_added_and_keeps_the_persons_own_files(
    journey: Journey, pushed: tuple[Path, Path], tmp_path: Path
) -> None:
    """Last on purpose: it removes the install the earlier tests share. A sandbox HOME, a fake ``npm`` that must not run."""

    repo, _origin = pushed
    user, home = journey.user, journey.home
    block = "# >>> lemoncrow path setup >>>\n. " + str(home / "shell-env.sh") + "\n# <<< lemoncrow path setup <<<\n"
    (home / "shell-env.sh").write_text("export PATH\n", encoding="utf-8")
    (user / ".bashrc").write_text("export KEEP=1\n\n" + block, encoding="utf-8")
    (user / ".profile").write_text("# my profile\n\n" + block, encoding="utf-8")
    (home / "review-local-ai").mkdir(exist_ok=True)
    (home / "review-local-ai" / "carried.state").write_text("cache", encoding="utf-8")
    journey.run("review", "hook", "install", cwd=repo)  # a hook it wrote, recorded for the uninstall
    hook = repo / ".git" / "hooks" / "pre-push"
    assert hook.is_file() and str(hook) in (home / "installed-hooks.txt").read_text(encoding="utf-8")
    npm = tmp_path / "npm-bin" / "npm"
    npm.parent.mkdir()
    npm.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "npm-ran"}\n', encoding="utf-8")
    npm.chmod(0o755)
    environment = {"PATH": f"{npm.parent}:/usr/bin:/bin"}
    settings_file = home / "plugin_settings.json"
    settings_file.write_text('{"llm.openai_api_key": "sk-keep-me"}', encoding="utf-8")

    def snapshot() -> tuple[tuple[str, bool, str], ...]:
        watched = [home / name for name in ("review", "bin", "shell-env.sh", "review-local-ai", "installed-hooks.txt")]
        watched += [settings_file, user / ".bashrc", user / ".profile", hook, repo / ".lemoncrow" / "review.yaml"]
        return tuple(
            (str(path), path.exists(), path.read_text(encoding="utf-8") if path.is_file() else "") for path in watched
        )

    # The dry run lists everything and changes nothing.
    before = snapshot()
    dry = journey.run("uninstall", "all", "--dry-run", cwd=repo, extra=environment)
    assert "Dry run: nothing was changed." in dry.stdout and "Removed" not in dry.stdout
    for wanted in (
        "The commands lc, lcr, lemoncrow-review-client",
        "The Review client and engine",
        "The lines the installer added to ~/.bashrc",
        "The lines the installer added to ~/.profile",
        "Your settings",
        "A git hook written by lc review hook install",
    ):
        assert wanted in dry.stdout, dry.stdout
    assert snapshot() == before

    # Without a terminal and without --yes it removes nothing.
    refused = journey.run("uninstall", "all", cwd=repo, extra=environment, ok=(2,))
    assert "Not a terminal, so nothing was removed" in refused.stdout and snapshot() == before

    # --yes removes what the installer added; settings (they hold a key), the hook and the repository stay.
    done = journey.run("uninstall", "all", "--yes", cwd=repo, extra=environment)
    assert "Kept, as listed above. To remove your data too: lc uninstall all --purge" in done.stdout
    assert not (home / "review").exists() and not (home / "bin").exists() and not (home / "shell-env.sh").exists()
    assert not (home / "review-local-ai").exists()
    assert (user / ".bashrc").read_text(encoding="utf-8") == "export KEEP=1\n"
    assert (user / ".profile").read_text(encoding="utf-8") == "# my profile\n"
    assert settings_file.read_text(encoding="utf-8") == '{"llm.openai_api_key": "sk-keep-me"}'
    assert hook.is_file() and (repo / ".lemoncrow" / "review.yaml").is_file()
    assert not (tmp_path / "npm-ran").exists()  # never runs npm
    # Open a new terminal: the command is gone.
    assert not journey.lc.exists()

    # --purge from the shell that is left (the commands are gone, so call the module the way the client does).
    from lemoncrow_client import uninstall

    sink = io.StringIO()
    assert (
        uninstall.main(["all", "--purge"], sink, env={**os.environ, **journey.env, **environment}, cwd=repo) == 0
    ), sink.getvalue()
    assert not settings_file.exists() and not hook.exists() and not (home / "installed-hooks.txt").exists()
    assert (repo / ".lemoncrow" / "review.yaml").is_file()  # a repository's folder is never purged blind
