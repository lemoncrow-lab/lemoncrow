"""Layer 1, derived: content-local analysis of one blob.

Everything in this module is a pure function of ``(bytes, parser_profile)``.
That is the whole point of the layer: the result is keyed
``(org_id, sha256(content), parser_profile_version)``, so it must not depend on
a path, a module name, a repository, a view or a neighbouring file. A rename
must not invalidate it, and two worktrees holding the same bytes must derive it
once.

What is derived here, and why each row exists:

``lexical_terms`` / ``trigrams``
    The lexical index. Terms answer identifier queries; trigrams answer
    substring queries that no tokenizer would produce. Both are content-local.
``definitions``
    Symbol definitions with their kind, line and byte span. A span is an offset
    into *these bytes*, never a location in a repository.
``imports`` / ``exports``
    The import *strings* a file contains and the names it offers. Resolving an
    import string to another file is path-sensitive, so it happens in Layer 3
    and never here.
``references``
    Identifier uses, with whether the use is a call. Layer 3 turns these into
    reference and call edges once it knows which files are in the view.
``embedding``
    A deterministic hashed bag-of-terms vector. Stdlib only, no model weights,
    no download -- the thin-client design forbids both on the client and this
    server has no business pulling a model at import time either.

Comments and string literals are blanked before definitions and references are
read, so a ``def`` inside a docstring is not a definition and a word inside a
string is not a reference. Import strings are read from a text with comments
blanked but strings intact, because in Go, TypeScript and Rust the module name
*is* a string literal.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from .contracts import AnalysisArtifact, ImportRef, SymbolDef, SymbolRef

__all__ = [
    "DEFAULT_PROFILE",
    "EMBEDDING_DIM",
    "PARSER_PROFILES",
    "LexicalQuery",
    "ParserProfile",
    "analyze",
    "cosine",
    "embed_terms",
    "idf_weight",
    "parse_query",
    "profile_for_path",
    "resolve_profile",
    "score_match",
    "score_weighted_match",
    "trigrams_of",
]

EMBEDDING_DIM: Final[int] = 64
DEFAULT_PROFILE: Final[str] = "lexical-1"

_MAX_LEXICAL_TERMS: Final[int] = 4096
_MAX_TRIGRAMS: Final[int] = 8192
_MAX_SYMBOL_SPANS: Final[int] = 2048
_MAX_REFERENCES: Final[int] = 4096
_MAX_IMPORTS: Final[int] = 1024
_MAX_EXPORTS: Final[int] = 2048

_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,63}")

#: Language keywords are never references. One shared set across profiles: a
#: keyword of another language is not an identifier anyone links against, so
#: over-filtering here costs nothing and keeps the set deterministic.
_KEYWORDS: Final[frozenset[str]] = frozenset("""
    and as assert async await break case catch class const continue declare def default defer del delete
    do elif else enum except export extends false fallthrough final finally fn for from func function
    global go goto if impl implements import in instanceof int interface is lambda let loop match mod
    move mut new nil none nonlocal not null or package pass pub raise range return select self static
    struct super switch this throw trait true try type typeof union unsafe use var void where while
    with yield string bool byte rune error len cap make append copy print println
    None True False Self
    """.split())


@dataclass(frozen=True, slots=True)
class ParserProfile:
    """How one ``parser_profile_version`` reads bytes.

    The profile is part of the derived-analysis key, so changing any field here
    requires a new profile name rather than an edit in place: existing rows for
    the old name stay valid because they describe what the old reader saw.
    """

    name: str
    language: str
    extensions: tuple[str, ...]
    definitions: tuple[tuple[re.Pattern[str], str], ...]
    line_comments: tuple[str, ...]
    block_comments: tuple[tuple[str, str], ...]
    quotes: tuple[str, ...]
    read_imports: Callable[[str], tuple[ImportRef, ...]]
    is_exported: Callable[[str, str], bool]


# --------------------------------------------------------------------------- #
# Scrubbing                                                                   #
# --------------------------------------------------------------------------- #


def _blank(out: list[str], start: int, stop: int) -> None:
    for index in range(start, stop):
        if out[index] != "\n":
            out[index] = " "


def _scrub(text: str, profile: ParserProfile, *, strings: bool) -> str:
    """Blank comments (and optionally string bodies), preserving every offset.

    Length, newlines and therefore every line number and byte span survive, so
    a span read from the scrubbed text addresses the original bytes.
    """
    out = list(text)
    index = 0
    length = len(text)
    while index < length:
        consumed = False
        for opener, closer in profile.block_comments:
            if text.startswith(opener, index):
                end = text.find(closer, index + len(opener))
                end = length if end < 0 else end + len(closer)
                _blank(out, index, end)
                index = end
                consumed = True
                break
        if consumed:
            continue
        for marker in profile.line_comments:
            if text.startswith(marker, index):
                end = text.find("\n", index)
                end = length if end < 0 else end
                _blank(out, index, end)
                index = end
                consumed = True
                break
        if consumed:
            continue
        if strings:
            for quote in profile.quotes:
                if text.startswith(quote, index):
                    end = _string_end(text, index + len(quote), quote)
                    _blank(out, index, end)
                    index = end
                    consumed = True
                    break
            if consumed:
                continue
        index += 1
    return "".join(out)


def _string_end(text: str, start: int, quote: str) -> int:
    length = len(text)
    index = start
    while index < length:
        if text[index] == "\\":
            index += 2
            continue
        if text.startswith(quote, index):
            return index + len(quote)
        index += 1
    return length


# --------------------------------------------------------------------------- #
# Import readers                                                              #
# --------------------------------------------------------------------------- #

_PY_IMPORT: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*import[ \t]+([A-Za-z_][\w.]*)(?:[ \t]+as[ \t]+([A-Za-z_]\w*))?",
    re.MULTILINE,
)
_PY_FROM: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*from[ \t]+(\.*)([A-Za-z_][\w.]*)?[ \t]+import[ \t]+(\*|[^\n#]+)",
    re.MULTILINE,
)
_ECMA_IMPORT: Final[re.Pattern[str]] = re.compile(
    r"""^[ \t]*(?:import|export)[ \t]+(?:([^'";]*?)[ \t]+from[ \t]+)?['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_ECMA_REQUIRE: Final[re.Pattern[str]] = re.compile(r"""require\([ \t]*['"]([^'"]+)['"]""")
_GO_SINGLE: Final[re.Pattern[str]] = re.compile(r'^[ \t]*import[ \t]+(?:\w+[ \t]+)?"([^"]+)"', re.MULTILINE)
_GO_BLOCK: Final[re.Pattern[str]] = re.compile(r"^[ \t]*import[ \t]*\(([^)]*)\)", re.MULTILINE | re.DOTALL)
_GO_BLOCK_ENTRY: Final[re.Pattern[str]] = re.compile(r'(?:(\w+)[ \t]+)?"([^"]+)"')
_RUST_USE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*(?:pub[ \t]+)?use[ \t]+([^;\n]+);", re.MULTILINE)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _split_binding(raw: str) -> tuple[str, str]:
    """``"alpha as a"`` -> ``("alpha", "a")``; ``"alpha"`` -> ``("alpha", "alpha")``."""
    parts = raw.split()
    if len(parts) >= 3 and parts[1] == "as":
        return parts[0], parts[2]
    return (parts[0], parts[0]) if parts else ("", "")


def _python_imports(text: str) -> tuple[ImportRef, ...]:
    refs: list[ImportRef] = []
    for match in _PY_IMPORT.finditer(text):
        module = match.group(1)
        alias = match.group(2) or module.split(".")[0]
        refs.append(ImportRef(module=module, bindings=((module, alias),), line=_line_of(text, match.start()), level=0))
    for match in _PY_FROM.finditer(text):
        level = len(match.group(1) or "")
        module = match.group(2) or ""
        raw_names = (match.group(3) or "").strip()
        if raw_names.startswith("*"):
            refs.append(
                ImportRef(
                    module=module,
                    bindings=(),
                    line=_line_of(text, match.start()),
                    level=level,
                    wildcard=True,
                )
            )
            continue
        bindings: list[tuple[str, str]] = []
        for piece in raw_names.strip("()").split(","):
            original, local = _split_binding(piece.strip())
            if original:
                bindings.append((original, local))
        refs.append(
            ImportRef(
                module=module,
                bindings=tuple(bindings),
                line=_line_of(text, match.start()),
                level=level,
            )
        )
    return tuple(refs[:_MAX_IMPORTS])


def _ecma_clause_bindings(clause: str) -> tuple[tuple[str, str], ...]:
    clause = clause.strip()
    if not clause:
        return ()
    bindings: list[tuple[str, str]] = []
    brace = clause.find("{")
    head = clause[:brace] if brace >= 0 else clause
    body = clause[brace + 1 : clause.find("}", brace)] if brace >= 0 else ""
    for piece in head.split(","):
        name = piece.strip().removeprefix("* as ").strip()
        if name and name != "*" and _IDENTIFIER_RE.fullmatch(name):
            bindings.append(("default", name))
    for piece in body.split(","):
        original, local = _split_binding(piece.strip())
        if original and _IDENTIFIER_RE.fullmatch(original):
            bindings.append((original, local))
    return tuple(bindings)


def _ecma_imports(text: str) -> tuple[ImportRef, ...]:
    refs: list[ImportRef] = []
    for match in _ECMA_IMPORT.finditer(text):
        refs.append(
            ImportRef(
                module=match.group(2),
                bindings=_ecma_clause_bindings(match.group(1) or ""),
                line=_line_of(text, match.start()),
                level=0,
            )
        )
    for match in _ECMA_REQUIRE.finditer(text):
        refs.append(ImportRef(module=match.group(1), bindings=(), line=_line_of(text, match.start()), level=0))
    return tuple(refs[:_MAX_IMPORTS])


def _go_imports(text: str) -> tuple[ImportRef, ...]:
    refs: list[ImportRef] = []
    for match in _GO_SINGLE.finditer(text):
        module = match.group(1)
        refs.append(
            ImportRef(
                module=module,
                bindings=((module.rsplit("/", 1)[-1], module.rsplit("/", 1)[-1]),),
                line=_line_of(text, match.start()),
                level=0,
            )
        )
    for block in _GO_BLOCK.finditer(text):
        body = block.group(1)
        base = block.start(1)
        for entry in _GO_BLOCK_ENTRY.finditer(body):
            module = entry.group(2)
            local = entry.group(1) or module.rsplit("/", 1)[-1]
            refs.append(
                ImportRef(
                    module=module,
                    bindings=((module.rsplit("/", 1)[-1], local),),
                    line=_line_of(text, base + entry.start()),
                    level=0,
                )
            )
    return tuple(refs[:_MAX_IMPORTS])


def _rust_uses(text: str) -> tuple[ImportRef, ...]:
    refs: list[ImportRef] = []
    for match in _RUST_USE.finditer(text):
        body = " ".join(match.group(1).split())
        line = _line_of(text, match.start())
        brace = body.find("{")
        if brace < 0:
            module, _, leaf = body.rpartition("::")
            if not module:
                module, leaf = body, body
            original, local = _split_binding(leaf.replace(" as ", " as "))
            refs.append(ImportRef(module=module, bindings=((original, local),), line=line, level=0))
            continue
        module = body[:brace].rstrip(": ")
        inner = body[brace + 1 : body.rfind("}")] if body.rfind("}") > brace else body[brace + 1 :]
        bindings: list[tuple[str, str]] = []
        for piece in inner.split(","):
            original, local = _split_binding(piece.strip())
            if original and original != "self":
                bindings.append((original, local))
        refs.append(ImportRef(module=module, bindings=tuple(bindings), line=line, level=0))
    return tuple(refs[:_MAX_IMPORTS])


def _no_imports(text: str) -> tuple[ImportRef, ...]:
    return ()


# --------------------------------------------------------------------------- #
# Export rules                                                                #
# --------------------------------------------------------------------------- #


def _exported_unless_underscored(name: str, line_text: str) -> bool:
    return not name.startswith("_")


def _exported_by_keyword(name: str, line_text: str) -> bool:
    return line_text.lstrip().startswith("export")


def _exported_by_capital(name: str, line_text: str) -> bool:
    return bool(name) and name[0].isupper()


def _exported_by_pub(name: str, line_text: str) -> bool:
    return line_text.lstrip().startswith("pub")


# --------------------------------------------------------------------------- #
# Profiles                                                                    #
# --------------------------------------------------------------------------- #

_GENERIC_DEFS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(
            r"^[ \t]*(?:async[ \t]+)?(?:def|class|function|fn|type|struct|interface)[ \t]+([A-Za-z_]\w*)",
            re.MULTILINE,
        ),
        "symbol",
    ),
)

_PYTHON_DEFS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)", re.MULTILINE), "function"),
    (re.compile(r"^[ \t]*class[ \t]+([A-Za-z_]\w*)", re.MULTILINE), "class"),
    (re.compile(r"^([A-Za-z_]\w*)[ \t]*(?::[^=\n]+)?=[^=\n]", re.MULTILINE), "binding"),
)

_ECMA_DEFS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(
            r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function\*?[ \t]+([A-Za-z_$]\w*)",
            re.MULTILINE,
        ),
        "function",
    ),
    (re.compile(r"^[ \t]*(?:export[ \t]+)?(?:abstract[ \t]+)?class[ \t]+([A-Za-z_$]\w*)", re.MULTILINE), "class"),
    (re.compile(r"^[ \t]*(?:export[ \t]+)?interface[ \t]+([A-Za-z_$]\w*)", re.MULTILINE), "interface"),
    (re.compile(r"^[ \t]*(?:export[ \t]+)?type[ \t]+([A-Za-z_$]\w*)", re.MULTILINE), "type"),
    (re.compile(r"^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$]\w*)", re.MULTILINE), "binding"),
)

_GO_DEFS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"^func[ \t]+(?:\([^)]*\)[ \t]*)?([A-Za-z_]\w*)", re.MULTILINE), "function"),
    (re.compile(r"^type[ \t]+([A-Za-z_]\w*)", re.MULTILINE), "type"),
    (re.compile(r"^(?:var|const)[ \t]+([A-Za-z_]\w*)", re.MULTILINE), "binding"),
)

_RUST_DEFS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?(?:async[ \t]+)?fn[ \t]+([A-Za-z_]\w*)", re.MULTILINE),
        "function",
    ),
    (re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?struct[ \t]+([A-Za-z_]\w*)", re.MULTILINE), "struct"),
    (re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?enum[ \t]+([A-Za-z_]\w*)", re.MULTILINE), "enum"),
    (re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?trait[ \t]+([A-Za-z_]\w*)", re.MULTILINE), "trait"),
    (re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?(?:const|static)[ \t]+([A-Za-z_]\w*)", re.MULTILINE), "binding"),
)

PARSER_PROFILES: Final[Mapping[str, ParserProfile]] = {
    "lexical-1": ParserProfile(
        name="lexical-1",
        language="generic",
        extensions=(),
        definitions=_GENERIC_DEFS,
        line_comments=("#", "//"),
        block_comments=(("/*", "*/"),),
        quotes=('"""', "'''", '"', "'", "`"),
        read_imports=_no_imports,
        is_exported=_exported_unless_underscored,
    ),
    "python-1": ParserProfile(
        name="python-1",
        language="python",
        extensions=(".py", ".pyi"),
        definitions=_PYTHON_DEFS,
        line_comments=("#",),
        block_comments=(),
        quotes=('"""', "'''", '"', "'"),
        read_imports=_python_imports,
        is_exported=_exported_unless_underscored,
    ),
    "typescript-1": ParserProfile(
        name="typescript-1",
        language="typescript",
        extensions=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
        definitions=_ECMA_DEFS,
        line_comments=("//",),
        block_comments=(("/*", "*/"),),
        quotes=('"', "'", "`"),
        read_imports=_ecma_imports,
        is_exported=_exported_by_keyword,
    ),
    "go-1": ParserProfile(
        name="go-1",
        language="go",
        extensions=(".go",),
        definitions=_GO_DEFS,
        line_comments=("//",),
        block_comments=(("/*", "*/"),),
        quotes=('"', "`"),
        read_imports=_go_imports,
        is_exported=_exported_by_capital,
    ),
    "rust-1": ParserProfile(
        name="rust-1",
        language="rust",
        extensions=(".rs",),
        definitions=_RUST_DEFS,
        line_comments=("//",),
        block_comments=(("/*", "*/"),),
        quotes=('"',),
        read_imports=_rust_uses,
        is_exported=_exported_by_pub,
    ),
}

_BY_EXTENSION: Final[Mapping[str, str]] = {
    extension: profile.name for profile in PARSER_PROFILES.values() for extension in profile.extensions
}


def profile_for_path(path: str) -> str:
    """Which profile a client should declare for ``path``.

    A *hint*, not identity: the profile the server derives under is whatever
    the manifest entry declares, because two repositories can legitimately read
    the same bytes as different languages.
    """
    lowered = path.lower()
    dot = lowered.rfind(".")
    if dot <= lowered.rfind("/"):
        return DEFAULT_PROFILE
    return _BY_EXTENSION.get(lowered[dot:], DEFAULT_PROFILE)


def resolve_profile(parser_profile: str) -> ParserProfile:
    """The reader for ``parser_profile``, falling back to the generic one.

    An unknown profile is not an error: the key still records what the client
    asked for, and the generic reader produces honest lexical rows for it.
    """
    return PARSER_PROFILES.get(parser_profile, PARSER_PROFILES[DEFAULT_PROFILE])


# --------------------------------------------------------------------------- #
# Derivation                                                                  #
# --------------------------------------------------------------------------- #


def trigrams_of(text: str, *, limit: int = _MAX_TRIGRAMS) -> tuple[str, ...]:
    """Sorted, deduplicated lowercase 3-grams over non-space runs."""
    seen: set[str] = set()
    lowered = text.lower()
    for index in range(len(lowered) - 2):
        gram = lowered[index : index + 3]
        if "\n" in gram or gram.isspace():
            continue
        seen.add(gram)
        if len(seen) >= limit:
            break
    return tuple(sorted(seen))


def embed_terms(terms: Iterable[str], *, dim: int = EMBEDDING_DIM) -> tuple[float, ...]:
    """Deterministic hashed bag-of-terms vector, L2-normalized.

    Signed hashing into ``dim`` buckets. No model, no weights, no network --
    the same bytes always produce the same vector on every machine, which is
    what lets the vector be part of an immutable, content-keyed artifact.
    """
    buckets = [0.0] * dim
    for term in terms:
        digest = int.from_bytes(_term_hash(term), "big")
        bucket = digest % dim
        sign = 1.0 if (digest >> 16) & 1 else -1.0
        buckets[bucket] += sign
    norm = math.sqrt(sum(value * value for value in buckets))
    if norm == 0.0:
        return tuple(buckets)
    return tuple(round(value / norm, 6) for value in buckets)


def _term_hash(term: str) -> bytes:
    return hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()


def analyze(digest: str, parser_profile: str, data: bytes) -> AnalysisArtifact:
    """Derive the Layer-1 artifact for ``data`` under ``parser_profile``."""
    profile = resolve_profile(parser_profile)
    text = data.decode("utf-8", errors="replace")
    comment_free = _scrub(text, profile, strings=False)
    scrubbed = _scrub(text, profile, strings=True)

    definitions = _read_definitions(scrubbed, profile)
    defined_spans = {(item.start, item.end) for item in definitions}
    references = _read_references(scrubbed, defined_spans)
    imports = profile.read_imports(comment_free)
    exports = tuple(sorted({item.name for item in definitions if item.exported})[:_MAX_EXPORTS])

    terms: list[str] = []
    seen: set[str] = set()
    for match in _IDENTIFIER_RE.finditer(text):
        token = match.group(0)
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= _MAX_LEXICAL_TERMS:
            break

    return AnalysisArtifact(
        digest=digest,
        parser_profile=parser_profile,
        line_count=text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        byte_count=len(data),
        lexical_terms=tuple(terms),
        symbol_spans=tuple((item.name, item.start, item.end) for item in definitions),
        language=profile.language,
        trigrams=trigrams_of(text),
        definitions=definitions,
        imports=imports,
        exports=exports,
        references=references,
        embedding=embed_terms(terms),
    )


def _read_definitions(scrubbed: str, profile: ParserProfile) -> tuple[SymbolDef, ...]:
    found: dict[tuple[int, int], SymbolDef] = {}
    for pattern, kind in profile.definitions:
        for match in pattern.finditer(scrubbed):
            start, end = match.start(1), match.end(1)
            if (start, end) in found:
                continue
            line_start = scrubbed.rfind("\n", 0, match.start()) + 1
            line_end = scrubbed.find("\n", match.start())
            line_text = scrubbed[line_start : line_end if line_end >= 0 else len(scrubbed)]
            name = match.group(1)
            found[(start, end)] = SymbolDef(
                name=name,
                kind=kind,
                line=_line_of(scrubbed, start),
                start=start,
                end=end,
                exported=profile.is_exported(name, line_text),
            )
            if len(found) >= _MAX_SYMBOL_SPANS:
                break
    return tuple(found[key] for key in sorted(found))


# --------------------------------------------------------------------------- #
# Query semantics, shared by every store                                      #
# --------------------------------------------------------------------------- #

_MAX_QUERY_TERMS: Final[int] = 16
_MAX_QUERY_GRAMS: Final[int] = 32


@dataclass(frozen=True, slots=True)
class LexicalQuery:
    """A parsed lexical query: exact terms plus the trigrams of the raw text."""

    terms: tuple[str, ...]
    grams: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.terms and not self.grams


def parse_query(query: str) -> LexicalQuery:
    """Split a query into the two row kinds Layer 1 can answer from.

    Terms answer ``alpha`` against an identifier index; trigrams answer
    ``lpha`` -- a substring no tokenizer would ever emit -- against a gram
    index. Both are needed, and a hit on either is a hit.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for match in _IDENTIFIER_RE.finditer(query):
        token = match.group(0)
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    collapsed = " ".join(query.split())
    grams = trigrams_of(collapsed, limit=_MAX_QUERY_GRAMS) if len(collapsed) >= 3 else ()
    return LexicalQuery(terms=tuple(terms), grams=grams)


def score_match(
    parsed: LexicalQuery,
    *,
    term_hits: int,
    gram_hits: int,
    match_any_term: bool = False,
) -> int:
    """Score from hit counts alone, so every store agrees exactly.

    Zero means "no match". A store may compute the two counts however it likes
    -- a dict lookup in process, a ``GROUP BY`` over an indexed row table --
    and still produce byte-identical results, which is what the conformance
    suite asserts. ``match_any_term`` is reserved for broad code search; direct
    lexical queries retain their exact all-terms contract.
    """
    exact = bool(parsed.terms) and (term_hits > 0 if match_any_term else term_hits == len(parsed.terms))
    substring = bool(parsed.grams) and gram_hits == len(parsed.grams)
    if not exact and not substring:
        return 0
    return 4 * term_hits + gram_hits


def idf_weight(document_count: int, document_frequency: int) -> float:
    """Bounded positive IDF used by every Layer-1 lexical store.

    The +1 smoothing keeps tiny repositories stable while still making a rare
    identifier worth more than several corpus-common words. A term present in
    every candidate document weighs exactly 1.0, preserving the old scale for
    non-discriminative queries.
    """
    if document_count <= 0:
        return 1.0
    frequency = max(0, min(document_frequency, document_count))
    return 1.0 + math.log((document_count + 1.0) / (frequency + 1.0))


def score_weighted_match(
    parsed: LexicalQuery,
    *,
    matched_terms: Sequence[str],
    term_document_frequency: Mapping[str, int],
    document_count: int,
    gram_hits: int,
    match_any_term: bool = False,
) -> float:
    """IDF-aware form of :func:`score_match` with identical match gates."""
    matched = tuple(dict.fromkeys(matched_terms))
    exact = bool(parsed.terms) and (bool(matched) if match_any_term else len(matched) == len(parsed.terms))
    substring = bool(parsed.grams) and gram_hits == len(parsed.grams)
    if not exact and not substring:
        return 0.0
    term_score = sum(4.0 * idf_weight(document_count, int(term_document_frequency.get(term, 0))) for term in matched)
    return term_score + float(gram_hits)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity over two already-normalized vectors, rounded.

    Rounded so two stores that reach the same vector by different routes rank
    identically instead of differing in the last bit.
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    return round(sum(a * b for a, b in zip(left, right, strict=True)), 6)


def _read_references(scrubbed: str, defined_spans: set[tuple[int, int]]) -> tuple[SymbolRef, ...]:
    seen: set[SymbolRef] = set()
    for match in _IDENTIFIER_RE.finditer(scrubbed):
        span = (match.start(), match.end())
        if span in defined_spans:
            continue
        name = match.group(0)
        if name in _KEYWORDS:
            continue
        tail = scrubbed[match.end() : match.end() + 8].lstrip()
        seen.add(SymbolRef(name=name, line=_line_of(scrubbed, match.start()), is_call=tail.startswith("(")))
        if len(seen) >= _MAX_REFERENCES:
            break
    return tuple(sorted(seen))
