"""The first-time journey of the installed client, from the built artifacts and no source checkout.

``curl ... | bash``, then ``lc review``, then an AI review in the terminal. This builds the two artifacts the installer
ships (the Review client and the Review engine), lays them out as the installer does (the client at
``$LEMONCROW_HOME/review/client`` with ``lc`` and ``lcr`` in ``bin``, the engine at ``$LEMONCROW_HOME/review/engine``),
starts the engine's loopback server, and then types what a new developer types into a temporary HOME:

* ``lc --help`` and ``lc review --help`` name ``--ai``, ``hook``, ``init-action`` and ``config``;
* ``lc review`` captures (and ``--open`` arms the browser pairing: the command that once ended in
  ``ModuleNotFoundError: lemoncrow_client.session``);
* ``lc review --ai`` with no key says what to do and spends nothing; with a key it reviews, here against a loopback
  stand-in for the Anthropic API, through the SDK-free route the installed engine uses;
* ``lc review hook install`` and a real ``git push``: a finding stops it, no key and a crashing ``lc`` never do;
* ``lc review init-action`` and ``lc review config``.

Run it (``slow``: excluded from the default run)::

    uv run pytest -m slow tests/gateway/test_installed_client_journey.py -q

Network: none with a warm uv cache (see ``test_review_engine_smoke``). No model key exists or is used.
"""

from __future__ import annotations

import json
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
                data = json.dumps(outer.answer(body)).encode()
                self.send_response(200)
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


def test_ai_review_with_no_key_says_what_to_do_and_spends_nothing(journey: Journey, repository: Path) -> None:
    done = journey.run("review", "--ai", cwd=repository)
    assert "no model route is configured" in done.stdout
    assert "export ANTHROPIC_API_KEY" in done.stdout and "--agent" in done.stdout
    assert "Traceback" not in done.stdout + done.stderr
    strict = journey.run("review", "--ai", "--strict", cwd=repository, ok=(2,))
    assert "incomplete and --strict is set" in strict.stdout
    # A flag that merely sounds like AI never starts a model.
    only_agent = journey.run("review", "--agent", "claude", cwd=repository, ok=(2,))
    assert "add --ai" in only_agent.stdout


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
    assert "no model route is configured" in done.stderr and "Traceback" not in done.stderr
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
