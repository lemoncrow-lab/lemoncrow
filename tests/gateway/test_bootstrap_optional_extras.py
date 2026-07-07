"""
test_bootstrap_optional_extras.py -- the interactive agent/skill checklist
step in the bootstrap flow.

Phases 1-3 (merged) made the default install bare-minimum (agent `code` only,
zero skills) and exposed a tested CLI (`atelier agent|skill list/install/
remove`) plus an in-chat `/atelier` skill for opting extras in after the fact.

The interactive checklist lives entirely inside host_wizard() (scripts/lib/
common.sh), called from main() in both scripts/bundle.sh and scripts/local.sh,
in this order:
  1. "Which agents should Atelier configure?"  (which HOSTS -- claude/codex/
     opencode -- confusingly named "agents" here, this is pre-existing wizard
     copy, not our agent-role concept)
  2. "Optional agent roles to install" -- a checklist containing `code` as
     a locked selected row plus every other role (explore/execute/plan/
     research/review/solve/auto/bare/general), with standard roles pre-checked
     and auto/bare available but deselected by default.
  3. "Optional skills" -- a checklist of the 6 public skills (the /atelier
     management skill itself is excluded -- it ships by default), ALL
     pre-UNchecked (opt in). Copy reminds the user skills are also
     installable later via /atelier.
  4. "Apply configs globally or just here?" (scope: global vs workspace)

Both checklists read role/skill names + a lightweight chars/4 cost estimate
directly off integrations/agents/*.md and integrations/skills/*/SKILL.md
frontmatter rather than shelling to the `atelier` CLI, since host_wizard()
runs before the CLI (and its tiktoken dependency) are installed.

Covers:
  * make dev no longer hardcodes --non-interactive (Bug 1): the TTY
    detection already in common.sh decides whether to prompt.
  * host_wizard() is independently gated by ATELIER_NON_INTERACTIVE /
    ATELIER_NO_HOSTS / has_interactive_input, same as before this feature.
  * The agent-roles block sits textually between the host-selection question
    and the scope question; `code` is excluded from the togglable set and
    shown as an info line instead; the skills block sits between the
    agent-roles block and the scope question and mentions /atelier.
  * The real arrow-key checkbox menu and the dumb-terminal fallback both
    default to "select standard optional roles" for agents and "select nothing"
    for skills, and both correctly thread --roles / --include-skills into
    HOST_EXTRA_ARGS without the trailing --claude-project append (added
    unconditionally later in host_wizard() when Claude + global scope are
    chosen) clobbering them.
  * install_hosts.sh forwards --roles to claude/codex/opencode and
    --include-skills to claude/codex only (opencode has no skills concept),
    and never breaks copilot/antigravity even when --all is combined with
    both flags.
"""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import time
from pathlib import Path

ATELIER_ROOT = Path(__file__).parent.parent.parent
SCRIPTS = ATELIER_ROOT / "scripts"
COMMON_SH = SCRIPTS / "lib" / "common.sh"
MAKEFILE = ATELIER_ROOT / "Makefile"
VENV_BIN = ATELIER_ROOT / ".venv" / "bin"


def test_host_extra_args_appended_to_host_install_args() -> None:
    content = COMMON_SH.read_text()
    assert "HOST_EXTRA_ARGS+=(--roles" in content
    assert "HOST_EXTRA_ARGS+=(--include-skills" in content
    assert 'host_install_args+=("${HOST_EXTRA_ARGS[@]}")' in content


def test_makefile_dev_target_does_not_force_non_interactive() -> None:
    content = MAKEFILE.read_text()
    dev_pos = content.index("\ndev:")
    next_target_pos = content.index("\nbuild:", dev_pos)
    dev_recipe = content[dev_pos:next_target_pos]
    assert "scripts/local.sh" in dev_recipe
    assert "--non-interactive" not in dev_recipe, (
        "make dev must not hardcode --non-interactive -- the TTY detection in "
        "common.sh (has_interactive_input/supports_interactive_selector) already "
        "decides whether to prompt"
    )


def test_agent_roles_and_skills_blocks_ordered_inside_host_wizard() -> None:
    """Static ordering check: host-select -> agent-roles -> skills -> scope,
    all inside one host_wizard() call -- exactly what the user asked for."""
    content = COMMON_SH.read_text()
    wizard_pos = content.index("host_wizard() {")
    next_fn_pos = content.index("\nhost_scope_is_workspace() {", wizard_pos)
    block = content[wizard_pos:next_fn_pos]
    which_agents_pos = block.index('"Which agents should Atelier configure?"')
    roles_pos = block.index("Optional agent roles to install")
    skills_pos = block.index("Optional skills")
    scope_pos = block.index('"Apply configs globally or just here?"')
    assert (
        which_agents_pos < roles_pos < skills_pos < scope_pos
    ), "expected order inside host_wizard(): which-hosts -> agent-roles -> skills -> scope"


def test_code_shown_as_locked_always_installed_role() -> None:
    content = COMMON_SH.read_text()
    wizard_pos = content.index("host_wizard() {")
    scope_pos = content.index('"Apply configs globally or just here?"', wizard_pos)
    block = content[wizard_pos:scope_pos]
    assert "LOCK_ICON" in content
    assert "always installed" in block
    assert (
        "code) SELECTED_ITEMS[$_rw_i]=1; LOCKED_ITEMS[$_rw_i]=1 ;;" in block
    ), "code must be rendered in the role list as selected and locked"


def test_skills_prompt_excludes_atelier_and_mentions_installing_later() -> None:
    content = COMMON_SH.read_text()
    wizard_pos = content.index("host_wizard() {")
    scope_pos = content.index('"Apply configs globally or just here?"', wizard_pos)
    block = content[wizard_pos:scope_pos]
    assert '"atelier"' in block, "the /atelier management skill (ships by default) must be excluded"
    assert (
        "/atelier" in block and "install" in block.lower()
    ), "the skills prompt copy must reference installing later via /atelier"


def test_claude_project_append_does_not_clobber_earlier_extra_args() -> None:
    """Regression: the trailing --claude-project append at the end of
    host_wizard() must use += (append), not = (overwrite) -- an overwrite
    here silently wipes out --roles/--include-skills set earlier in the same
    function whenever Claude + global scope are chosen."""
    content = COMMON_SH.read_text()
    assert 'HOST_EXTRA_ARGS+=(--claude-project "$(pwd)")' in content
    assert 'HOST_EXTRA_ARGS=(--claude-project "$(pwd)")' not in content


def _expect(master_fd: int, buf: bytearray, marker: str, timeout: float) -> bytearray:
    deadline = time.time() + timeout
    while marker not in buf.decode(errors="replace"):
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {marker!r}; got so far:\n{buf.decode(errors='replace')}")
        ready, _, _ = select.select([master_fd], [], [], remaining)
        if ready:
            chunk = os.read(master_fd, 4096)
            if not chunk:
                break
            buf.extend(chunk)
    return buf


def _drain(master_fd: int, buf: bytearray, proc: subprocess.Popen[bytes], timeout: float) -> bytearray:
    deadline = time.time() + timeout
    while proc.poll() is None and time.time() < deadline:
        ready, _, _ = select.select([master_fd], [], [], 0.5)
        if ready:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
    return buf


def _role_names_from_disk() -> list[str]:
    """Non-default role names straight off integrations/agents/*.md frontmatter --
    what host_wizard() reads (it runs before the CLI/venv exist in a real
    bootstrap, so it can't shell out to `atelier agent list`)."""
    names = []
    agents_dir = ATELIER_ROOT / "integrations" / "agents"
    for path in sorted(agents_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = re.search(r"^mode:\s*(\S+)", text, re.MULTILINE)
        if m and m.group(1) != "code":
            names.append(m.group(1))
    return names


def _skill_names_from_disk() -> list[str]:
    """Public, non-hidden, non-default skill names straight off
    integrations/skills/*/SKILL.md -- mirrors host_wizard()'s own exclusion
    set (HIDDEN_SKILLS dev-only names, plus `atelier` itself)."""
    hidden = {"analyze-failures", "context", "evals", "rescue", "savings", "status", "record", "atelier"}
    names = []
    skills_dir = ATELIER_ROOT / "integrations" / "skills"
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        name = path.parent.name
        if name not in hidden:
            names.append(name)
    return names


def test_non_interactive_and_no_tty_leave_host_extra_args_empty() -> None:
    for extra_env in ({"ATELIER_NON_INTERACTIVE": "1"}, {}):
        body = (
            "HOST_EXTRA_ARGS=()\n"
            "HOST_FLAGS=(--claude)\n"
            "HOST_SCOPE_ARGS=()\n"
            "host_wizard\n"
            'printf "COUNT=%s\\n" "${#HOST_EXTRA_ARGS[@]}"\n'
        )
        script = f'set -euo pipefail\nsource "{COMMON_SH}"\n{body}\n'
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
            "ATELIER_INSTALL_DIR": str(ATELIER_ROOT),
            **extra_env,
        }
        result = subprocess.run(
            ["bash", "-c", script], cwd=ATELIER_ROOT, capture_output=True, text=True, timeout=20, env=env
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        # Pre-set HOST_FLAGS/HOST_SCOPE_ARGS also short-circuit host_wizard()
        # entirely (contains_any_host_flag / HOST_SCOPE_ARGS checks) -- either
        # way, no prompt should fire and HOST_EXTRA_ARGS must stay untouched.
        assert "COUNT=0" in result.stdout, result.stdout


def _run_host_wizard(*, answers: list[bytes], markers: list[str], tmp_path: Path, term: str) -> Path:
    """Drive host_wizard() end to end: for each (marker, answer) pair, wait for
    the marker then send the answer. Captures HOST_FLAGS/HOST_EXTRA_ARGS after.
    """
    result_file = tmp_path / "result.txt"
    script_body = (
        "set -euo pipefail\n"
        f'source "{COMMON_SH}"\n'
        "HOST_FLAGS=()\n"
        "HOST_SCOPE_ARGS=()\n"
        "HOST_EXTRA_ARGS=()\n"
        "host_wizard\n"
        f': > "{result_file}"\n'
        'for a in "${HOST_EXTRA_ARGS[@]+"${HOST_EXTRA_ARGS[@]}"}"; do\n'
        f'  printf "<<%s>>\\n" "$a" >> "{result_file}"\n'
        "done\n"
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "TERM": term,
        "ATELIER_INSTALL_DIR": str(ATELIER_ROOT),
    }
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        ["bash", "-c", script_body],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        cwd=ATELIER_ROOT,
        start_new_session=True,
    )
    os.close(slave_fd)
    buf = bytearray()
    try:
        for marker, answer in zip(markers, answers, strict=True):
            buf = _expect(master_fd, buf, marker, timeout=15)
            time.sleep(0.25)
            os.write(master_fd, answer)
        buf = _drain(master_fd, buf, proc, timeout=10)
        proc.wait(timeout=5)
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
    transcript = buf.decode(errors="replace")
    assert proc.returncode == 0, f"host_wizard exited {proc.returncode}:\n{transcript}"
    assert result_file.exists(), f"transcript:\n{transcript}"
    return result_file


def _parse_result(result_file: Path) -> dict[str, str]:
    """Turn the '<<flag>>\\n<<value>>\\n...' result-file transcript into a dict."""
    tokens = [line.strip("<>") for line in result_file.read_text().splitlines()]
    pairs: dict[str, str] = {}
    it = iter(tokens)
    for flag in it:
        pairs[flag] = next(it)
    return pairs


# --- dumb-terminal fallback (free-text) --------------------------------------

_DUMB_MARKERS = [
    "Which agents should Atelier configure?",
    "Agents to add",
    "Skills to add",
    "Apply agent configs",
]


def _default_role_names() -> list[str]:
    return [name for name in _role_names_from_disk() if name not in {"auto", "bare"}]


def test_dumb_terminal_empty_answers_select_standard_roles_no_skills(tmp_path: Path) -> None:
    role_names = _role_names_from_disk()
    default_roles = _default_role_names()
    assert role_names and default_roles, "expected non-default roles under integrations/agents/"
    result_file = _run_host_wizard(
        answers=[b"2\n", b"\n", b"\n", b"1\n"],  # host: codex only (avoids --claude-project noise)
        markers=_DUMB_MARKERS,
        tmp_path=tmp_path,
        term="dumb",
    )
    pairs = _parse_result(result_file)
    assert pairs.get("--roles") == "code," + ",".join(default_roles)
    assert "auto" not in pairs["--roles"].split(",")
    assert "bare" not in pairs["--roles"].split(",")
    assert "--include-skills" not in pairs


def test_dumb_terminal_all_answer_includes_auto_and_bare(tmp_path: Path) -> None:
    role_names = _role_names_from_disk()
    assert {"auto", "bare"} <= set(role_names)
    result_file = _run_host_wizard(
        answers=[b"2\n", b"all\n", b"\n", b"1\n"],
        markers=_DUMB_MARKERS,
        tmp_path=tmp_path,
        term="dumb",
    )
    pairs = _parse_result(result_file)
    assert pairs.get("--roles") == "code," + ",".join(role_names)


def test_dumb_terminal_explicit_names_filter_unrecognized(tmp_path: Path) -> None:
    role_names = _role_names_from_disk()
    skill_names = _skill_names_from_disk()
    assert len(role_names) >= 2 and skill_names
    result_file = _run_host_wizard(
        answers=[
            b"2\n",
            (role_names[0] + ", bogus-role\n").encode(),
            (skill_names[0] + ", bogus-skill\n").encode(),
            b"1\n",
        ],
        markers=_DUMB_MARKERS,
        tmp_path=tmp_path,
        term="dumb",
    )
    pairs = _parse_result(result_file)
    assert pairs.get("--roles") == f"code,{role_names[0]}"
    assert pairs.get("--include-skills") == skill_names[0]


# --- real arrow-key checkbox menu ---------------------------------------------

_SELECTOR_MARKERS = [
    "Which agents should Atelier configure?",
    "Optional agent roles to install",
    "Optional skills",
    "Apply configs globally or just here?",
]


def test_confirming_defaults_selects_standard_roles_and_no_skills(tmp_path: Path) -> None:
    role_names = _role_names_from_disk()
    default_roles = _default_role_names()
    assert role_names and default_roles, "expected non-default roles from disk"
    result_file = _run_host_wizard(
        answers=[b"2\r", b"\r", b"\r", b"\r"],  # host: toggle codex only, confirm; accept both checklist defaults
        markers=_SELECTOR_MARKERS,
        tmp_path=tmp_path,
        term="xterm",
    )
    pairs = _parse_result(result_file)
    assert pairs.get("--roles") == "code," + ",".join(default_roles)
    assert "auto" not in pairs["--roles"].split(",")
    assert "bare" not in pairs["--roles"].split(",")
    assert "--include-skills" not in pairs


def test_toggling_code_does_not_deselect_it(tmp_path: Path) -> None:
    default_roles = _default_role_names()
    result_file = _run_host_wizard(
        answers=[b"2\r", b" \r", b"\r", b"\r"],  # try to toggle locked code off, confirm
        markers=_SELECTOR_MARKERS,
        tmp_path=tmp_path,
        term="xterm",
    )
    pairs = _parse_result(result_file)
    assert pairs.get("--roles") == "code," + ",".join(default_roles)


def test_deselecting_a_role_removes_it(tmp_path: Path) -> None:
    role_names = _role_names_from_disk()
    assert len(role_names) >= 2
    result_file = _run_host_wizard(
        answers=[b"2\r", b"jjj \r", b"\r", b"\r"],  # skip code/auto/bare, toggle first default role off
        markers=_SELECTOR_MARKERS,
        tmp_path=tmp_path,
        term="xterm",
    )
    default_roles = _default_role_names()
    pairs = _parse_result(result_file)
    assert pairs.get("--roles") == "code," + ",".join(default_roles[1:])
    assert default_roles[0] not in pairs["--roles"].split(",")


def test_selecting_a_skill_adds_it(tmp_path: Path) -> None:
    skill_names = _skill_names_from_disk()
    assert skill_names
    result_file = _run_host_wizard(
        answers=[b"2\r", b"\r", b" \r", b"\r"],  # accept all roles, toggle first skill on
        markers=_SELECTOR_MARKERS,
        tmp_path=tmp_path,
        term="xterm",
    )
    default_roles = _default_role_names()
    pairs = _parse_result(result_file)
    assert pairs.get("--roles") == "code," + ",".join(default_roles)
    assert pairs.get("--include-skills") == skill_names[0]


def test_claude_global_scope_appends_claude_project_without_losing_roles(tmp_path: Path) -> None:
    """The exact regression this feature hit: selecting Claude with global
    scope appends --claude-project at the very end of host_wizard() -- that
    append must not wipe out --roles/--include-skills set earlier."""
    result_file = _run_host_wizard(
        answers=[
            b"a\r",
            b"\r",
            b"\r",
            b"\r",
        ],  # all hosts (includes claude), accept both checklist defaults, global scope
        markers=_SELECTOR_MARKERS,
        tmp_path=tmp_path,
        term="xterm",
    )
    default_roles = _default_role_names()
    pairs = _parse_result(result_file)
    assert pairs.get("--roles") == "code," + ",".join(default_roles)
    assert pairs.get("--claude-project") == str(ATELIER_ROOT)


def test_install_hosts_parses_roles_and_include_skills_flags() -> None:
    content = (SCRIPTS / "install_hosts.sh").read_text()
    assert "--roles)" in content
    assert "--include-skills)" in content
    include_skills_block = content.split("--include-skills)", 1)[1].split(";;", 1)[0]
    assert "CLAUDE_EXTRA_ARGS+=" in include_skills_block
    assert "CODEX_EXTRA_ARGS+=" in include_skills_block
    assert "PASSTHROUGH+=" not in include_skills_block
    assert "OPENCODE_EXTRA_ARGS+=" not in include_skills_block


def test_install_hosts_dry_run_all_hosts_survive_roles_and_include_skills() -> None:
    result = subprocess.run(
        [
            "bash",
            str(SCRIPTS / "install_hosts.sh"),
            "--all",
            "--roles",
            "code,explore",
            "--include-skills",
            "benchmark",
            "--dry-run",
            "--print-only",
        ],
        cwd=ATELIER_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    for host in ("claude", "codex", "opencode", "copilot", "antigravity"):
        assert f"OK       {host}" in result.stdout, result.stdout
    assert "FAILED" not in result.stdout


def test_install_hosts_threads_roles_into_claude_and_codex_agent_files() -> None:
    result = subprocess.run(
        [
            "bash",
            str(SCRIPTS / "install_hosts.sh"),
            "--claude",
            "--codex",
            "--roles",
            "code,explore",
            "--include-skills",
            "benchmark",
            "--dry-run",
            "--print-only",
        ],
        cwd=ATELIER_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "ATELIER_VERBOSE": "1"},
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "agents/explore.md" in result.stdout or "atelier.explore" in result.stdout, result.stdout
    assert "--include-skills=benchmark" in result.stdout, result.stdout
