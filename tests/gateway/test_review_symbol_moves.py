"""P11-3: a decision survives a moved symbol -- and only a moved symbol.

Three layers, cheapest first:

* the pure rule (:func:`detect_symbol_moves` through :func:`reconcile`): one source, one destination,
  identical fingerprint, nothing else of that content anywhere; renamed and edited content never carries;
* a seeded randomized differential against an oracle written from the product rule instead of from the
  implementation, whose headline number is **fail-open == 0**: no program in which a ``reviewed`` mark ends up on
  content that is not exactly what its reviewer attested, or on content that is not the only copy of it;
* the Reader's one read: the audit reason that carries the provenance, the store query that reads it back and the
  ``moved_from`` / ``renamed_from`` fields on the target payload.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import revisions
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.revisions import (
    detect_symbol_moves,
    reconcile,
    symbol_move_candidates,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewMark, ReviewUnit
from lemoncrow.pro.capabilities.review.sources.local import mark_unit, open_or_create_session, refresh
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.targets import label_symbol_moves
from lemoncrow.pro.capabilities.review.units import symbol_body_digest, symbol_fingerprint, symbol_header, unit_key
from lemoncrow.pro.capabilities.review.validity import (
    REASONS,
    mark_event_reason,
    mark_results,
    parse_symbol_move_reason,
)

# A symbol is (path, qualified name, shape). The shape is the body with the name taken out: ``_body`` writes it back in,
# so two symbols with the same shape are the same code under different names.
_Sym = tuple[str, str, int]


def _body(name: str, shape: int) -> str:
    return f"def {name}(x):\n    return x + {shape}\n"


def _unit(sym: _Sym, *, method: str = "symbol_body_sha256") -> ReviewUnit:
    path, name, shape = sym
    return ReviewUnit(
        revision_id="rrv-1",
        unit_key=unit_key("symbol", path, name, 0),
        kind="symbol",
        path=path,
        content_fingerprint=symbol_fingerprint("function", name, _body(name, shape)),
        fingerprint_method=method,  # type: ignore[arg-type]
        symbol=name,
        start_line=1,
        end_line=2,
    )


def _digests(previous: list[_Sym], following: list[_Sym]) -> dict[str, str]:
    """What ``sources.local._symbol_body_digests`` computes: name-masked bodies of the churned symbols only."""

    shapes = {unit_key("symbol", p, n, 0): (n, k) for p, n, k in (*previous, *following)}
    removed, added = symbol_move_candidates([_unit(s) for s in previous], [_unit(s) for s in following])
    return {
        unit.unit_key: symbol_body_digest("symbol", shapes[unit.unit_key][0], _body(*shapes[unit.unit_key]))
        for unit in (*removed, *added)
    }


def _mark(sym: _Sym, state: str = "reviewed") -> ReviewMark:
    unit = _unit(sym)
    return ReviewMark(
        review_id="rev-1",
        unit_key=unit.unit_key,
        state=state,  # type: ignore[arg-type]
        reviewed_revision_id="rrv-1",
        content_fingerprint=unit.content_fingerprint,
    )


def _run(previous: list[_Sym], following: list[_Sym], marks: list[ReviewMark], **kwargs: Any) -> Any:
    """``reconcile`` with every unit undecorated unless the test passes ``headers`` (``None`` passes none)."""

    kwargs.setdefault("headers", {_unit(s).unit_key: "" for s in [*previous, *following]})
    return reconcile(
        [_unit(s) for s in previous],
        [_unit(s) for s in following],
        marks,
        body_digests=_digests(previous, following),
        **kwargs,
    )


_A: _Sym = ("src/a.py", "alpha", 1)
_B: _Sym = ("src/b.py", "alpha", 1)


def test_an_unambiguous_cross_file_move_carries_the_judgment_and_says_where_from() -> None:
    result = _run([_A], [_B], [_mark(_A)])
    [carried] = result.reopened
    assert (carried.unit_key, carried.state) == (_unit(_B).unit_key, "reviewed")
    assert carried.content_fingerprint == _unit(_A).content_fingerprint
    assert result.reasons == (("local", _unit(_B).unit_key, "symbol_moved"),)
    [move] = result.moves
    assert (move.kind, move.previous_path, move.previous_symbol, move.path) == (
        "moved",
        "src/a.py",
        "alpha",
        "src/b.py",
    )
    assert result.aliases == ((_unit(_A).unit_key, _unit(_B).unit_key),)
    assert (result.added, result.removed) == ((), ())
    assert [mark.unit_key for mark in result.dropped] == [_unit(_A).unit_key]
    [explained] = mark_results(result)
    assert (explained.reason_code, explained.state) == ("symbol_moved", "preserved")
    assert explained.causes == ()  # a symbol move is not a file rename


@pytest.mark.parametrize("state", ["needs_changes", "later"])
def test_a_request_or_a_set_aside_part_follows_a_move_unchanged(state: str) -> None:
    result = _run([_A], [_B], [_mark(_A, state)])
    assert [(mark.unit_key, mark.state) for mark in result.reopened] == [(_unit(_B).unit_key, state)]
    assert result.reasons[0][2] == "symbol_moved"


def _one_outcome_per_decision(result: Any) -> None:
    """Every (reviewer, unit) lands in exactly one outcome and has exactly one reason."""

    placed = Counter(
        (mark.reviewer_id, mark.unit_key)
        for outcome in (result.carried, result.reopened, result.rebased)
        for mark in outcome
    )
    assert all(count == 1 for count in placed.values()), placed
    reasons = Counter((reviewer, key) for reviewer, key, _code in result.reasons)
    assert all(count == 1 for count in reasons.values()), reasons
    assert set(reasons) == set(placed)


def test_a_move_never_overwrites_the_reviewers_own_newer_decision_on_the_destination() -> None:
    later = _mark(_B, "later")
    result = _run([_A], [_A, _B], [_mark(_A), later])
    # a.py::alpha is still there, so this is a copy, not a move; remove it to make the move look unique.
    assert result.moves == ()
    result = _run([_A], [_B], [_mark(_A), later])
    assert result.moves == ()
    assert [(m.unit_key, m.state) for m in result.carried] == [(later.unit_key, "later")]
    assert result.reopened == ()
    assert [m.unit_key for m in result.dropped] == [_unit(_A).unit_key]
    assert result.aliases == ()
    _one_outcome_per_decision(result)
    # Another reviewer's verdict on the source still follows the move; the destination is only protected per reviewer.
    other = replace(_mark(_A), reviewer_id="someone-else")
    result = _run([_A], [_B], [other, later])
    assert [(m.reviewer_id, m.unit_key, m.state) for m in result.reopened] == [
        ("someone-else", _unit(_B).unit_key, "reviewed")
    ]
    assert [(m.reviewer_id, m.state) for m in result.carried] == [("local", "later")]
    _one_outcome_per_decision(result)


def test_one_reviewers_followed_rename_does_not_hide_another_reviewers_dropped_request() -> None:
    renamed: _Sym = ("src/b.py", "alpha_two", 1)
    mine = _mark(_A)
    theirs = replace(_mark(_A, "needs_changes"), reviewer_id="bob")
    result = _run([_A], [renamed], [mine, theirs])
    [move] = result.moves
    assert (move.kind, move.reviewer_id) == ("renamed", "local")
    # bob's request is dropped (a rename never carries one) and it is *reported* as a removed unit, not as aliased away.
    explained = {(r.subject_ref, r.reason_code) for r in mark_results(result)}
    assert (f"mark:bob:{_unit(_A).unit_key}", "unit_removed") in explained
    assert not any(code == "unit_removed" and ":local:" in ref for ref, code in explained)
    assert [(m.reviewer_id, m.state) for m in result.reopened] == [("local", "changed_since_review")]
    _one_outcome_per_decision(result)


def test_a_stale_mark_does_not_become_fresh_by_moving() -> None:
    stale = replace(_mark(_A), content_fingerprint="what-the-reviewer-saw-long-ago")
    [moved] = _run([_A], [_B], [stale]).reopened
    assert moved.state == "changed_since_review"


@pytest.mark.parametrize(
    ("previous", "following"),
    [
        pytest.param([_A], [_B, ("src/c.py", "alpha", 1)], id="two-destinations"),
        pytest.param([_A, ("src/z.py", "alpha", 1)], [_B], id="two-sources"),
        pytest.param([_A, ("src/z.py", "alpha", 1)], [_B, ("src/z.py", "alpha", 1)], id="a-copy-stays-put"),
        pytest.param([_A], [("src/b.py", "alpha", 2)], id="moved-and-edited"),
    ],
)
def test_an_ambiguous_or_edited_move_carries_nothing(previous: list[_Sym], following: list[_Sym]) -> None:
    result = _run(previous, following, [_mark(_A)])
    assert not [mark for mark in result.marks if mark.state == "reviewed"]
    assert not any(code == "symbol_moved" for _r, _k, code in result.reasons)
    assert result.moves == ()
    assert _unit(_A).unit_key in {mark.unit_key for mark in result.dropped}


def test_a_move_that_gains_or_loses_a_decorator_carries_nothing() -> None:
    """The fingerprint covers the window, and a decorator sits above it: ``headers`` is the rest of the comparison."""

    old_key, new_key = _unit(_A).unit_key, _unit(_B).unit_key
    same = _run([_A], [_B], [_mark(_A)], headers={old_key: "@login_required", new_key: "@login_required"})
    assert [m.kind for m in same.moves] == ["moved"]
    for headers in (
        {old_key: "@login_required", new_key: ""},  # lost it
        {old_key: "", new_key: "@login_required"},  # gained one
        {old_key: "@login_required", new_key: "@admin_only"},  # swapped it
        {old_key: "@login_required"},  # the destination's header could not be read
        {},  # nothing could be read
    ):
        result = _run([_A], [_B], [_mark(_A)], headers=headers)
        assert result.moves == () and result.marks == (), headers
        assert [m.unit_key for m in result.dropped] == [old_key]


def test_a_caller_that_supplies_no_headers_follows_no_move() -> None:
    """``headers=None`` must not switch the decorator check off: with no source text nothing proves the move."""

    for kwargs in ({"headers": None}, {}):  # explicit None, and the default of ``reconcile`` itself
        result = reconcile([_unit(_A)], [_unit(_B)], [_mark(_A)], body_digests=_digests([_A], [_B]), **kwargs)
        assert result.moves == () and result.marks == () and result.carried == ()
        assert [m.unit_key for m in result.dropped] == [_unit(_A).unit_key]
    assert detect_symbol_moves([_unit(_A)], [_unit(_B)]) == ()
    assert [m.kind for m in detect_symbol_moves([_unit(_A)], [_unit(_B)], headers={})] == []  # unreadable: none
    both = {_unit(_A).unit_key: "", _unit(_B).unit_key: ""}
    assert [m.kind for m in detect_symbol_moves([_unit(_A)], [_unit(_B)], headers=both)] == ["moved"]


@pytest.mark.parametrize(
    ("above", "expected"),
    [
        ([], ""),
        (["", "def other(): pass"], ""),
        (["@login_required"], "@login_required"),
        (["@a", "@b(1)"], "@a\n@b(1)"),
        (["x = 1", "@app.route(", '    "/x",', "    methods=[1],", ")"], '@app.route(\n"/x",\nmethods=[1],\n)'),
        (["", "@login_required"], "@login_required"),
        (["@login_required", ""], "@login_required"),  # a blank line between the decorator and the definition
        (["@login_required", "", ""], "@login_required"),
        (["@a", "", "@b"], "@a\n@b"),
        (["@login_required", "# who may call this", "", "// and this", "/* and this */"], "@login_required"),
        (["@login_required", "/*", " * block", " */"], "@login_required"),
        (["@login_required", "/** one line */"], "@login_required"),
        (["x = 1", "# note", "", "@login_required"], "@login_required"),
        (["def other(): pass", "", "# a comment, nothing above it"], ""),
        (["x = 1  /* trailing */"], ""),  # a code line that ends in a comment is code
        (["@login_required", "x = 1  /* trailing */"], None),  # a statement under a decorator: unreadable
        (["@app.route(", "# why", '    "/x",', "", ")", ""], '@app.route(\n"/x",\n)'),
        (["@login_required", " * orphan block body", " */"], None),  # a block comment that never opens
        (["#[cfg(test)]", "", "#[derive(Debug)]"], "#[cfg(test)]\n#[derive(Debug)]"),
        (["[Authorize]", "", "// why"], "[Authorize]"),
        # The previous statement's multi-line call is not a decorator, however many blank lines follow it.
        (["def g():", "    return foo(", "        a,", "    )", ""], ""),
        (["def g():", "    return foo(", "        a,", "    )", "", ""], ""),
        (["export const x = f(", "  1,", ");", ""], ""),
        (["@login_required", "x = foo(", "    a,", ")", ""], None),  # under a decorator no statement can follow
        (["x = [", "    1,", "]", "", "@login_required"], "@login_required"),
        (
            ["x = [", "    1,", "]", "@login_required"],
            None,
        ),  # no blank line: cannot tell it from a decorator's argument
        (["@app.route(", '    "/x",', ")", "", "# note"], '@app.route(\n"/x",\n)'),
        (["@Roles(", "  /* who */", "  'admin',", ")"], "@Roles(\n'admin',\n)"),  # a whole-line comment inside
        # Brackets inside string literals and trailing comments are not counted (the round-5 fail-open).
        (["@route(", '    "/a(",', ")"], '@route(\n"/a(",\n)'),
        (["@route(", '    "/a)",', ")"], '@route(\n"/a)",\n)'),
        (
            ["@pytest.mark.parametrize(", '    "x",', '    [("12) twelfth", 1)],', ")"],
            '@pytest.mark.parametrize(\n"x",\n[("12) twelfth", 1)],\n)',
        ),
        (
            ["@pytest.mark.parametrize(", '    "x",', '    [("12) twelfth", 1)],', ")", "", "# n"],
            '@pytest.mark.parametrize(\n"x",\n[("12) twelfth", 1)],\n)',
        ),
        (["@Admin()", "// done)"], None),  # a C-like comment that unbalances: unreadable, so never carried
        (["@route(", "    'it''s',", ")"], "@route(\n'it''s',\n)"),
        (["@route(", '    "unterminated,', ")"], None),
        (['    """', "    A docstring.", '    """'], ""),  # the previous statement's closing quotes end the scan
        (["@login_required", '    """', "    note", '    """'], ""),
        (["@route(", '    """', "    a", '    """', ")"], None),  # inside a run it cannot be told apart: refused
        (["@route(", "    a,  # why (", ")"], "@route(\na, # why (\n)"),
        (["@route(", "    a,  // why (", ")"], None),
        (["x = f(", '    "a(",', ")", "", "# n"], ""),
        (["@login_required", "", "x = f(", '    "a(",', ")", ""], None),
        # Inside a decorator's arguments a comment must be a whole line: these two are code and differ.
        (["@cache(ttl=x", "    // 2)"], "@cache(ttl=x\n// 2)"),
        (["@cache(ttl=x", "    // 3)"], "@cache(ttl=x\n// 3)"),
        (["@Roles(", "  /* who */ 'admin',", ")"], "@Roles(\n/* who */ 'admin',\n)"),
        (["@Roles(", "  /* who */ 'user',", ")"], "@Roles(\n/* who */ 'user',\n)"),
        (["#[derive(Debug)]", "#[cfg(test)]"], "#[derive(Debug)]\n#[cfg(test)]"),
        (['[HttpGet("x")]'], '[HttpGet("x")]'),
        (["    @Override  "], "@Override"),
        (['    "/x",', ")"], None),  # a decorator's tail with no head: unreadable, so never equal to anything
        # A tail line the counter reads as balanced and head-less must not end the scan with ``""`` (round 7).
        (["@login_required \\", "    if FLAG else noop"], None),
        (["@login_required \\", "    if FLAG else noop", ""], None),
        (["@Matches(", "  /[a-z(]+/)"], None),
        (["@Matches(", "  /[a-z(]+/)", ""], None),
        (["@Get(", "  `/x/${`(`}`)"], None),
        (["@route(", '    f"{d["k("]}",', ")", ""], None),
        (["@Dec(", "  this.#x)"], "@Dec(\nthis.#x)"),  # ``#`` after an identifier character is no comment
        (["@Other(", "  this.#x)"], "@Other(\nthis.#x)"),
        (["@doc(", '  r#"(x"#,', ")"], '@doc(\nr#"(x"#,\n)'),
        # A multi-line attribute is read whole, and a changed argument changes it.
        (["#[derive(", "    Debug,", "    Clone,", ")]"], "#[derive(\nDebug,\nClone,\n)]"),
        (["#[derive(", "    Debug,", "    Copy,", ")]"], "#[derive(\nDebug,\nCopy,\n)]"),
        (["#[Route(", "    '/x',", ")]"], "#[Route(\n'/x',\n)]"),
        (["#[Route(", "    '/y',", ")]"], "#[Route(\n'/y',\n)]"),
        (["[Route(", '    "x",', ")]"], '[Route(\n"x",\n)]'),
        (["[Route(", '    "y",', ")]"], '[Route(\n"y",\n)]'),
        (["#[Route('/x')] # admin only"], "#[Route('/x')] # admin only"),
        # A ``[`` line that opens a run closing with something else is an array statement, not an attribute.
        (["[", "    1,", "];"], None),
    ],
)
def test_the_header_is_the_contiguous_decorator_lines_directly_above_the_window(
    above: list[str], expected: str | None
) -> None:
    assert symbol_header([*above, "def alpha(): ..."], len(above) + 1) == expected
    assert symbol_header(["def alpha(): ..."], 1) == ""


def test_the_same_path_is_not_a_move() -> None:
    previous, following = [("src/a.py", "alpha", 1)], [("src/a.py", "alpha", 1), ("src/a.py", "beta", 1)]
    assert detect_symbol_moves([_unit(s) for s in previous], [_unit(s) for s in following]) == ()


def test_an_unknown_fingerprint_is_never_matched() -> None:
    result = reconcile([_unit(_A, method="unknown")], [_unit(_B, method="unknown")], [_mark(_A)])
    assert result.moves == ()
    assert result.marks == ()


def test_a_renamed_symbol_reopens_with_its_own_reason_instead_of_carrying() -> None:
    renamed: _Sym = ("src/a.py", "gamma", 1)
    result = _run([_A], [renamed], [_mark(_A)])
    [reopened] = result.reopened
    assert (reopened.unit_key, reopened.state) == (_unit(renamed).unit_key, "changed_since_review")
    assert result.reasons == (("local", _unit(renamed).unit_key, "symbol_renamed"),)
    [move] = result.moves
    assert (move.kind, move.previous_symbol, move.symbol) == ("renamed", "alpha", "gamma")
    assert result.aliases == ()
    # The old key is the same part under a new name (the target delta reads it so), so it is not "removed" as well.
    assert result.added == (_unit(renamed).unit_key,) and result.removed == ()
    [explained] = mark_results(result)
    assert (explained.reason_code, explained.state) == ("symbol_renamed", "reopened")


def test_a_renamed_symbol_that_no_reviewer_followed_is_still_reported_removed() -> None:
    """Only a rename a reviewer's judgment actually reopened is the same part: with no mark on it, it is gone."""

    result = _run([_A], [("src/a.py", "gamma", 1)], [])
    assert result.moves == () and result.removed == (_unit(_A).unit_key,)


def test_a_rename_that_moved_files_too_is_still_renamed_and_still_reopens() -> None:
    result = _run([_A], [("src/b.py", "gamma", 1)], [_mark(_A)])
    assert [mark.state for mark in result.reopened] == ["changed_since_review"]
    assert [move.kind for move in result.moves] == ["renamed"]


@pytest.mark.parametrize("state", ["needs_changes", "later", "unreviewed"])
def test_a_rename_does_not_carry_a_request(state: str) -> None:
    """Conservative: no product decision says a name change keeps a request, so it behaves as before (dropped)."""

    result = _run([_A], [("src/a.py", "gamma", 1)], [_mark(_A, state)])
    assert result.marks == () and result.moves == ()


def test_two_renames_of_the_same_body_are_ambiguous_and_reopen_nothing() -> None:
    previous: list[_Sym] = [_A, ("src/z.py", "omega", 1)]
    following: list[_Sym] = [("src/a.py", "gamma", 1), ("src/z.py", "sigma", 1)]
    assert _run(previous, following, [_mark(_A)]).marks == ()


def test_hunk_and_file_units_never_follow_a_fingerprint() -> None:
    def other(kind: str, key: str, path: str) -> ReviewUnit:
        return ReviewUnit(
            revision_id="rrv-1",
            unit_key=key,
            kind=kind,  # type: ignore[arg-type]
            path=path,
            content_fingerprint="same-bytes",
            fingerprint_method="blob_sha256",
        )

    for kind in ("hunk", "file"):
        before, after = other(kind, "old", "src/a.py"), other(kind, "new", "src/b.py")
        mark = ReviewMark(
            review_id="rev-1",
            unit_key="old",
            state="reviewed",
            reviewed_revision_id="rrv-1",
            content_fingerprint="same-bytes",
        )
        result = reconcile([before], [after], [mark], body_digests={})
        assert (result.marks, result.moves, result.removed, result.added) == ((), (), ("old",), ("new",))


def test_a_file_rename_still_carries_every_symbol_by_alias() -> None:
    old, new = _unit(_A), _unit(_B)
    result = reconcile([old], [new], [_mark(_A)], renames=[("src/a.py", "src/b.py")], body_digests={})
    assert result.reasons == (("local", new.unit_key, "content_unchanged"),)
    assert result.moves == ()
    assert result.aliases == ((old.unit_key, new.unit_key),)


def _file_unit(path: str) -> ReviewUnit:
    """A file unit: its fingerprint includes the path, so a rename changes it (as the real one does)."""

    return ReviewUnit(
        revision_id="rrv-1",
        unit_key=unit_key("file", path, "", 0),
        kind="file",
        path=path,
        content_fingerprint=f"bytes of {path}",
        fingerprint_method="blob_sha256",
    )


def _mark_of(unit: ReviewUnit, state: str = "reviewed") -> ReviewMark:
    return ReviewMark(
        review_id="rev-1",
        unit_key=unit.unit_key,
        state=state,  # type: ignore[arg-type]
        reviewed_revision_id="rrv-1",
        content_fingerprint=unit.content_fingerprint,
    )


def _rename_with_a_move(previous: list[_Sym], following: list[_Sym], files: tuple[list[str], list[str]]) -> Any:
    before = [*(_file_unit(p) for p in files[0]), *(_unit(s) for s in previous)]
    after = [*(_file_unit(p) for p in files[1]), *(_unit(s) for s in following)]
    return reconcile(
        before,
        after,
        [_mark_of(u) for u in before],
        renames=[("src/a.py", "src/c.py")],
        body_digests=_digests(previous, following),
        headers={u.unit_key: "" for u in (*before, *after)},
    )


def _no_decision_in_two_outcomes(result: Any) -> None:
    _one_outcome_per_decision(result)
    placed = {m.unit_key for m in (*result.carried, *result.reopened, *result.rebased)}
    assert not placed & {m.unit_key for m in result.dropped}, "a key is both kept and dropped"
    assert not set(result.removed) & set(result.added)
    assert not placed & set(result.removed), "a decision sits on a key that is reported as removed"


def test_a_symbol_that_leaves_a_renamed_file_follows_the_move_and_the_rest_follows_the_file() -> None:
    """File ``a.py`` becomes ``c.py`` while ``alpha`` leaves it for ``b.py``, in the same revision."""

    result = _rename_with_a_move(
        [("src/a.py", "alpha", 1), ("src/a.py", "gamma", 2)],
        [("src/b.py", "alpha", 1), ("src/c.py", "gamma", 2)],
        (["src/a.py"], ["src/c.py"]),
    )
    alpha_b, gamma_c = unit_key("symbol", "src/b.py", "alpha", 0), unit_key("symbol", "src/c.py", "gamma", 0)
    file_c = unit_key("file", "src/c.py", "", 0)
    landed = {m.unit_key: m.state for m in result.marks}
    # The moved symbol's judgment lands on exactly one key: its destination, not the renamed file's copy of the key.
    assert landed[alpha_b] == "reviewed"
    assert unit_key("symbol", "src/c.py", "alpha", 0) not in landed
    assert [m.unit_key for m in result.marks].count(alpha_b) == 1
    # The symbol that stayed follows the file by alias; the file itself reopens once, as before.
    assert landed[gamma_c] == "reviewed" and landed[file_c] == "changed_since_review"
    # The file rename's keys are not also reported as moves: the one move is alpha's.
    assert [(m.kind, m.previous_path, m.path, m.symbol) for m in result.moves] == [
        ("moved", "src/a.py", "src/b.py", "alpha")
    ]
    codes = {key: code for _r, key, code in result.reasons}
    assert (codes[alpha_b], codes[gamma_c], codes[file_c]) == ("symbol_moved", "content_unchanged", "content_changed")
    assert len(landed) == 3
    _no_decision_in_two_outcomes(result)


def test_a_symbol_that_moves_into_a_renamed_file_lands_under_the_new_path_once() -> None:
    """``alpha`` leaves ``z.py`` for ``a.py`` while ``a.py`` becomes ``c.py``: the destination is the renamed file."""

    result = _rename_with_a_move(
        [("src/z.py", "alpha", 1), ("src/a.py", "gamma", 2)],
        [("src/c.py", "alpha", 1), ("src/c.py", "gamma", 2)],
        (["src/a.py", "src/z.py"], ["src/c.py", "src/z.py"]),
    )
    alpha_c, gamma_c = unit_key("symbol", "src/c.py", "alpha", 0), unit_key("symbol", "src/c.py", "gamma", 0)
    landed = {m.unit_key: m.state for m in result.marks}
    assert landed[alpha_c] == "reviewed" and landed[gamma_c] == "reviewed"
    assert [m.unit_key for m in result.marks].count(alpha_c) == 1
    assert landed[unit_key("file", "src/c.py", "", 0)] == "changed_since_review"
    assert landed[unit_key("file", "src/z.py", "", 0)] == "reviewed"
    assert [(m.kind, m.previous_path, m.path) for m in result.moves] == [("moved", "src/z.py", "src/c.py")]
    assert {key: code for _r, key, code in result.reasons}[alpha_c] == "symbol_moved"
    _no_decision_in_two_outcomes(result)


def test_a_file_renames_own_keys_are_not_reported_as_symbol_moves_too() -> None:
    """The rename list is what claims them; without it the same units read as a symbol move."""

    previous, following = [("src/a.py", "gamma", 2)], [("src/c.py", "gamma", 2)]
    with_rename = _rename_with_a_move(previous, following, (["src/a.py"], ["src/c.py"]))
    assert with_rename.moves == ()
    assert with_rename.aliases != ()
    before = [_file_unit("src/a.py"), *(_unit(s) for s in previous)]
    after = [_file_unit("src/c.py"), *(_unit(s) for s in following)]
    without = reconcile(
        before,
        after,
        [_mark_of(u) for u in before],
        body_digests=_digests(previous, following),
        headers={u.unit_key: "" for u in (*before, *after)},
    )
    assert [m.kind for m in without.moves] == ["moved"]


def test_a_symbol_the_file_rename_already_accounts_for_does_not_make_another_symbols_rename_ambiguous() -> None:
    """``alpha`` follows ``a.py`` to ``c.py`` by alias, edited on the way (shape 1 to 2); ``delta`` leaves ``z.py`` as
    ``omega`` in ``b.py`` with the body ``alpha`` used to have. The alias claims ``alpha``'s old and new keys, so
    ``delta`` is the only removed and the only added body of that shape and still reads as a rename, which reopens it.
    """

    result = _rename_with_a_move(
        [("src/a.py", "alpha", 1), ("src/z.py", "delta", 1)],
        [("src/c.py", "alpha", 2), ("src/b.py", "omega", 1)],
        (["src/a.py", "src/z.py"], ["src/c.py", "src/z.py"]),
    )
    landed = {m.unit_key: m.state for m in result.marks}
    assert landed[unit_key("symbol", "src/c.py", "alpha", 0)] == "changed_since_review"  # by alias, edited: reopens
    assert landed[unit_key("symbol", "src/b.py", "omega", 0)] == "changed_since_review"  # renamed: reopened, labelled
    assert [(m.kind, m.previous_symbol, m.symbol) for m in result.moves] == [("renamed", "delta", "omega")]
    _no_decision_in_two_outcomes(result)


def test_a_copy_of_a_renamed_files_symbol_is_not_a_move_of_it() -> None:
    """The alias keeps ``gamma`` in ``c.py``; a second copy in ``b.py`` has no source left to follow."""

    result = _rename_with_a_move(
        [("src/a.py", "gamma", 2)],
        [("src/c.py", "gamma", 2), ("src/b.py", "gamma", 2)],
        (["src/a.py"], ["src/c.py"]),
    )
    landed = {m.unit_key: m.state for m in result.marks}
    assert landed[unit_key("symbol", "src/c.py", "gamma", 0)] == "reviewed"
    assert unit_key("symbol", "src/b.py", "gamma", 0) not in landed
    assert result.moves == ()
    _no_decision_in_two_outcomes(result)


def test_the_reason_codes_are_registered_with_one_state_each() -> None:
    assert REASONS["symbol_moved"].state == "preserved"
    assert REASONS["symbol_renamed"].state == "reopened"


def test_the_provenance_round_trips_through_the_audited_reason() -> None:
    for kind in ("moved", "renamed"):
        text = mark_event_reason("symbol_moved", symbol_move=(kind, 'src/we"ird; path.py', "Cls.method"))
        assert parse_symbol_move_reason(text) == (kind, 'src/we"ird; path.py', "Cls.method")
        assert "revision reconciliation:" in text
    assert parse_symbol_move_reason(mark_event_reason("content_changed")) is None
    assert parse_symbol_move_reason("symbol moved from not-json") is None


# --------------------------------------------------------------------------------------------------------------------
# Seeded randomized differential
# --------------------------------------------------------------------------------------------------------------------

_NAMES = [f"fn_{c}" for c in "abcdefghijklmn"]
_RENAMES = [f"ren_{i}" for i in range(6)]
_PATHS = [f"src/m{i}.py" for i in range(4)]
_OPS = ("keep", "move", "rename", "edit", "delete", "copy", "move_two", "move_edit")
_WEIGHTS = (30, 25, 10, 10, 5, 8, 4, 8)
_STATES = ("reviewed", "needs_changes", "later")
_STATE_WEIGHTS = (70, 15, 15)


def _program(seed: int) -> tuple[list[_Sym], list[_Sym], list[tuple[_Sym, str]]]:
    rng = random.Random(seed)
    previous: list[_Sym] = []
    used: set[tuple[str, str]] = set()
    for _ in range(rng.randint(4, 10)):
        path, name = rng.choice(_PATHS), rng.choice(_NAMES)
        if (path, name) not in used:
            used.add((path, name))
            previous.append((path, name, rng.randrange(6)))
    following: list[_Sym] = []
    taken: set[tuple[str, str]] = set()
    fresh = iter(range(100, 100_000))

    def put(path: str, name: str, shape: int) -> None:
        if (path, name) not in taken:
            taken.add((path, name))
            following.append((path, name, shape))

    for path, name, shape in previous:
        others = [p for p in _PATHS if p != path]
        match rng.choices(_OPS, _WEIGHTS)[0]:
            case "keep":
                put(path, name, shape)
            case "move":
                put(rng.choice(others), name, shape)
            case "rename":
                put(rng.choice(_PATHS), rng.choice(_RENAMES), shape)
            case "edit":
                put(path, name, next(fresh))
            case "copy":
                put(path, name, shape)
                put(rng.choice(others), name, shape)
            case "move_two":
                for destination in rng.sample(others, 2):
                    put(destination, name, shape)
            case "move_edit":
                put(rng.choice(others), name, next(fresh))
    for _ in range(rng.randint(0, 2)):
        put(rng.choice(_PATHS), rng.choice(_NAMES), rng.randrange(6))
    marked = [(sym, rng.choices(_STATES, _STATE_WEIGHTS)[0]) for sym in previous if rng.random() < 0.85]
    return previous, following, marked


def _expected(
    previous: list[_Sym], following: list[_Sym], marked: list[tuple[_Sym, str]]
) -> tuple[dict[tuple[str, str], str], dict[_Sym, str]]:
    """What the product rule says each mark becomes, computed from the programs' own model, not from the code."""

    before = {(p, n): k for p, n, k in previous}
    after = {(p, n): k for p, n, k in following}
    content_before = Counter((n, k) for _p, n, k in previous)
    content_after = Counter((n, k) for _p, n, k in following)
    removed = [s for s in previous if (s[0], s[1]) not in after]
    added = [s for s in following if (s[0], s[1]) not in before]
    moved: dict[tuple[str, str], tuple[str, str]] = {}
    for src in removed:
        twins = [t for t in added if (t[1], t[2]) == (src[1], src[2])]
        if len(twins) == 1 and content_before[(src[1], src[2])] == 1 and content_after[(src[1], src[2])] == 1:
            moved[(src[0], src[1])] = (twins[0][0], twins[0][1])
    moved_to = set(moved.values())
    renamed: dict[tuple[str, str], tuple[str, str]] = {}
    for src in removed:
        if (src[0], src[1]) in moved:
            continue
        olds = [r for r in removed if (r[0], r[1]) not in moved and r[2] == src[2]]
        news = [a for a in added if (a[0], a[1]) not in moved_to and a[2] == src[2]]
        if len(olds) == 1 and len(news) == 1 and news[0][1] != src[1]:
            renamed[(src[0], src[1])] = (news[0][0], news[0][1])

    expected: dict[tuple[str, str], str] = {}
    category: dict[_Sym, str] = {}
    for sym, state in marked:
        key = (sym[0], sym[1])
        if key in after:
            if after[key] == sym[2]:
                expected[key], category[sym] = state, "carried"
            else:
                expected[key], category[sym] = ("changed_since_review" if state == "reviewed" else state), "changed"
        elif key in moved:
            expected[moved[key]], category[sym] = state, "carried"
        elif key in renamed and state == "reviewed":
            expected[renamed[key]], category[sym] = "changed_since_review", "renamed"
        else:
            twin_elsewhere = any((t[1], t[2]) == (sym[1], sym[2]) for t in added)
            category[sym] = "ambiguous" if twin_elsewhere else "gone"
    return expected, category


def _differential(seeds: range, counts: Counter[str]) -> None:
    for seed in seeds:
        previous, following, marked = _program(seed)
        expected, category = _expected(previous, following, marked)
        result = _run(previous, following, [_mark(sym, state) for sym, state in marked])
        by_key = {unit_key("symbol", p, n, 0): (p, n, k) for p, n, k in following}
        actual = {mark.unit_key: mark for mark in result.marks}
        want = {unit_key("symbol", p, n, 0): state for (p, n), state in expected.items()}
        counts["programs"] += 1
        for sym, state in marked:
            if state == "reviewed":
                counts[category[sym]] += 1
        counts["moved_carried"] += sum(1 for m in result.moves if m.kind == "moved")
        for key, mark in actual.items():
            # Intrinsic invariant, independent of the oracle: a reviewed mark sits on the exact content it attested,
            # and a mark that changed keys sits on the only copy of that content in either revision.
            if mark.state == "reviewed":
                destination = by_key.get(key)
                if destination is None or _unit(destination).content_fingerprint != mark.content_fingerprint:
                    counts["fail_open"] += 1
                elif key not in {unit_key("symbol", p, n, 0) for p, n, _k in previous}:
                    copies = [s for s in following if _unit(s).content_fingerprint == mark.content_fingerprint]
                    if len(copies) != 1:
                        counts["fail_open"] += 1
        for key in actual.keys() | want.keys():
            have = actual[key].state if key in actual else None
            if have == want.get(key):
                continue
            counts["divergence"] += 1
            if have == "reviewed" or (have is not None and key not in want):
                counts["fail_open"] += 1
            else:
                counts["fail_safe"] += 1


def test_randomized_move_programs_never_fail_open() -> None:
    counts: Counter[str] = Counter()
    _differential(range(3000), counts)
    print(f"\nP11-3 differential: {dict(counts)}")
    assert counts["programs"] == 3000
    assert counts["fail_open"] == 0, dict(counts)
    assert counts["divergence"] == 0, dict(counts)
    # Not vacuous: every outcome the rule distinguishes is exercised many times.
    for outcome in ("carried", "changed", "renamed", "ambiguous", "gone", "moved_carried"):
        assert counts[outcome] >= 100, (outcome, dict(counts))


def test_the_harness_does_see_fail_open_in_a_naive_matcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Differential proof that the differential bites: carry on fingerprint equality alone and it must light up."""

    def naive(
        previous_units: Any,
        next_units: Any,
        *,
        body_digests: Any = None,
        headers: Any = None,
        claimed_old: Any = (),
        claimed_new: Any = (),
    ) -> tuple[revisions.SymbolMove, ...]:
        removed, added = symbol_move_candidates(previous_units, next_units)
        return tuple(
            revisions._symbol_move("moved", old, new)
            for old in removed
            for new in added
            if old.content_fingerprint == new.content_fingerprint and old.path != new.path
        )

    monkeypatch.setattr(revisions, "detect_symbol_moves", naive)
    counts: Counter[str] = Counter()
    _differential(range(600), counts)
    assert counts["fail_open"] > 0


# --------------------------------------------------------------------------------------------------------------------
# The Reader's read
# --------------------------------------------------------------------------------------------------------------------


def test_the_target_payload_gets_moved_from_only_for_the_unit_whose_latest_event_is_a_move() -> None:
    rows = [{"unit_key": "a", "symbol": "alpha"}, {"unit_key": "b", "symbol": "beta"}, {"unit_key": "c", "symbol": "c"}]
    labelled = label_symbol_moves(
        rows,
        {"a": ("moved", "src/old.py", "alpha"), "b": ("renamed", "src/b.py", "beta_old")},
    )
    assert labelled[0]["moved_from"] == {"path": "src/old.py"}  # same name: only the path
    assert "renamed_from" not in labelled[0]
    assert labelled[1]["renamed_from"] == {"path": "src/b.py", "symbol": "beta_old"}
    assert "moved_from" not in labelled[1]
    assert labelled[2] == rows[2]
    assert label_symbol_moves(rows, {}) == rows


_BASE = "def keep(x):\n    return x\n"
_ALPHA = "def alpha(v):\n    total = v + 1\n    return total\n"
_BETA = "def beta(v):\n    return v * 2\n"
_ALPHA_DECORATED = "@login_required\n" + _ALPHA


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _reviewed_repo(tmp_path: Path) -> tuple[Path, ReviewStore, Any, Callable[[], Any]]:
    root = tmp_path / "repo"
    root.mkdir()
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"], repo.config["user.email"] = "Fixture Tester", "fixture@example.invalid"
    _write(root, "src/a.py", _BASE)
    repo.index.add_all()
    repo.index.write()
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, "baseline", repo.index.write_tree(), [])
    _write(root, "src/a.py", _BASE + "\n" + _ALPHA + "\n" + _BETA)
    store = ReviewStore(tmp_path / "store")
    session = open_or_create_session(store, root, resolve_rev_range(root))

    def advance() -> Any:
        return refresh(store, session, root, store_root=tmp_path / "store", rng=resolve_rev_range(root), limit=500)

    first = advance()
    for unit in store.list_units(first.revision.id):
        mark_unit(store, session, first.revision, unit)
    return root, store, session, advance


def _symbol_mark(store: ReviewStore, review_id: str, revision_id: str, path: str, name: str) -> Any:
    [unit] = [u for u in store.list_units(revision_id) if u.kind == "symbol" and (u.path, u.symbol) == (path, name)]
    return unit, next((m for m in store.list_marks(review_id) if m.unit_key == unit.unit_key), None)


def test_a_moved_function_keeps_its_reviewed_mark_through_a_real_refresh(tmp_path: Path) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", _ALPHA)
    second = advance()
    unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is not None and mark.state == "reviewed"
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {
        unit.unit_key: ("moved", "src/a.py", "alpha")
    }
    [event] = [e for e in store.list_mark_events(session.id, unit_key=unit.unit_key) if e.event_kind == "reconciled"]
    assert event.reason.startswith("symbol moved from ") and event.previous_unit_key
    assert not [m for m in store.list_marks(session.id) if m.unit_key == event.previous_unit_key]
    # Judging it again supersedes the label: it describes the latest thing that happened to the judgment.
    mark_unit(store, session, second.revision, unit, state="needs_changes")
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {}


def test_a_moved_function_that_lost_its_decorator_is_not_carried_by_a_real_refresh(tmp_path: Path) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n" + _ALPHA_DECORATED + "\n" + _BETA)
    first = advance()
    unit, _ = _symbol_mark(store, session.id, first.revision.id, "src/a.py", "alpha")
    mark_unit(store, session, first.revision, unit)  # reviewed with the decorator on
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", _ALPHA)  # same body, decorator gone
    second = advance()
    _unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is None
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {}


_GAPS = ["\n", "\n\n", "# who may call this\n", "\n# who may call this\n\n"]


@pytest.mark.parametrize("gap", _GAPS)
def test_a_decorator_above_a_blank_or_comment_line_still_counts_in_a_real_refresh(tmp_path: Path, gap: str) -> None:
    """The reviewer's probe: ``@login_required`` + blank line + ``def alpha`` moved without the decorator carried."""

    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n@login_required\n" + gap + _ALPHA + "\n" + _BETA)
    first = advance()
    unit, _ = _symbol_mark(store, session.id, first.revision.id, "src/a.py", "alpha")
    mark_unit(store, session, first.revision, unit)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", "import x\n\n" + _ALPHA)  # same body, decorator gone
    second = advance()
    _unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is None
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {}


@pytest.mark.parametrize("gap", _GAPS)
def test_a_decorator_kept_across_a_different_gap_is_carried_in_a_real_refresh(tmp_path: Path, gap: str) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n@login_required\n" + gap + _ALPHA + "\n" + _BETA)
    first = advance()
    unit, _ = _symbol_mark(store, session.id, first.revision.id, "src/a.py", "alpha")
    mark_unit(store, session, first.revision, unit)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", "import x\n\n@login_required\n" + _ALPHA)  # no gap any more
    second = advance()
    _unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is not None and mark.state == "reviewed"


@pytest.mark.parametrize("gap", ["\n", "\n\n\n"])
def test_an_undecorated_function_after_a_multi_line_call_is_still_carried_by_a_real_refresh(
    tmp_path: Path, gap: str
) -> None:
    """The statement above the function is not a decorator: its multi-line call must not end up in the header."""

    root, store, session, advance = _reviewed_repo(tmp_path)
    call = "def other(v):\n    return foo(\n        v,\n    )\n"
    _write(root, "src/a.py", _BASE + "\n" + call + gap + _ALPHA + "\n" + _BETA)
    first = advance()
    unit, _ = _symbol_mark(store, session.id, first.revision.id, "src/a.py", "alpha")
    mark_unit(store, session, first.revision, unit)
    _write(root, "src/a.py", _BASE + "\n" + call + "\n" + _BETA)
    _write(root, "src/b.py", "import x\n\n" + _ALPHA)  # same body, never decorated
    second = advance()
    _unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is not None and mark.state == "reviewed"


_ROUTE = '@route(\n    "/a(",\n)\n'


def test_a_multi_line_decorator_with_a_bracket_in_a_string_that_is_dropped_is_not_carried_by_a_real_refresh(
    tmp_path: Path,
) -> None:
    """Round 5's fail-open: ``"/a("`` unbalanced the count, the run was discarded and the header read as ``''``."""

    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n" + _ROUTE + _ALPHA + "\n" + _BETA)
    first = advance()
    unit, _ = _symbol_mark(store, session.id, first.revision.id, "src/a.py", "alpha")
    mark_unit(store, session, first.revision, unit)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", "import x\n\n" + _ALPHA)  # the decorator is gone
    second = advance()
    _unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is None
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {}


def test_a_multi_line_decorator_with_a_bracket_in_a_string_that_is_kept_is_carried_by_a_real_refresh(
    tmp_path: Path,
) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n" + _ROUTE + _ALPHA + "\n" + _BETA)
    first = advance()
    unit, _ = _symbol_mark(store, session.id, first.revision.id, "src/a.py", "alpha")
    mark_unit(store, session, first.revision, unit)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", "import x\n\n" + _ROUTE + _ALPHA)
    second = advance()
    _unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is not None and mark.state == "reviewed"


@pytest.mark.parametrize("gap", ["", "\n"])
def test_a_backslash_continued_decorator_that_is_dropped_is_not_carried_by_a_real_refresh(
    tmp_path: Path, gap: str
) -> None:
    """Round 7's fail-open: the continuation line read as balanced code, so the header became ``''``."""

    root, store, session, advance = _reviewed_repo(tmp_path)
    decorator = "@login_required \\\n    if FLAG else noop\n" + gap
    _write(root, "src/a.py", _BASE + "\n" + decorator + _ALPHA + "\n" + _BETA)
    first = advance()
    unit, _ = _symbol_mark(store, session.id, first.revision.id, "src/a.py", "alpha")
    mark_unit(store, session, first.revision, unit)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", "import x\n\n" + _ALPHA)  # the decorator is gone
    second = advance()
    _unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is None
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {}


def test_a_moved_function_that_keeps_its_decorator_is_carried_by_a_real_refresh(tmp_path: Path) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n" + _ALPHA_DECORATED + "\n" + _BETA)
    first = advance()
    unit, _ = _symbol_mark(store, session.id, first.revision.id, "src/a.py", "alpha")
    mark_unit(store, session, first.revision, unit)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", "import x\n\n" + _ALPHA_DECORATED)
    second = advance()
    unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is not None and mark.state == "reviewed"


def test_a_moved_and_edited_function_is_not_carried_by_a_real_refresh(tmp_path: Path) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", _ALPHA.replace("v + 1", "v + 2"))
    second = advance()
    _unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is None
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {}


def test_a_renamed_function_reopens_with_a_rename_label_by_a_real_refresh(tmp_path: Path) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", _ALPHA.replace("alpha", "alpha_two"))
    second = advance()
    unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha_two")
    assert mark is not None and mark.state == "changed_since_review"
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {
        unit.unit_key: ("renamed", "src/a.py", "alpha")
    }


def test_a_later_set_on_a_copy_survives_the_deletion_of_the_original_by_a_real_refresh(tmp_path: Path) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/b.py", _ALPHA)  # a copy: a.py still has alpha, so nothing moved
    second = advance()
    unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert mark is None
    mark_unit(store, session, second.revision, unit, state="later")
    _write(root, "src/a.py", _BASE + "\n" + _BETA)  # now the original goes, and the move looks unique
    third = advance()
    unit, mark = _symbol_mark(store, session.id, third.revision.id, "src/b.py", "alpha")
    assert mark is not None and mark.state == "later"
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {}
    events = [e for e in store.list_mark_events(session.id, unit_key=unit.unit_key) if e.event_kind == "reconciled"]
    assert not [e for e in events if e.to_state == "reviewed"]


def test_a_judgments_own_note_cannot_forge_a_moved_from_label(tmp_path: Path) -> None:
    _root, store, session, _advance = _reviewed_repo(tmp_path)
    current = store.get_session(session.id).current_revision_id  # type: ignore[union-attr]
    [alpha] = [u for u in store.list_units(current) if (u.kind, u.symbol) == ("symbol", "alpha")]
    revision = store.get_revision(current)
    assert revision is not None
    mark_unit(store, session, revision, alpha, state="needs_changes", note='symbol moved from ["../x",""]')
    forged = [
        e for e in store.list_mark_events(session.id, unit_key=alpha.unit_key) if e.reason.startswith("symbol moved")
    ]
    assert forged, "the note has to reach the audit log for this test to mean anything"
    assert store.list_symbol_move_provenance(session.id, revision_id=current) == {}


def test_the_moved_from_label_is_silent_on_every_revision_but_the_one_the_move_happened_at(tmp_path: Path) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", _ALPHA)
    second = advance()
    unit, _ = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha")
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {
        unit.unit_key: ("moved", "src/a.py", "alpha")
    }
    # The same code is copied back to where it came from: the label would now describe a source that is no longer
    # gone, so a later Snapshot must not carry it, however the judgment itself fares.
    _write(root, "src/a.py", _BASE + "\n" + _BETA + "\n" + _ALPHA)
    third = advance()
    assert third.revision.id != second.revision.id
    assert store.list_symbol_move_provenance(session.id, revision_id=third.revision.id) == {}


def test_rename_detection_reads_no_frozen_text_under_the_transition_lock_and_none_for_the_new_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    root, store, session, advance = _reviewed_repo(tmp_path)
    held = {"locked": False}
    reads: list[tuple[str, bool]] = []
    real_transition, real_read = store.atomic_review_transition, store.read_blob_artifact

    @contextlib.contextmanager
    def transition() -> Any:
        with real_transition() as conn:
            held["locked"] = True
            try:
                yield conn
            finally:
                held["locked"] = False

    def read(review_id: str, revision_id: str) -> Any:
        reads.append((revision_id, held["locked"]))
        return real_read(review_id, revision_id)

    monkeypatch.setattr(store, "atomic_review_transition", transition)
    monkeypatch.setattr(store, "read_blob_artifact", read)
    first_id = store.get_session(session.id).current_revision_id  # type: ignore[union-attr]
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", _ALPHA.replace("alpha", "alpha_two"))  # churn on both sides: digests are needed
    second = advance()
    assert second.revision.id != first_id
    assert [rid for rid, _locked in reads if rid == first_id], "the previous revision's text is read"
    assert not [r for r in reads if r[1]], "a frozen artifact was decompressed while the write lock was held"
    assert second.revision.id not in {rid for rid, _locked in reads}, "the new revision's text is already in memory"
    _unit, mark = _symbol_mark(store, session.id, second.revision.id, "src/b.py", "alpha_two")
    assert mark is not None and mark.state == "changed_since_review"  # and it still recognised the rename


def test_the_symbol_text_is_not_prefetched_when_the_new_revision_has_no_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    from lemoncrow.pro.capabilities.review.sources import local

    root, store, _session, advance = _reviewed_repo(tmp_path)
    held = {"locked": False}
    before_lock: list[str] = []
    real_transition, real_baseline = store.atomic_review_transition, local.baseline_revision

    @contextlib.contextmanager
    def transition() -> Any:
        with real_transition() as conn:
            held["locked"] = True
            try:
                yield conn
            finally:
                held["locked"] = False

    def baseline(*args: Any, **kwargs: Any) -> Any:
        if not held["locked"]:
            before_lock.append("baseline")
        return real_baseline(*args, **kwargs)

    monkeypatch.setattr(store, "atomic_review_transition", transition)
    monkeypatch.setattr(local, "baseline_revision", baseline)
    _write(root, "src/a.py", _BASE + "\n" + _BETA + "\n")  # symbols churn: the prefetch is needed and runs
    advance()
    assert before_lock, "control: with symbols in the new revision the baseline is read before the lock"
    before_lock.clear()
    _write(root, "src/a.py", _BASE)  # back to the base: the only change left is a note, which has no symbols
    _write(root, "notes.txt", "hello\n")
    second = advance()
    assert not [u for u in store.list_units(second.revision.id) if u.kind == "symbol"]
    assert before_lock == [], "nothing can have arrived as a symbol, so nothing is read ahead of the lock"


def test_a_function_copied_to_two_files_is_not_carried_to_either(tmp_path: Path) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", _ALPHA)
    _write(root, "src/c.py", _ALPHA)
    second = advance()
    for path in ("src/b.py", "src/c.py"):
        _unit, mark = _symbol_mark(store, session.id, second.revision.id, path, "alpha")
        assert mark is None
    assert store.list_symbol_move_provenance(session.id, revision_id=second.revision.id) == {}


def test_the_targets_endpoint_delivers_moved_from_with_the_targets_it_already_fetches(tmp_path: Path) -> None:
    root, store, session, advance = _reviewed_repo(tmp_path)
    _write(root, "src/a.py", _BASE + "\n" + _BETA)
    _write(root, "src/b.py", _ALPHA)
    advance()
    app = FastAPI()
    token = "t" * 32
    register_review_api(app, store, auth_dependency=make_token_dependency(token), repo_root=root, port=45998)
    client = TestClient(app, base_url="http://127.0.0.1:45998")
    payload = client.get(f"/api/reviews/{session.id}/targets", headers={"Authorization": f"Bearer {token}"}).json()
    labelled = {row["label"]: row for row in payload["targets"] if row.get("moved_from")}
    assert [row["path"] for row in labelled.values()] == ["src/b.py"]
    [row] = labelled.values()
    assert row["moved_from"] == {"path": "src/a.py"} and row["state"] == "reviewed"
    assert not [t for t in payload["targets"] if t.get("renamed_from")]
