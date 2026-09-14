"""``lc context doctor``: the compact view, and the promise that it is only a view.

The risk in this PR is not the four columns -- it is drift. ``context doctor``
and ``audit context`` must keep reaching the same verdicts from the same data,
so the strongest test here is not the table but
``test_context_doctor_json_matches_audit_context``: byte-equal payloads from the
two commands over one seeded store. If someone ever "improves" a heuristic in
one of the two front doors, that test fails before the table tests do.

Everything is seeded into a tmp store with MCP discovery stubbed, because the
real ``discover_mcp_configs`` reads the developer's ``~/.claude.json`` and the
skill scanner reads the cwd -- neither is a fixture.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from lemoncrow.core.capabilities import mcp_integration
from lemoncrow.core.capabilities.mcp_integration.loader import MCPServerConfig
from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.app import MCP_TOOL_ONLY_COMMANDS, MCP_TOOL_ONLY_GROUPS
from lemoncrow.gateway.cli.commands.audit import AuditItem, audit_context_cmd
from lemoncrow.gateway.cli.commands.context_doctor import (
    _item_dict,
    _render_doctor_table,
    _used_session_pcts,
    context_doctor_cmd,
    context_group,
)

# ----- fixtures ------------------------------------------------------------ #


def _seed_session(root: Path, session_id: str, tools: dict[str, int]) -> None:
    session = root / "sessions" / session_id
    session.mkdir(parents=True, exist_ok=True)
    (session / "stats.json").write_text(json.dumps({"tools_used": tools}), encoding="utf-8")


def _seed_dated_session(root: Path, session_id: str, tools: dict[str, int]) -> None:
    """The layout a real store actually uses: ``sessions/YYYY/MM/DD/<host>/<id>/``.

    Every session written by :func:`session_dir` lands here. Seeding only the
    flat ``sessions/<id>/`` shape above is what let the scanner's single glob
    look correct in tests while finding nothing at all on disk.
    """

    today = datetime.now(UTC)
    session = (
        root / "sessions" / today.strftime("%Y") / today.strftime("%m") / today.strftime("%d") / "claude" / session_id
    )
    session.mkdir(parents=True, exist_ok=True)
    (session / "stats.json").write_text(json.dumps({"tools_used": tools}), encoding="utf-8")


def _seed_skill(workspace: Path, name: str, chars: int) -> None:
    skill = workspace / ".agents" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("x" * chars, encoding="utf-8")


def _stub_mcp(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    configs = [MCPServerConfig(name=name, command=f"{name}-server") for name in names]
    monkeypatch.setattr(mcp_integration, "discover_mcp_configs", lambda *a, **k: list(configs))


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """An empty store + an empty workspace that is also the cwd.

    ``_discover_skill_files`` scans ``Path.cwd()`` in addition to the workspace,
    so the chdir is load-bearing: without it every test would also audit this
    repository's own ``integrations/*/skills``.
    """

    root = tmp_path / "store"
    (root / "sessions").mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _stub_mcp(monkeypatch)
    return root, workspace


def _populate(root: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan §8 scenario: one healthy server, one barely-used one, one dead skill.

    20 sessions is not arbitrary -- ``_compute_recommendation`` calls an item
    CONSIDER below a 10 % call-to-session ratio, so postgres needs a window wide
    enough for a single call to land under it.
    """

    _stub_mcp(monkeypatch, "github", "postgres")
    for i in range(16):
        _seed_session(root, f"used-{i}", {"mcp__github__search": 5})
    _seed_session(root, "postgres-once", {"mcp__postgres__query": 1})
    for i in range(3):
        _seed_session(root, f"idle-{i}", {})
    _seed_skill(workspace, "kubernetes", chars=8_400)  # 2,100 est tokens, never called


def _obj(root: Path, workspace: Path) -> dict[str, Any]:
    return {"root": root, "workspace": workspace}


def _ctx() -> click.Context:
    return click.Context(cli, info_name="lc")


# ----- registration -------------------------------------------------------- #


def test_context_group_registered_and_resolvable() -> None:
    ctx = _ctx()
    assert "context" in cli.list_commands(ctx)
    live = cli.get_command(ctx, "context")
    assert isinstance(live, click.Group)
    assert live is context_group

    result = CliRunner().invoke(cli, ["context", "doctor", "--help"])
    assert result.exit_code == 0, result.output
    assert "--days" in result.output
    assert "--threshold" in result.output
    assert "--json" in result.output


def test_context_name_moved_from_mcp_only_commands_to_groups() -> None:
    """The collision fix itself: ``context`` is a suppressed *group* name now.

    Leaving it in COMMANDS would have been silently fine (nothing registers
    ``@_dev_command("context")``) right up until someone added one, at which
    point the live group and a dev command would fight over the name.
    """

    assert "context" not in MCP_TOOL_ONLY_COMMANDS
    assert "context" in MCP_TOOL_ONLY_GROUPS


# ----- the compact table --------------------------------------------------- #


def test_context_doctor_compact_table_columns(isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = isolated
    _populate(root, workspace, monkeypatch)

    result = CliRunner().invoke(context_doctor_cmd, ["--days", "7"], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output

    header = result.output.splitlines()[0]
    assert "SOURCE" in header
    assert "DECLARED" in header
    assert "USED" in header
    assert "ACTION" in header


def test_context_doctor_used_column_is_share_of_sessions(
    isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """USED is per-session, not per-call -- 16 of 20 sessions is 80 %."""

    root, workspace = isolated
    _populate(root, workspace, monkeypatch)

    result = CliRunner().invoke(context_doctor_cmd, [], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output
    rows = {line.split()[0]: line for line in result.output.splitlines() if " tok " in line}

    assert "80%" in rows["github"]
    assert "5%" in rows["postgres"]
    assert "0%" in rows["kubernetes"]
    assert "Observed over 20 sessions." in result.output


def test_context_doctor_action_column_maps_recommendations(
    isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, workspace = isolated
    _populate(root, workspace, monkeypatch)

    result = CliRunner().invoke(context_doctor_cmd, ["--no-color"], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output
    rows = {line.split()[0]: line for line in result.output.splitlines() if " tok " in line}

    assert rows["github"].endswith("keep")
    assert rows["postgres"].endswith("lazy-load")
    assert rows["kubernetes"].endswith("disable by default")


def test_context_doctor_flags_unused_items_over_threshold(
    isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, workspace = isolated
    _populate(root, workspace, monkeypatch)

    flagged = CliRunner().invoke(context_doctor_cmd, ["--threshold", "500"], obj=_obj(root, workspace))
    assert flagged.exit_code == 0, flagged.output
    assert "1 unused source ≥500 declared tok" in flagged.output
    assert "2,100 declared tok/turn recoverable" in flagged.output

    # Raise the bar above the dead skill and the advisory disappears entirely.
    quiet = CliRunner().invoke(context_doctor_cmd, ["--threshold", "9000"], obj=_obj(root, workspace))
    assert quiet.exit_code == 0, quiet.output
    assert "recoverable" not in quiet.output


def test_declared_column_replaces_the_loaded_claim(
    isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported defect: ``LOADED`` named a measurement nothing in the pipeline takes.

    ``est_context_tokens`` is derived by reading ``.mcp.json`` and ``SKILL.md``
    off disk. That is what the configuration *declares*; no stage of the product
    observes what a host actually placed in a context window. Three things have
    to say so together -- the header, the ``~`` on each value, and a legend --
    because any one of them alone still lets the column read as a reading off an
    instrument.
    """

    root, workspace = isolated
    _populate(root, workspace, monkeypatch)

    result = CliRunner().invoke(context_doctor_cmd, ["--no-color"], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output

    header = result.output.splitlines()[0]
    assert "DECLARED" in header
    assert "LOADED" not in result.output

    # Table body only -- the blank line ends it, so the trailing legend and
    # advisory lines cannot be mistaken for rows.
    body = result.output.splitlines()
    rows = body[1 : body.index("")]
    assert len(rows) == 3, result.output
    # Column slice, not a substring search: the "~" has to be on the number
    # itself, not somewhere else on the line.
    assert all(row[31:44].strip().startswith("~") for row in rows), result.output
    assert "~2,100 tok" in result.output

    assert (
        "DECLARED = estimated tokens this source contributes when configured; "
        "not a measurement of host loading." in result.output
    )


def test_declared_legend_survives_an_empty_window(isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """The column renders with zero observations, so its caveat has to render too.

    The empty-window branch prints its own sentence about USED and previously
    returned before saying anything about the first column -- exactly the case
    where an unhedged token figure is most likely to be read as a fact.
    """

    root, workspace = isolated
    _stub_mcp(monkeypatch, "github")
    _seed_skill(workspace, "kubernetes", chars=8_400)

    result = CliRunner().invoke(context_doctor_cmd, ["--days", "7", "--no-color"], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output
    assert "not a measurement of host loading." in result.output
    assert "~2,100 tok" in result.output
    assert "LOADED" not in result.output
    # The pre-existing posture this fix is modelled on stays exactly as it was.
    assert "USED is unknown, not zero" in result.output
    assert "no data in window" in result.output


def test_recoverable_total_is_declared_and_conditional(
    isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``N tok/turn recoverable`` asserted a saving nobody measured.

    The figure is a sum of declared sizes for rows that went unused in the
    window; it becomes real only if the reader disables them. Both halves of
    that -- *declared*, and *if disabled* -- are now in the sentence, and the
    bare form must be gone rather than merely supplemented.
    """

    root, workspace = isolated
    _populate(root, workspace, monkeypatch)

    result = CliRunner().invoke(context_doctor_cmd, ["--threshold", "500"], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output
    assert "2,100 declared tok/turn recoverable if disabled." in result.output
    # The unhedged claim, verbatim as it used to render.
    assert "2,100 tok/turn recoverable." not in result.output


def test_help_text_never_promises_a_measurement() -> None:
    """Help is the first sentence a reader meets, so it hedges the way the table does.

    The group help read "what your agent context is actually spending" -- a
    measured spend, on the one surface whose leading column is an estimate of
    *declared* size. Both help strings now say ``declares``, and neither may
    reintroduce the LOADED framing. Assertions run over whitespace-normalised
    output because Click rewraps docstrings to the terminal width.
    """

    group_help = CliRunner().invoke(context_group, ["--help"])
    assert group_help.exit_code == 0, group_help.output
    group_text = " ".join(group_help.output.split())
    assert "Inspect what your agent context declares, and what your sessions use." in group_text
    assert "actually spending" not in group_text
    # The subcommand's short help renders inside the group listing.
    assert "What your context declares vs. what your sessions use." in group_text

    cmd_help = CliRunner().invoke(context_doctor_cmd, ["--help"])
    assert cmd_help.exit_code == 0, cmd_help.output
    cmd_text = " ".join(cmd_help.output.split())
    assert "What your context declares vs. what your sessions actually use." in cmd_text
    assert "DECLARED is the configured size of a source, not a measurement of what a host loaded." in cmd_text
    assert "LOADED" not in cmd_help.output


def test_context_doctor_reports_unknown_usage_not_zero(
    isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sessions in the window is a gap in the evidence, not a 0 % finding."""

    root, workspace = isolated
    _stub_mcp(monkeypatch, "github")

    result = CliRunner().invoke(context_doctor_cmd, [], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output
    assert "USED is unknown, not zero" in result.output
    assert "0%" not in result.output


def test_no_observations_means_no_recommendation(isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """The reported defect: every row USED = "—", every row still "disable by default".

    ``_compute_recommendation`` reaches DISABLE from ``used_in_any=False``, and an
    empty look-back window makes that false for every item -- so a store the
    command never managed to read produced a full column of confident advice.
    An unmeasured row has to say it is unmeasured, and the summary has to state
    the observation count that the advice would have rested on.
    """

    root, workspace = isolated
    _stub_mcp(monkeypatch, "github", "postgres")
    _seed_skill(workspace, "kubernetes", chars=8_400)

    result = CliRunner().invoke(context_doctor_cmd, ["--days", "7", "--no-color"], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output

    rows = [line for line in result.output.splitlines() if " tok " in line]
    assert rows, result.output
    assert all(row.endswith("no data in window") for row in rows), result.output
    assert "disable by default" not in result.output
    assert "lazy-load" not in result.output
    assert "0 sessions observed in the last 7 days" in result.output


def test_used_column_reads_the_dated_session_layout(
    isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why USED was empty at all: the scan globbed one layout and the store uses another.

    On a real store (2,134 session files, 83 of them inside a 7-day window) the
    scanner returned zero sessions, because it globbed ``sessions/*/stats.json``
    while every session since :func:`session_dir` is written to
    ``sessions/YYYY/MM/DD/<host>/<id>/``. The old fixtures seeded the flat shape,
    so the bug was invisible to the suite.
    """

    root, workspace = isolated
    _stub_mcp(monkeypatch, "github")
    for i in range(4):
        _seed_dated_session(root, f"dated-{i}", {"mcp__github__search": 3})

    result = CliRunner().invoke(context_doctor_cmd, ["--days", "7", "--no-color"], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output
    assert "Observed over 4 sessions." in result.output
    row = next(line for line in result.output.splitlines() if line.startswith("github"))
    assert "100%" in row
    assert row.endswith("keep")


def test_both_session_layouts_are_counted_together(isolated: tuple[Path, Path]) -> None:
    """The legacy flat layout still counts: the fix adds a glob, it does not swap one."""

    from lemoncrow.gateway.cli.commands.audit import _scan_sessions

    root, _workspace = isolated
    _seed_session(root, "flat-one", {"mcp__github__search": 1})
    _seed_dated_session(root, "dated-one", {"mcp__github__search": 1})

    sessions, total = _scan_sessions(root, datetime.now(UTC) - timedelta(days=7))

    assert total == 2
    assert len(sessions) == 2


# ----- degraded paths ------------------------------------------------------- #


def test_context_doctor_empty_store_exits_zero(isolated: tuple[Path, Path]) -> None:
    root, workspace = isolated
    result = CliRunner().invoke(context_doctor_cmd, [], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output
    assert "No MCP servers or skills found to audit." in result.output
    assert "Hint:" in result.output


def test_render_survives_an_unrecognised_recommendation() -> None:
    """An unmapped verdict degrades to a value; it must never raise.

    One observed session is part of the fixture, not decoration: the ACTION
    column only carries a verdict at all once the window has something in it.
    """

    item = AuditItem(
        name="mystery",
        source_type="mcp_server",
        source_path="mystery-server",
        est_context_tokens=900,
        recommendation="SOMETHING_NEW",
    )
    text = _render_doctor_table([item], [{"tools_used": {"mcp__mystery__go": 1}}])
    assert "review" in text
    assert "~900 tok" in text


def test_long_source_names_stay_inside_the_column() -> None:
    item = AuditItem(
        name="integ/antigravity/an-extremely-long-skill-name",
        source_type="skill",
        source_path="x",
        est_context_tokens=100,
        recommendation="KEEP",
    )
    row = _render_doctor_table([item], []).splitlines()[1]
    assert row.startswith("integ/antigravity/an-extrem")
    assert "…" in row
    assert row.index("~100 tok") >= 30


def test_used_pcts_are_none_without_sessions() -> None:
    item = AuditItem(name="github", source_type="mcp_server", source_path="x")
    assert _used_session_pcts([item], []) == [None]


# ----- JSON: the same payload as the audit --------------------------------- #


def test_context_doctor_json_shape(isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = isolated
    _populate(root, workspace, monkeypatch)

    result = CliRunner().invoke(context_doctor_cmd, ["--json"], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert set(payload) == {"days", "session_count", "items", "summary"}
    assert payload["session_count"] == 20
    assert {i["name"] for i in payload["items"]} == {"github", "postgres", "kubernetes"}
    assert set(payload["summary"]) == {
        "total",
        "disable",
        "consider",
        "keep",
        "recoverable_tokens_per_turn",
        "total_context_cost_usd",
        "total_potential_savings_usd",
        "total_net_benefit_usd",
    }


def test_context_doctor_json_matches_audit_context(
    isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drift guard. Two front doors, one payload -- plus one declared alias.

    ``context doctor`` adds ``declared_context_tokens`` to each item because
    ``est_context_tokens`` names a measurement nobody takes, and the honest name
    cannot be introduced in ``audit.py`` -- the module this whole surface exists
    to leave untouched. That alias is the *only* difference the guard tolerates:
    it is popped, its value is pinned to the key it renames, and everything else
    still has to match value for value.
    """

    root, workspace = isolated
    _populate(root, workspace, monkeypatch)

    doctor = CliRunner().invoke(context_doctor_cmd, ["--json", "--days", "7"], obj=_obj(root, workspace))
    audit = CliRunner().invoke(audit_context_cmd, ["--json", "--days", "7"], obj=_obj(root, workspace))
    assert doctor.exit_code == 0, doctor.output
    assert audit.exit_code == 0, audit.output

    doctor_payload = json.loads(doctor.output)
    audit_payload = json.loads(audit.output)

    assert len(doctor_payload["items"]) == 3, doctor_payload
    for item in doctor_payload["items"]:
        assert item.pop("declared_context_tokens") == item["est_context_tokens"]

    assert doctor_payload == audit_payload


def test_json_items_carry_the_declared_key_and_keep_the_old_one(
    isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename with a deliberate one-release overlap.

    Dropping ``est_context_tokens`` outright would break every existing consumer
    silently -- a JSON key vanishing raises nothing, it just reads as zero. Both
    names ship for one release; only then does the old one go.
    """

    root, workspace = isolated
    _populate(root, workspace, monkeypatch)

    result = CliRunner().invoke(context_doctor_cmd, ["--json"], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["items"]

    assert items
    for item in items:
        assert item["declared_context_tokens"] == item["est_context_tokens"]
    assert {i["name"]: i["declared_context_tokens"] for i in items}["kubernetes"] == 2_100


def test_item_dict_only_adds_and_never_mutates() -> None:
    """The alias is a wrapper, not an edit to ``AuditItem`` -- ``audit.py`` stays untouched."""

    item = AuditItem(name="github", source_type="mcp_server", source_path="x", est_context_tokens=1_234)
    plain = item.to_dict()

    enriched = _item_dict(item)

    assert set(enriched) - set(plain) == {"declared_context_tokens"}
    assert enriched["declared_context_tokens"] == 1_234
    assert item.to_dict() == plain


def test_context_doctor_json_still_reports_the_window_when_empty(isolated: tuple[Path, Path]) -> None:
    """Deliberate divergence from ``audit context``'s empty payload.

    ``audit_context_cmd`` drops ``days``/``session_count`` when it finds nothing
    to audit, so its JSON has two shapes. The doctor always emits four keys --
    a consumer should not have to branch on emptiness.
    """

    root, workspace = isolated
    result = CliRunner().invoke(context_doctor_cmd, ["--json", "--days", "3"], obj=_obj(root, workspace))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"days", "session_count", "items", "summary"}
    assert payload["days"] == 3
    assert payload["items"] == []


# ----- `audit context` is untouched ---------------------------------------- #


def test_audit_context_unchanged(isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = isolated
    _populate(root, workspace, monkeypatch)

    result = CliRunner().invoke(cli, ["--root", str(root), "audit", "context", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"days", "session_count", "items", "summary"}

    text = CliRunner().invoke(cli, ["--root", str(root), "audit", "context", "--no-color"])
    assert text.exit_code == 0, text.output
    # The audit keeps its own wide table -- the compact header is doctor-only.
    assert "Name" in text.output
    assert "SOURCE" not in text.output
