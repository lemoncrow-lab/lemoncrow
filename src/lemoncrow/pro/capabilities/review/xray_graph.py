"""The X-Ray graph document of one Review revision (contract v1: ``docs/reference/xray-graph.md``).

A pure, deterministic producer. :func:`build_index_graph` turns one revision's changed files, changed symbols and
recorded before/after runs (:class:`XRayInputs`) into a component-level graph: routes, handlers, changed functions
and classes, UI components, datastores, externals and tests in lanes, with kinded and labelled edges, sequence
flows, views and a short walkthrough. :func:`finalize_graph` enforces every producer rule of the contract on any
graph, the index graph and the AI-enriched one alike (:mod:`xray_graph_ai`):

1. referential integrity, 2. grounding (every anchor resolves on its side), 3. evidence (``recorded`` needs a run
   in ``provenance.recorded_runs``; a recorded payload needs ``evidence_ref``), 4. redaction and string cuts,
5. determinism (stable ids, sorted arrays, canonical JSON), 6. caps, folding into ``+N more`` module nodes.

Nothing here reads a store, calls a model or looks at the clock: the same inputs give byte-identical JSON.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from lemoncrow.pro.capabilities.review.run_evidence import protect_text

GRAPH_VERSION: Final[int] = 1
#: Part of every cache stamp: bump it whenever the same inputs would give a different graph.
PRODUCER_VERSION: Final[str] = "xray-graph/2"

MAX_TITLE: Final[int] = 80
MAX_SUMMARY: Final[int] = 240
MAX_LANES: Final[int] = 8
MAX_NODES: Final[int] = 60
MAX_EDGES: Final[int] = 150
MAX_FLOWS: Final[int] = 6
MAX_VIEWS: Final[int] = 32
MAX_DEGRADED: Final[int] = 5
MAX_BADGES: Final[int] = 3
MAX_LABEL: Final[int] = 60
MAX_SUBTITLE: Final[int] = 60
MAX_EDGE_LABEL: Final[int] = 40
MAX_FLOW_TITLE: Final[int] = 60
MAX_FLOW_SUMMARY: Final[int] = 140
MIN_PARTICIPANTS: Final[int] = 2
MAX_PARTICIPANTS: Final[int] = 8
MAX_STEPS: Final[int] = 24
MAX_STEP_LABEL: Final[int] = 48
MAX_NOTE: Final[int] = 140
MAX_VIEW_TITLE: Final[int] = 60
MAX_VIEW_SUMMARY: Final[int] = 140
MIN_TOUR: Final[int] = 2
MAX_TOUR: Final[int] = 12
MAX_HEADING: Final[int] = 48
MAX_BODY: Final[int] = 140
MAX_PAYLOAD_TYPE: Final[int] = 60
MAX_SHAPE_BYTES: Final[int] = 2048
MAX_SAMPLE_BYTES: Final[int] = 4096
MAX_SAMPLE_STRING: Final[int] = 256
#: Files larger than this are anchored but not scanned for routes, imports or calls.
MAX_SCAN_CHARS: Final[int] = 1_000_000

Side = Literal["base", "head"]
SIDES: Final[tuple[str, ...]] = ("base", "head")
DELTAS: Final[frozenset[str]] = frozenset({"added", "modified", "removed", "unchanged"})
EVIDENCE: Final[frozenset[str]] = frozenset({"recorded", "static", "inferred"})
NODE_KINDS: Final[frozenset[str]] = frozenset(
    {"ui", "route", "handler", "function", "class", "module", "datastore", "queue", "external", "job", "config", "test"}
)
EDGE_KINDS: Final[frozenset[str]] = frozenset({"http", "call", "data", "event", "dependency", "render"})
STEP_KINDS: Final[frozenset[str]] = frozenset({"sync", "async", "return", "self"})

#: Lanes, left to right: callers first, then the code they reach, then what it stores or calls out to.
LANE_ORDER: Final[tuple[str, ...]] = ("tests", "ui", "api", "data", "external", "config")
_LANE_LABELS: Final[Mapping[str, str]] = {
    "tests": "Tests",
    "ui": "Web UI",
    "api": "Code",
    "data": "Data",
    "external": "External",
    "config": "Config",
}


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChangedFile:
    """One changed file of the revision (``status``: added, modified or removed)."""

    path: str
    status: str = "modified"
    old_path: str = ""
    additions: int = 0
    deletions: int = 0
    category: str = ""
    #: Changed line spans on each side, inclusive.
    head_ranges: tuple[tuple[int, int], ...] = ()
    base_ranges: tuple[tuple[int, int], ...] = ()

    @property
    def side(self) -> Side:
        return "base" if self.status == "removed" else "head"

    @property
    def side_path(self) -> str:
        return (self.old_path or self.path) if self.status == "removed" else self.path


@dataclass(frozen=True, slots=True)
class ChangedSymbol:
    """One changed definition (``change``: added, modified or removed)."""

    name: str
    kind: str
    path: str
    start_line: int
    end_line: int
    change: str = "modified"


@dataclass(frozen=True, slots=True)
class RecordedRun:
    """One before/after run on this revision (``provenance.recorded_runs``)."""

    ref: str
    recipe: str
    side: Side
    at: str


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One recorded request of a request surface (a Bruno file) on one side."""

    ref: str
    recipe: str
    side: Side
    surface: str
    title: str = ""
    method: str = ""
    route: str = ""
    status: int | None = None
    body: Any = None
    has_body: bool = False


@dataclass(frozen=True, slots=True)
class ResponseChange:
    """One ``api_response`` behavior target: the same request answered differently on base and head."""

    behavior_key: str
    recipe: str
    title: str
    summary: str
    base_ref: str
    head_ref: str
    body_change: float = 0.0


@dataclass(frozen=True, slots=True)
class ObservedTrace:
    """One call edge a runtime observation saw (caller/callee are repository paths)."""

    ref: str
    side: Side
    caller: str
    caller_fn: str
    callee: str
    callee_fn: str


@dataclass(frozen=True, slots=True)
class ObservedEffect:
    """One side effect difference of a runtime observation (``change``: added, removed, changed)."""

    ref: str
    category: str
    identity: str
    change: str
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class XRayInputs:
    review_id: str
    revision_id: str
    title: str
    generated_at: str
    files: tuple[ChangedFile, ...]
    symbols: tuple[ChangedSymbol, ...] = ()
    runs: tuple[RecordedRun, ...] = ()
    calls: tuple[RecordedCall, ...] = ()
    responses: tuple[ResponseChange, ...] = ()
    traces: tuple[ObservedTrace, ...] = ()
    effects: tuple[ObservedEffect, ...] = ()
    #: ``data_path`` report rows (``flows.added|removed|rerouted``), as stored.
    data_flows: tuple[Mapping[str, Any], ...] = ()
    degraded: tuple[str, ...] = ()


ReadText = Callable[[str, str], "str | None"]
"""``(side, path) -> text``; ``None`` (or ``""``) when the path does not exist on that side."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_digest(graph: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of the graph without its ``digest``."""

    body = {key: value for key, value in graph.items() if key != "digest"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def clip(text: object, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def stable_id(prefix: str, *parts: object) -> str:
    return prefix + hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:12]


def _ext(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def _name(path: str) -> str:
    return posixpath.basename(path)


def _stem(path: str) -> str:
    name = _name(path)
    return name.split(".", 1)[0] if not name.startswith(".") else name


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _line_count(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _churn(additions: int, deletions: int) -> str:
    return f"+{additions} / -{deletions}"


_DELTA_RANK: Final[Mapping[str, int]] = {"unchanged": 0, "modified": 1, "added": 2, "removed": 2}
_EVIDENCE_RANK: Final[Mapping[str, int]] = {"inferred": 0, "static": 1, "recorded": 2}


def _stronger_delta(left: str, right: str) -> str:
    return left if _DELTA_RANK.get(left, 0) >= _DELTA_RANK.get(right, 0) else right


def _relation_delta(left: str, right: str) -> str:
    """An edge between two components is new when either end is new, and gone when either end is gone."""

    if "removed" in (left, right):
        return "removed"
    if "added" in (left, right):
        return "added"
    return "unchanged"


def anchor(path: str, side: str, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"path": path, "side": side}
    if start is not None and start >= 1:
        row["start_line"] = start
        row["end_line"] = max(start, end if end is not None else start)
    return row


# --------------------------------------------------------------------------- #
# anchors: every one must resolve on its side of the revision
# --------------------------------------------------------------------------- #


class AnchorIndex:
    """Reads each (side, path) of the revision at most once and resolves anchors against it."""

    def __init__(self, read_text: ReadText) -> None:
        self._read = read_text
        self._texts: dict[tuple[str, str], str | None] = {}
        self.oversized: set[str] = set()

    def text(self, side: str, path: str) -> str | None:
        key = (side, path)
        if key not in self._texts:
            value: str | None = None
            if side in SIDES and path:
                try:
                    value = self._read(side, path)
                except (OSError, ValueError, LookupError):
                    value = None
            self._texts[key] = value or None
        return self._texts[key]

    def scan_text(self, side: str, path: str) -> str:
        """The text to scan for structure; bounded, so a huge generated file is anchored but not parsed."""

        text = self.text(side, path) or ""
        if len(text) > MAX_SCAN_CHARS:
            self.oversized.add(path)
            return ""
        return text

    def lines(self, side: str, path: str) -> int:
        text = self.text(side, path)
        return _line_count(text) if text is not None else -1

    def resolves(self, raw: object) -> bool:
        if not isinstance(raw, Mapping):
            return False
        path, side = raw.get("path"), raw.get("side")
        if not isinstance(path, str) or not isinstance(side, str) or side not in SIDES:
            return False
        count = self.lines(side, path)
        if count < 0:
            return False
        start, end = raw.get("start_line"), raw.get("end_line")
        if start is None and end is None:
            return True
        first = _int(start)
        last = _int(end) if end is not None else first
        return first is not None and last is not None and 1 <= first <= last <= count

    def clean(self, rows: object) -> list[dict[str, Any]]:
        """The anchors of ``rows`` that resolve, normalized and de-duplicated, in a stable order."""

        out: dict[str, dict[str, Any]] = {}
        for raw in rows if isinstance(rows, list | tuple) else ():
            if not self.resolves(raw):
                continue
            row = anchor(str(raw["path"]), str(raw["side"]), _int(raw.get("start_line")), _int(raw.get("end_line")))
            out.setdefault(canonical_json(row), row)
        return [out[key] for key in sorted(out)]


# --------------------------------------------------------------------------- #
# redaction and caps of payload samples
# --------------------------------------------------------------------------- #


def _redact(value: Any, depth: int = 0) -> tuple[Any, int]:
    if isinstance(value, str):
        text, masked = protect_text(value)
        masked += value.count("<redacted-")  # already masked where the run was recorded
        if len(text) > MAX_SAMPLE_STRING:
            text = text[: MAX_SAMPLE_STRING - 1] + "…"
        return text, masked
    if value is None or isinstance(value, bool | int):
        return value, 0
    if isinstance(value, float):
        return (value if math.isfinite(value) else None), 0
    if depth >= 16:
        return "…", 0
    total = 0
    if isinstance(value, Mapping):
        mapping: dict[str, Any] = {}
        for key, item in value.items():
            clean_key, masked_key = _redact(str(key), depth + 1)
            clean_item, masked_item = _redact(item, depth + 1)
            mapping[str(clean_key)] = clean_item
            total += masked_key + masked_item
        return mapping, total
    if isinstance(value, list | tuple):
        items: list[Any] = []
        for item in value:
            clean_item, masked_item = _redact(item, depth + 1)
            items.append(clean_item)
            total += masked_item
        return items, total
    return _redact(str(value), depth)


def _sample_size(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _trim(value: Any, items: int) -> Any:
    if isinstance(value, Mapping):
        keys = list(value)
        out = {key: _trim(value[key], items) for key in keys[:items]}
        if len(keys) > items:
            out["…"] = f"{len(keys) - items} more keys"
        return out
    if isinstance(value, list):
        trimmed = [_trim(item, items) for item in value[:items]]
        if len(value) > items:
            trimmed.append(f"… {len(value) - items} more")
        return trimmed
    if isinstance(value, str) and len(value) > items * 8:
        return value[: max(8, items * 8)] + "…"
    return value


def redact_sample(value: Any) -> tuple[Any, bool]:
    """A JSON sample with secrets and personal data masked, strings cut at 256 characters and the whole
    serialised at most 4096 bytes. Returns ``(sample, masked)``."""

    clean, masked = _redact(value)
    if _sample_size(clean) <= MAX_SAMPLE_BYTES:
        return clean, masked > 0
    for items in (32, 16, 8, 4, 2, 1):
        trimmed = _trim(clean, items)
        if _sample_size(trimmed) <= MAX_SAMPLE_BYTES:
            return trimmed, masked > 0
    return {"truncated": True, "bytes": _sample_size(clean)}, masked > 0


def _shape(value: Any, depth: int = 0) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if depth >= 4:
        return "…"
    if isinstance(value, list):
        return (_shape(value[0], depth + 1) + "[]") if value else "[]"
    if isinstance(value, Mapping):
        keys = list(value)
        fields = [f"{key}: {_shape(value[key], depth + 1)}" for key in keys[:24]]
        if len(keys) > 24:
            fields.append("…")
        return "{ " + "; ".join(fields) + " }" if fields else "{}"
    return "unknown"


def type_sketch(value: Any) -> str:
    """A TypeScript-like sketch of a JSON value, at most 2048 bytes."""

    encoded = _shape(value).encode("utf-8")
    if len(encoded) <= MAX_SHAPE_BYTES:
        return encoded.decode("utf-8")
    return encoded[: MAX_SHAPE_BYTES - 3].decode("utf-8", errors="ignore") + "…"


def _clean_payload(raw: object, refs: frozenset[str]) -> dict[str, Any] | None:
    """A contract payload, or ``None`` when it may not be kept (a recorded one needs a known ``evidence_ref``)."""

    if not isinstance(raw, Mapping):
        return None
    evidence = raw.get("evidence")
    ref = str(raw.get("evidence_ref") or "")
    if evidence not in ("recorded", "inferred") or (evidence == "recorded" and ref not in refs):
        return None
    out: dict[str, Any] = {"evidence": evidence, "redacted": raw.get("redacted") is True}
    for key in ("before", "after"):
        if key in raw:
            sample, masked = redact_sample(raw[key])
            out[key] = sample
            out["redacted"] = out["redacted"] or masked
    if raw.get("type"):
        out["type"] = clip(raw["type"], MAX_PAYLOAD_TYPE)
    if raw.get("shape"):
        shape = str(raw["shape"]).encode("utf-8")
        out["shape"] = shape[:MAX_SHAPE_BYTES].decode("utf-8", errors="ignore")
    status = raw.get("status")
    if isinstance(status, Mapping):
        codes = {key: _int(status.get(key)) for key in ("before", "after")}
        kept = {key: value for key, value in codes.items() if value is not None}
        if kept:
            out["status"] = kept
    if ref:
        out["evidence_ref"] = ref
    if raw.get("behavior_key"):
        out["behavior_key"] = str(raw["behavior_key"])
    return out


# --------------------------------------------------------------------------- #
# files: what component a changed file is
# --------------------------------------------------------------------------- #

_DOC_EXT: Final[frozenset[str]] = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})
_MEDIA_EXT: Final[frozenset[str]] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".mp4", ".mov", ".webm", ".mp3", ".wav"}
    | {".woff", ".woff2", ".ttf", ".otf", ".eot", ".pdf", ".zip", ".gz"}
)
_LOCK_NAMES: Final[frozenset[str]] = frozenset(
    {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "cargo.lock", "go.sum"}
)
_CONFIG_NAMES: Final[frozenset[str]] = frozenset(
    {"compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml", "makefile", "package.json"}
    | {"tsconfig.json", "pyproject.toml", "setup.cfg", "setup.py", "requirements.txt", "procfile", "bruno.json"}
)
_CONFIG_DIRS: Final[frozenset[str]] = frozenset({".github", ".lemoncrow", ".circleci", ".gitlab", "environments"})
_STYLE_EXT: Final[frozenset[str]] = frozenset({".css", ".scss", ".sass", ".less"})
_UI_EXT: Final[frozenset[str]] = frozenset({".jsx", ".tsx", ".vue", ".svelte"})
_SCRIPT_EXT: Final[frozenset[str]] = frozenset({".js", ".mjs", ".cjs", ".ts"})
_UI_DIRS: Final[frozenset[str]] = frozenset({"frontend", "web", "ui", "client", "components", "pages"})
_CODE_EXT: Final[frozenset[str]] = _SCRIPT_EXT | frozenset(
    {".py", ".go", ".java", ".kt", ".rb", ".rs", ".php", ".cs", ".scala", ".swift", ".ex", ".exs"}
)
_DATA_EXT: Final[frozenset[str]] = frozenset(
    {".csv", ".tsv", ".sql", ".parquet", ".sqlite", ".db", ".jsonl", ".ndjson", ".json", ".xml"}
)
_CONFIG_EXT: Final[frozenset[str]] = frozenset(
    {".toml", ".ini", ".cfg", ".yml", ".yaml", ".env", ".conf", ".tf", ".hcl", ".properties", ".sh"}
)
_TEST_DIRS: Final[frozenset[str]] = frozenset({"tests", "test", "__tests__", "spec", "specs", "e2e"})
_TEST_SUFFIXES: Final[tuple[str, ...]] = ("_test.py", "_test.go", "_spec.rb", "test.java", "tests.py")

FileRole = Literal["skip", "test", "ui", "style", "datastore", "config", "code"]


def classify(path: str, category: str = "") -> FileRole:
    """The component role of one file, from its path alone (documentation, media and lock files are skipped)."""

    name = _name(path).lower()
    ext = _ext(path)
    parts = frozenset(path.lower().split("/")[:-1])
    if name in _LOCK_NAMES:
        return "skip"
    if name in _CONFIG_NAMES or name.startswith(("dockerfile", ".env")) or parts & _CONFIG_DIRS:
        return "config"
    if ext in _DOC_EXT or ext in _MEDIA_EXT or category in ("docs", "generated", "vendor"):
        return "skip"
    is_test = (
        ext == ".bru"
        or ".stories." in name
        or ".test." in name
        or ".spec." in name
        or name.startswith("test_")
        or name.endswith(_TEST_SUFFIXES)
        or bool(parts & _TEST_DIRS)
        or category == "test"
    )
    if is_test:
        return "test"
    if ext in _STYLE_EXT:
        return "style"
    if ext in _UI_EXT or (ext in _SCRIPT_EXT and bool(parts & _UI_DIRS)):
        return "ui"
    if ext in _DATA_EXT:
        return "config" if name.startswith(".") else "datastore"
    if ext in _CONFIG_EXT or category == "config":
        return "config"
    if ext in _CODE_EXT:
        return "code"
    return "skip"


# --------------------------------------------------------------------------- #
# parsing: routes, definitions, imports, request files (bounded regexes, no execution)
# --------------------------------------------------------------------------- #

_PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(")
_PY_CLASS = re.compile(r"^(\s*)class\s+(\w+)\b")
_JS_DEF = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\s*\*?\s*(\w+)\s*\(|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>)"
)
_PY_DECORATOR_ROUTE = re.compile(
    r"^\s*@[\w.]+\.(get|post|put|patch|delete|route|api_route|websocket)\(\s*[rbuf]?([\"'])(/[^\"']*)\2(.*)$"
)
_PY_METHODS = re.compile(r"methods\s*=\s*[\[(]\s*[\"'](\w+)[\"']")
_ROUTE_TABLE = re.compile(r"^\s*[\"'](/[^\"'\s]*)[\"']\s*:\s*([A-Za-z_][\w.]*)\s*,?\s*(?:#.*)?$")
_PATH_COMPARE = re.compile(r"\bpath\s*==\s*[\"'](/[^\"']*)[\"']")
_HTTP_SERVER_METHOD = re.compile(r"\bdef\s+do_([A-Z]+)\s*\(")
_JS_ROUTE = re.compile(
    r"\b(?:app|router|server|api|routes)\.(get|post|put|patch|delete|all)\(\s*([\"'`])(/[^\"'`]*)\2\s*(?:,([^)\n]*))?"
)
_DJANGO_ROUTE = re.compile(r"\b(?:re_)?path\(\s*r?[\"']([^\"']*)[\"']\s*,\s*([\w.]+)")
#: A request call whose first argument is the literal that follows (``fetch(``, ``axios.post(``, ``api.get(``).
_REQUEST_CALL = re.compile(
    r"(?:\bfetch|\baxios(?:\.(get|post|put|patch|delete|head))?|\$http\.(get|post|put|patch|delete|head)"
    r"|\b(?:api|client|http|request|instance|ky|got|superagent)\.(get|post|put|patch|delete|head))\s*\(\s*$"
)
_REQUEST_METHOD = re.compile(r"\bmethod\s*:\s*[\"'`](\w+)[\"'`]")
#: Client-side navigation targets (a router link, a redirect), which name pages rather than requests.
_NAVIGATION = re.compile(r"\b(?:path|to|href|src|action|navigate|redirect|push|replace)\s*(?:=\s*\{?|:|\()\s*$")
_URL_PREFIX = re.compile(r"^(?:\$\{\}|https?://[^/\s]*)+")
_PARAM_SEGMENT = re.compile(r"^(?:\{[^}]*\}|:\w+\??|<[^>]*>|\[[^\]]*\]|\*)$")
_ASSIGNED = re.compile(r"^\s*(?:export\s+)?(?:const\s+|let\s+|var\s+)?([A-Za-z_$][\w$]*)\s*(?::[^=]*)?=(?!=)")
_WRITE_HINT = re.compile(r"\bwrite|\bdump\(|\.to_(?:csv|json|parquet)\(|writeFile|appendFile|[\"'][wa]b?\+?[\"']")
_JS_IMPORT = re.compile(
    r"^\s*import\s+(?:([\w$]+)\s*,?\s*)?(?:\{([^}]*)\}\s*)?(?:\*\s+as\s+[\w$]+\s*)?(?:from\s+)?[\"']([^\"']+)[\"']",
    re.M,
)
_JS_REQUIRE = re.compile(r"\brequire\(\s*[\"']([^\"']+)[\"']\s*\)")
_PY_FROM_IMPORT = re.compile(r"^\s*from\s+([.\w]+)\s+import\s+\(?([\w,\s]+)\)?", re.M)
_PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+)", re.M)
_BRU_METHOD = re.compile(r"^(get|post|put|patch|delete|head|options)\s*\{\s*$", re.M | re.I)
_BRU_URL = re.compile(r"^\s*url:\s*(\S+)\s*$", re.M)
_BRU_NAME = re.compile(r"^\s*name:\s*(.+?)\s*$", re.M)
_SUMMARY_REQUEST = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)")
_COMMON_NAMES: Final[frozenset[str]] = frozenset(
    {"main", "run", "get", "set", "init", "setup", "test", "handler", "index", "app", "render", "default"}
)


def url_path(url: str) -> str:
    """``{{baseUrl}}/api/plans?x=1`` or ``https://host/api/plans`` to ``/api/plans``."""

    value = re.sub(r"^\{\{[^}]*\}\}", "", url.strip())
    if value.startswith(("http://", "https://")):
        value = urlsplit(value).path or "/"
    value = value.split("?", 1)[0].split("#", 1)[0]
    return value if value.startswith("/") else "/" + value


def parse_request_file(text: str) -> tuple[str, str, str, int]:
    """A Bruno request file's ``(name, METHOD, /path, url line)``; empty strings when it is not a request."""

    method = _BRU_METHOD.search(text)
    url = _BRU_URL.search(text)
    name = _BRU_NAME.search(text)
    if method is None or url is None:
        return "", "", "", 0
    line = text.count("\n", 0, url.start()) + 1
    return (name.group(1) if name else ""), method.group(1).upper(), url_path(url.group(1)), line


@dataclass(frozen=True, slots=True)
class Definition:
    name: str
    kind: str  # function | class
    start: int
    end: int


def definitions(path: str, text: str) -> tuple[Definition, ...]:
    """Top-level and nested function/class definitions with their line spans (Python by indentation)."""

    lines = text.splitlines()
    out: list[Definition] = []
    python = _ext(path) == ".py"
    for index, line in enumerate(lines):
        if python:
            match = _PY_DEF.match(line) or _PY_CLASS.match(line)
            if match is None:
                continue
            indent = len(match.group(1))
            end = index
            for later in range(index + 1, len(lines)):
                stripped = lines[later].strip()
                if not stripped:
                    continue
                if len(lines[later]) - len(lines[later].lstrip()) <= indent and not stripped.startswith(
                    (")", "]", "}")
                ):
                    break
                end = later
            kind = "class" if line.lstrip().startswith("class") else "function"
            out.append(Definition(match.group(2), kind, index + 1, end + 1))
        else:
            js = _JS_DEF.match(line)
            if js is not None:
                out.append(Definition(js.group(1) or js.group(2) or "", "function", index + 1, index + 1))
    return tuple(item for item in out if item.name)


def _enclosing(defs: Sequence[Definition], line: int) -> Definition | None:
    found = [item for item in defs if item.start <= line <= item.end and item.kind == "function"]
    return max(found, key=lambda item: item.start) if found else None


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    path: str
    handler: str
    line: int
    framework: str

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}" if self.method else self.path


def framework_of(path: str, text: str) -> str:
    if "fastapi" in text.lower() and ("FastAPI(" in text or "APIRouter(" in text):
        return "FastAPI"
    if re.search(r"^\s*(?:from|import)\s+flask\b", text, re.M):
        return "Flask"
    if "django" in text:
        return "Django"
    if "BaseHTTPRequestHandler" in text:
        return "http.server"
    if re.search(r"[\"']express[\"']", text):
        return "Express"
    if "@RestController" in text or "@RequestMapping" in text:
        return "Spring"
    return ""


def routes(path: str, text: str) -> tuple[Route, ...]:
    """HTTP routes one source file declares, with the function that handles each."""

    if not text:
        return ()
    framework = framework_of(path, text)
    defs = definitions(path, text)
    lines = text.splitlines()
    server_methods = sorted(set(_HTTP_SERVER_METHOD.findall(text)))
    table_method = server_methods[0] if len(server_methods) == 1 else ""
    found: dict[tuple[str, str], Route] = {}

    def add(route: Route) -> None:
        found.setdefault((route.method, route.path), route)

    for index, line in enumerate(lines):
        number = index + 1
        decorated = _PY_DECORATOR_ROUTE.match(line)
        if decorated is not None:
            verb, route_path, rest = decorated.group(1), decorated.group(3), decorated.group(4)
            method = verb.upper() if verb not in ("route", "api_route", "websocket") else ""
            listed = _PY_METHODS.search(rest)
            if not method:
                method = listed.group(1).upper() if listed else ("GET" if verb == "route" else "")
            handler = next((item.name for item in defs if item.start > number and item.kind == "function"), "")
            add(Route(method, route_path, handler, number, framework or "Python"))
            continue
        table = _ROUTE_TABLE.match(line)
        if table is not None and _ext(path) == ".py":
            add(Route(table_method, table.group(1), table.group(2).rsplit(".", 1)[-1], number, framework or "Python"))
            continue
        for compare in _PATH_COMPARE.finditer(line):
            owner = _enclosing(defs, number)
            method = owner.name[3:] if owner is not None and owner.name.startswith("do_") else ""
            add(Route(method, compare.group(1), owner.name if owner else "", number, framework or "Python"))
        for js in _JS_ROUTE.finditer(line):
            args = [part.strip() for part in (js.group(4) or "").split(",") if part.strip()]
            handler = args[-1] if args and re.fullmatch(r"[\w$.]+", args[-1]) else ""
            method = "" if js.group(1) == "all" else js.group(1).upper()
            add(Route(method, js.group(3), handler.rsplit(".", 1)[-1], number, framework or "Express"))
        if _name(path) == "urls.py":
            for django in _DJANGO_ROUTE.finditer(line):
                route_path = "/" + django.group(1).lstrip("^/").rstrip("$")
                add(Route("", route_path, django.group(2).rsplit(".", 1)[-1], number, "Django"))
    return tuple(found[key] for key in sorted(found))


@dataclass(frozen=True, slots=True)
class StringLiteral:
    """One string literal outside comments: its text (``${}`` per template substitution), line and offset."""

    value: str
    line: int
    offset: int


def string_literals(path: str, text: str) -> tuple[StringLiteral, ...]:
    """Every string literal of a Python or JS-family source file, comments skipped.

    One pass that tracks quotes, ``#`` / ``//`` / ``/* */`` comments, Python triple quotes and JS template
    substitutions. An unterminated ``'`` or ``"`` string ends at its line.
    """

    # lc-debt: a lexer, not a parser (a JS regex literal holding a quote can desync the rest of its line);
    # upgrade path: the code index's syntax-tree string nodes.
    python = _ext(path) in (".py", ".pyi")
    out: list[StringLiteral] = []
    size = len(text)
    index, line = 0, 1
    while index < size:
        char = text[index]
        if char == "\n":
            line += 1
            index += 1
            continue
        if (python and char == "#") or (not python and text.startswith("//", index)):
            stop = text.find("\n", index)
            index = size if stop < 0 else stop
            continue
        if not python and text.startswith("/*", index):
            stop = text.find("*/", index + 2)
            end = size if stop < 0 else stop + 2
            line += text.count("\n", index, end)
            index = end
            continue
        if char not in "'\"`" or (python and char == "`"):
            index += 1
            continue
        start, start_line = index, line
        if python and text.startswith(char * 3, index):
            stop = text.find(char * 3, index + 3)
            end = size if stop < 0 else stop
            value = text[index + 3 : end]
            line += value.count("\n")
            out.append(StringLiteral(value, start_line, start))
            index = end + 3
            continue
        parts: list[str] = []
        closed = False
        index += 1
        while index < size:
            current = text[index]
            if current == "\\" and index + 1 < size:
                if text[index + 1] == "\n":
                    line += 1
                parts.append(text[index + 1])
                index += 2
                continue
            if current == char:
                closed = True
                index += 1
                break
            if current == "\n":
                if char != "`":
                    break  # the outer loop counts this newline
                line += 1
            if char == "`" and text.startswith("${", index):
                depth, index = 1, index + 2
                while index < size and depth:
                    inner = text[index]
                    if inner == "{":
                        depth += 1
                    elif inner == "}":
                        depth -= 1
                    elif inner == "\n":
                        line += 1
                    index += 1
                parts.append("${}")
                continue
            parts.append(current)
            index += 1
        if closed:
            out.append(StringLiteral("".join(parts), start_line, start))
    return tuple(out)


def route_path_matches(route_path: str, request_path: str, *, loose: bool) -> bool:
    """Whether a request path reaches a declared route: equal, or (``loose``) equal up to path parameters."""

    declared = route_path.rstrip("/") or "/"
    if declared == request_path:
        return True
    if not loose or declared == "/":
        return False
    left, right = declared.strip("/").split("/"), request_path.strip("/").split("/")
    return len(left) == len(right) and all(
        want == got or (bool(got) and _PARAM_SEGMENT.match(want) is not None)
        for want, got in zip(left, right, strict=True)
    )


def route_references(
    path: str, text: str, literals: Sequence[StringLiteral] | None = None
) -> tuple[tuple[str, str, int, bool], ...]:
    """``(METHOD or "", /path, line, called)`` of each literal in client code that reads as a request path.

    ``called`` marks the first argument of a request call (``fetch``, ``axios``, ``api.get`` ...), which may
    match a route up to its path parameters; a bare literal (a URL constant) has no method and must match
    exactly. Literals on lines that declare routes, and navigation targets (``to=``, ``href=``,
    ``navigate(``), are not requests.
    """

    declared = {route.line for route in routes(path, text)}
    out: list[tuple[str, str, int, bool]] = []
    for literal in literals if literals is not None else string_literals(path, text):
        value = _URL_PREFIX.sub("", literal.value.strip())
        if literal.line in declared or not value.startswith("/") or value.startswith("//") or len(value) > 256:
            continue
        if any(char.isspace() for char in value):
            continue
        value = re.split(r"[?#]", value, maxsplit=1)[0].rstrip("/")
        if not value:
            continue
        before = text[max(0, literal.offset - 64) : literal.offset]
        call = _REQUEST_CALL.search(before)
        if call is None and _NAVIGATION.search(before):
            continue
        method = ""
        if call is not None:
            method = next((group for group in call.groups() if group), "").upper()
            if not method:
                named = _REQUEST_METHOD.search(text, literal.offset, literal.offset + 240)
                method = named.group(1).upper() if named else "GET"
        out.append((method, value, literal.line, call is not None))
    return tuple(out)


def imports(path: str, text: str) -> tuple[tuple[str, tuple[str, ...], int], ...]:
    """``(module spec, imported names, line)`` of each import statement."""

    out: list[tuple[str, tuple[str, ...], int]] = []
    if _ext(path) == ".py":
        for match in _PY_FROM_IMPORT.finditer(text):
            names = tuple(part.strip().split(" ")[0] for part in match.group(2).split(",") if part.strip())
            out.append((match.group(1), names, text.count("\n", 0, match.start()) + 1))
        for match in _PY_IMPORT.finditer(text):
            out.append((match.group(1), (), text.count("\n", 0, match.start()) + 1))
        return tuple(out)
    for match in _JS_IMPORT.finditer(text):
        named = tuple(
            part.strip().split(" as ")[0].strip() for part in (match.group(2) or "").split(",") if part.strip()
        )
        names = ((match.group(1),) if match.group(1) else ()) + named
        out.append((match.group(3), names, text.count("\n", 0, match.start()) + 1))
    for match in _JS_REQUIRE.finditer(text):
        out.append((match.group(1), (), text.count("\n", 0, match.start()) + 1))
    return tuple(out)


_JS_RESOLVE_SUFFIXES: Final[tuple[str, ...]] = (
    "",
    ".jsx",
    ".tsx",
    ".js",
    ".ts",
    ".vue",
    ".svelte",
    ".json",
    "/index.jsx",
    "/index.tsx",
    "/index.js",
    "/index.ts",
)


def resolve_import(importer: str, spec: str, known: Iterable[str]) -> str:
    """The known (changed) file an import names, or ``""``."""

    paths = sorted(set(known))
    if _ext(importer) == ".py":
        if spec.startswith("."):
            dots = len(spec) - len(spec.lstrip("."))
            base = posixpath.dirname(importer)
            for _ in range(dots - 1):
                base = posixpath.dirname(base)
            module = posixpath.join(base, spec.lstrip(".").replace(".", "/")) if spec.lstrip(".") else base
            candidates = (module + ".py", module + "/__init__.py")
            return next((item for item in candidates if item in paths), "")
        tail = spec.replace(".", "/")
        matches = [
            item
            for item in paths
            if item == tail + ".py"
            or item.endswith("/" + tail + ".py")
            or item == tail + "/__init__.py"
            or item.endswith("/" + tail + "/__init__.py")
        ]
        if not matches:
            return ""
        own = posixpath.dirname(importer).split("/")

        def shared(item: str) -> int:
            other = item.split("/")
            count = 0
            for left, right in zip(own, other, strict=False):
                if left != right:
                    break
                count += 1
            return count

        return max(matches, key=lambda item: (shared(item), item))
    if not spec.startswith("."):
        return ""
    target = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    return next((target + suffix for suffix in _JS_RESOLVE_SUFFIXES if target + suffix in paths), "")


def component_name(path: str, text: str) -> str:
    """The component a UI file defines: its default export, else its first capitalized function."""

    default = re.search(r"^\s*export\s+default\s+(?:function\s+|class\s+)?([A-Z][\w$]*)", text, re.M)
    if default:
        return default.group(1)
    named = re.search(r"^\s*(?:export\s+)?(?:function\s+|class\s+|const\s+)([A-Z][\w$]*)", text, re.M)
    return named.group(1) if named else _stem(path)


# --------------------------------------------------------------------------- #
# the index graph
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Node:
    id: str
    label: str
    kind: str
    lane: str
    delta: str
    anchors: list[dict[str, Any]]
    subtitle: str = ""
    badges: list[str] = field(default_factory=list)
    source: str = "static"
    path: str = ""
    name: str = ""
    churn: int = 0

    def to_wire(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "lane": self.lane,
            "delta": self.delta,
            "badges": list(dict.fromkeys(self.badges))[:MAX_BADGES],
            "anchors": self.anchors,
            "source": self.source,
        }
        if self.subtitle:
            row["subtitle"] = self.subtitle
        return row


@dataclass(slots=True)
class _Edge:
    source: str
    target: str
    kind: str
    label: str
    delta: str
    anchors: list[dict[str, Any]]
    evidence: str = "static"
    recorded: dict[str, Any] | None = None
    score: float = 0.0

    @property
    def id(self) -> str:
        return stable_id("e_", self.source, self.target, self.kind)

    def to_wire(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "from": self.source,
            "to": self.target,
            "kind": self.kind,
            "delta": self.delta,
            "evidence": self.evidence,
            "anchors": self.anchors,
        }
        if self.label:
            row["label"] = self.label
        if self.recorded is not None:
            row["recorded"] = self.recorded
        return row


class _Graph:
    """The index graph while it is being assembled."""

    def __init__(self, inputs: XRayInputs, texts: AnchorIndex) -> None:
        self.inputs = inputs
        self.texts = texts
        self.nodes: dict[str, _Node] = {}
        self.edges: dict[str, _Edge] = {}
        self.flows: list[dict[str, Any]] = []
        self.degraded: list[str] = list(inputs.degraded)
        self.platforms: dict[str, set[str]] = {}
        self.handler_of: dict[str, str] = {}  # route node -> handler node
        self.files: dict[str, ChangedFile] = {item.path: item for item in inputs.files}
        self.file_nodes: dict[str, list[str]] = {}  # path -> nodes anchored there, primary first
        self.route_candidates: list[_RouteCandidate] = []
        self.route_nodes: dict[tuple[str, str], str] = {}  # (METHOD, path) -> route node
        self.route_refs: list[_RouteRef] = []  # request paths client code names as literals
        self._literals: dict[tuple[str, str], tuple[StringLiteral, ...]] = {}

    def literals(self, side: str, path: str) -> tuple[StringLiteral, ...]:
        """The string literals of one file side, scanned once."""

        key = (side, path)
        if key not in self._literals:
            self._literals[key] = string_literals(path, self.texts.scan_text(side, path))
        return self._literals[key]

    # -- nodes and edges -------------------------------------------------- #

    def node(self, node: _Node) -> _Node:
        existing = self.nodes.get(node.id)
        if existing is not None:
            existing.delta = _stronger_delta(existing.delta, node.delta)
            existing.anchors = _merge_anchors(existing.anchors, node.anchors)
            return existing
        self.nodes[node.id] = node
        if node.path:
            self.file_nodes.setdefault(node.path, []).append(node.id)
        return node

    def edge(self, edge: _Edge) -> _Edge | None:
        if edge.source == edge.target or edge.source not in self.nodes or edge.target not in self.nodes:
            return None
        existing = self.edges.get(edge.id)
        if existing is None:
            self.edges[edge.id] = edge
            return edge
        existing.delta = _stronger_delta(existing.delta, edge.delta)
        existing.anchors = _merge_anchors(existing.anchors, edge.anchors)
        existing.label = existing.label or edge.label
        existing.score = max(existing.score, edge.score)
        if _EVIDENCE_RANK[edge.evidence] > _EVIDENCE_RANK[existing.evidence]:
            existing.evidence, existing.recorded = edge.evidence, edge.recorded
        return existing

    def platform(self, lane: str, name: str) -> None:
        if name:
            self.platforms.setdefault(lane, set()).add(name)

    def changed(self, path: str, side: str, line: int, last: int | None = None) -> bool:
        """Whether a changed span of ``path`` (keyed by its head path) touches ``line`` (through ``last``)."""

        item = self.files.get(path)
        if item is None:
            return False
        spans = item.head_ranges if side == "head" else item.base_ranges
        end = line if last is None else last
        return any(start <= end and line <= stop for start, stop in spans)

    def span_text(self, node: _Node) -> str:
        """The text of a node's own definition (its first anchor's span), else its whole file."""

        if not node.anchors:
            return ""
        first = node.anchors[0]
        text = self.texts.scan_text(str(first["side"]), str(first["path"]))
        start, end = _int(first.get("start_line")), _int(first.get("end_line"))
        if start is None or end is None or node.kind in ("test", "ui", "module"):
            return text
        return "\n".join(text.splitlines()[start - 1 : end])


def _merge_anchors(left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in (*left, *right):
        out.setdefault(canonical_json(row), dict(row))
    return [out[key] for key in sorted(out)]


def _file_delta(item: ChangedFile) -> str:
    return {"added": "added", "removed": "removed"}.get(item.status, "modified")


def _symbol_delta(change: str) -> str:
    return {"added": "added", "removed": "removed", "deleted": "removed"}.get(change, "modified")


def _file_badges(item: ChangedFile) -> list[str]:
    badges = [_churn(item.additions, item.deletions)] if item.additions or item.deletions else []
    if item.status == "added":
        badges.append("new")
    elif item.status == "removed":
        badges.append("removed")
    return badges


def _add_file_components(graph: _Graph) -> None:
    """One pass over the changed files: UI components, tests, datastores, config and code components."""

    texts = graph.texts
    styles: list[ChangedFile] = []
    by_symbol_path: dict[str, list[ChangedSymbol]] = {}
    for symbol in graph.inputs.symbols:
        by_symbol_path.setdefault(symbol.path, []).append(symbol)
    for item in sorted(graph.inputs.files, key=lambda row: row.path):
        role = classify(item.path, item.category)
        side, path = item.side, item.side_path
        text = texts.scan_text(side, path)
        whole = [anchor(path, side)]
        churn = item.additions + item.deletions
        if role == "skip":
            continue
        if role == "style":
            styles.append(item)
            continue
        if role == "ui":
            name = component_name(path, text)
            graph.node(
                _Node(
                    stable_id("n_", "ui", path),
                    name,
                    "ui",
                    "ui",
                    _file_delta(item),
                    whole,
                    subtitle=clip(path, MAX_SUBTITLE),
                    badges=_file_badges(item),
                    path=path,
                    name=name,
                    churn=churn,
                )
            )
            ext = _ext(path)
            graph.platform(
                "ui",
                {".vue": "Vue", ".svelte": "Svelte"}.get(
                    ext, "React" if "react" in text.lower() or ext in (".jsx", ".tsx") else ""
                ),
            )
            continue
        if role == "test":
            _add_test(graph, item, text, churn)
            continue
        if role in ("datastore", "config"):
            lane = "data" if role == "datastore" else "config"
            graph.node(
                _Node(
                    stable_id("n_", role, path),
                    _name(path),
                    role,
                    lane,
                    _file_delta(item),
                    whole,
                    subtitle=clip(posixpath.dirname(path) or "repository root", MAX_SUBTITLE),
                    badges=_file_badges(item),
                    path=path,
                    name=_name(path),
                    churn=churn,
                )
            )
            continue
        owned = [*by_symbol_path.get(item.path, []), *(by_symbol_path.get(item.old_path, []) if item.old_path else [])]
        _add_code(graph, item, owned, churn)
    _fold_styles(graph, styles)


def _fold_styles(graph: _Graph, styles: Sequence[ChangedFile]) -> None:
    """A stylesheet belongs to the component beside it (same stem, same folder); otherwise it is a UI module."""

    for item in styles:
        side, path = item.side, item.side_path
        owner = next(
            (
                graph.nodes[node_id]
                for node_id in sorted(graph.nodes)
                if graph.nodes[node_id].kind == "ui"
                and posixpath.dirname(graph.nodes[node_id].path) == posixpath.dirname(path)
                and _stem(graph.nodes[node_id].path) == _stem(path)
            ),
            None,
        )
        if owner is not None:
            owner.anchors = _merge_anchors(owner.anchors, [anchor(path, side)])
            owner.delta = _stronger_delta(owner.delta, "modified")
            owner.churn += item.additions + item.deletions
            graph.file_nodes.setdefault(path, []).append(owner.id)
            continue
        graph.node(
            _Node(
                stable_id("n_", "style", path),
                _name(path),
                "module",
                "ui",
                _file_delta(item),
                [anchor(path, side)],
                subtitle="stylesheet",
                badges=_file_badges(item),
                path=path,
                name=_name(path),
                churn=item.additions + item.deletions,
            )
        )


def _add_test(graph: _Graph, item: ChangedFile, text: str, churn: int) -> None:
    side, path = item.side, item.side_path
    if _ext(path) == ".bru":
        _request_test(graph, path, side, text, _file_delta(item), _file_badges(item), churn)
        return
    story = ".stories." in _name(path)
    if story:
        graph.platform("tests", "Storybook")
    elif _ext(path) == ".py":
        graph.platform("tests", "unittest" if "import unittest" in text else "pytest")
    elif "vitest" in text:
        graph.platform("tests", "Vitest")
    elif _ext(path) in _SCRIPT_EXT | _UI_EXT:
        graph.platform("tests", "Jest")
    graph.node(
        _Node(
            stable_id("n_", "test", path),
            _name(path),
            "test",
            "tests",
            _file_delta(item),
            [anchor(path, side)],
            subtitle=clip("Storybook story" if story else posixpath.dirname(path) or "tests", MAX_SUBTITLE),
            badges=_file_badges(item),
            path=path,
            name=_name(path),
            churn=churn,
        )
    )


def _request_test(
    graph: _Graph, path: str, side: str, text: str, delta: str, badges: list[str], churn: int
) -> _Node | None:
    """A request file (Bruno) as a test that calls a route; ``None`` when the file is not a request."""

    title, method, route_path, line = parse_request_file(text)
    if not method:
        return None
    graph.platform("tests", "Bruno")
    return graph.node(
        _Node(
            stable_id("n_", "test", path),
            clip(title or _name(path), MAX_LABEL),
            "test",
            "tests",
            delta,
            [anchor(path, side, line, line)],
            subtitle=clip(f"Bruno · {method} {route_path}", MAX_SUBTITLE),
            badges=badges,
            path=path,
            name=f"{method} {route_path}",
            churn=churn,
        )
    )


def _symbol_kind(kind: str) -> str:
    lowered = kind.lower()
    if "class" in lowered or lowered in ("struct", "interface", "enum", "trait"):
        return "class"
    if "function" in lowered or "method" in lowered or lowered in ("def", "func", "constructor"):
        return "function"
    return ""


def _add_code(graph: _Graph, item: ChangedFile, symbols: Sequence[ChangedSymbol], churn: int) -> None:
    """A source file's routes, its changed functions and classes, or (with neither) the file as a module."""

    texts = graph.texts
    side, path = item.side, item.side_path
    head_text = texts.scan_text("head", item.path) if item.status != "removed" else ""
    base_text = texts.scan_text("base", item.old_path or item.path) if item.status != "added" else ""
    language = {".py": "Python", ".go": "Go", ".java": "Java", ".rb": "Ruby", ".rs": "Rust"}.get(
        _ext(path), "TypeScript" if _ext(path) == ".ts" else "JavaScript" if _ext(path) in _SCRIPT_EXT else ""
    )
    framework = framework_of(path, head_text or base_text)
    graph.platform("api", framework or language)
    defs_head = definitions(item.path, head_text)
    defs_base = definitions(item.old_path or item.path, base_text)
    added_here: list[str] = []

    for symbol in sorted(symbols, key=lambda row: (row.start_line, row.name)):
        kind = _symbol_kind(symbol.kind)
        if not kind:
            continue
        removed = _symbol_delta(symbol.change) == "removed"
        symbol_side = "base" if removed else "head"
        symbol_path = (item.old_path or item.path) if removed else item.path
        defs = defs_base if removed else defs_head
        span = next((row for row in defs if row.name == symbol.name and row.start == symbol.start_line), None)
        end = max(symbol.end_line, span.end if span else symbol.end_line)
        node = graph.node(
            _Node(
                stable_id("n_", kind, item.path, symbol.name),
                f"{symbol.name}()" if kind == "function" else symbol.name,
                kind,
                "api",
                _symbol_delta(symbol.change),
                [anchor(symbol_path, symbol_side, symbol.start_line, end)],
                subtitle=clip(path, MAX_SUBTITLE),
                path=path,
                name=symbol.name,
                churn=churn,
            )
        )
        added_here.append(node.id)

    head_routes = {(route.method, route.path): route for route in routes(item.path, head_text)}
    base_routes = {(route.method, route.path): route for route in routes(item.old_path or item.path, base_text)}
    for key in sorted(set(head_routes) | set(base_routes)):
        on_head = key in head_routes
        route = head_routes[key] if on_head else base_routes[key]
        route_file = item.path if on_head else (item.old_path or item.path)
        line_changed = graph.changed(route_file if on_head else item.path, "head" if on_head else "base", route.line)
        if on_head and key in base_routes:
            delta = "modified" if line_changed else "unchanged"
        elif on_head:
            # Only a side that was read can prove a route new (or gone).
            delta = "added" if (base_text or item.status == "added") else ("modified" if line_changed else "unchanged")
        else:
            delta = "removed" if (head_text or item.status == "removed") else "unchanged"
        defs = defs_head if on_head else defs_base
        handler = next((row for row in defs if row.name == route.handler and row.kind == "function"), None)
        graph.route_candidates.append(
            _RouteCandidate(route, delta, "head" if on_head else "base", route_file, handler, item.path)
        )
    if not added_here and not head_routes and not base_routes:
        graph.node(
            _Node(
                stable_id("n_", "module", path),
                _name(path),
                "module",
                "api",
                _file_delta(item),
                [anchor(path, side)],
                subtitle=clip(posixpath.dirname(path) or "repository root", MAX_SUBTITLE),
                badges=_file_badges(item),
                path=path,
                name=_stem(path),
                churn=churn,
            )
        )


@dataclass(frozen=True, slots=True)
class _RouteCandidate:
    route: Route
    delta: str
    side: str
    path: str
    handler: Definition | None
    owner: str  # the changed file's (head) path: symbol node ids are keyed by it

    @property
    def node_id(self) -> str:
        return stable_id("n_", "route", self.route.method, self.route.path)


@dataclass(frozen=True, slots=True)
class _RouteRef:
    """A request path one changed client file names as a string literal (see :func:`route_references`)."""

    file: str
    side: str
    line: int
    method: str
    path: str
    called: bool

    def reaches(self, method: str, route_path: str) -> bool:
        if self.method and method and self.method != method:
            return False
        return route_path_matches(route_path, self.path, loose=self.called)


def _collect_route_refs(graph: _Graph) -> None:
    """Request-path literals of changed UI and script files; test files and comments never count."""

    for path in sorted(graph.file_nodes):
        owner = graph.nodes[graph.file_nodes[path][0]]
        if owner.path != path or owner.kind == "test" or classify(path) == "test":
            continue
        if _ext(path) not in _UI_EXT and _ext(path) not in _SCRIPT_EXT:
            continue
        side = str(owner.anchors[0]["side"]) if owner.anchors else "head"
        text = graph.texts.scan_text(side, path)
        for method, request_path, line, called in route_references(path, text, graph.literals(side, path)):
            graph.route_refs.append(_RouteRef(path, side, line, method, request_path, called))


def _owner_at(graph: _Graph, path: str, side: str, line: int) -> _Node | None:
    """The narrowest component whose code covers ``path:line``, else the file's own whole-file UI or module."""

    best: tuple[int, str] | None = None
    for node_id in graph.file_nodes.get(path, ()):
        for row in graph.nodes[node_id].anchors:
            start, end = _int(row.get("start_line")), _int(row.get("end_line"))
            if row["path"] != path or row["side"] != side or start is None or end is None or not start <= line <= end:
                continue
            if best is None or (end - start, node_id) < best:
                best = (end - start, node_id)
    if best is not None:
        return graph.nodes[best[1]]
    ids = graph.file_nodes.get(path, ())
    primary = graph.nodes[ids[0]] if ids else None
    return primary if primary is not None and primary.path == path and primary.kind in ("ui", "module") else None


def _match_route(graph: _Graph, method: str, path: str) -> str:
    """The route node a request reaches: the exact method and path, else the same path with a method unknown."""

    method = method.upper()
    exact = stable_id("n_", "route", method, path)
    if exact in graph.nodes:
        return exact
    loose = sorted(
        node_id
        for (route_method, route_path), node_id in graph.route_nodes.items()
        if route_path == path and (not route_method or not method)
    )
    return loose[0] if loose else ""


def _add_routes(graph: _Graph) -> None:
    """Keep the routes this change is about: changed ones, ones whose handler changed, and ones something calls."""

    targeted: set[tuple[str, str]] = set()
    for node in graph.nodes.values():
        if node.kind == "test" and " /" in node.name:
            method, _, route_path = node.name.partition(" ")
            targeted.add((method, route_path))
    for call in graph.inputs.calls:
        targeted.add((call.method.upper(), call.route))
    targeted_paths = {route_path for _method, route_path in targeted}
    for candidate in sorted(graph.route_candidates, key=lambda row: (row.route.path, row.route.method, row.path)):
        route = candidate.route
        handler_id = stable_id("n_", "function", candidate.owner, route.handler) if route.handler else ""
        # A handler changed when the packet names it, or (without symbols) when a changed hunk touches its body.
        span_changed = candidate.handler is not None and graph.changed(
            candidate.owner, candidate.side, candidate.handler.start, candidate.handler.end
        )
        handler_changed = span_changed or (
            bool(handler_id) and handler_id in graph.nodes and graph.nodes[handler_id].delta != "unchanged"
        )
        hit = (
            (route.method, route.path) in targeted
            or (not route.method and route.path in targeted_paths)
            or any(ref.reaches(route.method, route.path) for ref in graph.route_refs)
        )
        if candidate.delta == "unchanged" and not handler_changed and not hit:
            continue
        node = graph.node(
            _Node(
                candidate.node_id,
                clip(route.label, MAX_LABEL),
                "route",
                "api",
                candidate.delta,
                [anchor(candidate.path, candidate.side, route.line, route.line)],
                subtitle=clip(f"{route.framework} · {candidate.path}", MAX_SUBTITLE),
                path=candidate.path,
                name=route.label,
            )
        )
        graph.route_nodes[(route.method, route.path)] = node.id
        if candidate.handler is None or not handler_id:
            continue
        handler = graph.nodes.get(handler_id)
        if handler is None:
            handler = graph.node(
                _Node(
                    handler_id,
                    f"{route.handler}()",
                    "handler",
                    "api",
                    "modified" if span_changed else "unchanged",
                    [anchor(candidate.path, candidate.side, candidate.handler.start, candidate.handler.end)],
                    subtitle=clip(candidate.path, MAX_SUBTITLE),
                    path=candidate.path,
                    name=route.handler,
                )
            )
        elif handler.kind == "function":
            handler.kind = "handler"
        graph.handler_of[node.id] = handler.id
        graph.edge(
            _Edge(
                node.id,
                handler.id,
                "call",
                clip(f"{route.handler}()", MAX_EDGE_LABEL),
                _relation_delta(node.delta, handler.delta),
                [anchor(candidate.path, candidate.side, route.line, route.line)],
                score=1.0 if handler.delta != "unchanged" else 0.0,
            )
        )


def _line_in(node: _Node, offset: int) -> list[dict[str, Any]]:
    """An anchor on line ``offset`` (1-based) of the text :meth:`_Graph.span_text` returned for ``node``."""

    first = node.anchors[0]
    start = _int(first.get("start_line"))
    whole = start is None or node.kind in ("test", "ui", "module")
    line = offset if whole else (start or 1) + offset - 1
    return [anchor(str(first["path"]), str(first["side"]), line, line)]


def _add_request_edges(graph: _Graph) -> None:
    """Tests (request files) and client code (request-path literals) that call a route, as ``http`` edges."""

    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if node.kind == "test" and " /" in node.name:
            method, _, route_path = node.name.partition(" ")
            target = _match_route(graph, method, route_path)
            if target:
                graph.edge(
                    _Edge(
                        node.id,
                        target,
                        "http",
                        clip(node.name, MAX_EDGE_LABEL),
                        _relation_delta(node.delta, graph.nodes[target].delta),
                        list(node.anchors),
                        score=1.5 if graph.nodes[target].delta != "unchanged" else 0.5,
                    )
                )
    for ref in graph.route_refs:
        candidates = sorted(
            (method, route_path, node_id)
            for (method, route_path), node_id in graph.route_nodes.items()
            if ref.reaches(method, route_path)
        )
        if len(candidates) > 1:
            wanted = ref.method or "GET"
            candidates = [row for row in candidates if row[0] == wanted] or [row for row in candidates if not row[0]]
        if len(candidates) != 1:
            continue  # no route, or several the literal cannot tell apart
        method, route_path, target = candidates[0]
        source = _owner_at(graph, ref.file, ref.side, ref.line)
        if source is None or source.kind == "test":
            continue
        delta = _relation_delta(source.delta, graph.nodes[target].delta)
        if delta == "unchanged" and graph.changed(ref.file, ref.side, ref.line):
            delta = "modified"
        graph.edge(
            _Edge(
                source.id,
                target,
                "http",
                clip(f"{method or ref.method} {route_path}".strip(), MAX_EDGE_LABEL),
                delta,
                [anchor(ref.file, ref.side, ref.line, ref.line)],
                score=1.5,
            )
        )


def _add_import_edges(graph: _Graph) -> None:
    """Imports between changed files: renders, data reads, tests of functions, plain dependencies."""

    known = set(graph.file_nodes)
    for path in sorted(graph.file_nodes):
        owner = graph.nodes[graph.file_nodes[path][0]]
        if owner.path != path:
            continue  # a stylesheet folded into its component imports nothing of its own
        side = str(owner.anchors[0]["side"]) if owner.anchors else "head"
        text = graph.texts.scan_text(side, path)
        for spec, names, line in imports(path, text):
            target_path = resolve_import(path, spec, known)
            if not target_path or target_path == path:
                continue
            candidates = graph.file_nodes[target_path]
            picked = [node_id for node_id in candidates if graph.nodes[node_id].name in names] or candidates[:1]
            sources = [owner.id]
            if owner.kind not in ("test", "ui"):
                # A code file's import belongs to the component of that file that uses it, when one does.
                sources = [node_id for node_id in graph.file_nodes[path] if graph.nodes[node_id].kind != "route"][:1]
            for source_id in sources:
                source = graph.nodes[source_id]
                for target_id in picked:
                    target = graph.nodes[target_id]
                    if target.kind == "datastore":
                        kind, label = "data", f"reads {target.label}"
                    elif target.kind == "ui" and re.search(rf"<{re.escape(target.name)}\b", text):
                        kind, label = "render", f"renders {target.name}"
                    elif source.kind == "test" and target.kind in ("function", "handler", "class"):
                        kind, label = "call", f"tests {target.label}"
                    else:
                        kind, label = "dependency", f"imports {target.label}"
                    delta = _relation_delta(source.delta, target.delta)
                    if delta == "unchanged" and graph.changed(path, side, line):
                        delta = "modified"
                    graph.edge(
                        _Edge(
                            source.id,
                            target.id,
                            kind,
                            clip(label, MAX_EDGE_LABEL),
                            delta,
                            [anchor(path, side, line, line)],
                        )
                    )


def _add_reference_edges(graph: _Graph) -> None:
    """Calls of changed functions by name inside other components."""

    callables = {
        node.name: node
        for node in sorted(graph.nodes.values(), key=lambda row: row.id)
        if node.kind in ("function", "handler") and len(node.name) >= 3 and node.name not in _COMMON_NAMES
    }
    if not callables:
        return
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(name) for name in sorted(callables, key=len, reverse=True)) + r")\s*\("
    )
    for caller_id in sorted(graph.nodes):
        caller = graph.nodes[caller_id]
        if caller.kind not in ("test", "function", "handler", "ui", "module") or not caller.anchors:
            continue
        body = graph.span_text(caller)
        if not body:
            continue
        seen: set[str] = set()
        for match in pattern.finditer(body):
            target = callables[match.group(1)]
            if target.id == caller.id or target.id in seen:
                continue
            if target.path == caller.path and caller.kind in ("test", "ui", "module"):
                continue
            seen.add(target.id)
            line = body.count("\n", 0, match.start()) + 1
            graph.edge(
                _Edge(
                    caller.id,
                    target.id,
                    "call",
                    clip(("tests " if caller.kind == "test" else "calls ") + target.label, MAX_EDGE_LABEL),
                    _relation_delta(caller.delta, target.delta),
                    _line_in(caller, line),
                    score=0.5 if target.delta != "unchanged" else 0.0,
                )
            )


def _file_reference(value: str, targets: Mapping[str, _Node], by_name: Mapping[str, Sequence[str]]) -> str:
    """The graph file a path literal names (``"plans.csv"``, ``"../data/plans.csv"``, ``f"{DIR}/plans.csv"``)."""

    text = value.strip()
    if not text or len(text) > 256 or any(char.isspace() for char in text):
        return ""
    text = re.sub(r"^(?:\$\{\}|\{[^{}]*\})+", "", text).lstrip("/")
    while text.startswith(("./", "../")):
        text = text.split("/", 1)[1]
    name = _name(text)
    if "." not in name.strip("."):
        return ""
    if text in targets:
        return text
    # lc-debt: a bare file name resolves only among changed files, so an unchanged namesake elsewhere is not
    # seen; upgrade path: resolve against the revision's full file list.
    hits = [path for path in by_name.get(name, ()) if path.endswith("/" + text)]
    return hits[0] if len(hits) == 1 else ""


def _add_file_reference_edges(graph: _Graph) -> None:
    """Path literals in code that name a data or config file of the graph, as ``data`` edges ("reads plans.csv").

    The literal's own component reads the file; a module-level constant holding the path counts for the
    functions of that file that use the constant. Test files and comments never count.
    """

    targets = {node.path: node for node in graph.nodes.values() if node.kind in ("datastore", "config") and node.path}
    if not targets:
        return
    by_name: dict[str, list[str]] = {}
    for target_path in sorted(targets):
        by_name.setdefault(_name(target_path), []).append(target_path)
    for path in sorted(graph.file_nodes):
        owner = graph.nodes[graph.file_nodes[path][0]]
        if owner.path != path or owner.kind in ("test", "datastore", "config") or classify(path) == "test":
            continue
        if _ext(path) not in _CODE_EXT and _ext(path) not in _UI_EXT:
            continue
        side = str(owner.anchors[0]["side"]) if owner.anchors else "head"
        lines = graph.texts.scan_text(side, path).splitlines()
        constants: dict[str, tuple[str, int]] = {}
        for literal in graph.literals(side, path):
            target_path = _file_reference(literal.value, targets, by_name)
            if not target_path or target_path == path:
                continue
            source = _owner_at(graph, path, side, literal.line)
            if source is not None:
                if source.kind != "test":
                    _file_edge(graph, source, targets[target_path], path, side, lines, literal.line)
                continue
            assigned = _ASSIGNED.match(lines[literal.line - 1]) if literal.line <= len(lines) else None
            if assigned is not None:
                constants.setdefault(assigned.group(1), (target_path, literal.line))
        if not constants:
            continue
        uses = re.compile(r"\b(" + "|".join(re.escape(name) for name in sorted(constants)) + r")\b")
        for node_id in graph.file_nodes[path]:
            node = graph.nodes[node_id]
            if node.kind not in ("function", "handler", "class"):
                continue
            for name in sorted(set(uses.findall(graph.span_text(node)))):
                target_path, line = constants[name]
                _file_edge(graph, node, targets[target_path], path, side, lines, line)


def _file_edge(
    graph: _Graph, source: _Node, target: _Node, path: str, side: str, lines: Sequence[str], line: int
) -> None:
    text = lines[line - 1] if 0 < line <= len(lines) else ""
    verb = "writes" if _WRITE_HINT.search(text) else "reads"
    delta = _relation_delta(source.delta, target.delta)
    if delta == "unchanged" and graph.changed(path, side, line):
        delta = "modified"
    graph.edge(
        _Edge(
            source.id,
            target.id,
            "data",
            clip(f"{verb} {target.label}", MAX_EDGE_LABEL),
            delta,
            [anchor(path, side, line, line)],
            score=0.5 if target.delta != "unchanged" else 0.0,
        )
    )


# --------------------------------------------------------------------------- #
# the recorded layer: before/after runs of this exact revision
# --------------------------------------------------------------------------- #


def _response_type(route_path: str) -> str:
    segment = next((part for part in reversed(route_path.split("/")) if part and not part.startswith(("{", ":"))), "")
    words = re.split(r"[^A-Za-z0-9]+", segment)
    return clip("".join(word[:1].upper() + word[1:] for word in words if word) + "Response", MAX_PAYLOAD_TYPE)


def _response_payload(change: ResponseChange, base: RecordedCall | None, head: RecordedCall | None) -> dict[str, Any]:
    before, masked_before = redact_sample(base.body) if base is not None and base.has_body else (None, False)
    after, masked_after = redact_sample(head.body) if head is not None and head.has_body else (None, False)
    sample = head if head is not None else base
    route_path = sample.route if sample is not None else ""
    payload: dict[str, Any] = {
        "type": _response_type(route_path),
        "shape": type_sketch(after if after is not None else before),
        "before": before,
        "after": after,
        "evidence": "recorded",
        "evidence_ref": change.head_ref or change.base_ref,
        "behavior_key": change.behavior_key,
        "redacted": masked_before or masked_after,
    }
    status = {
        key: call.status
        for key, call in (("before", base), ("after", head))
        if call is not None and call.status is not None
    }
    if status:
        payload["status"] = status
    return payload


def _add_recorded(graph: _Graph) -> dict[str, tuple[str, str]]:
    """Mark the calls a run on this revision observed; returns ``recipe -> (caller node, route node)``."""

    inputs = graph.inputs
    by_recipe: dict[str, dict[str, RecordedCall]] = {}
    for call in sorted(inputs.calls, key=lambda row: (row.recipe, row.side, row.ref)):
        by_recipe.setdefault(call.recipe, {})[call.side] = call
    changes = {change.recipe: change for change in inputs.responses}
    matched: dict[str, tuple[str, str]] = {}
    unmatched = 0
    for recipe in sorted(by_recipe):
        sides = by_recipe[recipe]
        call = sides.get("head") or sides["base"]
        caller = graph.nodes.get(stable_id("n_", "test", call.surface))
        if caller is None:
            side = "head" if graph.texts.text("head", call.surface) else "base"
            caller = _request_test(
                graph, call.surface, side, graph.texts.scan_text(side, call.surface), "unchanged", [], 0
            )
        route_id = _match_route(graph, call.method, call.route) if call.route else ""
        change = changes.get(recipe)
        if caller is not None and not route_id and change is not None and call.method and call.route:
            # A response that changed although its endpoint's code is not in the change: still drawn (as the
            # endpoint the run called), so its two-step flow and payload stay reachable.
            route_id = graph.node(
                _Node(
                    stable_id("n_", "route", call.method, call.route),
                    clip(f"{call.method} {call.route}", MAX_LABEL),
                    "external",
                    "external",
                    "unchanged",
                    [],
                    subtitle="endpoint outside the changed code",
                    source="recorded",
                )
            ).id
        if caller is None or not route_id:
            unmatched += 1
            continue
        graph.edge(
            _Edge(
                caller.id,
                route_id,
                "http",
                clip(f"{call.method} {call.route}".strip(), MAX_EDGE_LABEL),
                "modified" if change is not None else _relation_delta(caller.delta, graph.nodes[route_id].delta),
                list(caller.anchors),
                evidence="recorded",
                recorded={"ref": call.ref, "count": len(sides)},
                score=(3.0 + change.body_change) if change is not None else 2.0,
            )
        )
        caller.badges.append("recorded")
        matched[recipe] = (caller.id, route_id)
    if unmatched:
        graph.degraded.append(f"{unmatched} recorded request(s) did not match a route in the changed code")
    _add_traces(graph)
    _add_effects(graph)
    return matched


def _function_node(graph: _Graph, path: str, name: str) -> _Node | None:
    for node_id in graph.file_nodes.get(path, ()):
        node = graph.nodes[node_id]
        if node.kind in ("function", "handler", "class") and node.name == name.rsplit(".", 1)[-1]:
            return node
    return None


def _add_traces(graph: _Graph) -> None:
    """Observed call edges between changed functions become (or confirm) recorded ``call`` edges."""

    seen: dict[tuple[str, str], dict[str, list[ObservedTrace]]] = {}
    for trace in graph.inputs.traces:
        caller = _function_node(graph, trace.caller, trace.caller_fn)
        callee = _function_node(graph, trace.callee, trace.callee_fn)
        if caller is None or callee is None or caller.id == callee.id:
            continue
        seen.setdefault((caller.id, callee.id), {}).setdefault(trace.side, []).append(trace)
    for (caller_id, callee_id), sides in sorted(seen.items()):
        delta = "unchanged" if len(sides) == 2 else ("added" if "head" in sides else "removed")
        newest = sorted(sides.get("head") or sides["base"], key=lambda row: row.ref)[0]
        graph.edge(
            _Edge(
                caller_id,
                callee_id,
                "call",
                clip(f"calls {graph.nodes[callee_id].label}", MAX_EDGE_LABEL),
                delta,
                [],
                evidence="recorded",
                recorded={"ref": newest.ref, "count": sum(len(rows) for rows in sides.values())},
                score=2.0 if delta != "unchanged" else 1.0,
            )
        )


def _add_effects(graph: _Graph) -> None:
    """Observed side-effect differences become recorded ``data`` edges to an external or a datastore."""

    dropped = 0
    for effect in sorted(graph.inputs.effects, key=lambda row: (row.category, row.identity, row.ref)):
        source = next(
            (
                graph.nodes[node_id]
                for raw in effect.sources
                for node_id in graph.file_nodes.get(raw.split(":", 1)[0], ())[:1]
            ),
            None,
        )
        delta = {"added": "added", "removed": "removed"}.get(effect.change, "modified")
        target: _Node | None = None
        if effect.category == "network" and effect.identity:
            target = graph.node(
                _Node(
                    stable_id("n_", "external", effect.identity),
                    clip(effect.identity, MAX_LABEL),
                    "external",
                    "external",
                    delta,
                    [],
                    subtitle="network call",
                    source="recorded",
                )
            )
        elif effect.category == "file_write" and graph.texts.lines("head", effect.identity) >= 0:
            target = graph.node(
                _Node(
                    stable_id("n_", "datastore", effect.identity),
                    _name(effect.identity),
                    "datastore",
                    "data",
                    delta,
                    [anchor(effect.identity, "head")],
                    subtitle="file written at runtime",
                    path=effect.identity,
                    name=_name(effect.identity),
                )
            )
        if source is None or target is None:
            dropped += 1
            continue
        verb = "calls" if effect.category == "network" else "writes"
        graph.edge(
            _Edge(
                source.id,
                target.id,
                "data",
                clip(f"{verb} {target.label}", MAX_EDGE_LABEL),
                delta,
                [],
                evidence="recorded",
                recorded={"ref": effect.ref, "count": 1},
                score=2.0,
            )
        )
    if dropped:
        graph.degraded.append(f"{dropped} observed side effect(s) had no changed component to attach to")


# --------------------------------------------------------------------------- #
# flows
# --------------------------------------------------------------------------- #


def _step(
    flow_id: str,
    index: int,
    source: str,
    target: str,
    label: str,
    kind: str,
    delta: str,
    evidence: str,
    anchors: Sequence[Mapping[str, Any]],
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": f"{flow_id}.s{index}",
        "from": source,
        "to": target,
        "label": clip(label, MAX_STEP_LABEL),
        "kind": kind,
        "delta": delta,
        "evidence": evidence,
        "anchors": [dict(item) for item in anchors],
    }
    row.update({key: value for key, value in extra.items() if value})
    return row


def _touched_steps(
    graph: _Graph, flow_id: str, handler: _Node, participants: list[dict[str, Any]], count: int
) -> list[dict[str, Any]]:
    """What a handler reads, writes or calls, one step each, within the participant and step caps."""

    touched = sorted(
        (edge for edge in graph.edges.values() if edge.source == handler.id and edge.kind in ("data", "call")),
        key=lambda edge: (0 if edge.kind == "data" else 1, edge.id),
    )
    members = {str(part["node"]) for part in participants}
    steps: list[dict[str, Any]] = []
    for edge in touched:
        if count + len(steps) >= MAX_STEPS - 2:
            break
        if edge.target not in members:
            if len(participants) >= MAX_PARTICIPANTS:
                continue
            participants.append({"node": edge.target})
            members.add(edge.target)
        steps.append(
            _step(
                flow_id,
                count + len(steps) + 1,
                handler.id,
                edge.target,
                edge.label or edge.kind,
                "sync",
                edge.delta,
                edge.evidence,
                edge.anchors,
                recorded=edge.recorded if edge.evidence == "recorded" else None,
            )
        )
    return steps


_RETURN_VERB = re.compile(r"^(?:get|list|fetch|load|read|handle|serve|show|find|query|build|compute)(?:_+|(?=[A-Z]))")
_RETURN_SUFFIX = re.compile(r"_?(?:handler|view|endpoint|route|controller|action)$", re.I)


def _returns_label(handler: _Node) -> str:
    """``returns plans`` from ``plans()``, ``get_plans()`` or ``listPlans()``."""

    stem = _RETURN_SUFFIX.sub("", _RETURN_VERB.sub("", handler.name.rsplit(".", 1)[-1].strip("_")))
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem).replace("_", " ").split()
    return clip("returns " + " ".join(words).lower(), MAX_STEP_LABEL) if words else "returns the body"


_IDENTITY_KEYS: Final[tuple[str, ...]] = ("name", "id", "key", "slug", "title", "code", "label")
MAX_FIELD_CHANGES: Final[int] = 64


def _identity(item: object) -> str:
    if not isinstance(item, Mapping):
        return ""
    for key in _IDENTITY_KEYS:
        value = item.get(key)
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value):
            return str(value)
    return ""


def _identities(items: Sequence[Any]) -> list[str] | None:
    """The identity (``name``, ``id`` ...) of every item of a list, when all have distinct ones."""

    keys = [_identity(item) for item in items]
    return keys if all(keys) and len(set(keys)) == len(keys) else None


def _value_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    if isinstance(value, Mapping):
        return "{…}"
    if isinstance(value, list | tuple):
        return f"[{len(value)}]"
    return clip(value, 18)


def _field_changes(
    before: Any, after: Any, path: tuple[str, ...], out: list[tuple[int, float, int, str]], depth: int = 0
) -> None:
    """Each leaf difference of two JSON values as ``(rank, -size, order, phrase)``; list items pair by identity.

    Rank orders numbers that changed (by how much), then other values that changed, then added and removed
    fields, so the first row after sorting is the biggest field change.
    """

    if len(out) >= MAX_FIELD_CHANGES:
        return
    where = " ".join(path)
    if isinstance(before, Mapping) and isinstance(after, Mapping) and depth < 8:
        for key in [*after, *(key for key in before if key not in after)]:
            inner = (*path, str(key))
            if key not in before:
                out.append((2, 0.0, len(out), f"{' '.join(inner)} added"))
            elif key not in after:
                out.append((3, 0.0, len(out), f"{' '.join(inner)} removed"))
            else:
                _field_changes(before[key], after[key], inner, out, depth + 1)
        return
    if isinstance(before, list) and isinstance(after, list) and depth < 8:
        left, right = _identities(before), _identities(after)
        if left is not None and right is not None:
            # Items with a name stand for themselves: "Team monthly", not "plans[1] monthly".
            old = dict(zip(left, before, strict=True))
            for ident, item in zip(right, after, strict=True):
                inner = (*path[:-1], ident)
                if ident in old:
                    _field_changes(old[ident], item, inner, out, depth + 1)
                else:
                    out.append((2, 0.0, len(out), f"{' '.join(inner)} added"))
            for ident in left:
                if ident not in right:
                    out.append((3, 0.0, len(out), f"{' '.join((*path[:-1], ident))} removed"))
            return
        for position in range(max(len(before), len(after))):
            inner = (*path[:-1], f"{path[-1]}[{position}]") if path else (f"[{position}]",)
            if position >= len(before):
                out.append((2, 0.0, len(out), f"{' '.join(inner)} added"))
            elif position >= len(after):
                out.append((3, 0.0, len(out), f"{' '.join(inner)} removed"))
            else:
                _field_changes(before[position], after[position], inner, out, depth + 1)
        return
    if before == after:
        return
    numbers = all(isinstance(value, int | float) and not isinstance(value, bool) for value in (before, after))
    size = abs(float(after) - float(before)) if numbers else 0.0
    phrase = f"{where} {_value_text(before)} → {_value_text(after)}".strip()
    out.append((0 if numbers else 1, -size, len(out), phrase))


def _return_label(before_status: int | None, after_status: int | None, before: Any, after: Any) -> str:
    """A recorded return step's label: the status and the biggest field change (``200 · Team monthly 49 → 59``)."""

    if before_status is not None and after_status is not None and before_status != after_status:
        prefix = f"{before_status} → {after_status}"
    else:
        status = after_status if after_status is not None else before_status
        prefix = str(status) if status is not None else ""
    changes: list[tuple[int, float, int, str]] = []
    if before is not None and after is not None:
        _field_changes(before, after, (), changes)
    elif after is not None:
        changes.append((2, 0.0, 0, "new body"))
    elif before is not None:
        changes.append((3, 0.0, 0, "body removed"))
    if not changes:
        if "→" in prefix:
            return clip(f"HTTP {prefix}", MAX_STEP_LABEL)
        return clip(f"HTTP {prefix}, response changed" if prefix else "response changed", MAX_STEP_LABEL)
    changes.sort(key=lambda row: row[:3])
    more = f", +{len(changes) - 1} more" if len(changes) > 1 else ""
    lead = f"{prefix} · " if prefix else ""
    room = max(MAX_STEP_LABEL - len(lead) - len(more), 12)
    return clip(lead + clip(changes[0][3], room) + more, MAX_STEP_LABEL)


def _response_flows(graph: _Graph, matched: Mapping[str, tuple[str, str]]) -> set[str]:
    """One flow per recorded response change: caller, route, handler, and the before/after body."""

    by_recipe: dict[str, dict[str, RecordedCall]] = {}
    for recorded_call in graph.inputs.calls:
        by_recipe.setdefault(recorded_call.recipe, {})[recorded_call.side] = recorded_call
    covered: set[str] = set()
    for change in sorted(graph.inputs.responses, key=lambda row: (-row.body_change, row.recipe)):
        if change.recipe not in matched:
            continue
        caller_id, route_id = matched[change.recipe]
        sides = by_recipe.get(change.recipe, {})
        base, head = sides.get("base"), sides.get("head")
        call = head or base
        if call is None:
            continue
        caller, route = graph.nodes[caller_id], graph.nodes[route_id]
        handler = graph.nodes.get(graph.handler_of.get(route_id, ""))
        flow_id = stable_id("f_", "response", change.recipe)
        # The run that observed this exchange: the newest side's record, over both sides it ran on.
        run = {"ref": call.ref, "count": len(sides)}
        participants = [{"node": caller_id, "label": caller.label}, {"node": route_id}]
        request = f"{call.method} {call.route}".strip()
        steps = [
            _step(
                flow_id, 1, caller_id, route_id, request, "sync", "unchanged", "recorded", caller.anchors, recorded=run
            )
        ]
        if handler is not None:
            participants.append({"node": handler.id})
            handler_delta = "modified" if handler.delta != "unchanged" else "unchanged"
            steps.append(
                _step(flow_id, 2, route_id, handler.id, handler.label, "sync", "unchanged", "static", route.anchors)
            )
            steps += _touched_steps(graph, flow_id, handler, participants, len(steps))
            steps.append(
                _step(
                    flow_id,
                    len(steps) + 1,
                    handler.id,
                    route_id,
                    _returns_label(handler),
                    "return",
                    handler_delta,
                    "static",
                    handler.anchors,
                )
            )
        payload = _response_payload(change, base, head)
        steps.append(
            _step(
                flow_id,
                len(steps) + 1,
                route_id,
                caller_id,
                _return_label(
                    base.status if base is not None else None,
                    head.status if head is not None else None,
                    payload["before"],
                    payload["after"],
                ),
                "return",
                "modified",
                "recorded",
                (),
                note=clip(change.summary, MAX_NOTE),
                payload={"response": payload},
                recorded=run,
            )
        )
        graph.flows.append(
            {
                "id": flow_id,
                "title": clip(f"{change.title or request} response", MAX_FLOW_TITLE),
                "summary": clip(change.summary, MAX_FLOW_SUMMARY),
                "participants": participants,
                "steps": steps,
            }
        )
        covered.add(route_id)
    return covered


def _parse_at(value: object) -> tuple[str, int]:
    path, _, line = str(value or "").rpartition(":")
    return (path, int(line)) if path and line.isdigit() else ("", 0)


def _node_at(graph: _Graph, path: str, side: str, line: int) -> _Node | None:
    """The narrowest component whose anchor on ``side`` covers ``path:line``, else the file's own node."""

    best: tuple[int, str] | None = None
    for node_id in graph.file_nodes.get(path, ()):
        for row in graph.nodes[node_id].anchors:
            start, end = _int(row.get("start_line")), _int(row.get("end_line"))
            if row["path"] != path or row["side"] != side or start is None or end is None or not start <= line <= end:
                continue
            if best is None or (end - start, node_id) < best:
                best = (end - start, node_id)
    if best is not None:
        return graph.nodes[best[1]]
    ids = graph.file_nodes.get(path, ())
    return graph.nodes[ids[0]] if ids else None


_SINKS: Final[Mapping[str, tuple[str, str, str]]] = {
    "db": ("datastore", "data", "Database"),
    "file": ("datastore", "data", "Files"),
    "log": ("datastore", "data", "Logs"),
    "http": ("external", "external", "HTTP service"),
}


def _data_path_flows(graph: _Graph) -> None:
    """Data-path flows (source to sink, from the code's own taint analysis) as static flows and ``data`` edges."""

    unresolved = 0
    order = {"added": 0, "rerouted": 1, "removed": 2}
    rows = sorted(
        (row for row in graph.inputs.data_flows if isinstance(row, Mapping)),
        key=lambda row: (order.get(str(row.get("change")), 3), str(_mapping(row.get("source")).get("at"))),
    )
    for row in rows:
        change = str(row.get("change") or "")
        delta = {"added": "added", "removed": "removed"}.get(change, "modified")
        side = "base" if change == "removed" else "head"
        kind, lane, label = _SINKS.get(str(row.get("kind") or ""), _SINKS["db"])
        sink = _mapping(row.get("sink"))
        sink_path, sink_line = _parse_at(sink.get("at"))
        sink_anchor = [anchor(sink_path, side, sink_line, sink_line)] if sink_path else []
        if kind != "external" and not sink_anchor:
            unresolved += 1
            continue
        target = graph.node(
            _Node(
                stable_id("n_", "sink", kind, label),
                label,
                kind,
                lane,
                delta,
                [] if kind == "external" else sink_anchor,
                subtitle=clip(f"{row.get('framework') or ''} data path".strip(), MAX_SUBTITLE),
            )
        )
        chain: list[_Node] = []
        hops: list[tuple[str, list[dict[str, Any]]]] = []
        source = _mapping(row.get("source"))
        points = [{"at": source.get("at"), "label": source.get("label") or source.get("symbol")}]
        points += [dict(step) for step in _mapping(row.get(side)).get("steps") or () if isinstance(step, Mapping)]
        for point in points:
            path, line = _parse_at(point.get("at"))
            found = _node_at(graph, path, side, line) if path else None
            if found is None or (chain and chain[-1].id == found.id):
                continue
            chain.append(found)
            hops.append(
                (str(point.get("label") or point.get("kind") or "passes data"), [anchor(path, side, line, line)])
            )
        if not chain:
            unresolved += 1
            continue
        callee = str(sink.get("callee") or sink.get("symbol") or label)
        graph.edge(_Edge(chain[-1].id, target.id, "data", clip(callee, MAX_EDGE_LABEL), delta, sink_anchor, score=0.8))
        flow_id = stable_id("f_", "data", change, str(source.get("at")), str(sink.get("at")))
        participants = [{"node": node.id} for node in chain[: MAX_PARTICIPANTS - 1]] + [{"node": target.id}]
        steps: list[dict[str, Any]] = []
        for index in range(1, min(len(chain), MAX_PARTICIPANTS - 1)):
            hop_label, hop_anchor = hops[index]
            steps.append(
                _step(
                    flow_id,
                    len(steps) + 1,
                    chain[index - 1].id,
                    chain[index].id,
                    hop_label,
                    "sync",
                    delta,
                    "static",
                    hop_anchor,
                )
            )
        last = chain[min(len(chain), MAX_PARTICIPANTS - 1) - 1]
        steps.append(_step(flow_id, len(steps) + 1, last.id, target.id, callee, "sync", delta, "static", sink_anchor))
        entry = str(row.get("entry_point") or source.get("symbol") or chain[0].label)
        graph.flows.append(
            {
                "id": flow_id,
                "title": clip(f"{entry} → {label} ({change or 'changed'})", MAX_FLOW_TITLE),
                "summary": clip(
                    f"{row.get('framework') or ''} {row.get('kind') or 'data'} flow, {change}".strip(), MAX_FLOW_SUMMARY
                ),
                "participants": participants,
                "steps": steps,
            }
        )
    if unresolved:
        graph.degraded.append(f"{unresolved} data-path flow(s) did not reach a changed component")


def _route_flows(graph: _Graph, covered: set[str]) -> None:
    """Changed routes no run recorded: caller (when one is known), route, handler and what it touches."""

    for route_id, handler_id in sorted(graph.handler_of.items()):
        route, handler = graph.nodes[route_id], graph.nodes[handler_id]
        if route_id in covered or (route.delta == "unchanged" and handler.delta == "unchanged"):
            continue
        callers = sorted(
            (edge for edge in graph.edges.values() if edge.target == route_id and edge.kind == "http"),
            key=lambda edge: (-_EVIDENCE_RANK[edge.evidence], edge.source),
        )
        flow_id = stable_id("f_", "route", route_id)
        participants: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        if callers:
            caller = graph.nodes[callers[0].source]
            participants.append({"node": caller.id, "label": caller.label})
            steps.append(
                _step(
                    flow_id,
                    1,
                    caller.id,
                    route_id,
                    route.label,
                    "sync",
                    callers[0].delta,
                    callers[0].evidence,
                    callers[0].anchors,
                    recorded=callers[0].recorded if callers[0].evidence == "recorded" else None,
                )
            )
        participants += [{"node": route_id}, {"node": handler_id}]
        steps.append(
            _step(
                flow_id,
                len(steps) + 1,
                route_id,
                handler_id,
                handler.label,
                "sync",
                route.delta,
                "static",
                route.anchors,
            )
        )
        steps += _touched_steps(graph, flow_id, handler, participants, len(steps))
        handler_delta = "modified" if handler.delta != "unchanged" else "unchanged"
        steps.append(
            _step(
                flow_id,
                len(steps) + 1,
                handler_id,
                route_id,
                _returns_label(handler),
                "return",
                handler_delta,
                "static",
                handler.anchors,
            )
        )
        if callers:
            steps.append(
                _step(
                    flow_id,
                    len(steps) + 1,
                    route_id,
                    participants[0]["node"],
                    "response",
                    "return",
                    handler_delta,
                    "static",
                    (),
                )
            )
        graph.flows.append(
            {
                "id": flow_id,
                "title": clip(route.label, MAX_FLOW_TITLE),
                "summary": (
                    clip(f"{route.label} is handled by {handler.label}, which changed.", MAX_FLOW_SUMMARY)
                    if handler.delta != "unchanged"
                    else clip(f"{route.label} {route.delta}.", MAX_FLOW_SUMMARY)
                ),
                "participants": participants,
                "steps": steps,
            }
        )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _describe(graph: _Graph, lanes: Sequence[Mapping[str, Any]], responses: int) -> tuple[str, str]:
    changed = [node for node in graph.nodes.values() if node.delta != "unchanged"]
    ui = sum(1 for node in changed if node.kind == "ui")
    routes_changed = {
        route_id
        for route_id, handler_id in graph.handler_of.items()
        if graph.nodes[route_id].delta != "unchanged" or graph.nodes[handler_id].delta != "unchanged"
    }
    if responses:
        title = f"Changes {_plural(responses, 'API response')}"
    elif routes_changed:
        title = f"Changes {_plural(len(routes_changed), 'route')}"
    else:
        title = f"Changes {_plural(len(changed), 'component')}"
    if ui and (responses or routes_changed):
        title += f" and {_plural(ui, 'UI component')}"
    lane_labels = ", ".join(str(lane["label"]) for lane in lanes if lane["id"] != "config") or "config"
    runs = len(graph.inputs.runs)
    summary = f"{_plural(len(changed), 'changed component')} across {lane_labels}. "
    if runs:
        summary += (
            f"{_plural(runs, 'recorded run')} on this revision; {_plural(responses, 'response')} "
            + ("differs" if responses == 1 else "differ")
            + " between base and head."
        )
    else:
        summary += "No before/after run was recorded on this revision."
    return clip(title, MAX_TITLE), clip(summary, MAX_SUMMARY)


def _views(
    nodes: Sequence[_Node], lanes: Sequence[Mapping[str, Any]], flows: Sequence[Mapping[str, Any]], summary: str
) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = [
        {
            "id": "v_" + str(flow["id"]),
            "title": clip(flow["title"], MAX_VIEW_TITLE),
            "lens": "flow",
            "summary": clip(flow.get("summary") or "", MAX_VIEW_SUMMARY),
            "scope": {"kind": "flow", "flow": flow["id"]},
            "children": [],
        }
        for flow in flows[:MAX_FLOWS]
    ]
    if len(lanes) > 1:
        for lane in lanes:
            members = sorted(node.id for node in nodes if node.lane == lane["id"])
            if len(members) >= 2:
                children.append(
                    {
                        "id": "v_lane_" + str(lane["id"]),
                        "title": clip(lane["label"], MAX_VIEW_TITLE),
                        "lens": "architecture",
                        "scope": {"kind": "nodes", "nodes": members},
                        "children": [],
                    }
                )
    return [
        {
            "id": "overview",
            "title": "Overview",
            "lens": "architecture",
            "summary": clip(summary, MAX_VIEW_SUMMARY),
            "scope": {"kind": "all"},
            "children": children[: MAX_VIEWS - 1],
        }
    ]


def _walkthrough(
    title: str, summary: str, flows: Sequence[Mapping[str, Any]], hero: _Edge | None, nodes: Mapping[str, _Node]
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {
            "id": "t1",
            "heading": clip(title, MAX_HEADING),
            "body": clip(summary, MAX_BODY),
            "stage": {"kind": "view", "view": "overview"},
            "focus": {"kind": "all"},
        }
    ]
    if hero is not None:
        steps.append(
            {
                "id": f"t{len(steps) + 1}",
                "heading": clip(_hero_heading(hero, nodes), MAX_HEADING),
                "body": clip(
                    f"{nodes[hero.source].label} to {nodes[hero.target].label}"
                    + (", observed by a recorded run." if hero.evidence == "recorded" else ", found in the code."),
                    MAX_BODY,
                ),
                "stage": {"kind": "view", "view": "overview"},
                "focus": {"kind": "selection", "nodes": [hero.source, hero.target], "edges": [hero.id]},
            }
        )
    for flow in flows[: MAX_TOUR - len(steps)]:
        changed = [str(step["id"]) for step in flow["steps"] if step["delta"] != "unchanged"]
        steps.append(
            {
                "id": f"t{len(steps) + 1}",
                "heading": clip(flow["title"], MAX_HEADING),
                "body": clip(flow.get("summary") or flow["title"], MAX_BODY),
                "stage": {"kind": "flow", "flow": flow["id"]},
                "focus": {"kind": "selection", "messages": changed} if changed else {"kind": "all"},
            }
        )
    return steps if len(steps) >= MIN_TOUR else []


def _hero_heading(hero: _Edge, nodes: Mapping[str, _Node]) -> str:
    if hero.delta != "unchanged":
        return f"{hero.label or nodes[hero.target].label} {hero.delta}"
    end = next((nodes[key] for key in (hero.target, hero.source) if nodes[key].delta != "unchanged"), None)
    if end is not None:
        return f"{end.label} {end.delta}"
    return hero.label or nodes[hero.target].label


def build_index_graph(inputs: XRayInputs, read_text: ReadText, *, texts: AnchorIndex | None = None) -> dict[str, Any]:
    """The deterministic, model-free graph of one revision, finalized (validated, capped, digested).

    ``texts`` shares one revision's file reads with a later :func:`finalize_graph` (the AI merge).
    """

    texts = texts if texts is not None else AnchorIndex(read_text)
    graph = _Graph(inputs, texts)
    _add_file_components(graph)
    _collect_route_refs(graph)
    _add_routes(graph)
    _add_request_edges(graph)
    _add_import_edges(graph)
    _add_reference_edges(graph)
    _add_file_reference_edges(graph)
    matched = _add_recorded(graph)
    covered = _response_flows(graph, matched)
    responses = len(graph.flows)
    _data_path_flows(graph)
    _route_flows(graph, covered)
    if texts.oversized:
        graph.degraded.append(f"{_plural(len(texts.oversized), 'large file')} were anchored but not scanned")

    for edge in graph.edges.values():
        for end in (edge.source, edge.target):
            if edge.evidence == "recorded" and graph.nodes[end].source == "static" and graph.nodes[end].kind == "test":
                graph.nodes[end].badges.append("recorded")
    present = {node.lane for node in graph.nodes.values()}
    lanes: list[dict[str, Any]] = []
    for lane_id in LANE_ORDER:
        if lane_id not in present:
            continue
        label = _LANE_LABELS[lane_id]
        if lane_id == "api" and graph.route_nodes:
            label = "Server"
        lane: dict[str, Any] = {"id": lane_id, "label": label}
        platforms = sorted(graph.platforms.get(lane_id, ()))
        if platforms:
            lane["platform"] = clip(" + ".join(platforms[:2]), 40)
        lanes.append(lane)
    hero = max(
        (edge for edge in graph.edges.values() if edge.score > 0 or edge.delta != "unchanged"),
        key=lambda edge: (
            edge.score,
            _DELTA_RANK[edge.delta],
            _EVIDENCE_RANK[edge.evidence],
            graph.nodes[edge.source].churn + graph.nodes[edge.target].churn,
            edge.id,
        ),
        default=None,
    )
    title, summary = _describe(graph, lanes, responses)
    nodes = sorted(graph.nodes.values(), key=lambda node: node.id)
    edges = []
    for edge in sorted(graph.edges.values(), key=lambda row: row.id):
        row = edge.to_wire()
        if hero is not None and edge.id == hero.id:
            row["hero"] = True
        edges.append(row)
    raw = {
        "version": GRAPH_VERSION,
        "review_id": inputs.review_id,
        "revision_id": inputs.revision_id,
        "title": title,
        "summary": summary,
        "provenance": {
            "producer": "index",
            "generated_at": inputs.generated_at,
            "recorded_runs": [
                {"ref": run.ref, "recipe": run.recipe, "side": run.side, "at": run.at} for run in inputs.runs
            ],
        },
        "lanes": lanes,
        "nodes": [node.to_wire() for node in nodes],
        "edges": edges,
        "flows": graph.flows,
        "views": _views(nodes, lanes, graph.flows, summary),
        "degraded": graph.degraded,
    }
    tour = _walkthrough(title, summary, graph.flows, hero, graph.nodes)
    if tour:
        raw["walkthrough"] = {"steps": tour}
    return finalize_graph(raw, texts)


# --------------------------------------------------------------------------- #
# finalize: every producer rule, for any graph (index or AI-enriched)
# --------------------------------------------------------------------------- #


def _text(value: object, limit: int) -> str:
    return clip(value, limit) if isinstance(value, str | int | float) and not isinstance(value, bool) else ""


def _choice(value: object, allowed: frozenset[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


_KIND_RANK: Final[Mapping[str, int]] = {"route": 0, "handler": 1, "ui": 2, "function": 3, "class": 3}


def _fold(
    nodes: dict[str, dict[str, Any]], protected: set[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, str], int]:
    """Keep the 60 most telling nodes; fold the rest of each lane into one ``+N more`` module node."""

    ordered = sorted(
        nodes.values(),
        key=lambda row: (
            0 if row["id"] in protected else 1,
            0 if row["delta"] != "unchanged" else 1,
            _KIND_RANK.get(str(row["kind"]), 4),
            str(row["label"]).casefold(),
            row["id"],
        ),
    )
    keep = MAX_NODES - 1
    while keep > 0:
        lanes = {str(row["lane"]) for row in ordered[keep:]}
        if keep + len(lanes) <= MAX_NODES:
            break
        keep -= 1
    kept = {row["id"]: row for row in ordered[:keep]}
    folded: dict[str, list[dict[str, Any]]] = {}
    for row in ordered[keep:]:
        folded.setdefault(str(row["lane"]), []).append(row)
    remap: dict[str, str] = {}
    for lane, rows in sorted(folded.items()):
        fold_id = stable_id("n_", "fold", lane)
        anchors = sorted({canonical_json(item): item for row in rows for item in row["anchors"]}.items())[:3]
        kept[fold_id] = {
            "id": fold_id,
            "label": f"+{len(rows)} more",
            "kind": "module",
            "lane": lane,
            "delta": "modified" if any(row["delta"] != "unchanged" for row in rows) else "unchanged",
            "subtitle": clip(f"{_plural(len(rows), 'component')} folded to stay within the cap", MAX_SUBTITLE),
            "badges": [],
            "anchors": [item for _key, item in anchors],
            "source": "static",
        }
        for row in rows:
            remap[row["id"]] = fold_id
    return kept, remap, sum(len(rows) for rows in folded.values())


def _clean_runs(raw: object) -> list[dict[str, str]]:
    runs: dict[tuple[str, str], dict[str, str]] = {}
    for row in raw if isinstance(raw, list | tuple) else ():
        if not isinstance(row, Mapping):
            continue
        ref, side = str(row.get("ref") or ""), str(row.get("side") or "")
        if ref and side in SIDES:
            runs.setdefault(
                (ref, side),
                {"ref": ref, "recipe": str(row.get("recipe") or ""), "side": side, "at": str(row.get("at") or "")},
            )
    return sorted(runs.values(), key=lambda row: (row["at"], row["ref"], row["side"]))


def _step_id(flow_id: str, value: object) -> str:
    """A flow step's graph-unique id, ``<flow id>.<local id>``; a bare local id (``s1``) gains the prefix."""

    raw = _text(value, 96)
    local = raw[len(flow_id) + 1 :] if raw.startswith(flow_id + ".") else raw
    local = local if len(local) <= 16 else ""
    return f"{flow_id}.{local}" if flow_id and local else ""


def finalize_graph(raw: Mapping[str, Any], texts: AnchorIndex) -> dict[str, Any]:
    """Enforce every producer rule on ``raw`` and return the canonical graph with its ``digest``.

    Idempotent: finalizing a finalized graph returns it unchanged.
    """

    notes: list[str] = [clip(note, 160) for note in raw.get("degraded") or () if isinstance(note, str) and note.strip()]
    provenance = _mapping(raw.get("provenance"))
    runs = _clean_runs(provenance.get("recorded_runs"))
    refs = frozenset(run["ref"] for run in runs)

    lanes: list[dict[str, Any]] = []
    for row in raw.get("lanes") or ():
        lane_id = _text(_mapping(row).get("id"), 40)
        if lane_id and lane_id not in {lane["id"] for lane in lanes}:
            lane = {"id": lane_id, "label": _text(row.get("label"), 40) or lane_id}
            if _text(row.get("platform"), 40):
                lane["platform"] = _text(row.get("platform"), 40)
            lanes.append(lane)
    if len(lanes) > MAX_LANES:
        notes.append(f"Kept the first {MAX_LANES} of {len(lanes)} lanes")
        lanes = lanes[:MAX_LANES]
    lane_ids = [lane["id"] for lane in lanes]

    nodes: dict[str, dict[str, Any]] = {}
    ungrounded = 0
    for row in raw.get("nodes") or ():
        row = _mapping(row)
        node_id, kind = _text(row.get("id"), 64), _choice(row.get("kind"), NODE_KINDS, "")
        if not node_id or node_id in nodes or not kind or row.get("lane") not in lane_ids:
            ungrounded += 1
            continue
        anchors = texts.clean(row.get("anchors"))
        if kind != "external" and not anchors:
            ungrounded += 1
            continue
        node: dict[str, Any] = {
            "id": node_id,
            "label": _text(row.get("label"), MAX_LABEL) or node_id,
            "kind": kind,
            "lane": row["lane"],
            "delta": _choice(row.get("delta"), DELTAS, "unchanged"),
            "badges": [_text(badge, 24) for badge in list(row.get("badges") or ())[:MAX_BADGES] if _text(badge, 24)],
            "anchors": anchors,
            "source": _choice(row.get("source"), EVIDENCE, "static"),
        }
        if _text(row.get("subtitle"), MAX_SUBTITLE):
            node["subtitle"] = _text(row.get("subtitle"), MAX_SUBTITLE)
        nodes[node_id] = node
    if ungrounded:
        notes.append(f"Left out {_plural(ungrounded, 'component')} whose code could not be found on this revision")

    raw_edges = [_mapping(row) for row in raw.get("edges") or ()]
    raw_flows = [_mapping(row) for row in raw.get("flows") or ()]
    remap: dict[str, str] = {}
    if len(nodes) > MAX_NODES:
        protected = {
            str(row.get(end))
            for row in raw_edges
            if row.get("hero") is True or row.get("evidence") == "recorded"
            for end in ("from", "to")
        }
        protected |= {
            str(_mapping(part).get("node")) for flow in raw_flows[:MAX_FLOWS] for part in flow.get("participants") or ()
        }
        nodes, remap, folded = _fold(nodes, protected)
        notes.append(f"Folded {_plural(folded, 'component')} into '+N more' groups to stay within {MAX_NODES}")

    def node_ref(value: object) -> str:
        key = str(value or "")
        key = remap.get(key, key)
        return key if key in nodes else ""

    edges: dict[str, dict[str, Any]] = {}
    edge_ids: dict[str, str] = {}
    downgraded = 0
    for row in raw_edges:
        source, target = node_ref(row.get("from")), node_ref(row.get("to"))
        kind = _choice(row.get("kind"), EDGE_KINDS, "")
        if not source or not target or source == target or not kind:
            continue
        evidence = _choice(row.get("evidence"), EVIDENCE, "static")
        recorded = _mapping(row.get("recorded"))
        count = _int(recorded.get("count"))
        if evidence == "recorded" and (str(recorded.get("ref") or "") not in refs or count is None or count < 1):
            evidence, downgraded = "static", downgraded + 1
        edge_id = stable_id("e_", source, target, kind)
        edge_ids[str(row.get("id") or "")] = edge_id
        edge: dict[str, Any] = {
            "id": edge_id,
            "from": source,
            "to": target,
            "kind": kind,
            "delta": _choice(row.get("delta"), DELTAS, "unchanged"),
            "evidence": evidence,
            "anchors": texts.clean(row.get("anchors")),
        }
        if _text(row.get("label"), MAX_EDGE_LABEL):
            edge["label"] = _text(row.get("label"), MAX_EDGE_LABEL)
        if evidence == "recorded":
            edge["recorded"] = {"ref": str(recorded["ref"]), "count": count}
        if row.get("hero") is True:
            edge["hero"] = True
        existing = edges.get(edge_id)
        if existing is not None:
            existing["delta"] = _stronger_delta(existing["delta"], edge["delta"])
            existing["anchors"] = _merge_anchors(existing["anchors"], edge["anchors"])
            if _EVIDENCE_RANK[edge["evidence"]] > _EVIDENCE_RANK[existing["evidence"]]:
                existing["evidence"] = edge["evidence"]
                existing["recorded"] = edge.get("recorded")
            if edge.get("hero"):
                existing["hero"] = True
            existing.setdefault("label", edge.get("label", ""))
            continue
        edges[edge_id] = edge

    def edge_priority(row: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
        return (
            0 if row.get("hero") else 1,
            -_EVIDENCE_RANK[str(row["evidence"])],
            -_DELTA_RANK[str(row["delta"])],
            0 if row["kind"] == "http" else 1,
            str(row["id"]),
        )

    ranked = sorted(edges.values(), key=edge_priority)
    if len(ranked) > MAX_EDGES:
        notes.append(
            f"Left out {_plural(len(ranked) - MAX_EDGES, 'lower-priority edge')} past the {MAX_EDGES}-edge cap"
        )
        ranked = ranked[:MAX_EDGES]
    heroes = [row for row in ranked if row.get("hero")]
    for row in heroes[1:]:
        row.pop("hero", None)
    for row in ranked:
        if not row.get("label"):
            row.pop("label", None)
    edges = {row["id"]: row for row in ranked}
    recorded_pairs: dict[frozenset[str], dict[str, Any]] = {}
    for row in sorted(edges.values(), key=lambda item: str(item["id"])):
        if row["evidence"] == "recorded":
            recorded_pairs.setdefault(frozenset((row["from"], row["to"])), row["recorded"])
    for node in nodes.values():
        if node["source"] == "recorded" and not any(node["id"] in pair for pair in recorded_pairs):
            node["source"], downgraded = "static", downgraded + 1

    flows: list[dict[str, Any]] = []
    flow_steps: dict[str, set[str]] = {}
    dropped_flow_parts = 0
    for row in raw_flows:
        flow_id, title = _text(row.get("id"), 64), _text(row.get("title"), MAX_FLOW_TITLE)
        if not flow_id or not title or flow_id in flow_steps:
            dropped_flow_parts += 1
            continue
        participants: list[dict[str, Any]] = []
        for part in row.get("participants") or ():
            node_id = node_ref(_mapping(part).get("node"))
            if node_id and node_id not in {item["node"] for item in participants}:
                item: dict[str, Any] = {"node": node_id}
                if _text(_mapping(part).get("label"), 40):
                    item["label"] = _text(_mapping(part).get("label"), 40)
                participants.append(item)
        participants = participants[:MAX_PARTICIPANTS]
        members = {item["node"] for item in participants}
        steps: list[dict[str, Any]] = []
        step_ids: set[str] = set()
        for raw_step in row.get("steps") or ():
            step = _mapping(raw_step)
            step_id = _step_id(flow_id, step.get("id"))
            source, target = node_ref(step.get("from")), node_ref(step.get("to"))
            kind = _choice(step.get("kind"), STEP_KINDS, "")
            if not step_id or step_id in step_ids or source not in members or target not in members or not kind:
                dropped_flow_parts += 1
                continue
            if source == target:
                kind = "self"
            evidence = _choice(step.get("evidence"), EVIDENCE, "static")
            payload: dict[str, Any] = {}
            for part_name in ("request", "response"):
                raw_part = _mapping(step.get("payload")).get(part_name)
                if raw_part is None:
                    continue
                cleaned = _clean_payload(raw_part, refs)
                if cleaned is None:
                    dropped_flow_parts += 1
                    continue
                payload[part_name] = cleaned
            # The run that observed the step: its own, else the recorded edge it travels, else its payload's.
            claimed = _mapping(step.get("recorded"))
            claimed_count = _int(claimed.get("count"))
            run: dict[str, Any] | None = None
            if str(claimed.get("ref") or "") in refs and claimed_count is not None and claimed_count >= 1:
                run = {"ref": str(claimed["ref"]), "count": claimed_count}
            elif frozenset((source, target)) in recorded_pairs:
                run = dict(recorded_pairs[frozenset((source, target))])
            else:
                run = next(
                    (
                        {"ref": str(item["evidence_ref"]), "count": 1}
                        for item in payload.values()
                        if item.get("evidence") == "recorded"
                    ),
                    None,
                )
            if evidence == "recorded" and run is None:
                evidence, downgraded = "static", downgraded + 1
            clean_step: dict[str, Any] = {
                "id": step_id,
                "from": source,
                "to": target,
                "label": _text(step.get("label"), MAX_STEP_LABEL) or kind,
                "kind": kind,
                "delta": _choice(step.get("delta"), DELTAS, "unchanged"),
                "evidence": evidence,
                "anchors": texts.clean(step.get("anchors")),
            }
            repeat = _int(step.get("repeat"))
            if repeat is not None and repeat >= 2:
                clean_step["repeat"] = repeat
            if _text(step.get("note"), MAX_NOTE):
                clean_step["note"] = _text(step.get("note"), MAX_NOTE)
            if payload:
                clean_step["payload"] = payload
            if evidence == "recorded" and run is not None:
                clean_step["recorded"] = run
            steps.append(clean_step)
            step_ids.add(step_id)
        if len(steps) > MAX_STEPS:
            dropped_flow_parts += len(steps) - MAX_STEPS
            steps = steps[:MAX_STEPS]
        if len(participants) < MIN_PARTICIPANTS or not steps:
            dropped_flow_parts += 1
            continue
        flow: dict[str, Any] = {"id": flow_id, "title": title, "participants": participants, "steps": steps}
        if _text(row.get("summary"), MAX_FLOW_SUMMARY):
            flow["summary"] = _text(row.get("summary"), MAX_FLOW_SUMMARY)
        flows.append(flow)
        flow_steps[flow_id] = {step["id"] for step in steps}
    if len(flows) > MAX_FLOWS:
        notes.append(f"Kept {MAX_FLOWS} of {len(flows)} flows")
        for flow in flows[MAX_FLOWS:]:
            flow_steps.pop(flow["id"], None)
        flows = flows[:MAX_FLOWS]
    if dropped_flow_parts:
        notes.append(f"Left out {_plural(dropped_flow_parts, 'flow part')} that did not resolve")
    if downgraded:
        notes.append(f"Shown as static: {_plural(downgraded, 'element')} without a recorded run on this revision")

    budget = [MAX_VIEWS]
    view_ids: set[str] = set()

    def clean_view(raw_view: object, depth: int) -> dict[str, Any] | None:
        view = _mapping(raw_view)
        view_id = _text(view.get("id"), 64)
        lens = _choice(view.get("lens"), frozenset({"architecture", "flow"}), "")
        title = _text(view.get("title"), MAX_VIEW_TITLE)
        scope = _mapping(view.get("scope"))
        if not view_id or view_id in view_ids or not lens or not title or budget[0] <= 0:
            return None
        if scope.get("kind") == "all":
            clean_scope: dict[str, Any] = {"kind": "all"}
        elif scope.get("kind") == "nodes":
            members = sorted({node_ref(item) for item in scope.get("nodes") or ()} - {""})
            if not members:
                return None
            clean_scope = {"kind": "nodes", "nodes": members}
        elif scope.get("kind") == "flow" and scope.get("flow") in flow_steps:
            clean_scope = {"kind": "flow", "flow": scope["flow"]}
        else:
            return None
        budget[0] -= 1
        view_ids.add(view_id)
        out: dict[str, Any] = {"id": view_id, "title": title, "lens": lens, "scope": clean_scope, "children": []}
        if _text(view.get("summary"), MAX_VIEW_SUMMARY):
            out["summary"] = _text(view.get("summary"), MAX_VIEW_SUMMARY)
        if depth < 4:
            for child in view.get("children") or ():
                cleaned = clean_view(child, depth + 1)
                if cleaned is not None:
                    out["children"].append(cleaned)
        return out

    raw_views = list(raw.get("views") or ())
    first = _mapping(raw_views[0]) if raw_views else {}
    if _mapping(first.get("scope")).get("kind") != "all":
        raw_views = [
            {
                "id": "overview",
                "title": "Overview",
                "lens": "architecture",
                "scope": {"kind": "all"},
                "children": raw_views,
            }
        ]
    views = [view for view in (clean_view(item, 0) for item in raw_views) if view is not None]

    tour: list[dict[str, Any]] = []
    for raw_step in _mapping(raw.get("walkthrough")).get("steps") or ():
        step = _mapping(raw_step)
        step_id = _text(step.get("id"), 16) or f"t{len(tour) + 1}"
        heading = _text(step.get("heading"), MAX_HEADING)
        stage = _mapping(step.get("stage"))
        if stage.get("kind") == "view" and stage.get("view") in view_ids:
            clean_stage: dict[str, Any] = {"kind": "view", "view": stage["view"]}
            messages_allowed: set[str] = set()
        elif stage.get("kind") == "flow" and stage.get("flow") in flow_steps:
            clean_stage = {"kind": "flow", "flow": stage["flow"]}
            messages_allowed = flow_steps[str(stage["flow"])]
        else:
            continue
        if not heading or step_id in {item["id"] for item in tour}:
            continue
        focus = _mapping(step.get("focus"))
        selection: dict[str, list[str]] = {}
        if focus.get("kind") == "selection":
            picked_nodes = sorted({node_ref(item) for item in focus.get("nodes") or ()} - {""})
            picked_edges = sorted(
                {edge_ids.get(str(item), str(item)) for item in focus.get("edges") or ()} & set(edges)
            )
            flow_ref = str(clean_stage.get("flow") or "")
            picked_messages = sorted(
                {_step_id(flow_ref, item) for item in focus.get("messages") or ()} & messages_allowed
            )
            for key, values in (("nodes", picked_nodes), ("edges", picked_edges), ("messages", picked_messages)):
                if values:
                    selection[key] = values
        tour.append(
            {
                "id": step_id,
                "heading": heading,
                "body": _text(step.get("body"), MAX_BODY) or heading,
                "stage": clean_stage,
                "focus": {"kind": "selection", **selection} if selection else {"kind": "all"},
            }
        )
    tour = tour[:MAX_TOUR]

    used_lanes = {node["lane"] for node in nodes.values()}
    lanes = [lane for lane in lanes if lane["id"] in used_lanes] or lanes[:1]
    order = {lane["id"]: index for index, lane in enumerate(lanes)}
    unique_notes = list(dict.fromkeys(notes))
    if len(unique_notes) > MAX_DEGRADED:
        extra = len(unique_notes) - MAX_DEGRADED + 1
        unique_notes = [*unique_notes[: MAX_DEGRADED - 1], f"and {_plural(extra, 'more note')}"]
    clean_provenance: dict[str, Any] = {
        "producer": _choice(provenance.get("producer"), frozenset({"index", "index+ai"}), "index"),
        "generated_at": str(provenance.get("generated_at") or ""),
        "recorded_runs": runs,
    }
    if clean_provenance["producer"] == "index+ai" and _text(provenance.get("model"), 120):
        clean_provenance["model"] = _text(provenance.get("model"), 120)
    graph: dict[str, Any] = {
        "version": GRAPH_VERSION,
        "review_id": str(raw.get("review_id") or ""),
        "revision_id": str(raw.get("revision_id") or ""),
        "title": _text(raw.get("title"), MAX_TITLE) or "Changes in this revision",
        "summary": _text(raw.get("summary"), MAX_SUMMARY),
        "provenance": clean_provenance,
        "lanes": lanes,
        "nodes": sorted(
            nodes.values(),
            key=lambda row: (order.get(row["lane"], len(order)), str(row["label"]).casefold(), row["id"]),
        ),
        "edges": sorted(edges.values(), key=lambda row: row["id"]),
        "flows": flows,
        "views": views,
        "degraded": unique_notes,
    }
    if len(tour) >= MIN_TOUR:
        graph["walkthrough"] = {"steps": tour}
    canonical = json.loads(canonical_json(graph))
    canonical["digest"] = graph_digest(canonical)
    return dict(json.loads(canonical_json(canonical)))
