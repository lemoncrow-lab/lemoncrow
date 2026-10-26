"""The pre-push hook, run by a real ``git push`` to a real bare remote.

The blocker this guards: a hook that called whatever ``lc`` was first on the PATH, so an installed client that
crashed (a missing module) made every push fail with a traceback, with no key and no ``--strict``. The hook must
stop a push for exactly two reasons, an open finding at or above ``--fail-on`` and (with ``--strict``) an incomplete
review, and never because LemonCrow broke.

``lc`` here is a small wrapper around the engine's own command line (``local_cli.main``) with the same fake model
the local-review tests use; a second wrapper crashes on purpose. Nothing calls a provider.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.gateway.pr_bot_support import AUTH_BUGGY, BASE, RULE, git
from tests.gateway.test_cli_review_local import make_lab

ROOT = Path(__file__).resolve().parents[2]

LAUNCHER = textwrap.dedent("""
    import sys
    sys.path[:0] = [{root!r}, {src!r}]
    if {fake!r}:
        from lemoncrow.pro.capabilities.review import intelligence_runtime
        from tests.gateway.pr_bot_support import FakeModel

        intelligence_runtime.execute_structured = FakeModel()
    from lemoncrow.pro.capabilities.review.local_cli.main import main

    arguments = sys.argv[1:]
    main(arguments[1:] if arguments[:1] == ["review"] else arguments)
    """)


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@dataclass
class Setup:
    repo: Path
    origin: Path
    tools: Path
    env: dict[str, str]
    lc: Path

    def push(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "push", "origin", "feature", *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )

    def install(self, *args: str, lc: Path | None = None) -> None:
        env = {**self.env, "LEMONCROW_LC_BIN": str(lc or self.lc)}
        done = subprocess.run(
            [str(lc or self.lc), "review", "hook", "install", "--repo-root", str(self.repo), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert done.returncode == 0, done.stdout + done.stderr

    def remote_has_feature(self) -> bool:
        return (
            subprocess.run(
                ["git", "-C", str(self.origin), "rev-parse", "--verify", "-q", "refs/heads/feature"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )


def _wrapper(tools: Path, name: str, *, fake: bool) -> Path:
    launcher = tools / f"{name}.py"
    launcher.write_text(LAUNCHER.format(root=str(ROOT), src=str(ROOT / "src"), fake=fake), encoding="utf-8")
    return _script(tools / name, f'exec "{sys.executable}" "{launcher}" "$@"\n')


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    """A repository with a critical rule, a bare origin that has ``main``, and a ``feature`` branch with a bug on it."""

    lab = make_lab(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": RULE})
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    git(lab.repo, "remote", "add", "origin", str(origin))
    git(lab.repo, "push", "-q", "origin", "main")
    git(lab.repo, "fetch", "-q", "origin")
    git(lab.repo, "remote", "set-head", "origin", "main")
    git(lab.repo, "checkout", "-qb", "feature")
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    tools = tmp_path / "tools"
    tools.mkdir()
    lc = _wrapper(tools, "lc", fake=True)
    env = {
        "PATH": f"{tools}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "LEMONCROW_ROOT": str(tmp_path / "store"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LEMONCROW_AST_GREP_BIN": str(tmp_path / "sg"),
        "OPENAI_API_KEY": "test-key",
        "LEMONCROW_LLM_BACKEND": "openai",
    }
    (tmp_path / "home").mkdir()
    return Setup(lab.repo, origin, tools, env, lc)


def test_the_hook_records_the_lc_that_wrote_it(setup: Setup) -> None:
    setup.install()
    text = (setup.repo / ".git" / "hooks" / "pre-push").read_text()
    assert f"LC={setup.lc}" in text


def test_an_open_finding_stops_the_push(setup: Setup) -> None:
    setup.install()
    done = setup.push()
    assert done.returncode != 0 and not setup.remote_has_feature()
    assert "P0 · must fix  src/auth.py" in done.stderr
    assert "stopped by an open finding at or above p0" in done.stderr
    assert "ModuleNotFoundError" not in done.stderr and "Traceback" not in done.stderr


def test_fail_on_none_lets_the_same_finding_through(setup: Setup) -> None:
    setup.install("--fail-on", "none")
    done = setup.push()
    assert done.returncode == 0 and setup.remote_has_feature(), done.stderr
    assert "P0 · must fix" in done.stderr  # it is still shown


def test_a_skipped_review_lets_the_push_through(setup: Setup) -> None:
    setup.install()
    done = setup.push()  # sanity: this one is stopped
    assert done.returncode != 0
    skipped = subprocess.run(
        ["git", "push", "origin", "feature"],
        cwd=setup.repo,
        env={**setup.env, "LEMONCROW_REVIEW_SKIP": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert skipped.returncode == 0 and setup.remote_has_feature()


def test_no_model_key_never_stops_a_push(setup: Setup) -> None:
    no_key = {key: value for key, value in setup.env.items() if key not in {"OPENAI_API_KEY", "LEMONCROW_LLM_BACKEND"}}
    # The real engine command with no fake model and no key, as an installed engine with nothing configured.
    plain_lc = _wrapper(setup.tools, "plain-lc", fake=False)
    setup.env = no_key
    setup.install(lc=plain_lc)
    done = setup.push()
    assert done.returncode == 0 and setup.remote_has_feature(), done.stderr
    assert "No AI model is set up" in done.stderr and "export ANTHROPIC_API_KEY" in done.stderr
    assert "Traceback" not in done.stderr


def test_no_model_key_stops_a_strict_hook_and_only_a_strict_hook(setup: Setup) -> None:
    no_key = {key: value for key, value in setup.env.items() if key not in {"OPENAI_API_KEY", "LEMONCROW_LLM_BACKEND"}}
    plain_lc = _wrapper(setup.tools, "plain-lc", fake=False)
    setup.env = no_key
    setup.install("--strict", lc=plain_lc)
    done = setup.push()
    assert done.returncode != 0 and not setup.remote_has_feature()
    assert "incomplete and --strict is set" in done.stderr


def test_a_crashing_lc_never_stops_a_push(setup: Setup) -> None:
    """The v0.8.0 failure: the installed client ended in ModuleNotFoundError, and the push was refused for it."""

    crash = _script(
        setup.tools / "crashing-lc",
        f'exec "{sys.executable}" -c "import lemoncrow_client.module_that_is_not_shipped" "$@"\n',
    )
    setup.install(lc=setup.lc)
    hook = setup.repo / ".git" / "hooks" / "pre-push"
    hook.write_text(hook.read_text().replace(f"LC={setup.lc}", f"LC={crash}"))
    done = setup.push()
    assert done.returncode == 0 and setup.remote_has_feature(), done.stderr
    assert "lc exited 1); not blocking this push" in done.stderr


def test_an_lc_that_is_gone_never_stops_a_push_and_says_so_only_once(setup: Setup) -> None:
    """The hook ``lc uninstall`` never found: a push still goes through, with one line the first time and none after."""

    setup.install()
    setup.lc.unlink()
    (setup.tools / "lc.py").unlink()
    done = setup.push()
    assert done.returncode == 0 and setup.remote_has_feature(), done.stderr
    assert "lc was not found, so this push was not reviewed" in done.stderr and "delete" in done.stderr
    assert ".git/hooks/pre-push" in done.stderr  # the file to delete, as git names it
    # Every later push of the same repository: nothing from LemonCrow at all.
    for branch in ("again", "and-again"):
        git(setup.repo, "checkout", "-qb", branch)
        quiet = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=setup.repo,
            env=setup.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert quiet.returncode == 0, quiet.stderr
        assert "lemoncrow" not in quiet.stderr.lower() and "lc was not found" not in quiet.stderr


def test_the_hook_uses_the_lc_that_wrote_it_not_another_one_on_the_path(setup: Setup) -> None:
    """The other half of the blocker: a different ``lc`` first on the PATH (a broken one) is not the hook's."""

    setup.install()
    _script(setup.tools / "elsewhere-lc", "exit 1\n")
    other = setup.tools.parent / "other-bin"
    other.mkdir()
    _script(other / "lc", "echo 'Traceback: not mine' >&2\nexit 1\n")
    setup.env["PATH"] = f"{other}:{setup.env['PATH']}"
    done = setup.push()
    assert "Traceback: not mine" not in done.stderr
    assert "P0 · must fix" in done.stderr and done.returncode != 0  # the recorded lc ran, and found the bug


def test_a_v2_hook_that_nagged_on_every_push_is_upgraded_in_place(setup: Setup) -> None:
    hook = setup.repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n# lemoncrow-review-hook v2 (pre-push)\nLC=''\n"
        'if [ -z "$LC" ]; then\n  echo "lemoncrow: lc was not found; not reviewing this push." >&2\n  exit 0\nfi\n'
    )
    hook.chmod(0o755)
    setup.install()  # no --force: it is ours
    text = hook.read_text()
    assert "lemoncrow-review-hook v3" in text and "this is said once" in text and os.access(hook, os.X_OK)


def test_a_v1_hook_is_upgraded_in_place(setup: Setup) -> None:
    hook = setup.repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n# lemoncrow-review-hook v1 (pre-push)\nif ! command -v lc >/dev/null 2>&1; then exit 0; fi\n"
        'exec lc review hook run --pre-push --fail-on p0 --depth auto "$@"\n'
    )
    hook.chmod(0o755)
    setup.install()  # no --force: it is ours, and the old one blocks a push when lc crashes
    assert "lemoncrow-review-hook v3" in hook.read_text() and os.access(hook, os.X_OK)
