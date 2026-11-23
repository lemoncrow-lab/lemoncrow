"""What changed in a part since the reviewer's own review (P11-2, SPEC 7.1 OWNER-14 and OWNER-15).

A part the reviewer already answered is reopened when its content changes. Most reopenings are routine: a
formatter ran, a comment moved, a function got a new name. This module decides, for one reopened symbol, whether
the change *since the reviewer's own review* is only that, so the Quick check batch can offer one step instead of a
second read.

The comparison is against the content the reviewer reviewed, found through the mark's own fingerprint, **not**
against the diff base: a part that went wrong between the base and the reviewed content and is now only reformatted
is routine; a part that was reviewed, then changed again, is not, however small the base diff looks.

Fail closed everywhere. A proof exists only when every one of these holds, and anything the module cannot establish
is "not routine", which leaves the part in the read queue:

* the reviewer's earlier answer was Looks good (a "needs changes" answer is never closed by a reformat);
* the text read back for the reviewed side matches the fingerprint the mark was made at, and the new side's text
  matches the current unit's fingerprint (a stale or substituted blob is never a proof);
* the decorator or attribute lines above the definition, which sit outside the fingerprint, are unchanged;
* the change is a comment-only or formatting-only change the parser proves for the whole file
  (:func:`routine.prove_region`), or the two bodies are identical once the definition's own name is replaced.

A hunk that was only renumbered (a hunk inserted or removed elsewhere in its file shifts every ordinal below it, so every
hunk mark of the file resets) is routine when the very same hunk was reviewed: the newest revision before this one in
which the reviewer judged this file's hunks has exactly one hunk with the same content, byte for byte (not only the same
fingerprint), this revision has exactly one too, and the reviewer's latest judgment on that hunk was Looks good at that
content. Two identical hunks, a changed character, a judgment of any other kind, or a hunk that is new all stay in the
read queue, and so does one that sits somewhere else: the same base position and ``@@`` function context are required, and
the move of its new-side start must equal the net lines added by the hunks above it. A person still confirms it; nothing is
carried as reviewed.

A rename is never trusted from the ``renamed_from`` label (``symbol_body_digest`` masks every use of the name). Only a
plain module-level function is eligible: a method, class member, nested function, Go receiver function or class can be reached
by name through an interface or a framework, and a name such as ``then``, ``render``, ``setUp`` or ``String`` is read by
one whichever way the rename goes. A module-level function that is decorated, exported from a JS or TS module, a member
of a namespace, a pytest hook or fixture or in ``conftest.py`` is looked up by name too, and is read. The reviewed text must contain the old name exactly once, on the definition's first line, the new text must contain the
new name exactly once and the old name nowhere, and the old text with that one word replaced must equal the new text
(or differ from it only by a proven formatting or comment change). A recursive call, a use in a string, an attribute
of the same name: each makes a second occurrence, and the part goes to the read queue.

A rename is also read when a file at the reviewed head names the old function (a Lambda handler, a ``pyproject`` entry
point, an ``npm`` script, a ``Procfile`` command, a Terraform ``handler``, a Makefile or shell line; ``config_names``):
the diff of the renamed file cannot show that the name is used by a file nobody changed. A source file in the renamed
function's own language counts only when it holds the qualified name, the module stem of the function's file (for Go
also its directory, the package) joined to the name by ``.``, ``::`` or ``:`` (a Django settings string
``app.views.old_name``, a CDK ``handler="index.old_name"``, a Celery beat entry, ``pkg.mod:old_name``, Rust's
``b::old_name``, Go's ``util.Alpha``); its bare name (``getattr(mod, "old_name")``, an import, a call) is not searched.
One bounded search per request covers every renamed part; a search that cannot finish refuses them all (SPEC 7.1
OWNER-19, OWNER-22).
"""

from __future__ import annotations

import builtins
import keyword
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, NamedTuple

from lemoncrow.pro.capabilities.review.config_names import (
    ConfigSearcher,
    HeadConfigFiles,
    module_stems,
    search_config_names,
    source_family,
)
from lemoncrow.pro.capabilities.review.routine import _MAX_BYTES, SideMemo, prove_region, routine_language
from lemoncrow.pro.capabilities.review.routine import _parse as _parse_source
from lemoncrow.pro.capabilities.review.units import (
    anchor_reason,
    bare_fingerprint,
    owned_text_digest,
    owns_deleted_text,
    symbol_fingerprint,
    symbol_header,
)
from lemoncrow.pro.capabilities.review.validity import mark_event_reason

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import ReviewUnit
    from lemoncrow.pro.capabilities.review.store import ReviewStore
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

SINCE_REVIEW_VERSION = 1

REHUNKED_RULE = "rehunked_identical"
_HUNK_FINGERPRINT = "hunk_patch_sha256"
_REHUNK_REASON = mark_event_reason("hunks_renumbered")

# Parts compared with what the reviewer reviewed in one request. Each comparison parses and diffs two whole files, so
# the cost grows with the parts, not with the revision; past the cap a part is "not routine" with a stated reason
# and stays in the read queue (fail closed).
# lc-debt: a flat per-request cap, in target order; upgrade path: prove the rest in a background pass and cache it.
MAX_PARTS_PROVED = 300

# Line breaks Python's ``str.splitlines`` honours but a source file rarely means: the line numbers of a unit
# (counted with ``splitlines``) and of the parser (counted on ``\n``) would disagree, so such a file has no proof.
_EXOTIC_BREAK = re.compile(r"[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_WORD = r"[\w$]"

# Names a rename must not take, or leave: they are read by something that is not in this file's text. A name that
# shadows a builtin rebinds every use of the builtin in the module; a test name is collected by convention and
# stops being collected when it changes.
_PYTHON_RESERVED = frozenset(dir(builtins)) | frozenset(keyword.kwlist)
_GO_PREDECLARED = frozenset(
    "append cap clear close complex copy delete imag len make max min new panic print println real recover "
    "any bool byte comparable complex64 complex128 error float32 float64 int int8 int16 int32 int64 "
    "rune string uint uint8 uint16 uint32 uint64 uintptr true false iota nil main init".split()
)
_JS_GLOBALS = frozenset(
    "Array Boolean Date Error Function JSON Map Math Number Object Promise Proxy Reflect RegExp Set String Symbol "
    "WeakMap WeakSet console document globalThis window undefined NaN Infinity eval parseInt parseFloat isNaN "
    "require module exports process constructor toString valueOf hasOwnProperty main "
    "fetch setTimeout setInterval clearTimeout clearInterval queueMicrotask structuredClone alert confirm prompt open "
    "close print atob btoa location history navigator localStorage sessionStorage URL Request Response Headers "
    "Buffer Intl BigInt".split()
)
_RUST_PRELUDE = frozenset(
    "Some None Ok Err Vec String Option Result Box Clone Copy Default Drop Eq Ord PartialEq PartialOrd Send Sync "
    "Sized Iterator drop main self Self".split()
)
_JAVA_LANG = frozenset(
    "String Object Integer Long Double Float Boolean Byte Short Character Math System Thread Runnable Class "
    "Exception Error Throwable Override main equals hashCode toString".split()
)
# Names a protocol or a framework looks up by itself, in any language: ``then`` makes an object awaitable, ``render``
# is called by React, ``setUp`` by unittest, ``String`` by Go's fmt, ``equals``/``hashCode``/``compareTo`` by the Java
# collections, ``toJSON`` by JSON.stringify. A rename *away from* one of them is as dangerous as a rename to one.
_PROTOCOL_NAMES = frozenset(
    "then render setUp tearDown setUpClass tearDownClass String Error equals hashCode compareTo valueOf toJSON "
    "constructor main init toString".split()
)
_RESERVED_NAMES: dict[str, frozenset[str]] = {
    language: names | _PROTOCOL_NAMES
    for language, names in {
        "python": _PYTHON_RESERVED,
        "go": _GO_PREDECLARED,
        "javascript": _JS_GLOBALS,
        "typescript": _JS_GLOBALS,
        "tsx": _JS_GLOBALS,
        "rust": _RUST_PRELUDE,
        "java": _JAVA_LANG,
    }.items()
}
# ``test_x`` (pytest), ``TestX`` / ``BenchmarkX`` / ``ExampleX`` / ``FuzzX`` (go test), ``testX`` (JUnit 3).
_COLLECTED_BY_NAME = re.compile(r"^(test|Test|Benchmark|Example|Fuzz)")


@dataclass(frozen=True)
class SinceReview:
    """The change since the reviewer's review, as far as it can be proved routine."""

    routine: bool
    rule: str = ""
    """``comment_only`` | ``formatting_only`` | ``rename_only`` when routine; ``""`` otherwise."""
    detail: str = ""
    """One sentence the Quick check row shows: what the proof found, or why none exists."""


def _no(reason: str) -> SinceReview:
    return SinceReview(False, "", reason)


def since_review_payload(proof: SinceReview) -> dict[str, Any]:
    return {"routine": proof.routine, "rule": proof.rule, "detail": proof.detail}


def _canonical(text: str) -> str | None:
    """*text* with CRLF read as LF, or ``None`` when it holds a line break the proof cannot place."""

    text = text.replace("\r\n", "\n")
    return None if _EXOTIC_BREAK.search(text) else text


def _leaf(qualified: str) -> str:
    """The last word of a qualified name when the name ends in one, else ``""``."""

    match = re.search(rf"({_WORD}+)$", qualified)
    return match.group(1) if match else ""


def _whole_word(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!{_WORD}){re.escape(word)}(?!{_WORD})")


def _rename_refusal(
    old_leaf: str, new_leaf: str, language: str, new_lines: Sequence[str], new_span: tuple[int, int]
) -> str:
    """Why a rename of *old_leaf* to *new_leaf* cannot be batched, or ``""`` when nothing in this file objects.

    The reviewer approved a body under one name; a different name can still change behavior with the body
    untouched: another definition of the new name in the file (it is now shadowed or shadows), a caller still using the
    old name (it now breaks or reaches something else), a builtin the module's other code reads, a test the runner
    no longer collects.

    lc-debt: callers in *other source files of the same language* are checked only by the qualified name (every other
    kind of file at the head is searched for the bare old name, a same-language one for ``stem.old_name``,
    ``stem::old_name`` and ``stem:old_name``, ``config_names``), so a rename that leaves an importer calling the bare old name
    is still offered in the batch (the importer is its own changed part when it changed); upgrade path: a reference
    check of both names across every file the revision changed.
    """

    reserved = _RESERVED_NAMES.get(language, frozenset())
    for name in (old_leaf, new_leaf):
        if name.startswith("__") and name.endswith("__"):
            return "A special method name (double underscores) is read by the language, not by a caller in this file."
        if _COLLECTED_BY_NAME.match(name):
            return "A test is found by its name, so a rename changes whether it runs."
        if name in reserved:
            return f"{name} is a name the language or a framework looks up by itself, so renaming it changes what reads this part."
    if language == "go" and old_leaf[:1].isupper() != new_leaf[:1].isupper():
        return "In Go the first letter's case decides whether other packages can use the name, so this rename changes who can call it."
    outside = [line for row, line in enumerate(new_lines, start=1) if not new_span[0] <= row <= new_span[1]]
    if any(_whole_word(new_leaf).search(line) for line in outside):
        return f"{new_leaf} is already used elsewhere in the file."
    if any(_whole_word(old_leaf).search(line) for line in new_lines):
        return f"{old_leaf} is still used in the file, so a caller was not renamed with it."
    return ""


# Python names a runner looks up in a module by itself: pytest hooks (``pytest_configure``), the xunit-style module and
# function fixtures (``setup_module``, ``teardown_function``), nose's ``setup``/``teardown``, unittest's module
# fixtures and ``load_tests``.
_PYTHON_ENTRY_NAME = re.compile(
    r"^(?:pytest_|setup_|teardown_)|^(?:setup|teardown|load_tests|setUpModule|tearDownModule)$"
)
_EXPORTED = re.compile(r"^\s*export\b")
_JS_LANGUAGES = frozenset({"javascript", "typescript", "tsx"})
# A classic script or a worker is read by the host through ``onmessage``, ``onload``, ``onerror``: assigning the
# global by declaring a function of that name is how the handler is installed.
_COMMENT_NODES = frozenset({"comment", "line_comment", "block_comment"})
_EVENT_HANDLER_NAME = re.compile(r"^on[a-z]+$")


_EXPORT_TREES_KEPT = 2


def _export_tree(trees: dict[tuple[str, str], Any], language: str, text: str) -> Any | None:
    """The parse of *text*, kept in *trees* (the caller's own dict) so a file is parsed once, not once per part.

    A request walks its parts by path, so the reviewed and the new file of the part in hand are all that are needed
    at once; the dict never holds more than ``_EXPORT_TREES_KEPT`` parses and goes away with its request.
    """

    key = (language, text)
    if key not in trees:
        if len(trees) >= _EXPORT_TREES_KEPT:
            trees.clear()
        trees[key] = _parse_source(language, text.encode("utf-8"))
    return trees[key]


def _is_lone_export(node: Any) -> bool:
    """Whether *node* is the statement tree-sitter makes of a bare ``export`` keyword (comments folded into it or not)."""

    if node.type != "expression_statement":
        return False
    content = [child for child in node.named_children if child.type not in _COMMENT_NODES]
    return len(content) == 1 and content[0].type == "identifier" and content[0].text == b"export"


def _under_export(language: str, text: str, start_line: int, trees: dict[tuple[str, str], Any]) -> bool:
    """Whether the definition on *start_line* sits directly under an ``export`` statement, wherever its keyword is.

    ``export`` may stand on a line of its own (or behind a comment), so the definition's first line does not show it;
    the parser's parent node does. A file the parser cannot read is answered "yes": nothing is proved about it.
    """

    source = text.encode("utf-8")
    if len(source) > _MAX_BYTES:
        return True
    tree = _export_tree(trees, language, text)
    if tree is None:
        return True
    row = start_line - 1
    line = text.split("\n")[row]
    point = (row, len(line) - len(line.lstrip()))
    node = tree.root_node.descendant_for_point_range(point, point)
    while node is not None:
        parent = node.parent
        if node.start_point == point and parent is not None:
            if parent.type == "export_statement":
                return True
            # tree-sitter reads ``export`` and the declaration after a line break as two statements, though ECMAScript
            # (no semicolon is inserted there) reads one export; the lone word before the definition is the export.
            before = node.prev_sibling
            while before is not None and before.type in _COMMENT_NODES:
                before = before.prev_sibling
            if before is not None and _is_lone_export(before):
                return True
        node = parent
    return False


def _function_refusal(
    old_symbol: str,
    new_symbol: str,
    kind: str,
    *,
    old_first_line: str,
    new_first_line: str,
    header: str,
    language: str,
    path: str,
    exported: bool = False,
) -> str:
    """Why a rename of this definition is read whatever its text says, or ``""`` for a module-level function.

    A method, a class member, a nested function, a Go receiver function and a class can be reached through their
    name by something that is not in the file: an interface that dispatches on it, a framework that discovers it, a
    serializer that writes it out. So can a module-level function that a tool registers or exports by name: a
    decorated or attributed one (``@pytest.fixture``, ``@app.route``, ``#[no_mangle]``), a JS or TS export (Next.js
    ``GET``, ``getServerSideProps``, any importer), a member of a TS namespace, a pytest hook or fixture, anything in
    ``conftest.py``. Only a plain module-level function is batched after a rename.

    A name that only *another file* looks up (a Lambda handler string, a pyproject entry point, a Makefile line) cannot
    be seen from this file or its diff; ``since_review_proofs`` searches the config, manifest and script files at the
    reviewed head for every renamed part at once, after the parts are proved (:func:`_refuse_config_named_renames`).
    """

    if old_symbol != _leaf(old_symbol) or new_symbol != _leaf(new_symbol):
        return (
            "A method or member can be reached by name through an interface or a framework, so a rename of it is read."
        )
    if header:
        return (
            "A decorator or attribute marks this definition for a framework that finds it by name, so a rename is read."
        )
    if old_first_line[:1].isspace() or new_first_line[:1].isspace():
        return "Only a module-level function is batched after a rename; a nested one or a namespace member is read."
    if language in _JS_LANGUAGES and (exported or _EXPORTED.match(old_first_line) or _EXPORTED.match(new_first_line)):
        return "An exported function is found by name by its importers and by frameworks, so a rename is read."
    if language in _JS_LANGUAGES and (
        _EVENT_HANDLER_NAME.match(_leaf(old_symbol)) or _EVENT_HANDLER_NAME.match(_leaf(new_symbol))
    ):
        return "A name such as onmessage or onload is an event handler the host calls by name, so a rename is read."
    if language == "python" and (
        PurePosixPath(path).name == "conftest.py"
        or _PYTHON_ENTRY_NAME.search(_leaf(old_symbol))
        or _PYTHON_ENTRY_NAME.search(_leaf(new_symbol))
    ):
        return "A test runner looks this name up in the module by itself, so a rename changes what it runs."
    if kind in {"lexical_declaration", "variable_declaration"}:
        from lemoncrow.pro.capabilities.review.impact import function_valued_binding

        if function_valued_binding(new_first_line):
            return ""
    elif kind == "function" or kind.startswith(("function_declaration", "function_item", "generator_function")):
        return ""
    return "Only a module-level function is batched after a rename; a method, class or other member is read."


def _normalised_old_body(old_body: str, old_symbol: str, new_symbol: str, new_body: str) -> tuple[str | None, str]:
    """The reviewed body with the definition's own name replaced by the new one, or ``(None, why)``."""

    old_leaf, new_leaf = _leaf(old_symbol), _leaf(new_symbol)
    if not old_leaf or not new_leaf or old_leaf == new_leaf:
        return None, "The name changed somewhere other than the definition's own name."
    if old_symbol[: len(old_symbol) - len(old_leaf)] != new_symbol[: len(new_symbol) - len(new_leaf)]:
        return None, "The name of the enclosing class or module changed."
    old_pattern, new_pattern = _whole_word(old_leaf), _whole_word(new_leaf)
    if len(old_pattern.findall(old_body)) != 1 or new_pattern.search(old_body):
        return None, f"The old name is used elsewhere in the code, or {new_leaf} already appeared in it."
    if len(new_pattern.findall(new_body)) != 1 or old_pattern.search(new_body):
        return None, f"The new name is used elsewhere in the code, or {old_leaf} still appears in it."
    first_line = old_body.split("\n", 1)[0]
    if not old_pattern.search(first_line):
        return None, "The name is not on the definition's first line."
    return old_pattern.sub(new_leaf, old_body, count=1), ""


def prove_symbol_since_review(
    *,
    old_path: str,
    old_text: str,
    old_span: tuple[int, int],
    old_symbol: str,
    new_path: str,
    new_text: str,
    new_span: tuple[int, int],
    new_symbol: str,
    new_side: SideMemo | None = None,
    new_kind: str | None = None,
    export_trees: dict[tuple[str, str], Any] | None = None,
) -> SinceReview:
    """Whether one symbol's *new_text* window differs from its reviewed *old_text* window only routinely.

    The spans are the inclusive 1-based line windows of the definition on each side (the same windows the unit
    fingerprints cover), and the two files are the whole text of each side. *new_kind* is the definition's kind
    on the new side (read from the file when not given). Pure: no store, no clock.
    """

    old_canonical, new_canonical = _canonical(old_text), _canonical(new_text)
    if old_canonical is None or new_canonical is None:
        return _no("The file has line breaks that cannot be compared line by line.")
    old_lines, new_lines = old_canonical.split("\n"), new_canonical.split("\n")
    for (start, end), lines in ((old_span, old_lines), (new_span, new_lines)):
        if not 0 < start <= end <= len(lines):
            return _no("The part's lines are outside its file.")
    language = routine_language(new_path)
    if language != routine_language(old_path):
        return _no("The file's language changed.")
    old_header = symbol_header(old_canonical.splitlines(), old_span[0])
    new_header = symbol_header(new_canonical.splitlines(), new_span[0])
    if old_header is None or new_header is None or old_header != new_header:
        return _no("The decorators or attributes above the definition changed, or could not be read.")
    old_body = "\n".join(old_lines[old_span[0] - 1 : old_span[1]])
    new_body = "\n".join(new_lines[new_span[0] - 1 : new_span[1]])

    renamed = old_symbol != new_symbol
    trees = export_trees if export_trees is not None else {}
    # lc-debt: a rename that touches any second use of the name in the file (recursion, a string, an attribute, a
    # caller) is read, not batched; callers in other files are not checked (see _rename_refusal for the upgrade path).
    compared = old_body
    if renamed:
        renamed_body, why = _normalised_old_body(old_body, old_symbol, new_symbol, new_body)
        if renamed_body is None:
            return _no(why)
        if new_kind is None:
            from lemoncrow.pro.capabilities.review.impact import symbol_windows

            new_kind = next(
                (w.kind for w in symbol_windows(new_canonical, new_path) if w.start_line == new_span[0]), ""
            )
        refusal = _function_refusal(
            old_symbol,
            new_symbol,
            new_kind,
            old_first_line=old_body.split("\n", 1)[0],
            new_first_line=new_body.split("\n", 1)[0],
            header=new_header,
            language=language,
            path=new_path,
            exported=language in _JS_LANGUAGES
            and (
                _under_export(language, old_canonical, old_span[0], trees)
                or _under_export(language, new_canonical, new_span[0], trees)
            ),
        ) or _rename_refusal(_leaf(old_symbol), _leaf(new_symbol), language, new_lines, new_span)
        if refusal:
            return _no(refusal)
        compared = renamed_body
    note = f"Renamed {_leaf(old_symbol)} to {_leaf(new_symbol)}. " if renamed else ""
    # Token-exact, not normalised: a trailing space inside a multi-line string is content, so a body that differs
    # in any byte goes through the parser proof below, which keeps string contents exactly.
    if compared == new_body:
        if renamed:
            return SinceReview(
                True,
                "rename_only",
                f"{note}The code is otherwise the same as when you reviewed it. Callers in other files are not checked.",
            )
        return SinceReview(True, "formatting_only", "The code is the same as when you reviewed it.")
    if not language:
        return _no("The change since your review cannot be proved routine for this kind of file.")

    # The reviewed window, spliced into the new file where the new window stands: the two files then differ only
    # in this one region, so the parser proof reads the region in its real context and any other change fails it.
    compared_lines = compared.split("\n")
    spliced = new_lines[: new_span[0] - 1] + compared_lines + new_lines[new_span[1] :]
    proof = prove_region(
        new_path,
        "\n".join(spliced),
        new_canonical,
        (new_span[0], new_span[0] + len(compared_lines) - 1),
        new_span,
        new_side=new_side,
    )
    if proof is None:
        return _no("The change since your review is more than formatting or comments.")
    suffix = " Callers in other files are not checked." if renamed else ""
    return SinceReview(True, proof.rule, f"{note}{proof.detail}{suffix}")


# ------------------------------------------------------------------ the store side


def _window_of(
    text: str, path: str, unit: ReviewUnit, windows_of: Callable[[str, str], Sequence[Any]]
) -> tuple[int, int] | None:
    """The unit's window in *text* when the text still has the body the unit was stored with.

    A unit that owns deleted base lines carries a composite fingerprint (``<body and deleted text>~<body>~<deleted text>``); the
    window is found by the body half, and :func:`_owned_old_unchanged` answers for the deleted-text half.
    """

    if unit.kind != "symbol" or unit.fingerprint_method == "unknown" or not 0 < unit.start_line <= unit.end_line:
        return None
    for window in windows_of(text, path):
        if (
            window.start_line == unit.start_line
            and window.end_line == unit.end_line
            and symbol_fingerprint(window.kind, unit.symbol, window.body) == bare_fingerprint(unit.content_fingerprint)
        ):
            return unit.start_line, unit.end_line
    return None


class _Renamed(NamedTuple):
    """A part proved routine after a rename: what a config search needs to know about its function."""

    name: str
    """The old name of the function."""
    families: frozenset[str]
    """The language families of its files (empty when one of them is not a known source language)."""
    stems: frozenset[str]
    """The module stems of its files, old and new path: what a same-language file is searched for it by."""


@dataclass
class _Request:
    """What one :func:`since_review_proofs` call reads once and shares across its parts."""

    marks: Mapping[str, Any]
    current: Mapping[str, ReviewUnit]
    events_by_key: Mapping[str, Sequence[Any]]
    store: ReviewStore
    review_id: str
    read_new_text: Callable[[str], str | None]
    windows_of: Callable[[str, str], Sequence[Any]]
    new_side: SideMemo
    symbols_by_fingerprint: dict[str, dict[str, list[ReviewUnit]]]
    blobs_by_revision: dict[str, Mapping[str, str] | None]
    revision_id: str = ""
    hunks_by_revision: dict[str, dict[str, list[ReviewUnit]]] = field(default_factory=dict)
    patches_by_revision: dict[str, dict[str, list[_PacketHunk]] | None] = field(default_factory=dict)
    prior_by_path: dict[str, str | None] = field(default_factory=dict)
    revisions: Sequence[Any] | None = None
    export_trees: dict[tuple[str, str], Any] = field(default_factory=dict)
    """Parses made to tell an exported definition; at most a few, and dropped with the request."""
    head_config_files: HeadConfigFiles | None = None
    config_search: ConfigSearcher | None = None
    renamed: dict[str, _Renamed] = field(default_factory=dict)
    """target_id -> the old name of every part proved routine after a rename, and the families and stems of its files."""


def since_review_proofs(
    store: ReviewStore,
    review_id: str,
    reviewer_id: str,
    revision_id: str,
    targets: Iterable[ReviewTarget],
    read_new_text: Callable[[str], str | None],
    *,
    limit: int | None = None,
    head_config_files: HeadConfigFiles | None = None,
    retry: set[str] | None = None,
    config_search: ConfigSearcher | None = None,
) -> dict[str, SinceReview]:
    """``target_id -> proof`` for every part this reviewer's earlier answer was reopened on.

    Parts that are not reopened get no entry, except an unanswered hunk of a file whose hunks were renumbered since the
    reviewer judged them (a re-hunking resets every hunk mark of the file): it is compared with the hunks the reviewer
    reviewed. A reopened part gets one entry, routine or not: an unprovable part has a reason, never a missing answer.
    At most *limit* parts (``MAX_PARTS_PROVED``) are compared; the rest are "not routine".

    *head_config_files* reads the searchable files at the reviewed head (``config_names.head_config_files``). A part
    proved routine after a rename stays routine only when none of them names the old function; without a reader
    (``None``), or when the search cannot finish, every such part is read. *retry*, when given, receives the ids of the
    parts refused only because the search ran out of time: that answer depends on load, so a caller should not keep it.
    *config_search* replaces :func:`search_config_names` (the product passes a per-head memoizing one).
    """

    limit = MAX_PARTS_PROVED if limit is None else limit

    from lemoncrow.pro.capabilities.review.impact import symbol_windows

    targets = list(targets)
    reopened = [target for target in targets if target.state == "changed_since_review" and target.changed_since_mark]
    hunks = [target for target in targets if target.kind == "hunk" and target.state == "unreviewed"]
    if not reopened and not hunks:
        return {}
    marks = {mark.unit_key: mark for mark in store.list_marks(review_id, reviewer_id=reviewer_id)}
    current = {unit.unit_key: unit for unit in store.list_units(revision_id)}
    events_by_key: dict[str, list[Any]] = defaultdict(list)
    # lc-debt: reads the reviewer's whole mark-event log once per revision; index by reopened keys if logs grow large.
    for event in store.list_mark_events(review_id, reviewer_id=reviewer_id):
        events_by_key[event.unit_key].append(event)

    # Only the file being compared is held: parts are walked file by file, so one window list and one parse of the
    # new text live at a time instead of one per file of the revision.
    window_cache: dict[tuple[int, str], Sequence[Any]] = {}

    def windows_of(text: str, path: str) -> Sequence[Any]:
        key = (hash(text), path)
        if key not in window_cache:
            if len(window_cache) >= 64:
                window_cache.clear()
            window_cache[key] = symbol_windows(text, path)
        return window_cache[key]

    request = _Request(
        marks, current, events_by_key, store, review_id, read_new_text, windows_of, SideMemo(), {}, {}, revision_id
    )
    request.head_config_files = head_config_files
    request.config_search = config_search
    # Only a hunk of a file that was in fact re-hunked has an answer; the others are simply unanswered parts.
    reopened += [target for target in hunks if _rehunked_prior(request, target.path) is not None]
    out: dict[str, SinceReview] = {}
    for target in reopened[limit:]:
        out[target.target_id] = _no(
            f"Only {limit} reopened parts are compared with your review at a time, so this one is read."
        )
    for target in sorted(reopened[:limit], key=lambda item: item.path):
        out[target.target_id] = _prove_target(target, request)
    _refuse_config_named_renames(request, out, retry)
    return out


def _refuse_config_named_renames(request: _Request, out: dict[str, SinceReview], retry: set[str] | None) -> None:
    """Turn every routine rename whose old name a config, manifest or script file at the head holds (or may hold) into a part to read.

    One search for all the old names of the request, not one per part. The search runs only when a part was proved
    routine after a rename.
    """

    pending = {target_id: item for target_id, item in request.renamed.items() if out[target_id].routine}
    if not pending:
        return
    found = None
    why = "The config, manifest and script files were not available to search for this function's name, so a rename is read."
    if request.head_config_files is not None:
        families: dict[str, frozenset[str]] = {}
        modules: dict[str, frozenset[str]] = {}
        for item in pending.values():
            families[item.name] = families[item.name] | item.families if item.name in families else item.families
            modules[item.name] = modules.get(item.name, frozenset()) | item.stems
        search = request.config_search or search_config_names
        found = search(set(families), request.head_config_files, families=families, modules=modules)
        why = (
            f"The config, manifest and script files could not all be searched for this function's name "
            f"({found.incomplete}), so a rename is read."
        )
    for target_id, item in pending.items():
        name = item.name
        if found is not None and name in found.named:
            out[target_id] = _no(
                "A config, manifest or script file names this function, so a rename is read. "
                f"Found in {found.named[name]}."
            )
        elif found is None or (found.incomplete and (found.unsearched is None or name in found.unsearched)):
            out[target_id] = _no(why)
            if retry is not None and found is not None and found.transient:
                retry.add(target_id)
        else:
            proof = out[target_id]
            out[target_id] = SinceReview(
                True,
                proof.rule,
                f"{proof.detail} No config, manifest or script file at the head names {name} "
                f"({found.searched} files searched).",
            )


def _reviewed_candidates(request: _Request, revision: str, fingerprint: str) -> list[ReviewUnit] | None:
    """The reviewed revision's symbol units with this content fingerprint, from an index built once per revision."""

    if revision not in request.symbols_by_fingerprint:
        index: dict[str, list[ReviewUnit]] = defaultdict(list)
        for item in request.store.list_units(revision):
            if item.kind == "symbol":
                index[item.content_fingerprint].append(item)
        request.symbols_by_fingerprint[revision] = index
        request.blobs_by_revision[revision] = request.store.read_blob_artifact(request.review_id, revision)
    if request.blobs_by_revision[revision] is None:
        return None
    return request.symbols_by_fingerprint[revision].get(fingerprint, [])


def _prove_target(target: ReviewTarget, request: _Request) -> SinceReview:
    if target.kind == "hunk" and target.state == "unreviewed":
        return _prove_rehunked(target, request)
    mark = request.marks.get(target.unit_key)
    unit = request.current.get(target.unit_key)
    if target.kind != "symbol" or mark is None or unit is None:
        return _no("Only a function, method or class can be compared with what you reviewed.")
    place = anchor_reason(mark.content_fingerprint, unit.content_fingerprint)
    if place == "part_order_changed":
        return _no(
            "Its position among the unchanged lines of an order-sensitive file changed since your review, so it is read."
        )
    if place is not None:
        return _no("Where it sits in an order-sensitive file cannot be checked against your review, so it is read.")
    if not mark.reviewed_revision_id or unit.content_fingerprint == mark.content_fingerprint:
        return _no("There is no earlier review to compare with.")
    # The answer the reopening replaced: the reviewer's latest judgment on this part or on the part it was renamed or
    # moved from (the reopening event itself says nothing about the old answer across a rename). It must be a
    # Looks good made at the very content the mark still holds.
    lineage = {target.unit_key}
    pending = [target.unit_key]
    while pending:
        for event in request.events_by_key.get(pending.pop(), ()):
            if event.previous_unit_key and event.previous_unit_key not in lineage:
                lineage.add(event.previous_unit_key)
                pending.append(event.previous_unit_key)
    judgments = [
        event for key in lineage for event in request.events_by_key.get(key, ()) if event.event_kind == "judgment"
    ]
    judgments.sort(key=lambda event: (event.created_at, event.id))
    if (
        not judgments
        or judgments[-1].to_state != "reviewed"
        or judgments[-1].content_fingerprint != mark.content_fingerprint
    ):
        return _no("Your earlier answer was not Looks good, so a new answer is needed.")

    candidates = _reviewed_candidates(request, mark.reviewed_revision_id, mark.content_fingerprint)
    blobs = request.blobs_by_revision[mark.reviewed_revision_id]
    if blobs is None or not candidates:
        return _no("The code you reviewed could not be read back.")
    reviewed: set[tuple[str, str, tuple[int, int], str]] = set()
    for item in candidates:
        text = blobs.get(item.path)
        span = _window_of(text, item.path, item, request.windows_of) if text is not None else None
        if text is not None and span is not None:
            reviewed.add((item.path, text, span, item.symbol))
    if len(reviewed) != 1:
        return _no("The code you reviewed could not be found exactly once.")
    old_path, old_text, old_span, old_symbol = next(iter(reviewed))

    new_text = request.read_new_text(unit.path)
    new_span = _window_of(new_text, unit.path, unit, request.windows_of) if new_text is not None else None
    if new_text is None or new_span is None:
        return _no("The new code could not be read back exactly.")
    if not _owned_old_unchanged(mark, unit):
        return _no("The deleted lines beside this part changed since your review, so it is read.")
    proof = prove_symbol_since_review(
        old_path=old_path,
        old_text=old_text,
        old_span=old_span,
        old_symbol=old_symbol,
        new_path=unit.path,
        new_text=new_text,
        new_span=new_span,
        new_symbol=unit.symbol,
        new_side=request.new_side,
        new_kind=next((w.kind for w in request.windows_of(new_text, unit.path) if w.start_line == new_span[0]), ""),
        export_trees=request.export_trees,
    )
    if proof.routine and old_symbol != unit.symbol:
        own = {source_family(old_path), source_family(unit.path)}
        stems = module_stems(old_path) | module_stems(unit.path)
        request.renamed[target.target_id] = _Renamed(
            _leaf(old_symbol), frozenset() if "" in own else frozenset(own), stems
        )
    return proof


def _owned_old_unchanged(mark: Any, unit: ReviewUnit) -> bool:
    """Whether the deleted base lines the part owns are the ones the verdict covered.

    The mark's fingerprint covers the reviewed body *and* the deleted text it owned (and where that text sat in
    the base file). A routine change of the body says nothing about that text, so the digest of the owned text in
    the mark must be the part's own now: only then was the deleted text read too. A part with no owned text on
    either side has nothing to compare, and a mark made before the digest existed cannot be compared (read).
    """

    if not owns_deleted_text(mark.content_fingerprint) and not owns_deleted_text(unit.content_fingerprint):
        return True
    reviewed = owned_text_digest(mark.content_fingerprint)
    return bool(reviewed) and reviewed == owned_text_digest(unit.content_fingerprint)


# --------------------------------------------------------------- a hunk that was only renumbered

# lc-debt: a hunk's earlier review is looked for in the newest few revisions only; upgrade path: index the reviewer's
# hunk judgments by revision once and read the file's history from it.
MAX_REVISIONS_WALKED = 8


def _hunk_index(request: _Request, revision_id: str) -> dict[str, list[ReviewUnit]]:
    """The revision's hunk units by path, read once per request."""

    if revision_id not in request.hunks_by_revision:
        index: dict[str, list[ReviewUnit]] = defaultdict(list)
        for item in request.store.list_units(revision_id):
            if item.kind == "hunk":
                index[item.path].append(item)
        request.hunks_by_revision[revision_id] = index
    return request.hunks_by_revision[revision_id]


@dataclass(frozen=True)
class _PacketHunk:
    """One hunk as the revision's stored packet printed it."""

    patch: str
    context: str
    """The text after the second ``@@`` of the header: the enclosing function as git saw it."""
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int


_HEADER_CONTEXT = re.compile(r"^@@[^@]*@@ ?(.*)$")


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _hunk_patches(request: _Request, revision_id: str) -> dict[str, list[_PacketHunk]] | None:
    """Each file's hunks in ordinal order, from the revision's stored packet, read once per request."""

    if revision_id not in request.patches_by_revision:
        from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

        revision = request.store.get_revision(revision_id)
        packet = None if revision is None else read_packet_json(request.store, revision)
        patches: dict[str, list[_PacketHunk]] | None = None
        if packet is not None:
            patches = {}
            for entry in packet.get("files") or ():
                if not isinstance(entry, dict):
                    continue
                hunks: list[_PacketHunk] = []
                for hunk in entry.get("hunks") or ():
                    raw = hunk if isinstance(hunk, dict) else {}
                    match = _HEADER_CONTEXT.match(str(raw.get("header") or ""))
                    hunks.append(
                        _PacketHunk(
                            str(raw.get("patch") or ""),
                            match.group(1) if match else "",
                            _int(raw.get("old_start")),
                            _int(raw.get("old_lines")),
                            _int(raw.get("new_start")),
                            _int(raw.get("new_lines")),
                        )
                    )
                patches[str(entry.get("path") or "")] = hunks
        request.patches_by_revision[revision_id] = patches
    return request.patches_by_revision[revision_id]


def _net_change_above(hunks: Sequence[_PacketHunk], ordinal: int) -> int:
    """Lines the hunks before *ordinal* added to the file, net: what shifts every hunk below them."""

    return sum(item.new_lines - item.old_lines for item in hunks[:ordinal])


def _judgments(request: _Request, key: str) -> list[Any]:
    """The reviewer's judgments on one part before this revision, oldest first."""

    found = [
        event
        for event in request.events_by_key.get(key, ())
        if event.event_kind == "judgment" and event.revision_id != request.revision_id
    ]
    found.sort(key=lambda event: (event.created_at, event.id))
    return found


def _rehunked_prior(request: _Request, path: str) -> str | None:
    """The newest earlier revision in which the reviewer judged this file's hunks, if the file's hunks were renumbered since.

    ``None`` when there is no such revision, or when the file has as many hunks as it had then (nothing was renumbered).
    """

    if path in request.prior_by_path:
        return request.prior_by_path[path]
    prior: str | None = None
    if request.revisions is None:
        request.revisions = request.store.list_revisions(request.review_id)
    revisions = request.revisions
    current = next((item.revision_number for item in revisions if item.id == request.revision_id), None)
    if current is not None:
        earlier = [item for item in revisions if item.revision_number < current][-MAX_REVISIONS_WALKED:]
        for item in reversed(earlier):
            hunks = _hunk_index(request, item.id).get(path, ())
            if any(event.revision_id == item.id for hunk in hunks for event in _judgments(request, hunk.unit_key)):
                if len(hunks) != len(_hunk_index(request, request.revision_id).get(path, ())):
                    prior = item.id
                break
    request.prior_by_path[path] = prior
    return prior


def _prove_rehunked(target: ReviewTarget, request: _Request) -> SinceReview:
    """Is this unanswered hunk exactly one hunk the reviewer reviewed, only renumbered by a re-hunking of its file?"""

    unit = request.current.get(target.unit_key)
    if unit is None or unit.kind != "hunk" or unit.fingerprint_method != _HUNK_FINGERPRINT:
        return _no("This hunk's content could not be compared with what you reviewed.")
    mark = request.marks.get(target.unit_key)
    if mark is not None:
        history = sorted(request.events_by_key.get(target.unit_key, ()), key=lambda event: (event.created_at, event.id))
        if not history or history[-1].event_kind != "reconciled" or history[-1].reason != _REHUNK_REASON:
            return _no("This hunk was not reopened only because its file was re-hunked.")
    prior = _rehunked_prior(request, unit.path)
    if prior is None:
        return _no("The file was not re-hunked since your review.")
    now = [
        item
        for item in _hunk_index(request, request.revision_id).get(unit.path, ())
        if item.content_fingerprint == unit.content_fingerprint
    ]
    then = [
        item
        for item in _hunk_index(request, prior).get(unit.path, ())
        if item.content_fingerprint == unit.content_fingerprint
    ]
    if not then:
        return _no("No hunk you reviewed has this content, so it is new to you.")
    if len(now) != 1 or len(then) != 1:
        return _no("The same hunk appears more than once, so which one you reviewed is not certain.")
    old = then[0]
    if old.fingerprint_method != _HUNK_FINGERPRINT:
        return _no("The hunk you reviewed could not be compared.")
    judgments = _judgments(request, old.unit_key)
    if (
        not judgments
        or judgments[-1].to_state != "reviewed"
        or judgments[-1].actor_type != "human"
        or judgments[-1].content_fingerprint != old.content_fingerprint
    ):
        return _no("The same hunk was not answered Looks good in your review, so a new answer is needed.")
    old_patches = _hunk_patches(request, prior)
    new_patches = _hunk_patches(request, request.revision_id)
    try:
        old_file = (old_patches or {})[old.path]
        new_file = (new_patches or {})[unit.path]
        before, after = old_file[old.ordinal], new_file[unit.ordinal]
    except (KeyError, IndexError):
        return _no("The code you reviewed could not be read back.")
    if not before.patch or before.patch != after.patch:
        return _no("The hunk is not byte for byte the one you reviewed.")
    # The same bytes can sit in a different place: a copy of the change made twenty lines lower is another change.
    # Only a shift that the hunks inserted or removed above account for is "only its position moved".
    if min(before.old_start, before.new_start, after.old_start, after.new_start) < 0:
        return _no("The hunk's position could not be compared with where you reviewed it.")
    if before.context != after.context or before.old_start != after.old_start:
        return _no("The same lines changed in a different place, so this is read again.")
    if after.new_start - before.new_start != _net_change_above(new_file, unit.ordinal) - _net_change_above(
        old_file, old.ordinal
    ):
        return _no("The hunk moved by more than the hunks above it added or removed, so this is read again.")
    return SinceReview(True, REHUNKED_RULE, "The same hunk you reviewed; only its position in the file moved.")


__all__ = [
    "MAX_PARTS_PROVED",
    "REHUNKED_RULE",
    "SINCE_REVIEW_VERSION",
    "SinceReview",
    "prove_symbol_since_review",
    "since_review_payload",
    "since_review_proofs",
]
