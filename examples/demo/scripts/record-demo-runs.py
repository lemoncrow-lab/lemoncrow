#!/usr/bin/env python3
"""Record the demo's before/after API runs on the latest Review revision.

Runs each changed Bruno request once on the base (``old``) and once on the head (``new``) side through the
local server's own runtime-validation route (the ``api-requests`` surface of ``.lemoncrow/review.yaml``, served by
the ``demo-api`` http-service runtime). The stored records are what the X-Ray draws as ``recorded`` calls, and the
base/head bodies become ``api_response`` comparisons (the Team plan price 49 -> 59, the review summary counts; for
``tenant-cache``, Globex's invoice list and the dashboard after an edit).

Usage: record-demo-runs.py <demo checkout> <review id> [scenario]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from lemoncrow_client.config import load_config

#: Per scenario, the request files the prepared change edits, in the order a reviewer reads them.
REQUESTS: dict[str, tuple[str, ...]] = {
    "standard": ("bruno/Plans.bru", "bruno/ReviewSummary.bru", "bruno/Settings.bru"),
    # Each runs its bru.runRequest steps first on the same server: Acme before Globex, the edit before the list.
    "tenant-cache": (
        "bruno/invoices/AcmeExport.bru",
        "bruno/invoices/GlobexAfterAcme.bru",
        "bruno/invoices/AcmeDashboardAfterEdit.bru",
    ),
}
TIMEOUT_S = 180.0


def _call(base: str, token: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read() or b"null")


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4) or (len(argv) == 4 and argv[3] not in REQUESTS):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    checkout, review_id = Path(argv[1]).resolve(), argv[2]
    requests = REQUESTS[argv[3] if len(argv) == 4 else "standard"]
    config = load_config(cwd=checkout, product="review")
    overview = _call(config.url, config.token, "GET", f"/api/reviews/{review_id}")
    revision_id = str(overview["revision"]["id"])
    failed = 0
    for surface in requests:
        for side in ("old", "new"):
            body = {"revision_id": revision_id, "provider": "bruno", "surface_id": surface, "side": side}
            try:
                result = _call(config.url, config.token, "POST", f"/api/reviews/{review_id}/runtime-validation", body)
            except urllib.error.HTTPError as exc:
                failed += 1
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                print(f"  {surface} ({side}): HTTP {exc.code} {detail}", file=sys.stderr)
                continue
            summary = result.get("summary") if isinstance(result, dict) else ""
            print(f"  recorded {surface} ({side}){': ' + str(summary) if summary else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
