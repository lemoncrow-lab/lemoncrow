"""``lc review config import PATH``: bring another review tool's config over, and say what became of each setting (G10).

The importer reads rules from a file ``import.rules_files`` lists, so a team's rules already work once that is written.
What did not move were its settings (a strictness, ignore patterns, comment types, a free-text brief, a severity per
rule), and nothing found the folder unless a hand-written ``.lemoncrow/review.yaml`` pointed at it. This command does
both from one path: it reads the folder or file, maps each setting to the LemonCrow setting that is closest
(:mod:`...settings_import`), writes ``.lemoncrow/review.yaml`` (and the instructions file) for the team, and prints a short
report: carried over, mapped to something close, not carried and why.

It names no product and looks for none: the folder is the one argument, so a team that points at it has chosen it. Nothing
is read from the pull request, nothing is sent anywhere and no model is called. It never overwrites what the team wrote:
an existing ``.lemoncrow/review.yaml`` that already has a ``review:`` or ``import:`` block is left alone, with the block to
add printed, unless ``--force`` is given (the file is then merged, your values win, and its comments are not kept).
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import click
import yaml

REVIEW_CONFIG = ".lemoncrow/review.yaml"
_MAX_FILE_BYTES = 1024 * 1024
_SETTINGS_FILE = "config.json"
_RULES_FILE = "rules.md"
_CONTEXT_FILES = "files.json"
_STRUCTURED = (".json", ".yaml", ".yml")
_MARKDOWN = (".md", ".markdown")
_HEADER = (
    "# Written by `lc review config import`. Commit it to your default branch: a review reads this file from the merge\n"
    "# base, so a pull request that edits it is still reviewed under the old one. `lc review config --effective` shows\n"
    "# what it does. A rule listed under review.rules is held at that severity (p1 at most); to make one a gate, list it\n"
    "# under import.critical and delete its review.rules entry.\n"
)
_ADDED = "# Added by `lc review config import`.\n"

Action = Literal["create", "append", "merge", "unchanged", "blocked"]


@dataclass(frozen=True, slots=True)
class FilePlan:
    """What happens to one file the command writes."""

    path: str
    action: Action
    text: str


def _toplevel(where: Path) -> Path:
    done = subprocess.run(["git", "-C", str(where), "rev-parse", "--show-toplevel"], capture_output=True, check=False)
    if done.returncode != 0:
        raise click.ClickException(f"{where} is not inside a Git repository")
    return Path(done.stdout.decode("utf-8", errors="replace").strip()).resolve()


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise click.ClickException(f"{path.name} is larger than {_MAX_FILE_BYTES // 1024} KiB; not read")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"cannot read {path}: {exc}") from exc


def _dump(document: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(document), sort_keys=False, default_flow_style=False, allow_unicode=True, width=10_000)


def _merge(
    existing: Mapping[str, Any], addition: Mapping[str, Any], kept: list[str], prefix: str = ""
) -> dict[str, Any]:
    """*addition* under *existing*: what the file already says wins, lists are joined, and a clash is named in *kept*."""

    out = dict(existing)
    for key, value in addition.items():
        here = f"{prefix}{key}"
        if key not in out:
            out[key] = value
        elif isinstance(out[key], Mapping) and isinstance(value, Mapping):
            out[key] = _merge(out[key], value, kept, f"{here}.")
        elif isinstance(out[key], list) and isinstance(value, list):
            out[key] = [*out[key], *[item for item in value if item not in out[key]]]
        elif out[key] != value:
            kept.append(f"{here}: your {out[key]!r} was kept; the import had {value!r}")
    return out


def _alias_clash(existing: Mapping[str, Any], addition: dict[str, Any], kept: list[str]) -> None:
    """``strictness`` and ``finding_threshold`` are one setting: the file's own word wins, so the other is not added."""

    review, mine = existing.get("review"), addition.get("review")
    if (
        isinstance(review, Mapping)
        and isinstance(mine, dict)
        and "strictness" in mine
        and "finding_threshold" in review
    ):
        kept.append(
            f"review.finding_threshold: your {review['finding_threshold']!r} was kept; "
            f"the import had strictness {mine.pop('strictness')!r}"
        )


def plan_review_yaml(existing_text: str | None, addition: dict[str, Any], *, force: bool) -> tuple[FilePlan, list[str]]:
    """How ``.lemoncrow/review.yaml`` changes, and the settings of the file that the import did not overwrite."""

    if not addition:
        return FilePlan(REVIEW_CONFIG, "unchanged", existing_text or ""), []
    if existing_text is None or not existing_text.strip():
        return FilePlan(REVIEW_CONFIG, "create", _HEADER + _dump(addition)), []
    try:
        existing = yaml.safe_load(existing_text)
    except yaml.YAMLError as exc:
        raise click.ClickException(
            f"{REVIEW_CONFIG} does not parse ({str(exc).splitlines()[0]}); fix it first"
        ) from exc
    if not isinstance(existing, Mapping):
        raise click.ClickException(f"{REVIEW_CONFIG} is not a YAML mapping; fix it first")
    kept: list[str] = []
    _alias_clash(existing, addition, kept)
    if not addition:
        return FilePlan(REVIEW_CONFIG, "unchanged", existing_text), kept
    merged = _merge(existing, addition, kept)
    if merged == dict(existing):
        return FilePlan(REVIEW_CONFIG, "unchanged", existing_text), kept
    if not set(addition) & set(existing):
        return FilePlan(REVIEW_CONFIG, "append", existing_text.rstrip("\n") + "\n\n" + _ADDED + _dump(addition)), kept
    return FilePlan(REVIEW_CONFIG, "merge" if force else "blocked", _dump(merged)), kept


def plan_instructions(existing: str | None, text: str, path: str, *, force: bool) -> FilePlan | None:
    if not text:
        return None
    wanted = f"# Review instructions\n\n{text}\n"
    if existing is None:
        return FilePlan(path, "create", wanted)
    if existing == wanted:
        return FilePlan(path, "unchanged", wanted)
    return FilePlan(path, "merge" if force else "blocked", wanted)


# --------------------------------------------------------------------------- #
# what the command reads
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Source:
    """The files the argument names, as repository paths."""

    label: str
    settings: str = ""  # a JSON or YAML file with settings and (maybe) rules
    rules_markdown: str = ""
    context_list: str = ""  # a per-scope list of context files, which has no counterpart


def _find_source(top: Path, given: Path) -> Source:
    target = (Path.cwd() / given.expanduser()).resolve()
    try:
        relative = PurePosixPath(target.relative_to(top).as_posix())
    except ValueError:
        raise click.ClickException(
            f"{given} is outside the repository {top}; a review reads its config from the repository"
        ) from None
    if ".git" in relative.parts:
        raise click.ClickException(f"{given} is inside .git")
    if target.is_dir():
        found = {name: (target / name).is_file() for name in (_SETTINGS_FILE, _RULES_FILE, _CONTEXT_FILES)}
        if not any(found.values()):
            raise click.ClickException(
                f"{given} holds none of {_SETTINGS_FILE}, {_RULES_FILE} or {_CONTEXT_FILES}; name the file itself"
            )
        prefix = "" if str(relative) == "." else f"{relative}/"
        return Source(
            f"{relative}/" if prefix else "./",
            settings=f"{prefix}{_SETTINGS_FILE}" if found[_SETTINGS_FILE] else "",
            rules_markdown=f"{prefix}{_RULES_FILE}" if found[_RULES_FILE] else "",
            context_list=f"{prefix}{_CONTEXT_FILES}" if found[_CONTEXT_FILES] else "",
        )
    if not target.is_file():
        raise click.ClickException(f"{given} does not exist")
    suffix = target.suffix.lower()
    if suffix in _STRUCTURED:
        return Source(str(relative), settings=str(relative))
    if suffix in _MARKDOWN:
        return Source(str(relative), rules_markdown=str(relative))
    raise click.ClickException(f"{given}: expected a folder, a .json, .yaml or .yml settings file, or a .md rules file")


def _parse_settings(top: Path, relative: str) -> Mapping[str, Any]:
    from lemoncrow.pro.capabilities.review.config_import import ImportNote, _structured_object
    from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

    document = TrustedBaseDocument(path=relative, content=_read(top / relative), state="present", source="working tree")
    notes: list[ImportNote] = []
    raw = _structured_object(document, notes)
    if raw is None:
        raise click.ClickException("; ".join(note.line() for note in notes) or f"{relative} could not be read")
    return raw


def _verified_rules(top: Path, review_text: str, rules_files: Sequence[str]) -> int:
    """How many rules the config imports once written, by running the real loader over it (no model, nothing written)."""

    from lemoncrow.pro.capabilities.review.rules import resolve_review_governance
    from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

    files = {REVIEW_CONFIG: review_text, **{path: _read(top / path) for path in rules_files}}

    def read(path: str) -> TrustedBaseDocument:
        text = files.get(path, "")
        return TrustedBaseDocument(
            path=path, content=text, state="present" if text else "absent", source="the import" if text else ""
        )

    governance = resolve_review_governance(read(REVIEW_CONFIG), read_base_document=read)
    imports = getattr(governance, "imports", None)
    return len(imports.rules) if imports is not None else 0


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #

_SECTIONS = (
    ("carried", "Carried over"),
    ("mapped", "Mapped to the closest LemonCrow setting"),
    ("dropped", "Not carried"),
)


def render_report(
    source: Source,
    rows: Sequence[Any],
    *,
    rules: int,
    rules_from: Sequence[str],
    files: Sequence[FilePlan],
    kept: Sequence[str],
    dry_run: bool,
    snippet: str,
) -> str:
    names = (
        (source.settings, source.rules_markdown)
        if (source.settings or source.rules_markdown)
        else (source.context_list,)
    )
    read = ", ".join(PurePosixPath(path).name for path in names if path)
    out = [f"Imported review config from {source.label} ({read})", ""]
    if not rows:
        out += ["No settings were found in it.", ""]
    for outcome, title in _SECTIONS:
        chosen = [row for row in rows if row.outcome == outcome]
        if not chosen:
            continue
        out.append(f"{title} ({len(chosen)})")
        for row in chosen:
            head = f"  {row.setting}" + (f"  ->  {row.target}" if row.target else "")
            out.append(head)
            out += (
                textwrap.wrap(row.detail, 100, initial_indent="      ", subsequent_indent="      ")
                if row.detail
                else []
            )
        out.append("")
    if rules:
        out.append(
            f"Rules: {rules} read through import.rules_files ({', '.join(rules_from)}) as advisory rules: a violation is "
            "P1 or P2, never P0, until you list a rule under import.critical. `lc review config --effective` lists them."
        )
    elif rules_from:
        out.append(f"Rules: none were found in {', '.join(rules_from)}.")
    if kept:
        out += ["", "Kept from your existing file:"] + [f"  {line}" for line in kept]
    out.append("")
    verbs = {
        "create": "Wrote",
        "append": "Added to",
        "merge": "Merged into",
        "unchanged": "Already up to date:",
        "blocked": "Left alone:",
    }
    for plan in files:
        verb = verbs[plan.action] if not (dry_run and plan.action not in ("unchanged", "blocked")) else "Would write"
        out.append(f"{verb} {plan.path}")
    blocked = [plan for plan in files if plan.action == "blocked"]
    if blocked:
        out += [
            "",
            "A file above already exists with different content, so nothing was written. Run again with --force to "
            f"replace it ({REVIEW_CONFIG} is merged instead: your values win, and it is rewritten without its comments).",
        ]
        if any(plan.path == REVIEW_CONFIG for plan in blocked):
            out += ["Or add this to it yourself:", "", textwrap.indent(snippet.rstrip("\n"), "    ")]
    elif any(plan.action in ("create", "append", "merge") for plan in files) and not dry_run:
        out += [
            "",
            "Next: commit these to your default branch. A review reads its config from the merge base, so the pull request "
            "that adds them is still reviewed under the old settings. Then `lc review config --effective --base <branch>` "
            "shows what applies.",
        ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


@click.command("import")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--dry-run", is_flag=True, help="Print the report and write nothing.")
@click.option(
    "--force",
    is_flag=True,
    help="Merge into a .lemoncrow/review.yaml that already has a review: or import: block, and replace a different "
    "instructions file. Your values win; the merged file is rewritten and its comments are not kept.",
)
@click.option(
    "--repo",
    "repo_path",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Repository to write into. Default: the current directory's.",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
def import_cmd(path: Path, dry_run: bool, force: bool, repo_path: Path | None, as_json: bool) -> None:
    """Bring another review tool's config over: its settings and its rules, with a report of what became of each.

    PATH is the folder that holds the config (it reads config.json and rules.md in it), or one .json/.yaml settings file
    or .md rules file. The settings are mapped to the closest LemonCrow setting and written to .lemoncrow/review.yaml (a
    free-text brief goes to .lemoncrow/review-instructions.md); the rules are listed under import.rules_files, where they
    are read as advisory rules. Nothing else is read, no model is called, and nothing leaves your machine.

    \b
    Carried over: strictness, ignore patterns, the largest-pull-request limit, summary-only mode.
    Mapped: comment types, free-text instructions, a severity per rule (never above P1: only a critical rule, or
    evidence beyond the model's own code, is a P0).
    Not carried, and said why: everything with no LemonCrow counterpart, each with the closest thing LemonCrow has.
    """

    from lemoncrow.pro.capabilities.review import settings_import

    top = _toplevel(((repo_path or Path.cwd()).expanduser()).resolve())
    source = _find_source(top, path)
    raw: Mapping[str, Any] = _parse_settings(top, source.settings) if source.settings else {}
    plan = settings_import.translate(raw, path=source.settings)
    rows = list(plan.rows)
    if source.context_list:
        rows.append(
            settings_import.ReportRow(
                _CONTEXT_FILES,
                "dropped",
                detail="a list of context files with a scope each has no counterpart: LemonCrow reads instruction files "
                "(AGENTS.md and the like) as repository-wide or per-folder context. Closest: list the paths in "
                "import.agent_docs_globs, which keeps no per-file scope or description.",
            )
        )

    has_rules = isinstance(raw.get("rules"), list) and bool(raw["rules"])
    rules_files = [file for file in (source.settings if has_rules else "", source.rules_markdown) if file]
    addition: dict[str, Any] = {}
    if plan.review:
        addition["review"] = dict(plan.review)
    imports: dict[str, Any] = {**({"rules_files": rules_files} if rules_files else {}), **plan.imports}
    if imports:
        addition["import"] = imports

    config_file = top / REVIEW_CONFIG
    review_plan, kept = plan_review_yaml(_read(config_file) if config_file.is_file() else None, addition, force=force)
    instructions_file = top / settings_import.INSTRUCTIONS_PATH
    instructions_plan = plan_instructions(
        _read(instructions_file) if instructions_file.is_file() else None,
        plan.instructions,
        settings_import.INSTRUCTIONS_PATH,
        force=force,
    )
    files = [item for item in (instructions_plan, review_plan if addition else None) if item is not None]
    blocked = any(item.action == "blocked" for item in files)

    # The count of rules is the one a review will see: the real loader, run over the config as it will be written.
    rules = _verified_rules(top, review_plan.text if addition else "", rules_files) if rules_files else 0

    if not dry_run and not blocked:
        for item in files:
            if item.action in ("unchanged", "blocked"):
                continue
            target = top / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.text, encoding="utf-8")

    snippet = _dump(addition) if addition else ""
    if as_json:
        counts = {
            outcome: sum(1 for row in rows if row.outcome == outcome) for outcome in ("carried", "mapped", "dropped")
        }
        document = {
            "source": source.label,
            "dry_run": dry_run,
            "written": not dry_run
            and not blocked
            and any(item.action not in ("unchanged", "blocked") for item in files),
            "summary": counts,
            "report": [row.to_wire() for row in rows],
            "rules_imported": rules,
            "rules_files": rules_files,
            "files": [{"path": item.path, "action": item.action} for item in files],
            "kept": kept,
            "review_yaml_addition": snippet,
        }
        click.echo(json.dumps(document, indent=2, sort_keys=True))
    else:
        click.echo(
            render_report(
                source,
                rows,
                rules=rules,
                rules_from=[PurePosixPath(file).name for file in rules_files],
                files=files,
                kept=kept,
                dry_run=dry_run,
                snippet=snippet,
            ),
            nl=False,
        )
    if blocked and not dry_run:
        raise click.exceptions.Exit(1)


__all__ = ["FilePlan", "import_cmd", "plan_instructions", "plan_review_yaml", "render_report"]
