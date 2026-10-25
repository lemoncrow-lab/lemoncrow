"""The commands the quick start and the setup guides show are real, so the docs cannot drift from the CLI.

The audit found a landing page and a README that told a new developer to run flags the installed ``lc`` did not have. This
extracts every ``lc ...`` command from the README's Quick start and Removal sections and from ``docs/setup/ai-review.md``
and ``docs/setup/installation.md``, and checks each subcommand, flag and setting key against what the installed ``lc``
prints: ``lc --help``, ``lc review --help`` (the client), the Review engine's own help for ``--ai``, ``hook``,
``init-action`` and ``config``, and ``lc settings``. Nothing is run: only help text and the settings registry are read.
"""

from __future__ import annotations

import io
import re
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner
from lemoncrow_client import cli as client_cli

from lemoncrow.core.settings import find_by_key
from lemoncrow.pro.capabilities.review.local_cli import main as engine_main
from lemoncrow.pro.capabilities.review.local_cli import settings_cli

ROOT = Path(__file__).resolve().parents[2]
FENCE = re.compile(r"```(?:bash|sh|shell|console)?\n(.*?)```", re.DOTALL)


def client_help(*argv: str) -> str:
    sink = io.StringIO()
    client_cli.main([*argv, "--help"], sink)
    return sink.getvalue()


def click_help(command: object, *argv: str) -> str:
    result = CliRunner().invoke(command, [*argv, "--help"])  # type: ignore[arg-type]
    assert result.exit_code == 0, result.output
    return str(result.output)


def has_flag(text: str, flag: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) is not None


def section(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin + len(start))]


def commands_in(markdown: str) -> list[str]:
    """Every ``lc ...`` command in the fenced shell blocks of *markdown* (comments cut, ``&&`` and ``;`` split)."""

    found: list[str] = []
    # A command in a sentence is a promise too: `lc review --ai --agent claude`.
    found += [span.strip() for span in re.findall(r"`(lc [^`\n]+)`", FENCE.sub("", markdown))]
    for block in FENCE.findall(markdown):
        joined = re.sub(r"\\\n\s*", " ", block)
        for raw in joined.splitlines():
            line = raw.strip().removeprefix("$ ").strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"\s+#\s.*$", "", line)  # a trailing comment
            for part in re.split(r"\s*(?:&&|;)\s*", line):
                if part.startswith("lc "):
                    found.append(part.strip())
    return found


def check(command: str, helps: dict[str, str]) -> list[str]:
    """What is wrong with *command* (empty when every word of it is real)."""

    problems: list[str] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return [f"{command}: cannot be split into words"]
    words = tokens[1:]
    flags = [word.split("=", 1)[0] for word in words if word.startswith("-")]
    verb = words[0]
    if verb in {"--version", "--help"}:
        return []  # root options every client has
    sub = words[1] if len(words) > 1 and not words[1].startswith("-") else ""
    root = helps["root"]
    if not re.search(rf"\blc {re.escape(verb)}\b", root):
        return [f"{command}: `lc {verb}` is not in `lc --help`"]
    corpus = ""
    if len(words) == 1:
        return []  # a bare command name in a sentence ("`lc with` is temporary"): it only has to exist
    if verb == "review":
        if sub in {"hook", "init-action", "config"}:
            if not re.search(rf"\blc review {re.escape(sub)}\b", root):
                problems.append(f"`lc review {sub}` is not in `lc --help`")
            deeper = words[2] if sub == "hook" and len(words) > 2 else ""
            corpus = helps[f"review {sub} {deeper}".strip()] if deeper in {"install", "run"} or not deeper else ""
            if sub == "hook" and deeper not in {"install", "run"}:
                problems.append(f"`lc review hook {deeper}` is not a hook command")
        else:
            corpus = helps["review client"] + helps["review engine"]
    elif verb == "settings":
        if sub and sub not in {"show", "get", "set", "reset"}:
            problems.append(f"`lc settings {sub}` is not a settings command")
        corpus = helps.get(f"settings {sub}".strip(), "")
        if sub in {"get", "set", "reset"}:
            key = words[2] if len(words) > 2 else ""
            if find_by_key(key) is None:
                problems.append(f"`{key}` is not a setting (see `lc settings show --all`)")
    elif verb == "with":
        host = sub
        if host not in {"claude", "codex", "opencode", "pi"} or host not in root.split("lc with")[1].splitlines()[0]:
            problems.append(f"`lc with {host}`: the host is not one `lc --help` names")
    elif verb in {"runtime", "rt"}:
        line = next(
            (row for row in helps["runtime"].splitlines() if re.search(rf"\bruntime {re.escape(sub)}\b", row)), ""
        )
        if not line:
            problems.append(f"`lc {verb} {sub}` is not in `lc rt --help`")
        corpus = line
    elif verb in {"status", "update"}:
        corpus = helps[verb]
    elif verb == "uninstall":
        if sub not in {"review", "runtime", "all"}:
            problems.append(f"`lc uninstall {sub}`: the product must be review, runtime or all")
        corpus = helps["uninstall"]
    for flag in flags:
        if not corpus or not has_flag(corpus, flag):
            problems.append(f"{flag} is not in the help of `lc {' '.join(words[: 2 if sub else 1])}`")
    return [f"{command}: {problem}" for problem in problems]


@pytest.fixture(scope="module")
def helps() -> dict[str, str]:
    review = engine_main.review_cmd
    hook = engine_main.SUBCOMMANDS["hook"]
    return {
        "root": client_help(),
        "review client": client_help("review"),
        "review engine": click_help(review),
        "review hook": click_help(hook),
        "review hook install": click_help(hook, "install"),
        "review hook run": click_help(hook, "run"),
        "review init-action": click_help(engine_main.SUBCOMMANDS["init-action"]),
        "review config": click_help(engine_main.SUBCOMMANDS["config"]),
        "settings": click_help(settings_cli.settings_group),
        "settings show": click_help(settings_cli.settings_group, "show"),
        "settings get": click_help(settings_cli.settings_group, "get"),
        "settings set": click_help(settings_cli.settings_group, "set"),
        "settings reset": click_help(settings_cli.settings_group, "reset"),
        "runtime": client_help("runtime"),
        "status": client_help("status"),
        "update": client_help("update"),
        "uninstall": client_help("uninstall"),
    }


def quick_start_commands() -> list[str]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quick = section(readme, "## Quick start", "\n---\n")
    removal = section(readme, "### Removal", "### Building from source")
    return commands_in(quick) + commands_in(removal)


def test_the_readme_quick_start_is_the_three_steps_and_its_commands_exist(helps: dict[str, str]) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quick = section(readme, "## Quick start", "\n---\n")
    first = FENCE.findall(quick)[0]
    lines = [line for line in first.splitlines() if line.strip()]
    assert len(lines) == 3, first  # install, a key (or an agent), the review
    assert lines[0].startswith("curl -fsSL https://lemoncrow.com/install.sh | bash")
    assert lines[1].startswith("export ANTHROPIC_API_KEY=")
    assert "lc review --ai" in lines[2]
    commands = quick_start_commands()
    assert "lc review --ai" in commands and "lc review --ai --agent claude" in commands
    assert "lc settings set llm.anthropic_api_key" in quick  # the key-saving path the docs promise
    problems = [problem for command in commands for problem in check(command, helps)]
    assert problems == []


@pytest.mark.parametrize("page", ["docs/setup/ai-review.md", "docs/setup/installation.md"])
def test_the_setup_pages_show_only_commands_that_exist(page: str, helps: dict[str, str]) -> None:
    commands = commands_in((ROOT / page).read_text(encoding="utf-8"))
    assert commands, page
    problems = [problem for command in commands for problem in check(command, helps)]
    assert problems == []


def test_the_first_ai_review_page_has_the_three_steps_and_the_message_the_cli_prints() -> None:
    from lemoncrow.pro.capabilities.review.pr_bot.local import DOCS_URL, no_route_text

    page = (ROOT / "docs/setup/ai-review.md").read_text(encoding="utf-8")
    first = FENCE.findall(page)[0]
    assert [line.split("#")[0].strip() for line in first.splitlines()][:3] == [
        "curl -fsSL https://lemoncrow.com/install.sh | bash",
        "export ANTHROPIC_API_KEY=sk-ant-...",
        "cd your-project && lc review --ai",
    ]
    assert no_route_text(lambda _name: None) in page  # the page shows exactly what the command prints
    assert DOCS_URL == "https://docs.lemoncrow.com/setup/ai-review" and DOCS_URL.endswith("/setup/ai-review")
    assert (ROOT / "docs" / "setup" / "ai-review.md").is_file()
    assert "setup/ai-review" in (ROOT / "docs-site" / "sidebars.ts").read_text(encoding="utf-8")


def test_the_checker_catches_a_command_that_is_not_real(helps: dict[str, str]) -> None:
    assert check("lc review --ai --agent claude --remember", helps) == []
    assert check("lc review hook install --strict", helps) == []
    assert check("lc settings set llm.openai_api_key", helps) == []
    assert check("lc uninstall all --dry-run", helps) == []
    for wrong in (
        "lc review --ai --turbo",
        "lc review hook uninstall",
        "lc review init-action --nope",
        "lc settings set no.such.key",
        "lc settings frobnicate",
        "lc uninstall everything",
        "lc frobnicate",
        "lc rt fly claude",
        "lc status --nope",
    ):
        assert check(wrong, helps), wrong
