"""Compare token usage across mitmproxy captures (e.g. Atelier on vs off).

Reads one or more mitmproxy ``.flow`` files, pulls the token usage out of each
model response (Bedrock or Anthropic-direct), aggregates per capture, and
prints a comparison table with a token->USD translation.

No Anthropic API key needed -- capture works with a Bedrock key or a Claude
Pro/Max subscription.

    # terminal A -- start the proxy, one capture file per run:
    mitmdump -w atelier_off.flow
    # terminal B -- route Claude Code through it, do ONE task, then quit:
    HTTPS_PROXY=http://127.0.0.1:8080 \\
        NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem claude
    # repeat for atelier_on.flow with the Atelier MCP server enabled, then:

    uv run python -m benchmarks.flowlib.report \\
        atelier_off=atelier_off.flow atelier_on=atelier_on.flow

List the baseline capture FIRST: the delta row is computed as
``second vs first``, so a negative percentage means the candidate saved.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from benchmarks.flowlib.usage_parser import Usage, extract_usage

logger = logging.getLogger(__name__)

# Model pricing -- USD per 1M tokens. Defaults: Claude Sonnet 4.6 on Amazon
# Bedrock (2025). Override per-run with --in / --out / --cache-read / --cache-write.
_DEFAULT_PRICING: dict[str, float] = {
    "input_per_m": 3.00,
    "output_per_m": 15.00,
    "cache_read_per_m": 0.30,  # ~10% of input
    "cache_write_per_m": 3.75,  # ~125% of input (5-minute write)
}

# Only responses to these hosts are counted (skips telemetry/other traffic).
_MODEL_HOST_HINTS = ("bedrock-runtime", "anthropic")


@dataclass
class RunStats:
    label: str
    usage: Usage = field(default_factory=Usage)
    requests: int = 0

    def add(self, u: Usage) -> None:
        self.usage += u
        self.requests += 1

    def cost_usd(self, pricing: dict[str, float]) -> float:
        u = self.usage
        return (
            u.input_tokens * pricing["input_per_m"]
            + u.output_tokens * pricing["output_per_m"]
            + u.cache_read_input_tokens * pricing["cache_read_per_m"]
            + u.cache_creation_input_tokens * pricing["cache_write_per_m"]
        ) / 1_000_000

    def cache_read_ratio(self) -> float:
        ti = self.usage.total_input
        return self.usage.cache_read_input_tokens / ti if ti else 0.0


def aggregate(label: str, records: Iterable[tuple[str, bytes]]) -> RunStats:
    """Aggregate usage from ``(content_type, body)`` records into one RunStats."""
    stats = RunStats(label=label)
    for content_type, body in records:
        u = extract_usage(content_type, body)
        if not u.is_empty():
            stats.add(u)
    return stats


def flow_records(path: str) -> Iterator[tuple[str, bytes]]:
    """Yield ``(content_type, body)`` for each model response in a .flow file."""
    try:
        from mitmproxy.io import FlowReader
    except ImportError as exc:  # pragma: no cover - optional dependency
        # Re-raise as ImportError (not SystemExit) so callers' broad excepts can
        # degrade to receipt-only usage instead of aborting an already-finished
        # run on a transient venv re-sync race.
        raise ImportError(
            "mitmproxy is required to read .flow files. Install it with:\n    uv pip install mitmproxy"
        ) from exc

    with open(path, "rb") as fh:
        try:
            flows = FlowReader(fh).stream()
        except Exception:
            logger.warning("flow_records: unable to read %s (corrupted at prefix)", path, exc_info=True)
            return
        while True:
            try:
                flow = next(flows)
            except StopIteration:
                return
            except Exception:
                logger.warning(
                    "flow_records: corrupted entry in %s — yielded entries retained, can't read further",
                    path,
                )
                return
            req = getattr(flow, "request", None)
            resp = getattr(flow, "response", None)
            if req is None or resp is None:
                continue
            host = (req.pretty_host or "").lower()
            if not any(hint in host for hint in _MODEL_HOST_HINTS):
                continue
            content_type = resp.headers.get("content-type", "")
            try:
                body = resp.content
            except ValueError:
                body = resp.raw_content
            if body:
                yield content_type, body


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _signed_pct(base: float, cand: float) -> str:
    if base == 0:
        return "n/a"
    return f"{(cand - base) / base * 100:+.1f}%"


def format_report(runs: list[RunStats], pricing: dict[str, float]) -> str:
    labels = [r.label for r in runs]
    rows: list[tuple[str, list[str]]] = [
        ("requests", [_fmt_int(r.requests) for r in runs]),
        ("input (non-cached)", [_fmt_int(r.usage.input_tokens) for r in runs]),
        ("cache read", [_fmt_int(r.usage.cache_read_input_tokens) for r in runs]),
        ("cache write", [_fmt_int(r.usage.cache_creation_input_tokens) for r in runs]),
        ("output", [_fmt_int(r.usage.output_tokens) for r in runs]),
        ("total input", [_fmt_int(r.usage.total_input) for r in runs]),
        ("total tokens", [_fmt_int(r.usage.total) for r in runs]),
        ("cache-read ratio", [_pct(r.cache_read_ratio()) for r in runs]),
        ("est. cost (USD)", [f"${r.cost_usd(pricing):.4f}" for r in runs]),
    ]

    metric_w = max([len(m) for m, _ in rows] + [len("metric")])
    col_w = [max([len(lab)] + [len(vals[i]) for _, vals in rows]) for i, lab in enumerate(labels)]

    def line(metric: str, vals: list[str]) -> str:
        cells = "  ".join(v.rjust(col_w[i]) for i, v in enumerate(vals))
        return f"{metric.ljust(metric_w)}  {cells}"

    out = [
        line("metric", labels),
        line("-" * metric_w, ["-" * w for w in col_w]),
        *[line(m, vals) for m, vals in rows],
    ]

    if len(runs) == 2:
        base, cand = runs
        tok_delta = cand.usage.total - base.usage.total
        cost_delta = cand.cost_usd(pricing) - base.cost_usd(pricing)
        out += [
            "",
            f"delta ({cand.label} vs {base.label}; negative = saved):",
            f"  total tokens : {tok_delta:+,} ({_signed_pct(base.usage.total, cand.usage.total)})",
            f"  est. cost    : ${cost_delta:+.4f} ({_signed_pct(base.cost_usd(pricing), cand.cost_usd(pricing))})",
        ]
    return "\n".join(out)


def _parse_args(argv: list[str]) -> tuple[list[tuple[str, str]], dict[str, float]]:
    import argparse

    p = argparse.ArgumentParser(
        prog="benchmarks.flowlib.report",
        description="Compare token usage across mitmproxy .flow captures.",
    )
    p.add_argument(
        "captures",
        nargs="+",
        metavar="LABEL=PATH",
        help="captures to compare, e.g. atelier_off=off.flow atelier_on=on.flow",
    )
    p.add_argument("--in", dest="input_per_m", type=float, default=_DEFAULT_PRICING["input_per_m"])
    p.add_argument("--out", dest="output_per_m", type=float, default=_DEFAULT_PRICING["output_per_m"])
    p.add_argument(
        "--cache-read",
        dest="cache_read_per_m",
        type=float,
        default=_DEFAULT_PRICING["cache_read_per_m"],
    )
    p.add_argument(
        "--cache-write",
        dest="cache_write_per_m",
        type=float,
        default=_DEFAULT_PRICING["cache_write_per_m"],
    )
    ns = p.parse_args(argv)

    captures: list[tuple[str, str]] = []
    for tok in ns.captures:
        if "=" in tok:
            label, path = tok.split("=", 1)
        else:
            label = tok.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            path = tok
        captures.append((label, path))
    pricing = {
        "input_per_m": ns.input_per_m,
        "output_per_m": ns.output_per_m,
        "cache_read_per_m": ns.cache_read_per_m,
        "cache_write_per_m": ns.cache_write_per_m,
    }
    return captures, pricing


def main(argv: list[str] | None = None) -> int:
    captures, pricing = _parse_args(sys.argv[1:] if argv is None else argv)
    runs = [aggregate(label, flow_records(path)) for label, path in captures]
    print(format_report(runs, pricing))
    if all(r.usage.is_empty() for r in runs):
        print(
            "\n[warn] No model usage found. Check that traffic was proxied and "
            "that the host matches one of: " + ", ".join(_MODEL_HOST_HINTS),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
