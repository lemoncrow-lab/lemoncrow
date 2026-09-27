"""``lc context doctor`` -- the compact front door onto the context audit.

``lc audit context`` already answers the right question (what does your
configuration load, and did any of it get used?) but answers it as a wall of
Rich panels aimed at someone tuning a fleet. The product surface people
actually want is four columns and a verdict, so this module reuses the audit's
collection, classification and scoring helpers *verbatim* and replaces only the
renderer. Nothing here re-derives a recommendation; the audit's heuristics stay
the single source of truth and ``lc audit context`` is left untouched.

The one figure the audit never computed is the USED column. The audit records
``session_count`` as 1-or-0 because its own table never showed a rate, so the
per-session breakdown is derived here by re-running the audit's own
``_collect_tool_names``/``_classify_item_calls`` pair once per session -- still
zero new classification logic, just applied at a finer grain.

The first column is called DECLARED, not LOADED. Its value is
``AuditItem.est_context_tokens``, an estimate of *configured* size read off
``.mcp.json`` and ``SKILL.md`` / ``AGENTS.md`` by ``_scan_mcp_servers`` /
``_scan_skills``. Nothing in this pipeline -- or anywhere else in the product --
observes what a host actually put into a context window, so calling the number
"loaded" claimed a measurement that was never taken. Every downstream sentence
is hedged the same way: the flagged total says *declared* tok/turn because it is
a projection of what disabling those sources would stop declaring, not an
observed saving. Same posture as the no-sessions branch's "USED is unknown, not
zero": say what was measured, never more.

Heavy imports stay inside the callback: ``audit`` pulls in ``savings_summary``
and the sessions module, and ``lc --help`` must not pay for that.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from lemoncrow.gateway.cli.commands._shared import _emit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lemoncrow.gateway.cli.commands.audit import AuditItem

# plan 2026-09-07 §8: the audit's three verdicts, said the way a user would act
# on them. Anything unrecognised degrades to "review" rather than raising.
_ACTION_BY_RECOMMENDATION = {
    "KEEP": "keep",
    "CONSIDER": "lazy-load",
    "DISABLE": "disable by default",
}
_ACTION_FALLBACK = "review"
# The audit reaches a verdict from `used_in_any=False` whether that is a
# measurement or an empty window, so a store with no sessions in range produced
# a full column of "disable by default" backed by nothing. A recommendation
# needs an observation; with none, the only honest cell says so.
_ACTION_NO_DATA = "no data in window"
_ACTION_COLOUR = {
    "keep": "green",
    "lazy-load": "yellow",
    "disable by default": "red",
}

# The legend that keeps the first column honest. Rendered in both branches --
# the column is present whether or not any session was observed, so its caveat
# has to be too.
_DECLARED_LEGEND = (
    "DECLARED = estimated tokens this source contributes when configured; not a measurement of host loading."
)

_SOURCE_WIDTH = 30
# Width kept at the old _LOADED_WIDTH so the table still fits: "DECLARED" is
# wider than "LOADED" but the values only grew by the "~" prefix.
_DECLARED_WIDTH = 13
_USED_WIDTH = 8


@click.group("context")
def context_group() -> None:
    """Inspect what your agent context declares, and what your sessions use."""
    # "is actually spending" promised a measured spend on the one surface whose
    # first column is an estimate of *declared* size. The group help is the
    # first sentence a reader meets, so it carries the same hedge as the table.


@context_group.command("doctor", short_help="What your context declares vs. what your sessions use.")
@click.option("--days", default=7, show_default=True, type=int, help="Look-back window in days.")
@click.option(
    "--threshold",
    default=500,
    show_default=True,
    type=int,
    help="Context-token threshold above which unused items are flagged.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON instead of text.")
@click.option("--no-color", is_flag=True, default=False, help="Disable ANSI colour / Rich output.")
@click.pass_context
def context_doctor_cmd(ctx: click.Context, days: int, threshold: int, as_json: bool, no_color: bool) -> None:
    """What your context declares vs. what your sessions actually use.

    Reads the same MCP server configs, skill files and session history as
    `lc audit context` and reaches the same verdicts -- this is the compact
    view of them. DECLARED is the configured size of a source, not a
    measurement of what a host loaded. `--json` emits the audit payload with
    one added key per item, `declared_context_tokens`.
    """

    from datetime import UTC, datetime, timedelta

    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.gateway.cli.commands.audit import (
        _classify_item_calls,
        _collect_tool_names,
        _compute_next_action,
        _compute_recommendation,
        _compute_savings_estimate,
        _scan_mcp_servers,
        _scan_sessions,
        _scan_skills,
    )

    obj = ctx.obj or {}
    root = Path(obj.get("root") or default_store_root())
    workspace = Path(obj.get("workspace") or Path.cwd())
    since = datetime.now(UTC) - timedelta(days=max(1, days))

    items: list[AuditItem] = [*_scan_mcp_servers(root), *_scan_skills(workspace)]
    sessions, total_sessions = _scan_sessions(root, since)
    tool_names = _collect_tool_names(sessions)

    # Identical to audit_context_cmd's cross-reference pass (audit.py:747-767).
    # Kept as a call sequence rather than a copy of the heuristics themselves.
    for item in items:
        total_matches, lemoncrow_calls, routable_calls = _classify_item_calls(tool_names, item)
        item.used = total_matches > 0
        item.use_count = total_matches
        item.lemoncrow_calls = lemoncrow_calls
        item.routable_calls = routable_calls
        item.session_count = 1 if total_matches > 0 else 0
        item.total_sessions = total_sessions
        _compute_savings_estimate(item)
        item.recommendation = _compute_recommendation(item, item.used, item.use_count, total_sessions)
        item.next_action = _compute_next_action(item)

    if as_json:
        _emit(_doctor_payload(items, days=days, total_sessions=total_sessions), as_json=True)
        return

    _emit(_render_doctor_table(items, sessions, threshold=threshold, no_color=no_color, days=days), as_json=False)


def _doctor_payload(items: list[AuditItem], *, days: int, total_sessions: int) -> dict[str, Any]:
    """Reproduce ``audit context --json`` value for value, plus one added alias.

    ``audit_context_cmd`` builds its payload inline (audit.py:770-793) and PR-11
    is not allowed to edit ``audit.py``, so the shape is mirrored here and
    pinned by ``test_context_doctor_json_matches_audit_context`` -- if the audit
    payload ever gains a field, or either surface's numbers move, that test
    fails rather than the two surfaces quietly drifting apart. The sole
    permitted difference is ``declared_context_tokens`` (see ``_item_dict``),
    which that test strips before comparing and then asserts separately.
    """

    recoverable = sum(i.est_context_tokens for i in items if i.recommendation in ("DISABLE", "CONSIDER"))
    return {
        "days": days,
        "session_count": total_sessions,
        "items": [_item_dict(i) for i in sorted(items, key=lambda x: (x.source_type, x.name))],
        "summary": {
            "total": len(items),
            "disable": sum(1 for i in items if i.recommendation == "DISABLE"),
            "consider": sum(1 for i in items if i.recommendation == "CONSIDER"),
            "keep": sum(1 for i in items if i.recommendation == "KEEP"),
            "recoverable_tokens_per_turn": recoverable,
            "total_context_cost_usd": round(float(sum(i.context_cost_usd for i in items)), 6),
            "total_potential_savings_usd": round(float(sum(i.potential_usd_saved for i in items)), 6),
            "total_net_benefit_usd": round(float(sum(i.net_benefit_usd for i in items)), 6),
        },
    }


def _item_dict(item: AuditItem) -> dict[str, Any]:
    """``AuditItem.to_dict()`` with the token figure also carried under its true name.

    ``est_context_tokens`` estimates *declared* size; no stage of the pipeline
    observes host loading. The honest key is added here rather than renamed in
    ``audit.py``, which this module may not touch, and the old key is kept
    alongside it for one release so no consumer breaks silently on upgrade.
    """

    payload = item.to_dict()
    payload["declared_context_tokens"] = payload["est_context_tokens"]
    return payload


def _used_session_pcts(items: list[AuditItem], sessions: list[dict[str, Any]]) -> list[float | None]:
    """Share of observed sessions that made at least one call matching each item.

    ``None`` means "no sessions observed", which is a different statement from
    0 % and must render differently -- an empty look-back window is a gap in the
    evidence, not a finding about the item.
    """

    from lemoncrow.gateway.cli.commands.audit import _classify_item_calls, _collect_tool_names

    if not sessions:
        return [None] * len(items)

    per_session = [_collect_tool_names([session]) for session in sessions]
    total = len(per_session)
    return [
        100.0 * sum(1 for names in per_session if _classify_item_calls(names, item)[0] > 0) / total for item in items
    ]


def _source_label(item: AuditItem) -> str:
    """``github`` -> ``github MCP``; ``k8s`` -> ``k8s skill`` (plan §8's SOURCE column)."""

    if item.source_type == "mcp_server":
        label = f"{item.name} MCP"
    elif item.source_type == "skill":
        label = f"{item.name} skill"
    else:
        label = item.name
    if len(label) > _SOURCE_WIDTH:
        return label[: _SOURCE_WIDTH - 1] + "…"
    return label


def _render_doctor_table(
    items: list[AuditItem],
    sessions: list[dict[str, Any]],
    *,
    threshold: int = 500,
    no_color: bool = False,
    days: int | None = None,
) -> str:
    """The plan §8 four-column form: SOURCE / DECLARED / USED / ACTION.

    Deliberately Rich-free. The whole point of this surface is that it fits in a
    glance and pastes into an issue, which a bordered table does not.

    ACTION is only ever a recommendation when USED is a measurement. A row whose
    usage is unknown reads ``no data in window`` instead, and the summary states
    how many sessions were observed, so the reader can tell "nothing used this"
    from "nothing was looked at".

    DECLARED gets the same treatment one column to the left. The figure is an
    estimate of configured size, so it renders with a ``~`` and carries a legend
    saying it is not a measurement of host loading, and the flagged total is
    phrased as *declared* tok/turn -- a projection of what would stop being
    declared, not an observed saving.
    """

    if not items:
        return (
            "No MCP servers or skills found to audit.\n"
            "Hint: this reads .mcp.json and SKILL.md / AGENTS.md from the workspace -- "
            "run it from a project checkout."
        )

    pcts = _used_session_pcts(items, sessions)
    # Plan §8 lists the healthy sources first and the ones to act on last;
    # unknown usage (no sessions) sorts below a measured 0 %.
    ordered = sorted(
        zip(items, pcts, strict=True),
        key=lambda pair: (-(pair[1] if pair[1] is not None else -1.0), -pair[0].est_context_tokens, pair[0].name),
    )

    lines = [f"{'SOURCE':<{_SOURCE_WIDTH}} {'DECLARED':>{_DECLARED_WIDTH}} {'USED':>{_USED_WIDTH}}    ACTION"]
    for item, pct in ordered:
        declared = f"~{item.est_context_tokens:,} tok"
        used = "—" if pct is None else f"{pct:.0f}%"
        if pct is None:
            action = _ACTION_NO_DATA
        else:
            action = _ACTION_BY_RECOMMENDATION.get(item.recommendation, _ACTION_FALLBACK)
        colour = _ACTION_COLOUR.get(action)
        shown = action if (no_color or colour is None) else click.style(action, fg=colour)
        lines.append(
            f"{_source_label(item):<{_SOURCE_WIDTH}} {declared:>{_DECLARED_WIDTH}} {used:>{_USED_WIDTH}}    {shown}"
        )

    lines.append("")
    if sessions:
        lines.append(f"Observed over {len(sessions)} session{'' if len(sessions) == 1 else 's'}.")
        lines.append("USED = share of those sessions with at least one matching tool call.")
    else:
        window = "the look-back window" if days is None else f"the last {days} day{'' if days == 1 else 's'}"
        lines.append(
            f"0 sessions observed in {window} -- USED is unknown, not zero, " "so no ACTION is recommended for any row."
        )
    lines.append(_DECLARED_LEGEND)

    flagged = [item for item, pct in ordered if pct == 0.0 and item.est_context_tokens >= threshold]
    if flagged:
        wasted = sum(item.est_context_tokens for item in flagged)
        lines.append(
            f"{len(flagged)} unused source{'' if len(flagged) == 1 else 's'} "
            f"≥{threshold:,} declared tok -- {wasted:,} declared tok/turn recoverable if disabled."
        )

    return "\n".join(lines)


__all__ = ["context_doctor_cmd", "context_group"]
