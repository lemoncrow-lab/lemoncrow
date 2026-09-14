"""`lc usage` and `lc usage optimize` may never disagree about spend.

This is the contract the reviewer's screen broke. `lc usage optimize` printed a
30-day spend of $19,070 while `lc usage --since 30d` printed $11,631 for the
same store and the same window -- a 64% disagreement between two commands one
keystroke apart, with a savings headline computed against the larger of the two.

The two figures came from different models: the usage read model (run ledgers +
traces, priced per row, unknown models deliberately unpriced) versus the savings
ledger's transcript-derived ``spend`` column, which additionally double-counted
any Claude session resumed across date partitions. There is now one source of
truth -- the usage read model -- and these tests hold both surfaces to it.

They are deliberately end-to-end through the CLI: the disagreement was not in
any single function, it was in two commands reading different things, and only
running both can prove that stopped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from click.testing import CliRunner, Result

from lemoncrow.gateway.cli import cli
from tests.helpers import init_store_at

_Call = tuple[str, int, int]
"""``(model, input_tokens, output_tokens)`` for one recorded call."""


def _invoke(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), *args])


def _write_run(
    root: Path,
    *,
    session_id: str,
    host: str = "claude",
    calls: Sequence[_Call] = (),
    days_ago: float = 0.5,
    total_cost_usd: float = 0.0,
) -> Path:
    """Seed one ``run.json`` in the canonical partitioned layout."""

    started = datetime.now(UTC) - timedelta(days=days_ago)
    snapshot: dict[str, Any] = {
        "session_id": session_id,
        "status": "completed",
        "created_at": started.isoformat(),
        "updated_at": started.isoformat(),
        "workspace_path": "/home/dev/alpha",
        "tool_call_count": 0,
        "telemetry": {},
        "cost": {
            "total_cost_usd": total_cost_usd,
            "calls": [
                {
                    "model": model,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                }
                for model, inp, out in calls
            ],
        },
        "events": [],
    }
    target = (
        root / "sessions" / started.strftime("%Y") / started.strftime("%m") / started.strftime("%d") / host / session_id
    )
    target.mkdir(parents=True, exist_ok=True)
    run_path = target / "run.json"
    run_path.write_text(json.dumps(snapshot), encoding="utf-8")
    return run_path


def _seed(root: Path) -> None:
    """A store with priced rows, an unpriced row, and rows across the windows."""

    # `lc usage optimize` reads the history store, so the store has to exist;
    # `lc usage` alone does not, which is why only this file initialises one.
    init_store_at(str(root))
    _write_run(root, session_id="s-today", calls=[("claude-sonnet-4-5", 400_000, 20_000)], days_ago=0.2)
    _write_run(root, session_id="s-week", calls=[("claude-opus-4-1", 200_000, 10_000)], days_ago=3.0)
    _write_run(root, session_id="s-month", calls=[("gpt-4o-mini", 900_000, 40_000)], days_ago=20.0)
    # A model with no rate card: it must be counted as unpriced on BOTH
    # surfaces, never silently priced at zero on one of them.
    _write_run(root, session_id="s-local", calls=[("some-unlisted-model-v9", 50_000, 5_000)], days_ago=10.0)


def _usage_total(root: Path, window: str) -> float:
    result = _invoke(root, "usage", "--since", window, "--json")
    assert result.exit_code == 0, result.output
    return float(json.loads(result.output)["totals"]["total_usd"])


def _optimize_payload(root: Path) -> dict[str, Any]:
    result = _invoke(root, "usage", "optimize", "--json")
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.output)["savings"]
    return payload


def test_optimize_spend_equals_usage_spend_for_every_shared_window(tmp_path: Path) -> None:
    """The contract, stated as plainly as it can be: same window, same number.

    Checked for all three windows the savings breakdown shows, because the
    original bug was not "the 30-day number is wrong" -- it was that nothing
    forced the two surfaces to read the same model at all.
    """

    root = tmp_path / ".lemoncrow"
    _seed(root)

    breakdown = _optimize_payload(root)["summary_breakdown"]

    for key, window in (("1D", "1d"), ("7D", "7d"), ("30D", "30d")):
        bucket = breakdown[key]
        assert bucket["spend_available"] is True, key
        assert bucket["spend"] == round(_usage_total(root, window), 2), key


def test_optimize_json_names_the_source_of_its_spend(tmp_path: Path) -> None:
    """A consumer must be able to see which model produced the number."""

    root = tmp_path / ".lemoncrow"
    _seed(root)

    payload = _optimize_payload(root)
    assert payload["spend_source"] == "usage_read_model"
    windows = payload["spend_windows"]
    assert set(windows) == {"1D", "7D", "30D"}
    for key, days in (("1D", 1), ("7D", 7), ("30D", 30)):
        assert windows[key]["window_days"] == days
        assert windows[key]["source"] == "usage_read_model"
        # The unpriced count travels with the total, so a renderer can qualify it.
        assert windows[key]["unpriced_rows"] >= 0


def test_optimize_carries_the_unpriced_count_usage_reports(tmp_path: Path) -> None:
    """Both surfaces must agree on what the spend figure LEFT OUT, too.

    Agreeing on a total while disagreeing on its coverage would still let one
    screen present a floor as a fact.
    """

    root = tmp_path / ".lemoncrow"
    _seed(root)

    usage = _invoke(root, "usage", "--since", "30d", "--json")
    assert usage.exit_code == 0, usage.output
    usage_totals = json.loads(usage.output)["totals"]

    head = _optimize_payload(root)["summary_breakdown"]["30D"]
    assert head["spend_unpriced_rows"] == usage_totals["unpriced_rows"]
    assert head["spend_rows"] == usage_totals["rows"]
    assert usage_totals["unpriced_rows"] > 0  # the seed really does exercise it


def test_optimize_text_headline_repeats_the_usage_total_verbatim(tmp_path: Path) -> None:
    """The rendered screen, not just the JSON, has to carry the same dollars."""

    root = tmp_path / ".lemoncrow"
    _seed(root)

    usage = _invoke(root, "usage", "--since", "30d", "--no-color")
    assert usage.exit_code == 0, usage.output
    total = re.search(r"TOTAL.*?(\$[\d,]+\.\d{2})", usage.output)
    assert total is not None, usage.output

    optimize = _invoke(root, "usage", "optimize")
    assert optimize.exit_code == 0, optimize.output
    spend_line = next((line for line in optimize.output.splitlines() if line.strip().startswith("Spend")), None)
    assert spend_line is not None, optimize.output
    assert total.group(1) in spend_line, f"{spend_line!r} vs usage TOTAL {total.group(1)}"


def test_optimize_headline_arithmetic_is_closed(tmp_path: Path) -> None:
    """Spend + Saved = Baseline, and Share = Saved / Baseline, on one screen.

    The old headline asserted a ratio between a lifetime figure and a 30-day
    denominator, so no reader could check it and on real data it was false.
    """

    root = tmp_path / ".lemoncrow"
    _seed(root)

    out = _invoke(root, "usage", "optimize").output

    def _money(label: str) -> float:
        match = re.search(rf"{label}\s+\$([\d,]+\.\d{{2}})", out)
        assert match is not None, f"{label} missing from:\n{out}"
        return float(match.group(1).replace(",", ""))

    spend = _money("Spend")
    saved = _money(r"Saved \(est\.\)")
    baseline = _money("Baseline")
    assert abs(baseline - (spend + saved)) < 0.01

    share = re.search(r"Share\s+([\d.]+)%", out)
    assert share is not None, out
    expected = saved / baseline * 100 if baseline else 0.0
    assert abs(float(share.group(1)) - expected) < 0.05


def test_optimize_never_prints_a_lifetime_figure_as_a_windowed_ratio(tmp_path: Path) -> None:
    """Regression on the exact shape of the reviewer's line.

    ``Saved $X (P% of $S spend · 30d)`` mixed a lifetime numerator into a
    30-day ratio. The lifetime total is still shown -- it is real -- but only on
    its own line and only labelled as lifetime.
    """

    root = tmp_path / ".lemoncrow"
    _seed(root)

    out = _invoke(root, "usage", "optimize").output
    assert "spend · 30d)" not in out
    assert not re.search(r"Saved\s+\$[\d,.]+\s+\(\d", out)
    if "Lifetime saved" in out:
        assert "Lifetime saved (all history, modelled)" in out
