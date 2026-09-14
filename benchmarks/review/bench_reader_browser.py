#!/usr/bin/env python3
"""Headless-Chrome performance probe for the real Review Reader.

This deliberately uses Chrome DevTools Protocol directly rather than adding a
Playwright dependency. It opens an existing durable review, waits for the
initial five-patch window, records heap/DOM/resource metrics, then jumps to a
far-away outline file and verifies that the reader fetches only a bounded
window around that destination.

Typical use after ``bench_reader.py --keep-store``::

    uv run python benchmarks/review/bench_reader_browser.py \
      --repo-root . --store-root /tmp/lc-review-bench-...
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from lemoncrow.pro.capabilities.review.revisions import compute_frontier
from lemoncrow.pro.capabilities.review.sources.local import read_packet_json
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.targets import derive_review_targets
from lemoncrow.pro.capabilities.review.workspace import ensure_workspace, read_registration


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _chrome_binary() -> str:
    override = os.environ.get("CHROME_BIN")
    if override and Path(override).is_file():
        return override
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    for path in (
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ):
        if Path(path).is_file():
            return path
    raise RuntimeError("Chrome/Chromium not found; set CHROME_BIN")


def _review_inputs(store_root: Path) -> tuple[str, tuple[str, ...]]:
    store = ReviewStore(store_root)
    sessions = store.list_sessions(status="", limit=10)
    if not sessions:
        raise RuntimeError(f"no durable review in {store_root}")
    session = sessions[0]
    revisions = store.list_revisions(session.id)
    if not revisions:
        raise RuntimeError(f"review {session.id} has no revision")
    revision = revisions[-1]
    units = store.list_units(revision.id)
    annotations = store.list_annotations(session.id)
    frontier = compute_frontier(
        session.id,
        session.reviewer_id,
        revision,
        units,
        store.list_marks(session.id, reviewer_id=session.reviewer_id),
        annotations=annotations,
    )
    targets = derive_review_targets(
        units,
        frontier.entries,
        read_packet_json(store, revision),
        annotations=annotations,
    )
    paths: list[str] = []
    seen: set[str] = set()
    for target in targets:
        if target.path not in seen:
            seen.add(target.path)
            paths.append(target.path)
    return session.id, tuple(paths)


def _distant_unique_path(paths: tuple[str, ...]) -> str:
    if not paths:
        raise RuntimeError("review has no target paths")
    counts = Counter(Path(path).name for path in paths)
    start = min(100, max(0, len(paths) // 3))
    for path in paths[start:]:
        if counts[Path(path).name] == 1:
            return path
    for path in reversed(paths):
        if counts[Path(path).name] == 1:
            return path
    return paths[-1]


class Cdp:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.next_id = 1

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        await self.ws.send_json({"id": request_id, "method": method, "params": params or {}})
        while True:
            message = await self.ws.receive()
            if message.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(message.data)
                if payload.get("id") != request_id:
                    continue
                if "error" in payload:
                    raise RuntimeError(f"CDP {method} failed: {payload['error']}")
                result = payload.get("result")
                return result if isinstance(result, dict) else {}
            if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise RuntimeError(f"CDP socket closed while waiting for {method}")

    async def evaluate(self, expression: str) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        remote = result.get("result", {})
        if isinstance(remote, dict) and "exceptionDetails" in result:
            raise RuntimeError(str(result["exceptionDetails"]))
        return remote.get("value") if isinstance(remote, dict) else None


async def _json_get(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json()


async def _page_target(session: aiohttp.ClientSession, port: int, origin: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        try:
            payload = await _json_get(session, f"http://127.0.0.1:{port}/json/list")
            if isinstance(payload, list):
                last = [item for item in payload if isinstance(item, dict)]
                for item in last:
                    if item.get("type") == "page" and str(item.get("url") or "").startswith(origin):
                        return item
        except (TimeoutError, aiohttp.ClientError):
            pass
        await asyncio.sleep(0.05)
    raise RuntimeError(f"Chrome page target did not appear; targets={last!r}")


async def _wait_js(cdp: Cdp, expression: str, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await cdp.evaluate(expression)
        if last:
            return last
        await asyncio.sleep(0.025)
    raise RuntimeError(f"browser condition timed out; last={last!r}")


async def _snapshot(cdp: Cdp) -> dict[str, Any]:
    heap = await cdp.call("Runtime.getHeapUsage")
    dom = await cdp.call("Memory.getDOMCounters")
    resources = await cdp.evaluate("""(() => {
          const all = performance.getEntriesByType('resource');
          const patches = all.filter((item) => item.name.includes('/patch'));
          return {
            resource_count: all.length,
            patch_requests: patches.length,
            patch_transfer_bytes: patches.reduce((n, item) => n + (item.transferSize || 0), 0),
            patch_encoded_bytes: patches.reduce((n, item) => n + (item.encodedBodySize || 0), 0),
            now_ms: performance.now(),
          };
        })()""")
    return {
        "heap_used_bytes": int(heap.get("usedSize") or 0),
        "heap_total_bytes": int(heap.get("totalSize") or 0),
        "dom_documents": int(dom.get("documents") or 0),
        "dom_nodes": int(dom.get("nodes") or 0),
        "dom_event_listeners": int(dom.get("jsEventListeners") or 0),
        **(resources if isinstance(resources, dict) else {}),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo_root).resolve()
    store_root = Path(args.store_root).resolve()
    review_id, paths = _review_inputs(store_root)
    expected_initial = min(5, len(paths))
    distant_path = _distant_unique_path(paths)
    distant_name = Path(distant_path).name

    existing = read_registration(store_root, repo)
    handle = ensure_workspace(store_root, repo, timeout=args.timeout)
    started_workspace = existing is None or existing.pid != handle.pid

    chrome_port = _free_port()
    profile_dir = Path(tempfile.mkdtemp(prefix="lc-review-chrome-"))
    page_url = f"{handle.url}/review#t={quote(handle.token)}&r={quote(review_id)}"
    chrome = subprocess.Popen(
        [
            _chrome_binary(),
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-default-apps",
            "--no-first-run",
            f"--remote-debugging-port={chrome_port}",
            f"--user-data-dir={profile_dir}",
            page_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        timeout_cfg = aiohttp.ClientTimeout(total=args.timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            target = await _page_target(session, chrome_port, handle.url, args.timeout)
            ws_url = str(target.get("webSocketDebuggerUrl") or "")
            if not ws_url:
                raise RuntimeError("Chrome page target had no webSocketDebuggerUrl")
            async with session.ws_connect(ws_url, max_msg_size=32 * 1024 * 1024) as ws:
                cdp = Cdp(ws)
                await cdp.call("Runtime.enable")
                ready_expression = """(() => {
                  const reader = document.querySelector('[data-testid="review-reader"]');
                  const patches = performance.getEntriesByType('resource').filter((item) => item.name.includes('/patch'));
                  return Boolean(reader) && patches.length >= __EXPECTED__;
                })()""".replace("__EXPECTED__", str(expected_initial))
                await _wait_js(cdp, ready_expression, args.timeout)
                initial = await _snapshot(cdp)

                before_patches = int(initial.get("patch_requests") or 0)
                click_expression = """(() => {
                  const name = __NAME__;
                  const outline = document.querySelector('aside[aria-label="Review outline"]');
                  if (!outline) return false;
                  const buttons = [...outline.querySelectorAll('button')];
                  const row = buttons.find((button) => {
                    const filename = button.querySelector('.truncate');
                    return filename && filename.textContent === name;
                  });
                  if (!row) return false;
                  row.click();
                  return true;
                })()""".replace("__NAME__", json.dumps(distant_name))
                clicked = await cdp.evaluate(click_expression)
                if not clicked:
                    raise RuntimeError(f"could not find distant outline row {distant_name!r}")
                nav_started = time.perf_counter()
                navigation_expression = """(() => {
                  const name = __NAME__;
                  const active = document.querySelector('aside[aria-label="Review outline"] [aria-current="location"] .truncate');
                  const patches = performance.getEntriesByType('resource').filter((item) => item.name.includes('/patch'));
                  return active && active.textContent === name && patches.length > __PATCHES__;
                })()""".replace("__NAME__", json.dumps(distant_name)).replace("__PATCHES__", str(before_patches))
                await _wait_js(cdp, navigation_expression, args.timeout)
                distant_navigation_ms = (time.perf_counter() - nav_started) * 1000
                after = await _snapshot(cdp)
                return {
                    "review_id": review_id,
                    "files": len(paths),
                    "distant_path": distant_path,
                    "initial_reader_ready_ms": round(float(initial.get("now_ms") or 0.0), 3),
                    "distant_navigation_ms": round(distant_navigation_ms, 3),
                    "initial": initial,
                    "after_distant_navigation": after,
                    "additional_patch_requests": int(after.get("patch_requests") or 0) - before_patches,
                }
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)
        if started_workspace:
            with contextlib.suppress(ProcessLookupError):
                os.kill(handle.pid, signal.SIGTERM)


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile the Review Reader in headless Chrome via CDP.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--store-root", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
