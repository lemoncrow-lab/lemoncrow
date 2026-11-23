from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Never leave a scripts/__pycache__ behind: it would show up as an untracked change.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_scenarios  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# The frontend has five routes and the API six requests; this change touches
# exactly three of each, with deliberately different sizes, so Review lists
# only the affected pages and requests and ranks them by how much they changed.
REPLACEMENTS = [
    (
        "README.md",
        "A compact billing and collaboration application used to exercise LemonCrow Review's built-in product surfaces and runtimes.",
        "A compact billing and collaboration application used by LemonCrow Cloud across product surfaces and runtimes.",
    ),
    (
        "docs/architecture.md",
        "The product has two application processes and two review-only verification paths.",
        "The product has two application processes, two review-only verification paths, and one intentionally shared runtime model.",
    ),
    # Frontend, large: /pricing features the Team plan; its price change and the new plan come from
    # GET /api/plans (data/plans.csv below). A static build serves public/api/plans in its place.
    (
        "frontend/src/pages/PricingPage.jsx",
        "const CARD_STYLE = { Team: { featured: false } };",
        "const CARD_STYLE = { Team: { featured: true } };",
    ),
    (
        "frontend/public/api/plans",
        '    { "name": "Team", "monthly": 49, "seats": 25 },\n'
        '    { "name": "Business", "monthly": 199, "seats": 100 }\n',
        '    { "name": "Team", "monthly": 59, "seats": 25 },\n'
        '    { "name": "Business", "monthly": 199, "seats": 100 },\n'
        '    { "name": "Enterprise", "monthly": 899, "seats": 500 }\n',
    ),
    # Frontend, medium: /surfaces through the shared SurfaceCard component (also a Storybook story).
    (
        "frontend/src/components/SurfaceCard.css",
        "border-radius: 10px; background: #111419;",
        "border-radius: 14px; background: #111419;",
    ),
    (
        "frontend/src/components/SurfaceCard.jsx",
        '<article className="surface-card">',
        '<article className="surface-card surface-card--reviewed">',
    ),
    (
        "frontend/src/components/SurfaceCard.stories.jsx",
        'export const Api = {\n  args: { name: "API", detail: "Bruno request surfaces" },\n};\n',
        'export const Api = {\n  args: { name: "API", detail: "Bruno request surfaces" },\n};\n\n'
        'export const Components = {\n  args: { name: "Components", detail: "Storybook story previews" },\n};\n',
    ),
    # Frontend, small: / changes one button label.
    (
        "frontend/src/content.json",
        '"cta": "Inspect this change"',
        '"cta": "Inspect the reviewed change"',
    ),
    # API, large: every review-summary count changes.
    (
        "backend/app.py",
        'return {"open": 4, "reviewed": 11, "stale_marks": 1}',
        'return {"open": 2, "reviewed": 13, "stale_marks": 3}',
    ),
    (
        "backend/tests/test_app.py",
        'self.assertEqual(review_summary()["stale_marks"], 1)',
        'self.assertEqual(review_summary()["stale_marks"], 3)',
    ),
    (
        "bruno/ReviewSummary.bru",
        "expect(res.body.stale_marks).to.equal(1);",
        "expect(res.body.stale_marks).to.equal(3);",
    ),
    # API, medium: a price change plus a new plan, from the plans.csv change the API reads (below).
    (
        "backend/tests/test_app.py",
        'self.assertEqual(team_plan["monthly"], 49)',
        'self.assertEqual(team_plan["monthly"], 59)',
    ),
    (
        "bruno/Plans.bru",
        "expect(res.body.plans.length).to.equal(3);",
        "expect(res.body.plans.length).to.equal(4);",
    ),
    # API, small: one setting in a long response.
    (
        "backend/app.py",
        '"preview_timeout_seconds": 120,',
        '"preview_timeout_seconds": 180,',
    ),
    (
        "backend/tests/test_app.py",
        'self.assertEqual(settings()["preview_timeout_seconds"], 120)',
        'self.assertEqual(settings()["preview_timeout_seconds"], 180)',
    ),
    (
        "bruno/Settings.bru",
        "expect(res.body.preview_timeout_seconds).to.equal(120);",
        "expect(res.body.preview_timeout_seconds).to.equal(180);",
    ),
    (
        "data/plans.csv",
        "Team,25,49,web;api\nBusiness,100,199,web;api;runtime\n",
        "Team,25,59,web;api;components\nBusiness,100,199,web;api;runtime;components\nEnterprise,500,899,all\n",
    ),
    (
        "compose.yml",
        'PORT: "8000"',
        'PORT: "8000"\n      APP_REVISION: "reviewed"',
    ),
]


INITIAL_PATHS = frozenset({"README.md", "docs/architecture.md"})


def _apply_scenario(scenario: str, phase: str, prepare_baseline: bool) -> None:
    phases = review_scenarios.PHASES[scenario]
    if prepare_baseline:
        touched = review_scenarios.prepare_baseline(ROOT, scenario)
        print(f"Prepared {scenario} baseline:")
    else:
        selected = phases if phase == "all" else (phase,)
        for item in selected:
            if item not in phases:
                raise SystemExit(f"scenario {scenario!r} has no phase {item!r}; phases: {', '.join(phases) or 'none'}")
        touched = sorted({p for item in selected for p in review_scenarios.apply_phase(ROOT, scenario, item)})
        print(f"Applied demo review change ({scenario}: {phase}):")
    for relative in touched:
        print(f"  {relative}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="standard")
    parser.add_argument("--phase", choices=("initial", "final", "change", "revision-2", "all"), default="all")
    parser.add_argument("--prepare-baseline", action="store_true", help="write the scenario's baseline additions")
    parser.add_argument("--check", action="store_true", help="only validate the scenario name")
    args = parser.parse_args()

    error = review_scenarios.check(args.scenario)
    if error:
        raise SystemExit(error)
    if args.check:
        return
    if args.scenario != "standard":
        _apply_scenario(args.scenario, args.phase, args.prepare_baseline)
        return
    if args.prepare_baseline:
        return
    if args.phase in ("change", "revision-2"):
        raise SystemExit(f"scenario 'standard' has no phase {args.phase!r}; phases: initial, final")

    changed: set[str] = set()
    for relative, before, after in REPLACEMENTS:
        if args.phase == "initial" and relative not in INITIAL_PATHS:
            continue
        if args.phase == "final" and relative in INITIAL_PATHS:
            continue
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        if after in text:
            continue
        if before not in text:
            raise SystemExit(f"cannot apply demo change: expected baseline text missing in {relative}")
        path.write_text(text.replace(before, after, 1), encoding="utf-8")
        changed.add(relative)

    if args.phase != "initial":
        media_after = ROOT / ".fixtures" / "media" / "after"
        media_target = ROOT / "media"
        media_target.mkdir(parents=True, exist_ok=True)
        for source in sorted(media_after.iterdir()):
            if not source.is_file():
                continue
            target = media_target / source.name
            shutil.copyfile(source, target)
            changed.add(target.relative_to(ROOT).as_posix())

    print(f"Applied demo review change ({args.phase}):")
    for relative in sorted(changed):
        print(f"  {relative}")


if __name__ == "__main__":
    main()
