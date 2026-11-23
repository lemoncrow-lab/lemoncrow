from __future__ import annotations

import csv
import json
import os
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# The plan catalog the pricing page shows; the API serves it as is.
PLANS_CSV = Path(__file__).resolve().parent.parent / "data" / "plans.csv"


def greeting(name: str) -> dict[str, str]:
    clean = name.strip() or "reviewer"
    return {"message": f"Hello, {clean}", "source": "lemoncrow-cloud"}


def plans() -> dict[str, Any]:
    with PLANS_CSV.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {
        "currency": "USD",
        "plans": [
            {"name": row["plan"], "monthly": int(row["monthly_usd"]), "seats": int(row["seats"])} for row in rows
        ],
    }


def review_summary() -> dict[str, Any]:
    return {"open": 4, "reviewed": 11, "stale_marks": 1}


def team() -> dict[str, Any]:
    return {"reviewers": ["Ada", "Grace", "Linus", "Margaret"]}


def settings() -> dict[str, Any]:
    return {
        "artifact_limit_mb": 25,
        "log_tail_chars": 4000,
        "network": False,
        "preview_timeout_seconds": 120,
        "required_reviewers": 1,
        "sandbox": "bubblewrap",
        "snapshot_pinning": "immutable revision",
        "stale_mark_policy": "reconcile against the new revision",
        "surface_discovery": "changed files only",
    }


ROUTES: dict[str, Callable[[], dict[str, Any]]] = {
    "/api/plans": plans,
    "/api/reviews/summary": review_summary,
    "/api/team": team,
    "/api/settings": settings,
}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path == "/health":
            self._json(200, {"status": "ok"})
            return
        if path == "/api/hello":
            name = "reviewer"
            for pair in query.split("&"):
                key, _, value = pair.partition("=")
                if key == "name" and value:
                    name = value.replace("+", " ")
            self._json(200, greeting(name))
            return
        handler = ROUTES.get(path)
        if handler is not None:
            self._json(200, handler())
            return
        self._json(404, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"cloud api listening on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
