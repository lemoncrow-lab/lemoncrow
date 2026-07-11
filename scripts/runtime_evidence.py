#!/usr/bin/env python3
"""Capture reproducible runtime evidence from a local LemonCrow stack."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def fetch_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            return {
                "ok": True,
                "status": response.status,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "body": json.loads(body) if body else None,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return {
            "ok": False,
            "status": exc.code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "body": body,
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "status": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "body": str(exc.reason),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787", help="service base URL")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/runtime-evidence/latest.json"),
        help="destination path for the evidence JSON",
    )
    parser.add_argument("--days", type=int, default=1, help="days window for analytics summary")
    parser.add_argument("--api-key", help="optional API key for authenticated endpoints")
    args = parser.parse_args()

    headers = {"accept": "application/json"}
    if args.api_key:
        headers["x-api-key"] = args.api_key

    base = args.base_url.rstrip("/")
    payload = {
        "base_url": base,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "health": fetch_json(f"{base}/health", headers),
        "analytics_summary": fetch_json(f"{base}/analytics/summary?days={args.days}", headers),
        "traces": fetch_json(f"{base}/v1/traces", headers),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
