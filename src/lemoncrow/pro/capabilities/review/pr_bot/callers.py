"""Who calls the changed code, read from the pull request's head commit: the Action's stand-in for the Reader's index.

When the capture had no code index, the Reader asks its current index who uses the changed symbols
(``file_relationships``) and ``consumer_inventory`` joins the answer to the Snapshot. The Action has no index, so it
builds the part of one that this question needs, from git at the head commit (the Action never checks the pull
request's files out), or from the working tree when there is no head commit (``lc review --ai`` on uncommitted work).
The Reader runs the same search when no index answers, so every surface gives one Risk for one Snapshot:

1. one ``git grep`` over the head tree, limited to the changed symbols' languages, lists every line that names one of
   them as a whole word; a line that plainly defines a name (``def f``, ``function f``) is noted;
2. a name plainly defined in another file too is ambiguous: its callers are not measured. Each file that mentions one of the
   other names is read from the head commit and parsed where the grep saw a name: Python with ``ast``, other
   languages with the repo map's tag rules (``tags._treesitter_tags``) and the capture's definition gate
   (``impact._dedupe_definition_tags``, so an import is not a definition);
3. a file outside the change that calls the name, or otherwise uses it, is a caller of that symbol when the name is
   defined in one file only, the changed one, and (Python, JavaScript, TypeScript) an import ties the file to it: the
   name rule of the Reader's index (``code_map.legacy_file_links``), tightened by :class:`_ImportGate`.

The answer has ``file_relationships``' shape, so ``consumer_inventory`` joins it exactly as it joins the Reader's and
``brief_summary.change_risk`` runs on the result unchanged. Test files among the callers are the tests that exercise
the changed code.

Bounds are semantics. A symbol the search could not finish for is listed in ``unmeasured`` with the reason, and the
Risk line says its callers were not measured; it never reads as "no outside callers". The bounds:

* more than :attr:`Bounds.symbols` changed symbols: the rest are not searched;
* a name more than :attr:`Bounds.files_per_name` files mention: its mentions are not parsed;
* :attr:`Bounds.files` files parsed, :attr:`Bounds.total_bytes` read, or :attr:`Bounds.seconds` spent: symbols whose
  mentions were not all parsed by then are not measured;
* a file over :attr:`Bounds.file_bytes`, or one that does not parse (or has no parser here): the names it mentions are
  not measured;
* git grep printing more than :attr:`Bounds.grep_bytes`, or failing: nothing is measured;
* a symbol in a language with no reference parser here (shell, SQL, one with no grammar configured), or a file whose
  grammar is not downloaded (the search never downloads one: a download cannot be cut by the time bound): not
  measured;
* a name defined in another file too: a use does not say which one it calls, so not measured, never "no callers".

Symbols in data and markup files (JSON, TOML, YAML, HTML, CSS, Markdown) are not code nothing calls, so they are not
searched and need no measurement. A deleted symbol is left to the capture's removed-symbol detector, which already
lists the files that still use it. Callers in another language than the changed symbol's are out of scope, as they
are for the capture's own caller pass.
"""

# lc-debt: a name defined in more than one file is not measured (two `save` methods), and an aliased import
# (`import f as g`) is not followed. Python and JavaScript/TypeScript uses must be tied by an import (`_ImportGate`);
# other languages still go by the name. Upgrade: import tags per language in infra.tree_sitter.tags.

from __future__ import annotations

import ast
import bisect
import contextlib
import functools
import re
import subprocess
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, Final

from lemoncrow.infra.tree_sitter.tags import (
    _DATA_LANGUAGES,
    _LEGACY_REGEX_LANGUAGES,
    _MARKUP_LANGUAGES,
    _NO_REFERENCE_LANGUAGES,
    Tag,
    detect_language,
    extract_tags_from_text,
    tree_sitter_languages,
)
from lemoncrow.pro.capabilities.review.architecture_imports import ImportResolver

#: ``source`` of the answer. Its sites' ``exactness`` is ``snapshot`` when read from the revision's head commit and
#: ``current_index`` when read from today's working tree, which may have moved since the capture.
SOURCE: Final[str] = "checkout_search"
_NAME: Final = re.compile(r"^[A-Za-z_$][\w$]*$")
#: Languages one name space spans: a TypeScript file calls a JavaScript function.
_FAMILY: Final[Mapping[str, str]] = {"typescript": "javascript"}
_NOT_CODE: Final[frozenset[str]] = _DATA_LANGUAGES | _MARKUP_LANGUAGES
_EXCLUDES: Final[tuple[str, ...]] = (":(exclude).lemoncrow", ":(exclude,glob)**/node_modules/**")
#: Rows below a definition's first line its name may sit on (decorators, a long export line).
_NAME_ROWS: Final[int] = 8


# lc-debt: fixed bounds sized so a typical repository answers in well under a second and a large one stops at a few
# seconds; past them symbols read as not measured. Upgrade: keep a per-repository tag cache between runs (the repo map's
# TagCache keyed by blob id) so the parse bound rarely binds.
@dataclass(frozen=True, slots=True)
class Bounds:
    """How far one search may go; past any of them the affected symbols are not measured."""

    symbols: int = 200
    files_per_name: int = 400
    files: int = 2_000
    file_bytes: int = 1 << 20
    total_bytes: int = 64 << 20
    grep_bytes: int = 16 << 20
    seconds: float = 5.0


DEFAULT_BOUNDS: Final = Bounds()


@dataclass(frozen=True, slots=True)
class _Symbol:
    path: str
    #: The packet's name (what the join matches) and the bare name a use spells.
    name: str
    short: str
    family: str


@dataclass(slots=True)
class _Uses:
    """One file's definitions, calls and other references of the searched names, each with its first line."""

    defs: set[str] = field(default_factory=set)
    calls: dict[str, int] = field(default_factory=dict)
    refs: dict[str, int] = field(default_factory=dict)
    #: How each name was spelled: on its own, on another object (``x.name``), on ``self``/``cls``/``this``.
    #: All empty for a reader that cannot tell (the regex tags).
    bare: set[str] = field(default_factory=set)
    member: set[str] = field(default_factory=set)
    own: set[str] = field(default_factory=set)


def _family(language: str) -> str:
    return _FAMILY.get(language, language)


def _searchable(language: str) -> bool:
    if language == "python" or language in _LEGACY_REGEX_LANGUAGES:
        return True
    if language in _NO_REFERENCE_LANGUAGES:
        return False
    parsers = tree_sitter_languages()
    return parsers is not None and language in parsers


def _language(path: str) -> str:
    return _language_of(path.rsplit("/", 1)[-1])


@functools.lru_cache(maxsize=4096)
def _language_of(filename: str) -> str:
    """A file's language from its name alone, as ``detect_language`` decides it."""

    return detect_language(Path(filename)) or ""


def _pathspecs(families: frozenset[str]) -> list[str]:
    """Git pathspecs for the files of *families*, so the grep reads no file the search would throw away."""

    from lemoncrow.infra.code_intel.languages import LANGUAGES

    specs: list[str] = []
    for language in LANGUAGES:
        if _family(language.name) in families:
            specs += [f":(icase)*{extension}" for extension in sorted(language.extensions)]
            specs += [f":(glob)**/{filename}" for filename in sorted(language.filenames)]
    return specs


def _note_python_place(uses: _Uses, node: ast.AST, name: str) -> None:
    if isinstance(node, ast.Attribute):
        owner = node.value.id if isinstance(node.value, ast.Name) else ""
        (uses.own if owner in ("self", "cls") else uses.member).add(name)
    else:
        uses.bare.add(name)


def _python_uses(text: str, names: frozenset[str], lines: Sequence[int]) -> _Uses:
    """Definitions (functions, classes, module-level names), calls (``f()``, ``x.f()``) and other uses of *names*.

    Only the parts of the tree that span one of *lines* (where the grep saw a name) are walked: the parse is the cost
    that is left, not a walk of every node of a large file.
    """

    uses = _Uses()
    tree = ast.parse(text)
    for stmt in tree.body:
        targets = (
            stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
        )
        uses.defs.update(target.id for target in targets if isinstance(target, ast.Name) and target.id in names)
    seen = sorted(set(lines))

    def spans(node: ast.AST) -> bool:
        start = getattr(node, "lineno", None)
        if start is None or not seen:
            return True
        decorators = getattr(node, "decorator_list", None) or ()
        start = min([start, *(int(item.lineno) for item in decorators)])
        end = getattr(node, "end_lineno", None) or start
        at = bisect.bisect_left(seen, start)
        return at < len(seen) and seen[at] <= end

    # Depth-first from the root: a call is always seen before the name it calls.
    called: set[int] = set()
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        stack.extend(child for child in ast.iter_child_nodes(node) if spans(child))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name in names:
                uses.defs.add(node.name)
            continue
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name in names:
                called.add(id(func))
                uses.calls[name] = min(uses.calls.get(name, node.lineno), node.lineno)
                _note_python_place(uses, func, name)
            continue
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
        if name in names and id(node) not in called:
            line = int(getattr(node, "lineno", 0))
            uses.refs[name] = min(uses.refs.get(name, line), line)
            _note_python_place(uses, node, name)
    return uses


def _tag_uses(text: str, path: str, language: str, names: frozenset[str]) -> _Uses:
    """The repo map's tags of a whole file (no grammar installed: the regex readers)."""

    from lemoncrow.pro.capabilities.review.impact import _dedupe_definition_tags

    uses = _Uses()
    tags = [tag for tag in extract_tags_from_text(text, path, language=language) if tag.name in names]
    definitions, _dropped = _dedupe_definition_tags(tags, text.encode("utf-8", "surrogatepass"))
    uses.defs.update(tag.name for tag in definitions)
    for tag in tags:
        if tag.kind != "definition":
            found = uses.calls if tag.kind == "call" else uses.refs
            found[tag.name] = min(found.get(tag.name, tag.line), tag.line)
    return uses


def _tail_identifier(node: Any, kinds: frozenset[str]) -> Any | None:
    """``tags._last_descendant``'s answer (the last identifier in pre-order) found from the right, not by walking all."""

    for child in reversed(node.children):
        found = _tail_identifier(child, kinds)
        if found is not None:
            return found
    return node if str(node.type) in kinds else None


def _tree_uses(text: str, path: str, language: str, names: frozenset[str], lines: Sequence[int]) -> _Uses | None:
    """The repo map's tag rules (``tags._treesitter_tags``) applied only where the grep saw a name.

    The same definition kinds, local scopes and container kinds, the same call fields and identifier kinds, and the
    capture's definition gate (``impact._dedupe_definition_tags``, so an import is not a definition); but only the
    nodes that span one of *lines* are visited, which is what makes a 10,000-line file cost its parse and no more.
    ``None`` when the language has no grammar here.
    """

    from lemoncrow.infra.tree_sitter import tags as tag_rules
    from lemoncrow.pro.capabilities.review.impact import _dedupe_definition_tags
    from lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast import (
        definition_node_kinds,
        tree_sitter_parser,
    )

    parser = tree_sitter_parser(language)
    if parser is None:
        return None
    source = text.encode("utf-8", "surrogatepass")
    root = parser.parse(source).root_node
    rows = sorted({line - 1 for line in lines})
    row_set = frozenset(rows)

    def spans(node: Any) -> bool:
        at = bisect.bisect_left(rows, int(node.start_point[0]))
        return at < len(rows) and rows[at] <= int(node.end_point[0])

    def named(node: Any) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace").strip()

    def place(node: Any, name: str) -> None:
        parent = node.parent
        receiver = parent.child_by_field_name("object") if parent is not None else None
        prop = parent.child_by_field_name("property") if parent is not None else None
        if receiver is not None and prop is not None and prop.start_byte == node.start_byte:
            (uses.own if named(receiver) == "this" else uses.member).add(name)
        else:
            uses.bare.add(name)

    definition_kinds = definition_node_kinds(language)
    local_scopes = tag_rules._LOCAL_SCOPE_KINDS.get(language, frozenset())
    call_fields = tag_rules._CALL_NODE_FIELDS.get(language, {})
    candidates: list[Tag] = []
    taken: set[tuple[int, int]] = set()
    uses = _Uses()
    stack: list[tuple[Any, bool, str | None]] = [(root, False, None)]
    while stack:
        node, in_local, container = stack.pop()
        kind = str(node.type)
        is_definition = kind in definition_kinds
        # A definition's name sits in its first lines; finding it in a large node walks the node, so only a
        # definition that starts near a line the grep saw is asked for its name.
        near = bisect.bisect_left(rows, int(node.start_point[0]))
        if is_definition and not in_local and near < len(rows) and rows[near] <= node.start_point[0] + _NAME_ROWS:
            bodyless = kind in tag_rules._BODYLESS_REFERENCE_KINDS and node.child_by_field_name("body") is None
            name_node = None if bodyless else tag_rules._definition_name_node(node, language)
            if name_node is not None and (name := named(name_node)) in names:
                line = int(name_node.start_point[0]) + 1
                span = (int(name_node.start_byte), int(name_node.end_byte))
                candidates.append(Tag(name, "definition", path, line, span, node_kind=kind, container_kind=container))
        fields = call_fields.get(kind)
        if fields:
            for field_name in fields:
                field_node = node.child_by_field_name(field_name)
                if field_node is not None:
                    if int(field_node.end_point[0]) not in row_set:
                        break  # The callee name ends the field, on a line no name is on.
                    callee = _tail_identifier(field_node, tag_rules._IDENTIFIER_KINDS) or field_node
                    if (name := named(callee)) in names:
                        line = int(callee.start_point[0]) + 1
                        uses.calls[name] = min(uses.calls.get(name, line), line)
                        taken.add((int(callee.start_byte), int(callee.end_byte)))
                        place(callee, name)
                    break
        if kind in tag_rules._IDENTIFIER_KINDS and (name := named(node)) in names:
            span = (int(node.start_byte), int(node.end_byte))
            if span not in taken:
                line = int(node.start_point[0]) + 1
                uses.refs[name] = min(uses.refs.get(name, line), line)
                place(node, name)
        child_local = in_local or kind in local_scopes
        child_container = kind if is_definition or kind in tag_rules._FUNCTION_EXPRESSION_KINDS else container
        stack.extend((child, child_local, child_container) for child in node.children if spans(child))
    # A file that defines the name is the changed one or makes the name ambiguous, so the definition's own name being
    # counted as a reference there changes nothing.
    definitions, _dropped = _dedupe_definition_tags(candidates, source)
    uses.defs.update(tag.name for tag in definitions)
    return uses


class _NoParser(ValueError):
    """A file the search has no parser for here; its message is the reason its names are not measured."""


def _ambiguous(paths: Sequence[str]) -> str:
    more = f" and {len(paths) - 1} more" if len(paths) > 1 else ""
    return f"the name is also defined in {paths[0]}{more}"


#: Languages whose grammar is on this machine. Only ever grows: a downloaded grammar stays downloaded.
_GRAMMARS_READY: set[str] = set()


def _grammar_ready(language: str) -> bool:
    """Whether *language*'s grammar is already downloaded. The search never downloads one: a download cannot be
    interrupted by the time bound, so a missing grammar makes its files not measured instead."""

    if language in _GRAMMARS_READY:
        return True
    try:
        from tree_sitter_language_pack import downloaded_languages

        from lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast import _LANG_CONFIG
    except ImportError:
        return False
    config = _LANG_CONFIG.get(language)
    if ((config.parser_name if config else "") or language) in set(downloaded_languages()):
        _GRAMMARS_READY.add(language)
        return True
    return False


def _uses(text: str, path: str, names: frozenset[str], lines: Sequence[int]) -> _Uses:
    language = _language(path)
    if language == "python":
        return _python_uses(text, names, lines)
    parsers = tree_sitter_languages()
    if parsers is not None and language in parsers and not _grammar_ready(language):
        raise _NoParser(f"the {language} grammar is not downloaded here")
    found = _tree_uses(text, path, language, names, lines) if parsers is not None and language in parsers else None
    if found is not None:
        return found
    if language not in _LEGACY_REGEX_LANGUAGES:
        # No grammar here and no regex reader: an empty answer would read as "no callers".
        raise _NoParser(f"no parser for {language}")
    return _tag_uses(text, path, language, names)


def _killer(process: subprocess.Popen[bytes], seconds: float) -> threading.Timer:
    timer = threading.Timer(max(seconds, 0.0), process.kill)
    timer.daemon = True
    timer.start()
    return timer


@dataclass(slots=True)
class _Mentions:
    """What the grep saw: the files that mention each name, and the files a line of which plainly defines it."""

    files: dict[str, set[str]] = field(default_factory=dict)
    defined: dict[str, set[str]] = field(default_factory=dict)
    #: The lines of each file that name one of them.
    lines: dict[str, list[int]] = field(default_factory=dict)


#: A line that plainly defines ``{name}``, per language family. A first pass only: two files that each have one make
#: the name ambiguous without parsing either; anything these miss the parse finds.
_DEFINES: Final[Mapping[str, str]] = {
    "python": r"^\s*(?:async\s+)?def\s+{name}\b|^\s*class\s+{name}\b|^{name}\s*(?::[^=]*)?=(?!=)",
    # ``type X =`` / ``type X<T> =`` only: ``import { type X }`` is an import, not a definition.
    "javascript": r"\b(?:function\*?|class|interface|enum|const|let|var)\s+{name}(?![\w$])|\btype\s+{name}\s*[<=]",
    "go": r"\bfunc\s+(?:\([^)]*\)\s*)?{name}\b|\b(?:type|var|const)\s+{name}\b",
    "rust": r"\b(?:fn|struct|enum|trait|type|const|static|mod)\s+{name}\b",
}
_DEFINES_ANY: Final[str] = r"\b(?:class|interface|struct|enum|trait|def|fn|func|function|fun|sub|module)\s+{name}\b"


@functools.lru_cache(maxsize=4096)
def _defines(family: str, name: str) -> re.Pattern[str]:
    return re.compile(_DEFINES.get(family, _DEFINES_ANY).replace("{name}", re.escape(name)))


def _grep(root: Path, rev: str, symbols: Sequence[_Symbol], bounds: Bounds, left: float) -> tuple[_Mentions, str]:
    """Every line at *rev* that names one of *names* as a whole word, or ``(empty, why)`` past a bound or on a failure.

    One Perl-compatible alternation: git's ``-w -F`` with many patterns takes tens of seconds on a large tree where
    this takes a fraction of one. A git built without PCRE gets the POSIX form, a few times slower.
    """

    names = sorted({symbol.short for symbol in symbols})
    specs = _pathspecs(frozenset(symbol.family for symbol in symbols))
    alternation = "|".join(name.replace("$", "\\$") for name in names)
    deadline = time.monotonic() + left
    perl = ["-P", f"(?<![\\w$])(?:{alternation})(?![\\w$])"]
    found, failed = _grep_once(root, rev, perl, specs, bounds, deadline)
    if failed == "exit 128":
        # Spelled out: ``\b`` is not in every platform's POSIX regex (macOS matches nothing with it).
        posix = ["-E", f"(^|[^[:alnum:]_$])({alternation})($|[^[:alnum:]_$])"]
        found, failed = _grep_once(root, rev, posix, specs, bounds, deadline)
    if failed.startswith("exit "):
        failed = f"the search could not run (git grep {failed})"
    if failed:
        return _Mentions(), failed
    word = re.compile("(?<![\\w$])(?:" + "|".join(re.escape(name) for name in names) + ")(?![\\w$])")
    prefix = f"{rev}:" if rev else ""
    mentions = _Mentions()
    families: dict[bytes, tuple[str, str]] = {}
    for line in found.split(b"\n"):
        parts = line.split(b"\0", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        known = families.get(parts[0])
        if known is None:
            path = parts[0].decode("utf-8", errors="surrogateescape").removeprefix(prefix)
            known = families[parts[0]] = (path, _family(_language(path)))
        path, family = known
        text = parts[2].decode("utf-8", errors="replace")
        mentions.lines.setdefault(path, []).append(int(parts[1]))
        for name in set(word.findall(text)):
            mentions.files.setdefault(name, set()).add(path)
            if _defines(family, name).search(text):
                mentions.defined.setdefault(name, set()).add(path)
    return mentions, ""


def _grep_once(
    root: Path, rev: str, pattern: Sequence[str], specs: Sequence[str], bounds: Bounds, deadline: float
) -> tuple[bytes, str]:
    # Options before the pattern: after it, git reads ``--untracked`` as a revision and exits 128.
    command = ["git", "-C", str(root), "-c", "core.quotepath=false", "grep", "-n", "-z", "-I"]
    command += [*([] if rev else ["--untracked"]), *pattern, *([rev] if rev else []), "--", *specs, *_EXCLUDES]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return b"", f"the search could not run ({type(exc).__name__})"
    timer = _killer(process, deadline - time.monotonic())
    chunks: list[bytes] = []
    size = 0
    over = False
    try:
        assert process.stdout is not None
        while chunk := process.stdout.read(1 << 16):
            size += len(chunk)
            if size > bounds.grep_bytes:
                over = True
                process.kill()
                break
            chunks.append(chunk)
    finally:
        timer.cancel()
        code = process.wait()
        if process.stdout is not None:
            process.stdout.close()
    if over:
        return b"", f"the lines that name them run past the {_size(bounds.grep_bytes)} search-output bound"
    if code not in (0, 1):
        if time.monotonic() >= deadline:
            return b"", f"the search stopped at its {bounds.seconds:g} s bound"
        return b"", f"exit {code}"
    return b"".join(chunks), ""


def _blobs(root: Path, rev: str, paths: Sequence[str], left: float) -> Generator[tuple[str, bytes | None]]:
    """Each path's bytes at *rev* (the working tree when *rev* is empty), in order; ``None`` for one not there.

    One ``git cat-file --batch`` reads them all; it is killed when the time left runs out, which ends the iteration.
    """

    if not rev:
        for path in paths:
            try:
                yield path, (root / path).read_bytes()
            except OSError:
                yield path, None
        return
    process = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    timer = _killer(process, left)

    def feed(stdin: IO[bytes]) -> None:
        try:
            for path in paths:
                stdin.write(f"{rev}:{path}\n".encode("utf-8", errors="surrogateescape"))
            stdin.close()
        except (OSError, ValueError):
            pass

    assert process.stdin is not None and process.stdout is not None
    writer = threading.Thread(target=feed, args=(process.stdin,), daemon=True)
    writer.start()
    try:
        for path in paths:
            header = process.stdout.readline().split()
            if len(header) < 2:
                return
            if header[1] != b"blob" or len(header) != 3:
                yield path, None
                continue
            body = process.stdout.read(int(header[2]))
            process.stdout.read(1)
            if len(body) != int(header[2]):
                return
            yield path, body
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        writer.join(timeout=1.0)
        with contextlib.suppress(OSError, ValueError):
            process.stdin.close()
        process.stdout.close()


def _symbols(packet: Mapping[str, Any]) -> tuple[list[_Symbol], dict[tuple[str, str], str]]:
    """The changed symbols to search, and those that cannot be, with why."""

    out: dict[tuple[str, str], _Symbol] = {}
    unmeasured: dict[tuple[str, str], str] = {}
    for raw in packet.get("symbols") or ():
        if not isinstance(raw, Mapping) or str(raw.get("change") or "") == "deleted":
            continue
        path, name = str(raw.get("file_path") or ""), str(raw.get("symbol_name") or "")
        if not path or not name or (path, name) in out:
            continue
        language = _language(path)
        if language in _NOT_CODE:
            continue
        short = name.rsplit(".", 1)[-1]
        if not _searchable(language) or not _NAME.match(short):
            unmeasured[(path, name)] = f"{language or PurePosixPath(path).suffix or 'its'} files are not searched"
            continue
        out[(path, name)] = _Symbol(path, name, short, _family(language))
    return list(out.values()), unmeasured


def _rows(packet: Mapping[str, Any]) -> dict[tuple[str, str], list[int]]:
    """The first line of every packet row of each changed ``(path, name)``: a name may be bound more than once."""

    rows: dict[tuple[str, str], list[int]] = {}
    for raw in packet.get("symbols") or ():
        if not isinstance(raw, Mapping):
            continue
        key = (str(raw.get("file_path") or ""), str(raw.get("symbol_name") or ""))
        start = raw.get("start_line")
        rows.setdefault(key, []).append(start if isinstance(start, int) and start > 0 else 0)
    return rows


#: JavaScript and TypeScript nodes a binding inside of is local to one function: no other file can name it. A function's
#: own name binds outside it, and is not.
_JS_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "function",
        "generator_function",
        "arrow_function",
        "method_definition",
        "class_static_block",
        "catch_clause",
    }
)
_JS_NAMES: Final[frozenset[str]] = frozenset(
    {"identifier", "type_identifier", "shorthand_property_identifier_pattern", "property_identifier"}
)
#: A namespace, ``declare global``/``declare module`` or a parse error: what is inside may be visible anywhere.
_JS_OPEN: Final[frozenset[str]] = frozenset({"ERROR", "ambient_declaration", "internal_module", "module"})
#: Top-level statements whose binding an ES module keeps to itself unless it exports it.
_JS_DECLARATIONS: Final[frozenset[str]] = frozenset(
    {
        "lexical_declaration",
        "variable_declaration",
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    }
)


def _js_hidden(root: Any, source: bytes, name: str, row: int) -> bool:
    """Whether the binding of *name* the packet places at 0-based *row* is an import or one no other file can name: a
    function's local, a callback's parameter, or a top-level declaration an ES module does not export. ``False`` when
    it is not found or anything is in doubt."""

    wanted = name.encode("utf-8")
    target = None
    stack = [root]
    while stack:
        node = stack.pop()
        start = int(node.start_point[0])
        if row <= start <= row + _NAME_ROWS and str(node.type) in _JS_NAMES:
            if source[node.start_byte : node.end_byte] == wanted:
                target = node
                break
        near = [
            child
            for child in node.children
            if int(child.end_point[0]) >= row and int(child.start_point[0]) <= row + _NAME_ROWS
        ]
        stack.extend(reversed(near))
    if target is None:
        return False
    member = str(target.type) == "property_identifier"  # a member: visible through its object, unless in a body
    in_body = False
    child, parent = target, target.parent
    while parent is not None and str(parent.type) != "program":
        kind = str(parent.type)
        if kind in _JS_OPEN:
            return False
        if kind == "import_statement":
            return True
        in_body = in_body or kind == "statement_block"
        if kind in _JS_FUNCTIONS and (in_body or not member):
            own = parent.child_by_field_name("name")
            if own is None or (own.start_byte, own.end_byte) != (target.start_byte, target.end_byte):
                return True
        child, parent = parent, parent.parent
    if parent is None or member or str(child.type) not in _JS_DECLARATIONS:
        return False
    tops = list(root.children)
    if not any(str(top.type) in ("import_statement", "export_statement") for top in tops):
        return False  # a script: its top-level names are global
    if _COMMONJS.search(source):
        return False  # ``module.exports`` / ``exports.x`` can export it by name
    # ``export { name }``, ``export default name``, ``export = name``: an export that is not itself a declaration (``export
    # function other() { name() }`` exports ``other``) and names it anywhere is taken to export it.
    word = re.compile(rb"(?<![\w$])" + re.escape(wanted) + rb"(?![\w$])")
    return not any(
        str(top.type) == "export_statement"
        and top.child_by_field_name("declaration") is None
        and word.search(source[top.start_byte : top.end_byte])
        for top in tops
    )


_COMMONJS: Final[re.Pattern[bytes]] = re.compile(rb"(?<![\w$.])(?:module\.)?exports\s*[.\[=]")


def _python_bindings(text: str, names: frozenset[str]) -> dict[str, list[tuple[int, bool]]]:
    """Each of *names*' bindings in *text* as ``(line, hidden)``, in line order: hidden is an import, or a binding
    inside a function (its locals, a nested function or class)."""

    found: dict[str, list[tuple[int, bool]]] = {}
    stack: list[tuple[ast.AST, bool]] = [(ast.parse(text), False)]
    while stack:
        node, local = stack.pop()
        bound: list[tuple[str, bool]] = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.append((node.name, local))
        elif isinstance(node, ast.Import | ast.ImportFrom):
            bound.extend(((alias.asname or alias.name.split(".")[0]), True) for alias in node.names)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound.extend((target.id, local) for target in targets if isinstance(target, ast.Name))
        for name, hidden in bound:
            if name in names:
                found.setdefault(name, []).append((int(getattr(node, "lineno", 0)), hidden))
        inner = local or isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
        stack.extend((child, inner) for child in ast.iter_child_nodes(node))
    for bindings in found.values():
        bindings.sort()
    return found


def _python_row_hidden(bindings: Sequence[tuple[int, bool]], row: int) -> bool:
    """The first binding at 1-based *row* or in the few rows after it (decorators) is hidden; ``False`` with none."""

    near = [hidden for line, hidden in bindings if row <= line <= row + _NAME_ROWS]
    return bool(near) and near[0]


def _js_grammar(path: str, language: str) -> str:
    return "tsx" if path.lower().endswith(".tsx") else language


def _hidden(
    repo_root: Path,
    packet: Mapping[str, Any],
    symbols: Sequence[_Symbol],
    bounds: Bounds,
    started: float,
) -> set[_Symbol]:
    """The changed symbols no other file can call: every row of each is an import, a function's local or parameter,
    or (JavaScript, TypeScript) a top-level name an ES module does not export. They need no caller search, so their
    callers are never "not measured". Fail-safe: a file over the size bound, one that does not parse, one with no
    grammar here, a row not found, or the time bound reached keeps the symbol.
    """

    rows = _rows(packet)
    by_path: dict[str, list[_Symbol]] = {}
    for symbol in symbols:
        if symbol.family in ("python", "javascript") and all(rows.get((symbol.path, symbol.name)) or [0]):
            by_path.setdefault(symbol.path, []).append(symbol)
    if not by_path:
        return set()

    def left() -> float:
        return bounds.seconds - (time.monotonic() - started)

    hidden: set[_Symbol] = set()
    stream = _blobs(repo_root, str(packet.get("head_sha") or ""), sorted(by_path), left())
    try:
        for path, body in stream:
            if left() <= 0:
                break
            if body is None or len(body) > bounds.file_bytes:
                continue
            try:
                hidden.update(_hidden_in(path, body, by_path[path], rows))
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                continue
    finally:
        stream.close()
    return hidden


def _hidden_in(
    path: str, body: bytes, symbols: Sequence[_Symbol], rows: Mapping[tuple[str, str], list[int]]
) -> list[_Symbol]:
    """Which of *symbols*, all in *path*, are hidden at every row the packet gives them (one parse of the file)."""

    language = _language(path)
    if language == "python":
        bindings = _python_bindings(body.decode("utf-8", errors="replace"), frozenset(item.short for item in symbols))
        return [
            symbol
            for symbol in symbols
            if all(
                _python_row_hidden(bindings.get(symbol.short, ()), line) for line in rows[(symbol.path, symbol.name)]
            )
        ]
    from lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast import tree_sitter_parser

    grammar = _js_grammar(path, language)
    parser = tree_sitter_parser(grammar) if _grammar_ready(grammar) else None  # never a download mid-review
    if parser is None:
        return []
    root = parser.parse(body).root_node
    return [
        symbol
        for symbol in symbols
        if all(_js_hidden(root, body, symbol.short, line - 1) for line in rows[(symbol.path, symbol.name)])
    ]


def _size(count: int) -> str:
    for unit, shift in (("MB", 20), ("KB", 10)):
        if count >= 1 << shift:
            return f"{count / (1 << shift):g} {unit}"
    return f"{count} bytes"


def checkout_callers(repo_root: Path, packet: Mapping[str, Any], *, bounds: Bounds = DEFAULT_BOUNDS) -> dict[str, Any]:
    """``file_relationships``-shaped callers of the packet's changed symbols at its head commit. Never raises.

    ``edges`` are ``calls`` or ``references`` rows from a file outside the change to the changed file, one per caller
    file and name, with the first line of the use; ``unmeasured`` is ``[{path, symbol, reason}]`` for every symbol the
    search could not finish. Deterministic for one commit and one set of bounds that none of the searches hit.
    """

    started = time.monotonic()
    symbols, unmeasured = _symbols(packet)
    try:
        hidden = _hidden(repo_root, packet, symbols, bounds, started)
    except Exception:  # a scope that cannot be read keeps every symbol searched (fail-safe)
        hidden = set()
    symbols = [symbol for symbol in symbols if symbol not in hidden]
    symbols.sort(key=lambda item: (item.path, item.name))
    for symbol in symbols[bounds.symbols :]:
        unmeasured[(symbol.path, symbol.name)] = f"past the {bounds.symbols}-symbol bound"
    symbols = symbols[: bounds.symbols]
    edges: list[dict[str, Any]] = []
    parsed: dict[str, _Uses | str] = {}
    if symbols:
        try:
            edges = _search(repo_root, packet, symbols, unmeasured, parsed, bounds, started)
        except Exception as exc:  # a search that fails measures nothing; it never fails the review
            unmeasured.update(
                {(item.path, item.name): f"the search could not run ({type(exc).__name__})" for item in symbols}
            )
            edges = []
    return {
        "source": SOURCE,
        "exactness": "snapshot" if packet.get("head_sha") else "current_index",
        "edges": edges,
        "degraded": False,
        "truncated": bool(unmeasured),
        "unmeasured": [
            {"path": path, "symbol": name, "reason": reason} for (path, name), reason in sorted(unmeasured.items())
        ],
        "files_read": sum(1 for value in parsed.values() if isinstance(value, _Uses)),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


_IMPORT_FAMILIES: Final[frozenset[str]] = frozenset({"python", "javascript"})
_DYNAMIC_IMPORT: Final[Mapping[str, re.Pattern[str]]] = {
    "python": re.compile(r"\b(?:importlib|import_module|__import__|spec_from_file_location)\b"),
    "javascript": re.compile(r"\b(?:import|require)\(\s*[^'\"\s]"),
}
_ES_MODULE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:import|export)\b", re.MULTILINE)


def _top_level(family: str, name: str) -> re.Pattern[str]:
    word = re.escape(name)
    if family == "python":
        return re.compile(rf"^(?:(?:async\s+)?def\s+{word}\b|class\s+{word}\b|{word}\s*(?::[^=\n]*)?=(?!=))", re.M)
    return re.compile(
        r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
        rf"(?:function\*?|class|interface|enum|const|let|var|type)\s+{word}(?![\w$])",
        re.M,
    )


class _ImportGate:
    """Whether a file that spells a changed name is tied to the changed file (Python, JavaScript, TypeScript).

    A name alone proves nothing: ``pages = 0`` in another file is not a use of a method called ``pages``. A module-level
    name has to be imported; a member is used through an object, so the file has to import the defining module. A use
    that may still be the symbol (``obj.pages()`` with no such import, a dynamic import, an unresolved module path) is
    ``unknown``: its symbol reads as not measured, never as having no callers.
    """

    def __init__(
        self,
        root: Path,
        rev: str,
        symbols: Sequence[_Symbol],
        texts: dict[str, str],
        bounds: Bounds,
        left: Callable[[], float],
    ) -> None:
        self._root, self._rev, self._texts, self._bounds, self._left = root, rev, texts, bounds, left
        self._exhausted = False
        definitions: dict[str, set[str]] = {}
        for symbol in symbols:
            definitions.setdefault(symbol.short, set()).add(symbol.path)
        paths = self._tracked()
        self._resolver = None if paths is None else ImportResolver(root, paths, definitions, read=self._read)

    def _git(self, *args: str) -> bytes | None:
        seconds = self._left()
        if seconds <= 0:
            self._exhausted = True
            return None
        try:
            done = subprocess.run(
                ["git", "-C", str(self._root), *args], capture_output=True, timeout=seconds, check=False
            )
        except (OSError, subprocess.SubprocessError):
            self._exhausted = True
            return None
        return done.stdout if done.returncode == 0 else None

    def _tracked(self) -> set[str] | None:
        args = ("ls-tree", "-r", "--name-only", "-z", self._rev) if self._rev else ("ls-files", "-z")
        listed = self._git(*args)
        if listed is None:
            return None
        return {item.decode("utf-8", errors="surrogateescape") for item in listed.split(b"\0") if item}

    def _read(self, path: str) -> str:
        known = self._texts.get(path)
        if known is not None:
            return known
        body: bytes | None
        if self._rev:
            body = self._git("show", f"{self._rev}:{path}")
        else:
            try:
                body = (self._root / path).read_bytes()
            except OSError:
                body = None
        text = "" if body is None or len(body) > self._bounds.file_bytes else body.decode("utf-8", errors="replace")
        self._texts[path] = text
        return text

    def verdict(self, symbol: _Symbol, path: str, uses: _Uses) -> str:
        """``caller``, ``unrelated`` or ``unknown`` for one file that uses *symbol*'s name."""

        short = symbol.short
        placed = short in uses.bare or short in uses.member or short in uses.own
        if self._resolver is None or symbol.family not in _IMPORT_FAMILIES or not placed:
            return "caller"
        answer = self._judge(symbol, path, uses, self._resolver)
        return "unknown" if answer == "unrelated" and self._exhausted else answer

    def _judge(self, symbol: _Symbol, path: str, uses: _Uses, resolver: ImportResolver) -> str:
        short = symbol.short
        definer = self._read(symbol.path)
        if not definer:
            return "caller"
        if symbol.name == short and _top_level(symbol.family, short).search(definer):
            if symbol.family == "javascript" and not (_ES_MODULE.search(definer) or _COMMONJS.search(definer.encode())):
                return "caller"  # a script: its top-level names are global
            if symbol.path in (resolver.targets(path, short) or ()):
                return "caller"
            dynamic = _DYNAMIC_IMPORT[symbol.family].search(self._read(path))
            return "unknown" if dynamic or resolver.binding_unresolved(path, short) else "unrelated"
        if resolver.reaches(path, symbol.path):
            return "caller" if short in uses.member or short in uses.own else "unrelated"
        return "unknown" if short in uses.member else "unrelated"


def _search(
    repo_root: Path,
    packet: Mapping[str, Any],
    symbols: Sequence[_Symbol],
    unmeasured: dict[tuple[str, str], str],
    parsed: dict[str, _Uses | str],
    bounds: Bounds,
    started: float,
) -> list[dict[str, Any]]:
    """The caller edges of *symbols*; what it cannot finish goes into *unmeasured*, each file it reads into *parsed*."""

    def left() -> float:
        return bounds.seconds - (time.monotonic() - started)

    rev = str(packet.get("head_sha") or "")
    changed = {str(item.get("path") or "") for item in packet.get("files") or () if isinstance(item, Mapping)}
    mentions, failed = _grep(repo_root, rev, symbols, bounds, left())
    if failed:
        unmeasured.update({(symbol.path, symbol.name): failed for symbol in symbols})
        return []

    def same_family(paths: set[str] | None, symbol: _Symbol) -> list[str]:
        return sorted(path for path in paths or () if _family(_language(path)) == symbol.family)

    candidates = {symbol: same_family(mentions.files.get(symbol.short), symbol) for symbol in symbols}
    wanted: dict[str, set[str]] = {}
    planned: list[_Symbol] = []
    for symbol in sorted(symbols, key=lambda item: (len(candidates[item]), item.path, item.name)):
        files = candidates[symbol]
        also = [path for path in same_family(mentions.defined.get(symbol.short), symbol) if path != symbol.path]
        if also:
            # Plainly defined in another file too: a use does not say which one it calls, so its callers are not
            # measured (never "no callers", which would let the Risk read Low).
            unmeasured[(symbol.path, symbol.name)] = _ambiguous(also)
            continue
        if len(files) > bounds.files_per_name:
            unmeasured[(symbol.path, symbol.name)] = (
                f"{len(files)} files mention it, past the {bounds.files_per_name}-file bound"
            )
            continue
        planned.append(symbol)
        for path in files:
            wanted.setdefault(path, set()).add(symbol.short)
    # Files in the order the planned symbols need them, so the bounds cut the symbols with the most mentions.
    order = list(dict.fromkeys(path for symbol in planned for path in candidates[symbol]))
    stop = ""
    read = 0
    texts: dict[str, str] = {}
    stream = _blobs(repo_root, rev, order[: bounds.files], left())
    try:
        for path, body in stream:
            if left() <= 0:
                stop = f"the search stopped at its {bounds.seconds:g} s bound"
                break
            if body is not None and len(body) > bounds.file_bytes:
                parsed[path] = f"a file that mentions it is over {_size(bounds.file_bytes)}: {path}"
                continue
            read += len(body or b"")
            if read > bounds.total_bytes:
                stop = f"past the {_size(bounds.total_bytes)} read bound"
                break
            try:
                text = (body or b"").decode("utf-8", errors="replace")
                parsed[path] = _uses(text, path, frozenset(wanted[path]), mentions.lines.get(path, ()))
                if _family(_language(path)) in _IMPORT_FAMILIES:
                    texts[path] = text
            except _NoParser as exc:
                parsed[path] = f"{exc}: {path}"
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                parsed[path] = f"a file that mentions it could not be parsed: {path}"
        else:
            if len(order) > bounds.files:
                stop = f"past the {bounds.files}-file bound"
    finally:
        stream.close()
    if not stop and len(parsed) < min(len(order), bounds.files):
        # The reader stopped early: git was killed at the deadline.
        stop = f"the search stopped at its {bounds.seconds:g} s bound"
    gate = _ImportGate(repo_root, rev, planned, texts, bounds, left)
    edges: list[dict[str, Any]] = []
    for symbol in planned:
        files = candidates[symbol]
        found = {path: uses for path in files if isinstance(uses := parsed.get(path), _Uses)}
        also = sorted(path for path, uses in found.items() if path != symbol.path and symbol.short in uses.defs)
        if also:
            # Defined in another file too: the name does not say which one a use calls. Not measured.
            unmeasured[(symbol.path, symbol.name)] = _ambiguous(also)
            continue
        gaps = [parsed.get(path, stop) for path in files if path not in found]
        if gaps:
            # Not measured, said so; the callers found before the bound are still callers.
            unmeasured[(symbol.path, symbol.name)] = str(gaps[0])
        strangers = 0
        for path, uses in sorted(found.items()):
            line = uses.calls.get(symbol.short) or uses.refs.get(symbol.short)
            if path in changed or line is None:
                continue
            verdict = gate.verdict(symbol, path, uses)
            if verdict != "caller":
                strangers += verdict == "unknown"
                continue
            edges.append(
                {
                    "source_path": path,
                    "target_path": symbol.path,
                    "kind": "calls" if symbol.short in uses.calls else "references",
                    "symbols": [{"name": symbol.name, "count": 1}],
                    "evidence": [{"path": path, "line": line, "symbol": symbol.name}],
                }
            )
        if strangers:
            # Not callers and not "no callers": the use may be this symbol, reached in a way imports do not show.
            unmeasured.setdefault(
                (symbol.path, symbol.name),
                f"{strangers} file{'s' if strangers != 1 else ''} use the name without an import that ties it here",
            )
    return edges


__all__ = ["DEFAULT_BOUNDS", "SOURCE", "Bounds", "checkout_callers"]
