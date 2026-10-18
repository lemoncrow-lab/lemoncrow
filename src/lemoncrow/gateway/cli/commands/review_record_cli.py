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
import sys
from pathlib import Path
from typing import Any

import click

from lemoncrow.gateway.cli.commands.review_data_flow_cli import data_flow_cmd
from lemoncrow.gateway.cli.commands.review_diagnostics_cli import diagnostics_cmd
from lemoncrow.gateway.cli.commands.review_hosted_cli import hosted_group
from lemoncrow.gateway.cli.commands.review_observation_cli import observation_worker_group

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
                f"  Review {summary.get('review_id', '')} Snapshot {summary.get('snapshot_number', '')} "
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
    "verify-record": verify_record_cmd,
    "record-key": record_key_group,
    "data-flow": data_flow_cmd,
    "hosted": hosted_group,
    "diagnostics": diagnostics_cmd,
    "observation-worker": observation_worker_group,
}


class ReviewCommand(click.Command):
    """``lc review`` with a few named subcommands ahead of its optional REV argument.

    A first argument naming a subcommand (``verify-record``, ``record-key``,
    ``data-flow``, ``hosted``, ``diagnostics``, ``observation-worker``) dispatches
    to it; anything else is parsed exactly as before.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] in REVIEW_SUBCOMMANDS:
            ctx.meta["lemoncrow.review_subcommand"] = (args[0], list(args[1:]))
            return []
        return super().parse_args(ctx, args)

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
        with formatter.section("Subcommands"):
            formatter.write_dl(
                [
                    ("verify-record FILE", "Verify a signed review record offline (exit 0/1/2/3)."),
                    ("record-key init|public", "Opt in to signing local review records with a personal key."),
                    ("data-flow [--base REF] [--json]", "Show where source, code and evidence may travel."),
                    (
                        "hosted attach|status|queue|sync|resolve|detach",
                        "Attach a local Review to a hosted one; queue offline actions and sync them.",
                    ),
                    ("diagnostics [--include NAME] [--out FILE]", "Export one redacted diagnostic bundle."),
                    (
                        "observation-worker run|check",
                        "Run hosted production observation on this customer machine (check: dry run).",
                    ),
                ]
            )


__all__ = ["REVIEW_SUBCOMMANDS", "ReviewCommand", "record_key_group", "verify_record_cmd"]
