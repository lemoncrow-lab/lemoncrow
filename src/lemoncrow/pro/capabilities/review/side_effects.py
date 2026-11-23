"""Static side-effect analysis of one Snapshot (Phase 9R items 9, 26, 28, 34, 35, 45).

The ``side_effects.static`` capability, run only when the effective run plan
schedules it on this executor. It reads the exact base and head text of the
Snapshot's changed files and reports, as behavior deltas through the one
``register_behavior_producer`` seam:

* **Side effects** (items 9 and 34): every inventory fact of the changed files
  (``contracts.inventory_side_effects`` and ``dependencies.dependency_side_effects``)
  compared with the **side-effect register** (item 35) -- the inventory of the
  base commit's whole tree. The register is *derived*, never written: it is
  recomputed from the base tree and only cached, keyed by that tree. A fact
  whose identity is new to the register is ``added``; an identity the change
  takes out of the register is ``removed``; a changed value set is ``changed``.
  An unchanged inventory yields nothing. When the register cannot be read whole
  it is ``truncated`` or ``changed_files_only`` and the analysis over-reports.
* **Agent instructions and tool permissions** (item 45): instruction files,
  agent/MCP tool configuration, prompts and ``.lemoncrow`` policy. Their
  content is untrusted data: it is digested and diffed, never interpreted,
  quoted into a target or applied.
* **Verification weakening and spec deltas** (items 26 and 28): see
  ``verification_delta.py``.

The per-Snapshot result is stored as a ``review_derived`` row (deleted with its
Snapshot), so it is computed and metered once.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import re
import threading
import time
import tomllib
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast

import yaml

from lemoncrow.pro.capabilities.review import request_reads
from lemoncrow.pro.capabilities.review.behavior import (
    BEHAVIOR_CONTRACT_VERSION,
    BehaviorChange,
    BehaviorContext,
    BehaviorDelta,
    BehaviorKind,
    BehaviorObservation,
    BehaviorProduction,
    BehaviorSeverity,
    BehaviorSide,
    register_behavior_producer,
)
from lemoncrow.pro.capabilities.review.capability_catalog import REVIEW_HOST
from lemoncrow.pro.capabilities.review.contracts import (
    SIDE_EFFECT_INVENTORY_VERSION,
    SideEffectFact,
    inventory_side_effects,
    side_effect_relevant,
)
from lemoncrow.pro.capabilities.review.dependencies import dependency_side_effects, supported_path
from lemoncrow.pro.capabilities.review.gitdiff import classify_path
from lemoncrow.pro.capabilities.review.verification_delta import (
    AUTO_APPROVAL_BLOCKED,
    AUTO_APPROVAL_FACT,
    VERIFICATION_ANALYSIS_VERSION,
    ChangedText,
    spec_deltas,
    verification_relevant,
    weakening_deltas,
)

PRODUCER_ID = "lemoncrow.static_side_effects"
STATIC_ENTRY_ID = "side_effects.static"
DERIVED_KIND = "static_side_effects"
STATIC_ANALYSIS_VERSION = 1
STAMP = f"static-v{STATIC_ANALYSIS_VERSION}.inv{SIDE_EFFECT_INVENTORY_VERSION}.ver{VERIFICATION_ANALYSIS_VERSION}"
"""Identifies the analysis code; a stored result with another stamp is recomputed."""

RegisterState = Literal["complete", "truncated", "changed_files_only"]
ReportStatus = Literal[
    "ran", "pending", "skipped", "inapplicable", "limited_by_budget", "not_available", "on_request", "not_run"
]

_ENVIRONMENT = f"static:v{STATIC_ANALYSIS_VERSION}.{SIDE_EFFECT_INVENTORY_VERSION}"
_INVENTORY_CATEGORIES = frozenset({"production", "config"})
_INSTALL_SCRIPT_NAMES = frozenset({"setup.py", "build.rs", "binding.gyp", "extconf.rb"})
_MAX_FILE_BYTES = 1 << 20
_MAX_CHANGED_FILES = 2000
_MAX_SIDE_EFFECT_TARGETS = 120

_CATEGORY_LABELS: Mapping[str, str] = {
    "env_var": "environment variable",
    "config_key": "configuration key",
    "outbound_host": "outbound host",
    "route": "route",
    "cli_flag": "CLI flag",
    "db_table": "database table",
    "db_drop": "destructive database operation",
    "migration": "database migration",
    "permission": "permission or scope",
    "feature_flag": "feature flag",
    "scheduled_job": "scheduled job",
    "telemetry_event": "telemetry event",
    "subprocess": "subprocess call",
    "filesystem_path": "filesystem path outside the repository",
    "data_flow": "sensitive data flow",
    "dependency": "dependency",
    "install_script": "install-time script",
}
_CATEGORY_ORDER = {name: index for index, name in enumerate(_CATEGORY_LABELS)}
# Critical: never bulk-judged and never auto-approved (an added or changed one).
_CRITICAL = frozenset({"permission", "db_drop", "install_script", "data_flow"})
_HIGH = frozenset({"outbound_host", "migration"})
_LOW = frozenset({"config_key", "telemetry_event", "cli_flag"})


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _trim(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# scope
# --------------------------------------------------------------------------- #

_AGENT_INSTRUCTION_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("lemoncrow_policy", "LemonCrow policy", re.compile(r"^\.lemoncrow/")),
    (
        "mcp_config",
        "MCP server configuration",
        re.compile(r"(?:^|/)\.?mcp(?:_servers?)?\.json$|(?:^|/)mcp\.ya?ml$"),
    ),
    (
        "agent_instructions",
        "Agent instructions",
        re.compile(
            r"(?:^|/)(?:AGENTS|CLAUDE|GEMINI|AGENT)\.md$|(?:^|/)CLAUDE\.local\.md$"
            r"|(?:^|/)\.(?:cursorrules|windsurfrules|clinerules|roorules|goosehints)$"
            r"|^\.github/(?:copilot-instructions\.md|instructions/|chatmodes/|agents/)"
        ),
    ),
    (
        "agent_tool_config",
        "Agent tool configuration",
        re.compile(
            r"^\.(?:claude|codex|gemini|cursor|windsurf|clinerules|roo|kiro|continue|opencode|amazonq|aider)/"
            r"|(?:^|/)\.aider\.conf\.ya?ml$|(?:^|/)opencode\.jsonc?$"
        ),
    ),
    (
        "prompt",
        "Prompt",
        re.compile(
            r"^\.github/prompts/|(?:^|/)prompts?/[^/]+\.(?:md|txt|ya?ml|json|j2|jinja2?|hbs|mustache|prompt|tmpl)$"
            r"|\.prompt(?:\.(?:md|ya?ml|txt|json))?$"
        ),
    ),
)


def agent_instruction_class(path: str) -> tuple[str, str] | None:
    """``(class, label)`` when *path* steers future agents, else ``None``."""

    if not path:
        return None
    for name, label, pattern in _AGENT_INSTRUCTION_RULES:
        if pattern.search(path):
            return name, label
    return None


def inventory_scope(path: str) -> bool:
    """Whether *path* contributes to the side-effect inventory and the register.

    Tests, documentation, vendored and generated files state no system side
    effect; agent-instruction files are handled (and never read) as item 45.
    """

    if not path or agent_instruction_class(path) is not None:
        return False
    if classify_path(path) not in _INVENTORY_CATEGORIES:
        return False
    return (
        side_effect_relevant(path)
        or PurePosixPath(path).name in _INSTALL_SCRIPT_NAMES
        or supported_path(path) is not None
    )


def analyzable(path: str, old_path: str = "") -> bool:
    """Whether any static analysis reads this changed file."""

    return any(
        candidate
        and (
            inventory_scope(candidate)
            or agent_instruction_class(candidate) is not None
            or verification_relevant(candidate)
        )
        for candidate in (path, old_path)
    )


def file_facts(path: str, text: str) -> tuple[SideEffectFact, ...]:
    return (*inventory_side_effects(path, text), *dependency_side_effects(path, text))


# --------------------------------------------------------------------------- #
# the register (item 35)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BaseFile:
    path: str
    size: int
    read: Callable[[], bytes | None]


@dataclass(frozen=True, slots=True)
class BaseTree:
    """One immutable base tree. ``key`` names exactly its content."""

    key: str
    base_sha: str
    files: Callable[[], Iterator[BaseFile]]


@dataclass(frozen=True, slots=True)
class ScanLimits:
    max_files: int = 20_000
    max_bytes: int = 48 << 20
    max_seconds: float = 20.0


_DEFAULT_LIMITS = ScanLimits()


@dataclass(frozen=True, slots=True)
class SideEffectRegister:
    """The base tree's inventory: identity -> value -> occurrences. Derived only."""

    key: str
    base_sha: str
    state: RegisterState
    entries: Mapping[str, Mapping[str, int]]
    files_scanned: int = 0
    note: str = ""

    def values(self, identity: str) -> Counter[str]:
        return Counter(dict(self.entries.get(identity, {})))

    def to_wire(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "base_sha": self.base_sha,
            "state": self.state,
            "files_scanned": self.files_scanned,
            "identities": len(self.entries),
            "note": self.note,
        }


def unavailable_register(note: str) -> SideEffectRegister:
    return SideEffectRegister(key="", base_sha="", state="changed_files_only", entries={}, note=note)


def build_register(
    tree: BaseTree | None,
    *,
    limits: ScanLimits = _DEFAULT_LIMITS,
    clock: Callable[[], float] = time.monotonic,
) -> SideEffectRegister:
    """Inventory the whole base tree within *limits*.

    lc-debt: a full base-tree scan per base commit (bounded by *limits*, cached
    per process). Upgrade: derive a commit's register from its parent's plus
    the parent..commit diff, stored per commit in a shared, evicting cache.
    """

    if tree is None:
        return unavailable_register("the base tree is not readable here; only the changed files' base side is known")
    entries: dict[str, Counter[str]] = {}
    started = clock()
    scanned = 0
    total = 0
    note = ""
    for item in tree.files():
        if not inventory_scope(item.path) or item.size > _MAX_FILE_BYTES:
            continue
        if (
            scanned >= limits.max_files
            or total + item.size > limits.max_bytes
            or clock() - started > limits.max_seconds
        ):
            note = (
                f"stopped after {scanned} files ({total} bytes); identities elsewhere in the base "
                "are not known, so some side effects may be reported as new"
            )
            break
        payload = item.read()
        if payload is None:
            continue
        scanned += 1
        total += len(payload)
        for fact in file_facts(item.path, payload.decode("utf-8", errors="replace")):
            entries.setdefault(fact.identity, Counter())[fact.value] += 1
    return SideEffectRegister(
        key=tree.key,
        base_sha=tree.base_sha,
        state="truncated" if note else "complete",
        entries={identity: dict(values) for identity, values in entries.items()},
        files_scanned=scanned,
        note=note,
    )


def _git_blobs(repo: Any, tree: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    for entry in tree:
        path = f"{prefix}/{entry.name}" if prefix else str(entry.name)
        mode = int(getattr(entry, "filemode", 0) or 0)
        if mode == 0o160000:
            continue
        if getattr(entry, "type_str", "") == "tree" or mode == 0o040000:
            try:
                subtree = repo[entry.id]
            except KeyError:
                continue
            yield from _git_blobs(repo, subtree, path)
        else:
            yield path, entry.id


def _content_reader(store: Any, digest: str) -> Callable[[], bytes | None]:
    def read() -> bytes | None:
        payload = store.read_source_content(digest) if digest else None
        return payload if isinstance(payload, bytes) else None

    return read


def _blob_reader(blob: Any) -> Callable[[], bytes | None]:
    def read() -> bytes | None:
        return bytes(blob.data)

    return read


def snapshot_base_tree(store: Any, revision: Any, roots: Sequence[Path | None]) -> BaseTree | None:
    """The Snapshot's base tree: its persisted manifest, else the base commit."""

    manifest = request_reads.source_tree_once(store, revision.review_id, revision.id, "old")
    if manifest is not None:
        rows = sorted((str(path), str(entry.get("content_digest") or "")) for path, entry in manifest.items())

        def manifest_files() -> Iterator[BaseFile]:
            for path, digest in rows:
                size = manifest[path].get("size")
                yield BaseFile(
                    path=path, size=int(size) if isinstance(size, int) else 0, read=_content_reader(store, digest)
                )

        return BaseTree(key=f"tree:{_digest(rows)}", base_sha=str(revision.base_sha or ""), files=manifest_files)
    sha = str(revision.base_sha or "")
    if not sha:
        return None
    from lemoncrow.pro.capabilities.review.gitdiff import _open_repo, _tree_for_sha

    for root in roots:
        if root is None:
            continue
        try:
            repo = _open_repo(root)
            git_tree = _tree_for_sha(repo, sha)
        except (KeyError, OSError, RuntimeError, ValueError):
            continue

        def git_files(repo: Any = repo, git_tree: Any = git_tree) -> Iterator[BaseFile]:
            for path, blob_id in _git_blobs(repo, git_tree):
                try:
                    blob = repo[blob_id]
                    size = int(blob.size)
                except (KeyError, ValueError, AttributeError):
                    continue
                yield BaseFile(path=path, size=size, read=_blob_reader(blob))

        return BaseTree(key=f"git:{sha}", base_sha=sha, files=git_files)
    return None


# --------------------------------------------------------------------------- #
# side-effect deltas (items 9, 34, 35)
# --------------------------------------------------------------------------- #


def _severity(category: str, change: str) -> tuple[BehaviorSeverity, bool]:
    if category in _CRITICAL:
        return ("high", True) if change != "removed" else ("medium", False)
    if category in _HIGH:
        return "high", False
    if category in _LOW:
        return "low", False
    return "medium", False


def _short(sha: str) -> str:
    return sha[:7] if sha else ""


def _register_phrase(register: SideEffectRegister) -> str:
    at = f" at base {_short(register.base_sha)}" if register.base_sha else " of the base"
    if register.state == "complete":
        return f"the side-effect register{at}"
    if register.state == "truncated":
        return f"the partial side-effect register{at}"
    return "the changed files' base side (the base register is unavailable)"


def _flow_parts(name: str) -> tuple[str, str, str]:
    tail = name.rsplit("::", 1)[-1]
    parts = tail.split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return "", "", tail


def _location(fact: SideEffectFact) -> str:
    return f"{fact.path}:{fact.line}" if fact.line else fact.path


def _side_effect_delta(
    producer: str,
    *,
    identity: str,
    change: BehaviorChange,
    before: frozenset[str],
    after: frozenset[str],
    base_fact: SideEffectFact | None,
    head_fact: SideEffectFact | None,
    register: SideEffectRegister,
) -> BehaviorDelta:
    fact = head_fact or base_fact
    assert fact is not None
    category = fact.category
    label = _CATEGORY_LABELS.get(category, category.replace("_", " "))
    display = _trim(fact.detail if category == "data_flow" and fact.detail else fact.name, 160)
    where = _register_phrase(register)
    if change == "added":
        title = f"New {label}: {display}"
        summary = f"Not in {where}; stated at {_location(fact)}."
    elif change == "removed":
        title = f"{label[:1].upper()}{label[1:]} removed: {display}"
        summary = f"In {where}; no longer stated anywhere after this change (last at {_location(fact)})."
    else:
        title = f"{label[:1].upper()}{label[1:]} changed: {display}"
        summary = (
            f"Value changed at {_location(fact)}: {', '.join(sorted(before))[:120] or '(none)'} → "
            f"{', '.join(sorted(after))[:120] or '(none)'}"
        )
    severity, critical = _severity(category, change)
    facts: list[tuple[str, str]] = []
    if critical:
        facts.append((AUTO_APPROVAL_FACT, AUTO_APPROVAL_BLOCKED))
    facts.append(("category", label))
    facts.append(("register", ("not in " if change == "added" else "in ") + where))
    if register.state != "complete":
        facts.append(("register_state", register.state))
    facts.append(("location", _location(fact)))
    if category == "data_flow":
        sink, data_class, term = _flow_parts(fact.name)
        facts.extend((("data_class", data_class), ("field", term), ("sink", sink.replace("_", " "))))
    elif fact.detail:
        facts.append(("detail", _trim(fact.detail, 200)))

    def observation(side: BehaviorSide, values: frozenset[str]) -> BehaviorObservation:
        if not values:
            return BehaviorObservation(side, status="absent", summary=f"no {label}")
        return BehaviorObservation(
            side,
            status="present",
            digest=_digest(sorted(values))[:32],
            summary=f"{label} present" + (f" ({len(values)} values)" if len(values) > 1 else ""),
        )

    paths = tuple(dict.fromkeys(item.path for item in (head_fact, base_fact) if item is not None))
    return BehaviorDelta(
        kind="side_effect",
        producer=producer,
        subject=identity,
        title=title,
        summary=summary,
        base=observation("base", before),
        head=observation("head", after),
        change=change,
        severity=severity,
        critical=critical,
        paths=paths,
        facts=tuple(facts),
        environment_fingerprint=_ENVIRONMENT,
    )


@dataclass(frozen=True, slots=True)
class _Side:
    values: dict[str, Counter[str]] = field(default_factory=dict)
    first: dict[str, SideEffectFact] = field(default_factory=dict)

    def add(self, facts: Iterable[SideEffectFact]) -> None:
        for fact in facts:
            self.values.setdefault(fact.identity, Counter())[fact.value] += 1
            self.first.setdefault(fact.identity, fact)


#: Categories another review surface already judges; counted, never a second target.
_JUDGED_ELSEWHERE: Final[Mapping[str, str]] = {"dependency": "the dependency review"}


def side_effect_deltas(
    producer: str,
    files: Sequence[ChangedText],
    register: SideEffectRegister,
) -> tuple[list[BehaviorDelta], dict[str, Any]]:
    """Changed-file inventory against the register: ``(deltas, register delta)``.

    ``post = register - base(changed) + head(changed)``, evaluated only for the
    identities the changed files state, so the work is incremental.
    """

    base, head = _Side(), _Side()
    for item in files:
        base_path = item.old_path or item.path
        if item.base and inventory_scope(base_path):
            base.add(file_facts(base_path, item.base))
        if item.head and inventory_scope(item.path):
            head.add(file_facts(item.path, item.head))
    rows: list[tuple[tuple[int, int, str], BehaviorDelta]] = []
    counts: dict[str, dict[str, int]] = {}
    elsewhere: dict[str, int] = {}
    for identity in sorted(set(base.values) | set(head.values)):
        changed_before = base.values.get(identity, Counter())
        registered = register.values(identity) | changed_before
        after_counter = registered - changed_before + head.values.get(identity, Counter())
        before = frozenset(value for value, count in registered.items() if count > 0)
        after = frozenset(value for value, count in after_counter.items() if count > 0)
        if before == after:
            continue
        fact = head.first.get(identity) or base.first[identity]
        change: BehaviorChange = "added" if not before else "removed" if not after else "changed"
        if change == "removed" and fact.category == "data_flow":
            continue  # a sensitive field that stops reaching a sink needs no judgment
        if fact.category in _JUDGED_ELSEWHERE:
            # One judgment point per fact: the dependency review's target (or the
            # manifest's own hunks) already carries it.
            elsewhere[fact.category] = elsewhere.get(fact.category, 0) + 1
            continue
        delta = _side_effect_delta(
            producer,
            identity=identity,
            change=change,
            before=before,
            after=after,
            base_fact=base.first.get(identity),
            head_fact=head.first.get(identity),
            register=register,
        )
        counts.setdefault(fact.category, {"added": 0, "removed": 0, "changed": 0})[change] += 1
        order = (0 if delta.critical else 1, _CATEGORY_ORDER.get(fact.category, 99), identity)
        rows.append((order, delta))
    rows.sort(key=lambda row: row[0])
    deltas = [delta for _, delta in rows[:_MAX_SIDE_EFFECT_TARGETS]]
    overflow: dict[str, list[BehaviorDelta]] = {}
    for _, delta in rows[_MAX_SIDE_EFFECT_TARGETS:]:
        overflow.setdefault(delta.subject.split(":", 1)[0], []).append(delta)
    for category, extra in sorted(overflow.items()):
        # Past the cap the rest stays visible, grouped: one target per category.
        label = _CATEGORY_LABELS.get(category, category)
        critical = any(item.critical for item in extra)
        names = [item.subject.split(":", 1)[-1] for item in extra]
        deltas.append(
            BehaviorDelta(
                kind="side_effect",
                producer=producer,
                subject=f"{category}:*more",
                title=f"{len(extra)} more {label} changes",
                summary="; ".join(_trim(item.title, 80) for item in extra[:8]) + (" …" if len(extra) > 8 else ""),
                base=BehaviorObservation("base", status="before", digest=_digest(["base", names])[:32]),
                head=BehaviorObservation("head", status="after", digest=_digest(["head", names])[:32]),
                change="changed",
                severity="high" if critical else "medium",
                critical=critical,
                paths=tuple(dict.fromkeys(path for item in extra for path in item.paths)),
                facts=(
                    *(((AUTO_APPROVAL_FACT, AUTO_APPROVAL_BLOCKED),) if critical else ()),
                    ("category", label),
                    ("grouped", f"{len(extra)} changes past the {_MAX_SIDE_EFFECT_TARGETS}-target cap"),
                    *((f"item {index + 1}", _trim(item.subject, 160)) for index, item in enumerate(extra[:8])),
                ),
                environment_fingerprint=_ENVIRONMENT,
            )
        )
    totals = {name: sum(row[name] for row in counts.values()) for name in ("added", "removed", "changed")}
    return deltas, {**totals, "by_category": counts, "judged_elsewhere": elsewhere}


# --------------------------------------------------------------------------- #
# agent instructions and tool permissions (item 45)
# --------------------------------------------------------------------------- #

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.@/-]{1,64}$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_SERVER_KEYS = ("mcpServers", "servers", "mcp_servers", "context_servers", "mcp")
_PERMISSION_LISTS = ("allow", "deny", "ask", "allowedTools", "autoApprove", "alwaysAllow", "trust")
_MODE_KEYS = (
    "approval_policy",
    "sandbox_mode",
    "defaultMode",
    "default_mode",
    "enableAllProjectMcpServers",
    "dangerouslySkipPermissions",
    "yolo",
)


def _structured(path: str, text: str) -> object:
    if not text.strip() or len(text) > _MAX_FILE_BYTES:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    try:
        if suffix in {".json", ".jsonc"}:
            return json.loads(text)
        if suffix == ".toml":
            return tomllib.loads(text)
        if suffix in {".yaml", ".yml"}:
            return yaml.safe_load(text)
    except (ValueError, tomllib.TOMLDecodeError, yaml.YAMLError):
        return None
    return None


def _name(value: object) -> str:
    text = str(value)
    return text if _SAFE_NAME.match(text) else f"(unnamed {_digest(text)[:8]})"


def agent_config_facts(path: str, text: str) -> dict[str, str]:
    """Typed, content-free facts of one agent/tool configuration file.

    Names only when they are plain identifiers; anything else is a digest. A
    value is never interpreted, so text inside cannot change what is reported.
    """

    doc = _structured(path, text)
    out: dict[str, str] = {}
    if not isinstance(doc, Mapping):
        return out

    def walk(node: Mapping[str, Any], depth: int) -> None:
        if depth > 4:
            return
        for key, value in node.items():
            name = str(key)
            if name in _SERVER_KEYS and isinstance(value, Mapping):
                for server, config in value.items():
                    out[f"mcp server {_name(server)}"] = _digest(config)[:12]
            elif name in _PERMISSION_LISTS and isinstance(value, list):
                for entry in value[:200]:
                    raw = str(entry)
                    tool = raw.split("(", 1)[0].strip()
                    shown = tool if _SAFE_VALUE.match(tool) else "(tool)"
                    suffix = "(…)" if "(" in raw else ""
                    out[f"{name} {shown}{suffix} #{_digest(raw)[:8]}"] = "listed"
            elif name in _MODE_KEYS and not isinstance(value, (Mapping, list)):
                shown = str(value)
                out[f"setting {name}"] = shown if _SAFE_VALUE.match(shown) else f"#{_digest(shown)[:8]}"
            elif name == "hooks":
                out["hooks"] = _digest(value)[:12]
            elif isinstance(value, Mapping):
                walk(value, depth + 1)

    walk(doc, 0)
    return out


def _file_observation(side: BehaviorSide, text: str, where: str, *, moved: bool) -> BehaviorObservation:
    if not text:
        return BehaviorObservation(side, status="absent", summary=f"no file at {where}")
    return BehaviorObservation(
        side,
        status="present_elsewhere" if moved else "present",
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
        summary=f"{len(text.splitlines())} lines at {where}",
    )


def _line_delta(base: str, head: str) -> tuple[int, int]:
    before, after = Counter(base.splitlines()), Counter(head.splitlines())
    return sum((after - before).values()), sum((before - after).values())


def agent_instruction_deltas(producer: str, files: Sequence[ChangedText]) -> list[BehaviorDelta]:
    """One critical target per changed agent-instruction / tool-permission file.

    Title, summary and observations come from the path and digests only.
    """

    out: list[BehaviorDelta] = []
    for item in files:
        klass = agent_instruction_class(item.path) or agent_instruction_class(item.old_path)
        if klass is None:
            continue
        name, label = klass
        if item.status == "added" or not item.base:
            change: BehaviorChange = "added"
        elif item.status == "deleted" or not item.head:
            change = "removed"
        else:
            change = "changed"
        verb = {"added": "added", "removed": "removed", "changed": "changed"}[change]
        if item.status == "renamed" and item.old_path and item.old_path != item.path:
            verb = f"moved from {item.old_path}"
        plus, minus = _line_delta(item.base, item.head)
        facts: list[tuple[str, str]] = [
            (AUTO_APPROVAL_FACT, AUTO_APPROVAL_BLOCKED),
            ("class", label),
            ("content", "untrusted; shown as data, never applied"),
            ("lines", f"+{plus} -{minus}"),
        ]
        before_config = agent_config_facts(item.old_path or item.path, item.base)
        after_config = agent_config_facts(item.path, item.head)
        for key in sorted(set(before_config) | set(after_config)):
            old, new = before_config.get(key), after_config.get(key)
            if old == new:
                continue
            state = "added" if old is None else "removed" if new is None else "changed"
            facts.append((state, key if state != "changed" or key.startswith("mcp") else f"{key}: {old} → {new}"))

        base_where = item.old_path or item.path
        out.append(
            BehaviorDelta(
                kind="agent_instruction",
                producer=producer,
                subject=f"{name}:{item.path}",
                title=f"{label} {verb}: {item.path}",
                summary=(
                    "Changes what future coding agents are told or allowed to do. Its content is "
                    "untrusted data: read it as text; LemonCrow never interprets or applies it."
                ),
                base=_file_observation("base", item.base, base_where, moved=base_where != item.path),
                head=_file_observation("head", item.head, item.path, moved=False),
                change=change,
                severity="high",
                critical=True,
                paths=tuple(dict.fromkeys(path for path in (item.path, item.old_path) if path)),
                facts=tuple(facts[:12]),
                environment_fingerprint=_ENVIRONMENT,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# one Snapshot
# --------------------------------------------------------------------------- #


def analyze_static(
    files: Sequence[ChangedText],
    register: SideEffectRegister,
    *,
    producer: str = PRODUCER_ID,
) -> tuple[list[BehaviorDelta], dict[str, Any]]:
    """Every static behavior delta of one Snapshot, plus its register delta."""

    side_effects, register_delta = side_effect_deltas(producer, files, register)
    return (
        [
            *agent_instruction_deltas(producer, files),
            *weakening_deltas(producer, files),
            *side_effects,
            *spec_deltas(producer, files),
        ],
        register_delta,
    )


def delta_to_wire(delta: BehaviorDelta) -> dict[str, Any]:
    def observation(item: BehaviorObservation | None) -> dict[str, Any] | None:
        return None if item is None else item.to_wire()

    return {
        "kind": delta.kind,
        "producer": delta.producer,
        "subject": delta.subject,
        "title": delta.title,
        "summary": delta.summary,
        "base": observation(delta.base),
        "head": observation(delta.head),
        "change": delta.change,
        "environment_fingerprint": delta.environment_fingerprint,
        "executor_class": delta.executor_class,
        "severity": delta.severity,
        "critical": delta.critical,
        "paths": list(delta.paths),
        "facts": [list(item) for item in delta.facts],
        "contract_version": delta.contract_version,
    }


def delta_from_wire(raw: Mapping[str, Any]) -> BehaviorDelta:
    def observation(value: object) -> BehaviorObservation | None:
        if not isinstance(value, Mapping):
            return None
        return BehaviorObservation(
            side=cast(BehaviorSide, str(value.get("side") or "base")),
            status=str(value.get("status") or ""),
            digest=str(value.get("digest") or ""),
            summary=str(value.get("summary") or ""),
        )

    return BehaviorDelta(
        kind=cast(BehaviorKind, str(raw["kind"])),
        producer=str(raw["producer"]),
        subject=str(raw["subject"]),
        title=str(raw["title"]),
        summary=str(raw.get("summary") or ""),
        base=observation(raw.get("base")),
        head=observation(raw.get("head")),
        change=cast(BehaviorChange, str(raw.get("change") or "changed")),
        environment_fingerprint=str(raw.get("environment_fingerprint") or ""),
        severity=cast(BehaviorSeverity, str(raw.get("severity") or "medium")),
        critical=raw.get("critical") is True,
        paths=tuple(str(item) for item in raw.get("paths") or ()),
        facts=tuple((str(item[0]), str(item[1])) for item in raw.get("facts") or () if len(item) == 2),
        contract_version=int(raw.get("contract_version") or BEHAVIOR_CONTRACT_VERSION),
    )


@dataclass(frozen=True, slots=True)
class StaticReport:
    """What ``side_effects.static`` did on one Snapshot, and why."""

    status: ReportStatus
    reason: str = ""
    deltas: tuple[BehaviorDelta, ...] = ()
    register: Mapping[str, Any] = field(default_factory=dict)
    register_delta: Mapping[str, Any] = field(default_factory=dict)
    degraded: tuple[str, ...] = ()
    files_analyzed: int = 0
    #: Where it ran: the process holding the Snapshot (``local`` or ``server``).
    executed_by: str = ""

    def summary_wire(self) -> dict[str, Any]:
        """The Brief's view: status, register and its delta; deltas travel as targets."""

        return {
            "status": self.status,
            "reason": self.reason,
            "register": dict(self.register),
            "register_delta": dict(self.register_delta),
            "degraded": list(self.degraded),
            "files_analyzed": self.files_analyzed,
            "targets": len(self.deltas),
            "executed_by": self.executed_by,
        }

    def to_wire(self) -> dict[str, Any]:
        return {**self.summary_wire(), "deltas": [delta_to_wire(delta) for delta in self.deltas]}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> StaticReport:
        register = raw.get("register")
        register_delta = raw.get("register_delta")
        return cls(
            status=cast(ReportStatus, str(raw.get("status") or "not_run")),
            reason=str(raw.get("reason") or ""),
            deltas=tuple(delta_from_wire(item) for item in raw.get("deltas") or () if isinstance(item, Mapping)),
            register=dict(register) if isinstance(register, Mapping) else {},
            register_delta=dict(register_delta) if isinstance(register_delta, Mapping) else {},
            degraded=tuple(str(item) for item in raw.get("degraded") or ()),
            files_analyzed=int(raw.get("files_analyzed") or 0),
            executed_by=str(raw.get("executed_by") or ""),
        )

    def token(self) -> str:
        return _digest(self.to_wire())[:32]


@dataclass(frozen=True, slots=True)
class ChangedPath:
    path: str
    old_path: str = ""
    status: str = "modified"


#: Plan statuses under which a report already stored for the Snapshot is served:
#: the work ran (and was paid for) under a plan that scheduled it, and budget
#: can only shrink once its own compute is metered.
_SERVED_WHEN_STORED = frozenset({"run", "limited_by_budget"})


def _on_event_loop() -> bool:
    """True on a thread that is running an asyncio event loop (a request handler)."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class StaticSideEffectService:
    """Runs ``side_effects.static`` exactly when the effective run plan says so.

    Every input is injected so the API, the hosted server and tests share it:
    *plan* returns the Snapshot's effective run plan; *changed* its changed
    paths; *read_text* one side's exact text (``""`` absent, ``None``
    unreadable); *base_tree* the base tree for the register; *recorder* the
    compute ledger's per-Snapshot recorder. *host* labels the process holding
    the Snapshot (``local`` or ``server``) on the metered compute.

    The analysis only reads stored content, so it runs in this process (plan
    locus ``review_host``), never on an event loop: a call from a loop thread
    starts it on a background thread and reports ``pending``. Concurrent
    callers for one Snapshot share one analysis; one base tree is scanned once
    at a time, and different base trees never wait on each other.
    """

    def __init__(
        self,
        *,
        store: Any,
        plan: Callable[[Any, Any], Any],
        changed: Callable[[Any], Sequence[ChangedPath]],
        read_text: Callable[[Any, str, str], str | None],
        base_tree: Callable[[Any], BaseTree | None],
        recorder: Callable[[str, str], Any],
        host: str = "local",
        limits: ScanLimits = _DEFAULT_LIMITS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._plan = plan
        self._changed = changed
        self._read_text = read_text
        self._base_tree = base_tree
        self._recorder = recorder
        self._host = host
        self._limits = limits
        self._clock = clock
        # Guards the tables below only; never held while analyzing or scanning.
        self._guard = threading.Lock()
        # lc-debt: per-process FIFO of 8 base-tree registers, keyed by tree content
        # (closed by FIFO eviction or process exit); upgrade to a shared store keyed
        # by commit if hosted fan-out re-scans the same bases.
        self._registers: OrderedDict[str, SideEffectRegister] = OrderedDict()
        # Finished reports by Snapshot id (immutable once stored); FIFO of 64.
        self._reports: OrderedDict[str, StaticReport] = OrderedDict()
        # In-flight work, keyed by Snapshot id and by base-tree key; removed when done.
        self._analyses: dict[str, Future[StaticReport]] = {}
        self._scans: dict[str, Future[SideEffectRegister]] = {}

    def _remember(self, revision: Any, report: StaticReport) -> StaticReport:
        with self._guard:
            self._reports[str(revision.id)] = report
            while len(self._reports) > 64:
                self._reports.popitem(last=False)
        return report

    def register_for(self, revision: Any) -> SideEffectRegister:
        """The base tree's register: scanned once per tree, concurrent callers of one tree share it."""

        tree = self._base_tree(revision)
        if tree is None:
            return build_register(None)
        with self._guard:
            cached = self._registers.get(tree.key)
            if cached is not None:
                return cached
            running = self._scans.get(tree.key)
            if running is None:
                running = Future()
                self._scans[tree.key] = running
                owner = True
            else:
                owner = False
        if not owner:
            return running.result()
        try:
            register = build_register(tree, limits=self._limits, clock=self._clock)
        except BaseException as exc:
            with self._guard:
                self._scans.pop(tree.key, None)
            running.set_exception(exc)
            raise
        with self._guard:
            self._registers[tree.key] = register
            while len(self._registers) > 8:
                self._registers.popitem(last=False)
            self._scans.pop(tree.key, None)
        running.set_result(register)
        return register

    def _stored(self, revision: Any) -> StaticReport | None:
        rows = self._store.derived_results(DERIVED_KIND, [revision.id])
        row = rows.get((revision.id, "current"))
        if row is None or row[0] != STAMP:
            return None
        try:
            return StaticReport.from_wire(row[1])
        except (KeyError, TypeError, ValueError):
            return None

    def _known(self, revision: Any) -> StaticReport | None:
        with self._guard:
            cached = self._reports.get(str(revision.id))
        if cached is not None:
            return cached
        stored = self._stored(revision)
        return None if stored is None else self._remember(revision, stored)

    def report(self, session: Any, revision: Any, *, latest: bool) -> StaticReport:
        """This Snapshot's static analysis, computing it at most once."""

        try:
            plan = self._plan(session, revision)
        except (RuntimeError, ValueError) as exc:
            return StaticReport("not_run", f"the effective run plan is unavailable: {exc}")
        entry = plan.entry(STATIC_ENTRY_ID) if plan is not None else None
        if entry is None:
            return StaticReport("skipped", "the effective run plan does not select side_effects.static")
        if entry.status not in _SERVED_WHEN_STORED:
            return StaticReport(cast(ReportStatus, entry.status), str(entry.reason or ""))
        known = self._known(revision)
        if known is not None:
            return known
        if entry.status != "run":
            return StaticReport(cast(ReportStatus, entry.status), str(entry.reason or ""))
        if entry.executor != REVIEW_HOST:
            return StaticReport(
                "not_run",
                f"planned on the {entry.executor} executor, which does not run static analysis; nothing was analyzed",
            )
        if not latest:
            return StaticReport("not_run", "not analyzed while this revision was current")
        rows = [row for row in self._changed(revision) if analyzable(row.path, row.old_path)]
        if not rows:
            return StaticReport(
                "inapplicable", "no changed file can state a side effect, a verification change or agent instructions"
            )
        if _on_event_loop():
            self._start_background(revision, rows)
            return StaticReport("pending", "static analysis of this revision is running off the request path")
        return self._shared_analysis(revision, rows)

    def _start_background(self, revision: Any, rows: Sequence[ChangedPath]) -> None:
        with self._guard:
            if str(revision.id) in self._analyses:
                return
        # lc-debt: one daemon thread per Snapshot analysis requested from an event
        # loop (deduplicated per Snapshot); upgrade: a durable hosted job.
        context = contextvars.copy_context()
        threading.Thread(
            target=context.run,
            args=(self._background_analysis, revision, tuple(rows)),
            name="lemoncrow-static-analysis",
            daemon=True,
        ).start()

    def _background_analysis(self, revision: Any, rows: Sequence[ChangedPath]) -> None:
        with contextlib.suppress(Exception):
            self._shared_analysis(revision, rows)

    def _shared_analysis(self, revision: Any, rows: Sequence[ChangedPath]) -> StaticReport:
        """One analysis per Snapshot: concurrent callers wait for the same result."""

        key = str(revision.id)
        with self._guard:
            running = self._analyses.get(key)
            if running is None:
                running = Future()
                self._analyses[key] = running
                owner = True
            else:
                owner = False
        if not owner:
            return running.result()
        try:
            report = self._compute(revision, rows)
        except BaseException as exc:
            with self._guard:
                self._analyses.pop(key, None)
            running.set_exception(exc)
            raise
        with self._guard:
            self._analyses.pop(key, None)
        running.set_result(report)
        return report

    def _compute(self, revision: Any, rows: Sequence[ChangedPath]) -> StaticReport:
        known = self._known(revision)
        if known is not None:
            return known
        started = self._clock()
        try:
            report = self._analyze(revision, rows)
        except Exception as exc:  # an analyzer bug must not take Review down
            return StaticReport("not_run", f"static analysis failed: {type(exc).__name__}: {str(exc)[:200]}")
        report = replace(report, executed_by=self._host)
        self._store.put_derived_results([_derived_row(revision, report)])
        self._recorder(str(revision.review_id), str(revision.id)).record_compute(
            STATIC_ENTRY_ID, self._host, "static", max(0.0, self._clock() - started)
        )
        return self._remember(revision, report)

    def _analyze(self, revision: Any, rows: Sequence[ChangedPath]) -> StaticReport:
        degraded: list[str] = []
        if len(rows) > _MAX_CHANGED_FILES:
            degraded.append(
                f"{len(rows) - _MAX_CHANGED_FILES} changed files past the {_MAX_CHANGED_FILES}-file bound "
                "were not statically analyzed"
            )
        files: list[ChangedText] = []
        for row in rows[:_MAX_CHANGED_FILES]:
            base_path = row.old_path or row.path
            base = "" if row.status == "added" else self._read_text(revision, base_path, "old")
            head = "" if row.status == "deleted" else self._read_text(revision, row.path, "new")
            if base is None or head is None:
                degraded.append(
                    f"{row.path}: {'base' if base is None else 'head'} content is unavailable; not analyzed"
                )
                continue
            if len(base) > _MAX_FILE_BYTES or len(head) > _MAX_FILE_BYTES:
                degraded.append(f"{row.path}: larger than 1 MiB; not analyzed")
                continue
            files.append(
                ChangedText(
                    path=row.path,
                    old_path=row.old_path,
                    status=row.status,
                    base=base,
                    head=head,
                    category=classify_path(row.path),
                )
            )
        register = self.register_for(revision)
        if register.state != "complete":
            degraded.append(f"side-effect register {register.state.replace('_', ' ')}: {register.note}")
        deltas, register_delta = analyze_static(files, register)
        return StaticReport(
            status="ran",
            deltas=tuple(deltas),
            register=register.to_wire(),
            register_delta=register_delta,
            degraded=tuple(degraded),
            files_analyzed=len(files),
        )


def _derived_row(revision: Any, report: StaticReport) -> Any:
    from lemoncrow.pro.capabilities.review.store import DerivedResult

    return DerivedResult(
        review_id=str(revision.review_id),
        revision_id=str(revision.id),
        kind=DERIVED_KIND,
        scope_key="current",
        stamp=STAMP,
        payload=report.to_wire(),
    )


class StaticSideEffectProducer:
    """The behavior producer: hands over what the service computed under the plan."""

    producer_id = PRODUCER_ID
    contract_version = BEHAVIOR_CONTRACT_VERSION

    def produce(self, context: BehaviorContext) -> BehaviorProduction:
        report = context.prepared.get(PRODUCER_ID)
        if not isinstance(report, StaticReport) or report.status != "ran":
            return BehaviorProduction()
        return BehaviorProduction(deltas=report.deltas, degraded=report.degraded)


def install_static_producer() -> None:
    """Register the producer through the single registration point (idempotent)."""

    register_behavior_producer(StaticSideEffectProducer(), replace_existing=True)


__all__ = [
    "DERIVED_KIND",
    "PRODUCER_ID",
    "STAMP",
    "STATIC_ANALYSIS_VERSION",
    "STATIC_ENTRY_ID",
    "BaseFile",
    "BaseTree",
    "ChangedPath",
    "ScanLimits",
    "SideEffectRegister",
    "StaticReport",
    "StaticSideEffectProducer",
    "StaticSideEffectService",
    "agent_config_facts",
    "agent_instruction_class",
    "agent_instruction_deltas",
    "analyzable",
    "analyze_static",
    "build_register",
    "delta_from_wire",
    "delta_to_wire",
    "file_facts",
    "install_static_producer",
    "inventory_scope",
    "side_effect_deltas",
    "snapshot_base_tree",
    "unavailable_register",
]
