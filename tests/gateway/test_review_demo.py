from __future__ import annotations

from pathlib import Path

from lemoncrow.pro.capabilities.review.review_setup import inspect_review_setup
from lemoncrow.pro.capabilities.review.surfaces import load_review_surface_config

DEMO_ROOT = Path(__file__).resolve().parents[2] / "demo" / "review-surfaces"


def test_review_surface_demo_exercises_builtin_surfaces_and_runners() -> None:
    config = load_review_surface_config(DEMO_ROOT)

    assert {runtime.id: runtime.runner for runtime in config.runtimes} == {
        "app-stack": "docker-compose",
        "backend-tests": "command",
        "demo-api": "http-service",
    }
    assert {(surface.id, surface.provider) for surface in config.surfaces} == {
        ("frontend", "web"),
        ("api-requests", "bruno"),
        ("app-services", "service"),
        ("backend-checks", "service"),
    }
    api_surface = next(surface for surface in config.surfaces if surface.id == "api-requests")
    assert api_surface.string("runtime") == "demo-api"

    report = inspect_review_setup(DEMO_ROOT)
    assert {candidate.provider for candidate in report.candidates} >= {"web", "bruno"}
    assert {candidate.runner for candidate in report.runtime_candidates} >= {"docker-compose"}
    assert not [issue for issue in report.issues if issue.level == "error"]


def test_review_surface_demo_carries_distinct_before_after_media_fixtures() -> None:
    before = DEMO_ROOT / ".fixtures" / "media" / "before"
    after = DEMO_ROOT / ".fixtures" / "media" / "after"
    names = {"demo.gif", "demo.mp4", "demo.wav", "demo.pdf"}

    assert {path.name for path in before.iterdir() if path.is_file()} == names
    assert {path.name for path in after.iterdir() if path.is_file()} == names
    for name in names:
        old = (before / name).read_bytes()
        new = (after / name).read_bytes()
        assert old
        assert new
        assert old != new
