"""Headless-browser render driver for deep rendering diffs (Phase 9R item 13).

The driver seam is :class:`RenderDriver`: given a static site directory and
a bounded list of :class:`RenderCell` (route or scripted flow x viewport x
theme) under :class:`DeterminismControls`, it returns one
:class:`PageCapture` per cell -- screenshot bytes, DOM and accessibility-tree
snapshots, per-page network and console events, layout shift and web
performance metrics, and the rectangles of masked dynamic regions.

:class:`ChromeCdpDriver` runs a stdlib Python script *inside* Bubblewrap
(private network and PID namespaces, read-only runtime, the site read-only)
that serves the site on loopback and drives Chrome over the DevTools pipe.
Determinism: pinned browser (its version enters the environment
fingerprint), host fonts (their digest enters it too), fixed locale and
timezone, a frozen clock, seeded ``Math.random``/``crypto.getRandomValues``,
animations and transitions off, reduced motion, and masked dynamic regions.
All outputs are untrusted (the reviewed page produced them) and are bounded
here; redaction and masking happen before storage (:mod:`render_diff`).

A deployment whose browser runs elsewhere implements :class:`RenderExecutor`:
it receives the side's source (:class:`RenderSource`) instead of a built site,
and builds and renders it where its sandbox runs. Captures cross that boundary
through :func:`capture_to_wire` / :func:`capture_from_wire`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

MAX_DOM_LINES: Final[int] = 5000
MAX_AX_LINES: Final[int] = 5000
MAX_EVENTS: Final[int] = 200
MAX_STEPS: Final[int] = 20
MAX_SCREENSHOT_BYTES: Final[int] = 8 * 1024 * 1024
_MAX_RESULT_BYTES: Final[int] = 64 * 1024 * 1024

StepKind = Literal["click", "type", "press", "wait", "hover"]
_STEP_KINDS: Final[frozenset[str]] = frozenset({"click", "type", "press", "wait", "hover"})
_MAX_WIRE_CELLS: Final[int] = 96
_MAX_WIRE_TEXT: Final[int] = 2048


def _wire_text(raw: Mapping[str, Any], key: str, default: str = "") -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or len(value) > _MAX_WIRE_TEXT:
        raise ValueError(f"render wire field {key} is invalid")
    return value


def _wire_int(raw: Mapping[str, Any], key: str, low: int, high: int) -> int:
    value = raw.get(key)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"render wire field {key} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class Viewport:
    id: str
    width: int
    height: int

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "width": self.width, "height": self.height}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Viewport:
        return cls(_wire_text(raw, "id"), _wire_int(raw, "width", 1, 10_000), _wire_int(raw, "height", 1, 10_000))


@dataclass(frozen=True, slots=True)
class InteractionStep:
    kind: StepKind
    selector: str = ""
    value: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"kind": self.kind, "selector": self.selector, "value": self.value}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> InteractionStep:
        kind = _wire_text(raw, "kind")
        if kind not in _STEP_KINDS:
            raise ValueError("render wire step kind is invalid")
        return cls(cast(StepKind, kind), _wire_text(raw, "selector"), _wire_text(raw, "value"))


@dataclass(frozen=True, slots=True)
class RenderCell:
    """One capture: a route (or a scripted flow starting at a route) at one viewport and theme."""

    route: str
    viewport: Viewport
    theme: str = "light"
    flow: str = ""
    steps: tuple[InteractionStep, ...] = ()

    @property
    def id(self) -> str:
        target = f"flow:{self.flow}" if self.flow else self.route
        return f"{target}@{self.viewport.id}/{self.theme}"

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "route": self.route,
            "viewport": self.viewport.to_wire(),
            "theme": self.theme,
            "flow": self.flow,
            "steps": [step.to_wire() for step in self.steps],
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> RenderCell:
        viewport, steps = raw.get("viewport"), raw.get("steps")
        if not isinstance(viewport, Mapping) or not isinstance(steps, list) or len(steps) > MAX_STEPS:
            raise ValueError("render wire cell is invalid")
        if not all(isinstance(step, Mapping) for step in steps):
            raise ValueError("render wire cell step is invalid")
        cell = cls(
            _wire_text(raw, "route"),
            Viewport.from_wire(viewport),
            _wire_text(raw, "theme", "light"),
            _wire_text(raw, "flow"),
            tuple(InteractionStep.from_wire(step) for step in steps),
        )
        if not cell.route.startswith("/") or cell.id != raw.get("id"):
            raise ValueError("render wire cell identity is invalid")
        return cell


@dataclass(frozen=True, slots=True)
class DeterminismControls:
    locale: str = "en-US"
    timezone: str = "UTC"
    clock_ms: int = 1_704_067_200_000  # 2024-01-01T00:00:00Z
    seed: int = 1
    reduced_motion: bool = True
    disable_animations: bool = True
    mask_selectors: tuple[str, ...] = ()
    settle_ms: int = 300

    def to_wire(self) -> dict[str, Any]:
        return {
            "locale": self.locale,
            "timezone": self.timezone,
            "clock_ms": self.clock_ms,
            "seed": self.seed,
            "reduced_motion": self.reduced_motion,
            "disable_animations": self.disable_animations,
            "mask_selectors": list(self.mask_selectors),
            "settle_ms": self.settle_ms,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> DeterminismControls:
        masks = raw.get("mask_selectors")
        if not isinstance(masks, list) or len(masks) > 64 or not all(isinstance(item, str) for item in masks):
            raise ValueError("render wire mask selectors are invalid")
        reduced, animations = raw.get("reduced_motion"), raw.get("disable_animations")
        if not isinstance(reduced, bool) or not isinstance(animations, bool):
            raise ValueError("render wire determinism flags are invalid")
        return cls(
            locale=_wire_text(raw, "locale"),
            timezone=_wire_text(raw, "timezone"),
            clock_ms=_wire_int(raw, "clock_ms", 0, 2**53),
            seed=_wire_int(raw, "seed", 0, 2**32),
            reduced_motion=reduced,
            disable_animations=animations,
            mask_selectors=tuple(masks),
            settle_ms=_wire_int(raw, "settle_ms", 0, 60_000),
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_wire(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RenderEnvironment:
    """Browser and font identity; both enter the environment fingerprint."""

    available: bool
    reason: str = ""
    browser: str = ""
    fonts_digest: str = ""
    sandbox: str = "bubblewrap"
    #: The executor's own image identity when it is not this process's (a remote worker).
    image_digest: str = ""


@dataclass(frozen=True, slots=True)
class PageCapture:
    cell_id: str
    status: Literal["captured", "failed"]
    reason: str = ""
    screenshot: bytes = b""
    mask_regions: tuple[tuple[int, int, int, int], ...] = ()
    dom_lines: tuple[str, ...] = ()
    dom_truncated: bool = False
    ax_lines: tuple[str, ...] = ()
    ax_truncated: bool = False
    network: tuple[str, ...] = ()
    console: tuple[str, ...] = ()
    cls: float = 0.0
    lcp_ms: float = 0.0
    metrics: Mapping[str, float] = field(default_factory=dict)


class RenderDriver(Protocol):
    def environment(self) -> RenderEnvironment: ...

    def capture(
        self,
        site: Path,
        cells: Sequence[RenderCell],
        controls: DeterminismControls,
        *,
        spa_fallback: bool,
        timeout: float,
    ) -> list[PageCapture]: ...


@dataclass(frozen=True, slots=True)
class RenderSource:
    """What a side renders from: a static directory, or an app built where the sandbox runs."""

    root: str
    spa: bool
    framework: str = ""  # empty: ``root`` is already a static site


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """What an off-machine executor needs from the Review to render one entry."""

    source: Callable[[], RenderSource]
    """What to render; raises ``RenderUnavailable`` when the change has nothing renderable."""
    materialize: Callable[[str, Path, str], None]
    """``(side, target, scope)``: write that Snapshot side; raises ``RenderUnavailable``."""
    hermetic: Callable[[str], tuple[Mapping[str, Any], str]]
    """``side -> (trusted policy wire, cache namespace)``; raises ``RenderUnavailable``."""


class BoundRender(RenderDriver, Protocol):
    """A :class:`RenderDriver` that also resolves each side's site for ``RenderRunInputs.site``."""

    @property
    def recipe_stages(self) -> tuple[str, ...]:
        """Stages the recipe declares, the same on both sides (they enter the fingerprint)."""
        ...

    def site(self, side: str, work: Path) -> tuple[Path, bool, str]: ...

    def stages(self, side: str) -> tuple[Any, ...]:
        """Fetch/build stage outcomes the executor reported for *side*."""
        ...


class RenderExecutor(Protocol):
    """A deployment's off-machine render path (``RunnerRegistry.render_executor``)."""

    def bind(self, request: RenderRequest) -> BoundRender: ...


# --------------------------------------------------------------------------- #
# page scripts
# --------------------------------------------------------------------------- #

INIT_SCRIPT: Final[str] = r"""
(() => {
  const C = __LC_CONTROLS__;
  const RealDate = Date;
  class FrozenDate extends RealDate {
    constructor(...args) { if (args.length === 0) { super(C.clock_ms); } else { super(...args); } }
    static now() { return C.clock_ms; }
  }
  FrozenDate.UTC = RealDate.UTC; FrozenDate.parse = RealDate.parse;
  window.Date = FrozenDate;
  let s = C.seed >>> 0;
  const rand = () => { s = (s + 0x6D2B79F5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  Math.random = rand;
  if (window.crypto && window.crypto.getRandomValues) {
    window.crypto.getRandomValues = (array) => {
      for (let i = 0; i < array.length; i++) { array[i] = Math.floor(rand() * 256); }
      return array;
    };
  }
  window.__lcVitals = { cls: 0, lcp: 0 };
  try { new PerformanceObserver((list) => { for (const e of list.getEntries()) { if (!e.hadRecentInput) window.__lcVitals.cls += e.value; } })
    .observe({ type: 'layout-shift', buffered: true }); } catch (e) {}
  try { new PerformanceObserver((list) => { for (const e of list.getEntries()) { window.__lcVitals.lcp = e.startTime; } })
    .observe({ type: 'largest-contentful-paint', buffered: true }); } catch (e) {}
  if (C.disable_animations) {
    const install = () => { const el = document.createElement('style'); el.setAttribute('data-lemoncrow', '');
      el.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important;scroll-behavior:auto!important}';
      (document.head || document.documentElement).appendChild(el); };
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', install); } else { install(); }
  }
})();
"""

DOM_SCRIPT: Final[str] = r"""
(() => {
  const masks = __LC_MASKS__; const LIMIT = __LC_LIMIT__;
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  const KEEP = ['id', 'role', 'aria-label', 'aria-labelledby', 'aria-describedby', 'aria-hidden', 'aria-expanded',
    'aria-checked', 'aria-pressed', 'aria-current', 'type', 'name', 'href', 'src', 'alt', 'title', 'for', 'placeholder',
    'disabled', 'checked', 'tabindex', 'lang', 'dir'];
  const masked = new Set();
  for (const sel of masks) { try { document.querySelectorAll(sel).forEach((e) => masked.add(e)); } catch (e) {} }
  const out = []; let truncated = false;
  const walk = (node, depth, hidden) => {
    if (out.length >= LIMIT) { truncated = true; return; }
    if (node.nodeType === 3) { const t = node.textContent.replace(/\s+/g, ' ').trim();
      if (t) out.push('  '.repeat(depth) + (hidden ? '"[masked]"' : JSON.stringify(t.slice(0, 200)))); return; }
    if (node.nodeType !== 1 || SKIP.has(node.tagName) || node.hasAttribute('data-lemoncrow')) return;
    const isMasked = hidden || masked.has(node);
    const attrs = [];
    for (const a of KEEP) { if (node.hasAttribute(a)) attrs.push(a + '=' + JSON.stringify(isMasked ? '[masked]' : String(node.getAttribute(a)).slice(0, 120))); }
    const cls = node.getAttribute('class');
    if (cls) attrs.push('class=' + JSON.stringify(cls.split(/\s+/).filter(Boolean).sort().join(' ').slice(0, 160)));
    out.push('  '.repeat(depth) + '<' + node.tagName.toLowerCase() + (attrs.length ? ' ' + attrs.join(' ') : '') + '>');
    for (const child of node.childNodes) walk(child, depth + 1, isMasked);
    if (node.shadowRoot) for (const child of node.shadowRoot.childNodes) walk(child, depth + 1, isMasked);
  };
  walk(document.documentElement, 0, false);
  const vw = window.innerWidth, vh = window.innerHeight; const rects = [];
  for (const el of masked) { const r = el.getBoundingClientRect();
    const x = Math.max(0, Math.floor(r.left)), y = Math.max(0, Math.floor(r.top));
    const w = Math.min(vw, Math.ceil(r.right)) - x, h = Math.min(vh, Math.ceil(r.bottom)) - y;
    if (w > 0 && h > 0 && rects.length < 64) rects.push([x, y, w, h]); }
  return { lines: out, truncated, rects, vitals: window.__lcVitals || { cls: 0, lcp: 0 } };
})()
"""

MASK_TEXT_SCRIPT: Final[str] = r"""
(() => { const masks = __LC_MASKS__;
  for (const sel of masks) { try { document.querySelectorAll(sel).forEach((e) => {
    e.textContent = '[masked]'; for (const a of ['aria-label', 'title', 'alt', 'value']) { if (e.hasAttribute(a)) e.setAttribute(a, '[masked]'); } }); } catch (e) {} }
  return true; })()
"""

#: Runs inside the sandbox with the system ``python3``: serves the site on
#: loopback and drives Chrome over ``--remote-debugging-pipe`` (fds 3/4).
SANDBOX_SCRIPT: Final[str] = r"""
import base64, functools, http.server, json, os, posixpath, queue, subprocess, sys, threading, time

cfg = json.load(open(sys.argv[1]))
out_dir = sys.argv[2]
site = cfg["site"]

class Handler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/favicon.ico" and not os.path.exists(self.translate_path(path)):
            # Browser chrome, not the page: answer empty so it never adds network/console noise.
            self.send_response(204)
            self.end_headers()
            return None
        if cfg["spa"] and not os.path.exists(self.translate_path(path)) and not posixpath.splitext(path)[1]:
            self.path = "/index.html"
        return super().send_head()
    def log_message(self, *args):
        pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=site))
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = f"http://127.0.0.1:{port}"

to_r, to_w = os.pipe()
from_r, from_w = os.pipe()

def wire():
    os.dup2(to_r, 100); os.dup2(from_w, 101); os.dup2(100, 3); os.dup2(101, 4)

controls = cfg["controls"]
chrome = subprocess.Popen(
    [cfg["chrome"], "--headless=new", "--remote-debugging-pipe", "--no-sandbox", "--disable-gpu",
     "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
     "--disable-extensions", "--disable-sync", "--disable-component-update", "--mute-audio", "--hide-scrollbars",
     "--font-render-hinting=none", "--force-color-profile=srgb", "--disable-lcd-text",
     "--disable-features=Translate,MediaRouter,OptimizationHints,PaintHolding", "--lang=" + controls["locale"],
     "--user-data-dir=/tmp/lc-profile", "about:blank"],
    preexec_fn=wire, close_fds=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=open("/tmp/lc-chrome.log", "wb"),
    env={"HOME": "/tmp", "TZ": controls["timezone"], "LANG": controls["locale"].replace("-", "_") + ".UTF-8",
         "PATH": "/usr/bin:/bin", "FONTCONFIG_PATH": "/etc/fonts"},
)
os.close(to_r); os.close(from_w)

responses = {}
events = queue.Queue()
cond = threading.Condition()

def reader():
    buf = b""
    while True:
        chunk = os.read(from_r, 1 << 20)
        if not chunk:
            break
        buf += chunk
        while b"\0" in buf:
            raw, buf = buf.split(b"\0", 1)
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "id" in msg:
                with cond:
                    responses[msg["id"]] = msg
                    cond.notify_all()
            else:
                events.put(msg)

threading.Thread(target=reader, daemon=True).start()
counter = [0]

def send(method, params=None, session=None, timeout=30.0):
    counter[0] += 1
    ident = counter[0]
    msg = {"id": ident, "method": method, "params": params or {}}
    if session:
        msg["sessionId"] = session
    os.write(to_w, json.dumps(msg).encode() + b"\0")
    deadline = time.monotonic() + timeout
    with cond:
        while ident not in responses:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(method)
            cond.wait(left)
        reply = responses.pop(ident)
    if "error" in reply:
        raise RuntimeError(f"{method}: {reply['error'].get('message')}")
    return reply.get("result", {})

result = {"browser": "", "pages": []}
try:
    result["browser"] = send("Browser.getVersion").get("product", "")
    target = send("Target.createTarget", {"url": "about:blank"})["targetId"]
    session = send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    for domain in ("Page", "Network", "Runtime", "Log", "Performance"):
        send(domain + ".enable", {}, session)
    send("Emulation.setTimezoneOverride", {"timezoneId": controls["timezone"]}, session)
    send("Emulation.setLocaleOverride", {"locale": controls["locale"]}, session)
    send("Page.addScriptToEvaluateOnNewDocument", {"source": cfg["init"]}, session)
    send("Network.setCacheDisabled", {"cacheDisabled": True}, session)

    def evaluate(expression, timeout=15.0):
        reply = send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, session, timeout)
        if reply.get("exceptionDetails"):
            raise RuntimeError("page script failed: " + str(reply["exceptionDetails"].get("text", ""))[:200])
        return reply.get("result", {}).get("value")

    def drain(state, until=None, quiet=None):
        end = time.monotonic() + (until or 0)
        last = time.monotonic()
        while True:
            left = end - time.monotonic()
            if quiet is not None and time.monotonic() - last >= quiet and not state["pending"]:
                return
            if left <= 0:
                return
            try:
                msg = events.get(timeout=min(left, 0.05))
            except queue.Empty:
                continue
            last = time.monotonic()
            method = msg.get("method", ""); params = msg.get("params", {})
            if method == "Page.loadEventFired":
                state["loaded"] = True
            elif method == "Network.requestWillBeSent":
                req = params.get("request", {})
                url = req.get("url", "")
                if url.startswith("data:"):
                    continue
                state["pending"].add(params.get("requestId"))
                state["requests"][params.get("requestId")] = [req.get("method", "GET"), url, ""]
            elif method == "Network.responseReceived":
                row = state["requests"].get(params.get("requestId"))
                if row is not None:
                    row[2] = str(params.get("response", {}).get("status", ""))
            elif method == "Network.loadingFinished":
                state["pending"].discard(params.get("requestId"))
            elif method == "Network.loadingFailed":
                state["pending"].discard(params.get("requestId"))
                row = state["requests"].get(params.get("requestId"))
                if row is not None:
                    row[2] = "blocked" if params.get("blockedReason") or "ERR_" in params.get("errorText", "") else "failed"
            elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "warning", "assert"):
                text = " ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", []))
                state["console"].append(params.get("type") + ": " + text[:300])
            elif method == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails", {})
                text = details.get("exception", {}).get("description") or details.get("text", "")
                state["console"].append("exception: " + str(text).splitlines()[0][:300] if text else "exception")
            elif method == "Log.entryAdded":
                entry = params.get("entry", {})
                if entry.get("level") in ("error", "warning"):
                    state["console"].append(entry.get("level") + ": " + str(entry.get("text", ""))[:300])

    def ax_lines():
        nodes = send("Accessibility.getFullAXTree", {}, session).get("nodes", [])
        by_id = {node.get("nodeId"): node for node in nodes}
        children = {nid: node.get("childIds", []) for nid, node in by_id.items()}
        lines = []
        truncated = False
        def visit(nid, depth):
            nonlocal truncated
            if len(lines) >= cfg["ax_limit"]:
                truncated = True
                return
            node = by_id.get(nid)
            if node is None:
                return
            role = str(node.get("role", {}).get("value", ""))
            name = " ".join(str(node.get("name", {}).get("value", "")).split())[:160]
            show = not node.get("ignored") and role not in ("none", "generic", "InlineTextBox", "LineBreak", "StaticText") or (role == "generic" and name)
            if show:
                props = []
                for prop in node.get("properties", []):
                    if prop.get("name") in ("checked", "expanded", "pressed", "selected", "disabled", "level", "required", "invalid"):
                        props.append(f"{prop['name']}={json.dumps(prop.get('value', {}).get('value'))}")
                lines.append("  " * depth + role + (" " + json.dumps(name) if name else "") + (" [" + ",".join(props) + "]" if props else ""))
            for child in children.get(nid, []):
                visit(child, depth + 1 if show else depth)
        if nodes:
            visit(nodes[0].get("nodeId"), 0)
        return lines, truncated

    for cell in cfg["cells"]:
        page = {"cell": cell["id"], "status": "failed", "reason": ""}
        state = {"loaded": False, "pending": set(), "requests": {}, "console": []}
        try:
            vp = cell["viewport"]
            send("Emulation.setDeviceMetricsOverride", {"width": vp["width"], "height": vp["height"], "deviceScaleFactor": 1, "mobile": vp["width"] < 600}, session)
            media = [{"name": "prefers-color-scheme", "value": cell["theme"]}]
            if controls["reduced_motion"]:
                media.append({"name": "prefers-reduced-motion", "value": "reduce"})
            send("Emulation.setEmulatedMedia", {"features": media}, session)
            while not events.empty():
                events.get_nowait()
            send("Page.navigate", {"url": origin + cell["route"]}, session)
            deadline = time.monotonic() + 15
            while not state["loaded"] and time.monotonic() < deadline:
                drain(state, until=0.1)
            if not state["loaded"]:
                raise TimeoutError("page load did not finish")
            drain(state, until=3.0, quiet=0.3)
            evaluate("document.fonts ? document.fonts.ready.then(() => true) : true")
            for step in cell["steps"]:
                sel = json.dumps(step["selector"]); val = json.dumps(step["value"])
                if step["kind"] == "click":
                    evaluate(f"(() => {{ const e = document.querySelector({sel}); if (!e) throw new Error('no element ' + {sel}); e.click(); return true; }})()")
                elif step["kind"] == "hover":
                    evaluate(f"(() => {{ const e = document.querySelector({sel}); if (!e) throw new Error('no element'); e.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}})); return true; }})()")
                elif step["kind"] == "type":
                    evaluate(f"(() => {{ const e = document.querySelector({sel}); if (!e) throw new Error('no element'); e.focus(); e.value = {val}; e.dispatchEvent(new Event('input', {{bubbles: true}})); e.dispatchEvent(new Event('change', {{bubbles: true}})); return true; }})()")
                elif step["kind"] == "press":
                    for kind in ("keyDown", "keyUp"):
                        send("Input.dispatchKeyEvent", {"type": kind, "key": step["value"]}, session)
                drain(state, until=2.0 if step["kind"] != "wait" else min(int(step["value"] or "0") / 1000.0, 5.0), quiet=0.2 if step["kind"] != "wait" else None)
            drain(state, until=controls["settle_ms"] / 1000.0)
            dom = evaluate(cfg["dom"])
            shot = send("Page.captureScreenshot", {"format": "png", "fromSurface": True, "captureBeyondViewport": False}, session).get("data", "")
            name = f"shot-{len(result['pages'])}.png"
            with open(os.path.join(out_dir, name), "wb") as handle:
                handle.write(base64.b64decode(shot))
            if cfg["masks"]:
                evaluate(cfg["mask_text"])
            ax, ax_truncated = ax_lines()
            metrics = {m["name"]: m["value"] for m in send("Performance.getMetrics", {}, session).get("metrics", [])}
            drain(state, until=0.05)
            network = []
            for method, url, status in list(state["requests"].values())[: cfg["event_limit"]]:
                if url.split("?", 1)[0].endswith("/favicon.ico"):
                    continue
                network.append(f"{method} {url.replace(origin, 'self')} -> {status or 'pending'}")
            page.update({
                "status": "captured", "screenshot": name, "rects": dom.get("rects", []), "dom": dom.get("lines", []),
                "dom_truncated": bool(dom.get("truncated")), "ax": ax, "ax_truncated": ax_truncated,
                "network": network, "console": state["console"][: cfg["event_limit"]],
                "cls": float(dom.get("vitals", {}).get("cls") or 0), "lcp": float(dom.get("vitals", {}).get("lcp") or 0),
                "metrics": {k: metrics[k] for k in ("LayoutCount", "RecalcStyleCount", "ScriptDuration", "TaskDuration", "JSHeapUsedSize", "Nodes") if k in metrics},
            })
        except Exception as exc:
            page["reason"] = f"{type(exc).__name__}: {exc}"[:300]
        result["pages"].append(page)
except Exception as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"[:500]
finally:
    with open(os.path.join(out_dir, "result.json"), "w") as handle:
        json.dump(result, handle)
    try:
        chrome.kill()
    except Exception:
        pass
    server.shutdown()
"""


def page_scripts(controls: DeterminismControls) -> dict[str, str]:
    masks = json.dumps(list(controls.mask_selectors))
    return {
        "init": INIT_SCRIPT.replace("__LC_CONTROLS__", json.dumps(controls.to_wire())),
        "dom": DOM_SCRIPT.replace("__LC_MASKS__", masks).replace("__LC_LIMIT__", str(MAX_DOM_LINES)),
        "mask_text": MASK_TEXT_SCRIPT.replace("__LC_MASKS__", masks),
    }


def chrome_executable() -> str:
    """A Linux Chrome/Chromium that runs from read-only mounts (snap builds do not), or ``""``."""

    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if not found:
            continue
        resolved = str(Path(found).resolve())
        if resolved.startswith("/snap/"):
            continue
        try:
            head = Path(resolved).read_bytes()[:8192]
        except OSError:
            continue
        if head.startswith(b"#!") and b"/snap/" in head:
            continue  # a wrapper that launches a snap build, which cannot run from read-only mounts
        return resolved
    return ""


def fonts_digest(roots: Sequence[str] = ("/usr/share/fonts", "/usr/local/share/fonts", "/etc/fonts")) -> str:
    """Digest of the font files and configuration the sandboxed browser sees (names, sizes)."""

    # lc-debt: walks the host font tree on every environment probe; cache per process if probes get frequent.
    rows: list[str] = []
    for root in roots:
        for current, dirs, files in os.walk(root):
            dirs.sort()
            for name in sorted(files):
                path = Path(current, name)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                rows.append(f"{path}\0{size}")
                if len(rows) >= 50_000:
                    break
    return "fonts:sha256:" + hashlib.sha256("\n".join(rows).encode()).hexdigest()


class ChromeCdpDriver:
    """Chrome over the DevTools pipe, inside Bubblewrap with no network."""

    def __init__(self, *, chrome: str | None = None, bwrap: str | None = None) -> None:
        self.chrome = chrome if chrome is not None else chrome_executable()
        self.bwrap = bwrap if bwrap is not None else (shutil.which("bwrap") or shutil.which("bubblewrap") or "")
        self._version = ""

    def _chrome_mount(self) -> tuple[str, str]:
        """(host directory to mount, chrome path inside the sandbox)."""

        path = Path(self.chrome)
        if str(path).startswith(("/usr/", "/bin/", "/lib/")):
            return "", str(path)
        return str(path.parent), str(path)

    def environment(self) -> RenderEnvironment:
        if not self.bwrap:
            return RenderEnvironment(False, "rendering diffs need bubblewrap; reviewed pages never render unsandboxed")
        if not self.chrome:
            return RenderEnvironment(False, "rendering diffs need a non-snap Chrome/Chromium on this host")
        if not self._version:
            try:
                proc = subprocess.run(
                    [self.chrome, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                    env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return RenderEnvironment(False, f"browser version probe failed: {exc}")
            self._version = (proc.stdout or "").strip()[:120]
        if not self._version:
            return RenderEnvironment(False, "browser version probe returned nothing")
        return RenderEnvironment(True, browser=self._version, fonts_digest=fonts_digest())

    def _sandbox(self, site: Path, work: Path) -> list[str]:
        args = [
            self.bwrap,
            "--die-with-parent",
            # Every namespace bwrap can unshare, the network included: no egress, no host IPC.
            "--unshare-all",
            "--new-session",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/dev/shm",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/bin",
            "/bin",
            "--ro-bind",
            "/lib",
            "/lib",
        ]
        for optional in (
            "/lib64",
            "/etc/fonts",
            "/var/cache/fontconfig",
            "/etc/hosts",
            "/etc/nsswitch.conf",
            # Debian's /usr/bin/chromium is a launcher script that sources these.
            "/etc/chromium",
            "/etc/chromium.d",
        ):
            if Path(optional).exists():
                args.extend(("--ro-bind", optional, optional))
        mount, _inside = self._chrome_mount()
        if mount:
            args.extend(("--ro-bind", mount, mount))
        args.extend(
            (
                "--ro-bind",
                str(site),
                "/site",
                "--ro-bind",
                str(work / "in"),
                "/lemoncrow-render",
                "--bind",
                str(work / "out"),
                "/capture",
                "--tmpfs",
                "/tmp",
                "--clearenv",
                "--setenv",
                "HOME",
                "/tmp",
                "--setenv",
                "PATH",
                # /usr is mounted whole; the sandbox's python3 may live in /usr/local/bin (official Python images).
                "/usr/local/bin:/usr/bin:/bin",
                "--chdir",
                "/tmp",
                "--",
            )
        )
        return args

    def capture(
        self,
        site: Path,
        cells: Sequence[RenderCell],
        controls: DeterminismControls,
        *,
        spa_fallback: bool,
        timeout: float,
    ) -> list[PageCapture]:
        from lemoncrow.pro.environment.sandbox_limits import SandboxCapacityBusy, run_limited

        environment = self.environment()
        if not environment.available:
            return [PageCapture(cell.id, "failed", environment.reason) for cell in cells]
        work = Path(tempfile.mkdtemp(prefix="lemoncrow-render-"))
        try:
            (work / "in").mkdir()
            (work / "out").mkdir()
            _mount, inside = self._chrome_mount()
            config = {
                "site": "/site",
                "spa": bool(spa_fallback),
                "chrome": inside,
                "controls": controls.to_wire(),
                "cells": [cell.to_wire() for cell in cells],
                "masks": list(controls.mask_selectors),
                "ax_limit": MAX_AX_LINES,
                "event_limit": MAX_EVENTS,
                **page_scripts(controls),
            }
            (work / "in" / "config.json").write_text(json.dumps(config), encoding="utf-8")
            (work / "in" / "render.py").write_text(SANDBOX_SCRIPT, encoding="utf-8")
            command = [
                *self._sandbox(site.resolve(), work),
                "python3",
                "-I",
                "/lemoncrow-render/render.py",
                "/lemoncrow-render/config.json",
                "/capture",
            ]
            try:
                proc = run_limited(
                    command,
                    timeout=timeout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired, SandboxCapacityBusy) as exc:
                return [PageCapture(cell.id, "failed", f"render could not run: {exc}"[:300]) for cell in cells]
            return _read_captures(work / "out", cells, proc.stdout or "")
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _read_file(path: Path, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return b""
    with os.fdopen(fd, "rb") as handle:
        return handle.read(limit + 1)


def _strings(raw: object, limit: int, width: int = 400) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(item)[:width] for item in raw[:limit])


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and value == value else 0.0


def _capture_from_row(cell_id: str, row: Mapping[str, Any], screenshot: bytes) -> PageCapture:
    """One captured page from an untrusted row: every field typed and bounded here."""

    shot = screenshot if len(screenshot) <= MAX_SCREENSHOT_BYTES and screenshot.startswith(b"\x89PNG") else b""
    rects: list[tuple[int, int, int, int]] = []
    raw_rects = row.get("rects")
    for item in (raw_rects if isinstance(raw_rects, list) else [])[:64]:
        if isinstance(item, list) and len(item) == 4 and all(type(v) is int and 0 <= v <= 10_000 for v in item):
            rects.append((item[0], item[1], item[2], item[3]))
    metrics_raw = row.get("metrics")
    metrics = (
        {str(k)[:40]: float(v) for k, v in list(metrics_raw.items())[:16] if isinstance(v, (int, float))}
        if isinstance(metrics_raw, dict)
        else {}
    )
    return PageCapture(
        cell_id,
        "captured",
        screenshot=shot,
        mask_regions=tuple(rects),
        dom_lines=_strings(row.get("dom"), MAX_DOM_LINES),
        dom_truncated=bool(row.get("dom_truncated")),
        ax_lines=_strings(row.get("ax"), MAX_AX_LINES),
        ax_truncated=bool(row.get("ax_truncated")),
        network=_strings(row.get("network"), MAX_EVENTS),
        console=_strings(row.get("console"), MAX_EVENTS),
        cls=_number(row.get("cls")),
        lcp_ms=_number(row.get("lcp")),
        metrics=metrics,
    )


def capture_to_wire(capture: PageCapture) -> dict[str, Any]:
    """One capture for the executor boundary (same row names as the sandbox result)."""

    if capture.status != "captured":
        return {"cell": capture.cell_id, "status": "failed", "reason": capture.reason[:300]}
    return {
        "cell": capture.cell_id,
        "status": "captured",
        "screenshot_b64": base64.b64encode(capture.screenshot).decode("ascii"),
        "rects": [list(rect) for rect in capture.mask_regions],
        "dom": list(capture.dom_lines),
        "dom_truncated": capture.dom_truncated,
        "ax": list(capture.ax_lines),
        "ax_truncated": capture.ax_truncated,
        "network": list(capture.network),
        "console": list(capture.console),
        "cls": capture.cls,
        "lcp": capture.lcp_ms,
        "metrics": dict(capture.metrics),
    }


def capture_from_wire(cell_id: str, row: object) -> PageCapture:
    """The capture an executor returned for *cell_id*; anything malformed is a failed cell."""

    if not isinstance(row, Mapping) or row.get("cell") != cell_id:
        return PageCapture(cell_id, "failed", "the executor returned no capture for this cell")
    if row.get("status") != "captured":
        return PageCapture(cell_id, "failed", str(row.get("reason") or "not captured")[:300])
    encoded = row.get("screenshot_b64")
    if not isinstance(encoded, str) or len(encoded) > (MAX_SCREENSHOT_BYTES * 4) // 3 + 8:
        return PageCapture(cell_id, "failed", "the executor returned an invalid screenshot")
    try:
        screenshot = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return PageCapture(cell_id, "failed", "the executor returned an invalid screenshot")
    return _capture_from_row(cell_id, row, screenshot)


def _read_captures(out: Path, cells: Sequence[RenderCell], log: str) -> list[PageCapture]:
    """Bounded, type-checked captures from the sandbox output (all of it untrusted)."""

    payload = _read_file(out / "result.json", _MAX_RESULT_BYTES)
    try:
        result = json.loads(payload) if payload and len(payload) <= _MAX_RESULT_BYTES else None
    except ValueError:
        result = None
    if not isinstance(result, dict):
        reason = "render produced no result" + (f": {log.strip()[-300:]}" if log.strip() else "")
        return [PageCapture(cell.id, "failed", reason) for cell in cells]
    pages = {str(row.get("cell")): row for row in result.get("pages") or [] if isinstance(row, dict)}
    error = str(result.get("error") or "")[:300]
    captures: list[PageCapture] = []
    for cell in cells:
        row = pages.get(cell.id)
        if row is None or row.get("status") != "captured":
            reason = str((row or {}).get("reason") or error or "not captured")[:300]
            captures.append(PageCapture(cell.id, "failed", reason))
            continue
        name = str(row.get("screenshot") or "")
        shot = _read_file(out / Path(name).name, MAX_SCREENSHOT_BYTES) if name else b""
        captures.append(_capture_from_row(cell.id, row, shot))
    return captures


def screenshot_identity(png: bytes) -> str:
    """Pixel identity of a PNG (decoded RGBA when Pillow is present, else the bytes)."""

    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            image.load()
            rgba = image.convert("RGBA")
            return "px:sha256:" + hashlib.sha256(f"{rgba.width}x{rgba.height}".encode() + rgba.tobytes()).hexdigest()
    except Exception:
        return "png:sha256:" + hashlib.sha256(png).hexdigest()


__all__ = [
    "BoundRender",
    "ChromeCdpDriver",
    "DeterminismControls",
    "InteractionStep",
    "PageCapture",
    "RenderCell",
    "RenderDriver",
    "RenderEnvironment",
    "RenderExecutor",
    "RenderRequest",
    "RenderSource",
    "Viewport",
    "capture_from_wire",
    "capture_to_wire",
    "chrome_executable",
    "fonts_digest",
    "page_scripts",
    "screenshot_identity",
]
