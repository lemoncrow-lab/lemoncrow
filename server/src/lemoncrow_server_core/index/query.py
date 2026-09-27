"""Queries: Layer 1 rows, filtered by Layer 2 membership, joined to Layer 3.

This is where the three-layer design stops being an argument about storage and
starts being the reason it is worth having. A query never copies Layer 1 into a
view. It takes the view's membership rows -- a few thousand of them -- and uses
the digests they select as the filter for an org-scoped Layer-1 search. N
worktrees of one repository therefore share one set of immutable artifacts and
pay only for their own manifests.

Four kinds, because they degrade differently:

``lexical`` and ``symbol``
    Answered from Layer 1 and membership alone. They are never flagged
    degraded for a link-layer reason -- that is the "exact path and lexical
    search stay available" half of the degradation contract.
``relations``
    Needs Layer 3. The layer is built on demand, within its budget; if it
    cannot finish inline the answer still returns, flagged, with the reason
    named.
``semantic``
    Needs the Layer-1 embedding rows for every member. Same bounded-build,
    flag-don't-fail rule.

A file over the content size cap is manifested but not uploaded. It is still
searchable **by path**, and a hit on one is marked ``content_indexed: false``
so the answer never implies the server read bytes it does not have.

The digests a view's membership selects are filtered through :mod:`.boundary`
before any of that happens. Membership is what the *client* declared; the
boundary is what the view has actually demonstrated possession of. Searching the
first rather than the second is how a manifest row naming a colleague's digest
turned into a content-derived answer about their source -- a lexical hit on a
literal inside it, the symbols it defines, the modules it imports, its language
and its line count -- without a byte ever being uploaded. A path a view declares
but cannot address is still matched **by path**, because the path is the
caller's own declaration, and it is reported exactly as an over-cap file is:
``content_indexed: false``.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import regex

from lemoncrow.core.capabilities.context_engine import ContextQuery, Ranker, get_ranker

from ..errors import AgentAction, ErrorCode, ServerError
from .analysis import embed_terms, parse_query, semantic_embedding
from .boundary import addressable_in
from .candidates import hit_candidate, record_ranked_candidates
from .contracts import Edge, EdgeKind, IndexBackend, IndexLayerStatus, ManifestEntry
from .deferred import enqueue_deferred_layer
from .planner import QueryIntent, QueryPlan, plan_code_query

__all__ = ["QUERY_KINDS", "IndexQuery", "QueryAnswer", "QueryHit"]

QUERY_KINDS: Final[frozenset[str]] = frozenset({"lexical", "symbol", "relations", "semantic"})

_DEFAULT_LIMIT: Final[int] = 50
_MAX_LIMIT: Final[int] = 200
_CANDIDATE_SCAN_LIMIT: Final[int] = 4_096
_PATH_CANDIDATE_LIMIT: Final[int] = 4_096
_PRECISE_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_DEFINITION_ALT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:def|class|function|fn|type|struct|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_IDENTIFIER_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_AUXILIARY_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(^|/)(docs?(?:-internal)?|documentation|examples?|galleries|benchmarks?|vendor|third_party)(/|$)"
    r"|\.(?:md|rst|ipynb|json|lock)$",
    re.IGNORECASE,
)
_AUXILIARY_QUERY_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:docs?|documentation|examples?|galler(?:y|ies)|benchmarks?|vendor|third[_ -]?party|readme)\b",
    re.IGNORECASE,
)
_AUXILIARY_SCORE_FACTOR: Final[float] = 0.68
_QUERY_SYMBOL_STOP: Final[frozenset[str]] = frozenset(
    {
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "case",
        "class",
        "continue",
        "def",
        "del",
        "do",
        "else",
        "except",
        "false",
        "finally",
        "for",
        "from",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "none",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "self",
        "super",
        "true",
        "try",
        "while",
        "with",
        "yield",
    }
)


def _split_pipe_query(query: str) -> tuple[str, ...]:
    """Interpret grep-style ``a|b|c`` as retrieval alternatives, not a body scan.

    Historical LemonCrow queries commonly came from grep/agent traces and use a
    pipe as an OR between identifiers/literals. Treating every such query as a
    regular expression forces an O(view) source scan and discards the exact
    definition channel. This mirrors the legacy retriever's normalization while
    leaving genuine non-pipe regexes on the bounded regex path below.
    """
    if "|" not in query:
        return ()
    seen: dict[str, None] = {}
    for part in query.split("|"):
        stripped = part.strip().lstrip("^").rstrip("$")
        stripped = re.sub(r"\\[bBsSwWdD]", "", stripped).strip()
        if len(stripped) < 3 or not re.search(r"[A-Za-z0-9_]", stripped):
            continue
        seen[stripped] = None
    return tuple(seen) if len(seen) >= 2 else ()


def _alternative_symbol(term: str) -> str:
    """Exact symbol named by one pipe alternative, if it has one."""
    definition = _DEFINITION_ALT_RE.search(term)
    if definition is not None:
        return definition.group(1)
    normalized = term.strip()
    if _PRECISE_SYMBOL_RE.fullmatch(normalized):
        return normalized.rsplit(".", 1)[-1]
    return ""


def _is_code_shaped_identifier(token: str) -> bool:
    """Whether a token is distinctive enough to merit an exact symbol probe."""
    return (
        "_" in token
        or token.isupper()
        or any(character.isupper() for character in token[1:])
        or token.startswith("__")
        or token.endswith("__")
    )


def _looks_like_regex_query(query: str) -> bool:
    """Whether ``query`` intentionally uses regex syntax rather than prose punctuation.

    Coding-task and SWE-bench prompts routinely contain parentheses, brackets,
    question marks, URLs, and snippets. Treating the presence of *any* regex
    metacharacter as regex intent turns those natural-language requests into a
    full repository body scan, and an unmatched parenthesis can reject the
    search outright. Require syntax that is strongly indicative of a regex.
    """
    stripped = query.strip()
    if not stripped:
        return False
    if stripped.startswith("^") or stripped.endswith("$") or "(?" in stripped:
        return True
    if any(token in stripped for token in (".*", ".+", ".?")):
        return True
    if re.search(r"\\[()\[\]{}.^$*+?|bBdDsSwWAZz]", stripped):
        return True
    if len(stripped) <= 256 and re.search(r"\[[^\]\n]{1,32}\]", stripped):
        return True
    return False


def _regex_required_literal(query: str) -> str:
    """Longest conservative literal required by a simple regex.

    This is a prefilter only. Unsupported constructs return ``""`` and fall
    back to the bounded full-view regex scan. We deliberately avoid guessing
    through alternation, groups, or character classes because a false mandatory
    literal would lose valid matches. Escaped punctuation is literal; boundary
    and character-class escapes split fragments. ``.*``/``.+`` simply separate
    required literal runs, which covers the common code-search shapes.
    """
    if "|" in query or "(?" in query:
        return ""
    fragments: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            value = "".join(current).strip()
            if len(value) >= 3 and re.search(r"[A-Za-z0-9_]", value):
                fragments.append(value)
            current.clear()

    index = 0
    while index < len(query):
        char = query[index]
        if char == "\\":
            if index + 1 >= len(query):
                flush()
                break
            escaped = query[index + 1]
            if escaped in "bBdDsSwWAZz":
                flush()
            else:
                current.append(escaped)
            index += 2
            continue
        if char in "[]()":
            return ""
        if char in "^$":
            flush()
            index += 1
            continue
        if char == ".":
            flush()
            index += 1
            if index < len(query) and query[index] in "*+?":
                index += 1
            continue
        if char in "*?":
            if current:
                current.pop()
            flush()
            index += 1
            continue
        if char == "+":
            flush()
            index += 1
            continue
        if char == "{":
            close = query.find("}", index + 1)
            if close < 0:
                return ""
            quantifier = query[index + 1 : close]
            minimum = quantifier.split(",", 1)[0].strip()
            if minimum in {"", "0"} and current:
                current.pop()
            flush()
            index = close + 1
            continue
        current.append(char)
        index += 1
    flush()
    return max(fragments, key=len, default="")


def _query_symbols(query: str, *, limit: int = 12) -> tuple[str, ...]:
    """Exact symbol anchors embedded in a broader code/prose query.

    A whole-query symbol lookup loses obvious anchors such as
    ``_CODE_INTEL_TOOLS definition``. The legacy HEF planner probed explicit
    definitions, precise identifiers, and code-shaped tokens separately. Keep
    that high-precision behavior here without turning ordinary prose words into
    dozens of symbol lookups.
    """
    candidates: list[str] = [match.group(1) for match in _DEFINITION_ALT_RE.finditer(query)]
    normalized = query.strip()
    if _PRECISE_SYMBOL_RE.fullmatch(normalized):
        candidates.append(normalized.rsplit(".", 1)[-1])
    for token in _IDENTIFIER_TOKEN_RE.findall(query):
        if token.lower() in _QUERY_SYMBOL_STOP or len(token) < 3:
            continue
        if _is_code_shaped_identifier(token):
            candidates.append(token)

    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        lowered = candidate.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(candidate)
        if len(out) >= limit:
            break
    return tuple(out)


@dataclass(frozen=True, slots=True)
class QueryHit:
    """One result, always named by the view path rather than by a digest.

    Paths are what the caller asked about; digests are an implementation fact
    of Layer 1, and putting one in an answer would leak the shape of the
    content-addressed store into the tool surface for no benefit.
    """

    path: str
    score: float
    language: str = ""
    line_count: int = 0
    size: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "score": round(self.score, 6),
            "language": self.language,
            "line_count": self.line_count,
            "size": self.size,
        }
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload


@dataclass(frozen=True, slots=True)
class QueryAnswer:
    kind: str
    hits: tuple[QueryHit, ...]
    at_view_revision: int
    searched_paths: int
    degraded: bool = False
    degraded_reason: str = ""
    truncated: bool = False
    remaining_hits: int = 0
    plan: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "hits": [hit.to_wire() for hit in self.hits],
            "view_revision": self.at_view_revision,
            "searched_paths": self.searched_paths,
            "degraded": self.degraded,
            "truncated": self.truncated,
            "remaining_hits": self.remaining_hits,
        }
        if self.degraded:
            payload["degraded_reason"] = self.degraded_reason
        if self.plan:
            payload["plan"] = dict(self.plan)
        return payload


class IndexQuery:
    """Reads the three layers through their contracts and nothing else.

    Written against the protocols rather than a store, so the in-process and
    the durable backend answer identically by construction -- which is what the
    conformance suite asserts rather than assumes.
    """

    __slots__ = ("_backend", "_cap", "_max_limit", "_ranker")

    def __init__(
        self,
        backend: IndexBackend,
        *,
        content_size_cap: int,
        max_limit: int = _MAX_LIMIT,
        ranker: Ranker | None = None,
    ) -> None:
        self._backend = backend
        self._cap = content_size_cap
        self._max_limit = max_limit
        self._ranker: Ranker = ranker or get_ranker(os.environ.get("LEMONCROW_CONTEXT_RANKER") or None)

    def run(
        self,
        *,
        org_id: str,
        view_id: str,
        kind: str,
        query: str = "",
        path: str = "",
        symbol: str = "",
        limit: int = _DEFAULT_LIMIT,
    ) -> QueryAnswer:
        backend = self._backend.for_view(org_id, view_id)
        if backend is not self._backend:
            return IndexQuery(backend, content_size_cap=self._cap, max_limit=self._max_limit, ranker=self._ranker).run(
                org_id=org_id, view_id=view_id, kind=kind, query=query, path=path, symbol=symbol, limit=limit
            )
        if kind not in QUERY_KINDS:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                f"kind must be one of: {', '.join(sorted(QUERY_KINDS))}",
                details={"field": "kind", "kind": kind},
                action=AgentAction.FIX_REQUEST,
            )
        bounded = max(1, min(limit, self._max_limit))
        retrieval_started = time.perf_counter()
        candidate_count = 0
        if kind == "lexical":
            answer, candidate_count = self._candidate_lexical(org_id, view_id, query, bounded)
        elif kind == "symbol":
            answer, candidate_count = self._candidate_symbol(org_id, view_id, symbol or query, bounded)
        elif kind == "semantic":
            answer, candidate_count = self._semantic(org_id, view_id, query, bounded)
        else:
            state, membership, reachable = self._view_scope(org_id, view_id, operation=kind)
            candidate_count = len(membership)
            answer = self._relations(org_id, view_id, membership, reachable, path, symbol, bounded, state.view_revision)
        ops = self._backend.ops
        if ops is not None:
            ops.retrieval(
                channel=kind,
                candidates=candidate_count,
                hits=len(answer.hits),
                duration_s=time.perf_counter() - retrieval_started,
            )
        return answer

    def search(
        self,
        *,
        org_id: str,
        view_id: str,
        query: str,
        paths: tuple[str, ...] = (),
        limit: int = 8,
        timeout_s: float = 1.0,
        pipe_include_grams: bool = True,
    ) -> QueryAnswer:
        """The MCP search surface over the same possession-filtered rows as queries.

        Scope is a ranking hint, as on the public tool: reserve the first two
        results for matching paths, then keep the remaining view-wide ranking.
        Apply the result limit only after ranking the entire eligible view.
        """
        backend = self._backend.for_view(org_id, view_id)
        if backend is not self._backend:
            return IndexQuery(
                backend, content_size_cap=self._cap, max_limit=self._max_limit, ranker=self._ranker
            ).search(
                org_id=org_id,
                view_id=view_id,
                query=query,
                paths=paths,
                limit=limit,
                timeout_s=timeout_s,
                pipe_include_grams=pipe_include_grams,
            )
        state = self._backend.views.get(org_id, view_id)
        revision = state.view_revision
        retrieval_started = time.perf_counter()
        candidate_count = 0
        channel_truncated = False
        degraded = False
        degraded_reason = ""
        hits: dict[str, QueryHit] = {}
        plan = plan_code_query(query)
        alternatives = _split_pipe_query(query)
        if plan.intent is QueryIntent.PIPE and alternatives:
            pure_identifier_alternation = all(
                _PRECISE_SYMBOL_RE.fullmatch(alternative.strip()) is not None for alternative in alternatives
            )
            # Pipe alternatives stay bounded and candidate-first. Any channel
            # whose Layer-1 candidate discovery truncates transparently falls
            # back to the exact full-View implementation inside the helper.
            candidate_limit = max(100, min(_CANDIDATE_SCAN_LIMIT, limit * 20))
            for alternative in alternatives:
                symbol = _alternative_symbol(alternative)
                definition_match = _DEFINITION_ALT_RE.search(alternative)
                ignored_terms = (alternative.split(None, 1)[0].lower(),) if definition_match is not None else ()
                lexical_answer, examined = self._candidate_lexical(
                    org_id,
                    view_id,
                    alternative,
                    candidate_limit,
                    match_any_term=True,
                    include_grams=pipe_include_grams,
                    include_substring_only=definition_match is None,
                    ignored_terms=ignored_terms,
                    hydrate=False,
                    expected_revision=revision,
                )
                candidate_count += examined
                channel_truncated = channel_truncated or lexical_answer.truncated
                for rank, hit in enumerate(lexical_answer.hits, 1):
                    score = hit.score + (1.0 / rank)
                    prior = hits.get(hit.path)
                    combined_score = (
                        score
                        if prior is None
                        else max(prior.score, score) if pure_identifier_alternation else prior.score + score
                    )
                    hits[hit.path] = QueryHit(
                        path=hit.path,
                        score=combined_score,
                        language=hit.language,
                        line_count=hit.line_count,
                        size=hit.size,
                        detail=(prior.detail if prior is not None and prior.detail.get("definitions") else hit.detail),
                    )

                if symbol:
                    symbol_answer, examined = self._candidate_symbol(
                        org_id,
                        view_id,
                        symbol,
                        candidate_limit,
                        hydrate=False,
                        expected_revision=revision,
                    )
                    candidate_count += examined
                    channel_truncated = channel_truncated or symbol_answer.truncated
                    for rank, hit in enumerate(symbol_answer.hits, 1):
                        score = 100.0 + hit.score + (1.0 / rank)
                        prior = hits.get(hit.path)
                        combined_score = (
                            score
                            if prior is None
                            else max(prior.score, score) if pure_identifier_alternation else prior.score + score
                        )
                        hits[hit.path] = QueryHit(
                            path=hit.path,
                            score=combined_score,
                            language=hit.language,
                            line_count=hit.line_count,
                            size=hit.size,
                            detail=hit.detail,
                        )
        elif plan.intent is QueryIntent.REGEX:
            state, membership, reachable = self._view_scope(org_id, view_id, operation="code_search")
            if state.view_revision != revision:
                raise ServerError(
                    ErrorCode.VIEW_REVISION_STALE,
                    "the view advanced while code search was preparing candidates",
                    details={"claimed_view_revision": revision, "committed_view_revision": state.view_revision},
                    action=AgentAction.REFRESH_VIEW_REVISION,
                )
            candidate_count = len(membership)
            members = self._indexable(membership, reachable)
            by_digest = self._paths_by_digest(members)
            # Regex confirmation still runs against source bytes, but use a
            # required literal/trigram anchor when the pattern is simple enough
            # to prove one. This keeps regex semantics exact while avoiding an
            # O(view) body scan for common shapes such as ``^class Foo`` or
            # ``prop\\.legend:.*format_ticks``.
            try:
                pattern = regex.compile(query)
            except regex.error as exc:
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "query is not a valid regular expression",
                    details={"field": "query"},
                    action=AgentAction.FIX_REQUEST,
                ) from exc
            required_literal = _regex_required_literal(query)
            candidate_paths: set[str] | None = None
            if required_literal:
                prefiltered = self._lexical(
                    org_id,
                    membership,
                    reachable,
                    required_literal,
                    len(membership),
                    revision,
                    match_any_term=False,
                    hydrate=False,
                    prepared_members=members,
                    prepared_by_digest=by_digest,
                ).hits
                candidate_paths = {hit.path for hit in prefiltered}
            deadline = time.monotonic() + timeout_s
            for path, entry in membership.items():
                if candidate_paths is not None and path not in candidate_paths:
                    continue
                indexed = entry.size <= self._cap and entry.content_digest in reachable
                data = self._backend.content.get(org_id, entry.content_digest) if indexed else None
                body = data.decode("utf-8", errors="replace") if data is not None else ""
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    match = pattern.search(body, timeout=remaining) if data is not None else None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    path_match = pattern.search(path, timeout=remaining)
                except TimeoutError as exc:
                    raise ServerError(
                        ErrorCode.DEADLINE_EXCEEDED,
                        "regular expression search exceeded its deadline",
                        details={"tool": "code_search"},
                        action=AgentAction.FIX_REQUEST,
                    ) from exc
                if match is not None or path_match is not None:
                    hits[path] = self._describe(
                        org_id,
                        path,
                        entry,
                        reachable,
                        1.0,
                        {
                            "content_indexed": indexed,
                            "line": body.count("\n", 0, match.start()) + 1 if match else 1,
                            **({"regex_prefilter": required_literal} if required_literal else {}),
                        },
                    )
        elif plan.intent is QueryIntent.PATH:
            path_answer, examined = self._path_search(
                org_id,
                view_id,
                plan.path,
                max(limit * 4, 20),
                expected_revision=revision,
            )
            candidate_count += examined
            channel_truncated = path_answer.truncated
            hits.update((hit.path, hit) for hit in path_answer.hits)
        elif plan.intent is QueryIntent.IMPACT:
            impact_answer, examined = self._impact_search(
                org_id,
                view_id,
                plan,
                max(limit * 4, 20),
                expected_revision=revision,
            )
            candidate_count += examined
            channel_truncated = impact_answer.truncated
            degraded = impact_answer.degraded
            degraded_reason = impact_answer.degraded_reason
            hits.update((hit.path, hit) for hit in impact_answer.hits)
        else:
            precise_symbol = plan.intent is QueryIntent.EXACT_SYMBOL
            definition_match = _DEFINITION_ALT_RE.search(query)
            ignored_terms = (query.split(None, 1)[0].lower(),) if definition_match is not None else ()
            if plan.intent is QueryIntent.EXACT_SYMBOL and plan.symbol:
                symbol_names = (plan.symbol,)
            else:
                symbol_names = _query_symbols(query) if "symbol" in plan.channels else ()
            symbol_hits: dict[str, QueryHit] = {}
            candidate_limit = max(100, min(_CANDIDATE_SCAN_LIMIT, limit * 20))
            for symbol_name in symbol_names:
                symbol_answer, examined = self._candidate_symbol(
                    org_id,
                    view_id,
                    symbol_name,
                    candidate_limit,
                    hydrate=False,
                    expected_revision=revision,
                )
                candidate_count += examined
                channel_truncated = channel_truncated or symbol_answer.truncated
                for rank, hit in enumerate(symbol_answer.hits, 1):
                    score = 100.0 + hit.score + (1.0 / rank)
                    prior = symbol_hits.get(hit.path)
                    detail = dict(prior.detail) if prior is not None else {}
                    prior_defs = detail.get("definitions")
                    merged_defs = list(prior_defs) if isinstance(prior_defs, list) else []
                    seen_defs = {
                        (
                            item.get("name"),
                            item.get("kind"),
                            item.get("line"),
                            item.get("start"),
                            item.get("end"),
                        )
                        for item in merged_defs
                        if isinstance(item, Mapping)
                    }
                    new_defs = hit.detail.get("definitions")
                    if isinstance(new_defs, list):
                        for item in new_defs:
                            if not isinstance(item, Mapping):
                                continue
                            key = (
                                item.get("name"),
                                item.get("kind"),
                                item.get("line"),
                                item.get("start"),
                                item.get("end"),
                            )
                            if key in seen_defs:
                                continue
                            seen_defs.add(key)
                            merged_defs.append(dict(item))
                    if merged_defs:
                        detail["definitions"] = merged_defs
                    symbol_hits[hit.path] = QueryHit(
                        path=hit.path,
                        score=score if prior is None else prior.score + score,
                        language=hit.language or (prior.language if prior is not None else ""),
                        line_count=hit.line_count or (prior.line_count if prior is not None else 0),
                        size=hit.size or (prior.size if prior is not None else 0),
                        detail=detail or hit.detail,
                    )
            symbols = list(symbol_hits.values())
            lexical: tuple[QueryHit, ...] = ()
            if "lexical" in plan.channels:
                lexical_answer, examined = self._candidate_lexical(
                    org_id,
                    view_id,
                    query,
                    _CANDIDATE_SCAN_LIMIT,
                    match_any_term=True,
                    include_substring_only=definition_match is None,
                    ignored_terms=ignored_terms,
                    hydrate=False,
                    expected_revision=revision,
                )
                candidate_count += examined
                channel_truncated = channel_truncated or lexical_answer.truncated
                lexical = lexical_answer.hits
            if precise_symbol:
                selected = list(symbols)
            else:
                selected = [*lexical, *symbols]
                if lexical and definition_match is None and "relations" in plan.channels:
                    link_status = self._backend.links.status(org_id, view_id)
                    if link_status.ready and link_status.at_view_revision == revision:
                        seed_scores = {hit.path: hit.score for hit in lexical}
                        choices: dict[str, tuple[float, str, Edge]] = {}
                        for edge in self._backend.links.edges_for_paths(org_id, view_id, tuple(seed_scores)):
                            if edge.src_path in seed_scores:
                                seed_path, other = edge.src_path, edge.dst_path
                            elif edge.dst_path in seed_scores:
                                seed_path, other = edge.dst_path, edge.src_path
                            else:
                                continue
                            if other in seed_scores:
                                continue
                            score = max(0.001, float(seed_scores[seed_path]) - 0.25)
                            prior = choices.get(other)
                            if prior is None or score > prior[0]:
                                choices[other] = (score, seed_path, edge)
                        related_entries: dict[str, ManifestEntry] = {}
                        for other in choices:
                            entry = self._backend.views.entry(org_id, view_id, other)
                            if entry is not None and entry.size <= self._cap:
                                related_entries[other] = entry
                        related_reachable = addressable_in(
                            self._backend.provenance,
                            state,
                            tuple(sorted({entry.content_digest for entry in related_entries.values()})),
                        )
                        candidate_count += len(related_entries)
                        for other, entry in related_entries.items():
                            if entry.content_digest not in related_reachable:
                                continue
                            score, seed_path, edge = choices[other]
                            selected.append(
                                QueryHit(
                                    path=other,
                                    score=score,
                                    size=entry.size,
                                    detail={
                                        "content_indexed": True,
                                        "related_via": edge.kind.value,
                                        "related_to": seed_path,
                                        "related_symbol": edge.symbol,
                                    },
                                )
                            )
                if "semantic" in plan.channels:
                    semantic_answer, examined = self._semantic(
                        org_id,
                        view_id,
                        query,
                        max(limit * 4, 20),
                    )
                    self._assert_view_still_at(org_id, view_id, revision)
                    candidate_count += examined
                    degraded = semantic_answer.degraded
                    degraded_reason = semantic_answer.degraded_reason
                    by_path = {hit.path: hit for hit in selected}
                    for semantic_hit in semantic_answer.hits:
                        prior = by_path.get(semantic_hit.path)
                        if prior is None:
                            by_path[semantic_hit.path] = semantic_hit
                            continue
                        detail = dict(prior.detail)
                        detail["semantic"] = dict(semantic_hit.detail)
                        by_path[semantic_hit.path] = QueryHit(
                            path=prior.path,
                            score=prior.score + semantic_hit.score,
                            language=prior.language or semantic_hit.language,
                            line_count=prior.line_count or semantic_hit.line_count,
                            size=prior.size or semantic_hit.size,
                            detail=detail,
                        )
                    selected = list(by_path.values())
            hits.update((hit.path, hit) for hit in selected)

        rankable = list(hits.values())
        if not _AUXILIARY_QUERY_RE.search(query):
            rankable = [
                (
                    QueryHit(
                        path=hit.path,
                        score=hit.score * _AUXILIARY_SCORE_FACTOR,
                        language=hit.language,
                        line_count=hit.line_count,
                        size=hit.size,
                        detail=hit.detail,
                    )
                    if _AUXILIARY_PATH_RE.search(hit.path)
                    else hit
                )
                for hit in rankable
            ]

        # Ranking is a swappable policy (R3): ``heuristic`` is the historical
        # order (definitions, exact path/basename, score, path; scopes reserve
        # two leading slots). Candidates + features are recorded on the active
        # runtime trace so alternative rankers can be replayed offline.
        ranker = self._ranker
        ordered = ranker.rank(
            ContextQuery(text=query, limit=limit, scopes=tuple(paths)),
            [hit_candidate(hit, query=query) for hit in rankable],
        )
        ranked = [candidate.payload for candidate in ordered]
        record_ranked_candidates(query=query, ordered=ordered, limit=limit, ranker=ranker.name, scopes=paths)
        final_hits = self._hydrate_ranked_hits(org_id, state, ranked[:limit])
        answer = QueryAnswer(
            kind="code_search",
            hits=final_hits,
            at_view_revision=revision,
            searched_paths=state.entry_count,
            degraded=degraded,
            degraded_reason=degraded_reason,
            truncated=channel_truncated or len(ranked) > limit,
            remaining_hits=max(0, len(ranked) - limit),
            plan=plan.to_wire(),
        )
        ops = self._backend.ops
        if ops is not None:
            ops.query_plan(intent=plan.intent.value, channels=plan.channels)
            ops.retrieval(
                channel="code_search",
                candidates=candidate_count,
                hits=len(answer.hits),
                duration_s=time.perf_counter() - retrieval_started,
            )
        return answer

    # -- helpers --------------------------------------------------------- #

    def _path_search(
        self,
        org_id: str,
        view_id: str,
        path_query: str,
        limit: int,
        *,
        expected_revision: int,
    ) -> tuple[QueryAnswer, int]:
        """Bounded path-only retrieval for an explicitly path-shaped query."""
        state = self._backend.views.get(org_id, view_id)
        self._assert_revision(state.view_revision, expected_revision)
        exact = self._backend.views.entry(org_id, view_id, path_query)
        rows: Mapping[str, ManifestEntry]
        truncated = False
        examined = 0
        if exact is not None:
            rows = {path_query: exact}
            examined = 1
        elif path_query.isascii():
            rows, truncated = self._backend.views.path_candidates(
                org_id,
                view_id,
                path_query.casefold(),
                limit=_PATH_CANDIDATE_LIMIT,
            )
            examined = len(rows)
        else:
            # Preserve Unicode path case-fold semantics. This uncommon branch
            # is the exact fallback; ordinary path-shaped queries stay bounded.
            membership = self._backend.views.membership(org_id, view_id)
            needle = path_query.casefold()
            rows = {path: entry for path, entry in membership.items() if needle in path.casefold()}
            examined = len(membership)
        self._assert_view_still_at(org_id, view_id, state.view_revision)
        indexable = {path: entry for path, entry in rows.items() if entry.size <= self._cap}
        reachable = addressable_in(
            self._backend.provenance,
            state,
            tuple(sorted({entry.content_digest for entry in indexable.values()})),
        )
        hits = [
            self._describe(
                org_id,
                path,
                entry,
                reachable,
                100.0 if path == path_query else 10.0,
                {
                    "content_indexed": entry.size <= self._cap and entry.content_digest in reachable,
                    "matched": "exact_path" if path == path_query else "path",
                },
            )
            for path, entry in rows.items()
        ]
        hits.sort(key=lambda hit: (-hit.score, len(hit.path), hit.path))
        return (
            QueryAnswer(
                kind="path",
                hits=tuple(hits[:limit]),
                at_view_revision=state.view_revision,
                searched_paths=state.entry_count,
                truncated=truncated or len(hits) > limit,
            ),
            examined,
        )

    def _impact_search(
        self,
        org_id: str,
        view_id: str,
        plan: QueryPlan,
        limit: int,
        *,
        expected_revision: int,
    ) -> tuple[QueryAnswer, int]:
        """Exact-symbol seed plus relation expansion; never generic broad recall."""
        state = self._backend.views.get(org_id, view_id)
        self._assert_revision(state.view_revision, expected_revision)
        symbol_answer, examined = self._candidate_symbol(
            org_id,
            view_id,
            plan.symbol,
            max(limit, 20),
            hydrate=False,
            expected_revision=expected_revision,
        )
        seeds = list(symbol_answer.hits)
        # Impact is a deliberately narrow plan. Do not reconstruct/build a
        # cold generation graph on the foreground request merely to discover
        # that a large repository exceeds the inline budget. View-open already
        # schedules intelligence, and this enqueue is idempotent for callers
        # that reach search first.
        status = self._backend.links.status(org_id, view_id)
        if not status.ready or status.at_view_revision != expected_revision:
            enqueue_deferred_layer(self._backend, org_id, view_id, "link")
            return (
                QueryAnswer(
                    kind="impact",
                    hits=tuple(seeds[:limit]),
                    at_view_revision=expected_revision,
                    searched_paths=state.entry_count,
                    degraded=True,
                    degraded_reason=f"link:{status.reason or status.state.value}",
                    truncated=symbol_answer.truncated or len(seeds) > limit,
                ),
                examined,
            )

        seed_paths = {hit.path for hit in seeds}
        choices: dict[str, tuple[float, str, Edge]] = {}
        for edge in self._backend.links.edges_for_paths(org_id, view_id, tuple(sorted(seed_paths))):
            if edge.src_path in seed_paths:
                seed_path, other = edge.src_path, edge.dst_path
            elif edge.dst_path in seed_paths:
                seed_path, other = edge.dst_path, edge.src_path
            else:
                continue
            if other in seed_paths:
                continue
            if edge.symbol and plan.symbol and edge.symbol != plan.symbol and edge.kind is not EdgeKind.IMPORT:
                continue
            score = 50.0 if edge.kind is EdgeKind.CALL else 25.0 if edge.kind is EdgeKind.REFERENCE else 10.0
            prior = choices.get(other)
            if prior is None or score > prior[0]:
                choices[other] = (score, seed_path, edge)

        related_entries: dict[str, ManifestEntry] = {}
        for path in choices:
            entry = self._backend.views.entry(org_id, view_id, path)
            if entry is not None and entry.size <= self._cap:
                related_entries[path] = entry
        reachable = addressable_in(
            self._backend.provenance,
            state,
            tuple(sorted({entry.content_digest for entry in related_entries.values()})),
        )
        examined += len(related_entries)
        related: list[QueryHit] = []
        for path, entry in related_entries.items():
            if entry.content_digest not in reachable:
                continue
            score, seed_path, edge = choices[path]
            related.append(
                QueryHit(
                    path=path,
                    score=score,
                    size=entry.size,
                    detail={
                        "content_indexed": True,
                        "related_via": edge.kind.value,
                        "related_to": seed_path,
                        "related_symbol": edge.symbol,
                    },
                )
            )
        selected = [*seeds, *related]
        selected.sort(key=lambda hit: (-hit.score, hit.path))
        return (
            QueryAnswer(
                kind="impact",
                hits=tuple(selected[:limit]),
                at_view_revision=expected_revision,
                searched_paths=state.entry_count,
                truncated=symbol_answer.truncated or len(selected) > limit,
            ),
            examined,
        )

    def _candidate_rows(
        self,
        org_id: str,
        view_id: str,
        digests: Sequence[str],
        *,
        operation: str,
        expected_revision: int | None = None,
    ) -> tuple[Any, Mapping[str, ManifestEntry], frozenset[str], int]:
        """Resolve and authorize only bounded Layer-1 candidate digests.

        Candidate generation happens inside the already tenant/repository-scoped
        analysis authority. View membership and possession are then intersected
        before any content-derived detail is returned. A guessed digest that is
        held elsewhere therefore produces exactly the same result as one that
        was never uploaded: no authorized candidate row.
        """
        started = time.perf_counter()
        state = self._backend.views.get(org_id, view_id)
        self._assert_revision(state.view_revision, expected_revision)
        rows = self._backend.views.entries_for_digests(org_id, view_id, tuple(dict.fromkeys(digests)))
        self._assert_view_still_at(org_id, view_id, state.view_revision)
        indexable = {path: entry for path, entry in rows.items() if entry.size <= self._cap}
        reachable = addressable_in(
            self._backend.provenance,
            state,
            tuple(sorted({entry.content_digest for entry in indexable.values()})),
        )
        members = {path: entry for path, entry in indexable.items() if entry.content_digest in reachable}
        ops = self._backend.ops
        if ops is not None:
            ops.membership(
                operation=operation,
                entries=len(rows),
                duration_s=time.perf_counter() - started,
            )
        return state, members, reachable, len(rows)

    def _candidate_lexical(
        self,
        org_id: str,
        view_id: str,
        query: str,
        limit: int,
        *,
        match_any_term: bool = False,
        include_grams: bool = True,
        include_substring_only: bool = True,
        ignored_terms: Sequence[str] = (),
        hydrate: bool = True,
        expected_revision: int | None = None,
    ) -> tuple[QueryAnswer, int]:
        digests, truncated = self._backend.analysis.lexical_candidates(
            org_id,
            query,
            limit=_CANDIDATE_SCAN_LIMIT,
            include_grams=include_grams,
            include_substring_only=include_substring_only,
            ignored_terms=ignored_terms,
        )
        if truncated:
            state, membership, reachable = self._view_scope(org_id, view_id, operation="lexical")
            self._assert_revision(state.view_revision, expected_revision)
            return (
                self._lexical(
                    org_id,
                    membership,
                    reachable,
                    query,
                    limit,
                    state.view_revision,
                    match_any_term=match_any_term,
                    include_grams=include_grams,
                    include_substring_only=include_substring_only,
                    ignored_terms=ignored_terms,
                    hydrate=hydrate,
                    document_count=state.entry_count,
                ),
                len(membership),
            )

        state, members, reachable, candidate_rows = self._candidate_rows(
            org_id,
            view_id,
            digests,
            operation="lexical",
            expected_revision=expected_revision,
        )
        by_digest = self._paths_by_digest(members)
        scores = self._backend.analysis.lexical_matches(
            org_id,
            tuple(by_digest),
            query,
            match_any_term=match_any_term,
            include_grams=include_grams,
            include_substring_only=include_substring_only,
            ignored_terms=ignored_terms,
            document_count=state.entry_count,
        )
        hits: list[QueryHit] = []
        for digest, score in scores.items():
            for path in by_digest.get(digest, ()):
                detail = {"content_indexed": True}
                hits.append(
                    self._describe(org_id, path, members[path], reachable, float(score), detail)
                    if hydrate
                    else QueryHit(path=path, score=float(score), size=members[path].size, detail=detail)
                )

        matched_paths = {hit.path for hit in hits}
        path_truncated = False
        needle = query.strip().lower()
        if needle:
            # SQLite's built-in lower()/NOCASE are ASCII-only. Preserve the
            # public Python case-folding semantics rather than making a rare
            # non-ASCII path query backend-dependent. Candidate overflow also
            # falls back: a bounded prefix must never silently omit a path that
            # the final ranker would have preferred.
            if not needle.isascii():
                full_state, membership, full_reachable = self._view_scope(org_id, view_id, operation="lexical")
                self._assert_revision(full_state.view_revision, expected_revision)
                return (
                    self._lexical(
                        org_id,
                        membership,
                        full_reachable,
                        query,
                        limit,
                        full_state.view_revision,
                        match_any_term=match_any_term,
                        include_grams=include_grams,
                        include_substring_only=include_substring_only,
                        ignored_terms=ignored_terms,
                        hydrate=hydrate,
                        document_count=full_state.entry_count,
                    ),
                    len(membership),
                )
            path_rows, path_truncated = self._backend.views.path_candidates(
                org_id, view_id, needle, limit=_PATH_CANDIDATE_LIMIT
            )
            self._assert_view_still_at(org_id, view_id, state.view_revision)
            if path_truncated:
                full_state, membership, full_reachable = self._view_scope(org_id, view_id, operation="lexical")
                self._assert_revision(full_state.view_revision, expected_revision)
                return (
                    self._lexical(
                        org_id,
                        membership,
                        full_reachable,
                        query,
                        limit,
                        full_state.view_revision,
                        match_any_term=match_any_term,
                        include_grams=include_grams,
                        include_substring_only=include_substring_only,
                        ignored_terms=ignored_terms,
                        hydrate=hydrate,
                        document_count=full_state.entry_count,
                    ),
                    len(membership),
                )
            candidate_rows += len(path_rows)
            path_indexable = {
                path: entry
                for path, entry in path_rows.items()
                if entry.size <= self._cap and path not in matched_paths
            }
            path_reachable = addressable_in(
                self._backend.provenance,
                state,
                tuple(sorted({entry.content_digest for entry in path_indexable.values()})),
            )
            all_reachable = reachable | path_reachable
            for path, entry in path_rows.items():
                if path in matched_paths:
                    continue
                indexed = entry.size <= self._cap and entry.content_digest in path_reachable
                detail = {"content_indexed": indexed, "matched": "path"}
                hits.append(
                    self._describe(org_id, path, entry, all_reachable, 1.0, detail)
                    if hydrate
                    else QueryHit(path=path, score=1.0, size=entry.size, detail=detail)
                )

        hits.sort(key=lambda hit: (-hit.score, hit.path))
        return (
            QueryAnswer(
                kind="lexical",
                hits=tuple(hits[:limit]),
                at_view_revision=state.view_revision,
                searched_paths=state.entry_count,
                truncated=path_truncated or len(hits) > limit,
            ),
            candidate_rows,
        )

    def _candidate_symbol(
        self,
        org_id: str,
        view_id: str,
        name: str,
        limit: int,
        *,
        hydrate: bool = True,
        expected_revision: int | None = None,
    ) -> tuple[QueryAnswer, int]:
        stripped = name.strip()
        digests, truncated = self._backend.analysis.definition_candidates(org_id, stripped, limit=_CANDIDATE_SCAN_LIMIT)
        if truncated:
            state, membership, reachable = self._view_scope(org_id, view_id, operation="symbol")
            self._assert_revision(state.view_revision, expected_revision)
            return (
                self._symbol(
                    org_id,
                    membership,
                    reachable,
                    stripped,
                    limit,
                    state.view_revision,
                    hydrate=hydrate,
                ),
                len(membership),
            )
        state, members, reachable, candidate_rows = self._candidate_rows(
            org_id,
            view_id,
            digests,
            operation="symbol",
            expected_revision=expected_revision,
        )
        by_digest = self._paths_by_digest(members)
        found = self._backend.analysis.definitions_named(org_id, tuple(by_digest), stripped)
        hits: list[QueryHit] = []
        for digest, definitions in found.items():
            for path in by_digest.get(digest, ()):
                detail = {
                    "definitions": [
                        {
                            "name": item.name,
                            "kind": item.kind,
                            "line": item.line,
                            "start": item.start,
                            "end": item.end,
                            "exported": item.exported,
                        }
                        for item in definitions
                    ]
                }
                hits.append(
                    self._describe(
                        org_id,
                        path,
                        members[path],
                        reachable,
                        float(len(definitions)),
                        detail,
                    )
                    if hydrate
                    else QueryHit(
                        path=path,
                        score=float(len(definitions)),
                        size=members[path].size,
                        detail=detail,
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.path))
        return (
            QueryAnswer(
                kind="symbol",
                hits=tuple(hits[:limit]),
                at_view_revision=state.view_revision,
                searched_paths=state.entry_count,
                truncated=len(hits) > limit,
            ),
            candidate_rows,
        )

    @staticmethod
    def _assert_revision(committed: int, expected: int | None) -> None:
        if expected is None or committed == expected:
            return
        raise ServerError(
            ErrorCode.VIEW_REVISION_STALE,
            "the view advanced while code search was preparing candidates",
            details={"claimed_view_revision": expected, "committed_view_revision": committed},
            action=AgentAction.REFRESH_VIEW_REVISION,
        )

    def _assert_view_still_at(self, org_id: str, view_id: str, expected: int) -> None:
        self._assert_revision(self._backend.views.get(org_id, view_id).view_revision, expected)

    def _hydrate_ranked_hits(self, org_id: str, state: Any, hits: Sequence[QueryHit]) -> tuple[QueryHit, ...]:
        """Hydrate only final ranked paths after candidate-scoped possession proof."""
        self._assert_view_still_at(org_id, state.view_id, state.view_revision)
        entries: dict[str, ManifestEntry] = {}
        for hit in hits:
            entry = self._backend.views.entry(org_id, state.view_id, hit.path)
            if entry is not None:
                entries[hit.path] = entry
        self._assert_view_still_at(org_id, state.view_id, state.view_revision)
        reachable = addressable_in(
            self._backend.provenance,
            state,
            tuple(sorted({entry.content_digest for entry in entries.values() if entry.size <= self._cap})),
        )
        hydrated: list[QueryHit] = []
        for hit in hits:
            entry = entries.get(hit.path)
            if entry is None:
                continue
            content_derived = "definitions" in hit.detail or hit.detail.get("content_indexed") is True
            if content_derived and entry.content_digest not in reachable:
                continue
            hydrated.append(self._describe(org_id, hit.path, entry, reachable, hit.score, hit.detail))
        return tuple(hydrated)

    def _view_scope(
        self, org_id: str, view_id: str, *, operation: str
    ) -> tuple[Any, Mapping[str, ManifestEntry], frozenset[str]]:
        started = time.perf_counter()
        state = self._backend.views.get(org_id, view_id)
        membership = self._backend.views.membership(org_id, view_id)
        reachable = addressable_in(
            self._backend.provenance,
            state,
            tuple(sorted({entry.content_digest for entry in membership.values()})),
        )
        ops = self._backend.ops
        if ops is not None:
            ops.membership(
                operation=operation,
                entries=len(membership),
                duration_s=time.perf_counter() - started,
            )
        return state, membership, reachable

    def _indexable(
        self, membership: Mapping[str, ManifestEntry], reachable: frozenset[str]
    ) -> dict[str, ManifestEntry]:
        """Members whose bytes this view both has room for and may address."""
        return {
            path: entry
            for path, entry in membership.items()
            if entry.size <= self._cap and entry.content_digest in reachable
        }

    def _paths_by_digest(self, members: Mapping[str, ManifestEntry]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for path, entry in members.items():
            out.setdefault(entry.content_digest, []).append(path)
        for paths in out.values():
            paths.sort()
        return out

    def _describe(
        self,
        org_id: str,
        path: str,
        entry: ManifestEntry,
        reachable: frozenset[str],
        score: float,
        detail: Mapping[str, Any],
    ) -> QueryHit:
        """One hit. The derived row is read only for a digest this view holds.

        ``language`` and ``line_count`` are computed from the bytes, so a view
        that merely named a digest must not get them: they would report on
        content it never had, and differ from the answer for a digest nobody has
        ever uploaded.
        """
        metadata = (
            self._backend.analysis.metadata(org_id, entry.content_digest, entry.parser_profile)
            if entry.content_digest in reachable
            else None
        )
        return QueryHit(
            path=path,
            score=score,
            language=metadata[0] if metadata is not None else "",
            line_count=metadata[1] if metadata is not None else 0,
            size=entry.size,
            detail=detail,
        )

    # -- kinds ----------------------------------------------------------- #

    def _lexical(
        self,
        org_id: str,
        membership: Mapping[str, ManifestEntry],
        reachable: frozenset[str],
        query: str,
        limit: int,
        revision: int,
        *,
        match_any_term: bool = False,
        include_grams: bool = True,
        include_substring_only: bool = True,
        ignored_terms: Sequence[str] = (),
        hydrate: bool = True,
        document_count: int | None = None,
        prepared_members: Mapping[str, ManifestEntry] | None = None,
        prepared_by_digest: Mapping[str, Sequence[str]] | None = None,
    ) -> QueryAnswer:
        members = prepared_members if prepared_members is not None else self._indexable(membership, reachable)
        by_digest = prepared_by_digest if prepared_by_digest is not None else self._paths_by_digest(members)
        scores = self._backend.analysis.lexical_matches(
            org_id,
            tuple(by_digest),
            query,
            match_any_term=match_any_term,
            include_grams=include_grams,
            include_substring_only=include_substring_only,
            ignored_terms=ignored_terms,
            document_count=document_count,
        )

        hits: list[QueryHit] = []
        for digest, score in scores.items():
            for path in by_digest.get(digest, ()):
                detail = {"content_indexed": True}
                hits.append(
                    self._describe(org_id, path, members[path], reachable, float(score), detail)
                    if hydrate
                    else QueryHit(path=path, score=float(score), size=members[path].size, detail=detail)
                )

        matched_paths = {hit.path for hit in hits}
        needle = query.strip().lower()
        if needle:
            for path, entry in membership.items():
                if path in matched_paths or needle not in path.lower():
                    continue
                # Path-only match: an over-cap file whose bytes were never
                # uploaded, a file this view named but never supplied, or one
                # whose content did not match but whose name did. Flagged in
                # every case, and identically -- the flag is what keeps "the
                # organization holds this under another scope" and "nobody ever
                # uploaded it" one answer.
                detail = {
                    "content_indexed": entry.size <= self._cap and entry.content_digest in reachable,
                    "matched": "path",
                }
                hits.append(
                    self._describe(org_id, path, entry, reachable, 1.0, detail)
                    if hydrate
                    else QueryHit(path=path, score=1.0, size=entry.size, detail=detail)
                )

        hits.sort(key=lambda hit: (-hit.score, hit.path))
        return QueryAnswer(
            kind="lexical",
            hits=tuple(hits[:limit]),
            at_view_revision=revision,
            searched_paths=len(membership),
            truncated=len(hits) > limit,
        )

    def _symbol(
        self,
        org_id: str,
        membership: Mapping[str, ManifestEntry],
        reachable: frozenset[str],
        name: str,
        limit: int,
        revision: int,
        *,
        hydrate: bool = True,
        prepared_members: Mapping[str, ManifestEntry] | None = None,
        prepared_by_digest: Mapping[str, Sequence[str]] | None = None,
    ) -> QueryAnswer:
        members = prepared_members if prepared_members is not None else self._indexable(membership, reachable)
        by_digest = prepared_by_digest if prepared_by_digest is not None else self._paths_by_digest(members)
        found = self._backend.analysis.definitions_named(org_id, tuple(by_digest), name.strip())
        hits: list[QueryHit] = []
        for digest, definitions in found.items():
            for path in by_digest.get(digest, ()):
                detail = {
                    "definitions": [
                        {
                            "name": item.name,
                            "kind": item.kind,
                            "line": item.line,
                            "start": item.start,
                            "end": item.end,
                            "exported": item.exported,
                        }
                        for item in definitions
                    ]
                }
                hits.append(
                    self._describe(
                        org_id,
                        path,
                        members[path],
                        reachable,
                        float(len(definitions)),
                        detail,
                    )
                    if hydrate
                    else QueryHit(
                        path=path,
                        score=float(len(definitions)),
                        size=members[path].size,
                        detail=detail,
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.path))
        return QueryAnswer(
            kind="symbol",
            hits=tuple(hits[:limit]),
            at_view_revision=revision,
            searched_paths=len(membership),
            truncated=len(hits) > limit,
        )

    def _relations(
        self,
        org_id: str,
        view_id: str,
        membership: Mapping[str, ManifestEntry],
        reachable: frozenset[str],
        path: str,
        symbol: str,
        limit: int,
        revision: int,
    ) -> QueryAnswer:
        status = self._refresh(self._backend.links, org_id, view_id)
        edges = self._backend.links.edges(org_id, view_id)
        # An edge names a symbol resolved out of a file's bytes, so both ends
        # have to be content this view may address. The layer already builds
        # only from addressable members; this is the second line, because the
        # graph outlives one build and the set could have narrowed under it.
        openings = {path for path, entry in membership.items() if entry.content_digest in reachable}
        selected: list[Edge] = []
        for edge in edges:
            if edge.src_path not in openings or edge.dst_path not in openings:
                continue
            if path and edge.src_path != path and edge.dst_path != path:
                continue
            if symbol and edge.symbol != symbol:
                continue
            selected.append(edge)

        hits = tuple(
            QueryHit(
                path=edge.src_path,
                score=1.0,
                size=membership[edge.src_path].size if edge.src_path in membership else 0,
                detail=edge.to_wire(),
            )
            for edge in selected[:limit]
        )
        return QueryAnswer(
            kind="relations",
            hits=hits,
            at_view_revision=revision,
            searched_paths=len(membership),
            degraded=not status.ready,
            degraded_reason=f"link:{status.reason or status.state.value}" if not status.ready else "",
            truncated=len(selected) > limit,
        )

    def _semantic(self, org_id: str, view_id: str, query: str, limit: int) -> tuple[QueryAnswer, int]:
        status = self._refresh(self._backend.semantic, org_id, view_id)
        state = self._backend.views.get(org_id, view_id)
        vector = semantic_embedding(query)
        if not status.ready:
            # Readiness is explicit, but semantic search remains useful while a
            # large ANN catches up. This is the only path that retains the old
            # O(View) exact scan; ready queries never reconstruct full membership.
            _state, membership, reachable = self._view_scope(org_id, view_id, operation="semantic")
            members = self._indexable(membership, reachable)
            by_digest = self._paths_by_digest(members)
            started = time.perf_counter()
            legacy_vector = embed_terms(parse_query(query).terms)
            ranked = self._backend.analysis.nearest(org_id, tuple(by_digest), legacy_vector, limit)
            ops = self._backend.ops
            if ops is not None:
                ops.semantic_scan(vectors=len(by_digest), duration_s=time.perf_counter() - started)
            hits: list[QueryHit] = []
            for digest, similarity in ranked:
                for path in by_digest.get(digest, ()):
                    hits.append(
                        self._describe(
                            org_id,
                            path,
                            members[path],
                            reachable,
                            similarity,
                            {"similarity": round(similarity, 6), "semantic_unit": "file-fallback"},
                        )
                    )
            hits.sort(key=lambda hit: (-hit.score, hit.path))
            return (
                QueryAnswer(
                    kind="semantic",
                    hits=tuple(hits[:limit]),
                    at_view_revision=state.view_revision,
                    searched_paths=state.entry_count,
                    degraded=True,
                    degraded_reason=f"semantic:{status.reason or status.state.value}",
                    truncated=len(hits) > limit,
                ),
                len(by_digest),
            )

        started = time.perf_counter()
        result = self._backend.semantic.nearest(org_id, view_id, vector, limit)
        ops = self._backend.ops
        if ops is not None:
            ops.semantic_ann(
                total_units=result.total_units,
                candidates=result.candidates_scored,
                duration_s=time.perf_counter() - started,
            )
        entries: list[tuple[Any, ManifestEntry]] = []
        for match in result.matches:
            entry = self._backend.views.entry(org_id, view_id, match.path)
            if entry is not None and entry.content_digest == match.digest:
                entries.append((match, entry))
        reachable = addressable_in(
            self._backend.provenance,
            state,
            tuple(dict.fromkeys(entry.content_digest for _match, entry in entries)),
        )
        hits = [
            self._describe(
                org_id,
                match.path,
                entry,
                reachable,
                match.score,
                {
                    "similarity": match.score,
                    "semantic_unit": match.unit_id,
                    "start_line": match.start_line,
                    "end_line": match.end_line,
                    "symbol": match.symbol,
                },
            )
            for match, entry in entries
            if entry.content_digest in reachable
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.path))
        return (
            QueryAnswer(
                kind="semantic",
                hits=tuple(hits[:limit]),
                at_view_revision=state.view_revision,
                searched_paths=state.entry_count,
                degraded=False,
                degraded_reason="",
                truncated=len(hits) > limit,
            ),
            result.candidates_scored,
        )

    def _refresh(self, layer: Any, org_id: str, view_id: str) -> IndexLayerStatus:
        """Build within the foreground budget and queue any deferred remainder."""
        status: IndexLayerStatus = layer.refresh(org_id, view_id)
        reason = status.reason
        if reason in {"link_rebuild_deferred", "semantic_rebuild_deferred"}:
            name = "link" if layer is self._backend.links else "semantic"
            enqueue_deferred_layer(self._backend, org_id, view_id, name)
        return status
