"""A hunk is wider than the change it carries — and the packet must not be.

Why this module exists: ``git`` prints three unchanged context lines on each
side of every edit, so a hunk header's ``+start,count`` describes the *printed*
block, not the edited one. Every consumer that intersected symbol ranges with
that block therefore reported definitions nobody had touched. On this repo's own
commit ``983c233f2`` — where ``paths.py`` is ``+43 -0`` pure additions and
``workspace_key`` is byte-identical across it — the packet opened with
``⚠ workspace_key changed — 4 untouched call sites``. Roughly one ATTENTION
headline in five was manufactured that way, which is the whole product: a review
surface that cries wolf once per screen is one a reviewer stops reading.

The acceptance criterion is deliberately blunt and lives in
:func:`test_comment_only_commit_produces_no_attention_at_all`: a ``+3 -0``
pure-comment commit must yield an ATTENTION section that does not exist. It is
run against a *real* index, because without one there are no caller signals at
all and the assertion would pass vacuously on the very code path it exists to
pin. The same repository, one commit earlier, carries a genuine breaking change
whose findings must survive — asserted here too, since "report nothing" is a
trivially achievable and useless way to pass the first test.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.gitdiff import (
    collect_diff,
    is_dirty,
    load_blobs,
    resolve_rev_range,
)
from lemoncrow.pro.capabilities.review.models import DiffHunk, ReviewPacket
from lemoncrow.pro.capabilities.review.packet import build_review_packet, build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.render import render_review

# The plan §4.1 demo repository, byte for byte: `SessionManager.refresh(user)`
# gains a required `context` and the "expired" status literal becomes "invalid",
# while four untouched files keep calling the old shape.
_SESSION_BASE = '''"""Session management."""


class SessionManager:
    """Owns session lifecycle."""

    def __init__(self, store):
        self.store = store

    def refresh(self, user):
        """Refresh a user's session."""
        record = self.store.get(user)
        if record is None:
            return {"status": "expired"}
        return {"status": "ok", "user": user}
'''

_SESSION_CHANGED = '''"""Session management."""


class SessionManager:
    """Owns session lifecycle."""

    def __init__(self, store):
        self.store = store

    def refresh(self, user, context):
        """Refresh a user's session in *context*."""
        record = self.store.get(user)
        if record is None:
            return {"status": "invalid"}
        return {"status": "ok", "user": user, "context": context}
'''

# The whole third commit: two comment lines and a blank one, at the top of the
# file, three lines above `class SessionManager`. Nothing else moves.
_SESSION_COMMENTED = _SESSION_CHANGED.replace(
    '"""Session management."""\n\n',
    '"""Session management."""\n\n# NOTE: sessions are refreshed by the nightly job.\n'
    "# This comment changes no behaviour whatsoever.\n\n",
)

_CALLER = """from src.auth.session import SessionManager


def {fn}(store, user):
    manager = SessionManager(store)
    return manager.refresh(user)
"""

_FRONTEND = """export function render(state: {status: string}) {
  if (state.status === "expired") {
    return "Please log in again";
  }
  return "OK";
}
"""


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep ast-grep discovery from reaching its managed download path.

    ``sg`` is the one candidate name discovery refuses outright, so detection
    falls back to the language-agnostic text layer instead of pulling a binary
    into a throwaway repo. The defect under test is in the line arithmetic, which
    ast-grep has no part in.
    """

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    who = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000 + offset, 0)
    return str(repo.create_commit("HEAD", who, who, message, tree, parents))


def _fixture_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Build the three-commit demo repo; return ``(root, breaking_sha, comment_sha)``."""

    root = tmp_path / "syn"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"

    _write(root, "src/auth/session.py", _SESSION_BASE)
    _write(root, "src/api/login.py", _CALLER.format(fn="login"))
    _write(root, "jobs/session_cleanup.py", _CALLER.format(fn="cleanup"))
    _write(root, "workers/token_refresh.py", _CALLER.format(fn="refresh_all"))
    _write(root, "tests/test_session.py", _CALLER.format(fn="test_refresh"))
    _write(root, "frontend/session.ts", _FRONTEND)
    _commit(repo, "base", 0)

    _write(root, "src/auth/session.py", _SESSION_CHANGED)
    _write(
        root,
        "src/api/login.py",
        _CALLER.format(fn="login").replace("manager.refresh(user)", "manager.refresh(user, context)"),
    )
    breaking = _commit(repo, "session: refresh() now requires context; expired -> invalid", 60)

    _write(root, "src/auth/session.py", _SESSION_COMMENTED)
    comment = _commit(repo, "docs: add a comment; no code change", 120)
    return root, breaking, comment


def _index(root: Path) -> None:
    """Build the real symbol index in place.

    Not stubbed: the false headline was produced by the *interaction* between a
    real index's line ranges and the hunk geometry, and a hand-written fake would
    have encoded whichever geometry the author already believed. The repo is six
    files, so this costs about a second.
    """

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    engine = CodeContextEngine(root, autosync_enabled=False)
    engine.index_repo(force=True, require_lock=False)
    assert engine.index_ready(), "fixture index did not build"


def _packet(root: Path, sha: str, store_root: Path) -> ReviewPacket:
    rng = resolve_rev_range(root, f"{sha}~1", head=sha)
    return build_review_packet(root, rng, store_root=store_root, with_provenance=False)


def _render(root: Path, sha: str, store_root: Path) -> str:
    return render_review(_packet(root, sha, store_root), no_color=True)


# --- the acceptance criterion ------------------------------------------------


def test_comment_only_commit_produces_no_attention_at_all(tmp_path: Path) -> None:
    """A ``+3 -0`` pure-comment commit must reach ATTENTION with nothing to say."""

    root, _breaking, comment = _fixture_repo(tmp_path)
    _index(root)

    text = _render(root, comment, tmp_path / "store")

    assert "ATTENTION" not in text, text
    assert "SessionManager" not in text, text


def test_the_same_repo_still_reports_the_real_breaking_change(tmp_path: Path) -> None:
    """The commit before it must keep every finding, or the test above is worthless."""

    root, breaking, _comment = _fixture_repo(tmp_path)
    _index(root)

    text = _render(root, breaking, tmp_path / "store")

    assert "ATTENTION" in text, text
    assert "SessionManager.refresh(...) now requires: context" in text, text
    assert "jobs/session_cleanup.py:L6" in text, text
    assert "workers/token_refresh.py:L6" in text, text
    assert 'contract literal changed: "expired" → "invalid"' in text, text
    assert "frontend/session.ts:L2" in text, text


def test_signature_finding_names_the_file_it_came_out_of(tmp_path: Path) -> None:
    """A finding without a source file is one a reviewer cannot trace back.

    The literal finding printed ``(src/auth/session.py)`` and the signature
    finding beside it printed nothing, because attribution asked only "which
    changed file edited a line mentioning ``refresh``" — true of the definition
    *and* of the in-patch caller, so it answered "ambiguous". The file that
    *defines* the symbol is the one the contract lives in.
    """

    root, breaking, _comment = _fixture_repo(tmp_path)
    _index(root)

    text = _render(root, breaking, tmp_path / "store")
    headline = next(line for line in text.splitlines() if "now requires: context" in line)

    assert "(src/auth/session.py)" in headline, headline


def test_review_order_counts_what_attention_lists(tmp_path: Path) -> None:
    """The ranking's site count and the ATTENTION section must not disagree.

    Both are printed on the same screen, so a file ranked on "1 untouched
    impacted site(s)" above a finding listing three is the packet contradicting
    itself. The two numbers come from one fact — which changed file a site is a
    consequence of — and an unattributed site was silently credited to no file at
    all.
    """

    root, breaking, _comment = _fixture_repo(tmp_path)
    _index(root)

    rng = resolve_rev_range(root, f"{breaking}~1", head=breaking)
    packet = build_review_packet(root, rng, store_root=tmp_path / "store", with_provenance=False)

    outside = [site for site in packet.impact if not site.in_patch]
    # An out-of-patch site with no source file is counted by nobody: the ranker
    # scores changed files, and the site's *own* file is by definition not one.
    # That is how ATTENTION came to list three sites above a file ranked on one.
    assert [site.path for site in outside if not site.source_path] == []

    listed = sum(1 for site in outside if site.source_path == "src/auth/session.py")
    entry = next(item for item in packet.order if item.path == "src/auth/session.py")

    assert listed == len(outside), [(site.kind, site.path, site.source_path) for site in outside]
    assert f"{listed} untouched impacted site(s)" in entry.reasons, (entry.reasons, listed)


# --- the mechanism, pinned directly ------------------------------------------


def test_hunk_ranges_exclude_the_context_lines(tmp_path: Path) -> None:
    """The comment hunk must report only the three lines it added."""

    root, _breaking, comment = _fixture_repo(tmp_path)
    rng = resolve_rev_range(root, f"{comment}~1", head=comment)
    files = collect_diff(root, rng).files

    hunk = next(h for item in files if item.path == "src/auth/session.py" for h in item.hunks)

    # `@@ -1,5 +1,8 @@`: the printed hunk runs 1..8 and so covers `class
    # SessionManager` on line 7. The change is lines 3..5 and nothing else.
    assert hunk.new_start == 1
    assert hunk.new_lines == 8
    assert hunk.new_ranges == ((3, 5),)
    assert hunk.new_start + hunk.new_lines - 1 >= 7, "the printed span must still reach the class"
    assert all(high < 7 for _low, high in hunk.new_ranges)


def test_overlaps_tests_changed_lines_not_the_printed_span() -> None:
    """``workspace_key``'s own geometry, reduced to its arithmetic.

    New-side lines 56-85, against a hunk whose header spans 18-58 and whose
    additions stop at 55. The three lines of overlap were context — the entire
    false finding.
    """

    from lemoncrow.pro.capabilities.review.impact import _overlaps

    hunk = DiffHunk(
        old_start=18,
        old_lines=6,
        new_start=18,
        new_lines=41,
        header="@@ -18,6 +18,41 @@ class WorkspaceNotRegisteredError(RuntimeError):",
        added=35,
        removed=0,
        new_ranges=((21, 55),),
    )

    assert _overlaps(56, 85, hunk) is False
    assert _overlaps(40, 45, hunk) is True
    # An empty range set means "the patch body could not be read", which must
    # widen back to the printed span rather than silently drop the symbol.
    assert _overlaps(56, 85, DiffHunk(18, 6, 18, 41, "@@", 35, 0)) is True


def test_a_brand_new_symbol_is_not_reported_as_changed(tmp_path: Path) -> None:
    """An added definition must not be announced as a modification.

    The renderer hardcoded ``f"{old} changed"`` for every ``untouched_caller``
    finding and never read the ``change`` the producer had already set correctly,
    so a symbol that did not exist before the diff was reported as having been
    altered — inviting the reviewer to go looking for a before-state there is no.
    """

    from lemoncrow.pro.capabilities.review.render import _headline

    assert _headline("untouched_caller", "SessionManager", "added") == "SessionManager added"
    assert _headline("untouched_caller", "SessionManager", "modified") == "SessionManager changed"
    assert _headline("untouched_caller", "SessionManager", "deleted") == "SessionManager removed"
    # No verdict recorded: the neutral word, never a guess at which one it was.
    assert _headline("untouched_caller", "SessionManager", None) == "SessionManager changed"


def test_callers_absent_from_the_reviewed_revision_are_dropped(tmp_path: Path) -> None:
    """The index is keyed to the workspace, not to the commit under review.

    A file added *after* the reviewed commit is still in the on-disk index, so it
    was cited as an untouched call site of that commit's change — a location the
    reviewer opens and does not find. Here ``workers/token_refresh.py`` is
    deleted after the breaking commit, then the index is built: reviewing the
    breaking commit must still name it, because at that revision it existed.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import head_path_filter

    root, breaking, comment = _fixture_repo(tmp_path)
    (root / "workers" / "token_refresh.py").unlink()
    repo = pygit2.Repository(str(root))
    repo.index.remove("workers/token_refresh.py")
    after = _commit(repo, "drop the worker", 180)

    at_breaking = head_path_filter(root, resolve_rev_range(root, f"{breaking}~1", head=breaking))
    at_after = head_path_filter(root, resolve_rev_range(root, f"{after}~1", head=after))

    assert at_breaking("workers/token_refresh.py") is True
    assert at_breaking("no/such/file.py") is False
    assert at_after("workers/token_refresh.py") is False
    assert comment  # the middle commit is unused here; named for readability


# --- the index and the reviewed revision are two coordinate systems -----------
#
# Everything above pins the *hunk* side of the arithmetic. This part pins the
# other operand. The index is keyed to the workspace on disk; a hunk's line
# numbers belong to the revision under review. Intersecting the two was a
# comparison across coordinate systems, and the damage was not symmetric noise:
# when the file had shifted between them the packet reported a byte-identical
# definition as changed *and dropped the one that had actually changed*, with the
# footer still reading `index: fresh`. The omission is the worse half and the
# half no earlier fix measured, so both directions are asserted below.

_SHIFT_HEADER_LINES = 60

_SHIFT_BODY_BASE = '''

def func_a(x):
    """A."""
    return x + 1


def func_b(x):
    """B."""
    return func_a(x) * 2


def func_c(x):
    """C."""
    total = 0
    for i in range(x):
        total += i
    return total


def func_d(x):
    """D — nobody touches me, ever."""
    scaled = func_b(x)
    return scaled - 1
'''

_SHIFT_BODY_CHANGED = _SHIFT_BODY_BASE.replace(
    '''def func_c(x):
    """C."""
    total = 0
    for i in range(x):
        total += i
    return total''',
    '''def func_c(x):
    """C, rewritten."""
    return sum(range(x)) + 99''',
)

_SHIFT_CALLER = """from lib.core import func_d, func_b


def {fn}(x):
    return func_d(x) + func_b(x)
"""


def _shift_repo(tmp_path: Path, trim: int) -> tuple[Path, str]:
    """Three commits; return ``(root, sha_of_the_commit_to_review)``.

    ``c0`` lays down a 60-line header and four functions, ``c1`` rewrites
    ``func_c`` and nothing else, ``c2`` deletes *trim* header lines. The index is
    built at ``c2`` -- the workspace -- and ``c1`` is what gets reviewed, so every
    symbol sits ``trim`` lines lower in the reviewed blob than the index believes.

    ``func_d`` is byte-identical across ``c1`` and has four untouched callers,
    which is what turns a line-number slip into a full fabricated ATTENTION
    headline rather than a quiet mislabel.
    """

    root = tmp_path / "shift"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"

    header = "\n".join(f"# header line {i}" for i in range(1, _SHIFT_HEADER_LINES + 1))
    _write(root, "lib/core.py", header + _SHIFT_BODY_BASE)
    for name in ("one", "two", "three", "four"):
        _write(root, f"app/{name}.py", _SHIFT_CALLER.format(fn=name))
    _commit(repo, "base", 0)

    _write(root, "lib/core.py", header + _SHIFT_BODY_CHANGED)
    reviewed = _commit(repo, "perf: rewrite func_c", 60)

    shrunk = "\n".join(f"# header line {i}" for i in range(1, _SHIFT_HEADER_LINES + 1 - trim))
    _write(root, "lib/core.py", shrunk + _SHIFT_BODY_CHANGED)
    _commit(repo, "chore: trim the header", 120)
    return root, reviewed


@pytest.mark.parametrize("trim", list(range(11)))
def test_a_shifted_workspace_neither_fabricates_nor_drops(tmp_path: Path, trim: int) -> None:
    """Both halves, at every offset the header can shift by.

    Before the fix this passed only at ``trim`` 0-2, and by luck: at 3-7 the
    packet announced ``⚠ func_d changed`` with four fabricated untouched call
    sites, and at 2 and 8-10 it reported nothing at all. The real edit to
    ``func_c`` was reported at 0 and 1 only.

    The absence assertion alone is worthless -- reporting nothing satisfies it --
    so the presence assertion is what makes this a test rather than a mute
    button.
    """

    root, reviewed = _shift_repo(tmp_path, trim)
    _index(root)

    rng = resolve_rev_range(root, f"{reviewed}~1", head=reviewed)
    packet = build_review_packet(root, rng, store_root=tmp_path / "store", with_provenance=False)
    text = render_review(packet, no_color=True)
    names = {symbol.symbol_name for symbol in packet.symbols}

    assert "func_c" in names, (trim, sorted(names), text)
    assert "func_d" not in names, (trim, sorted(names), text)
    # The fabricated headline and its four call sites, denied at the surface the
    # reviewer actually reads.
    assert "func_d" not in text, (trim, text)
    assert [site.path for site in packet.impact if site.old == "func_d"] == [], (trim, text)


@pytest.mark.parametrize("trim", [0, 1, 6, 10])
def test_the_footer_admits_the_shift_it_is_reading_across(tmp_path: Path, trim: int) -> None:
    """``index: fresh`` over a shifted index is the packet lying about itself.

    Staleness was probed by asking "does an indexed symbol end past the end of
    the file?", which can only fire when the workspace copy is *longer* than the
    reviewed one. Every shift in the other direction -- a deletion, which is half
    of all diffs -- was invisible, so the footer certified a packet built from
    mismatched line numbers. The probe is now content-based and symmetric.
    """

    root, reviewed = _shift_repo(tmp_path, trim)
    _index(root)

    rng = resolve_rev_range(root, f"{reviewed}~1", head=reviewed)
    packet = build_review_packet(root, rng, store_root=tmp_path / "store", with_provenance=False)

    assert packet.index_status == ("fresh" if trim == 0 else "stale"), (trim, packet.degraded)


def test_outline_drift_is_detected_in_both_directions() -> None:
    """The probe, reduced to its arithmetic.

    A row that no longer finds its own name on the line it claims is drift,
    whether the file grew or shrank. The old ``line_end > total`` test answered
    False for the shrinking case, which is the one the fixture above reproduces.
    """

    from lemoncrow.pro.capabilities.review.impact import _outline_drifted

    blob = "# a\n# b\ndef func_c(x):\n    return x\n"
    fresh = [{"name": "func_c", "line_start": 3, "line_end": 4}]
    shifted_up = [{"name": "func_c", "line_start": 1, "line_end": 2}]
    past_the_end = [{"name": "func_c", "line_start": 9, "line_end": 10}]

    assert _outline_drifted(fresh, blob) is False
    # Shorter-or-equal drift: entirely inside the file, and entirely wrong.
    assert _outline_drifted(shifted_up, blob) is True
    assert _outline_drifted(past_the_end, blob) is True
    # No blob is not evidence of drift; `blob_unreadable` already reports that.
    assert _outline_drifted(fresh, "") is False


def test_a_caller_file_that_does_not_name_the_symbol_is_dropped(tmp_path: Path) -> None:
    """File-granular was never enough.

    ``head_path_filter`` asks only whether the file exists at the reviewed
    revision. A file that exists there but whose copy at that revision never
    mentions the symbol -- the call was added later -- sends the reviewer to the
    same dead end as a missing file, one step further in.

    The filter answers ``(path, symbol, caller) -> int | None``: ``None`` to drop
    the site, a positive line -- in the reviewed revision -- to print beside it,
    and ``0`` for "keep it, but nothing here knows where".
    """

    from lemoncrow.pro.capabilities.review.gitdiff import head_symbol_filter

    root, breaking, _comment = _fixture_repo(tmp_path)
    repo = pygit2.Repository(str(root))
    _write(root, "jobs/session_cleanup.py", "# the call arrives in a later commit\n")
    later = _commit(repo, "jobs: stub out the cleanup", 180)

    at_breaking = head_symbol_filter(root, resolve_rev_range(root, f"{breaking}~1", head=breaking))
    at_later = head_symbol_filter(root, resolve_rev_range(root, f"{later}~1", head=later))

    # At the breaking commit the file exists, names the symbol, and the line it
    # answers with is `def cleanup` in *that* revision's copy -- line 4 of
    # `_CALLER`, whatever the workspace has since done to the file.
    assert _CALLER.format(fn="cleanup").splitlines()[3] == "def cleanup(store, user):"
    assert at_breaking("jobs/session_cleanup.py", "SessionManager", "cleanup") == 4
    assert at_breaking("jobs/session_cleanup.py", "NoSuchSymbol", "cleanup") is None
    assert at_breaking("no/such/file.py", "SessionManager", "cleanup") is None
    # At the later commit the file is still there; the call is not.
    assert at_later("jobs/session_cleanup.py", "SessionManager", "cleanup") is None
    # A non-identifier has no safe word-boundary spelling: waved through rather
    # than mis-matched, since a deleted real finding costs more than a kept one.
    # Kept means "not None"; the stub file names neither caller nor symbol, so
    # there is no line to claim and it says so with 0 rather than inventing one.
    assert at_later("jobs/session_cleanup.py", "status == 'expired'", "cleanup") == 0


# --- the caller side of the same coordinate mistake --------------------------

# `compute_total` is rewritten in the commit under review; the four files that
# call it are untouched by it and shift thirty lines down one commit later. The
# index is built last, so every caller line it holds belongs to a revision the
# reviewer is not reading.
_DRIFT_CORE = '''"""Core."""


def helper(x):
    return x + 1


def compute_total(items):
    """Sum."""
    total = 0
    for item in items:
        total += helper(item)
    return total
'''

_DRIFT_CORE_CHANGED = _DRIFT_CORE.replace(
    "    total = 0\n    for item in items:\n        total += helper(item)\n    return total",
    "    return sum(helper(i) for i in items) + 7",
)

_DRIFT_CALLER = """from lib.core import compute_total


def {fn}(items):
    return compute_total(items)
"""

_DRIFT_PADDING = "".join(f"# padding line {i}\n" for i in range(1, 31))

_DRIFT_CALLERS = ("one", "two", "three", "four")


def _drift_repo(tmp_path: Path) -> tuple[Path, str]:
    """Build the caller-drift repo; return ``(root, reviewed_sha)``."""

    root = tmp_path / "drift"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "<redacted-email>"

    _write(root, "lib/core.py", _DRIFT_CORE)
    for name in _DRIFT_CALLERS:
        _write(root, f"app/{name}.py", _DRIFT_CALLER.format(fn=name))
    _commit(repo, "base", 0)

    _write(root, "lib/core.py", _DRIFT_CORE_CHANGED)
    reviewed = _commit(repo, "perf: rewrite compute_total", 60)

    for name in _DRIFT_CALLERS:
        _write(root, f"app/{name}.py", _DRIFT_PADDING + _DRIFT_CALLER.format(fn=name))
    _commit(repo, "chore: pad the callers", 120)
    return root, reviewed


def test_every_cited_caller_line_resolves_in_the_reviewed_revision(tmp_path: Path) -> None:
    """Gating *which* files may be cited was never enough; *where* was still the workspace's.

    A file that survives into the reviewed revision and still names the symbol
    passes every existence guard, and the packet then printed the line number the
    index holds for it — a workspace coordinate. Pad the callers thirty lines
    after the commit under review and all four citations land past the end of the
    file the reviewer opens, under a footer reading ``index: fresh``.

    Two assertions, because either alone is passable by cheating: every cited
    line must exist in the reviewed blob *and* name the caller it claims, and the
    footer must not offer an unqualified ``fresh`` over geometry that had to be
    corrected to get there.
    """

    root, reviewed = _drift_repo(tmp_path)
    _index(root)
    repo = pygit2.Repository(str(root))

    packet = _packet(root, reviewed, tmp_path / "store")
    text = render_review(packet, no_color=True)

    sites = [site for site in packet.impact if site.kind == "untouched_caller"]
    assert len(sites) == len(_DRIFT_CALLERS), text
    for site in sites:
        path, _, raw_line = site.path.partition(":L")
        assert raw_line, f"no line at all for {site.path}"
        blob = (repo.revparse_single(reviewed).tree / path).data.decode("utf-8")
        lines = blob.splitlines()
        line = int(raw_line)
        assert 1 <= line <= len(lines), f"{site.path} is past the {len(lines)} lines that revision has"
        assert site.snippet.rpartition(".")[2] in lines[line - 1], f"{site.path} does not name {site.snippet!r}"

    # Concretely: `def one` sits at line 4 in the reviewed revision and line 34
    # in the indexed workspace, and the packet is about the former.
    assert "app/one.py:L4" in text, text
    assert "app/one.py:L34" not in text, text
    # `_outline_drifted` walks only the *changed* files, so nothing it can see
    # would have caught this. The caller-side signal is what keeps the footer
    # honest about geometry the index got wrong.
    assert "caller_line_ranges" in packet.degraded, packet.degraded


# --- a definition ends at its body, not at the next definition's decorators ---

_COMMANDS_BASE = '''import click


@click.group()
def worker_group():
    """Worker commands."""


@worker_group.command("start")
@click.pass_context
def worker_start(ctx):
    """Start the background worker loop."""
    click.echo("worker started")


@worker_group.command("run-once")
@click.pass_context
def worker_run_once(ctx):
    """Claim and process one pending job then exit."""
    click.echo("processed")
'''

_COMMANDS_CHANGED = _COMMANDS_BASE.replace(
    '@worker_group.command("run-once")\n@click.pass_context\ndef worker_run_once(ctx):\n'
    '    """Claim and process one pending job then exit."""\n    click.echo("processed")',
    '@worker_group.command("run-once")\n@click.option("--json", "as_json", is_flag=True)\n'
    "@click.pass_context\ndef worker_run_once(ctx, as_json):\n"
    '    """Claim and process one pending job then exit."""\n'
    '    click.echo("{}" if as_json else "processed")',
)


def test_a_decorator_added_to_the_next_command_leaves_the_one_above_it_alone(tmp_path: Path) -> None:
    """``Tag.line`` is the ``def``, so the decorators above it belonged to nobody.

    Giving each definition the span up to the *next definition's line* handed
    that next definition's decorator stack to the one before it. Adding a single
    ``@click.option(...)`` to ``worker_run_once`` therefore reported
    ``worker_start`` as changed — byte-identical across the commit, and immune to
    the ``body in baseline`` guard precisely because the body it was compared
    against had just grown the added line.

    The shape is the real one this was found in: two decorated click commands,
    the second gaining an option.
    """

    root = tmp_path / "cli"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "<redacted-email>"

    _write(root, "cli/servicectl.py", _COMMANDS_BASE)
    _write(root, "cli/caller.py", "from cli.servicectl import worker_start\n\n\ndef boot():\n    worker_start()\n")
    _commit(repo, "base", 0)

    _write(root, "cli/servicectl.py", _COMMANDS_CHANGED)
    added = _commit(repo, "worker run-once: add --json", 60)
    _index(root)

    # The precondition the assertion rests on: `worker_start` is untouched.
    start = "def worker_start(ctx):"
    base_body = _COMMANDS_BASE[_COMMANDS_BASE.index(start) :].split("\n\n\n")[0]
    assert base_body in _COMMANDS_CHANGED, "fixture no longer holds worker_start byte-identical"

    packet = _packet(root, added, tmp_path / "store")
    text = render_review(packet, no_color=True)
    names = {symbol.symbol_name for symbol in packet.symbols}

    assert "worker_start" not in names, sorted(names)
    assert "worker_start" not in text, text
    # And the definition that really did change is still named, or the test above
    # is satisfied by reporting nothing.
    assert "worker_run_once" in names, sorted(names)


# --- what the index does not know, it may not subtract -----------------------


def test_a_definition_the_index_has_no_row_for_is_never_silently_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-walked outline used to delete real findings without a word.

    Measured on this repository's ``983c233f2``: the reviewed blob of
    ``api.py`` defines 223 symbols, the index's outline held 94 rows, and the
    eleven hunk-touched definitions with no row among them were dropped where
    they stood — the packet reported *zero* symbols for a file whose
    ``create_app`` had grown by 3309 characters, under ``index: fresh``.

    The row is missing here rather than the whole outline, because "the index
    answered, incompletely" is the case that had no code path: a null outline
    already degrades ``symbol_relations`` and falls back to tree-sitter for the
    whole file.
    """

    from lemoncrow.pro.capabilities.review import impact as impact_module

    root, breaking, _comment = _fixture_repo(tmp_path)
    _index(root)

    full_outline = impact_module._outline_entries

    def _outline_without_refresh(engine: Any, path: str) -> list[dict[str, Any]] | None:
        rows = full_outline(engine, path)
        if rows is None:
            return None
        return [row for row in rows if row.get("name") != "refresh"]

    monkeypatch.setattr(impact_module, "_outline_entries", _outline_without_refresh)

    packet = _packet(root, breaking, tmp_path / "store")
    changed = {symbol.symbol_name: symbol for symbol in packet.symbols}

    assert "refresh" in changed, sorted(changed)
    # Named from the blob, so: no qualified name and no graph enrichment, because
    # those are the parts only the index knows.
    assert changed["refresh"].source == "tree_sitter"
    assert changed["refresh"].qualified_name is None
    # Reported *and* disclosed: the packet is under-informed here and says so.
    assert "index_outline_incomplete" in packet.degraded, packet.degraded


# --- a definition ends at its body, not at the next definition's prelude ------

# The window's *top* was fixed first: `Tag.line` is the `def`, so the decorators
# and comments stacked above the next definition had been handed to the one
# before it. Its *bottom* was still "the line before that prelude", which is not
# where a body ends either -- and everything that is neither a definition nor a
# prelude falls in the gap between the two. That is not an exotic shape: it is
# the try/except import guard, the `if` block configuring a logger, the bare
# registration call, the `for` loop and the `with` block that open half the
# modules in this repository. Adding one reported the byte-identical definition
# *above* it as changed, with its real untouched callers attached and nothing in
# `degraded` to hint the finding was manufactured -- and the `body in baseline`
# guard could not catch it, because the body it compared had just grown the
# added line.
_GAP_HEAD = '''"""Core."""

import logging

REGISTRY: dict[str, bool] = {}


def target(items):
    """Byte-identical across the commit under review. Four untouched callers."""
    return sum(items)
'''

_GAP_TAIL = """

def other(items):
    return target(items)
"""

# The genuinely changed definition, edited in the same commit. Without it,
# "report nothing at all" would pass every assertion below.
_GAP_TAIL_CHANGED = _GAP_TAIL.replace("return target(items)", "return target(items) * 2")

_GAP_STATEMENTS = {
    "bare_registration_call": 'REGISTRY.update({"fast": True})\n',
    "for_loop": 'for _name in ("alpha", "beta"):\n    REGISTRY[_name] = True\n',
    "if_block": "if not logging.getLogger(__name__).handlers:\n"
    "    logging.getLogger(__name__).setLevel(logging.INFO)\n",
    "try_except_import_guard": "try:\n    import orjson as _json\nexcept ImportError:  # pragma: no cover\n"
    "    import json as _json\n",
    "with_block": 'with open("/dev/null") as _fh:\n    _fh.write("boot")\n',
}

_GAP_CALLER = """from lib.core import target


def {fn}(items):
    return target(items)
"""

_GAP_CALLERS = ("one", "two", "three", "four")


def _gap_repo(tmp_path: Path, statement: str) -> tuple[Path, str]:
    """Add *statement* in the gap below ``target``, and edit ``other`` too."""

    root = tmp_path / "gap"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"

    _write(root, "lib/core.py", _GAP_HEAD + _GAP_TAIL)
    for name in _GAP_CALLERS:
        _write(root, f"app/{name}.py", _GAP_CALLER.format(fn=name))
    _commit(repo, "base", 0)

    _write(root, "lib/core.py", _GAP_HEAD + "\n\n" + statement + _GAP_TAIL_CHANGED)
    return root, _commit(repo, "core: wire up the module-level block; other() doubles", 60)


@pytest.mark.parametrize("shape", sorted(_GAP_STATEMENTS))
def test_a_statement_added_below_a_definition_is_not_that_definition(tmp_path: Path, shape: str) -> None:
    """Five ordinary Python shapes, each of which manufactured an ATTENTION headline.

    ``target`` is byte-identical across the commit and has four untouched
    callers, so if its window still runs past its own body every one of these
    prints ``⚠ target changed`` over four files nobody needs to open. The same
    commit really does change ``other``, which is what stops "report nothing"
    from being a passing answer.
    """

    root, added = _gap_repo(tmp_path, _GAP_STATEMENTS[shape])
    _index(root)

    # The precondition the assertions rest on.
    head_text = _GAP_HEAD + "\n\n" + _GAP_STATEMENTS[shape] + _GAP_TAIL_CHANGED
    definition = "def target(items):"
    body = _GAP_HEAD[_GAP_HEAD.index(definition) :]
    assert body in head_text, "fixture no longer holds target byte-identical"

    packet = _packet(root, added, tmp_path / "store")
    text = render_review(packet, no_color=True)
    names = {symbol.symbol_name for symbol in packet.symbols}

    assert "target" not in names, sorted(names)
    assert "target" not in text, text
    # Nothing may be cited on account of it either.
    assert [site.path for site in packet.impact if site.kind == "untouched_caller"] == [], text
    # And the definition that really changed is still reported.
    assert "other" in names, sorted(names)


_NESTED_BASE = '''"""Core."""


def target(items):
    """Byte-identical across the commit under review."""
    return sum(items)


def outer(items):
    def _scale(value):
        return value * 2

    total = _scale(len(items))
    return total + 1
'''

_NESTED_CHANGED = _NESTED_BASE.replace("    total = _scale(len(items))", "    total = _scale(len(items)) + 100")


def test_an_edit_after_a_nested_def_belongs_to_the_parent(tmp_path: Path) -> None:
    """The same arithmetic, one level in -- and it cost a finding as well as inventing one.

    "Up to the next definition" hands a nested ``def`` the remainder of its
    parent's body, so an edit below it was attributed to the nested function
    (which has no callers, so it stopped at ``packet.symbols``) while ``outer``,
    whose body actually changed, was never named at all. Both halves are asserted
    here: a window that is merely narrower would fix the fabrication by dropping
    the real finding too.
    """

    root = tmp_path / "nested"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"

    _write(root, "lib/core.py", _NESTED_BASE)
    for name in _GAP_CALLERS:
        _write(root, f"app/{name}.py", _GAP_CALLER.format(fn=name))
    _commit(repo, "base", 0)

    _write(root, "lib/core.py", _NESTED_CHANGED)
    edited = _commit(repo, "outer: scale the total", 60)
    _index(root)

    packet = _packet(root, edited, tmp_path / "store")
    names = {symbol.symbol_name for symbol in packet.symbols}

    assert "outer" in names, sorted(names)
    assert "_scale" not in names, sorted(names)
    assert "target" not in names, sorted(names)


# --- an anchor may not point at an import, or into the wrong class ------------

_TWO_CLASSES = '''"""Two classes, one method name."""

from lib.core import target


class Alpha:
    def close(self):
        return 1

    def run(self):
        return 2


class Beta:
    def run(self):
        return target(3)
'''


def test_a_caller_anchor_never_lands_on_an_import_or_a_foreign_class() -> None:
    """Both remaining ways the printed line could name something it is not.

    The last-resort anchor was "any mention of the changed symbol", and in a
    caller file that is, almost always, the ``import`` line -- printed under the
    caller's own name, three lines above a class it has nothing to do with. And
    the class-scoped search, which exists precisely because ``run`` matches every
    class in the file, retried from line 0 when the scope came up empty, handing
    back the very namesake the scope was found to exclude.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import _anchor_line

    # The scope works: `Beta.run` is line 15, `Alpha.run` line 10, `close` line 7.
    assert _anchor_line(_TWO_CLASSES, "Beta.run") == 15
    assert _anchor_line(_TWO_CLASSES, "Alpha.run") == 10
    assert _anchor_line(_TWO_CLASSES, "Alpha.close") == 7
    # `Beta` has no `close`. Line 7 -- `Alpha`'s -- is precisely the answer the
    # class scope exists to rule out, and is what the retry from line 0 returned.
    assert _anchor_line(_TWO_CLASSES, "Beta.close") == 0
    # A caller this file does not define at all is a bare path, not line 3: the
    # ``import`` is the only place a caller file spells the changed symbol, and
    # printing it under the caller's name says the call is somewhere it is not.
    assert _anchor_line(_TWO_CLASSES, "nowhere") == 0


# ---------------------------------------------------------------------------
# hunk bodies -- the content a hunk fingerprint is taken over
# ---------------------------------------------------------------------------


def _one_file_repo(tmp_path: Path, before: str, after: str, *, rel: str = "src/mod.py") -> Path:
    root = tmp_path / "hunkbody"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.test"
    _write(root, rel, before)
    _commit(repo, "base", 0)
    _write(root, rel, after)
    _commit(repo, "change", 60)
    return root


def test_diff_hunk_patch_round_trips_the_exact_hunk_body(tmp_path: Path) -> None:
    """The body is the ``+``/``-``/context lines verbatim, and nothing else.

    Not the ``@@`` header: that is already ``DiffHunk.header``, and it carries
    line numbers, which a content fingerprint must never depend on.
    """

    root = _one_file_repo(
        tmp_path,
        "def one():\n    return 1\n",
        "def one():\n    return 2\n",
    )
    rng = resolve_rev_range(root, "HEAD~1")
    hunks = [hunk for item in collect_diff(root, rng, with_patch_text=True).files for hunk in item.hunks]
    assert len(hunks) == 1
    assert hunks[0].patch == " def one():\n-    return 1\n+    return 2\n"
    assert not hunks[0].patch.startswith("@@")
    assert hunks[0].header.startswith("@@")


def test_a_hunk_body_over_the_cap_is_dropped_and_named_in_degraded(tmp_path: Path) -> None:
    """A half-patch that still looks like a patch is worse than no patch."""

    from lemoncrow.pro.capabilities.review.gitdiff import MAX_HUNK_PATCH_BYTES

    lines = MAX_HUNK_PATCH_BYTES // 8
    before = "x = 0\n"
    after = "".join(f"value_{index} = {index}\n" for index in range(lines))
    root = _one_file_repo(tmp_path, before, after)

    rng = resolve_rev_range(root, "HEAD~1")
    result = collect_diff(root, rng, with_patch_text=True)
    hunks = [hunk for item in result.files for hunk in item.hunks]
    assert hunks
    assert all(hunk.patch == "" for hunk in hunks)
    assert "hunk_patch_truncated" in result.degraded
    # The geometry survives: an over-cap hunk is still a reviewable hunk.
    assert any(hunk.new_ranges for hunk in hunks)


def test_persisted_human_review_can_capture_a_hunk_over_the_machine_packet_cap(tmp_path: Path) -> None:
    """The machine packet stays bounded; the human review surface must be complete."""

    from lemoncrow.pro.capabilities.review.gitdiff import MAX_HUNK_PATCH_BYTES

    lines = MAX_HUNK_PATCH_BYTES // 8
    root = _one_file_repo(
        tmp_path,
        "x = 0\n",
        "".join(f"value_{index} = {index}\n" for index in range(lines)),
    )
    build = build_review_packet_with_blobs(
        root,
        resolve_rev_range(root, "HEAD~1"),
        store_root=tmp_path / "store",
        with_patch_text=True,
        unbounded_patch_text=True,
    )
    hunks = [hunk for item in build.packet.files for hunk in item.hunks]
    assert hunks
    assert all(hunk.patch for hunk in hunks)
    assert "hunk_patch_truncated" not in build.packet.degraded
    assert sum(len(hunk.patch.encode("utf-8")) for hunk in hunks) > MAX_HUNK_PATCH_BYTES


def test_a_hunk_under_the_cap_is_never_reported_as_truncated(tmp_path: Path) -> None:
    root = _one_file_repo(tmp_path, "def one():\n    return 1\n", "def one():\n    return 2\n")
    result = collect_diff(root, resolve_rev_range(root, "HEAD~1"), with_patch_text=True)
    assert "hunk_patch_truncated" not in result.degraded


def test_capturing_hunk_bodies_changes_nothing_else_about_the_diff(tmp_path: Path) -> None:
    """Constraint 6: ``lc review`` with no flags must not move."""

    from dataclasses import replace

    root = _one_file_repo(tmp_path, "def one():\n    return 1\n", "def one():\n    return 2\n")
    rng = resolve_rev_range(root, "HEAD~1")
    plain = collect_diff(root, rng)
    captured = collect_diff(root, rng, with_patch_text=True)

    stripped = tuple(
        replace(item, hunks=tuple(replace(hunk, patch="") for hunk in item.hunks)) for item in captured.files
    )
    assert stripped == plain.files
    assert captured.degraded == plain.degraded


# ---------------------------------------------------------------------------
# a replacement names its own new-side position
# ---------------------------------------------------------------------------


def test_a_replacement_does_not_charge_the_context_line_above_it(tmp_path: Path) -> None:
    """``-x`` immediately followed by ``+y`` touches ``y``'s line and no other.

    A deletion sits *between* two new-side lines, so a pure deletion has to
    record the pair around it or it would be invisible to a new-side range test.
    A delete+add replacement is not that case: the added line already occupies
    the position, and recording the cursor as well charges the last unchanged
    *context* line above the edit. Where definitions abut without a blank line,
    that context line is the last line of the definition above -- so a one-line
    edit to ``beta`` reported byte-identical ``alpha`` as modified, gave it its
    own symbol unit, and sent the reviewer to read untouched code.
    """

    root = _one_file_repo(
        tmp_path,
        "def alpha():\n    return 1\ndef beta():\n    return 2\n",
        "def alpha():\n    return 1\ndef beta(x):\n    return 2\n",
    )
    rng = resolve_rev_range(root, "HEAD~1")
    hunk = next(h for item in collect_diff(root, rng).files for h in item.hunks)

    # Line 3 is `def beta(x):`; lines 1-2 are `alpha` and are byte-identical.
    assert hunk.new_ranges == ((3, 3),)

    from lemoncrow.pro.capabilities.review.units import _touches_hunk

    assert _touches_hunk(3, 4, hunk) is True, "beta must still be reported"
    assert _touches_hunk(1, 2, hunk) is False, "alpha was not touched"


def test_a_file_with_no_trailing_newline_gets_the_same_precision(tmp_path: Path) -> None:
    """libgit2's end-of-file-newline markers must not close the changed-line run.

    When the last line of a newline-less file is replaced, libgit2 emits the
    ``\\ No newline at end of file`` markers (origins ``>`` and ``<``) between
    the ``-`` and the ``+``. Treating them as context closed the deletion run
    before the addition joined it, so the run looked like a bare deletion and
    charged the context line above -- the very false positive the run-based
    join point removes. Files with no trailing newline are ordinary (generated
    JSON, TS, config), so this is the common case, not a curiosity.
    """

    root = _one_file_repo(
        tmp_path,
        "def alpha():\n    return 1\ndef beta(): pass",
        "def alpha():\n    return 1\ndef beta(x): pass",
    )
    rng = resolve_rev_range(root, "HEAD~1")
    hunk = next(h for item in collect_diff(root, rng).files for h in item.hunks)

    assert hunk.new_ranges == ((3, 3),)

    from lemoncrow.pro.capabilities.review.units import _touches_hunk

    assert _touches_hunk(3, 3, hunk) is True, "beta must still be reported"
    assert _touches_hunk(1, 2, hunk) is False, "alpha was not touched"


def test_a_pure_deletion_still_reports_the_lines_it_sits_between(tmp_path: Path) -> None:
    """The half the replacement fix must not eat: a deletion has no line of its own.

    Nothing on the new side carries the removed content, so the join point is
    the only way a new-side range test can see that anything happened there.
    """

    root = _one_file_repo(
        tmp_path,
        "one\ntwo\nthree\nfour\nfive\n",
        "one\ntwo\nfour\nfive\n",
    )
    rng = resolve_rev_range(root, "HEAD~1")
    hunk = next(h for item in collect_diff(root, rng).files for h in item.hunks)

    assert (hunk.added, hunk.removed) == (0, 1)
    assert hunk.new_ranges == ((2, 3),)


# ---------------------------------------------------------------------------
# what the worktree reader may open
# ---------------------------------------------------------------------------


def _repo_with_symlink(tmp_path: Path, link_name: str, target: Path) -> Path:
    root = tmp_path / "linked"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.test"
    _write(root, "keep.txt", "keep\n")
    _commit(repo, "base", 0)
    os.symlink(str(target), str(root / link_name))
    return root


def test_a_symlink_is_read_as_its_target_path_never_as_the_target_file(tmp_path: Path) -> None:
    """Git stores a symlink's blob as its target *path*; the worktree read must too.

    ``Path.stat()`` and ``Path.read_bytes()`` both follow links, so a working-tree
    review of a repo containing ``ln -s ~/.ssh/id_rsa key`` recorded the target
    file's bytes as the link's new-side text -- into the packet, into the blob
    artifact under ``.lemoncrow/``, and into the fingerprint taken over it. The
    same tree reviewed with ``--staged`` read the link text from the index, so
    the two modes disagreed about the same file.
    """

    outside = tmp_path / "outside_secret.txt"
    outside.write_text("OUTSIDE-REPO-SECRET\n", encoding="utf-8")
    root = _repo_with_symlink(tmp_path, "escape.txt", outside)

    worktree_rng = resolve_rev_range(root)
    worktree = load_blobs(root, worktree_rng, collect_diff(root, worktree_rng).files)
    assert worktree.new["escape.txt"] == str(outside)
    assert "OUTSIDE-REPO-SECRET" not in worktree.new["escape.txt"]

    repo = pygit2.Repository(str(root))
    repo.index.add_all()
    repo.index.write()
    staged_rng = resolve_rev_range(root, staged=True)
    staged = load_blobs(root, staged_rng, collect_diff(root, staged_rng).files)
    assert staged.new["escape.txt"] == worktree.new["escape.txt"], "the two modes must agree"


def test_a_symlink_to_a_fifo_is_never_opened(tmp_path: Path) -> None:
    """``open()`` on a FIFO blocks forever, somewhere ``except OSError`` cannot reach.

    The alarm turns the regression from a hang -- which a CI run can only be
    killed out of -- into a failed assertion: ``TimeoutError`` is an ``OSError``,
    so a reader that opened the pipe would report ``blob_unreadable`` here rather
    than the link text.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import _blob_from_disk

    fifo = tmp_path / "thefifo"
    os.mkfifo(str(fifo))
    root = _repo_with_symlink(tmp_path, "pipe.txt", fifo)

    def _refuse_to_block(_signum: int, _frame: Any) -> None:
        raise TimeoutError("blocked inside _blob_from_disk")

    previous = signal.signal(signal.SIGALRM, _refuse_to_block)
    signal.alarm(5)
    try:
        data, reason = _blob_from_disk(root, "pipe.txt")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    assert reason == ""
    assert data == os.fsencode(str(fifo))


# ---------------------------------------------------------------------------
# a blob nobody read is absent, not empty
# ---------------------------------------------------------------------------


def test_an_unread_new_side_blob_is_absent_and_its_unit_cannot_read_as_reviewed(tmp_path: Path) -> None:
    """``load_blobs`` stored ``""`` for a file it skipped, and the frontier believed it.

    Because the key was present, ``units._file_unit`` took the ``blob_sha256``
    branch and fingerprinted the file over empty text -- a per-path constant that
    does not move however the file is rewritten. The reviewer could mark a 946 KB
    lockfile ``reviewed`` with no downgrade note, the agent could regenerate it
    end to end, and the tree fingerprint would not change.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import MAX_BLOB_BYTES
    from lemoncrow.pro.capabilities.review.sources.local import effective_mark_state
    from lemoncrow.pro.capabilities.review.units import derive_units, file_fingerprint

    root = tmp_path / "oversize"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.test"
    _write(root, "uv.lock", "x = 0\n")
    _commit(repo, "base", 0)
    _write(root, "uv.lock", "y = 1\n" * ((MAX_BLOB_BYTES // 6) + 100))

    rng = resolve_rev_range(root)
    result = collect_diff(root, rng)
    blobs = load_blobs(root, rng, result.files)

    assert "large_file_skipped" in blobs.degraded
    assert "uv.lock" not in blobs.new, "a blob we did not read has no text, not empty text"

    build = build_review_packet_with_blobs(root, rng, store_root=tmp_path / "store", with_impact=False)
    unit = next(item for item in derive_units(build.packet, build.blobs.new) if item.kind == "file")
    assert unit.path == "uv.lock"
    assert unit.fingerprint_method == "unknown"
    assert unit.content_fingerprint != file_fingerprint("uv.lock", "")
    assert effective_mark_state(unit, "reviewed") == "unknown"


# ---------------------------------------------------------------------------
# rows and statuses the packet may not invent
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Fixture Tester", "-c", "user.email=fixture@example.test", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    ).stdout


def test_a_shallow_clone_refuses_rather_than_reviewing_the_whole_repository(tmp_path: Path) -> None:
    """An unfetched parent is not a root commit, and must not be treated as one.

    ``actions/checkout`` clones at ``--depth 1``. libgit2 grafts the boundary
    commit, so ``HEAD~1`` does not resolve and ``parent_ids`` is empty -- exactly
    what a real root commit looks like. Falling back to the empty tree there
    presents every tracked file as newly added under the last commit's subject:
    a review of the whole repository wearing the label of one change.
    """

    source = tmp_path / "source"
    source.mkdir()
    repo = pygit2.init_repository(str(source), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.test"
    _write(source, "a.py", "def a():\n    return 1\n")
    _write(source, "b.py", "def b():\n    return 2\n")
    _commit(repo, "one", 0)
    _write(source, "c.py", "def c():\n    return 3\n")
    _commit(repo, "two: add c", 60)

    clone = tmp_path / "clone"
    _git(["clone", "--quiet", "--depth", "1", f"file://{source}", str(clone)], tmp_path)
    assert (clone / "a.py").exists(), "the shallow clone did not happen"

    with pytest.raises(ValueError) as raised:
        resolve_rev_range(clone)
    assert "shallow" in str(raised.value)

    # A genuine root commit still falls back to the empty tree -- even when it,
    # too, was reached through a shallow clone, where `.git/shallow` names the
    # root commit itself and `is_shallow` is therefore True.
    single = tmp_path / "single"
    single.mkdir()
    solo = pygit2.init_repository(str(single), initial_head="main")
    solo.config["user.name"] = "Fixture Tester"
    solo.config["user.email"] = "fixture@example.test"
    _write(single, "only.py", "x = 1\n")
    _commit(solo, "only", 0)
    solo_clone = tmp_path / "solo-clone"
    _git(["clone", "--quiet", "--depth", "1", f"file://{single}", str(solo_clone)], tmp_path)

    root_range = resolve_rev_range(solo_clone)
    assert root_range.base_rev == "(empty tree)"
    assert [item.path for item in collect_diff(solo_clone, root_range).files] == ["only.py"]


def test_a_committed_store_file_is_a_row_and_a_skipped_one_is_counted(tmp_path: Path) -> None:
    """The skip covers this command's own cache, not a commit somebody made.

    Nothing inside a *tree* is our scratch space, so a commit that touches only
    ``.lemoncrow/`` used to review as ``0 files -- no changes in this range``
    with nothing in ``degraded`` to say a row had been dropped. Where the skip
    does apply -- an untracked index this very command just wrote -- it is now
    counted, because a silent skip is how a real change becomes invisible.
    """

    root = tmp_path / "store-dir"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.test"
    _write(root, "top.txt", "top\n")
    _commit(repo, "base", 0)
    _write(root, ".lemoncrow/settings.json", "{}\n")
    _commit(repo, "add team settings", 60)

    committed = collect_diff(root, resolve_rev_range(root, "HEAD~1"))
    assert [item.path for item in committed.files] == [".lemoncrow/settings.json"]

    _write(root, ".lemoncrow/workspace/index.sqlite", "not really sqlite\n")
    working = collect_diff(root, resolve_rev_range(root, working_tree=True))
    assert [item.path for item in working.files] == []
    assert "lemoncrow_store_excluded:1" in working.degraded


def test_an_unmerged_path_says_so_instead_of_reading_as_an_empty_modification(tmp_path: Path) -> None:
    """libgit2 reports a conflicted delta with ``status_char() == ' '``.

    The character table has no entry for that, so the row fell through to the
    ``modified`` default and rendered ``+0 -0`` with ``mode_only`` beside it --
    naming a cause that is not the cause, and telling a reviewer nothing about
    the fact that the path is unmerged and has no stage-0 content at all.
    """

    root = tmp_path / "conflicted"
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "--quiet", "--initial-branch=main", "."], root)
    _write(root, "c.txt", "base\n")
    _git(["add", "-A"], root)
    _git(["commit", "--quiet", "-m", "base"], root)
    _git(["checkout", "--quiet", "-b", "side"], root)
    _write(root, "c.txt", "side\n")
    _git(["commit", "--quiet", "-am", "side"], root)
    _git(["checkout", "--quiet", "main"], root)
    _write(root, "c.txt", "main\n")
    _git(["commit", "--quiet", "-am", "main"], root)
    _git(["merge", "side"], root)
    assert "c.txt" in _git(["ls-files", "-u"], root), "the fixture did not leave a conflict"

    rng = resolve_rev_range(root, staged=True)
    result = collect_diff(root, rng)

    assert [item.path for item in result.files] == ["c.txt"]
    assert "unmerged_paths:1" in result.degraded
    assert "mode_only" not in result.degraded
    # No stage-0 entry means no text: the file unit must fall to `unknown`
    # rather than fingerprinting the conflict as an empty file.
    assert "c.txt" not in load_blobs(root, rng, result.files).new


def test_dirt_inside_a_submodule_keeps_the_default_review_on_the_working_tree(tmp_path: Path) -> None:
    """Dirty submodule bytes belong to a nested frozen WORKDIR revision.

    The parent diff still refuses to invent a gitlink source row for an unmoved
    pointer, but the range must stay on HEAD -> WORKDIR so Review can capture and
    render the nested submodule snapshot. A committed pointer move remains a
    normal parent gitlink change.
    """

    sub = tmp_path / "subsrc"
    sub.mkdir()
    _git(["init", "--quiet", "--initial-branch=main", "."], sub)
    _write(sub, "inner.txt", "one\n")
    _git(["add", "-A"], sub)
    _git(["commit", "--quiet", "-m", "inner one"], sub)

    root = tmp_path / "parent"
    root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main", "."], root)
    _write(root, "top.py", "x = 1\n")
    _git(["add", "-A"], root)
    _git(["commit", "--quiet", "-m", "base"], root)
    _git(
        ["-c", "protocol.file.allow=always", "submodule", "add", "--quiet", str(sub), "vendor/lib"],
        root,
    )
    _git(["add", "-A"], root)
    _git(["commit", "--quiet", "-m", "vendor the library"], root)
    _write(root, "top.py", "x = 2\n")
    _git(["add", "-A"], root)
    _git(["commit", "--quiet", "-m", "bump top"], root)
    assert (root / "vendor" / "lib" / "inner.txt").exists(), "the submodule never checked out"
    assert not is_dirty(pygit2.Repository(str(root))), "the fixture did not start clean"

    # Dirt belonging to the submodule's own repository, not to this one. Both
    # `git status --porcelain` and pygit2 report it as a modified `vendor/lib`,
    # indistinguishable from a moved pointer without the submodule status bits.
    _write(root, "vendor/lib/inner.txt", "one\ntwo\n")
    assert "vendor/lib" in pygit2.Repository(str(root)).status(
        untracked_files="normal", ignored=False
    ), "the fixture did not make the submodule look dirty"

    assert is_dirty(pygit2.Repository(str(root))) is True
    rng = resolve_rev_range(root)
    assert (rng.mode, rng.base_rev, rng.head_rev) == ("working_tree", "HEAD", "WORKDIR")
    nested = collect_diff(root, rng)
    # The parent still does not manufacture a fake gitlink file row. The nested
    # surface snapshot owns the submodule's HEAD -> WORKDIR bytes.
    assert nested.files == ()
    assert rng.submodule_dirt_discounted == 0
    assert "submodule_dirty:1" in nested.degraded, nested.degraded

    # Committing inside the submodule moves the pointer this repo records: a real
    # change here, which keeps working-tree mode and renders as a gitlink row.
    inner = root / "vendor" / "lib"
    _git(["add", "-A"], inner)
    _git(["commit", "--quiet", "-m", "inner two"], inner)
    assert is_dirty(pygit2.Repository(str(root))) is True
    moved = collect_diff(root, resolve_rev_range(root))
    assert [item.path for item in moved.files] == ["vendor/lib"]
    assert moved.files[0].submodule_pointer is not None
    assert not any(signal.startswith("submodule_dirty") for signal in moved.degraded)
