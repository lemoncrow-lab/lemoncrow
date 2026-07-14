"""Forward-ported MINIFY hardening: comment-atomicity, per-language atomics, registry.

Covers the three hardening changes:

1. ``_COMMENT_TYPES`` nodes are captured atomically in ``_collect_tokens`` so
   structured doc comments (rust/scala) no longer leak inner markers into the
   inter-token gap as "non-whitespace bytes between tokens".
2. ``_LANG_EXTRA_ATOMIC`` makes html ``doctype`` and bash ``command`` nodes
   atomic per-language (so ``<!DOCTYPE html>`` unnamed content and bash
   backslash-newline continuations stay verbatim) without affecting other
   languages.
3. html/css/lua are registered in the language table.

The fail-closed-on-parse-error and revalidation guards, newline preservation,
and the no-ASI invariant are all asserted to survive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.infra.code_intel.languages import (
    ALL_LANGUAGES,
    LANGUAGES,
    language_by_name,
    language_for_path,
)
from lemoncrow.pro.capabilities.source_projection import build_minified_projection
from lemoncrow.pro.capabilities.source_projection.minify import _parser_for

_CORPUS = Path(__file__).resolve().parent / "_corpus"


def _reparses_clean(content: str, lang: str) -> bool:
    parser = _parser_for(lang)
    assert parser is not None, f"no grammar for {lang}"
    root = parser.parse(content.encode("utf-8")).root_node
    return root is not None and not root.has_error


# --------------------------------------------------------------------------- #
# Task 3: registry records (html/.html,.htm; css/.css; lua/.lua)               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "extensions"),
    [
        ("html", {".html", ".htm"}),
        ("css", {".css"}),
        ("lua", {".lua"}),
    ],
)
def test_new_language_records_registered(name: str, extensions: set[str]) -> None:
    record = language_by_name(name)
    assert record is not None
    assert record.name == name == record.parser_name  # name == parser_name
    assert record.parser_name is not None
    assert set(record.extensions) == extensions
    assert name in ALL_LANGUAGES
    for ext in extensions:
        resolved = language_for_path(f"file{ext}")
        assert resolved is not None and resolved.name == name


# --------------------------------------------------------------------------- #
# Task 1: comments are atomic -> rust/scala with comments minify cleanly       #
# --------------------------------------------------------------------------- #


RUST_SAMPLE = """//! Crate-level doc comment.
//! Spans two lines.

use std::fmt;

/// A worker that does work.
///
/// # Examples
/// ```
/// let w = Worker::new(1);
/// ```
pub struct Worker {
    id: u32,
}

impl Worker {
    /* block comment
       across lines */
    pub fn new(id: u32) -> Self {
        // line comment
        let label = "keep   inner   spaces";
        let _ = label;
        Worker { id }
    }
}
"""


SCALA_SAMPLE = """package demo

/** A worker.
 *  @param id the worker id
 *  @return nothing useful
 */
class Worker(id: Int) {
  // run it once
  def run(): Int = {
    val label = "keep   inner   spaces"
    id + label.length
  }
}
"""


def test_rust_with_doc_and_block_comments_minifies() -> None:
    r = build_minified_projection(RUST_SAMPLE, "rust", path="w.rs")
    assert r.applied, r.reason  # no longer "non-whitespace bytes between tokens"
    assert r.projected_tokens < r.original_tokens
    assert _reparses_clean(r.content, "rust")
    assert "//! Crate-level" not in r.content  # doc comments stripped
    assert "/// A worker" not in r.content
    assert "block comment" not in r.content
    assert "// line comment" not in r.content
    assert "keep   inner   spaces" in r.content  # string interior verbatim


def test_scala_with_doc_comment_minifies() -> None:
    r = build_minified_projection(SCALA_SAMPLE, "scala", path="w.scala")
    assert r.applied, r.reason
    assert r.projected_tokens < r.original_tokens
    assert _reparses_clean(r.content, "scala")
    assert "@param" not in r.content  # doc comment stripped
    assert "// run it once" not in r.content
    assert "keep   inner   spaces" in r.content


# --------------------------------------------------------------------------- #
# Task 2: per-language atomics -> html doctype + bash command continuations    #
# --------------------------------------------------------------------------- #


HTML_SAMPLE = """<!DOCTYPE html>
<html lang="en">
  <head>
    <title>Dashboard</title>
  </head>
  <body>
    <!-- a stripped comment -->
    <h1>Pool   Overview</h1>
    <p>Some inline   text content here.</p>
    <script>
      const x = 1;
      console.log(x);
    </script>
  </body>
</html>
"""


BASH_SAMPLE = """#!/bin/bash
# configure the build
configure --prefix=/usr \\
  --enable-foo \\
  --enable-bar

cat <<'EOF'
  this   heredoc   body
  is preserved verbatim
EOF

echo done
"""


def test_html_doctype_and_text_minifies() -> None:
    r = build_minified_projection(HTML_SAMPLE, "html", path="page.html")
    assert r.applied, r.reason  # doctype unnamed content no longer aborts
    assert r.projected_tokens < r.original_tokens
    assert _reparses_clean(r.content, "html")
    assert "<!DOCTYPE html>" in r.content  # doctype kept verbatim (atomic)
    assert "<!-- a stripped comment -->" not in r.content
    assert "<script>" in r.content and "const x = 1;" in r.content
    assert "Pool   Overview" in r.content  # text content not collapsed


def test_bash_heredoc_and_line_continuation_minifies() -> None:
    r = build_minified_projection(BASH_SAMPLE, "bash", path="build.sh")
    assert r.applied, r.reason
    assert r.projected_tokens < r.original_tokens
    assert _reparses_clean(r.content, "bash")
    assert "# configure the build" not in r.content  # comment stripped
    assert "\\\n" in r.content  # backslash-newline continuation preserved
    assert "this   heredoc   body" in r.content  # heredoc body verbatim
    assert "is preserved verbatim" in r.content


def test_extra_atomic_is_per_language_not_global() -> None:
    # `command` is atomic for bash but must NOT be treated as atomic elsewhere:
    # a python identifier named `command` still minifies normally (no leak of
    # the bash-only atomic name into other grammars).
    py = "# c\ndef command(x):\n    return  x +  1\n"
    r = build_minified_projection(py, "python", path="m.py")
    assert r.applied, r.reason
    assert _reparses_clean(r.content, "python")
    assert "# c" not in r.content
    assert "return x + 1" in r.content  # redundant inner whitespace collapsed


# --------------------------------------------------------------------------- #
# Newline preservation invariant (no ASI / no newline collapsing)             #
# --------------------------------------------------------------------------- #


def test_newlines_preserved_no_asi() -> None:
    js = "// c\nconst a = 1\nconst b = 2\nconst c = a + b\n"
    r = build_minified_projection(js, "javascript", path="m.js")
    assert r.applied, r.reason
    assert _reparses_clean(r.content, "javascript")
    # statements stay newline-separated; no semicolons synthesized.
    assert "const a = 1\nconst b = 2" in r.content
    assert ";" not in r.content  # no ASI logic added


# --------------------------------------------------------------------------- #
# Broad coverage: >= 20 languages minify on clean snippets                     #
# --------------------------------------------------------------------------- #


CLEAN_SNIPPETS: dict[str, str] = {
    "python": "# c\ndef f(a, b):\n    return a + b\n",
    "typescript": "// c\nfunction f(a: number) {\n  return a + 1;\n}\n",
    "javascript": "// c\nfunction f(a) {\n  return a + 1;\n}\n",
    "bash": "# c\necho hello\nls -la\n",
    "csharp": "// c\nclass A {\n  int X() { return 1; }\n}\n",
    "go": "// c\npackage main\nfunc f() int {\n\treturn 1\n}\n",
    "rust": "// c\nfn f() -> i32 {\n    1\n}\n",
    "java": "// c\nclass A {\n  int x() { return 1; }\n}\n",
    "scala": "// c\nobject A {\n  def f(): Int = 1\n}\n",
    "ruby": "# c\ndef f\n  1\nend\n",
    "cpp": "// c\nint f() {\n  return 1;\n}\n",
    "c": "int f(void) {\n  return 1;\n}\n",
    "swift": "// c\nfunc f() -> Int {\n  return 1\n}\n",
    "php": "<?php\n// c\nfunction f() {\n  return 1;\n}\n",
    "sql": "-- c\nSELECT a,\n       b\nFROM t;\n",
    "yaml": "# c\nkey: value\nlist:\n  - a\n  - b\n",
    "toml": '# c\nkey = "value"\n[section]\nx = 1\n',
    "json": '{\n  "a": 1,\n  "b": 2\n}\n',
    "html": "<!DOCTYPE html>\n<html>\n  <body>\n    <p>hi</p>\n  </body>\n</html>\n",
    "css": "/* c */\n.a {\n  color: red;\n}\n",
    "lua": "-- c\nlocal function f()\n  return 1\nend\n",
}


def test_at_least_twenty_languages_minify_on_clean_snippets() -> None:
    succeeded: list[str] = []
    for lang, src in CLEAN_SNIPPETS.items():
        r = build_minified_projection(src, lang)
        if r.applied and r.projected_tokens < r.original_tokens and _reparses_clean(r.content, lang):
            succeeded.append(lang)
    assert len(succeeded) >= 20, f"only {len(succeeded)} minified: {sorted(succeeded)}"
    # the three forward-ported registrations are among them.
    assert {"html", "css", "lua"}.issubset(succeeded)


def test_language_table_has_at_least_twenty_records() -> None:
    assert len(LANGUAGES) >= 20
    assert {"html", "css", "lua"}.issubset(ALL_LANGUAGES)


# --------------------------------------------------------------------------- #
# Fail-closed: kotlin + SCSS-as-.css real corpus files stay graceful          #
# --------------------------------------------------------------------------- #


def test_kotlin_corpus_fails_closed_gracefully() -> None:
    f = _CORPUS / "tmp.unmin.kt"
    if not f.exists():
        pytest.skip("kotlin corpus fixture unavailable")
    text = f.read_text()
    r = build_minified_projection(text, "kotlin", path=str(f))
    assert not r.applied  # legitimately fail-closed
    assert r.content == text  # returned unchanged, no corruption
    assert r.projected_tokens == r.original_tokens
    assert r.reason  # carries a diagnostic reason, raises nothing


def test_scss_as_css_corpus_fails_closed_gracefully() -> None:
    # The corpus .css uses CSS-nesting / SCSS-isms the grammar rejects; the
    # fail-closed-on-parse-error guard must keep it from ever being projected.
    f = _CORPUS / "tmp.unmin.css"
    if not f.exists():
        pytest.skip("css corpus fixture unavailable")
    text = f.read_text()
    r = build_minified_projection(text, "css", path=str(f))
    assert not r.applied
    assert r.content == text
    assert r.reason


# --------------------------------------------------------------------------- #
# Real corpus end-to-end: rust/html/bash apply + projected<original + reparse  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("ext", "lang"),
    [("rs", "rust"), ("html", "html"), ("sh", "bash")],
)
def test_corpus_targets_apply_and_reparse(ext: str, lang: str) -> None:
    f = _CORPUS / f"tmp.unmin.{ext}"
    if not f.exists():
        pytest.skip(f"corpus fixture tmp.unmin.{ext} unavailable")
    text = f.read_text()
    r = build_minified_projection(text, lang, path=str(f))
    assert r.applied, r.reason
    assert r.projected_tokens < r.original_tokens  # projected < original
    assert _reparses_clean(r.content, lang)  # clean re-parse
    assert "\n" in r.content  # newlines preserved as separators
