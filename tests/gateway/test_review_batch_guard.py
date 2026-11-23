"""The Quick check batch guard on real git diffs: arithmetic or a loop bound changed while the test did not run."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.answer_category import Answer
from lemoncrow.pro.capabilities.review.batch_guard import batch_guards, classify_change, guard_for_target
from lemoncrow.pro.capabilities.review.change_risk import _rows
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetSpan, TargetVerification

_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _hunks(tmp_path: Path, name: str, base: str, head: str) -> list[dict[str, Any]]:
    repo = Path(tempfile.mkdtemp(dir=tmp_path))

    def git(*args: str) -> str:
        done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        return done.stdout.decode("utf-8")  # bytes: text mode would turn a lone carriage return into a newline

    git("init", "-q")
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_bytes(base.encode("utf-8"))
    git("add", name)
    git("-c", "user.email=<redacted-email>", "-c", "user.name=t", "commit", "-qm", "base")
    (repo / name).write_bytes(head.encode("utf-8"))
    hunks: list[dict[str, Any]] = []
    for line in git("diff", "--no-color", "-U3", "--", name).split("\n"):
        match = _HEADER.match(line)
        if match:
            old_start, old_lines, new_start, new_lines = match.groups()
            hunks.append(
                {
                    "old_start": int(old_start),
                    "old_lines": int(old_lines if old_lines is not None else 1),
                    "new_start": int(new_start),
                    "new_lines": int(new_lines if new_lines is not None else 1),
                    "patch": "",
                }
            )
        elif hunks and line[:1] in (" ", "+", "-", "\\"):
            hunks[-1]["patch"] += f"{line}\n"
    return hunks


def _spans(hunks: list[dict[str, Any]]) -> tuple[TargetSpan, ...]:
    """One span per changed line, the way the target projection owns changed lines."""

    out: list[TargetSpan] = []
    for ordinal, hunk in enumerate(hunks):
        for row in _rows(hunk):
            if row.tag == "+":
                out.append(TargetSpan("new", row.new, row.new, ordinal))
            elif row.tag == "-":
                out.append(TargetSpan("old", row.old, row.old, ordinal))
    return tuple(out)


def _target(
    hunks: list[dict[str, Any]], path: str, *, ran: bool = False, category: str = "quick_check"
) -> ReviewTarget:
    return ReviewTarget(
        target_id=f"target:{path}",
        unit_key=path,
        kind="symbol",
        path=path,
        label=path,
        symbol="f",
        start_line=1,
        end_line=50,
        hunk_ordinals=tuple(range(len(hunks))),
        spans=_spans(hunks),
        state="unreviewed",
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=0,
        attention_level="normal",
        reasons=(),
        additions=0,
        deletions=0,
        fingerprint_method="ast",
        verification=TargetVerification(pass_count=1) if ran else TargetVerification(unknown_count=1),
        answer=Answer(category),  # type: ignore[arg-type]
    )


def _guard(tmp_path: Path, name: str, base: str, head: str, *, ran: bool = False) -> dict[str, Any]:
    hunks = _hunks(tmp_path, name, base, head)
    target = _target(hunks, name, ran=ran)
    return guard_for_target(target, {"path": name, "status": "modified", "hunks": hunks}).payload()


ORDER_TOTAL = "def order_total(items):\n    total = 0\n    for i in range(len(items)):\n        total += items[i].price\n    return total\n"


def test_a_loop_bound_that_stops_one_item_early_is_guarded_with_its_reason(tmp_path: Path) -> None:
    head = ORDER_TOTAL.replace("range(len(items))", "range(len(items) - 1)")
    assert _guard(tmp_path, "app/orders.py", ORDER_TOTAL, head) == {
        "guarded": True,
        "code": "loop_bound",
        "reason": "Test didn't run and the loop bound changed.",
    }


def test_a_loop_over_a_different_collection_is_a_loop_bound_change(tmp_path: Path) -> None:
    guard = _guard(
        tmp_path, "app/orders.py", ORDER_TOTAL, ORDER_TOTAL.replace("range(len(items))", "range(len(other))")
    )
    assert guard["code"] == "loop_bound" and guard["guarded"] is True


def test_changed_arithmetic_is_guarded(tmp_path: Path) -> None:
    head = ORDER_TOTAL.replace("total += items[i].price", "total -= items[i].price")
    guard = _guard(tmp_path, "app/orders.py", ORDER_TOTAL, head)
    assert (guard["guarded"], guard["code"], guard["reason"]) == (
        True,
        "arithmetic",
        "Test didn't run and the arithmetic changed.",
    )


def test_a_changed_constant_is_arithmetic(tmp_path: Path) -> None:
    base = "def pct(x):\n    return x * 100\n"
    assert _guard(tmp_path, "app/pct.py", base, base.replace("100", "10"))["code"] == "arithmetic"


def test_a_comparison_flip_is_guarded_and_in_a_loop_header_it_is_the_loop_bound(tmp_path: Path) -> None:
    base = "def f(i, n):\n    if i < n:\n        return 1\n    return 0\n"
    flipped = _guard(tmp_path, "app/f.py", base, base.replace("i < n", "i <= n"))
    assert (flipped["guarded"], flipped["code"]) == (True, "comparison")
    loop = "def g(n):\n    i = 0\n    while i < n:\n        i += 1\n"
    assert _guard(tmp_path, "app/g.py", loop, loop.replace("i < n", "i <= n"))["code"] == "loop_bound"


def test_a_script_loop_bound_is_read_too(tmp_path: Path) -> None:
    base = "export function sum(xs) {\n  let t = 0;\n  for (let i = 0; i < xs.length; i++) {\n    t += xs[i];\n  }\n  return t;\n}\n"
    guard = _guard(tmp_path, "src/sum.ts", base, base.replace("i < xs.length", "i < xs.length - 1"))
    assert (guard["guarded"], guard["code"]) == (True, "loop_bound")


def test_a_rename_only_part_is_not_guarded(tmp_path: Path) -> None:
    head = ORDER_TOTAL.replace("total", "running")
    assert _guard(tmp_path, "app/orders.py", ORDER_TOTAL, head)["guarded"] is False


def test_renaming_only_the_loop_variable_is_not_guarded(tmp_path: Path) -> None:
    base = "def names(xs):\n    out = []\n    for x in xs:\n        out.append(x.name)\n    return out\n"
    head = base.replace("for x in xs", "for item in xs").replace("x.name", "item.name")
    assert _guard(tmp_path, "app/names.py", base, head)["guarded"] is False


def test_a_comment_only_change_is_not_guarded(tmp_path: Path) -> None:
    head = ORDER_TOTAL.replace("    total = 0\n", "    total = 0  # start from zero, 1 + 1 = 2\n")
    assert _guard(tmp_path, "app/orders.py", ORDER_TOTAL, head)["guarded"] is False


def test_a_string_only_change_is_not_guarded(tmp_path: Path) -> None:
    base = 'def label():\n    return "Total: 10"\n'
    assert _guard(tmp_path, "app/label.py", base, base.replace("Total: 10", "Sum: 12"))["guarded"] is False


def test_a_moved_unchanged_line_is_not_guarded(tmp_path: Path) -> None:
    base = "def f(a, b):\n    x = a + b\n    y = a * b\n    return x, y\n"
    head = "def f(a, b):\n    y = a * b\n    x = a + b\n    return x, y\n"
    assert _guard(tmp_path, "app/f.py", base, head)["guarded"] is False


def test_a_result_posted_on_the_path_does_not_release_the_guard(tmp_path: Path) -> None:
    # `pass_count` is any file-level verification row on the path (author, agent, CI or lint). It is not the
    # reviewer's own Run test, so it must never unguard a part (D8).
    head = ORDER_TOTAL.replace("range(len(items))", "range(len(items) - 1)")
    hunks = _hunks(tmp_path, "app/orders.py", ORDER_TOTAL, head)
    for verification in (TargetVerification(pass_count=3), TargetVerification(fail_count=1), TargetVerification()):
        target = ReviewTarget(**{**_target(hunks, "app/orders.py").__dict__, "verification": verification})
        guard = guard_for_target(target, {"hunks": hunks})
        assert (guard.guarded, guard.code) == (True, "loop_bound")


@pytest.mark.parametrize(
    ("name", "base", "head", "code"),
    [
        (
            "slice bound",
            "def f(xs, limit, count):\n    return xs[:limit]\n",
            "def f(xs, limit, count):\n    return xs[:count]\n",
            "loop_bound",
        ),
        (
            "range operand",
            "def f(n, m):\n    return list(range(n))\n",
            "def f(n, m):\n    return list(range(m))\n",
            "loop_bound",
        ),
        (
            "multi-line for header",
            "def f(n, m):\n    for i in range(\n        0,\n        n,\n    ):\n        use(i)\n",
            "def f(n, m):\n    for i in range(\n        0,\n        m,\n    ):\n        use(i)\n",
            "loop_bound",
        ),
        (
            "operand swapped on an arithmetic line",
            "def f(price, qty, discount):\n    return price * qty\n",
            "def f(price, qty, discount):\n    return price * discount\n",
            "arithmetic",
        ),
        (
            "bitwise assignment",
            "def f(flags, m):\n    flags |= m\n    return flags\n",
            "def f(flags, m):\n    flags &= m\n    return flags\n",
            "arithmetic",
        ),
    ],
)
def test_real_changes_the_old_reader_missed_are_guarded(
    tmp_path: Path, name: str, base: str, head: str, code: str
) -> None:
    guard = _guard(tmp_path, "app/m.py", base, head)
    assert (guard["guarded"], guard["code"]) == (True, code), name


def test_script_slice_and_template_expressions_are_read(tmp_path: Path) -> None:
    base = "export function f(xs, n, m) {\n  return xs.slice(0, n);\n}\n"
    assert _guard(tmp_path, "src/f.ts", base, base.replace("0, n", "0, m"))["code"] == "loop_bound"
    tpl = "export const g = (x) => {\n  return `total ${x + 1}`;\n};\n"
    assert _guard(tmp_path, "src/g.ts", tpl, tpl.replace("x + 1", "x + 2"))["code"] == "arithmetic"
    text_only = tpl.replace("total ", "sum ")
    assert _guard(tmp_path, "src/g.ts", tpl, text_only)["guarded"] is False


def test_a_partial_rename_is_a_change_but_a_whole_rename_is_not(tmp_path: Path) -> None:
    base = "def f(price, qty, other):\n    cost = price * qty\n    return cost\n"
    whole = base.replace("price", "unit_price")
    assert _guard(tmp_path, "app/f.py", base, whole)["guarded"] is False
    swapped = base.replace("price * qty", "price * other")
    assert _guard(tmp_path, "app/f.py", base, swapped)["guarded"] is True
    # An operand that looks like a rename but binds nothing in the changed lines is a different value.
    lone = "def f():\n" + "    pass\n" * 6 + "    return n + 1\n"
    assert _guard(tmp_path, "app/h.py", lone, lone.replace("return n + 1", "return m + 1"))["guarded"] is True


@pytest.mark.parametrize("bad", ['x = "unterminated', "x = 1 ¤ 2", "tick = `open"])
def test_text_that_cannot_be_read_fails_closed_even_when_tests_ran(tmp_path: Path, bad: str) -> None:
    base = "x = 0\n"
    guard = _guard(tmp_path, "app/bad.py", base, f"{bad}\n")
    assert guard["guarded"] is True and guard["code"] == "unverifiable"
    assert (
        guard["reason"] == "Could not be checked: the changed text could not be read. This part stays out of the batch."
    )


def test_a_file_type_it_does_not_read_fails_closed(tmp_path: Path) -> None:
    base = "package main\nfunc f() int { return 1 }\n"
    guard = _guard(tmp_path, "main.go", base, base.replace("1", "2"))
    assert guard["code"] == "unverifiable" and "not read" in guard["reason"]


def test_missing_diff_text_fails_closed(tmp_path: Path) -> None:
    hunks = _hunks(
        tmp_path, "app/orders.py", ORDER_TOTAL, ORDER_TOTAL.replace("range(len(items))", "range(len(items) - 1)")
    )
    target = _target(hunks, "app/orders.py")
    assert guard_for_target(target, None).code == "unverifiable"
    assert guard_for_target(target, {"hunks": [{"patch": ""}]}).code == "unverifiable"
    assert guard_for_target(ReviewTarget(**{**target.__dict__, "spans": ()}), {"hunks": hunks}).code == "unverifiable"


def test_docs_config_and_test_files_are_not_guarded(tmp_path: Path) -> None:
    assert _guard(tmp_path, "docs/a.md", "total 1 + 1\n", "total 2 + 2\n")["guarded"] is False
    assert _guard(tmp_path, "config/a.yaml", "retries: 3\n", "retries: 5\n")["guarded"] is False
    test_base = "def test_a():\n    assert 1 + 1 == 2\n"
    assert _guard(tmp_path, "tests/test_a.py", test_base, test_base.replace("2\n", "3\n"))["guarded"] is False


def test_only_quick_checks_get_a_guard(tmp_path: Path) -> None:
    hunks = _hunks(
        tmp_path, "app/orders.py", ORDER_TOTAL, ORDER_TOTAL.replace("range(len(items))", "range(len(items) - 1)")
    )
    quick = _target(hunks, "app/orders.py")
    decision = _target(hunks, "app/other.py", category="decision")
    out = batch_guards(
        {"files": [{"path": "app/orders.py", "hunks": hunks}, {"path": "app/other.py", "hunks": hunks}]},
        [quick, decision],
    )
    assert list(out) == ["target:app/orders.py"] and out["target:app/orders.py"]["guarded"] is True
    assert batch_guards(None, [quick])["target:app/orders.py"]["code"] == "unverifiable"


def test_classifier_helper_reports_nothing_for_identical_rows() -> None:
    assert classify_change(["a = b + 1"], ["a = b + 1"], "python") == ""


_SUM = "def total_of(items):\n    total = 0\n    for item in items:\n        total += item.price\n    return total\n"


def test_indenting_a_return_into_the_loop_body_is_a_loop_change(tmp_path: Path) -> None:
    head = _SUM.replace("    return total\n", "        return total\n")
    guard = _guard(tmp_path, "app/sum.py", _SUM, head)
    assert (guard["guarded"], guard["code"]) == (True, "loop_bound")


def test_moving_a_statement_out_of_the_loop_is_a_loop_change(tmp_path: Path) -> None:
    base = "def count_up(items):\n    count = 0\n    for item in items:\n        use(item)\n        count += 1\n    return count\n"
    head = base.replace("        count += 1\n    return count\n", "    count += 1\n    return count\n")
    guard = _guard(tmp_path, "app/count.py", base, head)
    assert (guard["guarded"], guard["code"]) == (True, "loop_bound")


def test_moving_arithmetic_out_of_an_if_body_is_guarded(tmp_path: Path) -> None:
    base = "def f(total, flag):\n    if flag:\n        total = total * 2\n    return total\n"
    head = base.replace("        total = total * 2\n", "    total = total * 2\n")
    assert _guard(tmp_path, "app/f.py", base, head)["guarded"] is True


def test_a_pure_reindent_with_the_same_block_structure_is_not_guarded(tmp_path: Path) -> None:
    base = "def total_of(items):\n  total = 0\n  for item in items:\n    total += item.price\n  return total\n"
    head = (
        "def total_of(items):\n    total = 0\n    for item in items:\n        total += item.price\n    return total\n"
    )
    assert _guard(tmp_path, "app/sum.py", base, head)["guarded"] is False


def test_a_reindent_whose_block_is_outside_the_diff_fails_closed(tmp_path: Path) -> None:
    body = "".join(f"        step_{n}(item)\n" for n in range(6))
    base = f"def f(items):\n    for item in items:\n{body}        finish(item)\n"
    head = base.replace("        finish(item)\n", "    finish(item)\n")
    guard = _guard(tmp_path, "app/f.py", base, head)
    assert (guard["guarded"], guard["code"]) == (True, "unverifiable")
    assert "block is not in the diff" in guard["reason"]


def test_break_to_continue_and_a_changed_comparison_operand_are_guarded(tmp_path: Path) -> None:
    base = "def f(xs, n, m):\n    for x in xs:\n        if x:\n            break\n"
    assert _guard(tmp_path, "app/b.py", base, base.replace("break", "continue"))["code"] == "loop_bound"
    cmp_base = "def g(i, n, m):\n    if i < n:\n        return 1\n    return 0\n"
    assert _guard(tmp_path, "app/c.py", cmp_base, cmp_base.replace("i < n", "i < m"))["code"] == "comparison"


# --- A statement moved across a block boundary: the block chain is part of the move ---------------------------

_PY_BLOCKS = {
    "two loops": (
        "def f(a, b):\n    for x in a:\n        use(x)\n        log(x)\n    for y in b:\n        use(y)\n",
        "def f(a, b):\n    for x in a:\n        use(x)\n    for y in b:\n        use(y)\n        log(x)\n",
        "loop_bound",
    ),
    "if to else": (
        "def f(a):\n    if a:\n        one()\n        two()\n    else:\n        three()\n",
        "def f(a):\n    if a:\n        one()\n    else:\n        three()\n        two()\n",
        "block_moved",
    ),
    "if to elif": (
        "def f(a, b):\n    if a:\n        one()\n        two()\n    elif b:\n        three()\n",
        "def f(a, b):\n    if a:\n        one()\n    elif b:\n        three()\n        two()\n",
        "block_moved",
    ),
    "try to except": (
        "def f():\n    try:\n        load()\n        save()\n    except OSError:\n        retry()\n",
        "def f():\n    try:\n        load()\n    except OSError:\n        retry()\n        save()\n",
        "block_moved",
    ),
    "with to for": (
        "def f(path, xs):\n    with open(path) as fh:\n        read(fh)\n        log(fh)\n    for x in xs:\n        use(x)\n",
        "def f(path, xs):\n    with open(path) as fh:\n        read(fh)\n    for x in xs:\n        use(x)\n        log(fh)\n",
        "loop_bound",
    ),
    "for to with": (
        "def f(path, xs):\n    for x in xs:\n        use(x)\n        log(x)\n    with open(path) as fh:\n        read(fh)\n",
        "def f(path, xs):\n    for x in xs:\n        use(x)\n    with open(path) as fh:\n        read(fh)\n        log(x)\n",
        "loop_bound",
    ),
    "else back to if": (
        "def f(a):\n    if a:\n        one()\n    else:\n        three()\n        two()\n",
        "def f(a):\n    if a:\n        one()\n        two()\n    else:\n        three()\n",
        "block_moved",
    ),
    "into a second try body": (
        "def f():\n    try:\n        load()\n    finally:\n        close()\n        note()\n",
        "def f():\n    try:\n        load()\n        note()\n    finally:\n        close()\n",
        "block_moved",
    ),
}


@pytest.mark.parametrize("name", sorted(_PY_BLOCKS))
def test_a_python_statement_moved_across_a_block_at_the_same_indent_is_guarded(tmp_path: Path, name: str) -> None:
    base, head, code = _PY_BLOCKS[name]
    guard = _guard(tmp_path, "app/blocks.py", base, head)
    assert (guard["guarded"], guard["code"]) == (True, code)


def test_the_same_python_statement_moved_inside_one_block_is_not_guarded(tmp_path: Path) -> None:
    cases = [
        (
            "def f(a, b):\n    for x in a:\n        use(x)\n        log(x)\n    done(b)\n",
            "def f(a, b):\n    for x in a:\n        log(x)\n        use(x)\n    done(b)\n",
        ),
        (
            "def f(a):\n    if a:\n        one()\n    else:\n        three()\n        two()\n",
            "def f(a):\n    if a:\n        one()\n    else:\n        two()\n        three()\n",
        ),
        (
            "def f():\n    try:\n        load()\n        save()\n    except OSError:\n        retry()\n",
            "def f():\n    try:\n        save()\n        load()\n    except OSError:\n        retry()\n",
        ),
    ]
    for base, head in cases:
        assert _guard(tmp_path, "app/same.py", base, head)["guarded"] is False


def test_two_loops_with_the_same_header_still_count_as_two_blocks(tmp_path: Path) -> None:
    base = "def f(xs):\n    for x in xs:\n        use(x)\n        log(x)\n    for x in xs:\n        use(x)\n"
    head = "def f(xs):\n    for x in xs:\n        use(x)\n    for x in xs:\n        use(x)\n        log(x)\n"
    assert _guard(tmp_path, "app/twin.py", base, head)["code"] == "loop_bound"


def test_a_python_statement_moved_between_blocks_whose_headers_are_outside_the_diff_is_guarded(tmp_path: Path) -> None:
    body = "".join(f"        step_{n}(item)\n" for n in range(6))
    base = (
        f"def f(items):\n    for item in items:\n{body}        log(item)\n    for other in items:\n        use(other)\n"
    )
    head = base.replace("        log(item)\n", "").replace(
        "        use(other)\n", "        use(other)\n        log(item)\n"
    )
    assert _guard(tmp_path, "app/far.py", base, head)["guarded"] is True


_JS_BLOCKS = {
    "for": (
        "function f(xs) {\n  for (const x of xs) {\n    use(x);\n    log(x);\n  }\n  done();\n}\n",
        "function f(xs) {\n  for (const x of xs) {\n    use(x);\n  }\n  log(xs);\n  done();\n}\n",
        "loop_bound",
    ),
    "into a for": (
        "function f(xs) {\n  for (const x of xs) {\n    use(x);\n  }\n  log(xs);\n  done();\n}\n",
        "function f(xs) {\n  for (const x of xs) {\n    use(x);\n    log(xs);\n  }\n  done();\n}\n",
        "loop_bound",
    ),
    "while": (
        "function f(a) {\n  while (a) {\n    use(a);\n    log(a);\n  }\n  done();\n}\n",
        "function f(a) {\n  while (a) {\n    use(a);\n  }\n  log(a);\n  done();\n}\n",
        "loop_bound",
    ),
    "if to else": (
        "function f(a) {\n  if (a) {\n    one();\n    two();\n  } else {\n    three();\n  }\n}\n",
        "function f(a) {\n  if (a) {\n    one();\n  } else {\n    three();\n    two();\n  }\n}\n",
        "block_moved",
    ),
    "try to catch": (
        "function f() {\n  try {\n    load();\n    save();\n  } catch (e) {\n    retry();\n  }\n}\n",
        "function f() {\n  try {\n    load();\n  } catch (e) {\n    retry();\n    save();\n  }\n}\n",
        "block_moved",
    ),
    "function body to arrow body": (
        "function f(xs) {\n  log(xs);\n  return xs.map((x) => {\n    return use(x);\n  });\n}\n",
        "function f(xs) {\n  return xs.map((x) => {\n    log(xs);\n    return use(x);\n  });\n}\n",
        "block_moved",
    ),
    "out of an arrow body": (
        "function f(xs) {\n  return xs.map((x) => {\n    log(xs);\n    return use(x);\n  });\n}\n",
        "function f(xs) {\n  log(xs);\n  return xs.map((x) => {\n    return use(x);\n  });\n}\n",
        "block_moved",
    ),
    "into a nested function": (
        "function f(xs) {\n  setup(xs);\n  function inner(y) {\n    return use(y);\n  }\n  return inner;\n}\n",
        "function f(xs) {\n  function inner(y) {\n    setup(xs);\n    return use(y);\n  }\n  return inner;\n}\n",
        "block_moved",
    ),
    "switch case": (
        "function f(a) {\n  switch (a) {\n    case 1:\n      one();\n      log(a);\n      break;\n    case 2:\n      two();\n      break;\n  }\n}\n",
        "function f(a) {\n  switch (a) {\n    case 1:\n      one();\n      break;\n    case 2:\n      two();\n      log(a);\n      break;\n  }\n}\n",
        "block_moved",
    ),
}


@pytest.mark.parametrize("name", sorted(_JS_BLOCKS))
@pytest.mark.parametrize("suffix", ["ts", "js"])
def test_a_script_statement_moved_across_braces_is_guarded(tmp_path: Path, name: str, suffix: str) -> None:
    base, head, code = _JS_BLOCKS[name]
    guard = _guard(tmp_path, f"app/blocks.{suffix}", base, head)
    assert (guard["guarded"], guard["code"]) == (True, code)


def test_a_script_statement_moved_inside_one_block_is_not_guarded(tmp_path: Path) -> None:
    cases = [
        (
            "function f(a) {\n  if (a) {\n    one();\n  } else {\n    three();\n    two();\n  }\n}\n",
            "function f(a) {\n  if (a) {\n    one();\n  } else {\n    two();\n    three();\n  }\n}\n",
        ),
        (
            "function f() {\n  setup();\n  run();\n  return 1;\n}\n",
            "function f() {\n  run();\n  setup();\n  return 1;\n}\n",
        ),
    ]
    for base, head in cases:
        assert _guard(tmp_path, "app/same.ts", base, head)["guarded"] is False


def test_a_script_statement_leaving_a_loop_whose_header_is_outside_the_diff_is_guarded(tmp_path: Path) -> None:
    body = "".join(f"    step{n}(item);\n" for n in range(6))
    base = f"function f(items) {{\n  for (const item of items) {{\n{body}    log(item);\n  }}\n  done(items);\n}}\n"
    head = base.replace("    log(item);\n  }\n", "  }\n  log(item);\n")
    assert _guard(tmp_path, "app/far.ts", base, head)["guarded"] is True


def test_a_script_move_next_to_braces_that_do_not_balance_fails_closed(tmp_path: Path) -> None:
    base = "function f(a) {\n  one(a);\n  two(a);\n  return a;\n}\n"
    head = "function f(a) {\n  two(a);\n  one(a);\n  return a;\n}\n}\n"
    guard = _guard(tmp_path, "app/odd.ts", base, head)
    assert (guard["guarded"], guard["code"]) == (True, "unverifiable")
    assert "blocks around a moved line could not be read" in guard["reason"]


def test_a_script_move_beside_a_body_without_braces_fails_closed(tmp_path: Path) -> None:
    base = "function f(xs) {\n  for (const x of xs)\n    use(x);\n  log(xs);\n  done();\n}\n"
    head = "function f(xs) {\n  for (const x of xs)\n    use(x);\n    log(xs);\n  done();\n}\n"
    guard = _guard(tmp_path, "app/bare.ts", base, head)
    assert guard["guarded"] is True


def test_a_statement_removed_in_one_hunk_and_added_in_another_fails_closed(tmp_path: Path) -> None:
    filler = "".join(f"keep_{n}()\n" for n in range(12))
    base = f"one()\nlog(a)\n{filler}two()\n"
    head = f"one()\n{filler}two()\nlog(a)\n"
    guard = _guard(tmp_path, "app/hunks.py", base, head)
    assert (guard["guarded"], guard["code"]) == (True, "unverifiable")
    assert "from one changed spot to another" in guard["reason"]


def test_rows_inside_a_multi_row_template_literal_are_text_not_statements(tmp_path: Path) -> None:
    base = "function f(a) {\n  const t = `open\n  one(a);\n  two(a);\n  return t;\n`;\n}\n"
    swapped = base.replace("  one(a);\n  two(a);\n", "  two(a);\n  one(a);\n")
    assert _guard(tmp_path, "app/tpl.ts", base, swapped)["guarded"] is False


# --- Round 5: the cases that got past the block reading, and the coarse net under it ----------------------------

_FAR = "".join(f"  pad{n}();\n" for n in range(10))
_HELD = {
    "backslash while header, body moved out": (
        "app/a.py",
        "def f(a, b, x):\n    while a and \\\n            b:\n        send(x)\n        step()\n    done()\n",
        "def f(a, b, x):\n    while a and \\\n            b:\n        step()\n    send(x)\n    done()\n",
    ),
    "backslash if header, body moved to a sibling": (
        "app/a.py",
        "def f(a, b, x):\n    if a and \\\n            b:\n        send(x)\n        one()\n    if b:\n        two()\n",
        "def f(a, b, x):\n    if a and \\\n            b:\n        one()\n    if b:\n        send(x)\n        two()\n",
    ),
    "backslash with header, body moved out": (
        "app/a.py",
        "def f(a, b, x):\n    with a, \\\n            b:\n        send(x)\n        step()\n    done()\n",
        "def f(a, b, x):\n    with a, \\\n            b:\n        step()\n    send(x)\n    done()\n",
    ),
    "flush-left string between sibling loops": (
        "app/a.py",
        'def f(xs, ys, x):\n    for a in xs:\n        send(x)\n        keep(a)\n    SQL = """\nSELECT id FROM users\n"""\n'
        "    for b in ys:\n        other(b)\n",
        'def f(xs, ys, x):\n    for a in xs:\n        keep(a)\n    SQL = """\nSELECT id FROM users\n"""\n'
        "    for b in ys:\n        send(x)\n        other(b)\n",
    ),
    "no-argument call out of a loop, far away": (
        "app/a.ts",
        f"function f(xs) {{\n  for (const x of xs) {{\n    cleanup();\n    use(x);\n  }}\n{_FAR}  done();\n}}\n",
        f"function f(xs) {{\n  for (const x of xs) {{\n    use(x);\n  }}\n{_FAR}  cleanup();\n  done();\n}}\n",
    ),
    "no-argument call into a loop, far away": (
        "app/a.ts",
        f"function f(xs) {{\n  release();\n  for (const x of xs) {{\n    use(x);\n  }}\n{_FAR}  done();\n}}\n",
        f"function f(xs) {{\n  for (const x of xs) {{\n    use(x);\n    release();\n  }}\n{_FAR}  done();\n}}\n",
    ),
    "python one-line if replaced by its body": (
        "app/a.py",
        "def f(ok, msg):\n    prep()\n    if ok: send(msg)\n    done()\n",
        "def f(ok, msg):\n    prep()\n    send(msg)\n    done()\n",
    ),
    "python one-line with": (
        "app/a.py",
        "def f(lock, x):\n    prep()\n    with lock: send(x)\n    done()\n",
        "def f(lock, x):\n    prep()\n    send(x)\n    done()\n",
    ),
    "python one-line try": (
        "app/a.py",
        "def f(x):\n    prep()\n    try: send(x)\n    except OSError: pass\n    done()\n",
        "def f(x):\n    prep()\n    send(x)\n    except OSError: pass\n    done()\n",
    ),
    "script braceless if replaced by its body": (
        "app/a.ts",
        "function f(ok, msg) {\n  prep();\n  if (ok) send(msg);\n  done();\n}\n",
        "function f(ok, msg) {\n  prep();\n  send(msg);\n  done();\n}\n",
    ),
    "script one-line braced if replaced by its body": (
        "app/a.ts",
        "function f(ok, x) {\n  prep();\n  if (ok) { send(x); }\n  done();\n}\n",
        "function f(ok, x) {\n  prep();\n  send(x);\n  done();\n}\n",
    ),
    "script braceless else replaced by its body": (
        "app/a.ts",
        "function f(ok, x) {\n  prep();\n  if (ok) {\n    one();\n  } else send(x);\n  done();\n}\n",
        "function f(ok, x) {\n  prep();\n  if (ok) {\n    one();\n  }\n  send(x);\n  done();\n}\n",
    ),
}


@pytest.mark.parametrize("name", sorted(_HELD))
def test_changes_that_got_past_the_block_reading_are_held(tmp_path: Path, name: str) -> None:
    path, base, head = _HELD[name]
    assert _guard(tmp_path, path, base, head)["guarded"] is True


def _guard_owning(
    tmp_path: Path, name: str, base: str, head: str, *, new_lines: set[int], old_lines: set[int]
) -> dict[str, Any]:
    """The guard for one of two parts that share a hunk: it owns only these changed lines."""

    hunks = _hunks(tmp_path, name, base, head)
    spans = tuple(
        TargetSpan("new" if row.tag == "+" else "old", n, n, ordinal)
        for ordinal, hunk in enumerate(hunks)
        for row in _rows(hunk)
        for n in [row.new if row.tag == "+" else row.old]
        if (row.tag == "+" and n in new_lines) or (row.tag == "-" and n in old_lines)
    )
    target = replace(_target(hunks, name), spans=spans)
    return guard_for_target(target, {"path": name, "status": "modified", "hunks": hunks}).payload()


def test_a_wrap_without_reindenting_is_held_when_another_part_shares_the_hunk(tmp_path: Path) -> None:
    base = "function f(ok, x) {\n  prep();\n  send(x);\n}\nfunction g(y) {\n  return y;\n}\n"
    head = "function f(ok, x) {\n  prep();\n  if (ok) {\n  send(x);\n  }\n}\nfunction g(y) {\n  return y * 2;\n}\n"
    guard = _guard_owning(tmp_path, "app/a.ts", base, head, new_lines={3, 5}, old_lines=set())
    assert guard["guarded"] is True
    unwrapped = _guard_owning(tmp_path, "app/a.ts", head, base, new_lines={2}, old_lines={3, 5})
    assert unwrapped["guarded"] is True


def test_the_net_leaves_a_rename_a_changed_operand_and_a_pure_reformat_alone(tmp_path: Path) -> None:
    rename = "def f(price, qty):\n    cost = price * qty\n    return cost\n"
    assert _guard(tmp_path, "app/a.py", rename, rename.replace("price", "unit"))["guarded"] is False
    script = "export const f = (a) => {\n  const t = `n ${a}`;\n  return t;\n};\n"
    assert _guard(tmp_path, "app/a.ts", script, script.replace("`n ", "`m "))["guarded"] is False
    comment = "function f(a) {\n  // keep {braces} out\n  return a;\n}\n"
    assert _guard(tmp_path, "app/a.ts", comment, comment.replace("keep", "leave"))["guarded"] is False
    reformat = "def f(a):\n  if a:\n    run(a)\n  return a\n"
    assert _guard(tmp_path, "app/a.py", reformat, reformat.replace("  ", "    "))["guarded"] is False
    swap = "function f(a) {\n  one(a);\n  two(a);\n  return a;\n}\n"
    assert (
        _guard(tmp_path, "app/a.ts", swap, swap.replace("  one(a);\n  two(a);\n", "  two(a);\n  one(a);\n"))["guarded"]
        is False
    )


def test_the_net_holds_a_line_that_touches_a_block_or_changes_its_place() -> None:
    from lemoncrow.pro.capabilities.review.batch_guard import _block_altered

    def rows(old: list[str], new: list[str]) -> list[tuple[list[tuple[str, bool]], list[tuple[str, bool]]]]:
        return [([(t, True) for t in old], [(t, True) for t in new])]

    # (a) a changed line that is a block, in either language
    assert _block_altered(rows([], ["    else:"]), "python")
    assert _block_altered(rows(["    run(a) \\"], []), "python")
    assert _block_altered(rows([], ['    s = """x"""']), "python")
    assert _block_altered(rows([], ["  const o = {"]), "script")
    assert _block_altered(rows([], ["  }"]), "script")
    assert _block_altered(rows([], ["  if (ok)"]), "script")
    assert _block_altered(rows([], ["  case 2:"]), "script")
    assert _block_altered(rows(["  if (a) b();"], ["  b();"]), "script")
    # (b) the same text at another indent
    assert _block_altered(rows(["    run(a)"], ["        run(a)"]), "python")
    # (c) removed in one hunk, added in another
    assert _block_altered([([("run(a)", True)], []), ([], [("run(a)", True)])], "python")
    # (d) moved and renamed on the way
    assert _block_altered(rows(["use(x)", "keep()"], ["keep()", "use(item)"]), "python")
    # nothing structural: a complete fragment added or removed whole, an operand changed in place
    assert not _block_altered(rows([], ["  const o = { a: 1 };"]), "script")
    assert not _block_altered(rows(["  if (a) b();"], []), "script")
    assert not _block_altered(rows(['import { a } from "x";'], ['import { a, b } from "x";']), "script")
    assert not _block_altered(rows([], ["  // closes } early"]), "script")
    assert not _block_altered(rows(["def g():", "    return 1"], []), "python")
    assert not _block_altered(rows(["use(x)"], ["use(y)"]), "python")


# --- Round 6: formatting, comments and templates cannot hide a move ---------------------------------------------

from tests.gateway.review_batch_guard_adversarial import FREE as _FREE_CHANGES  # noqa: E402
from tests.gateway.review_batch_guard_adversarial import HELD as _ADVERSARIAL_HELD  # noqa: E402
from tests.gateway.review_batch_guard_adversarial import OFFERED as _DOCUMENTED_LIMITS  # noqa: E402


def _guard_whole(
    tmp_path: Path, name: str, base: str, head: str, *, status: str = "modified", reader: Any = "both"
) -> dict[str, Any]:
    """The guard with the whole file on both sides, the way the server calls it."""

    hunks = _hunks(tmp_path, name, base, head)
    target = _target(hunks, name)
    entry = {"path": name, "status": status, "hunks": hunks}
    read = (lambda _path, side: base if side == "old" else head) if reader == "both" else reader
    return guard_for_target(target, entry, read, {}).payload()


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL_HELD))
def test_every_case_a_reviewer_found_is_held(tmp_path: Path, name: str) -> None:
    path, base, head = _ADVERSARIAL_HELD[name]
    assert _guard_whole(tmp_path, path, base, head)["guarded"] is True, name


@pytest.mark.parametrize("name", sorted(_DOCUMENTED_LIMITS))
def test_the_documented_limit_is_offered_and_stays_a_recorded_limit(tmp_path: Path, name: str) -> None:
    # SPEC 7.1 P4-2 L: a statement that crosses a block and also changes shape, or a new guard clause above one.
    path, base, head = _DOCUMENTED_LIMITS[name]
    assert _guard_whole(tmp_path, path, base, head)["guarded"] is False, name


@pytest.mark.parametrize("name", sorted(_FREE_CHANGES))
def test_a_free_change_stays_offered_whatever_the_line_endings_and_separators(tmp_path: Path, name: str) -> None:
    path, base, head = _FREE_CHANGES[name]
    assert _guard_whole(tmp_path, path, base, head)["guarded"] is False, name


_LOOP_OUT = (
    "def f(xs, x):\n    for a in xs:\n        keep(a)\n        send(x)\n    done()\n",
    "def f(xs, x):\n    for a in xs:\n        keep(a)\n    send(x)\n    done()\n",
)


_REORDER = (
    "def f(a, b):\n    one(a)\n    two(b)\n    return a\n",
    "def f(a, b):\n    two(b)\n    one(a)\n    return a\n",
)


def test_a_file_whose_text_is_not_available_fails_closed(tmp_path: Path) -> None:
    base, head = _REORDER
    assert _guard_whole(tmp_path, "app/a.py", base, head)["guarded"] is False  # what the whole file shows is fine
    for reader in (lambda _path, _side: None, lambda path, side: head if side == "new" else None):
        guard = _guard_whole(tmp_path, "app/a.py", base, head, reader=reader)
        assert (guard["guarded"], guard["code"]) == (True, "unverifiable")
        assert "was not available" in guard["reason"]

    def broken(_path: str, _side: str) -> str:
        raise OSError("gone")

    assert _guard_whole(tmp_path, "app/a.py", base, head, reader=broken)["code"] == "unverifiable"
    # An added file has no old side and a deleted one no new side: nothing to read there.
    added = "def f():\n    return 1\n"
    only_new = lambda _path, side: added if side == "new" else None  # noqa: E731
    assert _guard_whole(tmp_path, "app/n.py", "", added, status="added", reader=only_new)["code"] != "unverifiable"


def test_a_parse_error_near_the_part_fails_closed(tmp_path: Path) -> None:
    base, head = _REORDER
    # a stray bracket inside the function the part sits in
    inside = head.replace("    return a\n", "    ) = return a\n")
    guard = _guard_whole(tmp_path, "app/a.py", base, inside)
    assert (guard["guarded"], guard["code"]) == (True, "unverifiable")
    assert "does not parse" in guard["reason"]
    # an error in another function, outside every block around the part, does not stop the reading
    elsewhere_base, elsewhere_head = base + "\n\ndef g(x):\n    pass\n", head + "\n\ndef g(x):\n    pass\n"
    elsewhere_head = elsewhere_head.replace("def g(x):", "def g(:")
    ok = _guard_whole(tmp_path, "app/a.py", elsewhere_base, elsewhere_head)
    assert "does not parse" not in ok["reason"]


def test_the_size_bound_is_a_stated_limit_tested_at_and_past_it(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.batch_guard_tree import MAX_FILE_CHARS

    base, head = _REORDER

    def padded(text: str, size: int) -> str:
        return text + "# " + "p" * (size - len(text) - 3) + "\n"

    at_base, at_head = padded(base, MAX_FILE_CHARS), padded(head, MAX_FILE_CHARS)
    assert len(at_base) == MAX_FILE_CHARS and len(at_head) == MAX_FILE_CHARS
    at = _guard_whole(tmp_path, "app/big.py", at_base, at_head)
    assert at["guarded"] is False  # at the bound the file is read, and the reorder is free
    past_base, past_head = padded(base, MAX_FILE_CHARS + 1), padded(head, MAX_FILE_CHARS + 1)
    past = _guard_whole(tmp_path, "app/big.py", past_base, past_head)
    assert (past["guarded"], past["code"]) == (True, "unverifiable")
    assert f"longer than {MAX_FILE_CHARS:,} characters" in past["reason"]
    one_side = _guard_whole(tmp_path, "app/big.py", at_base, past_head)
    assert "longer than" in one_side["reason"]


def test_the_whole_file_reading_places_a_statement_in_its_blocks(tmp_path: Path) -> None:
    base, head = _LOOP_OUT
    guard = _guard_whole(tmp_path, "app/a.py", base, head)
    assert (guard["guarded"], guard["code"]) == (True, "loop_bound")
    swapped = "def f(a, b):\n    one(a)\n    two(b)\n    return a\n"
    assert (
        _guard_whole(tmp_path, "app/a.py", swapped, swapped.replace("one(a)\n    two(b)", "two(b)\n    one(a)"))[
            "guarded"
        ]
        is False
    )
    # a block added around a statement the part did not touch
    wrapped_base = "function f(ok, x) {\n  prep();\n  send(x);\n}\nfunction g(y) {\n  return y;\n}\n"
    wrapped = "function f(ok, x) {\n  prep();\n  if (ok) {\n  send(x);\n  }\n}\nfunction g(y) {\n  return y * 2;\n}\n"
    hunks = _hunks(tmp_path, "app/a.ts", wrapped_base, wrapped)
    owning = _guard_owning(tmp_path, "app/a.ts", wrapped_base, wrapped, new_lines={3, 5}, old_lines=set())
    assert owning["guarded"] is True
    assert hunks


def test_a_statement_is_matched_whole_not_row_by_row(tmp_path: Path) -> None:
    base = "function f(xs, a, b) {\n  for (const x of xs) {\n    keep(x);\n  }\n  send(a, b);\n  done();\n}\n"
    wrapped = base.replace("  }\n  send(a, b);\n", "    send(\n      a,\n      b,\n    );\n  }\n")
    assert _guard(tmp_path, "app/a.ts", base, wrapped)["guarded"] is True
    no_semicolon = base.replace("    keep(x);\n  }\n  send(a, b);\n", "    keep(x)\n    send(a, b)\n  }\n")
    assert _guard(tmp_path, "app/a.ts", base, no_semicolon)["guarded"] is True
    py = "def f(xs, a, b):\n    for x in xs:\n        keep(x)\n    send(a, b)\n    done()\n"
    exploded = py.replace("    send(a, b)\n", "        send(\n            a,\n            b,\n        )\n")
    assert _guard(tmp_path, "app/a.py", py, exploded)["guarded"] is True
    reformat_only = "function f(a, b) {\n  send(a, b);\n  done();\n}\n"
    spread = reformat_only.replace("  send(a, b);\n", "  send(\n    a,\n    b,\n  );\n")
    assert _guard(tmp_path, "app/a.ts", reformat_only, spread)["guarded"] is False


def test_braces_in_a_multi_row_comment_or_template_are_not_blocks(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.batch_guard import _script_lex

    lexed, ok = _script_lex(
        [("a(); /* x {", False), ("if (b) {", False), ("*/ c(`t ${ {d: 1}.d } {", False), ("`);", False)]
    )
    texts = [[t.text for t in row] for row in lexed]
    assert ok and texts[0] == ["a", "(", ")", ";"] and texts[1] == []
    assert texts[2].count("{") == 1 and texts[2].count("}") == 1  # the object in ${ }, not the text around it
    assert "if" not in [x for row in texts for x in row]
    _, unfinished = _script_lex([("const t = `open", False), ("more", False)])
    assert unfinished is False  # a template cut by the hunk cannot be resolved
    _, stray = _script_lex([("still the comment", True), ("*/ run();", True)])
    assert stray is False  # a comment opened above the hunk, with the part's own rows in it
    _, doc = _script_lex([(" * a doc comment row", False), (" */", False), ("run();", False)])
    assert doc is True  # context rows of a doc comment are comment


def test_a_move_past_a_template_the_hunk_cuts_is_held(tmp_path: Path) -> None:
    body = "function f(ok, x) {\n  if (ok) {\n    keep();\n    send(x);\n    const t = `a ${ok} {\n    b`;\n  }\n  done();\n}\n"
    head = body.replace("    send(x);\n", "").replace("  done();\n", "  send(x);\n  done();\n")
    assert _guard(tmp_path, "app/a.ts", body, head)["guarded"] is True


def test_a_function_body_and_a_labelled_block_are_blocks_not_values(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.batch_guard import _touches_block, _value_braces

    assert not _value_braces("function g(): Promise<void> { run(); }")
    assert not _value_braces("class A<T> {")
    assert not _value_braces("outer: { run(); }")
    assert _touches_block("function g(): Promise<void> { run() }", "script")
    assert _touches_block("outer: { run() }", "script")
    # objects, types, JSX and imports stay values
    assert _value_braces("const o: { a: number } = { a: 1 };")
    assert _value_braces("type T = { a: string };")
    assert _value_braces("const el = <p>{ok && <a>x</a>}</p>;")
    assert _value_braces('import { a, b } from "x";')
    assert _value_braces("const f = (a): { x: number } => ({ x: a });")
    base = "function f(x) {\n  prep();\n  function g(): Promise<void> { run(x); }\n  done();\n}\n"
    head = "function f(x) {\n  prep();\n  run(x);\n  done();\n}\n"
    assert _guard(tmp_path, "app/a.ts", base, head)["guarded"] is True
    labelled = "function f(x) {\n  prep();\n  outer: { run(x); }\n  done();\n}\n"
    assert _guard(tmp_path, "app/a.ts", labelled, head)["guarded"] is True


def test_a_python_case_arm_on_one_line_is_a_block(tmp_path: Path) -> None:
    base = "def f(k, x, y):\n    match k:\n        case 1: send(x)\n        case 2: other()\n    done()\n"
    head = "def f(k, x, y):\n    match k:\n        case 1: keep(y)\n        case 2: other()\n    send(x)\n    done()\n"
    assert _guard(tmp_path, "app/a.py", base, head)["guarded"] is True
    assert _guard(tmp_path, "app/a.py", head, base)["guarded"] is True
    plain = "def f(match, case):\n    match = case + 1\n    return match\n"
    assert (
        _guard(tmp_path, "app/b.py", plain, plain.replace("match = case + 1", "match = case + 1  # same"))["guarded"]
        is False
    )


def test_an_error_in_the_net_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lemoncrow.pro.capabilities.review.batch_guard as module

    def boom(*_: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "_block_altered", boom)
    base = "def f(a):\n    return a\n"
    guard = _guard(tmp_path, "app/a.py", base, base.replace("return a", "return b"))
    assert (guard["guarded"], guard["code"]) == (True, "unverifiable")


def test_batch_guards_reads_each_file_once_for_all_its_parts(tmp_path: Path) -> None:
    base, head = _REORDER
    hunks = _hunks(tmp_path, "app/a.py", base, head)
    first, second = _target(hunks, "app/a.py"), replace(_target(hunks, "app/a.py"), target_id="target:second")
    reads: list[tuple[str, str]] = []

    def read(path: str, side: str) -> str | None:
        reads.append((path, side))
        return base if side == "old" else head

    out = batch_guards({"files": [{"path": "app/a.py", "status": "modified", "hunks": hunks}]}, [first, second], read)
    assert [value["guarded"] for value in out.values()] == [False, False]
    assert sorted(reads) == [("app/a.py", "new"), ("app/a.py", "old")]
    # without a reader the whole file is not read, only the hunks
    plain = batch_guards({"files": [{"path": "app/a.py", "hunks": hunks}]}, [first])
    assert plain["target:app/a.py"]["guarded"] is False


def test_a_regex_literal_with_an_escaped_slash_ends_where_it_closes() -> None:
    from lemoncrow.pro.capabilities.review.batch_guard import _regex_end

    assert _regex_end("x = /a\\/b/g;", 4) == 11
    assert _regex_end("x = /a[/]b/;", 4) == 11
    assert _regex_end("x = /open", 4) is None


def test_a_statement_is_its_tokens_without_what_formatting_adds() -> None:
    from lemoncrow.pro.capabilities.review.batch_guard_tree import _parse_side

    def keys(text: str, grammar: str, family: str) -> list[tuple[str, ...]]:
        return [unit.key for unit in _parse_side(text, grammar, family).units]

    one = keys("function f() {\n  send(a, b);\n}\n", "typescript", "script")
    wrapped = keys("function f() {\n  send(\n    a,\n    b,\n  )\n}\n", "typescript", "script")
    assert one[1] == wrapped[1] == ("send", "(", "a", ",", "b", ")")
    py_one = keys("def f():\n    send(a, b)\n", "python", "python")
    py_wrapped = keys("def f():\n    send(\n        a,\n        b,\n    )\n", "python", "python")
    assert py_one[1] == py_wrapped[1]
    # a bare expression is a statement of its own in Python, and a one-line body is not part of its header
    inline = keys("def f(ok):\n    if ok: send(a)\n", "python", "python")
    assert inline[-1] == ("send", "(", "a", ")") and inline[-2] == ("if", "ok", ":")
    # comments and strings do not make blocks
    quiet = keys("function f() {\n  /* if (x) { */\n  const s = '}';\n}\n", "typescript", "script")
    assert [key for key in quiet if "if" in key] == []


def test_dynamic_import_a_parameter_named_type_and_a_leading_star_are_read_by_the_grammar(tmp_path: Path) -> None:
    dynamic = "function f(x) {\n  import('./m').then((m) => {\n    m.init();\n    send(x);\n  });\n  done();\n}\n"
    out = dynamic.replace("    send(x);\n  });\n", "  });\n  send(x);\n")
    assert _guard_whole(tmp_path, "app/a.ts", dynamic, out)["guarded"] is True
    typed = (
        "function f(items, x, y) {\n  items.forEach((type) => {\n    keep(type);\n    send(x);\n  });\n  done();\n}\n"
    )
    moved = typed.replace("    send(x);\n  });\n", "  });\n  send(y);\n")
    assert _guard_whole(tmp_path, "app/a.ts", typed, moved)["guarded"] is True
    star = "function f(ok, x, w, h) {\n  const a = w\n    * h;\n  if (ok) {\n    keep();\n    send(x);\n  }\n  done();\n}\n"
    starred = star.replace("    send(x);\n  }\n", "  }\n  send(x);\n")
    assert _guard_whole(tmp_path, "app/a.ts", star, starred)["guarded"] is True


def test_a_text_that_is_not_the_diffs_side_never_gets_read_as_that_side(tmp_path: Path) -> None:
    base, head = _REORDER
    assert _guard_whole(tmp_path, "app/a.py", base, head)["guarded"] is False
    for reader in (
        lambda _path, side: head if side == "old" else base,  # the sides swapped
        lambda _path, side: base,  # the same text twice
        lambda _path, side: (base if side == "old" else head).replace("one(a)", "uno(a)"),
        lambda _path, side: "",
    ):
        guard = _guard_whole(tmp_path, "app/a.py", base, head, reader=reader)
        assert (guard["guarded"], guard["code"]) == (True, "unverifiable")
        assert "does not match the diff" in guard["reason"]


def _two_files(tmp_path: Path) -> tuple[dict[str, Any], list[Any], Any]:
    base, head = _REORDER
    files, targets = [], []
    for name in ("app/a.py", "app/b.py"):
        hunks = _hunks(tmp_path, name, base, head)
        files.append({"path": name, "status": "modified", "hunks": hunks})
        targets.append(_target(hunks, name))
    return {"files": files}, targets, lambda _path, side: base if side == "old" else head


def test_a_snapshot_has_a_budget_for_reading_files_whole_tested_at_and_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import batch_guard_tree as tree

    packet, targets, read = _two_files(tmp_path)
    base, head = _REORDER
    both = 2 * (len(base) + len(head))
    monkeypatch.setattr(tree, "MAX_SNAPSHOT_CHARS", both)
    at = batch_guards(packet, targets, read)
    assert [v["guarded"] for v in at.values()] == [False, False]
    monkeypatch.setattr(tree, "MAX_SNAPSHOT_CHARS", both - 1)
    past = batch_guards(packet, targets, read)
    first, second = past.values()
    assert first["guarded"] is False  # the file that fit is read
    assert (second["guarded"], second["code"]) == (True, "unverifiable")
    assert "too large to read whole together" in second["reason"]
    monkeypatch.setattr(tree, "MAX_SNAPSHOT_CHARS", both)
    monkeypatch.setattr(tree, "MAX_SNAPSHOT_SECONDS", -1.0)
    slow = batch_guards(packet, targets, read)
    assert all(v["code"] == "unverifiable" and "took too long" in v["reason"] for v in slow.values())


def test_old_sides_are_read_in_one_batch_and_match_the_one_at_a_time_reader(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from lemoncrow.pro.capabilities.review.repo_context import old_source_reader, old_sources

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    names = [f"pkg/m_{n}.py" for n in range(5)]
    for index, name in enumerate(names):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(f"def f_{index}():\n    return {index}\n", encoding="utf-8")
    (tmp_path / "pkg" / "blob.py").write_bytes(b"x = 1\0\n")
    git("add", "-A")
    git("-c", "user.email=<redacted-email>", "-c", "user.name=t", "commit", "-qm", "base")
    revision = SimpleNamespace(base_sha=git("rev-parse", "HEAD").strip(), review_id="r", id="v")
    store = SimpleNamespace(read_source_tree_artifact=lambda *_a, **_k: None)
    asked = [*names, "pkg/missing.py", "pkg/blob.py", ".env"]
    batch = old_sources(store, revision, tmp_path, asked)
    one = old_source_reader(store, revision, tmp_path)
    assert {name: batch.get(name) for name in names} == {name: one(name) for name in names}
    assert "pkg/missing.py" not in batch and "pkg/blob.py" not in batch and ".env" not in batch
    # a path that names a tree does not stop the files after it, and the total read is bounded
    with_tree = old_sources(store, revision, tmp_path, ["pkg", names[0], "pkg/missing.py", names[1], "nope", names[2]])
    assert with_tree == {name: one(name) for name in names[:3]}
    from lemoncrow.pro.capabilities.review import repo_context

    original = repo_context._WHOLE_TOTAL_BYTES
    repo_context._WHOLE_TOTAL_BYTES = len(one(names[0]) or "") + len(one(names[1]) or "")
    try:
        capped = old_sources(store, revision, tmp_path, names)
    finally:
        repo_context._WHOLE_TOTAL_BYTES = original
    assert list(capped) == names[:2]
    assert old_sources(store, SimpleNamespace(base_sha="", review_id="r", id="v"), tmp_path, names) == {}


def test_a_raw_string_a_tagged_template_and_a_plain_string_keep_what_their_quotes_do_not_decide() -> None:
    from lemoncrow.pro.capabilities.review.batch_guard_tree import _parse_side

    def key(text: str, grammar: str, family: str) -> tuple[str, ...]:
        return _parse_side(text, grammar, family).units[-1].key

    assert key("x = r'a\\'b'\n", "python", "python") != key('x = r"a\'b"\n', "python", "python")
    assert key("x = r'\\''\n", "python", "python") != key("x = r''\n", "python", "python")
    assert key("x = 'it\\'s'\n", "python", "python") == key('x = "it\'s"\n', "python", "python")
    assert key("x = r'a'\n", "python", "python") == key('x = r"a"\n', "python", "python")
    assert key("send('a');\n", "typescript", "script") == key('send("a");\n', "typescript", "script")
    assert key("send(`a`);\n", "typescript", "script") == key("send('a');\n", "typescript", "script")
    assert key("send(tag`a`);\n", "typescript", "script") != key("send('a');\n", "typescript", "script")
    assert key("send(String.raw`a\\n`);\n", "typescript", "script") != key("send(`a\\n`);\n", "typescript", "script")


def test_a_decorated_parameter_is_known_by_its_name_not_its_place(tmp_path: Path) -> None:
    moved = ("class S {\n  m(a: A, @Inject(T) x: X, y: Y) {}\n}\n", "class S {\n  m(x: X, @Inject(T) y: Y) {}\n}\n")
    assert _guard_whole(tmp_path, "app/a.ts", *moved)["guarded"] is True
    # the list shifts, the decorator stays on x
    shifted = ("class S {\n  m(a: A, @Inject(T) x: X) {}\n}\n", "class S {\n  m(@Inject(T) x: X) {}\n}\n")
    assert _guard_whole(tmp_path, "app/a.ts", *shifted)["code"] != "block_moved"
