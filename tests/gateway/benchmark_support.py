"""A local origin and a real-PR corpus for the review benchmark's tests.

``Origin`` is a real git repository the benchmark shallow-fetches by commit, exactly as it would from GitHub, so
capture, diffing, the AI endpoint, the run plan and the push simulation all run for real. Only the model is scripted.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lemoncrow.pro.capabilities.review.git_env import git_environment

IDENTITY = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

PRICING_V1 = "PRICES = {'basic': 10, 'pro': 25}\n\n\ndef quote(plan, seats):\n    return PRICES[plan] * seats\n"
# The change under review: a region factor that the scripted reviewer is told is wrong (a BUG: marker).
PRICING_V2 = (
    "PRICES = {'basic': 10, 'pro': 25}\n"
    "FACTORS = {'eu': 1.0, 'us': 1.2}\n\n\n"
    "def quote(plan, seats, region):\n"
    "    # BUG: quote ignores a missing region\n"
    "    return PRICES[plan] * seats * FACTORS[region]\n"
)
PRICING_FIXED = PRICING_V2.replace("    # BUG: quote ignores a missing region\n", "")
INVOICE_V1 = (
    "from api.pricing import quote\n\n\ndef build(customer):\n    return quote(customer.plan, customer.seats)\n"
)
INVOICE_V2 = INVOICE_V1 + "\n\ndef label(customer):\n    return str(customer.id)\n"
REPORTS_V1 = "def monthly(customers):\n    return len(customers)\n"
REPORTS_V2 = "def monthly(customers):\n    return sum(1 for c in customers if c)\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=git_environment(IDENTITY)
    ).stdout.strip()


@dataclass
class Origin:
    path: Path
    base: str
    bug: str
    fix: str
    plain: str
    plain_base: str
    helper: str = ""

    @property
    def url(self) -> str:
        return f"file://{self.path}"


def make_origin(root: Path) -> Origin:
    repo = root / "origin"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    files = {
        "src/api/pricing.py": PRICING_V1,
        "src/api/invoice.py": INVOICE_V1,
        "src/api/reports.py": REPORTS_V1,
        "src/helpers.py": "def clean(text):\n    return text.strip()\n",
    }

    def write(changes: dict[str, str]) -> None:
        for name, text in changes.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    write(files)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    write({"src/api/pricing.py": PRICING_V2, "src/api/invoice.py": INVOICE_V2})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Quote needs a region (#12)")
    bug = git(repo, "rev-parse", "HEAD")
    write({"src/api/pricing.py": PRICING_FIXED})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Fix the region regression (#13)")
    fix = git(repo, "rev-parse", "HEAD")
    write({"src/api/reports.py": REPORTS_V2})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Count only real customers (#14)")
    plain = git(repo, "rev-parse", "HEAD")
    write({"src/helpers.py": "def clean(text):\n    return text.strip().lower()\n"})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Lower-case cleaned text (#15)")
    helper = git(repo, "rev-parse", "HEAD")
    return Origin(repo, base, bug, fix, plain, fix, helper)


def corpus_dict(origin: Origin, **overrides: Any) -> dict[str, Any]:
    buggy_line = PRICING_V2.splitlines().index("    # BUG: quote ignores a missing region") + 1
    base: dict[str, Any] = {
        "version": 1,
        "id": "test-prs",
        "visibility": "public",
        "line_tolerance": 3,
        "match": "either",
        "prs": [
            {
                "id": "shop-12",
                "repository": "acme/shop",
                "clone_url": origin.url,
                "pull": 12,
                "title": "Quote needs a region",
                "license": "MIT",
                "provenance": {
                    "source": "public_repository",
                    "repository": "https://github.com/acme/shop",
                    "commit": origin.bug,
                    "note": "test fixture",
                },
                "base_sha": origin.base,
                "head_sha": origin.bug,
                "labels": [
                    {
                        "id": "missing-region",
                        "summary": "Fix the region regression (#13)",
                        "symbols": ["region"],
                        "regions": [
                            {"path": "src/api/pricing.py", "lines": [buggy_line, buggy_line], "role": "fix_site"}
                        ],
                        "evidence": {
                            "fix_commit": origin.fix,
                            "fix_url": "https://github.com/acme/shop/commit/" + origin.fix,
                            "introduced_by": origin.bug,
                            "attribution": "fix_message",
                        },
                    }
                ],
                "pushes": [
                    {"id": "apply-fix", "apply_commit": origin.fix, "apply_paths": ["src/api/pricing.py"]},
                ],
            },
            {
                "id": "shop-14",
                "repository": "acme/shop",
                "clone_url": origin.url,
                "pull": 14,
                "title": "Count only real customers",
                "license": "MIT",
                "provenance": {
                    "source": "public_repository",
                    "repository": "https://github.com/acme/shop",
                    "commit": origin.plain,
                    "note": "test fixture",
                },
                "base_sha": origin.fix,
                "head_sha": origin.plain,
            },
        ],
    }
    base.update(overrides)
    return base


def write_corpus(root: Path, origin: Origin, **overrides: Any) -> Path:
    path = root / "real_prs.yaml"
    path.write_text(yaml.safe_dump(corpus_dict(origin, **overrides), sort_keys=False), encoding="utf-8")
    return path
