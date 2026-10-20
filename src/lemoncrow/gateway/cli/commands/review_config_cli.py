"""``lc review config``: the effective, merged Review rules, with the file each one came from (G10).

The local surface of the same governance resolver the AI pull request reviewer runs
(:func:`lemoncrow.pro.capabilities.review.rules.resolve_review_governance`). It reads only the **trusted base** (the
pull request's merge base for ``--pr``, else ``origin/HEAD`` when known, else ``HEAD``), never the working tree, so what
it prints is what a review would use: ``.lemoncrow/review.yaml`` first, then ``.greptile/``, then ``greptile.json``, then
``AGENTS.md`` and ``CLAUDE.md`` as context.

``--effective`` prints every rule, exclusion, imported-but-not-applied setting and warning with its source file.
``--dry-run`` takes the files a pull request changes (``--pr``, ``--head`` or ``--files``) and shows which rules would
apply to each and which files the model would never read. No model is called and nothing is written.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.rules import ReviewGovernance
    from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

_REVIEW_CONFIG = ".lemoncrow/review.yaml"
_MAX_FILE_BYTES = 1024 * 1024
_MAX_LISTED_PATHS = 200_000
_SHOWN_FILES = 60


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)


@dataclass(frozen=True, slots=True)
class BaseCommit:
    """The commit the configuration is read from: what the user named, and the commit it resolved to."""

    ref: str
    sha: str

    def to_wire(self) -> dict[str, str]:
        return {"ref": self.ref, "sha": self.sha}


def resolve_base(repo: Path, ref: str) -> BaseCommit:
    if ref.startswith("-"):
        raise click.ClickException(f"--base {ref!r} is not a revision")
    resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if resolved.returncode != 0:
        raise click.ClickException(f"--base {ref!r} does not resolve to a commit in {repo}")
    return BaseCommit(ref, resolved.stdout.decode("ascii", errors="replace").strip())


def base_reader(repo: Path, base: BaseCommit) -> Callable[[str], TrustedBaseDocument]:
    """``read_base_document`` over ``git show <sha>:<path>``: only bytes committed at the base, never the work tree."""

    from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

    def read(path: str) -> TrustedBaseDocument:
        source = f"trusted base {base.ref} ({base.sha[:12]}) {path}"
        if _git(repo, "cat-file", "-e", f"{base.sha}:{path}").returncode != 0:
            return TrustedBaseDocument(path=path, content="", state="absent", source=source)
        shown = _git(repo, "show", f"{base.sha}:{path}")
        if shown.returncode != 0 or len(shown.stdout) > _MAX_FILE_BYTES:
            return TrustedBaseDocument(path=path, content="", state="provisional", source=source)
        content = shown.stdout.decode("utf-8", errors="replace")
        if not content:
            return TrustedBaseDocument(path=path, content="", state="absent", source=source)
        return TrustedBaseDocument(path=path, content=content, state="present", source=source)

    return read


def base_paths_lister(repo: Path, base: BaseCommit) -> Callable[[], Sequence[str] | None]:
    def listing() -> Sequence[str] | None:
        tree = _git(repo, "ls-tree", "-r", "--name-only", "-z", base.sha)
        if tree.returncode != 0:
            return None
        names = tree.stdout.decode("utf-8", errors="replace").split("\0")
        return [name for name in names[:_MAX_LISTED_PATHS] if name]

    return listing


def effective_governance(repo: Path, base: BaseCommit) -> ReviewGovernance:
    """The governance a review of this repository resolves from *base*: the repository layers, no organization policy."""

    from lemoncrow.pro.capabilities.review.rules import resolve_review_governance

    read = base_reader(repo, base)
    return resolve_review_governance(
        read(_REVIEW_CONFIG), read_base_document=read, base_paths=base_paths_lister(repo, base)
    )


def changed_paths(repo: Path, base_sha: str, head: str) -> list[str]:
    """Paths that differ between *base_sha* and *head* (the pull request's files; renames count as both names)."""

    if head.startswith("-"):
        raise click.ClickException(f"--head {head!r} is not a revision")
    diff = _git(repo, "diff", "--name-only", "--no-renames", "-z", base_sha, head)
    if diff.returncode != 0:
        raise click.ClickException(
            f"cannot diff {base_sha[:12]} against {head!r}: {diff.stderr.decode(errors='replace').strip()}"
        )
    return [name for name in diff.stdout.decode("utf-8", errors="replace").split("\0") if name]


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #


def _scope(paths: Sequence[str]) -> str:
    return ", ".join(paths) if paths else "all paths"


def _trim(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def render_effective_text(wire: dict[str, Any], base: BaseCommit) -> str:
    rules = wire["rules"]
    critical = sum(1 for row in rules if row["severity"] == "critical")
    out = [
        "Effective Review configuration",
        f"  trusted base: {base.ref} ({base.sha[:12]}); rules and context are read from here, never from a pull request",
        "  precedence:   " + " > ".join(wire["precedence"]),
        "  scope:        repository files only; organization policy is not included in this local view",
        "",
        "Files read",
    ]
    for row in wire["sources"]:
        out.append(f"  {row['path']:<40} {row['state']:<12} {row['role']}")
    out += ["", f"Rules ({len(rules)}: {critical} critical, {len(rules) - critical} advisory)"]
    if not rules:
        out.append("  none")
    for row in rules:
        out.append(f"  {row['key']}  [{row['severity']}]  scope: {_scope(row['paths'])}")
        out.append(f"      from {row['source']} ({row['origin']})")
        out.append(f"      {_trim(row['instruction'], 160)}")
    ignore = wire["ignore"]
    other_rows = [row for row in wire["content_access"] if not row["imported"]]
    out += ["", "AI content access (paths the model may not read)"]
    if ignore is None and not other_rows:
        out.append("  none")
    if ignore is not None:
        out.append(
            f"  ignorePatterns from {ignore['source']} ({len(ignore['globs'])} globs from {len(ignore['patterns'])} patterns)"
        )
        out.append("      " + _trim(", ".join(ignore["patterns"]), 200))
    for row in other_rows:
        out.append(f"  {row['mode']} from {row['source'] or row['origin']}: {_scope(row['paths'])}")
    out += ["", "Imported, not applied"]
    if not wire["settings"]:
        out.append("  none")
    for row in wire["settings"]:
        target = f" -> closest {row['mapped_to']}" if row["mapped_to"] else ""
        out.append(f"  {row['key']} {row['value']}{target}  (from {row['source']})")
        out.append(f"      {row['reason']}")
    out += ["", "Project conventions (context for the investigator, never a rule)"]
    if not wire["conventions"]:
        out.append("  none")
    for row in wire["conventions"]:
        depths = ", ".join(f"{depth} {tokens}" for depth, tokens in row["tokens_by_depth"].items())
        out.append(
            f"  {row['path']}  {row['chars']:,} characters; tokens in a prompt at quick/auto/thorough/deep: {depths}"
        )
    notes = [
        row for row in wire["notes"] if row["kind"] in {"warning", "not_applied", "overridden", "disabled", "dropped"}
    ]
    out += ["", f"Notes ({len(notes)})"]
    if not notes:
        out.append("  none")
    for row in notes:
        out.append(f"  {row['kind']:<12} {row['source']}: {row['message']}")
    for message in wire["errors"]:
        out.append(f"  error        {message}")
    return "\n".join(out) + "\n"


def render_dry_run_text(dry: dict[str, Any], *, label: str) -> str:
    rules = dry["rules_applying"]
    critical = sum(1 for row in rules if row["severity"] == "critical")
    out = [
        f"Dry run: {label}",
        f"  {dry['files_changed']} changed files; {dry['files_excluded']} the model would never read",
        "",
    ]
    for row in dry["files"][:_SHOWN_FILES]:
        if not row["reviewed"]:
            out.append(
                f"  {row['path']}  excluded from AI review ({row.get('excluded_by') or row['reason'] or 'ignored'})"
            )
        elif row["rules"]:
            out.append(f"  {row['path']}  rules: {', '.join(row['rules'])}")
        else:
            out.append(f"  {row['path']}  no rule applies")
    if len(dry["files"]) > _SHOWN_FILES:
        out.append(f"  ... and {len(dry['files']) - _SHOWN_FILES} more files")
    out += ["", f"Rules that would apply: {len(rules)} ({critical} critical, {len(rules) - critical} advisory)"]
    for row in rules:
        out.append(f"  {row['key']}  [{row['severity']}]")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


def resolve_pull_request_range(repo: Path, spec: str) -> tuple[str, str, str]:
    """``(merge base sha, head sha, label)`` of a pull request: the range the AI reviewer would review."""

    from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError, resolve_pull_request

    try:
        pull = resolve_pull_request(repo, spec)
    except (GitHubCliError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    return (
        pull.merge_base_sha,
        pull.head_sha,
        f"PR #{pull.ref.number} ({pull.ref.slug}), merge base {pull.merge_base_sha[:12]}",
    )


@click.command("config")
@click.option(
    "--effective", "effective", is_flag=True, help="Print the effective merged rules, with the file each came from."
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="Show which rules would apply to the files of a pull request (--pr), a range (--head) or a list (--files).",
)
@click.option(
    "--base",
    "base_ref",
    default=None,
    help="Trusted base to read from. Default: the PR's merge base with --pr, else origin/HEAD when known, else HEAD.",
)
@click.option(
    "--pr",
    "pr_spec",
    default=None,
    metavar="NUMBER|URL",
    help="With --dry-run: the pull request whose changed files to use.",
)
@click.option(
    "--head", "head_ref", default=None, help="With --dry-run: the files changed between the base and this revision."
)
@click.option("--files", "files", multiple=True, help="With --dry-run: a changed path (repeatable).")
@click.option(
    "--repo",
    "repo_path",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Repository to describe. Default: the current directory.",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
def config_cmd(
    effective: bool,
    dry_run: bool,
    base_ref: str | None,
    pr_spec: str | None,
    head_ref: str | None,
    files: tuple[str, ...],
    repo_path: Path | None,
    as_json: bool,
) -> None:
    """Show the effective Review configuration a pull request review would use, and what it would apply to.

    Reads .lemoncrow/review.yaml, then .greptile/ and greptile.json, then AGENTS.md and CLAUDE.md (context only), all from
    the trusted base. A pull request that adds or edits any of them cannot change its own review.
    """

    if not effective and not dry_run:
        raise click.UsageError("choose --effective, --dry-run, or both")
    sources = [name for name, value in (("--pr", pr_spec), ("--head", head_ref), ("--files", files)) if value]
    if dry_run and len(sources) != 1:
        raise click.UsageError("--dry-run needs exactly one of --pr, --head or --files")
    if not dry_run and sources:
        raise click.UsageError(f"{sources[0]} only applies with --dry-run")

    from lemoncrow.pro.capabilities.review.config_import import dry_run_wire, effective_wire
    from lemoncrow.pro.capabilities.review.reproduce import default_trusted_base

    where = (repo_path or Path.cwd()).expanduser()
    top = _git(where, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise click.ClickException(f"{where} is not inside a Git repository")
    repo = Path(top.stdout.decode("utf-8", errors="replace").strip())

    paths: list[str] = []
    label = ""
    pr_base = ""
    if dry_run:
        if pr_spec:
            pr_base, pr_head, label = resolve_pull_request_range(repo, pr_spec)
            paths = changed_paths(repo, pr_base, pr_head)
        elif head_ref:
            # The range is base...head; the base is resolved below, so the files are listed after it.
            label = f"changes up to {head_ref}"
        else:
            paths = list(dict.fromkeys(files))
            label = f"{len(paths)} named files"
    base = resolve_base(repo, base_ref or pr_base or default_trusted_base(repo))
    if dry_run and head_ref:
        paths = changed_paths(repo, base.sha, head_ref)
        label = f"{base.ref}..{head_ref}"
    governance = effective_governance(repo, base)

    wire: dict[str, Any] = {"base": base.to_wire()}
    if effective:
        wire["effective"] = effective_wire(governance)
    if dry_run:
        wire["dry_run"] = dry_run_wire(governance, paths)
    if as_json:
        click.echo(json.dumps(wire, indent=2, sort_keys=True))
        return
    if effective:
        click.echo(render_effective_text(wire["effective"], base), nl=False)
    if dry_run:
        if effective:
            click.echo()
        click.echo(render_dry_run_text(wire["dry_run"], label=label), nl=False)


__all__ = [
    "BaseCommit",
    "base_paths_lister",
    "base_reader",
    "changed_paths",
    "config_cmd",
    "effective_governance",
    "render_dry_run_text",
    "render_effective_text",
    "resolve_base",
]
