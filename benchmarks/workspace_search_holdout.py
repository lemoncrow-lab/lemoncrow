"""Frozen second set of manually labeled cross-repository routing examples.

This set is intentionally separate from workspace_search_relevance.CASES,
and no planner weights should be tuned against the outcome. The project owner
labels are approximate and refer to repository scope, not proof of a code hit.
Read-only: scans only workspace Git membership and existing bounded READMEs.
Usage: PYTHONPATH=client/src python benchmarks/workspace_search_holdout.py --root ~/Projects/Olaryn
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from lemoncrow_client.lite.workspace_search_plan import choose_first_wave, hints
from lemoncrow_client.workspace_members import discover_members

# These prompts were derived from project roles and existing documentation,
# not from the implementation's ranking results.
HOLDOUT: tuple[tuple[str, str], ...] = (
    ("ACSS", "small internal software cloud deployment"),
    ("ACSS", "bespoke internal tools hosting enterprise"),
    ("Analytics", "self hosted Umami web analytics"),
    ("Analytics", "cookie free website traffic measurement"),
    ("Authward", "Better Auth identity provider"),
    ("Authward", "OAuth OIDC provider authorization"),
    ("Authward", "passkey and account switching authentication"),
    ("Benchmarks", "historical code benchmark results datasets"),
    ("Benchmarks", "evaluation artefacts SWE bench"),
    ("Beseam", "Shopify storefront product intelligence"),
    ("Beseam", "AI product visibility crawling probes"),
    ("Beseam", "Temporal workers ecommerce backend"),
    ("Beseam/landings/beseam-fit-lp", "create react app legacy fit landing"),
    ("Beseam/landings/beseam-site", "beseam store health marketing homepage"),
    ("Beseam/landings/beseam-site", "AI visibility Shopify landing site SEO"),
    ("Beseam/pipelines/fit-data/catalog", "footwear sizing charts catalog data"),
    ("Beseam/pipelines/fit-data/catalog", "merchant size chart data repository"),
    ("Billing", "Stripe payment subscriptions integration"),
    ("Billing", "commerce plans coupons customer history"),
    ("Billing", "revenue billing invoices checkout"),
    ("CI-Lifeboat", "self hosted runner when GitHub actions billing fails"),
    ("CI-Lifeboat", "CI fallback controller runner quotas"),
    ("Console", "cloudflare operator dashboard"),
    ("Console", "admin operations application monitoring"),
    ("Design-System", "shared UI design tokens typography radius"),
    ("Design-System", "centralized semantic colors components"),
    ("LemonCrow", "MCP server code intelligence"),
    ("LemonCrow", "review reader change risk analysis"),
    ("LemonCrow", "agent worktree branch orchestration"),
    ("LemonCrow/landing", "Astro documentation marketing landing"),
    ("LemonCrow/landing", "open core runtime engineering docs website"),
    ("Olaryn-Website", "Cloudflare worker for olaryn dot com"),
    ("Olaryn-Website", "holding company static homepage"),
    ("Secrets", "OpenBao secret engine and access control"),
    ("Secrets", "GCP KMS auto unseal PostgreSQL Raft migration"),
    ("Secrets", "vault backup rotation disaster recovery"),
    ("Trevra", "agent operated lead qualification and outbound"),
    ("Trevra", "GTM approval gated campaigns and account research"),
    ("Trevra", "opportunity revenue operations evidence ledger"),
    ("Foot-Measure-Artifacts/foot-measure-data", "foot measuring evaluation dataset images"),
    ("Foot-Measure-Artifacts/foot-measure-data", "raw foot measure research data files"),
    ("Foot-Measure-Artifacts/foot-measure-model", "fine tuned foot measurement model weights"),
    ("Foot-Measure-Artifacts/foot-measure-model", "ultralytics FastSAM checkpoint registry"),
)


def evaluate(root: Path, *, cap: int = 8) -> dict[str, object]:
    members = discover_members(root)
    if members.truncated:
        raise ValueError("discovery truncated: ranking claims not meaningful")
    actual = {item.relpath for item in members.members}
    unavailable = {owner for owner, _ in HOLDOUT} - actual
    if unavailable:
        raise ValueError(f"missing expected repositories: {sorted(unavailable)}")
    base_hits = 0
    planned_hits = 0
    observations = []
    elapsed_ms = []
    for expected, query in HOLDOUT:
        baseline = tuple(item.repo.relpath for item in hints(query, members.members)[:cap])
        started = time.perf_counter()
        planned = choose_first_wave(query, members.members, cap=cap)
        elapsed_ms.append((time.perf_counter() - started) * 1000)
        base_hits += expected in baseline
        planned_hits += expected in planned
        observations.append(
            {"owner": expected, "query": query, "base": expected in baseline, "planned": expected in planned}
        )
    return {
        "repositories": len(members.members),
        "cases": len(HOLDOUT),
        "top_k": cap,
        "baseline_hit_at_k": base_hits,
        "planned_hit_at_k": planned_hits,
        "median_planning_ms": round(statistics.median(elapsed_ms), 3),
        "p95_planning_ms": round(sorted(elapsed_ms)[int(0.95 * (len(elapsed_ms) - 1))], 3),
        "misses": [row for row in observations if not row["planned"]],
        "observations": observations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cap", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.cap <= 64:
        parser.error("cap must be 1..64")
    print(json.dumps(evaluate(args.root.resolve(), cap=args.cap), indent=2))


if __name__ == "__main__":
    main()
