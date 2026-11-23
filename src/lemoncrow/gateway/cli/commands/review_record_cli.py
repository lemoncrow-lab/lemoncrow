"""``lc review verify-record`` and ``lc review record-key``: signed review records offline.

``verify-record`` checks a record file against published keys with no server:
signature, key validity at signing time, revocation, payload digest and
predicate schema. Exit codes: 0 verified, 1 invalid, 2 unsigned, 3 revoked. An
unsigned record is always reported as UNSIGNED, never as verified.

``record-key init`` opts this machine's reviewer into signing local records
with a personal Ed25519 key kept outside any repository.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import click

from lemoncrow.gateway.cli.commands.review_data_flow_cli import data_flow_cmd
from lemoncrow.gateway.cli.commands.review_diagnostics_cli import diagnostics_cmd
from lemoncrow.gateway.cli.commands.review_hosted_cli import hosted_group
from lemoncrow.gateway.cli.commands.review_observation_cli import observation_worker_group
from lemoncrow.gateway.cli.commands.review_proof_cli import proof_group
from lemoncrow.gateway.cli.commands.review_qa_cli import qa_group
from lemoncrow.gateway.cli.commands.review_status_cli import status_cmd
from lemoncrow.gateway.cli.commands.review_watch_deploy_cli import watch_deploy_cmd
from lemoncrow.pro.capabilities.review.local_cli.config import config_cmd
from lemoncrow.pro.capabilities.review.local_cli.hook import hook_group
from lemoncrow.pro.capabilities.review.local_cli.init_action import init_action_cmd

_MAX_KEYS_BYTES = 1024 * 1024
_MAX_RECORD_BYTES = 64 * 1024 * 1024


def _store_root(ctx: click.Context) -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    obj = ctx.find_root().obj or {}
    return Path(obj.get("root") or default_store_root())


def _read_json(path: Path, *, limit: int, what: str) -> Any:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise click.ClickException(f"cannot read {what} {path}: {exc}") from exc
    if len(data) > limit:
        raise click.ClickException(f"{what} {path} is larger than {limit} bytes")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise click.ClickException(f"{what} {path} is not JSON") from exc


def _fetch_keys(url: str) -> Any:
    from urllib.parse import urlsplit
    from urllib.request import Request, urlopen

    if urlsplit(url).scheme != "https":
        raise click.ClickException("--keys URLs must use https")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "lemoncrow-verify-record"})
    try:
        with urlopen(request, timeout=10) as response:  # https only, checked above
            data = response.read(_MAX_KEYS_BYTES + 1)
    except OSError as exc:
        raise click.ClickException(f"cannot fetch the key set {url}: {exc}") from exc
    if len(data) > _MAX_KEYS_BYTES:
        raise click.ClickException(f"the key set at {url} is larger than {_MAX_KEYS_BYTES} bytes")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise click.ClickException(f"the key set at {url} is not JSON") from exc


def _local_key_set(root: Path) -> dict[str, Any] | None:
    from lemoncrow.pro.capabilities.review.record_signing import (
        RecordSigningUnavailable,
        configured_user_key_path,
        key_set_document,
        load_user_key,
        public_key_entry,
        user_key_id,
    )

    path = configured_user_key_path(root)
    if path is None:
        return None
    try:
        key = load_user_key(path)
    except RecordSigningUnavailable as exc:
        raise click.ClickException(exc.reason) from exc
    key_id = user_key_id(key.public_key())
    return key_set_document([public_key_entry(key.public_key(), key_id=key_id, issuer=key_id)], issuer=key_id)


@click.command("verify-record")
@click.argument("record_file", type=click.Path(path_type=Path, dir_okay=False))
@click.option(
    "--keys",
    "keys_source",
    default=None,
    help="Published key set: a JSON file or an https URL. Default: your personal record-signing key, if configured.",
)
@click.option("--json", "as_json", is_flag=True, help="Output the verdict as JSON.")
@click.pass_context
def verify_record_cmd(ctx: click.Context, record_file: Path, keys_source: str | None, as_json: bool) -> None:
    """Verify a signed review record offline.

    Exit status: 0 verified, 1 invalid, 2 unsigned, 3 revoked.
    """

    from lemoncrow.pro.capabilities.review.record_verify import parse_key_set, verify_record

    document = _read_json(record_file, limit=_MAX_RECORD_BYTES, what="record")
    raw_keys: Any = None
    if keys_source:
        raw_keys = (
            _fetch_keys(keys_source)
            if keys_source.startswith(("https://", "http://"))
            else _read_json(Path(keys_source).expanduser(), limit=_MAX_KEYS_BYTES, what="key set")
        )
    else:
        raw_keys = _local_key_set(_store_root(ctx))
    try:
        keys = parse_key_set(raw_keys) if raw_keys is not None else None
    except ValueError as exc:
        raise click.ClickException(f"the key set is malformed: {exc}") from exc
    result = verify_record(document, keys=keys)
    if as_json:
        click.echo(json.dumps(result.to_wire(), indent=2, sort_keys=True))
    else:
        click.echo(result.headline())
        summary = result.summary
        if summary:
            click.echo(
                f"  Review {summary.get('review_id', '')} revision {summary.get('snapshot_number', '')} "
                f"({summary.get('revision_id', '')})"
            )
            click.echo(
                f"  Decision: {summary.get('decision', '')} by {summary.get('decided_by', '')} "
                f"at {summary.get('decided_at', '')}"
            )
            click.echo(f"  Policy generation: {summary.get('policy_generation', '')}")
        if result.payload_digest:
            click.echo(f"  Payload digest: {result.payload_digest}")
    sys.exit(result.exit_code)


_RECORD_LINK = re.compile(r"/api/reviews/(?P<review>[^/?#]+)/records/(?P<record>[^/?#]+)")


def _record_document(target: str, repo_root: Path) -> Any:
    """The record a ``verify`` target names: a file, or a record link read through this machine's Review session.

    A link is rebuilt on the configured Review server (as ``lc review open`` does), so a pasted old host or port
    still reaches the live server; the record itself is verified here, offline, whatever the server says.
    """

    from urllib.parse import unquote

    link = _RECORD_LINK.search(target) if target.startswith(("http://", "https://")) else None
    if link is None:
        return _read_json(Path(target).expanduser(), limit=_MAX_RECORD_BYTES, what="record")
    from lemoncrow_client.config import load_config
    from lemoncrow_client.errors import ClientError
    from lemoncrow_client.review_terminal import session_request

    path = f"/api/reviews/{link['review']}/records/{link['record']}"
    try:
        return session_request(load_config(product="review", cwd=repo_root))("GET", unquote(path), None)
    except ClientError as exc:
        raise click.ClickException(f"cannot read the record at {target}: {exc.message}") from exc


def _retraction_document(target: str, retraction_file: Path | None, repo_root: Path) -> Any:
    """The signed retraction to check with the record: a file given with --retraction, else, for a record link, the
    one the server holds (a record never retracted answers 404 and has none). A file target has none unless given."""

    from urllib.parse import unquote

    if retraction_file is not None:
        return _read_json(retraction_file.expanduser(), limit=_MAX_RECORD_BYTES, what="retraction")
    link = _RECORD_LINK.search(target) if target.startswith(("http://", "https://")) else None
    if link is None:
        return None
    from lemoncrow_client.config import load_config
    from lemoncrow_client.errors import ClientError
    from lemoncrow_client.review_terminal import session_request

    path = f"/api/reviews/{link['review']}/records/{link['record']}/retraction"
    try:
        return session_request(load_config(product="review", cwd=repo_root))("GET", unquote(path), None)
    except ClientError as exc:
        if "404" in exc.message or "has not been retracted" in exc.message:
            return None
        raise click.ClickException(f"cannot read the retraction at {target}: {exc.message}") from exc


def _current_code(repo_root: Path, signed_head: str) -> Any:
    """This checkout's HEAD, only when it is the repository the record is about (the signed commit is in it)."""

    import subprocess

    from lemoncrow.pro.capabilities.review.record_outcome import CurrentCode

    if not signed_head:
        return None

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    if git("cat-file", "-e", f"{signed_head}^{{commit}}") is None:
        return None
    head = git("rev-parse", "HEAD")
    return CurrentCode(head_sha=head) if head else None


#: Exit codes of `lc review verify` besides the outcome codes 0 to 5.
EXIT_INPUT_ERROR = 11
EXIT_USAGE_ERROR = 12


class _VerifyInputError(click.ClickException):
    """The record or the key set could not be read: not an outcome, so not one of the outcome codes."""

    exit_code = EXIT_INPUT_ERROR


class _VerifyUsageError(click.UsageError):
    exit_code = EXIT_USAGE_ERROR


class _VerifyCommand(click.Command):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as exc:
            raise _VerifyUsageError(exc.message, ctx=exc.ctx) from exc


@click.command("verify", cls=_VerifyCommand)
@click.argument("target")
@click.option(
    "--keys",
    "keys_source",
    default=None,
    help="Published key set: a JSON file or an https URL. Default: your personal record-signing key, if configured.",
)
@click.option(
    "--repo-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="The checkout to compare the signed commit with. Default: the current one.",
)
@click.option(
    "--retraction",
    "retraction_file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="The signed retraction file of this record. A record link reads the server's own.",
)
@click.option("--json", "as_json", is_flag=True, help="Output the verdict as JSON.")
@click.pass_context
def verify_cmd(
    ctx: click.Context,
    target: str,
    keys_source: str | None,
    repo_root: Path | None,
    retraction_file: Path | None,
    as_json: bool,
) -> None:
    """Check a signed record: a file, or a record link from the Verify dialog.

    Exit status: 0 valid, 1 not valid, 2 unsigned, 3 key not allowed, 4 code changed, 5 retracted;
    11 the record or key set could not be read, 12 a usage error.
    Code changed compares the signed commit with this checkout's HEAD; a record of a working tree
    (no commit) says the code was not checked. Retracted needs the signed retraction: given with --retraction,
    or read from the server for a record link. A retraction that does not withdraw this record reads Not valid.
    """

    import base64

    from lemoncrow.pro.capabilities.review.record_outcome import record_outcome
    from lemoncrow.pro.capabilities.review.record_verify import envelope_of, parse_key_set

    root = (repo_root or Path.cwd()).expanduser().resolve()
    raw_keys: Any = None
    try:
        document = _record_document(target, root)
        retraction = _retraction_document(target, retraction_file, root)
        if keys_source:
            raw_keys = (
                _fetch_keys(keys_source)
                if keys_source.startswith(("https://", "http://"))
                else _read_json(Path(keys_source).expanduser(), limit=_MAX_KEYS_BYTES, what="key set")
            )
        else:
            raw_keys = _local_key_set(_store_root(ctx))
    except click.ClickException as exc:
        raise _VerifyInputError(exc.message) from exc
    # Only the default key set is this machine's own key; a key set someone else published is not "you".
    own_key_id = ""
    if not keys_source and raw_keys:
        own_key_id = str(raw_keys["keys"][0]["kid"]) if raw_keys.get("keys") else ""
    try:
        keys = parse_key_set(raw_keys) if raw_keys is not None else None
    except ValueError as exc:
        raise _VerifyInputError(f"the key set is malformed: {exc}") from exc
    signed_head = ""
    envelope = envelope_of(document)
    if envelope is not None:
        try:
            statement = json.loads(base64.b64decode(str(envelope.get("payload") or "")))
            snapshot = statement["predicate"]["snapshot"]
            signed_head = str(snapshot.get("head_sha") or "") if snapshot.get("dirty") is False else ""
        except (ValueError, KeyError, TypeError, AttributeError):
            signed_head = ""
    result = record_outcome(
        document,
        keys=keys,
        current=_current_code(root, signed_head),
        retraction_document=retraction,
        own_key_id=own_key_id,
    )
    if as_json:
        click.echo(
            json.dumps({**result.to_wire(), "verification": result.verification.to_wire()}, indent=2, sort_keys=True)
        )
    else:
        click.echo(result.headline())
        if result.reasons and result.outcome in {"not_valid", "key_not_allowed"}:
            for reason in result.reasons:
                click.echo(f"  {reason}")
        if result.verification.payload_digest:
            click.echo(f"  Payload digest: {result.verification.payload_digest}")
    sys.exit(result.exit_code)


@click.group("record-key")
def record_key_group() -> None:
    """Manage your personal key for signing local review records (explicit opt-in)."""


@record_key_group.command("init")
@click.option(
    "--path",
    "key_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Where to create the key. Default: <LEMONCROW_ROOT>/review-signing/user-key.pem (never inside a repository).",
)
@click.pass_context
def record_key_init(ctx: click.Context, key_path: Path | None) -> None:
    """Create an owner-only Ed25519 key and opt local review records into signing with it."""

    from lemoncrow.core.settings import write_setting
    from lemoncrow.pro.capabilities.review.record_signing import (
        USER_KEY_SETTING,
        RecordSigningUnavailable,
        generate_user_key,
    )

    root = _store_root(ctx)
    target = (key_path or root / "review-signing" / "user-key.pem").expanduser().resolve()
    try:
        key_id = generate_user_key(target)
    except RecordSigningUnavailable as exc:
        raise click.ClickException(exc.reason) from exc
    write_setting(root, USER_KEY_SETTING, str(target))
    click.echo(f"Created personal record-signing key {key_id} at {target} (mode 0600).")
    click.echo("Local review records you export are now signed with it.")
    click.echo("Share `lc review record-key public` output with whoever verifies your records.")


@record_key_group.command("public")
@click.option("--out", "out_path", type=click.Path(path_type=Path, dir_okay=False), default=None)
@click.pass_context
def record_key_public(ctx: click.Context, out_path: Path | None) -> None:
    """Print (or write) the published key set for your personal record-signing key."""

    document = _local_key_set(_store_root(ctx))
    if document is None:
        raise click.ClickException("no personal record-signing key is configured; run `lc review record-key init`")
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if out_path is None:
        click.echo(text, nl=False)
    else:
        out_path.write_text(text, encoding="utf-8")
        click.echo(f"Wrote {out_path}")


#: ``lc review <name> ...`` subcommands (``review_cmd`` dispatches them before parsing REV).
REVIEW_SUBCOMMANDS: dict[str, click.Command] = {
    "status": status_cmd,
    "verify-record": verify_record_cmd,
    "verify": verify_cmd,
    "record-key": record_key_group,
    "data-flow": data_flow_cmd,
    "config": config_cmd,
    "hook": hook_group,
    "init-action": init_action_cmd,
    "hosted": hosted_group,
    "diagnostics": diagnostics_cmd,
    "observation-worker": observation_worker_group,
    "qa": qa_group,
    "proof": proof_group,
    "watch-deploy": watch_deploy_cmd,
}


class _LocalReviewUsageError(click.UsageError):
    """A command line a local `--ai` review cannot use: exit 3 (a hook reads 1 as a finding and 2 as incomplete)."""

    exit_code = 3


_HELP_ALL_KEY = "lemoncrow.review_help_all"

#: ``lc review --help`` examples: the human loop and the agent loop, exactly as typed.
_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("lc review", "review the working tree and open the Reader"),
    ("lc review main...HEAD", "review a commit range"),
    ("lc review --no-open --track", "agent: capture a new Revision, print a summary"),
    ("lc review status --json", "ready? exit 0 yes, 1 not yet, 2 none, 3 error"),
    ("lc review --feedback", "open comments as Markdown, for the agent"),
    ("lc review --mark src/auth.py", "a person marks it reviewed"),
    ("lc review --comment TEXT --on src/auth.py:L81", "a person comments on a line"),
)
_EXAMPLE_COLUMN = 31
#: One-line option help for the short screen; ``--help-all`` shows each option's full help.
_SHORT_OPTION_HELP: dict[str, str] = {
    "ai_review": "AI review on your own model key or --agent: prints findings, posts nothing.",
    "open_html": "Open the Reader (default). --no-open alone records nothing; add --track.",
    "snapshot_title": "Title for this Revision.",
    "repo_root": "Repository to review. Default: the current one.",
}


class ReviewCommand(click.Command):
    """``lc review`` with a few named subcommands ahead of its optional REV argument.

    A first argument naming a subcommand (``status``, ``verify-record``, ``record-key``, ``data-flow``, ``config``,
    ``hook``, ``init-action``, ``hosted``, ``diagnostics``, ``observation-worker``, ...) dispatches to it; anything
    else is parsed as the review itself.

    ``--help`` is one screen: the options in :attr:`COMMON_OPTIONS`, examples and the main subcommands. Every
    other option is hidden there (still accepted) and ``--help-all`` lists them all with :attr:`help_all_text`.
    """

    COMMON_OPTIONS: frozenset[str] = frozenset(
        {"staged", "open_html", "as_json", "repo_root", "ai_review", "snapshot_title", "help_all"}
    )
    help_all_text: str = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.params.append(
            click.Option(
                ["--help-all"],
                is_flag=True,
                expose_value=False,
                is_eager=True,
                callback=self._show_help_all,
                help="Show every option and subcommand, then exit.",
            )
        )
        self._always_hidden = {param.name for param in self.params if getattr(param, "hidden", False)}
        for param in self.params:
            if isinstance(param, click.Option) and param.name not in self.COMMON_OPTIONS:
                param.hidden = True

    def _show_help_all(self, ctx: click.Context, _param: click.Parameter, value: bool) -> None:
        if not value or ctx.resilient_parsing:
            return
        ctx.meta[_HELP_ALL_KEY] = True
        click.echo(ctx.get_help(), color=ctx.color)
        ctx.exit()

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        full = bool(ctx.meta.get(_HELP_ALL_KEY))
        options = [param for param in self.params if isinstance(param, click.Option)]
        hidden = {param.name: param.hidden for param in options}
        option_help = {param.name: param.help for param in options}
        short_help = self.help
        if full:
            for param in options:
                if param.name not in self._always_hidden:
                    param.hidden = False
            self.help = self.help_all_text or self.help
        else:
            for param in options:
                param.help = _SHORT_OPTION_HELP.get(param.name or "", param.help)
        try:
            self.format_usage(ctx, formatter)
            self.format_help_text(ctx, formatter)
            if not full:
                with formatter.section("Examples"):
                    indent = " " * formatter.current_indent
                    for command, meaning in _EXAMPLES:
                        if len(command) < _EXAMPLE_COLUMN:
                            formatter.write(f"{indent}{command:<{_EXAMPLE_COLUMN}}{meaning}\n")
                        else:
                            formatter.write(f"{indent}{command}\n{indent}{'':<{_EXAMPLE_COLUMN}}{meaning}\n")
            self.format_options(ctx, formatter)
            self.format_epilog(ctx, formatter)
        finally:
            self.help = short_help
            for param in options:
                param.hidden = hidden[param.name]
                param.help = option_help[param.name]

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] in REVIEW_SUBCOMMANDS:
            ctx.meta["lemoncrow.review_subcommand"] = (args[0], list(args[1:]))
            return []
        given = list(args)  # click consumes `args` as it parses
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as exc:
            # A local `--ai` review is run by hooks, which read the exit code: 1 means a finding is open and 2 an
            # incomplete review, so a command line click cannot parse is 3, the usage-error code of that command.
            if "--ai" in given and not any(arg == "--pr" or arg.startswith("--pr=") for arg in given):
                raise _LocalReviewUsageError(exc.message, ctx=exc.ctx) from exc
            raise

    def invoke(self, ctx: click.Context) -> Any:
        routed = ctx.meta.pop("lemoncrow.review_subcommand", None)
        if routed is None:
            return super().invoke(ctx)
        name, rest = routed
        command = REVIEW_SUBCOMMANDS[name]
        with command.make_context(name, rest, parent=ctx) as sub_ctx:
            return command.invoke(sub_ctx)

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        super().format_epilog(ctx, formatter)
        common = [
            ("status [--json]", "This repository's current Review and whether it is ready (exit 0/1/2/3)."),
            ("hook install", "Git hook: AI review before each push."),
            ("init-action", "Write the GitHub Action workflow that reviews pull requests."),
            ("config --effective", "The review rules this repository sets."),
        ]
        if not ctx.meta.get(_HELP_ALL_KEY):
            with formatter.section("Subcommands"):
                formatter.write_dl(common)
            formatter.write_paragraph()
            formatter.write_text("Run `lc review --help-all` for every option and subcommand.")
            return
        with formatter.section("Subcommands"):
            formatter.write_dl(
                [
                    common[0],
                    ("verify RECORD", "Check a signed record, a file or a record link (exit 0 to 5)."),
                    ("verify-record FILE", "Verify a signed review record offline (exit 0/1/2/3)."),
                    ("record-key init|public", "Opt in to signing local review records with a personal key."),
                    ("data-flow [--base REF] [--json]", "Show where source, code and evidence may travel."),
                    (
                        "config --effective [--dry-run --pr N] [--base REF]",
                        "Show the merged review rules (review.yaml, import.rules_files, AGENTS.md) and what applies.",
                    ),
                    (
                        "config import PATH [--dry-run] [--force]",
                        "Bring another review tool's config over: map its settings, write .lemoncrow/review.yaml, "
                        "print what was carried and dropped.",
                    ),
                    (
                        "hook install|run [--pre-push|--pre-commit]",
                        "Install a git hook that runs `lc review --ai` on what is pushed or committed (--print shows it).",
                    ),
                    (
                        "init-action [--provider P] [--fail-on F] [--chat]",
                        "Write .github/workflows/lemoncrow-review.yml from the setup guide's sample workflow.",
                    ),
                    (
                        "hosted attach|status|queue|sync|resolve|detach",
                        "Attach a local Review to a hosted one; queue offline actions and sync them.",
                    ),
                    ("diagnostics [--include NAME] [--out FILE]", "Export one redacted diagnostic bundle."),
                    (
                        "observation-worker run|check",
                        "Run hosted production observation on this customer machine (check: dry run).",
                    ),
                    ("qa run --url URL [--sha SHA]", "Replay saved browser scenarios against a preview in Chromium."),
                ]
            )


__all__ = ["REVIEW_SUBCOMMANDS", "ReviewCommand", "record_key_group", "verify_cmd", "verify_record_cmd"]
