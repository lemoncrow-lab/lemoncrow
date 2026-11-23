"""A short, evidence-backed reading of the Snapshot, not a comment feed."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from lemoncrow_client.review_words import part_count

# lc-debt: conservative extractive fallback when no model summary exists; replace
# with a validated, cached whole-change synthesis when that contract is available.
_CHANGE_REPORT = re.compile(
    r"^(?:implemented|added|updated|changed|fixed|moved|removed|replaced|reduced|"
    r"improved|enabled|disabled|preserved|introduced|resolved|hardened|"
    r"now\b|the change\b|this change\b)",
    re.IGNORECASE,
)


def is_change_summary(title: str) -> bool:
    """Conservative filter for captured prose; explicit guidance is trusted."""
    return bool(_CHANGE_REPORT.match(title))


def _short(value: Any, limit: int = 280) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def _sentence(value: Any) -> str:
    text = re.sub(r"^(?:#{1,6}\s+|[-*+]\s+)", "", str(value or "").strip())
    return _short(re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0])


_LIST_HEAD = 3


def _listed(names: Sequence[str]) -> str:
    """``a, b, c and 2 more``: the first few, then a count of the rest."""

    head = ", ".join(names[:_LIST_HEAD])
    rest = len(names) - _LIST_HEAD
    return f"{head} and {rest} more" if rest > 0 else head


def _joined(names: Sequence[str]) -> str:
    """``a, b and c``; past three, :func:`_listed`'s ``a, b, c and 2 more``."""

    if len(names) > _LIST_HEAD:
        return _listed(names)
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word if count == 1 else word + 's'}"


def _fact(text: str, kind: str, target_ids: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "text": text,
        "origin": "observed",
        "kind": kind,
        "target_ids": list(dict.fromkeys(target_ids)),
        "claim_ids": [],
    }


def _untested_by(tested: Mapping[str, Sequence[str]]) -> str:
    """``tests/a.py exercises f and g; nothing ran it`` from test file -> exercised names."""

    files = list(tested)
    names = list(dict.fromkeys(name for values in tested.values() for name in values))
    one = len(files) == 1
    return (
        f"{_joined(files)} {'exercises' if one else 'exercise'} {_joined(names)}; nothing ran {'it' if one else 'them'}"
    )


_CALLER_GAPS = {
    "no_index": "no code index",
    "not_run": "impact analysis has not run",
    "unavailable": "change packet unavailable",
}


#: Index gaps that can hide callers, in the words the Risk uses; the same set Blind spots marks as hiding consumers
#: (``frontend/src/review/investigation/impactReadiness.ts``). Any one of them means callers were not fully measured.
#: ``astgrep_unavailable`` is not one: structural matching falls back to text, which over-matches rather than misses.
_HIDING_GAPS: Final[Mapping[str, str]] = {
    "stale": "the code index is stale",
    "symbol_relations": "the code index could not answer who calls it",
    "ambiguous_symbol_counts": "a name is defined in more than one place",
    "symbol_resolution_truncated": "not every changed symbol was resolved",
    "caller_queries_truncated": "the caller search was capped",
    "index_outline_incomplete": "the code index had not read every definition",
    "cross_repository_sites": "callers in a submodule or nested repository were dropped",
    "impact_sites_truncated": "more call sites were found than are kept",
}


def _unmeasured(projection: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Symbols whose callers a pass that ran could not finish (a bound, an unsupported language), with why."""

    readiness = (projection.get("context") or {}).get("readiness") or {}
    return [item for item in readiness.get("unmeasured") or () if isinstance(item, Mapping) and item.get("symbol")]


def _index_gaps(projection: Mapping[str, Any]) -> list[str]:
    """Why a pass that ran may still have missed callers: the index state and degradations that hide them."""

    readiness = (projection.get("context") or {}).get("readiness") or {}
    if readiness.get("impact_pass") != "ran":
        return []
    names = [str(readiness.get("index_status") or "")]
    names += [str(name) for name in readiness.get("degraded") or () if isinstance(name, str)]
    return list(dict.fromkeys(_HIDING_GAPS[name] for name in names if name in _HIDING_GAPS))


def _callers_gap(projection: Mapping[str, Any]) -> str:
    """``callers of the changed code (no code index)`` when callers were not measured; ``""`` when they were.

    A pass that ran but could not finish some symbols names them: ``callers of f and g (the search stopped at its 5 s
    bound)``; one whose index had a gap that can hide callers says which: ``callers of the changed code (a name is
    defined in more than one place)``.
    """

    readiness = (projection.get("context") or {}).get("readiness") or {}
    impact_pass = readiness.get("impact_pass")
    if impact_pass == "ran":
        parts = [_symbols_gap(_unmeasured(projection))] if _unmeasured(projection) else []
        gaps = _index_gaps(projection)
        if gaps:
            parts.append(f"callers of the changed code ({'; '.join(gaps)})")
        return "; ".join(parts)
    reason = _CALLER_GAPS.get(str(impact_pass or ""), "")
    return f"callers of the changed code ({reason})" if reason else "callers of the changed code"


#: How the caller search says a name is defined in another file too (``pr_bot.callers._ambiguous``).
_ALSO_DEFINED: Final[str] = "the name is also defined in "


def _symbols_gap(unmeasured: Sequence[Mapping[str, Any]]) -> str:
    """One clause for every symbol whose callers were not measured: ``callers of f (why)`` for one name, else ``callers
    of 7 names: a, b, c and 4 more (why; why)``, the reasons said once each, a name defined elsewhere too as one."""

    names: list[str] = []
    reasons: dict[str, str] = {}
    for item in unmeasured:
        names.append(str(item["symbol"]))
        reason = str(item.get("reason") or "not measured")
        reasons.setdefault("also defined elsewhere" if reason.startswith(_ALSO_DEFINED) else reason, reason)
    names = list(dict.fromkeys(names))
    if len(names) == 1:
        return f"callers of {names[0]} ({'; '.join(reasons.values())})"
    also = "each is also defined in another file" if len(reasons) == 1 else "some are also defined in another file"
    said = [also if key == "also defined elsewhere" else key for key in reasons]
    why = "; ".join(said[:_LIST_HEAD]) + (
        f"; and {len(said) - _LIST_HEAD} more reasons" if len(said) > _LIST_HEAD else ""
    )
    return f"callers of {len(names)} names: {_listed(names)} ({why})"


def _tests_gap(projection: Mapping[str, Any]) -> str:
    """``which tests exercise the changed code``, or the symbols' names when only some callers went unmeasured."""

    names = list(dict.fromkeys(str(item["symbol"]) for item in _unmeasured(projection)))
    if names and not _index_gaps(projection):
        return f"which tests exercise {_listed(names)}"
    return "which tests exercise the changed code"


def _unchecked(projection: Mapping[str, Any], recorded: Mapping[str, str], tested: Mapping[str, Sequence[str]]) -> str:
    """``Not checked: ...`` from the recorded checks and the caller pass's readiness; ``""`` when nothing is.

    With nothing recorded, test files the consumer inventory lists as using a
    changed part are named: they exist and nothing ran them.
    """

    parts: list[str] = []
    if not recorded:
        parts.append(_untested_by(tested) if tested else "no test or check ran on this revision")
    readiness = (projection.get("context") or {}).get("readiness") or {}
    if readiness.get("impact_pass") in _CALLER_GAPS or _unmeasured(projection) or _index_gaps(projection):
        parts.append(_callers_gap(projection))
    return "Not checked: " + "; ".join(parts) if parts else ""


_LEAD_LINES = 3


def _sensitive(target: Any) -> str:
    """``access-control code`` when the canonical priority flagged the target's path; ``""`` otherwise."""

    from .criticality import sensitive_area, sensitive_path_attention

    priority = getattr(target, "priority", None)
    reasons = getattr(priority, "reasons", ()) or ()
    if not any(getattr(reason, "code", "") == "sensitive_path_attention" for reason in reasons):
        return ""
    label = sensitive_path_attention(str(target.path))
    return sensitive_area(label) if label else ""


def _callers(row: Any) -> str:
    """``called from app/views.py``; past three files ``called from 5 files: a, b, c and 2 more``."""

    verb = "called from" if row.calls_only else "used in"
    count = len(row.files)
    if count <= _LIST_HEAD and not row.at_least:
        return f"{verb} {_joined(list(row.files))}"
    bound = f"at least {count}" if row.at_least else str(count)
    return f"{verb} {bound} files: {_listed(list(row.files))}"


def change_facts(
    projection: Mapping[str, Any],
    recorded: Mapping[str, str],
    *,
    packet: Mapping[str, Any] | None,
    targets: Sequence[Any],
    check_command: Callable[[], str] | None = None,
) -> list[dict[str, Any]]:
    """Short statements built only from engine facts, riskiest first; no model, no inferred intent.

    ``targets`` arrive in the canonical review order, and the lead lines keep
    it: each changed symbol with outside callers or on a sensitive path, at
    most three lines, then one line naming the rest. The remaining changed
    symbols, new and deleted files, and what nobody checked follow. A clause
    the packet and the consumer inventory cannot back is left out.
    ``check_command`` reads a recorded check command for the repository; it is
    called only when nothing was checked, and its ``""`` means none is recorded.
    """

    from .consumer_inventory import outside_reach

    files = [item for item in (packet or {}).get("files") or () if isinstance(item, Mapping)]
    status = {str(item.get("path") or ""): str(item.get("status") or "") for item in files}
    change: dict[tuple[str, str], str] = {}
    for symbol in (packet or {}).get("symbols") or ():
        if isinstance(symbol, Mapping):
            path = str(symbol.get("file_path") or "")
            for name in (symbol.get("qualified_name"), symbol.get("symbol_name")):
                if name:
                    change.setdefault((path, str(name)), str(symbol.get("change") or ""))

    def change_of(target: Any) -> str:
        path, name = str(target.path), str(target.symbol)
        return change.get((path, name)) or change.get((path, name.rsplit(".", 1)[-1])) or "unknown"

    symbol_targets = [target for target in targets if getattr(target, "kind", "") == "symbol" and target.symbol]
    facts: list[dict[str, Any]] = []
    covered: set[str] = set()

    reach = outside_reach(projection.get("context"))
    # Test files that use a changed part, in review order; named on the not-checked line when nothing ran.
    tested: dict[str, list[str]] = {}
    exercised: list[str] = []
    for target in symbol_targets:
        row = reach.get(target.target_id)
        if row is not None and row.tests:
            exercised.append(target.target_id)
            for path in row.tests:
                tested.setdefault(path, []).append(str(target.symbol))
    name_tests = not recorded and bool(tested)
    lead: list[str] = []
    for target in symbol_targets:
        row = reach.get(target.target_id)
        calls = row is not None and bool(row.files) and row.change != "added"
        area = _sensitive(target)
        if not calls and not area:
            continue
        covered.add(target.target_id)
        if len(lead) >= _LEAD_LINES:
            lead.append(target.target_id)
            continue
        how = row.change if row is not None else change_of(target)
        verb = "removed" if how == "deleted" else "added" if how == "added" else "changed"
        clauses = [area] if area else []
        if calls and row is not None:
            tests = f", plus {_plural(len(row.tests), 'test file')}" if row.tests and not name_tests else ""
            clauses.append(_callers(row) + tests)
        text = " · ".join([f"{target.symbol} ({target.path}) {verb}", *clauses])
        facts.append(_fact(text, "reach", (target.target_id,)))
        lead.append(target.target_id)
    rest = set(lead[_LEAD_LINES:])
    if rest:
        names = [f"{item.symbol} ({item.path})" for item in symbol_targets if item.target_id in rest]
        facts.append(
            _fact(f"And {_plural(len(rest), 'more part')} like these: {_listed(names)}", "reach", lead[_LEAD_LINES:])
        )

    new_files = [path for path, value in status.items() if value == "added"]
    deleted_files = [path for path, value in status.items() if value == "deleted"]
    whole = set(new_files) | set(deleted_files)
    # Symbols in a new or deleted file are named on that file's line instead.
    by_label: dict[str, list[Any]] = {"Changed": [], "Removed": [], "Added": []}
    for target in symbol_targets:
        if target.target_id not in covered and target.path not in whole:
            kind = change_of(target)
            by_label["Removed" if kind == "deleted" else "Added" if kind == "added" else "Changed"].append(target)
    for label, rows in by_label.items():
        if rows:
            names = [f"{target.symbol} ({target.path})" for target in rows]
            facts.append(_fact(f"{label}: {_listed(names)}", "symbols", [target.target_id for target in rows]))

    def file_fact(paths: list[str], one: str, many: str) -> None:
        if not paths:
            return
        if len(paths) == 1:
            names = [str(target.symbol) for target in symbol_targets if target.path == paths[0]]
            detail = f" ({_listed(names)})" if names else ""
            text = f"{one}: {paths[0]}{detail}"
        else:
            text = f"{many}: {_listed(paths)}"
        listed = set(paths)
        ids = [target.target_id for target in targets if target.path in listed]
        facts.append(_fact(text, "files", ids))

    file_fact(new_files, "New file", "New files")
    file_fact(deleted_files, "Deleted file", "Deleted files")
    if not facts and status:
        facts.append(_fact(f"Changed files: {_listed(list(status))}", "files"))
    unchecked = _unchecked(projection, recorded, tested)
    if unchecked:
        fact = _fact(unchecked, "unchecked", exercised if name_tests else ())
        command = check_command() if check_command is not None and not recorded else ""
        if command:
            fact["command"] = command
        facts.append(fact)
    return facts


_RISK_KIND_ORDER = {"sensitive": 0, "removed_safeguard": 1, "outside_callers": 2, "untested": 3}
_NO_CHECK = " and no check ran on this revision"
#: A part judged on the current revision: Reviewed or Needs changes both mean someone read it.
_JUDGED = frozenset({"reviewed", "needs_changes"})


def understanding_text(reviewed: int, total: int, *, risky: bool) -> str:
    """``0 of 2 risky parts reviewed``; with nothing risky, ``1 of 3 parts reviewed``."""

    if risky:
        return f"{reviewed} of {total} risky {'part' if total == 1 else 'parts'} reviewed"
    return f"{reviewed} of {part_count(total)} reviewed"


def change_risk(
    projection: Mapping[str, Any],
    recorded: Mapping[str, str],
    *,
    packet: Mapping[str, Any] | None,
    targets: Sequence[Any],
    checks_unmeasured: str = "",
) -> dict[str, Any]:
    """Risk (what could this break?) and Understanding (do I know what it does?), from measured facts only.

    Per part, the risk facts are: a sensitive area (the path-only
    :func:`criticality.sensitive_path_attention`, so a Reviewed mark never
    hides it), a
    removed or narrowed safeguard (:func:`change_risk.removed_safeguards`),
    files outside the change that call it (the consumer inventory the Map
    shares), and test files that exercise it when nothing ran. Each fact
    carries its own level; the change's level is
    :func:`change_risk.risk_level` over all of them, and its reason is the top
    fact plus ``· and N more`` for the other facts. ``not_measured`` names
    every input that could not be read.
    *checks_unmeasured* names the check records when they could not be read
    (the GitHub Action's CI read, refused); then whether a check ran is
    unknown, never "none ran".
    Other repositories of the workspace that depend on the change (the
    packet's ``workspace_impact``) never set the level; a Low reason then says
    it covers this repository and names them.
    Understanding counts the parts judged (Reviewed or Needs changes) on this
    revision among the risky parts: the queue's Review first parts
    (``TargetPriority.review_first``, which a judgment does not clear), every
    part when there are none. The Reader recounts it live from its marks.
    """

    from .change_risk import RISK_LABELS, RISK_ORDER, removed_safeguards, risk_level, unread_safeguard_files
    from .consumer_inventory import outside_reach
    from .criticality import sensitive_area, sensitive_path_attention
    from .workspace_impact import risk_clause

    reach = outside_reach(projection.get("context"))
    callers_gap = _callers_gap(projection)
    checks_ran: bool | None = None if checks_unmeasured else bool(recorded)
    after = _NO_CHECK if checks_ran is False else ""
    safeguards = removed_safeguards(packet, targets)
    rows: list[tuple[int, dict[str, Any]]] = []
    for position, target in enumerate(targets):
        name = str(target.symbol or target.path)
        target_id = str(target.target_id)
        found: list[tuple[str, str, str]] = []
        label = sensitive_path_attention(str(target.path))
        area = sensitive_area(label) if label else ""
        if area:
            found.append(("sensitive", "high", f"{name} is {area}"))
        found.extend(("removed_safeguard", "high", f"{name} {phrase}") for phrase in safeguards.get(target_id, ()))
        row = reach.get(target_id)
        if row is not None and row.files and row.change != "added":
            reach_level = "high" if checks_ran is False else "medium"
            found.append(("outside_callers", reach_level, f"{name} is {_callers(row)}{after}"))
        if row is not None and row.tests and checks_ran is False:
            tests = _joined(list(row.tests))
            found.append(("untested", "medium", f"{name} is exercised by {tests}, which did not run"))
        rows.extend(
            (position, {"text": text, "kind": kind, "level": level, "target_ids": [target_id]})
            for kind, level, text in found
        )
    rows.sort(key=lambda item: (RISK_ORDER[item[1]["level"]], _RISK_KIND_ORDER[item[1]["kind"]], item[0]))
    facts = [fact for _position, fact in rows]
    kinds = {fact["kind"] for fact in facts}
    level = risk_level(
        removed_safeguard="removed_safeguard" in kinds,
        sensitive="sensitive" in kinds,
        outside_callers="outside_callers" in kinds,
        tested_not_run="untested" in kinds,
        callers_measured=not callers_gap,
        checks_ran=checks_ran,
    )
    not_measured: list[str] = []
    if callers_gap:
        not_measured.append(callers_gap)
        not_measured.append(_tests_gap(projection))
    if checks_unmeasured:
        not_measured.append(checks_unmeasured)
    for reason, paths in unread_safeguard_files(packet, targets).items():
        not_measured.append(f"removed safeguards in {_listed(paths)} ({reason})")
    if level in ("high", "medium"):
        more = len(facts) - 1
        reason_text = str(facts[0]["text"]) + (f" · and {more} more" if more else "")
    elif level == "low":
        reason_text = "no sensitive code, outside callers, removed safeguards or unrun tests found"
        # Workspace reach is never a Risk fact; the Low line says it covers this repository and points at it.
        reach_elsewhere = risk_clause(packet)
        if reach_elsewhere:
            reason_text += f" in this repository · {reach_elsewhere} (see Workspace impact)"
    else:
        reason_text = "could not measure " + (not_measured[0] if not_measured else "callers or checks")
    # The risky parts are the queue's Review first parts; with none, every part counts.
    risky = [
        str(target.target_id)
        for target in targets
        if getattr(getattr(target, "priority", None), "review_first", False) is True
    ]
    counted = set(risky) if risky else {str(target.target_id) for target in targets}
    reviewed = sum(
        1 for target in targets if str(target.target_id) in counted and getattr(target, "state", "") in _JUDGED
    )
    return {
        "level": level,
        "label": RISK_LABELS[level],
        "reason": reason_text,
        "text": f"{RISK_LABELS[level]} · {reason_text}",
        "facts": facts,
        "not_measured": not_measured,
        "risky_target_ids": risky,
        "understanding": {
            "reviewed": reviewed,
            "total": len(counted),
            "risky": bool(risky),
            "text": understanding_text(reviewed, len(counted), risky=bool(risky)),
        },
    }


def recorded_checks(checks: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """``{check name: STATUS}`` of the checks whose result is current on the revision: what Risk reads as having run.

    *checks* are the revision's verification rows (``verification.build_verification_projection``'s ``checks``).
    """

    recorded: dict[str, str] = {}
    for check in checks:
        if check.get("state") != "current":
            continue
        key = str(check.get("name") or check.get("source_ref") or "")
        if key:
            recorded[key] = str(check.get("status") or "UNKNOWN").upper()
    return recorded


def build_brief_summary(
    projection: Mapping[str, Any],
    checks: Sequence[Mapping[str, Any]] = (),
    *,
    packet: Mapping[str, Any] | None = None,
    targets: Sequence[Any] = (),
    check_command: Callable[[], str] | None = None,
    title: str = "",
) -> dict[str, Any]:
    """Distill current explanations; every inferred sentence keeps exact refs.

    ``facts`` always carries the engine-fact statements (:func:`change_facts`);
    with no author or AI explanation they are also the summary's statements.
    ``risk`` is :func:`change_risk`: the Risk and Understanding signals.
    ``headline`` is what the Brief leads with: the Review's own *title* (the first statement only when it has none)
    and, under it, the count line of files, parts and added and removed lines.
    """

    items = projection.get("items") or ()
    intents = projection.get("plan", {}).get("intents") or ()
    paragraphs: list[str] = []
    statements: list[dict[str, Any]] = []
    sources: set[str] = set()
    seen: set[str] = set()

    def append(
        text: str,
        source: str,
        *,
        target_ids: Sequence[str] = (),
        claim_ids: Sequence[str] = (),
    ) -> bool:
        identity = text.casefold().rstrip(".")
        if not text or identity in seen:
            return False
        seen.add(identity)
        paragraphs.append(text)
        statements.append(
            {
                "text": text,
                "origin": source,
                "target_ids": list(dict.fromkeys(str(value) for value in target_ids if str(value))),
                "claim_ids": list(dict.fromkeys(str(value) for value in claim_ids if str(value))),
            }
        )
        sources.add(source)
        return True

    for intent in intents:
        if append(_sentence(intent.get("intent")), "author"):
            break

    intent_count = len(paragraphs)
    explanations = 0
    for item in items:
        if item.get("stale") or item.get("rationale_origin") == "none":
            continue
        origin = str(item.get("rationale_origin") or "author")
        text = _sentence(item.get("after") or item.get("rationale") or item.get("title"))
        if str(item.get("id", "")).startswith("agent:"):
            text = _sentence(item.get("title"))
            if not is_change_summary(text):
                continue
        if text and text.casefold().rstrip(".") not in seen:
            explanations += 1
            if len(paragraphs) < 3:
                append(
                    text,
                    origin,
                    target_ids=tuple(item.get("primary_target_ids") or ())
                    + tuple(item.get("related_target_ids") or ()),
                    claim_ids=tuple(item.get("claim_ids") or ()),
                )

    coverage = projection.get("coverage") or {}
    scope = f"{coverage.get('file_count', 0)} files · {part_count(int(coverage.get('target_count') or 0))}"

    recorded = recorded_checks(checks)
    if recorded:
        passed = sum(status == "PASS" for status in recorded.values())
        failed = sum(status == "FAIL" for status in recorded.values())
        unresolved = len(recorded) - passed - failed
        parts = [f"{passed} passed", f"{failed} failed"]
        if unresolved:
            parts.append(f"{unresolved} unresolved")
        verification = "Recorded checks: " + ", ".join(parts) + "."
    else:
        verification = "No current verification results recorded."

    facts = change_facts(projection, recorded, packet=packet, targets=targets, check_command=check_command)
    risk = change_risk(projection, recorded, packet=packet, targets=targets)
    if not paragraphs:
        if facts:
            for fact in facts:
                paragraphs.append(fact["text"])
                statements.append(fact)
            sources.add("observed")
        else:
            append("No change explanation has been recorded for this revision.", "observed")

    outstanding = projection.get("decision_progress", {}).get("outstanding")
    if outstanding is None:
        outstanding = len(projection.get("decisions") or ())
    questions = projection.get("questions") or ()
    actions = projection.get("actions") or ()
    attention = []
    if outstanding:
        attention.append(f"{outstanding} open decision{'s' if outstanding != 1 else ''}")
    priority_targets = {
        target_id
        for item in items
        if item.get("attention") == "review_first"
        for target_id in item.get("primary_target_ids") or ()
    }
    if priority_targets:
        attention.append(
            f"{_plural(len(priority_targets), 'part')} {'needs' if len(priority_targets) == 1 else 'need'} attention"
        )
    if questions or actions:
        attention.append(
            f"{len(questions) + len(actions)} issue{'s' if len(questions) + len(actions) != 1 else ''} to investigate"
        )
    files = int(coverage.get("file_count") or 0)
    added = sum(int(getattr(target, "additions", 0) or 0) for target in targets)
    removed = sum(int(getattr(target, "deletions", 0) or 0) for target in targets)
    counts = [_plural(files, "file"), part_count(int(coverage.get("target_count") or 0))]
    if added + removed:
        counts.append(f"+{added} \N{MINUS SIGN}{removed}")
    return {
        "headline": {
            "title": " ".join(str(title or "").split()) or (statements[0]["text"] if statements else ""),
            "counts": " · ".join(counts),
        },
        "paragraphs": paragraphs,
        "statements": statements,
        "facts": facts,
        "risk": risk,
        "origin": next(iter(sources)) if len(sources) == 1 else "mixed",
        "scope": scope,
        "verification": verification,
        "attention": "; ".join(attention),
        "additional_explanations": max(0, explanations - (len(paragraphs) - intent_count)),
    }
