"""R20: the reader's progress denominator is changed spans, not raw units."""

from __future__ import annotations

from dataclasses import replace

from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    AnnotationAnchor,
    FrontierEntry,
    ReviewEvidence,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.targets import (
    TargetSpan,
    build_review_outline,
    derive_review_targets,
    reason_is_promoting,
    review_progress,
    revision_target_delta,
    target_has_unresolved_request_change,
)


def _unit(
    kind: str,
    key: str,
    *,
    path: str = "src/mod.py",
    symbol: str = "",
    ordinal: int = 0,
    start: int = 0,
    end: int = 0,
    fingerprint_method: str = "blob_sha256",
    group: str = "production",
) -> ReviewUnit:
    return ReviewUnit(
        revision_id="rev-1",
        unit_key=key,
        kind=kind,  # type: ignore[arg-type]
        path=path,
        content_fingerprint=f"fp:{key}",
        fingerprint_method=fingerprint_method,  # type: ignore[arg-type]
        symbol=symbol,
        ordinal=ordinal,
        start_line=start,
        end_line=end,
        attention_rank=1,
        attention_group=group,  # type: ignore[arg-type]
        reasons=("public contract changed",),
    )


def _entry(unit: ReviewUnit, *, state: str = "unreviewed", changed: bool = False) -> FrontierEntry:
    return FrontierEntry(
        unit_key=unit.unit_key,
        kind=unit.kind,
        path=unit.path,
        symbol=unit.symbol,
        state=state,  # type: ignore[arg-type]
        attention_rank=unit.attention_rank,
        reasons=unit.reasons,
        changed_since_mark=changed,
        reviewed_revision_id="rev-0" if state != "unreviewed" else "",
        ordinal=unit.ordinal,
        start_line=unit.start_line,
    )


def _packet(
    *hunks: dict[str, object],
    path: str = "src/mod.py",
    symbol_rows: tuple[dict[str, object], ...] = (),
    **file_fields: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "path": path,
        "status": "modified",
        "is_binary": False,
        "additions": 0,
        "deletions": 0,
        "hunks": list(hunks),
    }
    row.update(file_fields)
    return {"files": [row], "symbols": list(symbol_rows)}


def _hunk(
    old_start: int,
    new_start: int,
    patch: str,
    *,
    old_lines: int = 1,
    new_lines: int = 1,
    old_ranges: tuple[tuple[int, int], ...] = (),
    new_ranges: tuple[tuple[int, int], ...] = (),
) -> dict[str, object]:
    return {
        "old_start": old_start,
        "old_lines": old_lines,
        "new_start": new_start,
        "new_lines": new_lines,
        "old_ranges": old_ranges,
        "new_ranges": new_ranges,
        "patch": patch,
    }


def _covered(targets: object) -> set[tuple[str, int, int]]:
    out: set[tuple[str, int, int]] = set()
    for target in targets:  # type: ignore[union-attr]
        for span in target.spans:
            for line in range(span.start_line, span.end_line + 1):
                key = (span.side, span.hunk_ordinal, line)
                assert key not in out, f"changed line counted twice: {key}"
                out.add(key)
    return out


def test_deepest_trustworthy_symbol_owns_nested_changed_text_once() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=4, fingerprint_method="hunk_patch_sha256")
    outer_class = _unit("symbol", "sym-class", symbol="Reader", start=1, end=4, fingerprint_method="symbol_body_sha256")
    outer_method = _unit(
        "symbol", "sym-run", symbol="Reader.run", start=2, end=4, fingerprint_method="symbol_body_sha256"
    )
    inner = _unit(
        "symbol",
        "sym-normalize",
        symbol="Reader.run.normalize",
        start=3,
        end=4,
        fingerprint_method="symbol_body_sha256",
    )
    units = (file_unit, hunk_unit, outer_class, outer_method, inner)
    patch = (
        " class Reader:\n     def run(self):\n         def normalize():\n-            return 1\n+            return 2\n"
    )

    targets = derive_review_targets(
        units, tuple(_entry(unit) for unit in units), _packet(_hunk(1, 1, patch, old_lines=4, new_lines=4))
    )

    assert [(target.kind, target.symbol) for target in targets] == [("symbol", "Reader.run.normalize")]
    assert _covered(targets) == {("old", 0, 4), ("new", 0, 4)}


def test_one_symbol_target_can_cover_multiple_hunks() -> None:
    file_unit = _unit("file", "fil")
    hunk0 = _unit("hunk", "hun-0", ordinal=0, start=2, end=2, fingerprint_method="hunk_patch_sha256")
    hunk1 = _unit("hunk", "hun-1", ordinal=1, start=8, end=8, fingerprint_method="hunk_patch_sha256")
    symbol = _unit("symbol", "sym", symbol="run", start=1, end=10, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk0, hunk1, symbol)
    packet = _packet(
        _hunk(2, 2, "-    a = 1\n+    a = 2\n"),
        _hunk(8, 8, "-    b = 1\n+    b = 2\n"),
    )

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)

    assert len(targets) == 1
    assert targets[0].unit_key == "sym"
    assert targets[0].hunk_ordinals == (0, 1)
    assert targets[0].additions == 2
    assert targets[0].deletions == 2


def test_unowned_changed_text_falls_back_to_its_hunk() -> None:
    file_unit = _unit("file", "fil", path="README.md")
    hunk_unit = _unit(
        "hunk",
        "hun",
        path="README.md",
        ordinal=0,
        start=1,
        end=2,
        fingerprint_method="hunk_patch_sha256",
    )
    units = (file_unit, hunk_unit)
    packet = _packet(_hunk(1, 1, "-old\n+new\n"), path="README.md")
    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)
    assert [(target.kind, target.unit_key) for target in targets] == [("hunk", "hun")]
    assert targets[0].label == "README.md#0"
    assert _covered(targets) == {("old", 0, 1), ("new", 0, 1)}
    assert _covered(targets) == {("old", 0, 1), ("new", 0, 1)}


def test_symbol_and_hunk_targets_partition_a_mixed_hunk_without_double_counting() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=3, fingerprint_method="hunk_patch_sha256")
    symbol = _unit("symbol", "sym", symbol="f", start=2, end=3, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, symbol)
    patch = "+TOP = 1\n def f():\n-    return 1\n+    return 2\n"

    targets = derive_review_targets(
        units, tuple(_entry(unit) for unit in units), _packet(_hunk(1, 1, patch, old_lines=2, new_lines=3))
    )

    assert {(target.kind, target.unit_key) for target in targets} == {("hunk", "hun"), ("symbol", "sym")}
    assert _covered(targets) == {("new", 0, 1), ("old", 0, 2), ("new", 0, 3)}
    hunk_target = next(target for target in targets if target.kind == "hunk")
    symbol_target = next(target for target in targets if target.kind == "symbol")
    assert hunk_target.spans == (TargetSpan("new", 1, 1, 0),)
    assert {(span.side, span.start_line, span.end_line) for span in symbol_target.spans} == {
        ("old", 2, 2),
        ("new", 3, 3),
    }


def test_micro_local_bindings_fold_into_the_enclosing_function_target() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=5, fingerprint_method="hunk_patch_sha256")
    parent = _unit("symbol", "parent", symbol="Reader", start=1, end=5, fingerprint_method="symbol_body_sha256")
    state = _unit("symbol", "state", symbol="reviewId", start=2, end=2, fingerprint_method="symbol_body_sha256")
    temp = _unit("symbol", "temp", symbol="rows", start=3, end=3, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, parent, state, temp)
    patch = "+function Reader() {\n+  const reviewId = useState('');\n+  const rows = data.rows;\n+  return rows.map(render);\n+}\n"
    packet = _packet(
        _hunk(1, 1, patch, old_lines=0, new_lines=5),
        symbol_rows=(
            {
                "file_path": "src/mod.py",
                "symbol_name": "Reader",
                "qualified_name": "Reader",
                "start_line": 1,
                "kind": "function",
                "caller_count": 0,
            },
            {
                "file_path": "src/mod.py",
                "symbol_name": "reviewId",
                "qualified_name": "reviewId",
                "start_line": 2,
                "kind": "variable",
                "caller_count": 0,
            },
            {
                "file_path": "src/mod.py",
                "symbol_name": "rows",
                "qualified_name": "rows",
                "start_line": 3,
                "kind": "variable",
                "caller_count": 0,
            },
        ),
    )

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)

    assert [(target.kind, target.symbol) for target in targets] == [("symbol", "Reader")]
    assert _covered(targets) == {("new", 0, line) for line in range(1, 6)}


def test_metadata_less_leaf_symbols_fold_into_their_semantic_parent() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=8, fingerprint_method="hunk_patch_sha256")
    parent = _unit(
        "symbol",
        "parent",
        symbol="deriveReviewPrimaryAction",
        start=1,
        end=8,
        fingerprint_method="symbol_body_sha256",
    )
    parameter = _unit(
        "symbol",
        "parameter",
        symbol="deriveReviewPrimaryAction.historical",
        start=2,
        end=2,
        fingerprint_method="symbol_body_sha256",
    )
    local = _unit(
        "symbol",
        "local",
        symbol="deriveReviewPrimaryAction.destination",
        start=4,
        end=4,
        fingerprint_method="symbol_body_sha256",
    )
    helper = _unit(
        "symbol",
        "helper",
        symbol="deriveReviewPrimaryAction.helper",
        start=5,
        end=7,
        fingerprint_method="symbol_body_sha256",
    )
    units = (file_unit, hunk_unit, parent, parameter, local, helper)
    patch = (
        "+function deriveReviewPrimaryAction({\n"
        "+  historical,\n"
        "+}: Options) {\n"
        "+  const destination = label();\n"
        "+  function helper() {\n"
        "+    return destination;\n"
        "+  }\n"
        "+  return helper();\n"
    )
    packet = _packet(
        _hunk(1, 1, patch, old_lines=0, new_lines=8),
        symbol_rows=(
            {
                "file_path": "src/mod.py",
                "symbol_name": "deriveReviewPrimaryAction",
                "qualified_name": "deriveReviewPrimaryAction",
                "start_line": 1,
                "kind": "function_declaration",
                "caller_count": 0,
            },
        ),
    )

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)

    assert {(target.kind, target.symbol) for target in targets} == {
        ("symbol", "deriveReviewPrimaryAction"),
        ("symbol", "deriveReviewPrimaryAction.helper"),
    }
    parent_target = next(target for target in targets if target.symbol == "deriveReviewPrimaryAction")
    helper_target = next(target for target in targets if target.symbol.endswith(".helper"))
    assert {
        line for span in parent_target.spans if span.side == "new" for line in range(span.start_line, span.end_line + 1)
    } == {
        1,
        2,
        3,
        4,
        8,
    }
    assert {
        line for span in helper_target.spans if span.side == "new" for line in range(span.start_line, span.end_line + 1)
    } == {
        5,
        6,
        7,
    }


def test_metadata_less_interface_fields_fold_into_the_interface_target() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=4, fingerprint_method="hunk_patch_sha256")
    parent = _unit(
        "symbol",
        "parent",
        symbol="ReviewPrimaryAction",
        start=1,
        end=4,
        fingerprint_method="symbol_body_sha256",
    )
    kind = _unit(
        "symbol",
        "kind",
        symbol="ReviewPrimaryAction.kind",
        start=2,
        end=2,
        fingerprint_method="symbol_body_sha256",
    )
    label = _unit(
        "symbol",
        "label",
        symbol="ReviewPrimaryAction.label",
        start=3,
        end=3,
        fingerprint_method="symbol_body_sha256",
    )
    units = (file_unit, hunk_unit, parent, kind, label)
    patch = "+interface ReviewPrimaryAction {\n+  kind: string;\n+  label: string;\n+}\n"
    packet = _packet(
        _hunk(1, 1, patch, old_lines=0, new_lines=4),
        symbol_rows=(
            {
                "file_path": "src/mod.py",
                "symbol_name": "ReviewPrimaryAction",
                "qualified_name": "ReviewPrimaryAction",
                "start_line": 1,
                "kind": "interface_declaration",
                "caller_count": 0,
            },
        ),
    )

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)

    assert [(target.kind, target.symbol) for target in targets] == [("symbol", "ReviewPrimaryAction")]
    assert _covered(targets) == {("new", 0, line) for line in range(1, 5)}


def test_folded_micro_symbol_comment_counts_against_its_human_target() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=5, fingerprint_method="hunk_patch_sha256")
    parent = _unit("symbol", "parent", symbol="Reader", start=1, end=5, fingerprint_method="symbol_body_sha256")
    micro = _unit("symbol", "micro", symbol="reviewId", start=2, end=2, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, parent, micro)
    packet = _packet(
        _hunk(
            1,
            1,
            "+function Reader() {\n+  const reviewId = useState('');\n+  return reviewId;\n+}\n+\n",
            old_lines=0,
            new_lines=5,
        ),
        symbol_rows=(
            {
                "file_path": "src/mod.py",
                "symbol_name": "Reader",
                "qualified_name": "Reader",
                "start_line": 1,
                "kind": "function",
                "caller_count": 0,
            },
            {
                "file_path": "src/mod.py",
                "symbol_name": "reviewId",
                "qualified_name": "reviewId",
                "start_line": 2,
                "kind": "variable",
                "caller_count": 0,
            },
        ),
    )
    comment = Annotation(
        id="ann-1",
        review_id="review-1",
        revision_id="rev-1",
        anchor=AnnotationAnchor(path="src/mod.py", side="new", start_line=2, end_line=2, unit_key="micro"),
        body="Keep this state explicit.",
        kind="request_change",
        author_response="addressed",
    )

    targets = derive_review_targets(
        units,
        tuple(_entry(unit) for unit in units),
        packet,
        annotations=(comment,),
    )

    assert [(target.kind, target.symbol) for target in targets] == [("symbol", "Reader")]
    assert targets[0].annotation_counts.open == 1
    assert targets[0].annotation_counts.addressed_needs_rereview == 1
    assert target_has_unresolved_request_change(targets[0], (comment,)) is True


def test_cohesive_local_callback_is_a_target_but_its_temporary_binding_is_not() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=12, fingerprint_method="hunk_patch_sha256")
    parent = _unit("symbol", "parent", symbol="Reader", start=1, end=12, fingerprint_method="symbol_body_sha256")
    callback = _unit("symbol", "callback", symbol="loadPath", start=2, end=10, fingerprint_method="symbol_body_sha256")
    request = _unit("symbol", "request", symbol="request", start=4, end=9, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, parent, callback, request)
    patch = "+function Reader() {\n+  const loadPath = useCallback(async () => {\n+    if (!path) return;\n+    const request = fetch(path)\n+      .then(parse)\n+      .then(store)\n+      .catch(report)\n+      .finally(done);\n+    return request;\n+  }, []);\n+  return null;\n+}\n"
    packet = _packet(
        _hunk(1, 1, patch, old_lines=0, new_lines=12),
        symbol_rows=(
            {
                "file_path": "src/mod.py",
                "symbol_name": "Reader",
                "qualified_name": "Reader",
                "start_line": 1,
                "kind": "function",
                "caller_count": 0,
            },
            {
                "file_path": "src/mod.py",
                "symbol_name": "loadPath",
                "qualified_name": "loadPath",
                "start_line": 2,
                "kind": "variable",
                "caller_count": 0,
            },
            {
                "file_path": "src/mod.py",
                "symbol_name": "request",
                "qualified_name": "request",
                "start_line": 4,
                "kind": "variable",
                "caller_count": 0,
            },
        ),
    )

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)

    assert {(target.kind, target.symbol) for target in targets} == {("symbol", "Reader"), ("symbol", "loadPath")}
    load_path = next(target for target in targets if target.symbol == "loadPath")
    assert {(span.start_line, span.end_line) for span in load_path.spans if span.side == "new"} == {(2, 10)}
    assert ("symbol", "request") not in {(target.kind, target.symbol) for target in targets}
    assert _covered(targets) == {("new", 0, line) for line in range(1, 13)}


def test_short_variable_with_known_external_usage_stays_a_target() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=2, fingerprint_method="hunk_patch_sha256")
    container = _unit("symbol", "container", symbol="Config", start=1, end=2, fingerprint_method="symbol_body_sha256")
    exported = _unit("symbol", "exported", symbol="TIMEOUT", start=2, end=2, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, container, exported)
    packet = _packet(
        _hunk(1, 1, "+class Config {}\n+const TIMEOUT = 5;\n", old_lines=0, new_lines=2),
        symbol_rows=(
            {
                "file_path": "src/mod.py",
                "symbol_name": "Config",
                "qualified_name": "Config",
                "start_line": 1,
                "kind": "class",
                "caller_count": 0,
            },
            {
                "file_path": "src/mod.py",
                "symbol_name": "TIMEOUT",
                "qualified_name": "TIMEOUT",
                "start_line": 2,
                "kind": "variable",
                "caller_count": 4,
            },
        ),
    )

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)

    assert {target.symbol for target in targets if target.kind == "symbol"} == {"Config", "TIMEOUT"}


def test_ambiguous_or_unknown_symbol_identity_falls_back_to_hunk() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=1, fingerprint_method="hunk_patch_sha256")
    ambiguous_a = _unit("symbol", "sym-a", symbol="run", start=1, end=1, fingerprint_method="symbol_body_sha256")
    ambiguous_b = _unit("symbol", "sym-b", symbol="run", start=1, end=1, fingerprint_method="symbol_body_sha256")
    unknown = _unit("symbol", "sym-u", symbol="other", start=1, end=1, fingerprint_method="unknown")
    units = (file_unit, hunk_unit, ambiguous_a, ambiguous_b, unknown)

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), _packet(_hunk(1, 1, "-a\n+b\n")))

    assert [(target.kind, target.unit_key) for target in targets] == [("hunk", "hun")]


def test_binary_or_nontextual_change_is_one_file_target() -> None:
    file_unit = _unit("file", "fil", path="logo.png", fingerprint_method="unknown")
    packet = _packet(path="logo.png", is_binary=True, additions=0, deletions=0)

    targets = derive_review_targets((file_unit,), (_entry(file_unit, state="unknown"),), packet)

    assert len(targets) == 1
    assert targets[0].kind == "file"
    assert targets[0].path == "logo.png"
    assert targets[0].state == "unknown"


def test_missing_hunk_body_stays_a_hunk_target_instead_of_claiming_symbol_precision() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=3, end=4, fingerprint_method="unknown")
    symbol = _unit("symbol", "sym", symbol="f", start=1, end=8, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, symbol)
    packet = _packet(
        _hunk(
            3,
            3,
            "",
            old_lines=2,
            new_lines=2,
            old_ranges=((3, 3),),
            new_ranges=((3, 3),),
        )
    )

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)

    assert [(target.kind, target.unit_key) for target in targets] == [("hunk", "hun")]


def test_missing_patch_and_exact_ranges_falls_back_to_file_not_context_header() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=10, end=16, fingerprint_method="unknown")
    symbol = _unit("symbol", "sym", symbol="f", start=12, end=14, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, symbol)
    packet = _packet(
        _hunk(10, 10, "", old_lines=7, new_lines=7),
        additions=1,
        deletions=1,
    )

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)

    assert [(target.kind, target.unit_key) for target in targets] == [("file", "fil")]
    assert targets[0].spans == ()
    assert (targets[0].additions, targets[0].deletions) == (1, 1)


def test_one_untrustworthy_hunk_promotes_whole_file_instead_of_partial_progress() -> None:
    file_unit = _unit("file", "fil")
    hunk0 = _unit("hunk", "hun-0", ordinal=0, start=2, end=2, fingerprint_method="hunk_patch_sha256")
    hunk1 = _unit("hunk", "hun-1", ordinal=1, start=20, end=26, fingerprint_method="unknown")
    symbol = _unit("symbol", "sym", symbol="run", start=1, end=5, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk0, hunk1, symbol)
    packet = _packet(
        _hunk(2, 2, "-    a = 1\n+    a = 2\n"),
        _hunk(20, 20, "", old_lines=7, new_lines=7),
        additions=2,
        deletions=2,
    )

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)

    assert [(target.kind, target.unit_key) for target in targets] == [("file", "fil")]
    assert targets[0].spans == ()
    assert (targets[0].additions, targets[0].deletions) == (2, 2)


def test_pure_deletion_does_not_borrow_the_following_symbol_at_its_join_point() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=2, fingerprint_method="hunk_patch_sha256")
    following = _unit("symbol", "sym", symbol="f", start=1, end=2, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, following)
    patch = "-OLD = 1\n def f():\n     return 1\n"

    targets = derive_review_targets(
        units,
        tuple(_entry(unit) for unit in units),
        _packet(_hunk(1, 1, patch, old_lines=3, new_lines=2)),
    )

    assert [(target.kind, target.unit_key) for target in targets] == [("hunk", "hun")]
    assert targets[0].spans == (TargetSpan("old", 1, 1, 0),)


def test_a_rewritten_symbol_keeps_only_the_deletions_its_fingerprint_covers() -> None:
    """R21: a reviewed symbol must not absorb a neighbour deleted beside it.

    ``C.a``'s body is rewritten and ``C.b`` is deleted in the same block, so
    every added line has one owner and the old side ends up under it. But a
    symbol's carry-forward fingerprint hashes its *new-side* body only: deleting
    another method later on would grow that owned old span without changing the
    fingerprint, and the reviewed mark would carry over a deletion nobody saw.

    What separates this from an ordinary in-place rewrite is the deleted text
    itself: it opens a definition at ``C.a``'s own nesting level, which is not
    ``C.a``'s body. That definition and its blank-line prelude belong to the
    hunk, whose patch fingerprint does cover them.
    """

    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=3, fingerprint_method="hunk_patch_sha256")
    klass = _unit("symbol", "sym-class", symbol="C", start=1, end=3, fingerprint_method="symbol_body_sha256")
    method = _unit("symbol", "sym-a", symbol="C.a", start=2, end=3, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, klass, method)
    patch = (
        " class C:\n"
        "     def a(self):\n"
        "-        return 1\n"
        "-\n"
        "-    def b(self):\n"
        "-        return 2\n"
        "+        return 99\n"
    )

    targets = derive_review_targets(
        units,
        tuple(_entry(unit) for unit in units),
        _packet(_hunk(1, 1, patch, old_lines=6, new_lines=3)),
    )

    by_kind = {target.kind: target for target in targets}
    assert set(by_kind) == {"symbol", "hunk"}
    assert by_kind["symbol"].unit_key == "sym-a"
    assert {(span.side, span.start_line, span.end_line) for span in by_kind["symbol"].spans} == {
        ("new", 3, 3),
        ("old", 3, 3),
    }
    assert by_kind["symbol"].deletions == 1
    assert {(span.side, span.start_line, span.end_line) for span in by_kind["hunk"].spans} == {("old", 4, 6)}
    assert _covered(targets) == {("new", 0, 3), ("old", 0, 3), ("old", 0, 4), ("old", 0, 5), ("old", 0, 6)}


def test_renamed_callback_declaration_stays_with_modified_symbol() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=3, fingerprint_method="hunk_patch_sha256")
    callback = _unit(
        "symbol",
        "sym-callback",
        symbol="sendFeedbackToAgent",
        start=1,
        end=3,
        fingerprint_method="symbol_body_sha256",
    )
    units = (file_unit, hunk_unit, callback)
    patch = (
        "-const sendFeedbackToAuthor = useCallback(async () => {\n"
        "+const sendFeedbackToAgent = useCallback(async () => {\n"
        "   send();\n"
        " }, []);\n"
    )

    targets = derive_review_targets(
        units,
        tuple(_entry(unit) for unit in units),
        _packet(_hunk(1, 1, patch, old_lines=3, new_lines=3)),
    )

    assert [(target.kind, target.unit_key) for target in targets] == [("symbol", "sym-callback")]
    assert {(span.side, span.start_line, span.end_line) for span in targets[0].spans} == {
        ("old", 1, 1),
        ("new", 1, 1),
    }
    assert (targets[0].additions, targets[0].deletions) == (1, 1)


def test_a_body_rewritten_in_place_stays_one_review_target() -> None:
    """The everyday shrink: five body lines collapse to one, and nothing else.

    Nothing in the deleted text opens a definition, so every deleted line is the
    owner's own body and it keeps them. Splitting here would cost a second
    review target and a larger progress denominator for every rewrite, and would
    report the symbol's churn as ``+1 -1`` when the function lost five lines.
    """

    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=2, fingerprint_method="hunk_patch_sha256")
    fn = _unit("symbol", "sym-f", symbol="f", start=1, end=2, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, fn)
    patch = " def f():\n-    a = 1\n-    b = 2\n-    c = 3\n-    d = 4\n-    e = 5\n+    return 0\n"

    targets = derive_review_targets(
        units,
        tuple(_entry(unit) for unit in units),
        _packet(_hunk(1, 1, patch, old_lines=6, new_lines=2)),
    )

    assert [(target.kind, target.unit_key) for target in targets] == [("symbol", "sym-f")]
    assert {(span.side, span.start_line, span.end_line) for span in targets[0].spans} == {
        ("new", 2, 2),
        ("old", 2, 6),
    }
    assert (targets[0].additions, targets[0].deletions) == (1, 5)


def test_a_helper_deleted_inside_its_owners_body_stays_on_the_owner() -> None:
    """A nested definition is body, and the owner's fingerprint does cover it.

    ``inner`` is deleted, but it lived inside ``outer``, whose
    ``symbol_body_sha256`` hashes the whole body it lived in -- so nothing can
    grow under a carried mark here. Indentation is what says so: a definition
    deeper than the owner's own header is the owner's, one at or above it is a
    neighbour's.
    """

    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=2, fingerprint_method="hunk_patch_sha256")
    outer = _unit("symbol", "sym-outer", symbol="outer", start=1, end=2, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, outer)
    patch = " def outer():\n-    def inner():\n-        return 1\n-    return inner()\n+    return 0\n"

    targets = derive_review_targets(
        units,
        tuple(_entry(unit) for unit in units),
        _packet(_hunk(1, 1, patch, old_lines=4, new_lines=2)),
    )

    assert [(target.kind, target.unit_key) for target in targets] == [("symbol", "sym-outer")]
    assert {(span.side, span.start_line, span.end_line) for span in targets[0].spans} == {
        ("new", 2, 2),
        ("old", 2, 4),
    }


def test_a_form_feed_inside_a_changed_line_does_not_invent_a_changed_row() -> None:
    """A diff row ends at a newline; ``str.splitlines`` disagrees.

    ``\\x0c`` inside a string literal split one changed line into two rows, and
    the tail began with ``'+'``, so the parser read it as another added line:
    every later line number in the hunk shifted by one and the untouched
    ``gamma`` below became a changed symbol nobody wrote.
    """

    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=3, end=5, fingerprint_method="hunk_patch_sha256")
    beta = _unit("symbol", "sym-beta", symbol="beta", start=2, end=3, fingerprint_method="symbol_body_sha256")
    gamma = _unit("symbol", "sym-gamma", symbol="gamma", start=4, end=5, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, beta, gamma)
    patch = '-    text = "old"\n+    text = "\x0c+plus"\n def gamma():\n     return 2\n'

    targets = derive_review_targets(
        units,
        tuple(_entry(unit) for unit in units),
        _packet(_hunk(3, 3, patch, old_lines=4, new_lines=4)),
    )

    assert [(target.kind, target.unit_key) for target in targets] == [("symbol", "sym-beta")]
    assert {(span.side, span.start_line, span.end_line) for span in targets[0].spans} == {
        ("new", 3, 3),
        ("old", 3, 3),
    }


def test_one_thread_is_counted_on_exactly_one_target() -> None:
    """A comment anchored on one target is not also claimed by a sibling's span.

    ``owning_symbol_unit`` anchors a multi-line selection on the tightest symbol
    that contains all of it -- the class, when the selection crosses two of its
    methods -- while the anchor's first line may sit in a method that is a target
    of its own. Identity wins: one thread is one judgment, and counting it twice
    would show two open objections where the reviewer left one.
    """

    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=4, fingerprint_method="hunk_patch_sha256")
    klass = _unit("symbol", "sym-class", symbol="C", start=1, end=4, fingerprint_method="symbol_body_sha256")
    method = _unit("symbol", "sym-a", symbol="C.a", start=3, end=4, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, klass, method)
    patch = " class C:\n-    X = 1\n+    X = 2\n     def a(self):\n-        return 1\n+        return 99\n"
    comment = Annotation(
        id="ann-1",
        review_id="review-1",
        revision_id="rev-1",
        anchor=AnnotationAnchor(path="src/mod.py", side="new", start_line=4, end_line=4, unit_key="sym-class"),
        body="Both of these need rethinking together.",
        kind="request_change",
    )

    targets = derive_review_targets(
        units,
        tuple(_entry(unit) for unit in units),
        _packet(_hunk(1, 1, patch, old_lines=4, new_lines=4)),
        annotations=(comment,),
    )

    counts = {target.unit_key: target.annotation_counts.open for target in targets}
    assert counts == {"sym-class": 1, "sym-a": 0}


def test_symbol_descriptors_alone_do_not_make_a_target_high_attention() -> None:
    """Every symbol unit says "symbol modified", so it argues for nothing.

    Every symbol unit carries its change verb, and a caller count of zero says
    the index found nothing pointing at it. Reading those as findings puts the
    whole review in "needs attention" and leaves bulk completion with nothing it
    is allowed to sweep.
    """

    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=2, end=2, fingerprint_method="hunk_patch_sha256")
    symbol = _unit("symbol", "sym", symbol="f", start=1, end=3, fingerprint_method="symbol_body_sha256")
    described = replace(symbol, reasons=("+1 -1", "symbol modified", "0 known callers"))
    units = (file_unit, hunk_unit, described)
    packet = _packet(_hunk(2, 2, "-    a = 1\n+    a = 2\n"))

    targets = derive_review_targets(units, tuple(_entry(unit) for unit in units), packet)
    assert [(target.unit_key, target.attention_level) for target in targets] == [("sym", "normal")]

    promoted = replace(described, reasons=(*described.reasons, "public contract changed"))
    targets = derive_review_targets(
        (file_unit, hunk_unit, promoted),
        tuple(_entry(unit) for unit in (file_unit, hunk_unit, promoted)),
        packet,
    )
    assert [(target.unit_key, target.attention_level) for target in targets] == [("sym", "high")]


def test_a_caller_count_promotes_the_file_it_ranks_and_no_symbol_inside_it() -> None:
    """ "4 known callers" is written by two emitters in identical words.

    ``ordering.rank_files`` writes it for a file, where it separates that file
    from the others. ``units._symbol_reasons`` then copies the file's whole
    reason list onto every symbol and hunk unit of that file and adds the
    symbol's own count in the same words -- so inside one file it is on every
    row and separates nothing. Suppressing only the ``0`` spelling left every
    target "high" on any repo whose call index resolves, and bulk completion
    with nothing it was allowed to sweep.
    """

    ranked = ("+3 -1", "4 known callers")
    file_unit = replace(_unit("file", "fil"), reasons=ranked)
    hunk_unit = replace(
        _unit("hunk", "hun", ordinal=0, start=2, end=2, fingerprint_method="hunk_patch_sha256"),
        reasons=ranked,
    )
    symbol = replace(
        _unit("symbol", "sym", symbol="f", start=1, end=3, fingerprint_method="symbol_body_sha256"),
        reasons=(*ranked, "symbol modified", "0 known callers"),
    )
    units = (file_unit, hunk_unit, symbol)

    targets = derive_review_targets(
        units,
        tuple(_entry(unit) for unit in units),
        _packet(_hunk(2, 2, "-    a = 1\n+    a = 2\n")),
    )
    assert [(target.unit_key, target.attention_level) for target in targets] == [("sym", "normal")]

    # The same words on the row the ranker actually wrote them for still promote:
    # a binary file has no symbols to be confused with, and falls back to a file
    # target that keeps the ranker's finding.
    binary_unit = replace(_unit("file", "bin", path="assets/blob.bin"), reasons=ranked)
    binary_targets = derive_review_targets(
        (binary_unit,),
        (_entry(binary_unit),),
        _packet(path="assets/blob.bin", is_binary=True, additions=3, deletions=1),
    )
    assert [(target.kind, target.attention_level) for target in binary_targets] == [("file", "high")]

    assert reason_is_promoting("4 known callers") is True
    assert reason_is_promoting("4 known callers", kind="symbol") is False
    assert reason_is_promoting("1 known caller", kind="hunk") is False
    assert reason_is_promoting("0 known callers") is False


def test_file_scoped_verification_uses_newest_current_check_per_title() -> None:
    file_unit = _unit("file", "fil", path="notes.txt")
    hunk_unit = _unit(
        "hunk",
        "hun",
        path="notes.txt",
        ordinal=0,
        start=1,
        end=1,
        fingerprint_method="hunk_patch_sha256",
    )
    packet = _packet(_hunk(1, 1, "+new\n", old_lines=0, new_lines=1), path="notes.txt")
    evidence = (
        ReviewEvidence(
            id="ev-new-fail",
            review_id="review-1",
            revision_id="rev-1",
            kind="document",
            title="Focused tests",
            path="notes.txt",
            verification_status="FAIL",
            created_at="2026-09-13T12:00:00+00:00",
        ),
        ReviewEvidence(
            id="ev-old-pass",
            review_id="review-1",
            revision_id="rev-1",
            kind="document",
            title="Focused tests",
            path="notes.txt",
            verification_status="PASS",
            created_at="2026-09-13T11:00:00+00:00",
        ),
        ReviewEvidence(
            id="ev-typecheck",
            review_id="review-1",
            revision_id="rev-1",
            kind="document",
            title="Typecheck",
            path="notes.txt",
            verification_status="PASS",
            created_at="2026-09-13T12:30:00+00:00",
        ),
        ReviewEvidence(
            id="ev-lint",
            review_id="review-1",
            revision_id="rev-1",
            kind="document",
            title="Lint",
            path="notes.txt",
            verification_status="NOT_RUN",
            created_at="2026-09-13T12:15:00+00:00",
        ),
        ReviewEvidence(
            id="ev-global",
            review_id="review-1",
            revision_id="rev-1",
            kind="document",
            title="Full suite",
            path="",
            verification_status="FAIL",
            created_at="2026-09-13T12:50:00+00:00",
        ),
        ReviewEvidence(
            id="ev-context-only",
            review_id="review-1",
            revision_id="rev-1",
            kind="screenshot",
            title="Preview",
            path="notes.txt",
            verification_status="",
            created_at="2026-09-13T12:55:00+00:00",
        ),
    )

    targets = derive_review_targets(
        (file_unit, hunk_unit),
        (_entry(file_unit), _entry(hunk_unit)),
        packet,
        evidence=evidence,
    )

    assert len(targets) == 1
    assert targets[0].verification.pass_count == 1
    assert targets[0].verification.fail_count == 1
    assert targets[0].verification.unknown_count == 1


def test_progress_counts_targets_not_overlapping_raw_units() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=4, end=4, fingerprint_method="hunk_patch_sha256")
    outer = _unit("symbol", "outer", symbol="C", start=1, end=5, fingerprint_method="symbol_body_sha256")
    inner = _unit("symbol", "inner", symbol="C.f", start=3, end=5, fingerprint_method="symbol_body_sha256")
    units = (file_unit, hunk_unit, outer, inner)
    frontier = tuple(_entry(unit, state="reviewed" if unit is inner else "unreviewed") for unit in units)
    packet = _packet(_hunk(4, 4, "-        return 1\n+        return 2\n"))

    targets = derive_review_targets(units, frontier, packet)
    progress = review_progress(targets)

    assert len(units) == 4
    assert progress.target_count == 1
    assert progress.reviewed == 1
    assert progress.unreviewed == 0


def test_revision_delta_preserves_reviewed_work_and_keeps_rename_aliases_out_of_new_work() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=1, fingerprint_method="hunk_patch_sha256")
    symbol = _unit("symbol", "sym", symbol="run", start=1, end=1, fingerprint_method="symbol_body_sha256")
    base = derive_review_targets(
        (file_unit, hunk_unit, symbol),
        tuple(
            _entry(unit, state="reviewed" if unit is symbol else "unreviewed")
            for unit in (file_unit, hunk_unit, symbol)
        ),
        _packet(_hunk(1, 1, "-old\n+new\n")),
    )[0]
    assert base.unit_key == "sym"

    renamed = replace(
        base,
        target_id="target:sym-renamed",
        unit_key="sym-renamed",
        path="src/renamed.py",
        label="src/renamed.py::run",
    )
    reopened = replace(base, target_id="target:changed", unit_key="changed", state="changed_since_review")
    new_target = replace(base, target_id="target:new", unit_key="new", symbol="fresh", state="unreviewed")
    removed = replace(base, target_id="target:removed", unit_key="removed", symbol="gone")

    delta = revision_target_delta(
        (base, replace(base, target_id="target:old-changed", unit_key="changed"), removed),
        (renamed, reopened, new_target),
        aliases=(("sym", "sym-renamed"),),
    )

    assert [target.unit_key for target in delta.preserved] == ["sym-renamed"]
    assert [target.unit_key for target in delta.reopened] == ["changed"]
    assert [target.unit_key for target in delta.added] == ["new"]
    assert [target.unit_key for target in delta.removed] == ["removed"]
    assert {target.unit_key for target in delta.active} == {"changed", "new"}


def test_revision_delta_keeps_eighty_percent_preserved_reviewed_work_out_of_active_queue() -> None:
    file_unit = _unit("file", "fil")
    hunk_unit = _unit("hunk", "hun", ordinal=0, start=1, end=1, fingerprint_method="hunk_patch_sha256")
    symbol = _unit("symbol", "sym", symbol="run", start=1, end=1, fingerprint_method="symbol_body_sha256")
    exemplar = derive_review_targets(
        (file_unit, hunk_unit, symbol),
        tuple(
            _entry(unit, state="reviewed" if unit is symbol else "unreviewed")
            for unit in (file_unit, hunk_unit, symbol)
        ),
        _packet(_hunk(1, 1, "-old\n+new\n")),
    )[0]
    previous = tuple(replace(exemplar, target_id=f"target:t{i}", unit_key=f"t{i}", symbol=f"f{i}") for i in range(5))
    current = tuple(
        replace(target, state="changed_since_review" if index == 4 else "reviewed")
        for index, target in enumerate(previous)
    )

    delta = revision_target_delta(previous, current)

    assert len(delta.preserved) == 4
    assert [target.unit_key for target in delta.reopened] == ["t4"]
    assert [target.unit_key for target in delta.active] == ["t4"]


def test_outline_summarizes_target_state_per_file() -> None:
    file_unit = _unit("file", "fil", path="generated.lock", group="generated")
    hunk_unit = _unit(
        "hunk",
        "hun",
        path="generated.lock",
        ordinal=0,
        start=1,
        end=1,
        fingerprint_method="hunk_patch_sha256",
        group="generated",
    )
    targets = derive_review_targets(
        (file_unit, hunk_unit),
        (_entry(file_unit), _entry(hunk_unit)),
        _packet(_hunk(1, 1, "+x\n", old_lines=0, new_lines=1), path="generated.lock"),
    )

    outline = build_review_outline(targets)
    progress = review_progress(targets)

    assert len(outline) == 1
    assert outline[0].path == "generated.lock"
    assert outline[0].target_count == 1
    assert outline[0].mechanical == 1
    assert progress.mechanical == 1
