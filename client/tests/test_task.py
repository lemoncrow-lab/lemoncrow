"""``lc task`` against real Git repositories, each with a local bare ``origin``."""

from __future__ import annotations

import contextlib
import io
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import lemoncrow_client
import pytest
from lemoncrow_client.lite.index import LiteIndex
from lemoncrow_client.task import task_main

DEFINITION = """\
version = 1

[[checks]]
id = "src"
paths = ["src"]
run = "echo src >> \\"$RUNS\\"; test ! -e src/broken"
"""


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "gitconfig"
    config.write_text("[user]\n\tname = Tester\n\temail = tester@example.test\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("RUNS", str(tmp_path / "runs"))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def make_repo(path: Path, remotes: Path, files: dict[str, str] | None = None) -> Path:
    """``main`` with a definition and ``src/a.py``, pushed to a bare ``origin`` under ``remotes``."""

    remotes.mkdir(exist_ok=True)
    origin = remotes / f"{path.name}.git"
    git(remotes, "init", "-q", "--bare", origin.name)
    path.mkdir(parents=True)
    git(path, "init", "-q")
    for name, text in {".lemoncrow/environment.toml": DEFINITION, "src/a.py": "a = 1\n", **(files or {})}.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    git(path, "remote", "add", "origin", str(origin))
    git(path, "push", "-q", "-u", "origin", "main")
    return path


def commit(worktree: Path, name: str, text: str = "x\n") -> None:
    (worktree / name).parent.mkdir(parents=True, exist_ok=True)
    (worktree / name).write_text(text)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", f"add {name}")


def origin_main(repo: Path) -> str:
    return git(repo, "ls-remote", "origin", "refs/heads/main").split()[0]


def hook(repo: Path, body: str) -> None:
    path = repo / ".git" / "hooks" / "pre-push"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def overlay_base(worktree: Path) -> str | None:
    """The base index the worktree's Runtime Lite overlay stacks on; ``None`` without an index."""

    index = worktree / ".lemoncrow" / "workspace" / "runtime-lite-v3.sqlite3"
    if not index.is_file():
        return None
    with contextlib.closing(sqlite3.connect(index)) as raw:
        return dict(raw.execute("SELECT key, value FROM meta")).get("base_path")


def base_index(repo: Path) -> str:
    """Build the main checkout's index, which task worktrees stack their overlays on."""

    index = LiteIndex(repo)
    index.prepare()
    return str(index.db_path)


def runs(tmp_path: Path) -> int:
    path = tmp_path / "runs"
    return len(path.read_text().splitlines()) if path.exists() else 0


def lc_task(*arguments: str) -> tuple[int, str]:
    sink = io.StringIO()
    code = task_main(list(arguments), sink)
    return code, sink.getvalue()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = make_repo(tmp_path / "repo", tmp_path / "remotes")
    monkeypatch.chdir(repo)
    return repo


def start(repo: Path, monkeypatch: pytest.MonkeyPatch, task_id: str = "t1") -> Path:
    monkeypatch.chdir(repo)
    code, output = lc_task("start", task_id)
    assert code == 0, output
    worktree = repo / ".worktrees" / task_id
    monkeypatch.chdir(worktree)
    return worktree


def test_land_refuses_a_repository_without_checks(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git(repo, "rm", "-q", ".lemoncrow/environment.toml")
    git(repo, "commit", "-q", "-m", "no definition")
    git(repo, "push", "-q", "origin", "main")
    before = origin_main(repo)
    worktree = start(repo, monkeypatch)
    commit(worktree, "src/b.py")

    code, output = lc_task("land")

    assert code == 1 and "define the checks there before landing" in output and "not landed" in output
    assert origin_main(repo) == before


def test_start_branches_from_the_fetched_origin_main(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "remotes" / "repo.git"), str(other))
    commit(other, "src/b.py")
    git(other, "push", "-q", "origin", "main")  # origin/main in repo is now stale
    commit(repo, "local.txt")  # local main is ahead: not the base

    code, output = lc_task("start", "t1")
    assert code == 0, output
    worktree = repo / ".worktrees" / "t1"
    assert git(worktree, "symbolic-ref", "--short", "HEAD") == "agent/t1"
    assert git(worktree, "rev-parse", "HEAD") == origin_main(repo)
    assert git(repo, "status", "--porcelain") == ""  # the worktree is not untracked work in the main checkout

    assert lc_task("start", "t1")[0] == 1  # the folder and branch exist
    assert lc_task("start", ".hidden")[0] == 2
    assert lc_task("start", "t2", "--repos", "x")[0] == 2  # one repository: no --repos
    assert not (repo / ".worktrees" / "t2").exists()


def test_land_checks_pushes_main_and_finish_removes_the_landed_task(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = start(repo, monkeypatch)
    commit(worktree, "src/b.py")
    code, output = lc_task("land")
    assert code == 0, output
    assert "1 check(s) passed" in output and runs(tmp_path) == 1
    assert origin_main(repo) == git(worktree, "rev-parse", "HEAD")

    monkeypatch.chdir(repo)
    assert lc_task("list") == (0, f"agent/t1  landed  {repo.resolve() / '.worktrees' / 't1'}\n")
    monkeypatch.chdir(worktree)
    code, output = lc_task("finish", "t1")
    assert code == 1 and "outside" in output
    monkeypatch.chdir(repo)
    assert lc_task("finish", "t1") == (0, "lc task: removed agent/t1\n")
    assert not worktree.exists()
    assert git(repo, "branch", "--list", "agent/t1") == ""


def test_land_refuses_uncommitted_or_untracked_work(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = origin_main(repo)
    worktree = start(repo, monkeypatch)
    commit(worktree, "src/b.py")
    (worktree / "notes.txt").write_text("untracked\n")
    code, output = lc_task("land")
    assert code == 1 and "commit your work first" in output
    assert origin_main(repo) == before


def test_land_stops_on_a_failing_check_and_pushes_nothing(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = origin_main(repo)
    worktree = start(repo, monkeypatch)
    commit(worktree, "src/broken")
    code, output = lc_task("land")
    assert code == 1 and "FAILED: src" in output and "not landed" in output
    assert runs(tmp_path) == 1  # no retry
    assert origin_main(repo) == before


def test_land_retries_only_when_main_moved(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "remotes" / "repo.git"), str(other))
    log, mark = tmp_path / "hook.log", tmp_path / "moved"
    # The first push lands another commit on main just before ours reaches the remote.
    hook(
        repo,
        f'echo push >> "{log}"\n'
        f'[ -e "{mark}" ] && exit 0\ntouch "{mark}"\nunset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE\n'
        f'cd "{other}" && echo moved > moved.txt && git add moved.txt && git commit -qm moved && git push -q origin main',
    )
    worktree = start(repo, monkeypatch)
    commit(worktree, "src/b.py")
    code, output = lc_task("land")
    assert code == 0, output
    assert "main moved, rebasing again" in output
    assert log.read_text() == "push\npush\n" and runs(tmp_path) == 2  # checked again after the rebase
    assert git(worktree, "log", "--format=%s", "-2", origin_main(repo)) == "add src/b.py\nmoved"

    # Any other push failure stops at once.
    hook(repo, f'echo push >> "{log}"\nexit 1')
    log.unlink()
    worktree = start(repo, monkeypatch, "t2")
    commit(worktree, "src/c.py")
    code, output = lc_task("land")
    assert code == 1 and "push failed; read the output above" in output
    assert log.read_text() == "push\n"


def test_a_pre_push_hook_that_runs_lc_verify_runs_the_checks_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Path(lemoncrow_client.__file__).resolve().parents[1]
    hook(repo, f'PYTHONPATH="{client}" exec "{sys.executable}" -m lemoncrow_client verify --pre-push "$@"')
    before = origin_main(repo)
    worktree = start(repo, monkeypatch)
    commit(worktree, "src/broken")
    code, output = lc_task("land")
    assert code == 1 and "the pre-push hook runs the checks" in output and "push failed" in output
    assert runs(tmp_path) == 1 and origin_main(repo) == before

    git(worktree, "rm", "-q", "src/broken")
    commit(worktree, "src/b.py")
    code, output = lc_task("land")
    assert code == 0, output
    assert runs(tmp_path) == 2  # once, by the hook
    assert origin_main(repo) == git(worktree, "rev-parse", "HEAD")


def test_start_warms_the_index_overlay_and_finish_removes_it_with_the_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert lc_task("start", "cold")[0] == 0  # no index in the main checkout: nothing to stack on, nothing built
    assert overlay_base(repo / ".worktrees" / "cold") is None

    base = base_index(repo)
    generation = LiteIndex(repo).generation()
    code, output = lc_task("start", "t1")
    assert code == 0 and f"lc task: index overlay ready on the index of {repo.resolve()}\n" in output, output
    worktree = repo / ".worktrees" / "t1"
    assert overlay_base(worktree) == base

    # The agent's edits answer from the overlay; the base is not rebuilt.
    commit(worktree, "src/b.py", "def task_only_feature():\n    return 1\n")
    child = LiteIndex(worktree)
    assert [row["path"] for row in child.exact_symbols("task_only_feature")] == ["src/b.py"]
    assert overlay_base(worktree) == base and LiteIndex(repo).generation() == generation
    assert not LiteIndex(repo).exact_symbols("task_only_feature")

    monkeypatch.chdir(worktree)
    code, output = lc_task("land")  # the overlay is ignored: not work the gate refuses
    assert code == 0, output
    monkeypatch.chdir(repo)
    assert lc_task("finish", "t1") == (0, "lc task: removed agent/t1\n")
    assert not worktree.exists() and git(repo, "status", "--porcelain") == ""
    assert LiteIndex(repo).generation() == generation


def test_a_worktree_removed_another_way_leaves_no_index_behind(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_index(repo)
    for task_id in ("t1", "t2", "t3"):
        assert lc_task("start", task_id)[0] == 0  # nothing committed: landed
        shutil.rmtree(repo / ".worktrees" / task_id)

    # An index cannot bring the removed worktree back...
    gone = repo / ".worktrees" / "t1"
    with pytest.raises(RuntimeError):
        LiteIndex(gone).prepare()
    assert not gone.exists()
    # ...but a writer from before could: what it left is evicted with the task.
    store = gone / ".lemoncrow" / "workspace"
    store.mkdir(parents=True)
    (store / "runtime-lite-v3.sqlite3").write_bytes(b"")
    (store / "session_state.json").write_text("{}")
    code, output = lc_task("finish", "t1")
    assert code == 0, output
    assert output == f"lc task: removed a leftover index in {gone}\nlc task: removed agent/t1\n"
    assert not gone.exists() and not git(repo, "branch", "--list", "agent/t1")

    # Deleted by hand and nothing left: Git forgets the worktree, then the branch goes.
    assert lc_task("finish", "t2") == (0, "lc task: removed agent/t2\n")
    assert str(repo / ".worktrees" / "t2") not in git(repo, "worktree", "list")

    # Anything else where the worktree was is not the task's to remove.
    (repo / ".worktrees" / "t3").mkdir()
    (repo / ".worktrees" / "t3" / "notes.txt").write_text("keep me\n")
    code, output = lc_task("finish", "t3")
    assert code == 1 and "not a Git worktree" in output and "Nothing removed" in output
    assert (repo / ".worktrees" / "t3" / "notes.txt").exists() and git(repo, "branch", "--list", "agent/t3")

    # A leftover index does not block starting the same task again.
    store.mkdir(parents=True)
    (store / "runtime-lite-v3.sqlite3").write_bytes(b"")
    code, output = lc_task("start", "t1")
    assert code == 0 and "index overlay ready" in output, output
    assert git(gone, "symbolic-ref", "--short", "HEAD") == "agent/t1" and overlay_base(gone)


def test_finish_refuses_unlanded_or_untracked_work_and_removes_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = base_index(repo)
    worktree = start(repo, monkeypatch)
    commit(worktree, "src/b.py")
    monkeypatch.chdir(repo)
    code, output = lc_task("finish", "t1")
    assert code == 1 and "not on origin/main" in output and "Nothing removed" in output
    assert lc_task("list") == (0, f"agent/t1  1 commit(s) not landed  {repo.resolve() / '.worktrees' / 't1'}\n")

    clean = start(repo, monkeypatch, "t2")  # no commits: landed, but it holds an untracked file
    (clean / "notes.txt").write_text("keep me\n")
    monkeypatch.chdir(repo)
    code, output = lc_task("finish", "t2")
    assert code == 1 and "untracked" in output
    for task_id, top, path in (("t1", worktree, worktree / "src" / "b.py"), ("t2", clean, clean / "notes.txt")):
        assert path.exists() and git(repo, "branch", "--list", f"agent/{task_id}")
        assert overlay_base(top) == base  # and its index overlay, which still answers
    assert "src/a.py" in {item["path"] for item in LiteIndex(worktree).repo_map(limit=50)["items"]}


def test_multi_repo_task_lands_in_dependency_order_only_when_every_repository_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, remotes, pushes = tmp_path / "folder", tmp_path / "remotes", tmp_path / "pushes"
    # app depends on zcore, so zcore lands first although it sorts last.
    app = make_repo(
        folder / "app", remotes, {"pyproject.toml": '[project]\nname = "app"\ndependencies = ["zcore-lib"]\n'}
    )
    zcore = make_repo(folder / "zcore", remotes, {"pyproject.toml": '[project]\nname = "zcore-lib"\n'})
    for repo in (app, zcore):
        hook(repo, f'echo {repo.name} >> "{pushes}"')
    before = {repo: origin_main(repo) for repo in (app, zcore)}
    bases = {repo: base_index(repo) for repo in (app, zcore)}
    monkeypatch.chdir(folder)
    assert lc_task("start", "t1")[0] == 2  # a folder needs --repos
    assert lc_task("start", "t1", "--repos", "app,nope")[0] == 2
    code, output = lc_task("start", "t1", "--repos", "app,zcore")
    assert code == 0, output
    assert "lc task: app: index overlay ready" in output and "lc task: zcore: index overlay ready" in output
    for repo in (app, zcore):
        assert overlay_base(repo / ".worktrees" / "t1") == bases[repo]

    commit(zcore / ".worktrees" / "t1", "src/z.py")
    commit(app / ".worktrees" / "t1", "src/broken")  # fails app's check
    code, output = lc_task("land", "t1")
    assert code == 1 and "app: checks failed" in output and "Nothing pushed" in output
    assert not pushes.exists() and {repo: origin_main(repo) for repo in (app, zcore)} == before
    code, output = lc_task("list")
    assert code == 0 and "zcore: agent/t1  1 commit(s) not landed" in output

    monkeypatch.chdir(zcore / ".worktrees" / "t1")
    code, output = lc_task("land")  # one repository alone would break all-or-nothing
    assert code == 1 and "lc task land t1" in output and not pushes.exists()

    git(app / ".worktrees" / "t1", "rm", "-q", "src/broken")
    commit(app / ".worktrees" / "t1", "src/b.py")
    monkeypatch.chdir(folder)
    code, output = lc_task("finish", "t1")
    assert code == 1 and "Nothing removed" in output
    code, output = lc_task("land", "t1")
    assert code == 0, output
    assert pushes.read_text() == "zcore\napp\n"
    for repo in (app, zcore):
        assert origin_main(repo) == git(repo / ".worktrees" / "t1", "rev-parse", "HEAD") != before[repo]

    assert lc_task("finish", "t1") == (0, "lc task: app: removed agent/t1\nlc task: zcore: removed agent/t1\n")
    assert not (app / ".worktrees" / "t1").exists() and not (zcore / ".worktrees" / "t1").exists()
