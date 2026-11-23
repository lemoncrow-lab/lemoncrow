"""P11-2: a hunk the reviewer reviewed that was only renumbered by a re-hunking of its file.

A hunk's key is its ordinal in the file, so a hunk inserted or removed anywhere shifts every ordinal below it and every
hunk mark of the file resets (``hunks_renumbered``). When the content of such a hunk is byte for byte one hunk the same
reviewer answered Looks good on the revision before, the server says so (``since_review.rule == "rehunked_identical"``)
and the part waits in the Quick check batch for a person to confirm; it is never carried as reviewed.

Anything ambiguous stays in the read queue: a changed character (also trailing whitespace the fingerprint ignores), the
same hunk twice, a hunk that was not answered Looks good, an answer of any other kind. The differential at the end
builds random re-hunkings from how they were *made*, compares them with ``git diff`` read by the test itself, and its
headline is fail-open == 0.
"""

from __future__ import annotations

import inspect
import random
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review import since_review
from lemoncrow.pro.capabilities.review.sources.local import mark_unit
from tests.gateway.test_review_since_review import _no_astgrep_download, _Repo, _write  # noqa: F401  (autouse fixture)

NOTES = "notes/n.txt"
RULE = "rehunked_identical"


def _plain(changed: Iterable[int], edit: Callable[[int, str], str] = lambda i, line: f"{line} CHANGED") -> str:
    marked = set(changed)
    return "".join(f"{edit(i, f'line {i}') if i in marked else f'line {i}'}\n" for i in range(80))


def _periodic(changes: Mapping[int, str]) -> str:
    """Every hunk of a file built this way has the same context, so two hunks differ only in what they changed."""

    return "".join(f"row {i % 10}{changes.get(i, '')}\n" for i in range(130))


def _commit_base(repo: _Repo, text: str) -> None:
    _write(repo.root, NOTES, text)
    git = pygit2.Repository(str(repo.root))
    git.index.add_all()
    git.index.write()
    signature = pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000001, 0)
    git.create_commit("HEAD", signature, signature, "notes", git.index.write_tree(), [git.head.target])


def _notes(tmp_path: Path, base: str) -> _Repo:
    repo = _Repo(tmp_path)
    _commit_base(repo, base)
    return repo


def _hunks(repo: _Repo) -> dict[int, dict[str, Any]]:
    return {int(label.rsplit("#", 1)[1]): row for label, row in repo.targets().items() if label.startswith(f"{NOTES}#")}


def _routine(row: dict[str, Any]) -> bool:
    proof = row.get("since_review") or {}
    return bool(proof.get("routine")) and proof.get("rule") == RULE


def _judge(
    repo: _Repo, revision: Any, ordinals: Iterable[int], state: str = "reviewed", actor_type: str = "human"
) -> None:
    wanted = set(ordinals)
    for unit in repo.store.list_units(revision.id):
        if unit.kind == "hunk" and unit.path == NOTES and unit.ordinal in wanted:
            mark_unit(repo.store, repo.session, revision, unit, state=state, actor_type=actor_type)


# --------------------------------------------------------------- the rule on a real two-revision refresh

_BASE = _plain(())


def test_a_hunk_that_only_moved_is_routine_and_still_unanswered(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    repo.review_everything()
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))  # a hunk inserted above two reviewed ones
    repo.advance()
    rows = _hunks(repo)
    assert [_routine(rows[i]) for i in range(4)] == [True, False, True, True]
    assert "No hunk you reviewed has this content" in rows[1]["since_review"]["detail"]
    # Nothing is carried as reviewed: every one of them is an open part until a person answers.
    assert {row["state"] for row in rows.values()} == {"unreviewed"}
    assert all(row["answer"]["category"] == "quick_check" for row in rows.values())
    assert rows[2]["since_review"]["detail"] == "The same hunk you reviewed; only its position in the file moved."


def test_a_hunk_removed_above_renumbers_the_ones_below_it(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    repo.review_everything()
    _write(repo.root, NOTES, _plain([15, 30, 60]))
    repo.advance()
    assert [_routine(row) for _, row in sorted(_hunks(repo).items())] == [True, True, True]


@pytest.mark.parametrize(
    ("label", "edit"),
    [
        ("a changed character in the changed line", lambda i, line: f"{line} CHANGED{'!' if i == 30 else ''}"),
        ("a changed character in a line of context", None),
        (
            "trailing whitespace, which the fingerprint ignores",
            lambda i, line: f"{line} CHANGED{' ' if i == 30 else ''}",
        ),
    ],
)
def test_a_one_character_change_is_read_even_when_the_fingerprint_would_match(
    tmp_path: Path, label: str, edit: Callable[[int, str], str] | None
) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    repo.review_everything()
    if edit is None:
        text = _plain([5, 15, 30, 60]).replace("line 32\n", "line 32!\n")  # inside the hunk of line 30's context
    else:
        text = _plain([5, 15, 30, 60], edit)
    _write(repo.root, NOTES, text)
    repo.advance()
    rows = _hunks(repo)
    changed = 2  # the hunk of line 30 is the third now
    assert not _routine(rows[changed]), label
    assert [_routine(rows[i]) for i in (0, 3)] == [True, True], "the untouched hunks stay provable"


def test_two_identical_hunks_before_are_never_one_reviewed_hunk(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _periodic({}))
    _write(repo.root, NOTES, _periodic({5: " X", 35: " X", 65: " Y"}))
    repo.review_everything()
    _write(repo.root, NOTES, _periodic({5: " X", 20: " Z", 35: " X", 65: " Y"}))
    repo.advance()
    rows = _hunks(repo)
    # The two X hunks were reviewed, but "which one" is not certain, so neither is routine. The Y hunk is unique.
    assert [_routine(rows[i]) for i in range(4)] == [False, False, False, True]
    assert "more than once" in rows[0]["since_review"]["detail"]


def test_one_hunk_now_where_two_identical_ones_were_reviewed_is_read(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _periodic({}))
    _write(repo.root, NOTES, _periodic({5: " X", 35: " X", 65: " Y"}))
    repo.review_everything()
    _write(repo.root, NOTES, _periodic({5: " X", 20: " Z", 35: " W", 65: " Y"}))
    repo.advance()
    rows = _hunks(repo)
    assert not _routine(rows[0]), "the reviewer reviewed this twice over; which one it is cannot be told"
    assert _routine(rows[3])


def test_the_same_hunk_twice_now_is_read_even_though_it_was_reviewed_once(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _periodic({}))
    _write(repo.root, NOTES, _periodic({5: " X", 65: " Y"}))
    repo.review_everything()
    _write(repo.root, NOTES, _periodic({5: " X", 20: " Z", 35: " X", 65: " Y"}))  # X copied further down
    repo.advance()
    rows = _hunks(repo)
    assert [_routine(rows[i]) for i in range(4)] == [False, False, False, True]


def test_a_request_for_changes_is_never_closed_by_a_renumbering(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    repo.review_everything(state="needs_changes")
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    repo.advance()
    rows = _hunks(repo)
    assert not any(_routine(row) for row in rows.values())
    assert (
        rows[3]["state"] == "unreviewed"
    ), "the last hunk had no mark; it is still not proved by a request for changes"


def test_a_hunk_the_reviewer_changed_their_mind_about_is_read(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    first = repo.review_everything()
    _judge(repo, first.revision, [1], state="needs_changes")  # a later judgment on the middle hunk
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    repo.advance()
    rows = _hunks(repo)
    assert [_routine(rows[i]) for i in range(4)] == [True, False, False, True]


def test_a_hunk_the_reviewer_never_answered_is_read(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    first = repo.advance()
    _judge(repo, first.revision, [0, 2])  # the middle hunk is left alone
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    repo.advance()
    rows = _hunks(repo)
    assert [_routine(rows[i]) for i in range(4)] == [True, False, False, True]
    assert "not answered Looks good" in rows[2]["since_review"]["detail"]


def test_a_part_left_for_later_is_not_a_reviewed_hunk(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    first = repo.advance()
    _judge(repo, first.revision, [0, 2])
    _judge(repo, first.revision, [1], state="later")
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    repo.advance()
    assert not _routine(_hunks(repo)[2])


def test_a_file_that_was_not_re_hunked_has_no_rehunk_answer(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    repo.review_everything()
    _write(repo.root, NOTES, _plain([5, 30, 45]))  # one hunk replaced by another: the count is the same
    repo.advance()
    rows = _hunks(repo)
    assert not any(_routine(row) for row in rows.values())
    assert RULE not in str(rows[2].get("since_review")), "the file was not re-hunked, so no re-hunk rule applies"


def test_a_reviewer_with_no_earlier_answers_gets_no_rehunk_answer(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    repo.advance()
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    repo.advance()
    assert not any("since_review" in row for row in _hunks(repo).values())


def test_the_proof_outlives_an_answer_given_on_the_new_revision(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    repo.review_everything()
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    second = repo.advance()
    _judge(
        repo, second.revision, [1]
    )  # the new hunk is read and answered; the reviewer's latest look is now this revision
    rows = _hunks(repo)
    assert rows[1]["state"] == "reviewed"
    assert [_routine(rows[i]) for i in (0, 2, 3)] == [True, True, True]


def test_a_proof_is_not_borrowed_across_files(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    repo.review_everything()
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    repo.advance()
    assert not any(label.startswith("src/") and _routine(row) for label, row in repo.targets().items())


def _grown(changes: Mapping[int, str], inserts: Mapping[int, int] | None = None) -> str:
    """``_periodic`` with whole lines inserted after the given rows: an insertion shifts every hunk below it."""

    rows: list[str] = []
    for i in range(130):
        rows.append(f"row {i % 10}{changes.get(i, '')}")
        rows.extend(f"new line {i} {k}" for k in range((inserts or {}).get(i, 0)))
    return "\n".join(rows) + "\n"


def test_a_hunk_inserted_above_shifts_the_ones_below_by_exactly_its_own_lines(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _grown({}))
    _write(repo.root, NOTES, _grown({5: " X", 65: " Y"}))
    repo.review_everything()
    _write(repo.root, NOTES, _grown({5: " X", 65: " Y"}, {20: 2}))
    repo.advance()
    rows = _hunks(repo)
    assert [_routine(rows[i]) for i in range(3)] == [True, False, True]
    assert "No hunk you reviewed has this content" in rows[1]["since_review"]["detail"]


def test_an_identical_change_made_twenty_lines_lower_is_a_different_change(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _periodic({}))
    _write(repo.root, NOTES, _periodic({5: " X", 65: " Y"}))
    repo.review_everything()
    _write(repo.root, NOTES, _periodic({25: " X", 45: " Z", 65: " Y"}))  # the same edit, twenty lines further down
    repo.advance()
    rows = _hunks(repo)
    assert [_routine(rows[i]) for i in range(3)] == [False, False, True]
    assert "different place" in rows[0]["since_review"]["detail"]


def test_a_hunk_under_another_function_is_not_the_one_reviewed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # git prints the enclosing function of a hunk from the *base* file, so it differs between two revisions only when
    # the base moved under them (a rebase): the same bytes then sit in another function.
    repo = _notes(tmp_path, _grown({}))
    _write(repo.root, NOTES, _grown({5: " X", 65: " Y"}))
    repo.review_everything()
    _write(repo.root, NOTES, _grown({5: " X", 65: " Y"}, {20: 2}))
    real = since_review._hunk_patches

    def elsewhere(request: Any, revision_id: str) -> Any:
        patches = real(request, revision_id)
        if patches is None or revision_id != request.revision_id:
            return patches
        return {p: [replace(x, context="def other():") for x in v] for p, v in patches.items()}

    monkeypatch.setattr(since_review, "_hunk_patches", elsewhere)
    repo.advance()
    rows = _hunks(repo)
    assert not any(_routine(row) for row in rows.values())
    assert "different place" in rows[2]["since_review"]["detail"]


def test_a_shift_the_hunks_above_do_not_account_for_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _notes(tmp_path, _grown({}))
    _write(repo.root, NOTES, _grown({5: " X", 65: " Y"}))
    repo.review_everything()
    _write(repo.root, NOTES, _grown({5: " X", 65: " Y"}, {20: 2}))
    real = since_review._hunk_patches

    def drifted(request: Any, revision_id: str) -> Any:
        patches = real(request, revision_id)
        if patches is None or revision_id != request.revision_id:
            return patches
        return {p: [replace(x, new_start=x.new_start + 1) for x in v] for p, v in patches.items()}

    monkeypatch.setattr(since_review, "_hunk_patches", drifted)
    repo.advance()
    rows = _hunks(repo)
    assert not any(_routine(row) for row in rows.values())
    assert all("more than the hunks above" in rows[i]["since_review"]["detail"] for i in (0, 2))


@pytest.mark.parametrize("position", [-1, -7], ids=["missing", "negative"])
def test_a_hunk_whose_position_could_not_be_read_is_not_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: int
) -> None:
    # Every hunk that moves here is a one-line replacement, so the shift above is zero: with the positions unreadable on
    # both revisions, "equal start, equal shift" would hold by accident. Only the guard on the position refuses it.
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    repo.review_everything()
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    real = since_review._hunk_patches

    def unreadable(request: Any, revision_id: str) -> Any:
        patches = real(request, revision_id)
        if patches is None:
            return patches
        return {p: [replace(x, old_start=position, new_start=position) for x in v] for p, v in patches.items()}

    monkeypatch.setattr(since_review, "_hunk_patches", unreadable)
    repo.advance()
    rows = _hunks(repo)
    assert not any(_routine(row) for row in rows.values())
    assert all("position could not be compared" in rows[i]["since_review"]["detail"] for i in (0, 2, 3))


def test_the_revision_list_is_read_once_however_many_files_have_unanswered_hunks(tmp_path: Path) -> None:
    def counted(root: Path, files: int) -> int:
        repo = _Repo(root)
        names = [f"notes/n{k}.txt" for k in range(files)]
        for name in names:
            _write(repo.root, name, _plain(()))
        git = pygit2.Repository(str(repo.root))
        git.index.add_all()
        git.index.write()
        signature = pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000001, 0)
        git.create_commit("HEAD", signature, signature, "notes", git.index.write_tree(), [git.head.target])
        for name in names:
            _write(repo.root, name, _plain([5, 30, 60]))
        repo.review_everything()
        for k, name in enumerate(
            names
        ):  # half are re-hunked, half had one hunk replaced by another (a changed part, no unanswered hunk)
            _write(repo.root, name, _plain([5, 15, 30, 60] if k % 2 else [5, 30, 45]))
        repo.advance()
        calls: list[str] = []
        real = repo.store.list_revisions

        def spy(review_id: str, *args: Any, **kwargs: Any) -> Any:
            if any(frame.filename == since_review.__file__ for frame in inspect.stack(0)):
                calls.append(review_id)
            return real(review_id, *args, **kwargs)

        repo.store.list_revisions = spy  # type: ignore[method-assign]
        repo.targets()
        return len(calls)

    assert counted(tmp_path, 8) == 1


def test_a_judgment_that_a_person_did_not_make_is_never_a_reviewed_hunk(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    first = repo.advance()
    _judge(repo, first.revision, [0, 1, 2], actor_type="agent")
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    repo.advance()
    rows = _hunks(repo)
    assert not any(_routine(row) for row in rows.values())
    assert "not answered Looks good" in rows[2]["since_review"]["detail"]


def test_an_answer_given_to_other_content_at_the_same_ordinal_is_not_an_answer_to_this_hunk(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    first = repo.review_everything()
    _write(repo.root, NOTES, _plain([5, 33, 60]))  # the middle hunk now holds other content, and nobody read it
    second = repo.advance()
    _judge(repo, second.revision, [0])  # a later look at the file: the reviewer's latest revision is this one
    assert first.revision.id != second.revision.id
    _write(repo.root, NOTES, _plain([5, 15, 33, 60]))
    repo.advance()
    rows = _hunks(repo)
    assert not _routine(rows[2]), "the Looks good on this ordinal was given to the content that was there before"
    assert "not answered Looks good" in rows[2]["since_review"]["detail"]


def test_a_file_with_the_same_hunk_count_has_no_rehunk_answer_for_a_hunk_the_reviewer_never_answered(
    tmp_path: Path,
) -> None:
    repo = _notes(tmp_path, _periodic({}))
    _write(repo.root, NOTES, _periodic({5: " X", 35: " Q", 65: " Y"}))
    first = repo.advance()
    _judge(repo, first.revision, [0, 2])
    _write(repo.root, NOTES, _periodic({5: " W", 25: " X", 65: " Y"}))  # three hunks again: nothing was renumbered
    repo.advance()
    rows = _hunks(repo)
    assert rows[1]["state"] == "unreviewed"
    assert "since_review" not in rows[1], "no re-hunking, so there is no re-hunk answer, not even a refusal"


def test_a_hunk_the_reviewer_set_back_to_unanswered_is_not_only_renumbered(tmp_path: Path) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    repo.review_everything()
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    second = repo.advance()
    _judge(repo, second.revision, [2], state="unreviewed")  # the reviewer took their answer back, on purpose
    rows = _hunks(repo)
    assert rows[2]["state"] == "unreviewed" and not _routine(rows[2])
    assert "not reopened only because its file was re-hunked" in rows[2]["since_review"]["detail"]


@pytest.mark.parametrize("which", ["now", "then"])
def test_a_hunk_whose_content_was_not_fingerprinted_as_a_hunk_is_not_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    repo = _notes(tmp_path, _BASE)
    _write(repo.root, NOTES, _plain([5, 30, 60]))
    repo.review_everything()
    _write(repo.root, NOTES, _plain([5, 15, 30, 60]))
    second = repo.advance()
    real = repo.store.list_units

    def odd(revision_id: str, *args: Any, **kwargs: Any) -> Any:
        units = real(revision_id, *args, **kwargs)
        if (revision_id == second.revision.id) != (which == "now"):
            return units
        return [replace(unit, fingerprint_method="unknown") if unit.kind == "hunk" else unit for unit in units]

    monkeypatch.setattr(repo.store, "list_units", odd)
    rows = _hunks(repo)
    assert not any(_routine(row) for row in rows.values())
    details = [str(row["since_review"]["detail"]) for row in rows.values() if "since_review" in row]
    assert any("could not be compared" in detail for detail in details), details


# ------------------------------------------------------------------- a differential, from how each case was made
#
# Ground truth comes from the test's own reading of ``git diff``: the hunk bodies of both revisions, compared as bytes.
# A hunk is safe when its body is the only one of that content now, was the only one before (counting bodies the
# fingerprint cannot tell apart, which is why trailing whitespace is a variant here), the file was re-hunked, and every
# hunk before was answered Looks good. Fail-open: the module said routine and the oracle says it is not safe.

_VARIANTS = (" A", " B", " C", " D", " E", " F", " G", " A ")  # " A " differs from " A" only by trailing whitespace


def _diff_bodies(repo: _Repo) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo.root), "diff", "-U3", "--no-color", "--", NOTES],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    bodies: list[list[str]] = []
    for line in out.splitlines(keepends=True):
        if line.startswith("@@"):
            bodies.append([])
        elif bodies:
            bodies[-1].append(line)
    return ["".join(body) for body in bodies]


def _same_modulo_trailing_space(a: str, b: str) -> bool:
    return [line.rstrip() for line in a.splitlines()] == [line.rstrip() for line in b.splitlines()]


def _safe(now: list[str], before: list[str], ordinal: int) -> bool:
    body = now[ordinal]
    if len(now) == len(before):
        return False
    return (
        sum(_same_modulo_trailing_space(body, other) for other in now) == 1
        and sum(_same_modulo_trailing_space(body, other) for other in before) == 1
        and sum(body == other for other in before) == 1
    )


def _random_states(rng: random.Random) -> tuple[dict[int, str], dict[int, str]]:
    slots = [5 + 10 * m for m in range(12)]
    first = {slot: rng.choice(_VARIANTS) for slot in rng.sample(slots, rng.randint(2, 6))}
    second = dict(first)
    for _ in range(rng.randint(1, 3)):
        op = rng.choice(["add", "drop", "alter", "copy", "twin", "toggle"])
        free = [slot for slot in slots if slot not in second]
        if op == "add" and free:
            second[rng.choice(free)] = rng.choice(_VARIANTS)
        elif op == "drop" and len(second) > 1:
            del second[rng.choice(sorted(second))]
        elif op == "alter" and second:
            second[rng.choice(sorted(second))] = rng.choice(_VARIANTS)
        elif op == "copy" and free and second:
            second[rng.choice(free)] = second[rng.choice(sorted(second))]
        elif op == "toggle" and second:  # trailing whitespace added or removed on a hunk that was reviewed
            slot = rng.choice(sorted(second))
            second[slot] = second[slot].rstrip() if second[slot].endswith(" ") else second[slot] + " "
        elif op == "twin" and free and second:  # the same hunk but for trailing whitespace the fingerprint ignores
            second[rng.choice(free)] = rng.choice(sorted(second.values())).rstrip() + " "
    return first, second


def _differential(tmp_path: Path, trials: int, seed: int) -> Counter[str]:
    rng = random.Random(seed)
    tally: Counter[str] = Counter()
    for trial in range(trials):
        first, second = _random_states(rng)
        (tmp_path / f"t{trial}").mkdir()
        repo = _notes(tmp_path / f"t{trial}", _periodic({}))
        _write(repo.root, NOTES, _periodic(first))
        before = _diff_bodies(repo)
        repo.review_everything()
        _write(repo.root, NOTES, _periodic(second))
        now = _diff_bodies(repo)
        repo.advance()
        rows = _hunks(repo)
        for ordinal, body in enumerate(now):
            row = rows[ordinal]
            if row["state"] != "unreviewed":
                continue
            proved, safe = _routine(row), _safe(now, before, ordinal)
            tally["hunks"] += 1
            if proved and not safe:
                tally["fail_open"] += 1
            elif safe and not proved:
                tally["over_refused"] += 1
            elif proved:
                tally["correct_accept"] += 1
            else:
                tally["correct_refuse"] += 1
            assert body
    return tally


def test_randomized_rehunkings_never_fail_open(tmp_path: Path) -> None:
    tally = _differential(tmp_path, trials=30, seed=20260515)
    assert tally["fail_open"] == 0, tally
    assert tally["over_refused"] == 0, tally
    assert tally["correct_accept"] >= 20, tally
    assert tally["correct_refuse"] >= 20, tally


def test_the_harness_does_see_fail_open_when_trailing_whitespace_is_not_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = since_review._hunk_patches

    def blind(request: Any, revision_id: str) -> Any:
        patches = real(request, revision_id)
        return (
            None
            if patches is None
            else {p: [replace(x, patch=x.patch.replace(" \n", "\n")) for x in v] for p, v in patches.items()}
        )

    monkeypatch.setattr(since_review, "_hunk_patches", blind)
    assert _differential(tmp_path, trials=30, seed=20260515)["fail_open"] > 0


def test_the_harness_does_see_fail_open_when_two_identical_hunks_count_as_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = since_review._hunk_index

    def first_of_each(request: Any, revision_id: str) -> Any:
        index = real(request, revision_id)
        return {
            path: list({unit.content_fingerprint: unit for unit in reversed(units)}.values())
            for path, units in index.items()
        }

    monkeypatch.setattr(since_review, "_hunk_index", first_of_each)
    assert _differential(tmp_path, trials=30, seed=20260515)["fail_open"] > 0
