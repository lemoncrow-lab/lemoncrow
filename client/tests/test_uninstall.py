"""``lc uninstall`` removes what the installer added, lists it first, and asks before anything of the user's.

Everything runs in a sandbox HOME laid out the way the installer lays it out (commands linked into ``bin``, the Review
client and engine, the installer's scripts, shell startup blocks in two files), with a real git repository as the working
directory. Nothing here touches the real HOME, the network, or ``npm``.
"""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest
from lemoncrow_client import cli, uninstall

BLOCK = "# >>> lemoncrow path setup >>>\n. {home}/.lemoncrow/shell-env.sh\n# <<< lemoncrow path setup <<<\n"


def tree(root: Path) -> dict[str, str]:
    """Every path under *root* with what it is, to compare before and after."""

    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        found[relative] = (
            f"link:{os.readlink(path)}" if path.is_symlink() else "dir" if path.is_dir() else path.read_text()
        )
    return found


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    user = tmp_path / "home"
    state = user / ".lemoncrow"
    for name in ("LEMONCROW_HOME", "LEMONCROW_BIN_DIR", "ZDOTDIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(user))
    # The installer's layout.
    (state / "review" / "client" / "bin").mkdir(parents=True)
    (state / "review" / "client" / "bin" / "lc").write_text("#!/bin/sh\n")
    (state / "review" / "engine" / "lib").mkdir(parents=True)
    (state / "review" / "engine" / "lib" / "big.bin").write_bytes(b"x" * 4096)
    (state / "bin").mkdir()
    for name in ("lc", "lcr", "lemoncrow-review-client"):
        (state / "bin" / name).symlink_to(state / "review" / "client" / "bin" / "lc")
    (state / "install" / "scripts").mkdir(parents=True)
    (state / "install" / "frontend").mkdir()
    (state / "install" / "frontend" / "index.html").write_text("<html>")
    stop_log = tmp_path / "server-stop.log"
    script = state / "install" / "scripts" / "local_server.sh"
    script.write_text(f'#!/bin/sh\necho "$@" >> {stop_log}\n')
    (state / "shell-env.sh").write_text("export PATH\n")
    (state / "token").write_text("t\n")
    (state / "server" / "data" / "reviews").mkdir(parents=True)
    (state / "server" / "data" / "reviews" / "r1.db").write_text("your marks")
    (state / "server" / "server.log").write_text("log\n")
    (state / "server" / "target-product").write_text("review\n")
    (state / "review-local-ai" / "abc").mkdir(parents=True)
    (state / "review-local-ai" / "abc" / "k.state").write_text("carried")
    (state / "plugin_settings.json").write_text('{"llm.openai_api_key": "sk-secret"}')
    (user / ".config" / "lemoncrow").mkdir(parents=True)
    (user / ".config" / "lemoncrow" / "telemetry_id").write_text("id\n")
    block = BLOCK.format(home=user)
    (user / ".bashrc").write_text(f"export KEEP=1\n\n{block}")
    (user / ".profile").write_text(f"# my profile\n\n{block}")
    (user / ".zshrc").write_text("# untouched zsh\n")
    # A foreign program that happens to be called lc must survive.
    foreign = tmp_path / "other-tools" / "lc"
    foreign.parent.mkdir()
    foreign.write_text("#!/bin/sh\n")
    # A repository the person is working in, with the files a review and the hook installer leave.
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "review.yaml").write_text("review:\n  strictness: 3\n")
    (repo / ".lemoncrow-local-fs-proof").write_text("proof")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text("# mine\n/.lemoncrow-local-fs-proof\n/build\n")
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\n# lemoncrow-review-hook v3 (pre-push)\nexit 0\n")
    (state / "installed-hooks.txt").write_text(f"{hook}\n{tmp_path}/gone/.git/hooks/pre-push\n")
    other = tmp_path / "elsewhere" / ".git" / "hooks" / "pre-push"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n# lemoncrow-review-hook v3 (pre-push)\n")
    with (state / "installed-hooks.txt").open("a") as record:
        record.write(f"{other}\n")
    # A fake npm that would record being run: the uninstall must never run it.
    npm = tmp_path / "bin-on-path" / "npm"
    npm.parent.mkdir()
    npm.write_text(f'#!/bin/sh\necho npm "$@" >> {tmp_path}/npm-was-run\n')
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{npm.parent}{os.pathsep}{os.environ['PATH']}")
    return {
        "user": user,
        "state": state,
        "repo": repo,
        "hook": hook,
        "other_hook": other,
        "foreign": foreign,
        "stop_log": stop_log,
        "tmp": tmp_path,
    }


def run(
    args: list[str], box: dict[str, Path], *, answers: list[bool] | None = None, tty: bool = False
) -> tuple[int, str]:
    sink = io.StringIO()
    asked: list[str] = []
    queue = list(answers or [])

    def ask(prompt: str) -> bool:
        asked.append(prompt.strip())
        return queue.pop(0) if queue else False

    code = uninstall.main(args, sink, cwd=box["repo"], ask=ask, interactive=tty)
    return code, sink.getvalue() + ("\nQUESTIONS: " + " | ".join(asked) if asked else "")


def test_a_dry_run_lists_everything_changes_nothing_and_says_so(box: dict[str, Path]) -> None:
    before = tree(box["tmp"])
    code, out = run(["review", "--dry-run"], box)
    assert code == 0 and tree(box["tmp"]) == before
    assert "dry run: nothing will be changed" in out and "Dry run: nothing was changed." in out
    for wanted in (
        "Stop the background Review server",
        "The commands lc, lcr, lemoncrow-review-client",
        "The Review client and engine",
        "Carried review results (a cache)",
        "The installer's scripts and the Reader",
        "The lines the installer added to ~/.bashrc",
        "The lines the installer added to ~/.profile",
        "Your settings",
        "includes any API key you saved with lc settings",
        "The reviews you opened in the Reader",
        "A git hook written by lc review hook install",
        "The .lemoncrow folder here",
    ):
        assert wanted in out, f"{wanted!r} missing from:\n{out}"
    assert "~/.zshrc" not in out  # a startup file with no block is not listed
    assert "Removed" not in out  # the audit: a dry run used to print "Removed ..." lines
    assert not box["stop_log"].exists() and not (box["tmp"] / "npm-was-run").exists()


def test_yes_removes_what_the_installer_added_and_keeps_your_data(box: dict[str, Path]) -> None:
    state, user = box["state"], box["user"]
    code, out = run(["review", "--yes"], box)
    assert code == 0, out
    for gone in ("review", "install", "shell-env.sh", "token", "review-local-ai", "bin"):
        assert not (state / gone).exists(), gone
    assert sorted(child.name for child in (state / "server").iterdir()) == ["data"]  # log and process files went
    assert (state / "plugin_settings.json").read_text() == '{"llm.openai_api_key": "sk-secret"}'  # yours, kept
    assert (state / "server" / "data" / "reviews" / "r1.db").exists()
    assert (user / ".config" / "lemoncrow" / "telemetry_id").exists()
    # The shell files lose the block and nothing else.
    assert (user / ".bashrc").read_text() == "export KEEP=1\n"
    assert (user / ".profile").read_text() == "# my profile\n"
    assert (user / ".zshrc").read_text() == "# untouched zsh\n"
    # The git hooks it wrote go with `lc` (a hook for a command that is gone prints "lc was not found" on every push),
    # found through its own list, in another repository too. A repository's own files are not asked about under --yes.
    assert not box["hook"].exists() and not box["other_hook"].exists() and not (state / "installed-hooks.txt").exists()
    assert (box["repo"] / ".lemoncrow").exists() and (box["repo"] / ".lemoncrow-local-fs-proof").exists()
    assert (
        "A git hook written by lc review hook install" in out.split("Keeps (yours")[0]
    )  # under "Removes", not "Keeps"
    assert "Keeps (yours, or in your repositories;" in out and "Your settings" in out.split("Keeps (yours")[1]
    assert "git hook" not in out.split("Keeps (yours")[1].split("Does not touch")[0]
    assert "Kept, as listed above. To remove your data too: lc uninstall all --purge" in out
    assert box["stop_log"].read_text().strip() == "stop"  # the server was stopped with its own helper
    assert not (box["tmp"] / "npm-was-run").exists()
    assert box["foreign"].exists()


def test_purge_removes_your_data_and_the_hooks_it_wrote_but_not_a_repositorys_folder(box: dict[str, Path]) -> None:
    state, user = box["state"], box["user"]
    code, out = run(["all", "--purge"], box)
    assert code == 0, out
    assert not state.exists()  # nothing left, so the folder itself went
    assert not (user / ".config" / "lemoncrow").exists()
    assert not box["hook"].exists() and not box["other_hook"].exists()  # as under --yes: found through its own list
    assert (box["repo"] / ".lemoncrow" / "review.yaml").exists()  # a repository's folder is never purged blind
    assert (box["repo"] / ".lemoncrow-local-fs-proof").exists()
    assert "The .lemoncrow folder here" in out.split("Keeps (yours")[1]  # listed under what it keeps, not under --purge
    assert "With --purge, also removes (yours):" in out and "Your settings" in out.split("With --purge")[1]


def test_questions_are_asked_one_group_at_a_time_and_the_answers_decide(box: dict[str, Path]) -> None:
    state = box["state"]
    code, out = run(["review"], box, answers=[True, False, True], tty=True)
    assert code == 0, out
    questions = out.split("QUESTIONS: ")[1].split(" | ")
    assert questions == [
        "Remove the items under 'Removes what the installer added'?",
        "Also delete your settings, saved reviews and sign-in?",
        "Also remove the LemonCrow files in this repository?",
    ]
    assert (state / "plugin_settings.json").exists()  # said no
    assert (
        not box["hook"].exists() and not box["other_hook"].exists()
    )  # part of the first yes: they call the lc it removes
    assert not (box["repo"] / ".lemoncrow").exists() and not (box["repo"] / ".lemoncrow-local-fs-proof").exists()
    exclude = (box["repo"] / ".git" / "info" / "exclude").read_text()
    assert exclude == "# mine\n/build\n"  # the line it added, not the person's own lines
    assert git(box["repo"], "status", "--porcelain") == "?? app.py"


def test_a_committed_review_yaml_is_the_teams_file_and_only_the_index_is_offered(box: dict[str, Path]) -> None:
    repo = box["repo"]
    (repo / ".lemoncrow" / "workspace").mkdir()
    (repo / ".lemoncrow" / "workspace" / "index.db").write_text("index")
    git(repo, "add", ".lemoncrow/review.yaml")
    code, out = run(["review", "--dry-run"], box)
    assert code == 0 and "The .lemoncrow folder here" not in out
    assert "The workspace index in this repository's .lemoncrow folder (the committed files there stay)" in out
    assert run(["review", "--yes"], box)[0] == 0
    assert (repo / ".lemoncrow" / "review.yaml").exists() and (
        repo / ".lemoncrow" / "workspace"
    ).exists()  # --yes keeps it
    code, out = run(
        ["review"], box, answers=[False, True], tty=True
    )  # nothing installed is left to ask about; your data: no; the repo question: yes
    assert (repo / ".lemoncrow" / "review.yaml").read_text() == "review:\n  strictness: 3\n"
    assert not (repo / ".lemoncrow" / "workspace").exists()


def test_only_a_hook_with_our_marker_is_removed_and_a_dry_run_lists_every_one(box: dict[str, Path]) -> None:
    """Hooks are found by the marker line: a hook someone replaced, or one that is gone, is never touched."""

    replaced = box["tmp"] / "mine" / ".git" / "hooks" / "pre-push"
    replaced.parent.mkdir(parents=True)
    replaced.write_text("#!/bin/sh\necho my own check\n")
    precommit = box["tmp"] / "second" / ".git" / "hooks" / "pre-commit"
    precommit.parent.mkdir(parents=True)
    precommit.write_text("#!/bin/sh\n# lemoncrow-review-hook v3 (pre-commit)\nexit 0\n")
    with (box["state"] / "installed-hooks.txt").open("a") as record:
        record.write(f"{replaced}\n{precommit}\n")
    before = tree(box["tmp"])
    code, out = run(["review", "--dry-run"], box)
    assert code == 0 and tree(box["tmp"]) == before
    removes = out.split("Would ask")[0]
    for listed in (box["hook"], box["other_hook"], precommit):
        assert str(listed) in removes, listed
    assert str(replaced) not in out
    assert run(["review", "--yes"], box)[0] == 0
    assert not box["hook"].exists() and not box["other_hook"].exists() and not precommit.exists()
    assert replaced.read_text() == "#!/bin/sh\necho my own check\n"


def test_a_hook_in_the_repository_you_are_in_is_found_without_being_listed(box: dict[str, Path]) -> None:
    (box["state"] / "installed-hooks.txt").unlink()
    assert run(["review", "--yes"], box)[0] == 0
    assert not box["hook"].exists()  # the cwd repository's own hook
    assert box["other_hook"].exists()  # nothing told it about this one, and it is not where the person stands


def test_saying_no_to_the_first_question_removes_nothing(box: dict[str, Path]) -> None:
    before = tree(box["tmp"])
    code, out = run(["review"], box, answers=[False], tty=True)
    assert code == 0 and "Nothing was removed." in out and tree(box["tmp"]) == before


def test_without_a_terminal_or_yes_it_refuses_instead_of_guessing(box: dict[str, Path]) -> None:
    before = tree(box["tmp"])
    code, out = run(["review"], box, tty=False)
    assert code == 2 and "Not a terminal, so nothing was removed" in out and tree(box["tmp"]) == before


def test_the_dry_run_is_the_plan_the_real_run_follows(box: dict[str, Path]) -> None:
    """Whatever the dry run lists as installed by LemonCrow is exactly what disappears (and nothing else)."""

    plan = uninstall.build_plan("review", env=dict(os.environ), cwd=box["repo"])
    listed = {str(step.path) for step in plan if step.group == "installed" and step.action in {"remove", "unlink"}}
    before = set(tree(box["tmp"]))
    assert run(["review", "--yes"], box)[0] == 0
    gone = {str(box["tmp"] / relative) for relative in before - set(tree(box["tmp"]))}
    removed_roots = {path for path in gone if not any(path.startswith(root + os.sep) for root in gone if root != path)}
    # Every listed path is gone, and every top-level removal is a listed path or a link/rc/server file the plan names.
    assert listed <= gone
    allowed = listed | {str(box["state"] / "bin" / name) for name in ("lc", "lcr", "lemoncrow-review-client")}
    allowed |= {str(box["state"] / "bin")}
    allowed |= {str(box["state"] / "server" / name) for name in ("server.log", "target-product")}
    assert removed_roots <= allowed, removed_roots - allowed


def test_a_foreign_command_with_the_same_name_is_not_ours_to_remove(box: dict[str, Path]) -> None:
    bin_dir = box["tmp"] / "usr-local-bin"
    bin_dir.mkdir()
    (bin_dir / "lc").symlink_to(box["foreign"])
    (bin_dir / "lcr").symlink_to(box["state"] / "review" / "client" / "bin" / "lc")
    os.environ["LEMONCROW_BIN_DIR"] = str(bin_dir)
    try:
        assert run(["review", "--yes"], box)[0] == 0
    finally:
        del os.environ["LEMONCROW_BIN_DIR"]
    assert (bin_dir / "lc").is_symlink() and not (bin_dir / "lcr").exists()
    assert bin_dir.exists()  # it never removes a bin directory it did not make


def test_review_alone_keeps_runtime_working(box: dict[str, Path]) -> None:
    runtime = box["state"] / "runtime" / "client" / "bin"
    runtime.mkdir(parents=True)
    (runtime / "lemoncrow-client").write_text("#!/bin/sh\n")
    wrapper = box["state"] / "install" / "scripts" / "uninstall_product.sh"
    called = box["tmp"] / "wrapper.log"
    wrapper.write_text(f'#!/bin/sh\necho "$@" >> {called}\n')
    _, out = run(["review", "--dry-run"], box)
    assert "Review: its client and engine, and its commands (Runtime keeps working)" in out
    for kept in ("shell setup file", "installer's scripts", "The lines the installer added", "Your settings"):
        assert kept not in out, kept  # Runtime still needs them
    assert run(["review", "--yes"], box)[0] == 0
    assert called.read_text().strip() == "review"  # the product script does the hand-off of the lc command
    assert (box["state"] / "install" / "scripts" / "uninstall_product.sh").exists() and not (
        box["state"] / "review-local-ai"
    ).exists()
    assert (
        "export KEEP=1" in (box["user"] / ".bashrc").read_text()
        and "lemoncrow path setup" in (box["user"] / ".bashrc").read_text()
    )


def test_all_with_runtime_removes_it_first_through_its_own_script(box: dict[str, Path]) -> None:
    (box["state"] / "runtime" / "client" / "bin").mkdir(parents=True)
    wrapper = box["state"] / "install" / "scripts" / "uninstall_product.sh"
    called = box["tmp"] / "wrapper.log"
    wrapper.write_text(f'#!/bin/sh\necho "$@" >> {called}\n')
    _, out = run(["all", "--dry-run"], box)
    assert "Runtime: its coding-agent entries (claude, codex, opencode, pi) and its client" in out
    assert not called.exists()
    assert run(["all", "--yes"], box)[0] == 0
    assert called.read_text().strip() == "runtime" and not (box["state"] / "install").exists()


def test_nothing_installed_says_so_and_a_bad_command_line_is_usage(box: dict[str, Path], tmp_path: Path) -> None:
    empty = tmp_path / "empty-home"
    empty.mkdir()
    sink = io.StringIO()
    code = uninstall.main(
        ["review", "--yes"], sink, env={"HOME": str(empty), "PATH": os.environ["PATH"]}, cwd=empty, interactive=False
    )
    assert code == 0 and "Nothing to remove" in sink.getvalue()
    for bad in ([], ["review", "--force"], ["everything"]):
        sink = io.StringIO()
        assert uninstall.main(bad, sink) == 2 and "usage: lc uninstall review|runtime|all" in sink.getvalue()


def test_the_client_command_goes_through_it_and_runtime_still_uses_the_script(
    box: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(box["repo"])
    sink = io.StringIO()
    assert cli.main(["uninstall", "review", "--dry-run"], sink) == 0
    assert "Dry run: nothing was changed." in sink.getvalue()
    script = box["state"] / "install" / "scripts" / "uninstall_product.sh"
    script.write_text("#!/bin/sh\nprintf 'script:%s\\n' \"$*\"\n")
    sink = io.StringIO()
    assert cli.main(["uninstall", "runtime", "--dry-run"], sink) == 0
    assert sink.getvalue() == "script:runtime --dry-run\n"


def test_the_source_checkout_script_no_longer_uninstalls_global_npm_packages() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
    assert "npm uninstall" not in script
