"""Which config, manifest or script files at a Snapshot's head name a function (P12-4 and P13-1, SPEC 7.1 OWNER-19, OWNER-22).

A function that only a file other than its own source looks up (a Lambda ``handler: app.old_name``, a
``[project.scripts]`` entry ``pkg.mod:old_name``, an ``npm`` script, a ``Procfile`` command, a Terraform ``handler =``,
a ``.flaskenv``, a Makefile or shell line, a systemd ``ExecStart=``, a CDK stack in another language) breaks at runtime
when it is renamed, and nothing in the diff of the renamed file shows it. ``since_review`` therefore reads the head's
files once per request and refuses to batch a rename whose old name any of them contains.

What is searched is a denylist, so that a file type nobody listed is still read: every regular, non-binary text file
at the head except (a) generated lockfiles and (b) prose docs (Markdown, reStructuredText, AsciiDoc), and in a source
file in the same language as the renamed function's file (a Python importer of a Python function: the P11-2
``lc-debt`` in ``since_review``) only the qualified name counts, as below. Binary is a known binary extension, or (only for a file larger than the per-file bound,
which would otherwise refuse) a file whose first 8 KiB has a NUL byte and no UTF-16 or UTF-32 byte-order mark, is not
shaped like UTF-16, is dense in control bytes and holds none of the searched names in any decoding. A file with a BOM is decoded by it; a file with
NUL bytes and no BOM is searched as UTF-8, UTF-16-LE and UTF-16-BE (a false match in a binary file can only refuse a
rename).

The rule is deliberately blunt and errs toward reading: a whole word equal to the bare name, anywhere in a searched
file, counts. ``module.func`` and ``module:func`` contain the bare name between non-word characters, so they match; a
name in a comment or in prose inside the file matches too.

Source files in the renamed function's own language are the exception to the bare name (P13-1): a Python file that
imports the function and calls it names it bare, and that is every importer, so the bare name there would refuse almost
every rename. They are searched for the qualified name only: the module stem of the renamed function's file, then ``.``,
``::`` or ``:``, then the name, as whole words. The stem is the file name without its extension (``__init__.*`` and
Rust's ``mod.rs`` take the directory's name; for Go the directory's name is a stem too, because a Go caller qualifies by
package), so ``app.views.old_name`` (a Django settings string, a Celery beat entry), ``index.old_name`` (a CDK
``handler=``), ``pkg.mod:old_name``, ``src/handlers.old_name``, Rust's ``b::old_name`` and Go's ``util.Alpha`` from
``util/strings.go`` all match, and ``other.old_name`` does not. Around the dot (and only there) white space, a line
break and a backslash continuation are allowed, as Python allows them: ``b .old_name``, ``(b`` then a line break and
``.old_name)``, ``b.`` then a backslash, a line break and ``old_name``. A colon takes no gap, because ``views: old_name`` is an
annotation. The stems of the old and the new path of the renamed file are both searched. A function whose file has no
stem (an ``__init__.py`` or ``mod.rs`` at the top of the tree) has no qualified form to search, so its same-language
files stay unsearched.

Fail closed: a search that cannot finish (a file, the total, the time or the head itself is past its bound or cannot be
read) is reported as incomplete, and its caller refuses every candidate name.

One walk per Snapshot head at a time, and each answer is kept (:class:`HeadSearchMemo`): requests that arrive together
share the walk, and a later request that brings more names walks only for the names not yet answered. An answer is kept
for what was searched: the name, its language families and, for a name whose own-language files are searched by the
qualified form, its module stems (``a.f`` and ``b.f`` are two questions). A walk that runs out of time is retried once;
after a second timeout for the same head, the refusal is kept for that Snapshot.

Reading same-language source spends the same bounds as any other file (20,000 files, 8 MiB for a file, 128 MiB in all,
10 seconds) and an incomplete search refuses as before, so a head with a text source file past the per-file bound (a
committed bundle, generated code) refuses every rename of a function in that language.

Limits: a symlink (mode 120000) is skipped, because the file it points to is walked on its own, and a submodule's
contents are not walked (a gitlink is a pointer, not a file). A text file over the per-file bound refuses the search
(a bundle or a data file of text), while a binary one is skipped. An oversized file is judged by its first 8 KiB only:
one that is UTF-32 with no mark, or UTF-16 that is mostly not ASCII and names the function only past those bytes, is
skipped. A same-language file is searched for the qualified name only, so a bare name in it is never found: ``getattr(mod,
"old_name")``, ``from mod import old_name`` and a plain call stay unsearched, and so does a name qualified by something
other than the file's stem (a Go package named differently from its directory, a Java class named differently from the
file, ``mod.py:old_name``, a package re-export under the directory's name, a Rust crate root reached as ``crate::name``).
A qualified name that is built or spelled another way is not found either: string concatenation (``"app.b." "old_name"``,
``"app.b." + "old_name"``), an f-string, an import alias (``import src.b as bb; bb.old_name()``), ``"src/b.py::old_name"``
and ``"pkg.__init__.old_name"``.
"""

from __future__ import annotations

import codecs
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import ReviewRevision
    from lemoncrow.pro.capabilities.review.store import ReviewStore

# Generated, and they name packages, never a function of this repository. Without this a large lockfile would pass the
# per-file bound and make every rename in its repository unbatchable. Exact base names, in lower case.
LOCKFILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "cargo.lock",
        "gemfile.lock",
        "composer.lock",
        "go.sum",
        "uv.lock",
        "pipfile.lock",
    }
)
PROSE_SUFFIXES = frozenset({".md", ".markdown", ".mdown", ".rst", ".adoc", ".asciidoc"})
# Never worth reading (and some would be large): images, office files, archives, compiled code, fonts, media, data.
BINARY_SUFFIXES = frozenset(
    ".png .jpg .jpeg .gif .bmp .ico .icns .webp .tif .tiff .psd .avif .heic .pdf .doc .docx .xls .xlsx .ppt .pptx "
    ".zip .gz .tgz .bz2 .xz .zst .7z .rar .tar .jar .war .ear .whl .egg .class .so .dylib .dll .exe .o .a .lib .obj "
    ".pyc .pyo .wasm .bin .node .woff .woff2 .ttf .otf .eot .mp3 .mp4 .mov .avi .wav .ogg .webm .flac .mkv "
    ".sqlite .sqlite3 .db .parquet .pkl .pickle .npy .npz .onnx .pt .pth .h5 .hdf5 .safetensors .gguf .ggml .ckpt .pb .tflite "
    ".mlmodel .caffemodel .tfrecord .arrow .feather .orc .avro .joblib .mat .rds .rdata .lmdb .mdb .iso .dmg .pkg .deb "
    ".rpm .msi .apk .ipa .aab .dex .jks .keystore .p12 .pfx .mo .glb .blend .fbx .swf .pack".split()
)
# Source files by language. JavaScript and TypeScript call each other, so they are one family. A file in the same family
# as the renamed function's file is searched for the qualified name only (P11-2 ``lc-debt`` in ``since_review``, P13-1).
SOURCE_FAMILIES: Mapping[str, str] = {
    **dict.fromkeys((".py", ".pyi", ".pyw"), "python"),
    **dict.fromkeys((".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx"), "javascript"),
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}
# Python files that configure a tool and name functions as strings: searched whatever the renamed function's language.
_CONFIG_SOURCES = frozenset({"setup.py", "gunicorn.conf.py", "gunicorn_conf.py"})

# Bounds. Every one of them refuses (the caller reads the part); none silently skips a file.
# lc-debt: flat ceilings sized for a large monorepo's text files; upgrade path: a per-Snapshot index of file tokens kept
# beside the revision, so a request reads a set instead of walking the tree.
# lc-debt: same-language source spends these bounds too (P13-1). A Python rename on this repository's head reads about
# 82 MB of the 128 MiB, so a repository about 1.6 times this one's text, or a same-language text file over 8 MiB (a
# bundle, generated code), refuses every rename in that language. Upgrade path: search an oversized same-language text
# file in chunks, the length of the pattern as the overlap; test the raw bytes for the name before decoding a file; or
# build the per-Snapshot token index above.
MAX_CONFIG_FILES = 20000
MAX_CONFIG_FILE_BYTES = 8 * 1024 * 1024
MAX_CONFIG_BYTES = 128 * 1024 * 1024
MAX_SEARCH_SECONDS = 10.0
_BINARY_SNIFF = 8192


def _whole_words(text: str, names: Collection[str]) -> set[str]:
    """The *names* that *text* holds as a whole word: a substring test first, which almost every file fails."""

    present = [name for name in names if name in text]
    if not present:
        return set()
    pattern = re.compile(
        r"(?<![\w$])(?:" + "|".join(re.escape(name) for name in sorted(present, key=len, reverse=True)) + r")(?![\w$])"
    )
    return {match.group() for match in pattern.finditer(text)}


def is_searched_path(path: str) -> bool:
    """Whether *path* may be searched: not a generated lockfile, a prose doc or a known binary type."""

    pure = PurePosixPath(path)
    name = pure.name.lower()
    suffix = pure.suffix.lower()
    return name not in LOCKFILES and suffix not in PROSE_SUFFIXES and suffix not in BINARY_SUFFIXES


def source_family(path: str) -> str:
    """The language family of a source file, or ``""`` for anything else (and for a Python file that configures a tool)."""

    pure = PurePosixPath(path)
    if pure.name.lower() in _CONFIG_SOURCES:
        return ""
    return SOURCE_FAMILIES.get(pure.suffix.lower(), "")


def module_stem(path: str) -> str:
    """The module stem of a source file: its name without the extension, or its directory's name for a package file.

    A package file is ``__init__.*`` and, in Rust, ``mod.rs`` (``src/b/mod.rs`` is the module ``b``). ``""`` when there
    is none (such a file at the top of the tree). ``lib.rs`` and ``main.rs`` keep their own stems: their directory is
    ``src``, which no caller writes (a crate root is reached as ``crate::name`` or by the crate's name).
    """

    pure = PurePosixPath(path)
    package_file = pure.stem == "__init__" or (pure.stem == "mod" and pure.suffix.lower() == ".rs")
    return pure.parent.name if package_file else pure.stem


def module_stems(path: str) -> frozenset[str]:
    """Every name a source file of the same language may qualify a function of *path* by (never an empty one).

    The module stem, and for Go the directory's name too: a Go caller qualifies by package, which is the directory
    (``util.Alpha`` from ``util/strings.go``), not by the file.
    """

    stems = {module_stem(path)}
    if PurePosixPath(path).suffix.lower() == ".go":
        stems.add(PurePosixPath(path).parent.name)
    stems.discard("")
    return frozenset(stems)


def _only_family(families: Mapping[str, frozenset[str]], name: str) -> str:
    """The one language family *name*'s function lives in, or ``""`` (none known, or defined in several languages).

    Only a name with one family skips the bare name in the source files of that family: a name that lives in two
    languages is searched bare in both.
    """

    own = families.get(name, frozenset())
    return next(iter(own)) if len(own) == 1 else ""


# Whitespace, a line break, or a backslash followed by a line break. Python allows all of them around a dot
# (``b .alpha``, ``(b`` then a line break and ``.alpha)``, ``b.`` then a backslash, a line break and ``alpha``).
_GAP = r"(?:\s|\\\r?\n)*"
# ``.`` (with a gap on either side), ``::`` (Rust) or ``:``. Never a gap around a colon: ``views: alpha`` is an annotation.
_SEPARATOR = rf"(?:{_GAP}\.{_GAP}|::|:)"


def _qualified_pattern(name: str, stems: Collection[str]) -> re.Pattern[str]:
    """A module stem, a separator, then *name*, each as a whole word: ``views.old_name``, ``app:old_name``, ``b::old_name``.

    The separator is ``.`` (a gap of white space, line breaks or backslash continuations allowed around it), ``::`` or
    ``:``. The stem must start at a word boundary, so a dotted or slash path that ends in it (``app.views.old_name``,
    ``src/handlers.old_name``) matches and ``myviews.old_name`` does not.
    """

    stem_part = "|".join(re.escape(stem) for stem in sorted(stems, key=len, reverse=True))
    return re.compile(r"(?<![\w$])(?:" + stem_part + r")" + _SEPARATOR + re.escape(name) + r"(?![\w$])")


_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
    (codecs.BOM_UTF8, "utf-8-sig"),
)


def _has_bom(payload: bytes) -> bool:
    return any(payload.startswith(mark) for mark, _ in _BOMS)


def is_binary_head(head: bytes) -> bool:
    """Whether the start of a file reads as binary: a NUL byte in its first 8 KiB and no byte-order mark."""

    return not _has_bom(head) and b"\0" in head[:_BINARY_SNIFF]


def _looks_utf16(head: bytes) -> bool:
    """NUL bytes at one parity of the offsets and mostly ASCII at the other: UTF-16 text, not a binary format."""

    for parity in (0, 1):
        nuls, ascii_ = head[parity::2], head[1 - parity :: 2]
        if (
            nuls
            and ascii_
            and nuls.count(0) >= 0.6 * len(nuls)
            and sum(b in (9, 10, 13) or 32 <= b < 127 for b in ascii_) >= 0.6 * len(ascii_)
        ):
            return True
    return False


def skippable_oversized(head: bytes, names: Collection[str]) -> bool:
    """Whether a file past the per-file bound may be skipped instead of refusing the search, judged by its first 8 KiB.

    Only when it reads as binary (a NUL, no byte-order mark, and control bytes in at least 5% of it, which text with a
    stray NUL does not have), is not UTF-16-shaped, and none of *names* is in the start of it in any of the three
    decodings. Anything else is text, or may be, and refuses.
    """

    if not is_binary_head(head) or _looks_utf16(head[:_BINARY_SNIFF]):
        return False
    # Text with one stray NUL is text: binary data is full of control bytes.
    if sum(b < 32 and b not in (9, 10, 13) for b in head) < 0.05 * len(head):
        return False
    texts = decode_texts(head[:_BINARY_SNIFF])
    return not any(name in text for name in names for text in texts)


def decode_texts(payload: bytes) -> tuple[str, ...]:
    """The texts of *payload* to search. A byte-order mark decides the encoding before any NUL does.

    A file with NUL bytes and no mark is most likely binary, but could be UTF-16 without a mark (Git itself treats that
    as binary), or UTF-8 text with a stray NUL: it is searched in all three decodings, UTF-8 and UTF-16 in both byte
    orders. A wrong decoding of a real file only adds text to match, so the result can over-refuse and never under-refuse.
    """

    for mark, codec in _BOMS:
        if payload.startswith(mark):
            return (payload.decode(codec, errors="replace"),)
    if b"\0" in payload[:_BINARY_SNIFF]:
        return (
            payload.decode("utf-8", errors="replace"),
            payload.decode("utf-16-le", errors="replace"),
            payload.decode("utf-16-be", errors="replace"),
        )
    return (payload.decode("utf-8", errors="replace"),)


class SearchBound(Exception):
    """The search cannot be finished within its bounds, or the head cannot be read; the message says why."""


class SearchTimeout(SearchBound):
    """The time bound was reached. Unlike the other bounds it depends on load, so its refusal is not remembered."""


@dataclass
class ConfigBudget:
    """What one search may spend. ``tick`` and ``admit`` raise :class:`SearchBound` past a limit; a value *at* it passes."""

    max_files: int = MAX_CONFIG_FILES
    max_file_bytes: int = MAX_CONFIG_FILE_BYTES
    max_bytes: int = MAX_CONFIG_BYTES
    seconds: float = MAX_SEARCH_SECONDS
    clock: Callable[[], float] = time.monotonic
    files: int = field(default=0, init=False)
    bytes: int = field(default=0, init=False)
    _started: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._started = self.clock()

    def tick(self) -> None:
        if self.clock() - self._started > self.seconds:
            raise SearchTimeout(f"the search took longer than {self.seconds:g} seconds")

    def admit(self, path: str, size: int) -> None:
        self.files += 1
        if self.files > self.max_files:
            raise SearchBound(f"there are more than {self.max_files} files to search")
        if size > self.max_file_bytes:
            raise SearchBound(f"{path} is larger than {self.max_file_bytes} bytes")
        self.bytes += size
        if self.bytes > self.max_bytes:
            raise SearchBound(f"the files to search hold more than {self.max_bytes} bytes")


class ConfigFile(NamedTuple):
    """One file at the head: its path, its size in bytes, and a reader that is called only when it is searched."""

    path: str
    size: int
    read: Callable[[], bytes]
    sniff: Callable[[], bytes] | None = None
    """The start of the file (at least 8 KiB), when that is cheaper than ``read``; it decides an oversized file."""


#: Makes the head's config files, spending the budget as it walks (a tree can be large before any file matches).
HeadConfigFiles = Callable[[ConfigBudget], Iterable[ConfigFile]]


@dataclass(frozen=True)
class ConfigSearch:
    """What one search found. ``incomplete`` is non-empty when it stopped early: then no name is cleared."""

    named: Mapping[str, str]
    """name -> the first file that holds it."""
    incomplete: str = ""
    searched: int = 0
    """How many files were read."""
    transient: bool = False
    """The search stopped on the time bound: asking again may finish it."""
    unsearched: frozenset[str] | None = None
    """The names ``incomplete`` applies to (``None``: every name that is not in ``named``)."""


def search_config_names(
    names: Collection[str],
    head_files: HeadConfigFiles,
    budget: ConfigBudget | None = None,
    *,
    families: Mapping[str, frozenset[str]] | None = None,
    modules: Mapping[str, frozenset[str]] | None = None,
) -> ConfigSearch:
    """Every one of *names* that a searched file of *head_files* holds as a whole word, in one pass over the files.

    *families* maps a name to the language families of the files its function lives in, and *modules* to the module
    stems of those files (:func:`module_stems`). In a source file of the only family a name belongs to, the name is
    searched by its qualified form (a stem of *modules*, ``.``, ``::`` or ``:``, the name) instead of bare; with no stem there
    is nothing to search for, and the file is not read for that name. Everywhere else the bare name is searched. The
    search stops early once every name is found. Anything that goes wrong while reading (a bound, an unreadable head,
    an unreadable file) ends it as incomplete, which the caller reads as "nothing is cleared".
    """

    wanted = frozenset(names)
    named: dict[str, str] = {}
    if not wanted:
        return ConfigSearch(named)
    budget = budget if budget is not None else ConfigBudget()
    own = {name: _only_family(families or {}, name) for name in wanted}
    stems = modules or {}
    qualified = {name: _qualified_pattern(name, stems[name]) for name in wanted if stems.get(name)}
    searched = 0
    try:
        for file in head_files(budget):
            budget.tick()
            if not is_searched_path(file.path):
                continue
            family = source_family(file.path)
            bare: set[str] = set()
            by_stem: set[str] = set()
            for name in wanted:
                if name in named:
                    continue
                if not family or own[name] != family:
                    bare.add(name)
                elif name in qualified:
                    by_stem.add(name)
            if not bare and not by_stem:
                continue
            if file.size > budget.max_file_bytes and skippable_oversized(
                (file.sniff or file.read)()[:_BINARY_SNIFF], bare | by_stem
            ):
                budget.admit(file.path, 0)  # a large binary file (model weights, a bundle of data) is not a refusal
                continue
            budget.admit(file.path, file.size)
            searched += 1
            for text in decode_texts(file.read()):
                for token in _whole_words(text, bare):
                    named.setdefault(token, file.path)
                for name in by_stem:
                    if name not in named and name in text and qualified[name].search(text):
                        named[name] = file.path
            if len(named) == len(wanted):
                break
    except SearchBound as stop:
        return ConfigSearch(named, str(stop), searched, isinstance(stop, SearchTimeout))
    except Exception as exc:  # a boundary around git and the store: whatever fails, the head was not fully read
        return ConfigSearch(named, f"the head could not be read ({type(exc).__name__})", searched)
    return ConfigSearch(named, searched=searched)


# ---------------------------------------------------------------- one walk per head, answers kept

#: The search function of one request: :func:`search_config_names`, or a memoizing wrapper of it.
ConfigSearcher = Callable[..., ConfigSearch]

# A walk that ran out of time is asked again once (the load may have passed); the second time the refusal is kept.
MAX_TIMEOUT_WALKS = 2
# lc-debt: a per-process LRU of Snapshot heads with a count cap on the answers of one head; upgrade path: one walk in the
# background when the revision is created, kept beside it, so no request walks the tree at all.
MAX_MEMO_HEADS = 64
MAX_MEMO_ANSWERS = 4096


@dataclass(frozen=True)
class _Answer:
    path: str = ""
    incomplete: str = ""
    searched: int = 0


#: What one answer is about: the name, the families it was searched in and the module stems it was qualified by.
_Key = tuple[str, tuple[str, ...], tuple[str, ...]]


def _answer_key(name: str, families: Mapping[str, frozenset[str]], modules: Mapping[str, frozenset[str]]) -> _Key:
    """The key of *name*'s answer: the stems count only when they were searched, that is for a name of one family."""

    stems = modules.get(name, frozenset()) if _only_family(families, name) else frozenset()
    return (name, tuple(sorted(families.get(name, frozenset()))), tuple(sorted(stems)))


class _HeadState:
    def __init__(self) -> None:
        self.lock = threading.Lock()  # held for one walk of this head only; other heads and other work never wait on it
        self.answers: dict[_Key, _Answer] = {}
        self.timeouts = 0
        self.walks = 0
        self.last: ConfigSearch | None = None
        self.last_keys: frozenset[_Key] = frozenset()


class HeadSearchMemo:
    """Single flight and memory for the searches of Snapshot heads.

    A head is immutable, so what a search learned about a name stays true: a name found, or cleared by a search that
    read every searched file, is answered from memory (per name, language families and module stems, because a name that
    lives only in Python is searched in the Python files by its qualified form, which depends on the file's module stem,
    and one that lives in two languages is searched bare in both). A request that needs names not yet
    answered walks once for those, and requests that arrive for the same head while a walk runs wait for it and share
    its result instead of walking themselves. A deterministic bound (files, bytes) is kept as the answer. A timeout is
    kept only from the :data:`MAX_TIMEOUT_WALKS`-th time on, so a load spike is retried and a head that always runs out
    of time stops costing a walk per request.
    """

    def __init__(self, search: ConfigSearcher | None = None) -> None:
        self._search = search
        self._heads: OrderedDict[str, _HeadState] = OrderedDict()
        self._mutex = threading.Lock()  # guards the dict only

    def _state(self, head: str) -> _HeadState:
        with self._mutex:
            state = self._heads.pop(head, None) or _HeadState()
            self._heads[head] = state
            while len(self._heads) > MAX_MEMO_HEADS:
                self._heads.popitem(last=False)
            return state

    def forget(self, head: str) -> None:
        """Drop what is known about one head (its revision went away)."""

        with self._mutex:
            self._heads.pop(head, None)

    def searcher(self, head: str) -> ConfigSearcher:
        """A function with the signature of :func:`search_config_names` that serves *head* from memory and one walk."""

        def search(
            names: Collection[str],
            head_files: HeadConfigFiles,
            budget: ConfigBudget | None = None,
            *,
            families: Mapping[str, frozenset[str]] | None = None,
            modules: Mapping[str, frozenset[str]] | None = None,
        ) -> ConfigSearch:
            return self._run(head, names, head_files, budget, families or {}, modules or {})

        return search

    def _run(
        self,
        head: str,
        names: Collection[str],
        head_files: HeadConfigFiles,
        budget: ConfigBudget | None,
        families: Mapping[str, frozenset[str]],
        modules: Mapping[str, frozenset[str]],
    ) -> ConfigSearch:
        state = self._state(head)
        keys = {name: _answer_key(name, families, modules) for name in names}
        walks_seen = state.walks
        with state.lock:
            todo = [name for name, key in keys.items() if key not in state.answers]
            result: ConfigSearch | None = None
            if todo:
                wanted = frozenset(keys[name] for name in todo)
                if state.walks != walks_seen and state.last is not None and wanted <= state.last_keys:
                    result = state.last  # a walk that ran while this request waited covered these names
                else:
                    search = self._search or search_config_names
                    result = search(
                        todo,
                        head_files,
                        budget,
                        families={n: families[n] for n in todo if n in families},
                        modules={n: modules[n] for n in todo if n in modules},
                    )
                    state.walks += 1
                    state.last, state.last_keys = result, wanted
                    self._record(state, keys, todo, result)
            return self._answer(state, keys, result)

    @staticmethod
    def _record(state: _HeadState, keys: Mapping[str, _Key], todo: Iterable[str], result: ConfigSearch) -> None:
        if result.transient:
            state.timeouts += 1
        keep_timeout = state.timeouts >= MAX_TIMEOUT_WALKS
        for name in todo:
            if len(state.answers) >= MAX_MEMO_ANSWERS:
                return
            if name in result.named:  # found: true whatever happened to the rest of the walk
                state.answers[keys[name]] = _Answer(path=result.named[name], searched=result.searched)
            elif not result.incomplete:
                state.answers[keys[name]] = _Answer(searched=result.searched)
            elif keep_timeout or not result.transient:
                state.answers[keys[name]] = _Answer(incomplete=result.incomplete, searched=result.searched)

    @staticmethod
    def _answer(state: _HeadState, keys: Mapping[str, _Key], result: ConfigSearch | None) -> ConfigSearch:
        named: dict[str, str] = {}
        unsearched: set[str] = set()
        reasons: list[str] = []
        searched = 0
        transient = False
        for name, key in keys.items():
            answer = state.answers.get(key)
            if answer is None and result is not None:  # not kept: a first timeout, or the answers are full
                answer = (
                    _Answer(path=result.named[name], searched=result.searched)
                    if name in result.named
                    else _Answer(incomplete=result.incomplete, searched=result.searched)
                )
                transient = transient or (bool(answer.incomplete) and result.transient)
            if answer is None:
                answer = _Answer(incomplete="the search was not run")
            if answer.path:
                named[name] = answer.path
            elif answer.incomplete:
                unsearched.add(name)
                reasons.append(answer.incomplete)
            searched = max(searched, answer.searched)
        return ConfigSearch(
            named,
            reasons[0] if reasons else "",
            searched,
            transient,
            frozenset(unsearched) if reasons else None,
        )


# ---------------------------------------------------------------- the Snapshot's head


def _frozen_file(path: str, blobs: Mapping[str, str]) -> ConfigFile:
    """A changed file at its frozen text. A changed file with no frozen text is found out only if it is searched."""

    text = blobs.get(path)

    def read() -> bytes:
        if text is None:
            raise SearchBound(f"the changed file {path} has no frozen text")
        return text.encode("utf-8")

    return ConfigFile(path, 0 if text is None else len(text.encode("utf-8")), read)


def _media_file(path: str, media: Mapping[str, bytes] | None) -> ConfigFile:
    """A changed file that Git calls binary (UTF-16 text is one), at its frozen bytes from the media artifact.

    Bytes that are not there end the search, but only when this file is searched.
    """

    payload = None if media is None else media.get(path)

    def read() -> bytes:
        if payload is None:
            raise SearchBound(f"the changed binary file {path} has no frozen bytes")
        return payload

    return ConfigFile(path, 0 if payload is None else len(payload), read)


def head_config_files(
    store: ReviewStore, revision: ReviewRevision, repo_root: Path, budget: ConfigBudget
) -> Iterator[ConfigFile]:
    """The searchable files of the revision's head: its commit's tree, or its base tree under the frozen changed files.

    The mutable checkout is never read. A commit range's head is the tree of ``head_sha``. A working-tree or staged
    capture's head is the tree of ``base_sha`` with the changed files replaced by the capture's frozen text (a deleted
    path, or the old path of a rename, removed), the same head ``snapshot.materialize_review_side`` builds; a changed
    file that Git calls binary comes from the media artifact, as in ``snapshot.frozen_revision_overlay``. A frozen
    changed file that would be searched but whose text or bytes are missing, or any other kind of revision, ends the
    search as incomplete (:class:`SearchBound`). A symlink is skipped and a submodule's contents are not walked.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import _open_repo, _tree_for_sha
    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    changed: set[str] = set()
    removed: set[str] = set()
    frozen: list[str] = []
    binary: list[str] = []
    blobs: Mapping[str, str] = {}
    if revision.range_mode == "commit_range":
        tree_sha = revision.head_sha
        if not tree_sha:
            raise SearchBound("this revision has no head commit")
    elif revision.range_mode in {"working_tree", "staged"}:
        tree_sha = revision.base_sha
        packet = read_packet_json(store, revision)
        loaded = store.read_blob_artifact(revision.review_id, revision.id)
        files = None if packet is None else packet.get("files")
        if loaded is None or not isinstance(files, list):
            raise SearchBound("the frozen changed files are not available")
        blobs = loaded
        for entry in files:
            if not isinstance(entry, Mapping):
                raise SearchBound("the frozen changed files are not readable")
            path = str(entry.get("path") or "")
            status = str(entry.get("status") or "")
            old_path = str(entry.get("old_path") or "")
            if status == "deleted":
                removed.add(path)
                continue
            if status == "renamed" and old_path and old_path != path:
                removed.add(old_path)
            changed.add(path)
            if entry.get("submodule_pointer") is None and is_searched_path(path):
                (binary if entry.get("is_binary") else frozen).append(path)
    else:
        raise SearchBound("this revision has no head tree")

    for path in frozen:
        yield _frozen_file(path, blobs)
    if binary:
        media = store.read_media_artifact(
            revision.review_id, revision.id
        )  # read once, only when a binary one is searched
        for path in binary:
            yield _media_file(path, media)
    repo = _open_repo(repo_root)
    tree = _tree_for_sha(repo, tree_sha)
    yield from _walk(repo, tree, "", skip=removed | changed, budget=budget)


def _blob_reader(repo: Any, oid: Any) -> Callable[[], bytes]:
    def read() -> bytes:
        return bytes(repo[oid].data)

    return read


def _blob_sniffer(repo: Any, oid: Any) -> Callable[[], bytes]:
    # lc-debt: libgit2 inflates the whole blob even for a sniff, so an oversized file costs one read and a transient copy
    # of its size; upgrade path: a size-capped odb read, or the file's size class from a per-Snapshot index.
    def sniff() -> bytes:
        return bytes(repo[oid].data[:_BINARY_SNIFF])

    return sniff


def _walk(repo: Any, tree: Any, prefix: str, *, skip: Collection[str], budget: ConfigBudget) -> Iterator[ConfigFile]:
    for entry in tree:
        budget.tick()
        path = f"{prefix}{entry.name}"
        kind = entry.type_str
        if kind == "tree":
            yield from _walk(repo, repo[entry.id], path + "/", skip=skip, budget=budget)
        elif kind == "blob" and entry.filemode != 0o120000 and path not in skip and is_searched_path(path):
            yield ConfigFile(path, entry.size, _blob_reader(repo, entry.id), _blob_sniffer(repo, entry.id))
        # a commit entry is a submodule pointer (not walked); a symlink holds a path, and the file it points to is walked itself
