from __future__ import annotations

from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]

# The frontend has five routes and the API six requests; this change touches
# exactly three of each, with deliberately different sizes, so Review lists
# only the affected pages and requests and ranks them by how much they changed.
REPLACEMENTS = [
    (
        "README.md",
        "A deliberately small application that exercises LemonCrow Review's built-in review surfaces and runtimes.",
        "A deliberately small application that demonstrates LemonCrow Review across product surfaces and runtimes.",
    ),
    (
        "docs/architecture.md",
        "The demo has two product processes and two review-only verification paths.",
        "The demo has two product processes, two review-only verification paths, and one intentionally shared runtime model.",
    ),
    # Frontend, large: /pricing gets a featured plan, a price change, and a new plan.
    (
        "frontend/src/pages/PricingPage.jsx",
        '{ name: "Team", price: 49, seats: 25, surfaces: "Web + API" },',
        '{ name: "Team", price: 59, seats: 25, surfaces: "Web + API + Components", featured: true },',
    ),
    (
        "frontend/src/pages/PricingPage.jsx",
        '{ name: "Business", price: 199, seats: 100, surfaces: "Web + API + Runtime" },\n];',
        '{ name: "Business", price: 199, seats: 100, surfaces: "Web + API + Runtime" },\n'
        '  { name: "Enterprise", price: 899, seats: 500, surfaces: "Everything" },\n];',
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
    # API, medium: a price change plus a new plan.
    (
        "backend/app.py",
        '{"name": "Team", "monthly": 49, "seats": 25},',
        '{"name": "Team", "monthly": 59, "seats": 25},',
    ),
    (
        "backend/app.py",
        '{"name": "Business", "monthly": 199, "seats": 100},\n',
        '{"name": "Business", "monthly": 199, "seats": 100},\n'
        '            {"name": "Enterprise", "monthly": 899, "seats": 500},\n',
    ),
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
        'PORT: "8000"\n      DEMO_REVISION: "reviewed"',
    ),
]


def main() -> None:
    changed: set[str] = set()
    for relative, before, after in REPLACEMENTS:
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        if after in text:
            continue
        if before not in text:
            raise SystemExit(f"cannot apply demo change: expected baseline text missing in {relative}")
        path.write_text(text.replace(before, after, 1), encoding="utf-8")
        changed.add(relative)

    media_after = ROOT / ".fixtures" / "media" / "after"
    media_target = ROOT / "media"
    media_target.mkdir(parents=True, exist_ok=True)
    for source in sorted(media_after.iterdir()):
        if not source.is_file():
            continue
        target = media_target / source.name
        shutil.copyfile(source, target)
        changed.add(target.relative_to(ROOT).as_posix())

    print("Applied demo review change:")
    for relative in sorted(changed):
        print(f"  {relative}")


if __name__ == "__main__":
    main()
