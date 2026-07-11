"""Savings benchmark cases: read + grep across languages, plus code-intel tools.

## Fixture strategy
- **outline**: one representative individual file per language (2k-9k lines).
  Baseline = Claude native Read capped at 2000 lines of that file.
  Using combined blobs here produced artificial negatives because the outline
  covered 12k-line blobs while the baseline only saw the first 2000 lines.
- **range**: combined file per language (100k+ tokens) — guarantees content
  past line 2000 so the range read always beats the native Read cap.
- **grep**: combined file per language — more matches, more realistic savings.

All fixtures are downloaded from public GitHub refs and cached under
``benchmarks/mcp_tools/fixtures/downloaded/``. Delete the cache dir to re-fetch.

## Code-intel cases
Code-intel cases (node/callers/callees/symbols) run against the LemonCrow repo itself
(which already has a code index). Baseline = what a naive agent would
consume via grep + full-file reads.
"""

from __future__ import annotations

import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import tiktoken
from lemoncrow.core.capabilities.native_read_baseline import (
    CLAUDE_NATIVE_READ_LINE_LIMIT,
    claude_read_baseline_text,
)

from benchmarks.mcp_tools.harness import BaselineMeasurement, BenchCase

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "downloaded"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENC = tiktoken.get_encoding("cl100k_base")


def _tok(text: str) -> int:
    return len(_ENC.encode(text))


# ---------------------------------------------------------------------------
# Fixture sets — (language, ext, output_stem, [(cached_name, url), ...])
# Each set is concatenated into a single combined file of 100k+ tokens.
# ---------------------------------------------------------------------------

_BASE = "https://raw.githubusercontent.com"

_FIXTURE_SETS: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    (
        "c",
        "c",
        "c_combined",
        [
            ("c_select.c", f"{_BASE}/sqlite/sqlite/version-3.45.3/src/select.c"),  # 93k tok
            ("c_vdbe.c", f"{_BASE}/sqlite/sqlite/version-3.45.3/src/vdbe.c"),  # +35k tok
            ("c_where.c", f"{_BASE}/sqlite/sqlite/version-3.45.3/src/where.c"),  # +25k tok
        ],
    ),
    (
        "go",
        "go",
        "go_combined",
        [
            ("go_server.go", f"{_BASE}/golang/go/go1.22.3/src/net/http/server.go"),  # 32k tok
            (
                "go_transport.go",
                f"{_BASE}/golang/go/go1.22.3/src/net/http/transport.go",
            ),  # +35k tok
            ("go_request.go", f"{_BASE}/golang/go/go1.22.3/src/net/http/request.go"),  # +9k tok
            ("go_client.go", f"{_BASE}/golang/go/go1.22.3/src/net/http/client.go"),  # +7k tok
            ("go_tls_conn.go", f"{_BASE}/golang/go/go1.22.3/src/crypto/tls/conn.go"),  # +12k tok
            (
                "go_tls_hs_server.go",
                f"{_BASE}/golang/go/go1.22.3/src/crypto/tls/handshake_server.go",
            ),  # +8k tok
        ],
    ),
    (
        "java",
        "java",
        "java_combined",
        [
            (
                "java_annotation_utils.java",
                f"{_BASE}/spring-projects/spring-framework/v6.1.8/spring-core/src/main/java/org/springframework/core/annotation/AnnotationUtils.java",
            ),  # 14k tok
            (
                "java_abstract_bean_factory.java",
                f"{_BASE}/spring-projects/spring-framework/v6.1.8/spring-beans/src/main/java/org/springframework/beans/factory/support/AbstractBeanFactory.java",
            ),  # +13k
            (
                "java_default_lb_factory.java",
                f"{_BASE}/spring-projects/spring-framework/v6.1.8/spring-beans/src/main/java/org/springframework/beans/factory/support/DefaultListableBeanFactory.java",
            ),  # +15k
            (
                "java_bd_parser_delegate.java",
                f"{_BASE}/spring-projects/spring-framework/v6.1.8/spring-beans/src/main/java/org/springframework/beans/factory/xml/BeanDefinitionParserDelegate.java",
            ),  # +13k
            (
                "java_abstract_app_ctx.java",
                f"{_BASE}/spring-projects/spring-framework/v6.1.8/spring-context/src/main/java/org/springframework/context/support/AbstractApplicationContext.java",
            ),  # +10k
            (
                "java_abstract_autowire.java",
                f"{_BASE}/spring-projects/spring-framework/v6.1.8/spring-beans/src/main/java/org/springframework/beans/factory/support/AbstractAutowireCapableBeanFactory.java",
            ),  # +17k
            (
                "java_dispatcher_servlet.java",
                f"{_BASE}/spring-projects/spring-framework/v6.1.8/spring-webmvc/src/main/java/org/springframework/web/servlet/DispatcherServlet.java",
            ),  # +12k
            (
                "java_mvc_handler_mapping.java",
                f"{_BASE}/spring-projects/spring-framework/v6.1.8/spring-webmvc/src/main/java/org/springframework/web/servlet/handler/AbstractHandlerMapping.java",
            ),  # +8k
            (
                "java_tx_interceptor.java",
                f"{_BASE}/spring-projects/spring-framework/v6.1.8/spring-tx/src/main/java/org/springframework/transaction/interceptor/TransactionInterceptor.java",
            ),  # +6k
        ],
    ),
    (
        "javascript",
        "js",
        "javascript_combined",
        [
            ("js_lodash.js", f"{_BASE}/lodash/lodash/4.17.21/lodash.js"),  # 143k tok
            # webpack Compilation.js: CommonJS class with top-level class_declaration
            # (unlike Lodash which is IIFE-wrapped and produces no outline)
            (
                "js_webpack_compilation.js",
                f"{_BASE}/webpack/webpack/v5.91.0/lib/Compilation.js",
            ),  # ~3k lines
        ],
    ),
    (
        "python",
        "py",
        "python_combined",
        [
            (
                "py_django_compiler.py",
                f"{_BASE}/django/django/stable/5.0.x/django/db/models/sql/compiler.py",
            ),  # 17k tok
            (
                "py_django_query.py",
                f"{_BASE}/django/django/stable/5.0.x/django/db/models/query.py",
            ),  # +25k tok
            (
                "py_cpython_typing.py",
                f"{_BASE}/python/cpython/main/Lib/test/test_typing.py",
            ),  # +75k tok
        ],
    ),
    (
        "ruby",
        "rb",
        "ruby_combined",
        [
            (
                "rb_ar_base.rb",
                f"{_BASE}/rails/rails/v7.1.3/activerecord/lib/active_record/base.rb",
            ),  # 3.6k tok
            (
                "rb_routing_mapper.rb",
                f"{_BASE}/rails/rails/v7.1.3/actionpack/lib/action_dispatch/routing/mapper.rb",
            ),  # +18k
            (
                "rb_form_helper.rb",
                f"{_BASE}/rails/rails/v7.1.3/actionview/lib/action_view/helpers/form_helper.rb",
            ),  # +13k
            (
                "rb_query_methods.rb",
                f"{_BASE}/rails/rails/v7.1.3/activerecord/lib/active_record/relation/query_methods.rb",
            ),  # +9k
            ("rb_sinatra_base.rb", f"{_BASE}/sinatra/sinatra/main/lib/sinatra/base.rb"),  # +16k
            (
                "rb_dependencies.rb",
                f"{_BASE}/rails/rails/v7.1.3/activesupport/lib/active_support/dependencies.rb",
            ),  # +8k
            (
                "rb_action_mailer_base.rb",
                f"{_BASE}/rails/rails/v7.1.3/actionmailer/lib/action_mailer/base.rb",
            ),  # +5k
            (
                "rb_url_helper.rb",
                f"{_BASE}/rails/rails/v7.1.3/actionview/lib/action_view/helpers/url_helper.rb",
            ),  # +5k
            (
                "rb_form_tag_helper.rb",
                f"{_BASE}/rails/rails/v7.1.3/actionview/lib/action_view/helpers/form_tag_helper.rb",
            ),  # +7k
            (
                "rb_railties_app.rb",
                f"{_BASE}/rails/rails/v7.1.3/railties/lib/rails/application.rb",
            ),  # +5k
        ],
    ),
    (
        "rust",
        "rs",
        "rust_combined",
        [
            (
                "rs_tokio_worker.rs",
                f"{_BASE}/tokio-rs/tokio/tokio-1.38.0/tokio/src/runtime/scheduler/multi_thread/worker.rs",
            ),  # 9k tok
            (
                "rs_rustc_ast.rs",
                f"{_BASE}/rust-lang/rust/1.78.0/compiler/rustc_ast/src/ast.rs",
            ),  # +25k
            (
                "rs_rustc_hir.rs",
                f"{_BASE}/rust-lang/rust/1.78.0/compiler/rustc_hir/src/hir.rs",
            ),  # +17k
            ("rs_serde_json_de.rs", f"{_BASE}/serde-rs/json/v1.0.117/src/de.rs"),  # +22k
            ("rs_serde_json_ser.rs", f"{_BASE}/serde-rs/json/v1.0.117/src/ser.rs"),  # +7k
            ("rs_serde_json_value.rs", f"{_BASE}/serde-rs/json/v1.0.117/src/value/mod.rs"),  # +12k
            (
                "rs_tokio_runtime.rs",
                f"{_BASE}/tokio-rs/tokio/tokio-1.38.0/tokio/src/runtime/runtime.rs",
            ),  # +6k
        ],
    ),
    (
        "typescript",
        "ts",
        "typescript_combined",
        [
            (
                "ts_utilities.ts",
                f"{_BASE}/microsoft/TypeScript/v5.4.5/src/compiler/utilities.ts",
            ),  # 88k tok
            (
                "ts_scanner.ts",
                f"{_BASE}/microsoft/TypeScript/v5.4.5/src/compiler/scanner.ts",
            ),  # +38k tok
        ],
    ),
]


# Per-language individual file used for outline benchmarks.
# One representative file per language — large enough that the outline is
# meaningful, but a single real file (not an artificial concatenation).
# These are downloaded as a side-effect of _build_combined / _ensure_fixtures.
_OUTLINE_INDIVIDUAL: dict[str, str] = {
    "c": "c_select.c",  # SQLite select.c       (8.6k lines)
    "go": "go_server.go",  # net/http server.go    (3.8k lines)
    "java": "java_abstract_bean_factory.java",  # Spring ABF            (2.1k lines)
    "javascript": "js_webpack_compilation.js",  # webpack Compilation.js (~3k lines, top-level class)
    "python": "py_django_compiler.py",  # Django SQL compiler   (2.1k lines)
    "ruby": "rb_routing_mapper.rb",  # Rails routing mapper  (2.3k lines)
    "rust": "rs_serde_json_de.rs",  # serde_json de         (2.7k lines)
    "typescript": "ts_scanner.ts",  # TypeScript scanner    (2.9k lines)
}

# ---------------------------------------------------------------------------
# Fixture download + concatenation
# ---------------------------------------------------------------------------


def _download(filename: str, url: str) -> Path | None:
    dest = _FIXTURE_DIR / filename
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            dest.write_bytes(resp.read())
        return dest
    except Exception as exc:
        print(f"  [savings] WARNING: could not download {filename}: {exc}")
        return None


def _build_combined(lang: str, ext: str, stem: str, parts: list[tuple[str, str]]) -> Path | None:
    """Download component files and concatenate into combined fixture. Cached."""
    combined = _FIXTURE_DIR / f"{stem}.{ext}"
    if combined.exists() and combined.stat().st_size > 0:
        return combined

    pieces: list[str] = []
    # Use language-appropriate comment syntax so the combined file remains parseable.
    _COMMENT_PREFIX: dict[str, str] = {"python": "#", "ruby": "#", "bash": "#"}
    comment = _COMMENT_PREFIX.get(lang, "//")
    for filename, url in parts:
        path = _download(filename, url)
        if path is None:
            print(f"  [savings] WARNING: skipping {lang} — could not download {filename}")
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        pieces.append(f"{comment} --- {filename} ---\n{text}")

    if not pieces:
        return None

    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    combined.write_text("\n\n".join(pieces), encoding="utf-8")
    tok = _tok(combined.read_text(encoding="utf-8", errors="replace"))
    print(f"  [savings] {lang}: {combined.name}  {tok:,} tokens")
    return combined


def _ensure_fixtures() -> dict[str, Path]:
    """Return {language: combined_path} for all fixture sets."""
    result: dict[str, Path] = {}
    for lang, ext, stem, parts in _FIXTURE_SETS:
        path = _build_combined(lang, ext, stem, parts)
        if path is not None:
            result[lang] = path
    return result


# ---------------------------------------------------------------------------
# Baseline builders
# ---------------------------------------------------------------------------


def _baseline_claude_read(case: BenchCase) -> BaselineMeasurement:
    """Baseline = Claude's built-in Read (capped at the native line limit).

    This is the honest baseline: what would an agent receive if they called
    the native Read tool instead of LemonCrow's ``read``?  Claude Code truncates
    at a fixed line cap, so any file longer than that only exposes a partial
    view. The cap and truncation come from the shared runtime estimator, so this
    benchmark and the live savings numbers can never silently diverge.
    """
    path = Path(str(case.args["path"]))
    text = path.read_text(encoding="utf-8", errors="replace")
    native = claude_read_baseline_text(text)
    line_count = min(len(text.splitlines()), CLAUDE_NATIVE_READ_LINE_LIMIT)
    return BaselineMeasurement(
        payload=native,
        input_file_tokens=_tok(native),
        commands=[f"Read({path.name})  # native, {line_count} lines"],
    )


# ---------------------------------------------------------------------------
# Code-intel baseline helpers — grep + read matched files (what agent does without code-intel)
# ---------------------------------------------------------------------------


def _rg(pattern: str, path: str, *, flags: list[str] | None = None) -> str:
    """Run ripgrep, return stdout (capped at 200k chars)."""
    cmd = ["rg", *(flags or []), "-n", pattern, path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=str(_REPO_ROOT))
        return (result.stdout or "")[:200_000]
    except FileNotFoundError:
        return ""


def _rg_files(pattern: str, path: str) -> list[str]:
    """Return list of files matching *pattern* under *path*."""
    cmd = ["rg", "-l", pattern, path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=str(_REPO_ROOT))
        return [f.strip() for f in (result.stdout or "").splitlines() if f.strip()]
    except FileNotFoundError:
        return []


def _read_file(p: Path, max_chars: int = 150_000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        return ""


def _make_node_baseline(symbol: str, containing_file: str) -> Any:
    """Baseline for `node`: agent reads the whole file containing the symbol."""

    def _b(case: BenchCase) -> BaselineMeasurement:
        content = _read_file(_REPO_ROOT / containing_file)
        return BaselineMeasurement(
            payload=content,
            commands=[f"rg -l {symbol!r} src/lemoncrow", f"cat {containing_file}"],
        )

    return _b


def _make_callers_baseline(symbol: str) -> Any:
    """Baseline for `callers`: agent greps + reads every file that mentions the symbol.

    Honest cost: agent sees the grep matches, then opens each caller file to
    understand context — not just the grep text alone.
    """

    def _b(case: BenchCase) -> BaselineMeasurement:
        files = _rg_files(symbol, "src/lemoncrow")
        grep_out = _rg(symbol, "src/lemoncrow")
        file_content = "".join(_read_file(_REPO_ROOT / f) for f in files[:8])
        return BaselineMeasurement(
            payload=grep_out + file_content,
            commands=[f"rg -n {symbol!r} src/lemoncrow"] + [f"cat {f}" for f in files[:8]],
        )

    return _b


def _make_symbols_baseline(symbol: str) -> Any:
    """Baseline for symbol search: grep for name + read first matched file."""

    def _b(case: BenchCase) -> BaselineMeasurement:
        files = _rg_files(symbol, "src/lemoncrow")
        grep_out = _rg(symbol, "src/lemoncrow")
        first_content = _read_file(_REPO_ROOT / files[0]) if files else ""
        return BaselineMeasurement(
            payload=grep_out + first_content,
            commands=[f"rg -n {symbol!r} src/lemoncrow"] + ([f"cat {files[0]}"] if files else []),
        )

    return _b


# ---------------------------------------------------------------------------
# Custom assertions
# ---------------------------------------------------------------------------


def _assert_outline(result: dict[str, Any]) -> None:
    assert result.get("mode") == "outline", f"expected outline mode, got mode={result.get('mode')!r}"
    outline = result.get("outline")
    assert outline is not None, "outline key must be present and non-null"
    # Guard against silent failures (empty outline from parse errors, broken
    # fallbacks, etc.). A real outline must have at least some content.
    if isinstance(outline, dict):
        symbols = outline.get("symbols") or []
        text = outline.get("text") or ""
        assert (
            len(symbols) > 0 or len(text) > 10
        ), f"outline appears empty: symbols={len(symbols)}, text_len={len(text)} — likely a silent parse failure"


def _assert_range(result: dict[str, Any]) -> None:
    assert "content" in result, "range read must have 'content'"
    assert "range" in result, "range read must have 'range'"
    ts = result.get("tokens_saved", 0)
    assert isinstance(ts, int) and ts > 0, f"range read must report tokens_saved > 0, got {ts!r}"


def _assert_grep_ranked(result: dict[str, Any]) -> None:
    assert "matches" in result or "content" in result, "grep must return matches or content"


def _assert_intel_node(result: dict[str, Any]) -> None:
    # node returns signature/id/path from code index (body only with snippet=full)
    has_content = bool(result.get("signature") or result.get("id") or result.get("name"))
    assert has_content, f"node must return a symbol record, got keys: {list(result.keys())}"


def _assert_intel_callers(result: dict[str, Any]) -> None:
    # callers response uses 'related' (list of caller records) or 'target'
    has_refs = (
        isinstance(result.get("related"), list)
        or isinstance(result.get("references"), dict)
        or isinstance(result.get("callers"), list)
        or bool(result.get("target"))
        or bool(result.get("rendered"))
    )
    assert has_refs, f"callers must return related/target/references, got keys: {list(result.keys())}"


def _assert_intel_search(result: dict[str, Any]) -> None:
    assert (
        "items" in result or "symbols" in result or "rendered" in result
    ), f"symbols search must return items/symbols/rendered, got keys: {list(result.keys())}"


# ---------------------------------------------------------------------------
# Case builders: read / grep / code-intel
# ---------------------------------------------------------------------------


def _read_outline_case(lang: str, path: Path) -> BenchCase:
    return BenchCase(
        op="read",
        label=f"read/outline/{lang}",
        args={"path": str(path), "include_meta": True},
        assert_keys=["mode", "outline"],
        custom_assert=_assert_outline,
        baseline_builder=_baseline_claude_read,
        min_baseline_tokens=5_000,  # native Read of any 100k+ tok file is >5k tok
    )


def _read_range_case(lang: str, path: Path) -> BenchCase:
    """Read lines 2100-2200 -- past Claude native Read cap (2000 lines).

    Baseline = native Read (returns lines 1-2000 only -- agent cannot
    reach these lines without LemonCrow or a shell command).
    """
    return BenchCase(
        op="read",
        label=f"read/range/{lang}",
        args={"path": str(path), "range": "2100-2200", "include_meta": True},
        assert_keys=["content", "range", "tokens_saved"],
        custom_assert=_assert_range,
        baseline_builder=_baseline_claude_read,
        min_baseline_tokens=5_000,
    )


def _grep_baseline_builder(pattern: str, path: Path) -> Any:
    """Baseline = raw `rg` output (what an agent gets from a shell grep)."""

    def _builder(case: BenchCase) -> BaselineMeasurement:
        result = subprocess.run(
            ["rg", "-n", pattern, str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        out = (result.stdout or "")[:200_000]
        return BaselineMeasurement(
            payload=out,
            commands=[f"rg -n {pattern!r} {path.name}"],
        )

    return _builder


def _grep_case(lang: str, path: Path, pattern: str) -> BenchCase:
    return BenchCase(
        op="grep",
        label=f"grep/ranked/{lang}",
        args={
            "path": str(path.parent),
            "content_regex": pattern,
            "mode": "map",
            "context_budget_tokens": 3000,
            "include_meta": True,
        },
        assert_keys=[],
        custom_assert=_assert_grep_ranked,
        baseline_builder=_grep_baseline_builder(pattern, path),
        min_baseline_tokens=0,  # some patterns match sparsely; report, do not gate
    )


# Patterns chosen to match real function definitions across the combined fixtures.
# Avoid anchors that miss indented methods (Python/Ruby) or private fns (Rust).
_GREP_PATTERNS: dict[str, str] = {
    "c": r"^[a-zA-Z].*\w+\s*\(",  # top-level C function definitions
    "go": r"^func ",  # all Go funcs (exported + unexported)
    "java": r"\bpublic \w+ \w+\(",  # public methods/functions
    "javascript": r"\bfunction \w+",  # named functions
    "python": r"\bdef \w+",  # all defs including methods
    "ruby": r"\bdef \w+",  # all defs including methods
    "rust": r"\bfn \w+",  # all fns (pub and private)
    "typescript": r"\bfunction \w+",  # named functions
}

# Code-intel cases — run against the LemonCrow repo (requires code index)
_INTEL_CASES: list[BenchCase] = [
    # symbols/search: baseline = grep text + reading first matched file
    BenchCase(
        op="symbols",
        label="intel/symbols/SemanticFileMemoryCapability",
        args={"op": "search", "query": "SemanticFileMemoryCapability"},
        assert_keys=[],
        custom_assert=_assert_intel_search,
        baseline_builder=_make_symbols_baseline("SemanticFileMemoryCapability"),
        min_baseline_tokens=0,
    ),
    # node: baseline = read whole containing file (agent reads file to find def)
    BenchCase(
        op="symbols",
        label="intel/node/compute_savings_summary",
        args={"op": "node", "symbol_name": "compute_savings_summary"},
        assert_keys=[],
        custom_assert=_assert_intel_node,
        baseline_builder=_make_node_baseline(
            "compute_savings_summary",
            "src/lemoncrow/core/capabilities/savings_summary.py",
        ),
        min_baseline_tokens=0,
    ),
    # node: function in 45k-token file — baseline = read whole mcp_server.py
    BenchCase(
        op="symbols",
        label="intel/node/tool_smart_read",
        args={"op": "node", "symbol_name": "tool_smart_read"},
        assert_keys=[],
        custom_assert=_assert_intel_node,
        baseline_builder=_make_node_baseline(
            "tool_smart_read",
            "src/lemoncrow/gateway/adapters/mcp_server.py",
        ),
        min_baseline_tokens=0,
    ),
    # callers: baseline = grep + read every file that mentions the symbol
    BenchCase(
        op="symbols",
        label="intel/callers/_append_savings",
        args={"op": "callers", "symbol_name": "_append_savings", "depth": 1, "limit": 20},
        assert_keys=[],
        custom_assert=_assert_intel_callers,
        baseline_builder=_make_callers_baseline("_append_savings"),
        min_baseline_tokens=0,
    ),
    BenchCase(
        op="symbols",
        label="intel/callers/compute_savings_summary",
        args={"op": "callers", "symbol_name": "compute_savings_summary", "depth": 1, "limit": 20},
        assert_keys=[],
        custom_assert=_assert_intel_callers,
        baseline_builder=_make_callers_baseline("compute_savings_summary"),
        min_baseline_tokens=0,
    ),
    # callees: baseline = full capability.py (agent reads file to trace calls manually)
    BenchCase(
        op="symbols",
        label="intel/callees/smart_read",
        args={"op": "callees", "symbol_name": "smart_read", "depth": 1, "limit": 20},
        assert_keys=[],
        custom_assert=lambda r: None,
        baseline_builder=_make_node_baseline(
            "smart_read",
            "src/lemoncrow/core/capabilities/semantic_file_memory/capability.py",
        ),
        min_baseline_tokens=0,
    ),
    # search: baseline = raw grep text for the search terms
    BenchCase(
        op="symbols",
        label="intel/search/token-savings-computation",
        args={"op": "search", "query": "token savings computation outline mode", "limit": 10},
        assert_keys=[],
        custom_assert=_assert_intel_search,
        baseline_builder=lambda c: BaselineMeasurement(
            payload=_rg("tokens_saved", "src/lemoncrow"),
            commands=["rg -n tokens_saved src/lemoncrow"],
        ),
        min_baseline_tokens=0,
    ),
]


# ---------------------------------------------------------------------------
# Extra repo-local cases (always available, fill outline/range/grep to 10 each)
# ---------------------------------------------------------------------------

_REPO_EXTRA_OUTLINE: list[BenchCase] = [
    BenchCase(
        op="read",
        label="read/outline/repo/mcp_server.py",
        args={
            "path": str(_REPO_ROOT / "src/lemoncrow/gateway/adapters/mcp_server.py"),
            "include_meta": True,
        },
        assert_keys=["mode"],
        custom_assert=_assert_outline,
        baseline_builder=_baseline_claude_read,
        min_baseline_tokens=5_000,
    ),
    BenchCase(
        op="read",
        label="read/outline/repo/capability.py",
        args={
            "path": str(_REPO_ROOT / "src/lemoncrow/core/capabilities/semantic_file_memory/capability.py"),
            "include_meta": True,
        },
        assert_keys=["mode"],
        custom_assert=_assert_outline,
        baseline_builder=_baseline_claude_read,
        min_baseline_tokens=2_000,
    ),
]

_REPO_EXTRA_RANGE: list[BenchCase] = [
    BenchCase(
        op="read",
        label="read/range/repo/mcp_server.py:3000-3100",
        args={
            "path": str(_REPO_ROOT / "src/lemoncrow/gateway/adapters/mcp_server.py"),
            "range": "3000-3100",
            "include_meta": True,
        },
        assert_keys=["content", "range", "tokens_saved"],
        custom_assert=_assert_range,
        baseline_builder=_baseline_claude_read,
        min_baseline_tokens=5_000,
    ),
    BenchCase(
        op="read",
        label="read/range/repo/savings_summary.py:200-300",
        args={
            "path": str(_REPO_ROOT / "src/lemoncrow/core/capabilities/savings_summary.py"),
            "range": "200-300",
            "include_meta": True,
        },
        assert_keys=["content", "range", "tokens_saved"],
        custom_assert=_assert_range,
        baseline_builder=_baseline_claude_read,
        min_baseline_tokens=2_000,
    ),
]

_PY_COMBINED = _FIXTURE_DIR / "python_combined.py"
_TS_COMBINED = _FIXTURE_DIR / "typescript_combined.ts"

_REPO_EXTRA_GREP: list[BenchCase] = [
    BenchCase(
        op="grep",
        label="grep/file_paths_only/python",
        args={
            "path": str(_PY_COMBINED),
            "content_regex": r"\bdef \w+",
            "mode": "paths",
            "include_meta": True,
        },
        assert_keys=[],
        custom_assert=lambda r: None,
        baseline_builder=_grep_baseline_builder(r"\bdef \w+", _PY_COMBINED),
        min_baseline_tokens=0,
    ),
    BenchCase(
        op="grep",
        label="grep/file_paths_with_content/typescript",
        args={
            "path": str(_TS_COMBINED),
            "content_regex": r"\bfunction \w+",
            "mode": "content",
            "context_budget_tokens": 3000,
            "include_meta": True,
        },
        assert_keys=[],
        custom_assert=lambda r: None,
        baseline_builder=_grep_baseline_builder(r"\bfunction \w+", _TS_COMBINED),
        min_baseline_tokens=0,
    ),
]

# ---------------------------------------------------------------------------
# Code-intel expanded cases — 10 per tool type
# ---------------------------------------------------------------------------

_NODE_TARGETS = [
    ("compute_savings_summary", "src/lemoncrow/core/capabilities/savings_summary.py"),
    ("tool_smart_read", "src/lemoncrow/gateway/adapters/mcp_server.py"),
    ("smart_read", "src/lemoncrow/core/capabilities/semantic_file_memory/capability.py"),
    ("_append_savings", "src/lemoncrow/gateway/adapters/mcp_server.py"),
    ("_extract_tokens_saved", "src/lemoncrow/gateway/adapters/mcp_server.py"),
    ("claude_transcript_candidates", "src/lemoncrow/core/capabilities/savings_summary.py"),
    ("resolve_model_id", "src/lemoncrow/core/capabilities/savings_summary.py"),
    ("_read_claude_session_savings", "src/lemoncrow/core/capabilities/savings_summary.py"),
    ("_get_host_session_sidecar_path", "src/lemoncrow/gateway/adapters/mcp_server.py"),
    ("_tool_code_alias_handler", "src/lemoncrow/gateway/adapters/mcp_server.py"),
]

_INTEL_NODE_CASES: list[BenchCase] = [
    BenchCase(
        op="symbols",
        label=f"intel/node/{sym}",
        args={"op": "node", "symbol_name": sym},
        assert_keys=[],
        custom_assert=_assert_intel_node,
        baseline_builder=_make_node_baseline(sym, f),
        min_baseline_tokens=0,
    )
    for sym, f in _NODE_TARGETS
]

_CALLERS_TARGETS = [
    "_append_savings",
    "compute_savings_summary",
    "smart_read",
    "_extract_tokens_saved",
    "_append_workspace_savings",
    "resolve_model_id",
    "claude_transcript_candidates",
    "_read_claude_session_savings",
    "_get_host_session_sidecar_path",
    "_tool_code_alias_handler",
]

_INTEL_CALLERS_CASES: list[BenchCase] = [
    BenchCase(
        op="symbols",
        label=f"intel/callers/{sym}",
        args={"op": "callers", "symbol_name": sym, "depth": 1, "limit": 20},
        assert_keys=[],
        custom_assert=_assert_intel_callers,
        baseline_builder=_make_callers_baseline(sym),
        min_baseline_tokens=0,
    )
    for sym in _CALLERS_TARGETS
]

_INTEL_CALLEES_CASES: list[BenchCase] = [
    BenchCase(
        op="symbols",
        label=f"intel/callees/{sym}",
        args={"op": "callees", "symbol_name": sym, "depth": 1, "limit": 20},
        assert_keys=[],
        custom_assert=lambda r: None,
        baseline_builder=_make_node_baseline(sym, f),
        min_baseline_tokens=0,
    )
    for sym, f in _NODE_TARGETS
]

_SYMBOLS_TARGETS = [
    (
        "SemanticFileMemoryCapability",
        "SemanticFileMemoryCapability",
        "SemanticFileMemoryCapability",
    ),
    ("compute_savings_summary", "compute_savings_summary", "compute_savings_summary"),
    ("RunLedger", "RunLedger", "RunLedger"),
    ("ContextBudgetRecorder", "ContextBudgetRecorder", "ContextBudget"),
    ("classify_command", "classify_command bash shell policy", "classify_command"),
    ("token-savings-outline", "token savings outline mode threshold", "tokens_saved"),
    ("session-sidecar-path", "MCP session sidecar path per host", "session_stats"),
    ("transcript-parent-subagent", "transcript parent session ID subagent", "sessionId"),
    ("context-budget-session", "context budget recorder session tokens", "ContextBudget"),
    ("smart-file-threshold", "smart file memory outline threshold LOC", "outline_threshold"),
]

_INTEL_SYMBOLS_CASES: list[BenchCase] = [
    BenchCase(
        op="symbols",
        label=f"intel/symbols/{lbl}",
        args={"op": "search", "query": query},
        assert_keys=[],
        custom_assert=_assert_intel_search,
        baseline_builder=_make_symbols_baseline(grep_t),
        min_baseline_tokens=0,
    )
    for lbl, query, grep_t in _SYMBOLS_TARGETS
]

_SEARCH_TARGETS = [
    ("token-savings-outline", "token savings computation outline mode", "tokens_saved"),
    ("session-savings-transcript", "session savings summary Claude transcript", "session_stats"),
    ("mcp-tool-savings", "MCP tool savings recording per call", "_append_savings"),
    ("workspace-session-state", "workspace session state JSON path hash", "session_state"),
    ("read-outline-ast", "read file outline mode AST tree-sitter", "outline_threshold"),
    ("context-budget-recorder", "context budget recorder tokens session", "ContextBudget"),
    ("intel-callers-graph", "code-intel callers", "callers"),
    ("transcript-parent-id", "transcript parent session subagent ID lookup", "sessionId"),
    ("smart-file-capability", "smart file memory capability outline threshold", "smart_read"),
    ("ranked-grep-budget", "ranked file map grep context budget tokens", "context_budget"),
]

_INTEL_SEARCH_CASES: list[BenchCase] = [
    BenchCase(
        op="symbols",
        label=f"intel/search/{lbl}",
        args={"op": "search", "query": query, "limit": 10},
        assert_keys=[],
        custom_assert=_assert_intel_search,
        baseline_builder=(
            lambda c, t=grep_t: BaselineMeasurement(
                payload=_rg(t, "src/lemoncrow"),
                commands=[f"rg -n {t!r} src/lemoncrow"],
            )
        ),
        min_baseline_tokens=0,
    )
    for lbl, query, grep_t in _SEARCH_TARGETS
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_savings_cases() -> tuple[list[BenchCase], list[BenchCase]]:
    """Return (file_cases, scip_cases). Downloads fixtures on first call."""
    fixtures = _ensure_fixtures()  # also downloads individual files as a side-effect
    file_cases: list[BenchCase] = []
    for lang, combined_path in sorted(fixtures.items()):
        # outline: use individual file — avoids the combined-blob baseline problem
        outline_name = _OUTLINE_INDIVIDUAL.get(lang)
        outline_path = _FIXTURE_DIR / outline_name if outline_name else combined_path
        if not outline_path.exists():
            outline_path = combined_path  # graceful fallback if download failed
        file_cases.append(_read_outline_case(lang, outline_path))
        # range + grep: keep using the combined file
        file_cases.append(_read_range_case(lang, combined_path))
        pattern = _GREP_PATTERNS.get(lang, r"\w+")
        file_cases.append(_grep_case(lang, combined_path, pattern))
    file_cases.extend(_REPO_EXTRA_OUTLINE)
    file_cases.extend(_REPO_EXTRA_RANGE)
    file_cases.extend(_REPO_EXTRA_GREP)

    all_intel = (
        _INTEL_SYMBOLS_CASES + _INTEL_NODE_CASES + _INTEL_CALLERS_CASES + _INTEL_CALLEES_CASES + _INTEL_SEARCH_CASES
    )
    return file_cases, all_intel


# Eagerly build at import time so pytest parameterization works.
SAVINGS_CASES, SCIP_CASES = build_savings_cases()
