from __future__ import annotations

import unittest

from app import greeting, plans, review_summary, settings, team


class GreetingTest(unittest.TestCase):
    def test_default_name(self) -> None:
        self.assertEqual(greeting(""), {"message": "Hello, reviewer", "source": "lemoncrow-cloud"})

    def test_named_reviewer(self) -> None:
        self.assertEqual(greeting("Ada")["message"], "Hello, Ada")


class CatalogTest(unittest.TestCase):
    def test_team_plan_price(self) -> None:
        team_plan = next(plan for plan in plans()["plans"] if plan["name"] == "Team")
        self.assertEqual(team_plan["monthly"], 49)

    def test_review_summary_counts(self) -> None:
        self.assertEqual(review_summary()["stale_marks"], 1)

    def test_settings_keep_previews_offline(self) -> None:
        self.assertFalse(settings()["network"])
        self.assertEqual(settings()["preview_timeout_seconds"], 120)

    def test_team(self) -> None:
        self.assertEqual(len(team()["reviewers"]), 4)


if __name__ == "__main__":
    unittest.main()
