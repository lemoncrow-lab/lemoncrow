"""G10: a team that moves from another review tool brings its settings along, and loses none of them silently.

``settings_import`` is a plain key map from a settings file (JSON or YAML, beside the rules ``import.rules_files`` already
reads) to LemonCrow's settings. Each setting is carried, mapped to the closest setting LemonCrow has, or dropped with the
reason and the closest thing it has. A rule's severity can never be written above P1: a settings file is not evidence,
and only a critical rule, or evidence from beyond the model's own code, makes a P0 (``criticality.severity_for``).

The fixtures are tiny and synthetic; no tool is named.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review import settings_import as si
from lemoncrow.pro.capabilities.review.criticality import severity_for
from lemoncrow.pro.capabilities.review.pr_bot.settings import MAX_IGNORE, resolve_policy, severity_with_overrides
from lemoncrow.pro.capabilities.review.rules import path_matches, resolve_review_governance
from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument, resolve_trusted_base_document

REVIEW_YAML = ".lemoncrow/review.yaml"
CONFIG = "team-review/config.json"
KEY = f"imported:{CONFIG}"


def _rows(plan: si.SettingsPlan, outcome: str | None = None) -> list[si.ReportRow]:
    return [row for row in plan.rows if outcome is None or row.outcome == outcome]


def _translate(**settings: Any) -> si.SettingsPlan:
    return si.translate(settings, path=CONFIG)


def _doc(path: str, files: dict[str, str]) -> TrustedBaseDocument:
    document = resolve_trusted_base_document(path, files.get(path, ""))
    return TrustedBaseDocument(
        path=document.path,
        content=document.content,
        state=document.state,
        source=f"base:{path}" if document.state == "present" else "",
    )


def _governance(files: dict[str, str]):
    return resolve_review_governance(_doc(REVIEW_YAML, files), read_base_document=lambda path: _doc(path, files))


def _notes(governance, kind: str) -> list[str]:
    return [row.message for row in governance.imports.notes if row.kind == kind]


# --------------------------------------------------------------------------- #
# strictness -> review.strictness
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", [1, 2, 3])
def test_strictness_is_carried_to_the_same_number(level: int) -> None:
    plan = _translate(strictness=level)
    assert plan.review == {"strictness": level}
    [row] = plan.rows
    assert (row.outcome, row.target) == ("carried", f"review.strictness: {level}")


def test_strictness_two_says_the_default_differs() -> None:
    [row] = _translate(strictness=2).rows
    assert "default differs" in row.detail and "use 1 for inline P2 comments" in row.detail
    assert "default differs" not in _translate(strictness=3).rows[0].detail


@pytest.mark.parametrize("bad", [0, 4, True, "high", 2.5, None, [3]])
def test_a_strictness_outside_one_to_three_is_dropped_and_names_the_closest_setting(bad: object) -> None:
    plan = _translate(strictness=bad)
    assert plan.review == {} and plan.needs == ()
    [row] = plan.rows
    assert row.outcome == "dropped" and "review.strictness (1, 2 or 3)" in row.detail


# --------------------------------------------------------------------------- #
# ignorePatterns -> review.ignore
# --------------------------------------------------------------------------- #


def test_gitignore_lines_become_globs_that_match_what_the_lines_match() -> None:
    lines = [
        "# generated",
        "",
        "dist/",
        "*.lock",
        "/build",
        "node_modules",
        "src/gen/*.py",
        "**/vendored/**",
        "docs/a.md",
    ]
    globs, problems = si.gitignore_globs(lines)
    assert problems == {}
    for path in (
        "dist/a.js",  # a directory name, anywhere
        "pkg/dist/a.js",
        "a.lock",  # a file pattern, at any depth
        "pkg/b.lock",
        "build/out.js",  # anchored by the leading slash
        "node_modules",
        "pkg/node_modules/x/y.js",
        "src/gen/a.py",
        "vendored/a.txt",  # a leading **/ and a trailing /** , at any depth
        "pkg/vendored/a.txt",
        "docs/a.md",
    ):
        assert path_matches(path, globs), path
    for path in (
        "distribution/a.js",  # a name is not a prefix
        "mydist/a.js",
        "src/build/out.js",  # anchored: not at depth
        "src/gen/readme.txt",
        "src/app.py",
        "docs/a.md.bak",
    ):
        assert not path_matches(path, globs), path


def test_an_escaped_hash_or_bang_is_a_literal_name_not_a_comment_or_a_negation() -> None:
    globs, problems = si.gitignore_globs(["\\#notes.txt", "\\!bang.txt"])
    assert problems == {}
    assert path_matches("#notes.txt", globs) and path_matches("pkg/!bang.txt", globs)
    assert not path_matches("notes.txt", globs)


def test_lines_that_cannot_be_carried_are_named_and_never_widen_what_is_ignored() -> None:
    globs, problems = si.gitignore_globs(["!keep/**", "*", "**", "../up", "a\\b", "~/x", "docs/"])
    assert globs == ["docs/**", "**/docs/**"]
    assert problems == {
        "negation": ["!keep/**"],
        "everything": ["*", "**"],
        "unsafe": ["../up", "a\\b", "~/x"],
    }


def test_ignore_patterns_are_carried_to_review_ignore_and_each_problem_is_a_row() -> None:
    plan = _translate(ignorePatterns="dist/\n*.lock\n!dist/keep.js\n*\n")
    assert plan.review["ignore"] == ["dist/**", "**/dist/**", "*.lock"]
    carried = _rows(plan, "carried")[0]
    assert carried.target == "review.ignore: 3 globs"
    assert "2 pattern lines became 3 globs" in carried.detail  # the negation and the match-all line are not carried
    assert "hides an ignored file from the model as context" in carried.detail
    dropped = " ".join(row.detail for row in _rows(plan, "dropped"))
    assert "Negation (!dist/keep.js) cannot be expressed" in dropped and "narrow the earlier pattern" in dropped
    assert "* would leave every file out of the review; not carried." in dropped


def test_ignore_patterns_given_as_a_list_work_and_a_bad_type_is_dropped() -> None:
    assert _translate(ignorePatterns=["dist/", "*.lock\n*.tmp"]).review["ignore"] == [
        "dist/**",
        "**/dist/**",
        "*.lock",
        "*.tmp",
    ]
    plan = _translate(ignorePatterns=7)
    assert plan.review == {} and _rows(plan)[0].outcome == "dropped"
    assert "Closest: review.ignore" in _rows(plan)[0].detail


def test_globs_over_the_limit_are_left_out_and_said() -> None:
    plan = _translate(ignorePatterns="\n".join(f"*.e{index}" for index in range(MAX_IGNORE + 6)))
    assert len(plan.review["ignore"]) == MAX_IGNORE
    [row] = _rows(plan, "dropped")
    assert f"6 globs are over the limit of {MAX_IGNORE}" in row.detail


# --------------------------------------------------------------------------- #
# commentTypes -> the finding-type controls LemonCrow has
# --------------------------------------------------------------------------- #


def test_the_defect_comment_types_need_no_setting() -> None:
    plan = _translate(commentTypes=["logic", "syntax"])
    assert plan.review == {} and plan.needs == ()
    assert [(row.setting, row.outcome, row.target) for row in plan.rows] == [
        ("commentTypes: logic", "carried", "built in"),
        ("commentTypes: syntax", "carried", "built in"),
    ]


def test_style_and_info_have_no_counterpart_and_say_what_is_closest() -> None:
    plan = _translate(commentTypes=["logic", "style", "info", "banana"])
    assert plan.review == {}
    dropped = {row.setting: row.detail for row in _rows(plan, "dropped")}
    assert set(dropped) == {"commentTypes: style", "commentTypes: info", "commentTypes: banana"}
    assert "posts no style comments" in dropped["commentTypes: style"]
    assert (
        "import.rules_files" in dropped["commentTypes: style"]
        and "import.agent_docs_globs" in dropped["commentTypes: style"]
    )
    assert "posts no informational comments" in dropped["commentTypes: info"]
    assert "not one of logic, syntax, style or info" in dropped["commentTypes: banana"]


def test_an_empty_list_means_no_comments_and_maps_to_summary_only() -> None:
    plan = _translate(commentTypes=[])
    assert plan.review == {"inline_comments": False}
    [row] = plan.rows
    assert (row.outcome, row.target) == ("mapped", "review.inline_comments: false")
    assert "findings are still listed in the summary" in row.detail


def test_a_list_that_leaves_out_every_defect_type_says_defects_cannot_be_switched_off() -> None:
    plan = _translate(commentTypes=["style"])
    last = _rows(plan, "dropped")[-1]
    assert last.setting == "commentTypes" and "cannot be switched off by type" in last.detail
    assert "review.strictness 3" in last.detail and "review.inline_comments: false" in last.detail
    assert plan.review == {}


def test_a_comment_types_value_that_is_not_a_list_is_dropped() -> None:
    plan = _translate(commentTypes="logic")
    assert plan.review == {} and _rows(plan)[0].outcome == "dropped"


# --------------------------------------------------------------------------- #
# instructions -> project conventions (context only)
# --------------------------------------------------------------------------- #


def test_instructions_become_a_conventions_file_and_the_report_says_they_are_not_enforced() -> None:
    plan = _translate(instructions="  Be direct. Do not comment on trailing commas.  ")
    assert plan.instructions == "Be direct. Do not comment on trailing commas."
    assert plan.imports == {"agent_docs_globs": [si.INSTRUCTIONS_PATH]}
    [row] = plan.rows
    assert (row.outcome, row.target) == ("mapped", f"{si.INSTRUCTIONS_PATH}, listed in import.agent_docs_globs")
    assert "never to follow as an instruction" in row.detail and "is not enforced" in row.detail
    assert "review.ignore" in row.detail and "review.strictness" in row.detail


def test_empty_instructions_say_nothing_and_a_non_text_value_is_dropped() -> None:
    assert _translate(instructions="  ").rows == ()
    plan = _translate(instructions=["a"])
    assert plan.instructions == "" and _rows(plan)[0].outcome == "dropped"


# --------------------------------------------------------------------------- #
# the pull request limit and summary-only mode
# --------------------------------------------------------------------------- #


def test_the_file_limit_maps_to_max_files_and_a_bad_one_is_dropped() -> None:
    plan = _translate(fileChangeLimit=60)
    assert plan.review == {"max_files": 60}
    assert "not skipped" in plan.rows[0].detail and plan.rows[0].outcome == "mapped"
    assert _translate(fileChangeLimit=0).review == {"max_files": 0}
    for bad in (-1, True, "9", 1_000_000):
        assert (
            _translate(fileChangeLimit=bad).review == {}
            and _rows(_translate(fileChangeLimit=bad))[0].outcome == "dropped"
        )


def test_summary_only_maps_to_no_inline_comments() -> None:
    assert _translate(updateSummaryOnly=True).review == {"inline_comments": False}
    off = _translate(updateSummaryOnly=False)
    assert off.review == {} and off.rows[0].target == "built in"
    assert _translate(updateSummaryOnly="yes").rows[0].outcome == "dropped"


# --------------------------------------------------------------------------- #
# what has no counterpart is dropped, with the reason and the closest thing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", sorted(k for k in si.KNOWN_KEYS if k in si._DROPPED))
def test_a_setting_with_no_counterpart_is_dropped_with_why_and_the_closest_setting(key: str) -> None:
    plan = _translate(**{key: True})
    assert plan.review == {} and plan.imports == {} and plan.needs == ()
    [row] = plan.rows
    assert row.outcome == "dropped" and row.setting == key
    assert "so nothing was set. Closest: " in row.detail
    assert row.detail[0].isupper()


def test_a_key_nobody_knows_is_dropped_and_rules_are_left_to_the_loader() -> None:
    plan = _translate(frobnicate=1, rules=[])
    assert [(row.setting, row.outcome) for row in plan.rows] == [("frobnicate", "dropped")]


# --------------------------------------------------------------------------- #
# a rule's severity: never above P1
# --------------------------------------------------------------------------- #


def _rule(rule_id: str, severity: object, **extra: Any) -> dict[str, Any]:
    return {"id": rule_id, "rule": f"Rule {rule_id}.", "severity": severity, **extra}


@pytest.mark.parametrize(
    ("word", "written"),
    [
        ("high", "p1"),
        ("HIGH", "p1"),
        ("medium", "p2"),
        ("low", "p2"),
        ("p1", "p1"),
        ("p2", "p2"),
        ("critical", "p1"),
        ("p0", "p1"),
        ("P0", "p1"),
    ],
)
def test_a_rule_severity_is_written_as_p1_or_p2_and_never_as_p0(word: str, written: str) -> None:
    plan = _translate(rules=[_rule("r", word)])
    assert plan.review["rules"] == {f"{KEY}:r": {"severity": written}}
    assert written in ("p1", "p2")
    assert plan.needs == (("review", "rules", f"{KEY}:r"),)


def test_asking_for_p0_is_written_as_p1_and_the_report_says_why() -> None:
    plan = _translate(rules=[_rule("a", "critical"), _rule("b", "p0"), _rule("c", "high"), _rule("d", "low")])
    by_word = {row.setting.split(" (")[0]: row for row in plan.rows}
    assert set(by_word) == {"severity: critical", "severity: p0", "severity: high", "severity: low"}
    for word in ("critical", "p0"):
        row = by_word[f"severity: {word}"]
        assert row.target == "review.rules.<key>.severity: p1" and row.outcome == "mapped"
        assert "Asked for P0, written as p1" in row.detail and "criticality.severity_for" in row.detail
    assert "p1 is the most a settings file can set" in by_word["severity: high"].detail
    assert "no rule severity below P2" in by_word["severity: low"].detail
    assert by_word["severity: high"].setting == "severity: high (1 rule: c)"


def test_many_rules_with_one_severity_are_one_row() -> None:
    plan = _translate(rules=[_rule(f"r{index}", "high") for index in range(8)])
    [row] = plan.rows
    assert row.setting == "severity: high (8 rules: r0, r1, r2, r3, r4 and 3 more)"


def test_an_unknown_severity_word_is_dropped_and_the_ladder_applies() -> None:
    plan = _translate(rules=[_rule("odd", "urgent")])
    assert "rules" not in plan.review
    [row] = plan.rows
    assert row.outcome == "dropped" and row.setting == "severity (1 rule: odd 'urgent')"
    assert "ordinary ladder applies" in row.detail


def test_only_a_rule_the_loader_imports_gets_a_severity() -> None:
    plan = _translate(
        rules=[
            _rule("ok", "high"),
            _rule("off", "high", enabled=False),
            _rule("noscope", "high", scope=[]),
            _rule("escape", "high", scope=["../x"]),
            {"id": "notext", "severity": "high"},
            {"rule": "no id", "severity": "high"},
            "not an object",
        ]
    )
    assert plan.review["rules"] == {f"{KEY}:ok": {"severity": "p1"}}


def test_the_severity_key_is_the_one_the_loader_gives_the_rule() -> None:
    """The key is written against the rule the loader really imports, even with an odd id."""

    config = {"rules": [_rule("Needs Cleaning!", "high"), _rule(7, "low")]}
    plan = si.translate(config, path=CONFIG)
    files = {REVIEW_YAML: yaml.safe_dump({"import": {"rules_files": [CONFIG]}}), CONFIG: json.dumps(config)}
    keys = {rule.stable_key for rule in _governance(files).rules}
    assert set(plan.review["rules"]) == keys


# --------------------------------------------------------------------------- #
# the cap holds all the way to the finding's severity
# --------------------------------------------------------------------------- #


def _policy_for(config: dict[str, Any], review: dict[str, Any]):
    files = {
        REVIEW_YAML: yaml.safe_dump({"review": review, "import": {"rules_files": [CONFIG]}}),
        CONFIG: json.dumps(config),
    }
    governance = _governance(files)
    return governance, resolve_policy(config=_doc(REVIEW_YAML, files), triage=None, rules=tuple(governance.rules))


def test_a_severity_from_a_settings_file_cannot_make_a_model_only_finding_p0() -> None:
    config = {"rules": [_rule("high", "high"), _rule("asks-p0", "p0"), _rule("low", "low")]}
    plan = si.translate(config, path=CONFIG)
    governance, policy = _policy_for(config, dict(plan.review))
    refs = {rule.stable_key: rule.ref for rule in governance.rules}
    assert policy.notes == ()  # every key the import wrote is a rule the loader imported
    for rule_id in ("high", "asks-p0"):
        ref = refs[f"{KEY}:{rule_id}"]
        for category in ("correctness", "security", "persistence_migration"):
            # A model-only finding (nothing from beyond its own code backs it) is at most P1, with or without the file ...
            assert severity_for(category, impact="high", verified=False) == "p1"
            assert (
                severity_with_overrides(
                    category, impact="high", verified=False, rule_refs=[ref], overrides=policy.rule_severity
                )
                == "p1"
            )
    # ... and the file raised a P2-rated finding to P1, which is what a high severity is for.
    ref = refs[f"{KEY}:high"]
    assert severity_for("correctness", impact="medium", verified=False) == "p2"
    assert (
        severity_with_overrides(
            "correctness", impact="medium", verified=False, rule_refs=[ref], overrides=policy.rule_severity
        )
        == "p1"
    )
    assert set(policy.rule_severity.values()) <= {"p1", "p2"}


def test_a_hand_edited_p0_for_an_imported_rule_still_stops_at_p1() -> None:
    """What the import never writes is stopped anyway, by the policy (an imported rule is advisory)."""

    config = {"rules": [_rule("r", "high")]}
    governance, policy = _policy_for(config, {"rules": {f"{KEY}:r": {"severity": "p0"}}})
    [rule] = governance.rules
    assert rule.severity == "advisory"
    assert policy.rule_severity == {rule.ref: "p1"}
    assert any("only a critical rule can be P0" in note for note in policy.notes)


def test_a_severity_the_import_wrote_holds_a_promoted_rule_below_p0_until_its_entry_is_deleted() -> None:
    """The documented interaction: ``review.rules`` replaces the ladder, so a gate needs its entry removed."""

    config = {"rules": [_rule("gate", "high")]}
    plan = si.translate(config, path=CONFIG)
    key = f"{KEY}:gate"
    imports = {"rules_files": [CONFIG], "critical": [key]}
    held = {
        REVIEW_YAML: yaml.safe_dump({"review": dict(plan.review), "import": imports}),
        CONFIG: json.dumps(config),
    }
    governance = _governance(held)
    [rule] = governance.rules
    assert rule.severity == "critical"
    policy = resolve_policy(config=_doc(REVIEW_YAML, held), triage=None, rules=tuple(governance.rules))
    assert policy.rule_severity == {rule.ref: "p1"}  # held at what the import wrote
    free = {REVIEW_YAML: yaml.safe_dump({"import": imports}), CONFIG: json.dumps(config)}
    policy = resolve_policy(config=_doc(REVIEW_YAML, free), triage=None, rules=tuple(_governance(free).rules))
    assert policy.rule_severity == {}  # entry deleted: the rule is a gate again


# --------------------------------------------------------------------------- #
# a settings key in a rules file: told what it was, quiet once review.yaml carries it
# --------------------------------------------------------------------------- #

SETTINGS_FILE: dict[str, Any] = {
    "strictness": 3,
    "commentTypes": ["logic", "style"],
    "ignorePatterns": "dist/\n*.lock",
    "instructions": "Be direct.",
    "fileChangeLimit": 40,
    "labels": ["skip"],
    "rules": [_rule("no-raw-sql", "high", scope=["src/db/**"]), _rule("use-logger", "low")],
}


def _carried_review_yaml(config: dict[str, Any]) -> str:
    plan = si.translate(config, path=CONFIG)
    return yaml.safe_dump({"review": dict(plan.review), "import": {"rules_files": [CONFIG], **plan.imports}})


def test_before_the_import_each_setting_is_a_specific_warning_that_names_the_closest_setting() -> None:
    files = {REVIEW_YAML: yaml.safe_dump({"import": {"rules_files": [CONFIG]}}), CONFIG: json.dumps(SETTINGS_FILE)}
    warnings = _notes(_governance(files), "warning")
    expected = {
        "strictness is a setting, and a rules file holds rules": "review.strictness: 3",
        "ignorePatterns is a setting": "review.ignore: 3 globs",
        "instructions is a setting": f"{si.INSTRUCTIONS_PATH}, listed in import.agent_docs_globs",
        "fileChangeLimit is a setting": "review.max_files: 40",
        "commentTypes: LemonCrow posts no style comments": "import.agent_docs_globs",
        "labels: Which pull requests are reviewed": "branches:, paths-ignore:",
        "rule 'no-raw-sql' severity is not applied from a rules file": f"review.rules.{KEY}:no-raw-sql.severity: p1",
        "rule 'use-logger' severity is not applied from a rules file": f"review.rules.{KEY}:use-logger.severity: p2",
    }
    for start, closest in expected.items():
        [message] = [w for w in warnings if w.startswith(start)]
        assert closest in message, (start, message)
        if "is a setting" in start or "severity" in start:
            assert "Run `lc review config import <folder>` to write it." in message
    assert not [w for w in warnings if "unknown key" in w]


def test_once_review_yaml_carries_a_setting_its_note_is_not_a_warning() -> None:
    files = {REVIEW_YAML: _carried_review_yaml(SETTINGS_FILE), CONFIG: json.dumps(SETTINGS_FILE)}
    governance = _governance(files)
    warnings = _notes(governance, "warning")
    # Only what LemonCrow cannot take is left: the style comment type and the filter that belongs to the workflow.
    assert sorted(w.split(":")[0] for w in warnings) == ["commentTypes", "labels"]
    infos = _notes(governance, "info")
    for carried in (
        "strictness: carried by .lemoncrow/review.yaml",
        "ignorePatterns: carried by .lemoncrow/review.yaml",
        "instructions: carried by .lemoncrow/review.yaml",
        "fileChangeLimit: carried by .lemoncrow/review.yaml",
        "rule 'no-raw-sql' severity: carried by .lemoncrow/review.yaml",
        "rule 'use-logger' severity: carried by .lemoncrow/review.yaml",
    ):
        assert carried in infos, (carried, infos)
    assert governance.errors == ()


def test_a_hand_set_finding_threshold_also_counts_as_carrying_strictness() -> None:
    files = {
        REVIEW_YAML: yaml.safe_dump({"review": {"finding_threshold": "high"}, "import": {"rules_files": [CONFIG]}}),
        CONFIG: json.dumps({"strictness": 3, "rules": []}),
    }
    governance = _governance(files)
    assert _notes(governance, "warning") == []
    assert "strictness: carried by .lemoncrow/review.yaml" in _notes(governance, "info")


def test_the_defect_comment_types_alone_are_not_a_warning() -> None:
    files = {
        REVIEW_YAML: yaml.safe_dump({"import": {"rules_files": [CONFIG]}}),
        CONFIG: json.dumps({"commentTypes": ["logic"], "rules": []}),
    }
    governance = _governance(files)
    assert _notes(governance, "warning") == []
    assert "commentTypes: nothing to set in LemonCrow" in _notes(governance, "info")


def test_a_key_that_is_not_a_setting_is_still_just_unknown() -> None:
    files = {REVIEW_YAML: yaml.safe_dump({"import": {"rules_files": [CONFIG]}}), CONFIG: json.dumps({"frobnicate": 1})}
    assert "unknown key 'frobnicate' ignored" in _notes(_governance(files), "warning")


def test_a_review_yaml_that_does_not_parse_carries_nothing() -> None:
    assert si.configured_settings("review: [unclosed") == {}
    assert si.configured_settings("") == {}
    assert si.configured_settings("- a list") == {}
    assert si.configured_settings("review: {strictness: 3}\nimport: 5\nother: 1") == {"review": {"strictness": 3}}


def test_the_import_report_counts_each_outcome() -> None:
    plan = si.translate(SETTINGS_FILE, path=CONFIG)
    counts = plan.counts()
    assert counts["dropped"] == 2  # the style comment type, the filter
    assert counts["carried"] >= 3 and counts["mapped"] >= 3
    assert sum(counts.values()) == len(plan.rows)
    assert all(row.to_wire().keys() == {"setting", "outcome", "target", "detail"} for row in plan.rows)
