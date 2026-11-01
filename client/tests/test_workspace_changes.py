"""``lc workspace status|branch|commit|push|pr`` against real Git repositories."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from lemoncrow_client import workspace_changes
from lemoncrow_client.workspace_changes import run


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "gitconfig"
    config.write_text(
        "[user]\n\tname = Tester\n\temail = tester@example.test\n[init]\n\tdefaultBranch = main\n"
        '[protocol "file"]\n\tallow = always\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def commit_file(repo: Path, name: str, text: str, message: str = "change") -> None:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def make_repo(base: Path, name: str, remotes: Path) -> Path:
    """A repository on ``main`` pushed to a local bare remote."""

    remotes.mkdir(exist_ok=True)
    bare = remotes / f"{name.replace('/', '_')}.git"
    git(base, "init", "-q", "--bare", str(bare))
    repo = base / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    commit_file(repo, "README.md", f"{name}\n", "init")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "main")
    return repo


@dataclass(frozen=True)
class Workspace:
    root: Path
    alpha: Path
    beta: Path
    gamma: Path
    core: Path
    remotes: Path


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    """A folder root: ``alpha``, ``beta`` and ``gamma``, which holds the submodule ``libs/core``."""

    root = tmp_path / "ws"
    root.mkdir()
    remotes = tmp_path / "remotes"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    alpha = make_repo(root, "alpha", remotes)
    beta = make_repo(root, "beta", remotes)
    core_origin = make_repo(scratch, "core", remotes)
    gamma = make_repo(root, "gamma", remotes)
    git(gamma, "submodule", "add", "-q", str(remotes / "core.git"), "libs/core")
    git(gamma, "commit", "-q", "-m", "add core")
    git(gamma, "push", "-q")
    git(gamma / "libs" / "core", "switch", "-q", "--detach")  # as `git submodule update` leaves it
    del core_origin
    return Workspace(root, alpha, beta, gamma, gamma / "libs" / "core", remotes)


def lc(*arguments: str) -> tuple[int, str]:
    sink = io.StringIO()
    code = run(arguments[0], list(arguments[1:]), sink)
    return code, sink.getvalue()


def branch_of(repo: Path) -> str:
    return git(repo, "branch", "--show-current")


def test_status_reports_branch_counts_tracking_and_drift(workspace: Workspace) -> None:
    ws = workspace
    # alpha: one local commit not pushed, plus staged, unstaged and untracked changes.
    commit_file(ws.alpha, "a.txt", "one\n", "local")
    (ws.alpha / "README.md").write_text("edited\n")
    (ws.alpha / "new.txt").write_text("staged\n")
    git(ws.alpha, "add", "new.txt")
    (ws.alpha / "junk.txt").write_text("untracked\n")
    # beta: the remote moved on.
    other = ws.root.parent / "other"
    git(ws.root.parent, "clone", "-q", str(ws.remotes / "beta.git"), str(other))
    commit_file(other, "b.txt", "remote\n", "remote work")
    git(other, "push", "-q")
    git(ws.beta, "fetch", "-q")
    # core: a commit in the submodule the parent does not record.
    commit_file(ws.core, "c.txt", "c\n", "core work")

    code, text = lc("status", "--root", str(ws.root))
    assert code == 0, text
    lines = {line.split(":", 1)[0]: line for line in text.splitlines()}
    assert "main (origin/main, ahead 1 behind 0); 1 staged, 1 unstaged, 1 untracked" in lines["alpha"]
    assert "main (origin/main, ahead 0 behind 1); clean" in lines["beta"]
    assert "1 unstaged" in lines["gamma"]  # the moved submodule pointer
    assert "detached @" in lines["gamma/libs/core"]
    assert "drifted: HEAD" in lines["gamma/libs/core"]

    code, text = lc("status", "--json", "--root", str(ws.root), "--only", "alpha,gamma/libs/core")
    assert code == 0
    data = json.loads(text)
    rows = {row["path"]: row for row in data["members"]}
    assert set(rows) == {"alpha", "gamma/libs/core"}
    assert (rows["alpha"]["branch"], rows["alpha"]["ahead"], rows["alpha"]["behind"]) == ("main", 1, 0)
    assert (rows["alpha"]["staged"], rows["alpha"]["unstaged"], rows["alpha"]["untracked"]) == (1, 1, 1)
    assert rows["gamma/libs/core"]["detached"] is True
    assert rows["gamma/libs/core"]["parent"] == "gamma"
    assert rows["gamma/libs/core"]["drift"] is True
    assert rows["gamma/libs/core"]["recorded"] == git(ws.gamma, "ls-tree", "HEAD", "libs/core").split()[2]
    assert rows["alpha"]["drift"] is None


def test_status_submodule_in_sync(workspace: Workspace) -> None:
    code, text = lc("status", "--root", str(workspace.root), "--only", "gamma/libs/core")
    assert code == 0
    assert "in sync with parent" in text


def test_branch_switches_changed_repos_and_the_submodule_parent(workspace: Workspace) -> None:
    ws = workspace
    (ws.alpha / "README.md").write_text("edited\n")
    (ws.core / "c.txt").write_text("dirty\n")

    code, text = lc("branch", "feat", "--root", str(ws.root))
    assert code == 0, text
    assert branch_of(ws.alpha) == "feat"
    assert branch_of(ws.core) == "feat"
    assert branch_of(ws.gamma) == "feat"  # holds the changed submodule
    assert branch_of(ws.beta) == "main"
    assert "beta" not in text
    assert (ws.alpha / "README.md").read_text() == "edited\n"  # changes carried over

    # An existing branch is switched to, not recreated.
    git(ws.beta, "branch", "feat")
    code, text = lc("branch", "feat", "--root", str(ws.root), "--only", "beta")
    assert code == 0, text
    assert branch_of(ws.beta) == "feat"
    assert "switched to feat" in text


def test_branch_only_and_unknown_name(workspace: Workspace) -> None:
    ws = workspace
    code, text = lc("branch", "topic", "--root", str(ws.root), "--only", "beta")
    assert code == 0, text
    assert branch_of(ws.beta) == "topic"
    assert branch_of(ws.alpha) == "main"

    code, text = lc("branch", "topic", "--root", str(ws.root), "--only", "nope")
    assert code == 2
    assert "unknown repository: nope" in text
    assert "alpha" in text and "gamma/libs/core" in text  # lists the members

    code, _ = lc("branch", "--root", str(ws.root))  # NAME missing
    assert code == 2


def test_branch_reports_member_failure_and_continues(workspace: Workspace) -> None:
    ws = workspace
    (ws.alpha / "README.md").write_text("edited\n")
    (ws.beta / "README.md").write_text("edited\n")
    # alpha has a conflicting local change against the target branch, so git refuses the switch.
    git(ws.alpha, "switch", "-q", "-c", "other")
    git(ws.alpha, "commit", "-q", "-am", "diverge")
    git(ws.alpha, "switch", "-q", "main")
    (ws.alpha / "README.md").write_text("conflicting\n")

    code, text = lc("branch", "other", "--root", str(ws.root), "--only", "alpha,beta")
    assert code == 1, text
    assert "alpha: FAILED" in text
    assert "beta: created and switched to other" in text
    assert branch_of(ws.beta) == "other"


def test_commit_orders_submodule_before_parent_and_records_its_head(workspace: Workspace) -> None:
    ws = workspace
    (ws.core / "c.txt").write_text("core change\n")
    (ws.gamma / "g.txt").write_text("gamma change\n")
    assert lc("branch", "feat", "--root", str(ws.root))[0] == 0

    code, text = lc("commit", "-m", "work", "--root", str(ws.root))
    assert code == 0, text
    lines = text.splitlines()
    assert lines[0].startswith("gamma/libs/core: committed")
    assert lines[1].startswith("gamma: committed")
    core_head = git(ws.core, "rev-parse", "HEAD")
    assert git(ws.gamma, "ls-tree", "HEAD", "libs/core").split()[2] == core_head
    assert git(ws.gamma, "show", "--stat", "--format=%s", "HEAD").splitlines()[0] == "work"
    assert git(ws.alpha, "rev-list", "--count", "HEAD") == "1"  # untouched

    code, text = lc("commit", "-m", "again", "--root", str(ws.root), "--only", "gamma")
    assert code == 0
    assert "nothing to commit" in text


def test_commit_refuses_detached_head_and_does_not_commit_the_parent_pointer(workspace: Workspace) -> None:
    ws = workspace
    (ws.core / "c.txt").write_text("core change\n")
    (ws.gamma / "g.txt").write_text("gamma change\n")
    parent_before = git(ws.gamma, "rev-parse", "HEAD")

    code, text = lc("commit", "-m", "work", "--root", str(ws.root))
    assert code == 1, text
    assert "gamma/libs/core: FAILED: refused, detached HEAD" in text
    assert "lc workspace branch NAME" in text
    assert "gamma: skipped, submodule gamma/libs/core was not committed" in text
    assert git(ws.gamma, "rev-parse", "HEAD") == parent_before
    assert git(ws.core, "status", "--porcelain") == "?? c.txt"  # nothing staged or committed
    assert git(ws.gamma, "diff", "--cached", "--name-only") == ""


def test_commit_records_a_clean_detached_submodule_at_a_pushed_commit(workspace: Workspace, tmp_path: Path) -> None:
    ws = workspace
    # Upstream moves on; the submodule is updated to it, detached and clean (as `git submodule update --remote`).
    other = tmp_path / "other-core"
    git(tmp_path, "clone", "-q", str(ws.remotes / "core.git"), str(other))
    commit_file(other, "new.txt", "upstream\n", "upstream change")
    git(other, "push", "-q", "origin", "main")
    git(ws.core, "fetch", "-q", "origin")
    git(ws.core, "switch", "-q", "--detach", "origin/main")
    assert lc("branch", "bump", "--root", str(ws.root), "--only", "gamma")[0] == 0

    code, text = lc("commit", "-m", "bump core", "--root", str(ws.root))
    assert code == 0, text
    assert "gamma/libs/core: skipped, nothing to commit, detached @" in text
    assert git(ws.gamma, "ls-tree", "HEAD", "libs/core").split()[2] == git(ws.core, "rev-parse", "HEAD")

    # A detached commit no remote has is still refused, and the parent is left alone.
    commit_file(ws.core, "local.txt", "local\n", "local only")
    parent_before = git(ws.gamma, "rev-parse", "HEAD")
    code, text = lc("commit", "-m", "again", "--root", str(ws.root))
    assert code == 1, text
    assert "gamma/libs/core: FAILED: refused, detached HEAD" in text
    assert git(ws.gamma, "rev-parse", "HEAD") == parent_before


def test_push_sends_submodule_first_and_sets_upstream(workspace: Workspace) -> None:
    ws = workspace
    (ws.core / "c.txt").write_text("core change\n")
    (ws.alpha / "a.txt").write_text("alpha change\n")
    assert lc("branch", "feat", "--root", str(ws.root))[0] == 0
    assert lc("commit", "-m", "work", "--root", str(ws.root))[0] == 0

    code, text = lc("push", "--root", str(ws.root))
    assert code == 0, text
    lines = text.splitlines()
    pushed = [line.split(":")[0] for line in lines if ": pushed feat" in line]
    assert pushed.index("gamma/libs/core") < pushed.index("gamma")
    assert set(pushed) == {"alpha", "gamma", "gamma/libs/core"}
    assert any(line.startswith("beta: skipped") for line in lines)
    for repo in (ws.alpha, ws.gamma, ws.core):
        assert git(repo, "rev-parse", "--abbrev-ref", "@{u}") == "origin/feat"
    core_head = git(ws.core, "rev-parse", "HEAD")
    assert git(ws.remotes, "--git-dir", str(ws.remotes / "core.git"), "rev-parse", "refs/heads/feat") == core_head
    assert git(ws.remotes, "--git-dir", str(ws.remotes / "gamma.git"), "rev-parse", "refs/heads/feat") == git(
        ws.gamma, "rev-parse", "HEAD"
    )

    code, text = lc("push", "--root", str(ws.root))  # now up to date
    assert code == 0
    assert "pushed" not in text


def test_push_failure_blocks_the_parent(workspace: Workspace) -> None:
    ws = workspace
    (ws.core / "c.txt").write_text("core change\n")
    assert lc("branch", "feat", "--root", str(ws.root))[0] == 0
    assert lc("commit", "-m", "work", "--root", str(ws.root))[0] == 0
    git(ws.core, "remote", "set-url", "origin", str(ws.remotes / "missing.git"))

    code, text = lc("push", "--root", str(ws.root), "--only", "gamma,gamma/libs/core")
    assert code == 1, text
    assert "gamma/libs/core: FAILED" in text
    assert "gamma: skipped, submodule gamma/libs/core was not pushed" in text


def write_fake_gh(directory: Path) -> Path:
    """A ``gh`` that logs its argv (JSON lines in ``$GH_LOG``) and keeps pull requests as files in ``$GH_STATE``."""

    directory.mkdir(exist_ok=True)
    script = directory / "gh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "cwd = os.getcwd()\n"
        "name = os.path.basename(cwd)\n"
        "with open(os.environ['GH_LOG'], 'a') as log:\n"
        "    log.write(json.dumps({'cwd': cwd, 'argv': args}) + '\\n')\n"
        "state = os.path.join(os.environ['GH_STATE'], name + '.json')\n"
        "def option(flag):\n"
        "    return args[args.index(flag) + 1] if flag in args else None\n"
        "if args[:2] == ['pr', 'view']:\n"
        "    if not os.path.exists(state):\n"
        "        sys.stderr.write('no pull requests found\\n'); sys.exit(1)\n"
        "    print(open(state).read()); sys.exit(0)\n"
        "if args[:2] == ['pr', 'create']:\n"
        "    url = 'https://example.test/' + name + '/pull/1'\n"
        "    json.dump({'url': url, 'body': option('--body') or '', 'state': 'OPEN'}, open(state, 'w'))\n"
        "    print('Creating pull request...'); print(url); sys.exit(0)\n"
        "if args[:2] == ['pr', 'edit']:\n"
        "    data = json.load(open(state)); data['body'] = option('--body')\n"
        "    json.dump(data, open(state, 'w')); sys.exit(0)\n"
        "sys.exit(3)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def gh_calls(log: Path) -> list[dict[str, object]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def pushed(workspace: Workspace) -> Workspace:
    ws = workspace
    (ws.core / "c.txt").write_text("core change\n")
    (ws.alpha / "a.txt").write_text("alpha change\n")
    assert lc("branch", "feat", "--root", str(ws.root))[0] == 0
    assert lc("commit", "-m", "Add the thing", "--root", str(ws.root))[0] == 0
    assert lc("push", "--root", str(ws.root))[0] == 0
    return ws


def test_pr_creates_and_cross_links(pushed: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = pushed
    write_fake_gh(tmp_path / "bin")
    state = tmp_path / "gh-state"
    state.mkdir()
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("GH_STATE", str(state))

    code, text = lc("pr", "--draft", "--body", "Why", "--root", str(ws.root))
    assert code == 0, text
    assert "beta: skipped, on its default branch main" in text
    for name in ("alpha", "gamma", "core"):
        assert f"https://example.test/{name}/pull/1" in text
    calls = gh_calls(log)
    creates = [call for call in calls if call["argv"][:2] == ["pr", "create"]]  # type: ignore[index]
    assert len(creates) == 3
    assert all("--draft" in call["argv"] for call in creates)  # type: ignore[operator]
    assert creates[0]["argv"][creates[0]["argv"].index("--title") + 1] == "Add the thing"  # type: ignore[index]

    gamma_body = json.loads((state / "gamma.json").read_text())["body"]
    assert gamma_body.startswith("Why\n\n## Related pull requests\n")
    assert "https://example.test/alpha/pull/1" in gamma_body
    assert "https://example.test/core/pull/1" in gamma_body
    assert "https://example.test/gamma/pull/1" not in gamma_body
    edits = [call for call in calls if call["argv"][:2] == ["pr", "edit"]]  # type: ignore[index]
    assert len(edits) == 3

    # Running it again reuses every pull request and does not create or duplicate anything.
    before = len(calls)
    code, text = lc("pr", "--root", str(ws.root))
    assert code == 0, text
    assert text.count("reused") == 3
    again = gh_calls(log)[before:]
    assert not [call for call in again if call["argv"][:2] == ["pr", "create"]]  # type: ignore[index]
    assert not [call for call in again if call["argv"][:2] == ["pr", "edit"]]  # type: ignore[index]
    assert json.loads((state / "gamma.json").read_text())["body"] == gamma_body


def test_pr_reuses_an_existing_pull_request(pushed: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = pushed
    write_fake_gh(tmp_path / "bin")
    state = tmp_path / "gh-state"
    state.mkdir()
    (state / "alpha.json").write_text(
        json.dumps({"url": "https://example.test/alpha/pull/7", "body": "Existing text", "state": "OPEN"})
    )
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("GH_STATE", str(state))

    code, text = lc("pr", "--root", str(ws.root), "--only", "alpha,gamma")
    assert code == 0, text
    assert "alpha: reused https://example.test/alpha/pull/7" in text
    assert "gamma: created" in text
    creates = [call for call in gh_calls(log) if call["argv"][:2] == ["pr", "create"]]  # type: ignore[index]
    assert [Path(str(call["cwd"])).name for call in creates] == ["gamma"]
    alpha_body = json.loads((state / "alpha.json").read_text())["body"]
    assert alpha_body.startswith("Existing text\n\n## Related pull requests\n")
    assert "https://example.test/gamma/pull/1" in alpha_body


def test_pr_skips_unpushed_branches(workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = workspace
    write_fake_gh(tmp_path / "bin")
    (tmp_path / "gh-state").mkdir()
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("GH_STATE", str(tmp_path / "gh-state"))
    (ws.alpha / "a.txt").write_text("alpha change\n")
    assert lc("branch", "feat", "--root", str(ws.root), "--only", "alpha")[0] == 0
    assert lc("commit", "-m", "work", "--root", str(ws.root), "--only", "alpha")[0] == 0

    code, text = lc("pr", "--root", str(ws.root), "--only", "alpha")
    assert code == 0, text
    assert "alpha: skipped, branch not pushed" in text
    assert not [call for call in gh_calls(tmp_path / "gh.log") if call["argv"][:2] == ["pr", "create"]]  # type: ignore[index]


def test_pr_without_gh_changes_nothing(pushed: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = pushed
    heads = {repo: git(repo, "rev-parse", "HEAD") for repo in (ws.alpha, ws.gamma, ws.core)}
    monkeypatch.setattr(workspace_changes.shutil, "which", lambda _name, *a, **k: None)

    code, text = lc("pr", "--root", str(ws.root))
    assert code == 2
    assert "gh" in text and "Nothing was changed" in text
    assert {repo: git(repo, "rev-parse", "HEAD") for repo in heads} == heads
    assert branch_of(ws.alpha) == "feat"
