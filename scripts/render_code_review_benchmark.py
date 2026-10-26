#!/usr/bin/env python3
"""Render and check the public code-review benchmark table.

``evals/review/published/code_review_benchmark.json`` is the one source of the published table: the public 50-PR
benchmark's dataset and method, the figures other tools were reported to score (each with its source, date and who
reported it) and, once a run exists, LemonCrow's own block. This script validates that file and renders it into

* the README, between ``<!-- lemoncrow:benchmark:start -->`` and ``<!-- lemoncrow:benchmark:end -->``, and
* the landing site's data copy, ``src/data/code-review-benchmark.json`` in the landing checkout (``--landing DIR``).

While LemonCrow's block is ``{"status": "pending"}`` the README section is EMPTY and the landing renders nothing: a
table of other tools' scores is never published without ours. Nothing here estimates, defaults or fills a LemonCrow
number. The only way one gets into the file is ``--from-run``, which reads a run's ``benchmark_summary`` (the shape is
in ``evals/review/published/README.md``), refuses anything that does not add up, copies the raw run report into
``evals/review/published/runs/`` and links it.

Usage::

    uv run python scripts/render_code_review_benchmark.py                 # render the README from the JSON
    uv run python scripts/render_code_review_benchmark.py --check         # write nothing; exit 1 if stale or invalid
    uv run python scripts/render_code_review_benchmark.py --landing ../landing
    uv run python scripts/render_code_review_benchmark.py --from-run RUN.json --landing ../landing
    uv run python scripts/render_code_review_benchmark.py --from-run RUN.json --rescored TOOL_ID

``make benchmark-publish`` is the same command (``RUN=`` and ``LANDING=``). Public: this script and its test are mirrored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final

ROOT: Final = Path(__file__).resolve().parents[1]
DATA_RELPATH: Final = "evals/review/published/code_review_benchmark.json"
RUNS_RELPATH: Final = "evals/review/published/runs"
README_RELPATH: Final = "README.md"
LANDING_RELPATH: Final = "src/data/code-review-benchmark.json"
START: Final = "<!-- lemoncrow:benchmark:start -->"
END: Final = "<!-- lemoncrow:benchmark:end -->"
#: A raw run report is linked from the public repository, not from anywhere else.
RUN_URL_PREFIX: Final = "https://github.com/lemoncrow-lab/lemoncrow/blob/main/" + RUNS_RELPATH + "/"
LANDING_SOURCE_NOTE: Final = (
    "Copy of evals/review/published/code_review_benchmark.json in the LemonCrow repository, written by "
    "`make benchmark-publish` (scripts/render_code_review_benchmark.py). Do not edit it here: change the product "
    "file and publish again."
)

TOP_LEVEL: Final = frozenset(
    {
        "schema",
        "updated",
        "dataset",
        "method",
        "third_party",
        "third_party_discrepancies",
        "vendor_claims",
        "lemoncrow",
        "competitor_rescored",
    }
)
VERIFICATION_STATUSES: Final = frozenset({"primary_read", "search_retrieval"})
COUNT_FIELDS: Final = ("tool_comments_scored", "tool_comments_matched", "golden_scored", "golden_matched")
#: Fields of a published block, by kind. A run's ``benchmark_summary`` carries all but the four the importer adds.
_COMMON: Final = {"status", "measured_by", "scored_by", "date", "prs_reviewed", "answer_key_commit"} | {
    "precision",
    "recall",
    "f_score",
    "counts",
    "judge",
    "run_report",
}
PUBLISHED_FIELDS: Final = {
    "lemoncrow": frozenset(_COMMON | {"review", "engine", "cost"}),
    "competitor_rescored": frozenset(_COMMON | {"tool_id", "tool", "comments_source"}),
}
_ADDED_BY_IMPORT: Final = frozenset({"status", "measured_by", "scored_by", "run_report"})
SUMMARY_FIELDS: Final = {kind: fields - _ADDED_BY_IMPORT for kind, fields in PUBLISHED_FIELDS.items()}
_DATE: Final = re.compile(r"\d{4}-\d{2}(-\d{2})?")
_SHA40: Final = re.compile(r"[0-9a-f]{40}")
_SHA64: Final = re.compile(r"[0-9a-f]{64}")
_SLUG: Final = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_OURS: Final = ("LemonCrow", "LemonCrow's judge")


# --------------------------------------------------------------------------- #
# Numbers                                                                      #
# --------------------------------------------------------------------------- #


def half_up(value: float, places: int = 0) -> Decimal:
    """*value* rounded half up to *places* decimals, from its shortest decimal spelling (1.005 -> 1.01)."""

    return Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def percent(value: float) -> str:
    return f"{half_up(value)}%"


def dollars(value: float) -> str:
    return f"${half_up(value, 2)}"


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _date(value: object, *, day: bool = False) -> bool:
    return isinstance(value, str) and _DATE.fullmatch(value) is not None and (len(value) == 10 or not day)


def _https(value: object) -> bool:
    return isinstance(value, str) and value.startswith("https://") and " " not in value


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #


def _object(
    where: str, block: object, fields: Sequence[str] | frozenset[str] | set[str], found: list[str]
) -> dict[str, Any]:
    """*block* when it is an object with exactly *fields*; problems go to *found*. Never raises."""

    if not isinstance(block, dict):
        found.append(f"{where}: must be an object")
        return {}
    if missing := sorted(set(fields) - set(block)):
        found.append(f"{where}: missing {missing}")
    if extra := sorted(set(block) - set(fields)):
        found.append(f"{where}: unknown fields {extra}")
    return block


def _source(where: str, block: Mapping[str, Any], found: list[str]) -> None:
    for name in ("title", "publisher"):
        if not _text(block.get(name)):
            found.append(f"{where}.{name}: required")
    if not _https(block.get("url")):
        found.append(f"{where}.url: must be an https URL")
    if not _date(block.get("date")):
        found.append(f"{where}.date: must be YYYY-MM or YYYY-MM-DD")
    for name in ("author", "date_kind"):
        if name in block and not _text(block[name]):
            found.append(f"{where}.{name}: must be text")


def _verification(where: str, block: object, found: list[str]) -> None:
    verification = _object(f"{where}.verification", block, {"status", "checked_on", "note"}, found)
    if verification.get("status") not in VERIFICATION_STATUSES:
        found.append(f"{where}.verification.status: must be one of {sorted(VERIFICATION_STATUSES)}")
    if not _date(verification.get("checked_on"), day=True):
        found.append(f"{where}.verification.checked_on: must be YYYY-MM-DD")
    if not _text(verification.get("note")):
        found.append(f"{where}.verification.note: say what was checked, and how")


def _percent_field(where: str, value: object, found: list[str]) -> None:
    if not _number(value) or not 0 <= float(value) <= 100:  # type: ignore[arg-type]
        found.append(f"{where}: must be a number from 0 to 100")


def _check_dataset(dataset: Mapping[str, Any], found: list[str]) -> None:
    if not _text(dataset.get("name")):
        found.append("dataset.name: required")
    repositories = dataset.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        found.append("dataset.repositories: a non-empty list")
    else:
        listed = [
            _object(f"dataset.repositories[{i}]", r, {"name", "language", "prs"}, found)
            for i, r in enumerate(repositories)
        ]
        if not all(_text(r.get("name")) and _text(r.get("language")) and _whole(r.get("prs")) for r in listed):
            found.append("dataset.repositories: each needs a name, a language and a whole number of prs")
        elif sum(r["prs"] for r in listed) != dataset.get("pr_count"):
            found.append("dataset.pr_count: is not the sum of the repositories' prs")
        elif dataset.get("languages") != [r["language"] for r in listed]:
            found.append("dataset.languages: must list the repositories' languages, in order")
    origin = _object(
        "dataset.origin",
        dataset.get("origin"),
        {"title", "publisher", "url", "date", "date_kind", "verification"},
        found,
    )
    _source("dataset.origin", origin, found)
    _verification("dataset.origin", origin.get("verification"), found)
    key_fields = {"name", "publisher", "repository_url", "commit", "commit_date", "golden_comments", "low_severity"} | {
        "scored_comments",
        "readme_states_golden_comments",
        "note",
        "verification",
    }
    key = _object("dataset.answer_key", dataset.get("answer_key"), key_fields, found)
    if not (isinstance(key.get("commit"), str) and _SHA40.fullmatch(key["commit"])):
        found.append("dataset.answer_key.commit: must be the 40-hex pinned commit")
    if not _https(key.get("repository_url")):
        found.append("dataset.answer_key.repository_url: must be an https URL")
    if not _date(key.get("commit_date"), day=True):
        found.append("dataset.answer_key.commit_date: must be YYYY-MM-DD")
    counts = [key.get(n) for n in ("golden_comments", "low_severity", "scored_comments")]
    if not all(_whole(n) for n in counts):
        found.append("dataset.answer_key: golden_comments, low_severity and scored_comments are whole numbers")
    elif counts[0] - counts[1] != counts[2]:
        found.append("dataset.answer_key: golden_comments minus low_severity must equal scored_comments")
    _verification("dataset.answer_key", key.get("verification"), found)


def _check_method(method: Mapping[str, Any], found: list[str]) -> None:
    for name in ("judge_rule", "low_severity", "metrics"):
        if not _text(method.get(name)):
            found.append(f"method.{name}: required")
    source = method.get("source")
    if not isinstance(source, dict):
        found.append("method.source: must be an object")
        return
    _source("method.source", {k: v for k, v in source.items() if k != "verification"}, found)
    _verification("method.source", source.get("verification"), found)


def _check_third_party(rows: object, found: list[str]) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        found.append("third_party: a non-empty list")
        return []
    fields = {"id", "tool", "precision", "recall", "f_score", "reported_by", "self_reported", "scored_by", "run_date"}
    fields |= {"settings", "source", "verification"}
    listed: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        where = f"third_party[{index}]"
        block = _object(where, row, fields, found)
        listed.append(block)
        if not (isinstance(block.get("id"), str) and _SLUG.fullmatch(block["id"])):
            found.append(f"{where}.id: a lowercase slug")
        for name in ("tool", "reported_by", "scored_by", "settings"):
            if not _text(block.get(name)):
                found.append(f"{where}.{name}: required")
        for name in ("precision", "recall", "f_score"):
            _percent_field(f"{where}.{name}", block.get(name), found)
        figures = [block.get(n) for n in ("precision", "recall", "f_score")]
        if all(_number(n) for n in figures) and figures[0] + figures[1]:
            harmonic = 2 * figures[0] * figures[1] / (figures[0] + figures[1])
            if abs(harmonic - figures[2]) > 1.0:  # published figures are rounded to whole numbers
                found.append(f"{where}: F-score {figures[2]} is not the harmonic mean of {figures[0]} and {figures[1]}")
        if not isinstance(block.get("self_reported"), bool):
            found.append(f"{where}.self_reported: true or false")
        if not _date(block.get("run_date"), day=True):
            found.append(f"{where}.run_date: YYYY-MM-DD")
        source = _object(f"{where}.source", block.get("source"), {"title", "publisher", "author", "url", "date"}, found)
        _source(f"{where}.source", source, found)
        _verification(where, block.get("verification"), found)
    for name in ("id", "tool"):
        values = Counter(r.get(name) for r in listed)
        found.extend(f"third_party: duplicate {name} {value!r}" for value, count in values.items() if count > 1)
    return listed


def _check_vendor_claims(claims: object, found: list[str]) -> None:
    if not isinstance(claims, list):
        found.append("vendor_claims: a list")
        return
    fields = {
        "id",
        "tool",
        "claim",
        "value",
        "measures",
        "comparable",
        "comparable_note",
        "reported_by",
        "self_reported",
    }
    for index, claim in enumerate(claims):
        where = f"vendor_claims[{index}]"
        block = _object(where, claim, fields | {"source", "verification"}, found)
        for name in ("id", "tool", "claim", "measures", "comparable_note", "reported_by"):
            if not _text(block.get(name)):
                found.append(f"{where}.{name}: required")
        _percent_field(f"{where}.value", block.get("value"), found)
        if not (isinstance(block.get("comparable"), bool) and isinstance(block.get("self_reported"), bool)):
            found.append(f"{where}: comparable and self_reported are true or false")
        source = _object(
            f"{where}.source", block.get("source"), {"title", "publisher", "url", "date", "date_kind"}, found
        )
        _source(f"{where}.source", source, found)
        _verification(where, block.get("verification"), found)


def _check_scores(where: str, block: Mapping[str, Any], dataset: Mapping[str, Any], found: list[str]) -> None:
    """The judge, the counts and the three figures: the figures must be what the counts give."""

    own: list[str] = []
    judge = _object(f"{where}.judge", block.get("judge"), {"model", "prompt_sha256"}, own)
    if not _text(judge.get("model")):
        own.append(f"{where}.judge.model: required")
    if not (isinstance(judge.get("prompt_sha256"), str) and _SHA64.fullmatch(judge["prompt_sha256"])):
        own.append(f"{where}.judge.prompt_sha256: must be the 64-hex digest of the judge prompt")
    counts = _object(f"{where}.counts", block.get("counts"), COUNT_FIELDS, own)
    for name in COUNT_FIELDS:
        if not _whole(counts.get(name)):
            own.append(f"{where}.counts.{name}: must be a whole number of at least 0")
    for name in ("precision", "recall", "f_score"):
        _percent_field(f"{where}.{name}", block.get(name), own)
    found.extend(own)
    if own:
        return
    scored = dataset.get("answer_key", {}).get("scored_comments")
    if counts["golden_scored"] != scored:
        found.append(f"{where}.counts.golden_scored: {counts['golden_scored']} is not the answer key's {scored}")
    if (
        counts["tool_comments_matched"] > counts["tool_comments_scored"]
        or counts["golden_matched"] > counts["golden_scored"]
    ):
        found.append(f"{where}.counts: more matched than scored")
        return
    precision = (
        100 * counts["tool_comments_matched"] / counts["tool_comments_scored"] if counts["tool_comments_scored"] else 0
    )
    recall = 100 * counts["golden_matched"] / counts["golden_scored"] if counts["golden_scored"] else 0
    harmonic = (
        2 * block["precision"] * block["recall"] / (block["precision"] + block["recall"])
        if block["precision"] + block["recall"]
        else 0
    )
    for name, expected, tolerance, basis in (
        ("precision", precision, 0.1, "matched over scored tool comments"),
        ("recall", recall, 0.1, "matched over scored answer-key comments"),
        ("f_score", harmonic, 0.15, "the harmonic mean of precision and recall"),
    ):
        if abs(expected - block[name]) > tolerance:
            found.append(f"{where}.{name}: {block[name]} is not {basis} ({expected:.1f})")


def _check_run_report(where: str, block: Mapping[str, Any], root: Path | None, found: list[str]) -> None:
    report = _object(f"{where}.run_report", block.get("run_report"), {"url", "sha256"}, found)
    url, digest = report.get("url"), report.get("sha256")
    if not (
        isinstance(url, str)
        and url.startswith(RUN_URL_PREFIX)
        and url.endswith(".json")
        and "/" not in url[len(RUN_URL_PREFIX) :]
    ):
        found.append(f"{where}.run_report.url: must be a .json file directly under {RUN_URL_PREFIX}")
    elif not (isinstance(digest, str) and _SHA64.fullmatch(digest)):
        found.append(f"{where}.run_report.sha256: must be the 64-hex digest of the report")
    elif root is not None:
        local = root / RUNS_RELPATH / url.removeprefix(RUN_URL_PREFIX)
        if not local.is_file():
            found.append(f"{where}.run_report: {local.relative_to(root).as_posix()} is not in the checkout")
        elif hashlib.sha256(local.read_bytes()).hexdigest() != digest:
            found.append(f"{where}.run_report.sha256: does not match {local.relative_to(root).as_posix()}")


def _check_published(
    where: str, kind: str, block: object, data: Mapping[str, Any], root: Path | None, found: list[str]
) -> None:
    fields = PUBLISHED_FIELDS[kind]
    published_block = _object(where, block, fields, found)
    dataset = data["dataset"]
    if (published_block.get("measured_by"), published_block.get("scored_by")) != _OURS:
        found.append(f'{where}: measured_by must be "LemonCrow" and scored_by "LemonCrow\'s judge"')
    if not _date(published_block.get("date"), day=True):
        found.append(f"{where}.date: must be the run's date, YYYY-MM-DD")
    if published_block.get("prs_reviewed") != dataset.get("pr_count"):
        found.append(
            f"{where}.prs_reviewed: {published_block.get('prs_reviewed')!r} is not the dataset's {dataset.get('pr_count')!r} pull "
            "requests; a partial run is not published as the benchmark"
        )
    if published_block.get("answer_key_commit") != dataset.get("answer_key", {}).get("commit"):
        found.append(f"{where}.answer_key_commit: is not the answer key's pinned commit")
    _check_scores(where, published_block, dataset, found)
    _check_run_report(where, published_block, root, found)
    if kind == "lemoncrow":
        review = _object(f"{where}.review", published_block.get("review"), {"model", "route", "depth"}, found)
        engine = _object(f"{where}.engine", published_block.get("engine"), {"version", "commit"}, found)
        cost = _object(
            f"{where}.cost",
            published_block.get("cost"),
            {"median_usd_per_pr", "total_usd", "median_calls_per_pr", "total_calls"},
            found,
        )
        found.extend(f"{where}.review.{n}: required" for n in ("model", "route", "depth") if not _text(review.get(n)))
        if not _text(engine.get("version")):
            found.append(f"{where}.engine.version: required")
        if not (isinstance(engine.get("commit"), str) and _SHA40.fullmatch(engine["commit"])):
            found.append(f"{where}.engine.commit: must be the 40-hex commit that was run")
        found.extend(
            f"{where}.cost.{n}: a number of at least 0 (a missing cost is not zero)"
            for n in ("median_usd_per_pr", "total_usd", "median_calls_per_pr", "total_calls")
            if not (_number(cost.get(n)) and cost[n] >= 0)
        )
    else:
        rows = {row.get("id"): row for row in data["third_party"] if isinstance(row, dict)}
        if published_block.get("tool_id") not in rows:
            found.append(f"{where}.tool_id: must be the id of a third_party row")
        elif published_block.get("tool") != rows[published_block["tool_id"]].get("tool"):
            found.append(f"{where}.tool: must be that row's tool")
        source = _object(
            f"{where}.comments_source",
            published_block.get("comments_source"),
            {"description", "url", "collected_on"},
            found,
        )
        if not (
            _text(source.get("description"))
            and _https(source.get("url"))
            and _date(source.get("collected_on"), day=True)
        ):
            found.append(
                f"{where}.comments_source: a description, an https url and the date the comments were collected"
            )


def _unverified(data: Mapping[str, Any]) -> list[str]:
    """Figures a published table would show that were not read from their source (pending files are exempt)."""

    spots = [("dataset.origin", data["dataset"]["origin"]), ("dataset.answer_key", data["dataset"]["answer_key"])]
    spots.append(("method.source", data["method"]["source"]))
    spots += [(f"third_party {row['id']}", row) for row in data["third_party"]]
    spots += [(f"vendor_claims {claim['id']}", claim) for claim in data["vendor_claims"]]
    return [
        f"{where}: not read from its source (verification.status is not primary_read); open the source, compare it with "
        "the file, then set the status"
        for where, spot in spots
        if spot["verification"]["status"] != "primary_read"
    ]


def problems(data: Mapping[str, Any], root: Path | None = None) -> list[str]:
    """Everything wrong with the published-results file. With *root*, the linked run reports must be there too."""

    found: list[str] = []
    if data.get("schema") != 1:
        found.append("schema must be 1")
    if extra := sorted(set(data) - TOP_LEVEL):
        found.append(f"unknown top-level keys {extra}")
    if missing := sorted(TOP_LEVEL - set(data)):
        return [*found, f"missing top-level keys {missing}"]
    if not _date(data["updated"], day=True):
        found.append("updated: must be YYYY-MM-DD")
    dataset = _object(
        "dataset", data["dataset"], {"name", "pr_count", "languages", "repositories", "origin", "answer_key"}, found
    )
    if dataset:
        _check_dataset(dataset, found)
    method = _object("method", data["method"], {"judge_rule", "low_severity", "metrics", "source"}, found)
    if method:
        _check_method(method, found)
    _check_third_party(data["third_party"], found)
    notes = data["third_party_discrepancies"]
    if not isinstance(notes, list) or not all(_text(n) for n in notes):
        found.append("third_party_discrepancies: a list of sentences")
    _check_vendor_claims(data["vendor_claims"], found)
    if found:  # the blocks below are checked against the dataset and the rows; do not reason from a broken file
        return found

    own = data["lemoncrow"]
    if not isinstance(own, dict) or own.get("status") not in ("pending", "published"):
        found.append('lemoncrow: {"status": "pending"} or a published block')
    elif own["status"] == "pending":
        if set(own) != {"status"}:
            found.append('lemoncrow: a pending block is exactly {"status": "pending"}; no figure may sit beside it')
    else:
        _check_published("lemoncrow", "lemoncrow", own, data, root, found)
        if not found:
            found.extend(_unverified(data))
    rescored = data["competitor_rescored"]
    if rescored is not None:
        if not isinstance(rescored, dict) or rescored.get("status") not in ("pending", "published"):
            found.append('competitor_rescored: null, {"status": "pending"} or a published block')
        elif rescored["status"] == "pending":
            if set(rescored) != {"status"}:
                found.append('competitor_rescored: a pending block is exactly {"status": "pending"}')
        else:
            _check_published("competitor_rescored", "competitor_rescored", rescored, data, root, found)
    return found


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Row:
    label: str
    precision: float
    recall: float
    f_score: float
    cost: str
    scored_by: str
    source_label: str
    source_url: str
    source_date: str
    ours: bool


def published(block: object) -> bool:
    return isinstance(block, dict) and block.get("status") == "published"


def rows(data: Mapping[str, Any]) -> list[Row]:
    """The table's rows, highest F-score first, ours ahead of a tie. Empty until LemonCrow's block is published."""

    own = data["lemoncrow"]
    if not published(own):
        return []
    table = [
        Row(
            "LemonCrow",
            own["precision"],
            own["recall"],
            own["f_score"],
            dollars(own["cost"]["median_usd_per_pr"]),
            own["scored_by"],
            "run report",
            own["run_report"]["url"],
            own["date"],
            True,
        )
    ]
    for row in data["third_party"]:
        scored_by = row["scored_by"] + (", its own tool" if row["self_reported"] else "")
        source = row["source"]
        table.append(
            Row(
                row["tool"],
                row["precision"],
                row["recall"],
                row["f_score"],
                "n/a",
                scored_by,
                "source",
                source["url"],
                source["date"],
                False,
            )
        )
    rescored = data.get("competitor_rescored")
    if published(rescored):
        table.append(
            Row(
                f"{rescored['tool']}, scored by our judge",
                rescored["precision"],
                rescored["recall"],
                rescored["f_score"],
                "n/a",
                rescored["scored_by"],
                "run report",
                rescored["run_report"]["url"],
                rescored["date"],
                False,
            )
        )
    return sorted(table, key=lambda r: (-r.f_score, not r.ours, r.label))


def _link(text: str, url: str) -> str:
    return f"[{text}]({url})"


def _method_paragraph(data: Mapping[str, Any]) -> str:
    dataset, key, method, own = data["dataset"], data["dataset"]["answer_key"], data["method"], data["lemoncrow"]
    origin, first = dataset["origin"], data["third_party"][0]
    names = ", ".join(r["name"] for r in dataset["repositories"])
    reporters = " and ".join(sorted({r["reported_by"] for r in data["third_party"]}))
    parts = [
        f"**Method.** {dataset['pr_count']} pull requests from {names} ({', '.join(dataset['languages'])}), the set "
        f"{origin['publisher']} published ({_link(origin['title'], origin['url'])}, {origin['date']}), scored against the "
        f"answer key {key['publisher']} expanded ({_link('golden_comments @ ' + key['commit'][:7], key['repository_url'] + '/tree/' + key['commit'])}): "
        f"{key['scored_comments']} comments count, after dropping {key['low_severity']} Low-severity ones.",
        f"{method['judge_rule']} {method['low_severity']}",
        f"LemonCrow's row is our own run on {own['date']}: review model {own['review']['model']} through {own['review']['route']} "
        f"at depth {own['review']['depth']}, engine {own['engine']['version']} ({own['engine']['commit'][:7]}), judged by "
        f"{own['judge']['model']} with a prompt whose sha256 starts {own['judge']['prompt_sha256'][:12]}. The {own['prs_reviewed']} "
        f"reviews made {own['cost']['total_calls']:g} model calls and cost {dollars(own['cost']['total_usd'])} in all; the median pull "
        f"request took {own['cost']['median_calls_per_pr']:g} calls. Every comment and judgment is in the "
        f"{_link('raw run report', own['run_report']['url'])}.",
        f"The other rows are the figures {reporters} published ({_link(first['source']['title'], first['source']['url'])}, "
        f"{first['source']['date']}; run {first['run_date']}, default settings, their judge). We did not re-run those tools, so "
        "the columns are not strictly like for like.",
    ]
    rescored = data.get("competitor_rescored")
    if published(rescored):
        parts.append(
            f"The row marked \"scored by our judge\" scores {rescored['tool']}'s own comments ({rescored['comments_source']['description']}) "
            "on the same pull requests with the judge LemonCrow used, so it shows how far the two judges differ."
        )
    for claim in data["vendor_claims"]:
        verdict = "" if claim["comparable"] else f". {claim['comparable_note']}"
        sentence = (
            f"{claim['reported_by']} reports {claim['claim']} on this set ({_link(claim['source']['title'], claim['source']['url'])}, "
            f"{claim['source']['date']}): {claim['measures']}{verdict}"
        )
        parts.append(sentence if sentence.endswith(".") else sentence + ".")
    return " ".join(parts)


def render_section(data: Mapping[str, Any]) -> str:
    """The README block: a heading, the table and one method paragraph. Empty while LemonCrow is pending."""

    table = rows(data)
    if not table:
        return ""
    lines = [
        f"### Code-review benchmark\n\nF-score on the public {data['dataset']['pr_count']}-PR code-review benchmark, highest first. "
        "Rows scored by LemonCrow's judge are our own runs; every other row is a figure its source published. Each row links its source.\n",
        "| Tool | Precision | Recall | F-score | Median cost / PR | Scored by |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in table:
        cells = [row.label, percent(row.precision), percent(row.recall), percent(row.f_score), row.cost]
        if row.ours:
            cells = [f"**{cell}**" for cell in cells]
        lines.append(
            "| "
            + " | ".join(cells)
            + f" | {row.scored_by} ({_link(row.source_label, row.source_url)}, {row.source_date}) |"
        )
    return "\n".join([*lines, "", _method_paragraph(data)]) + "\n"


def splice(readme: str, section: str) -> str:
    """*readme* with the text between the two markers replaced by *section* (by nothing, when it is empty)."""

    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError(f"needs exactly one {START} and one {END}")
    head, rest = readme.split(START, 1)
    if END not in rest:
        raise ValueError(f"{START} must come before {END}")
    tail = rest.split(END, 1)[1]
    return f"{head}{START}\n{END}{tail}" if not section else f"{head}{START}\n\n{section}\n{END}{tail}"


def dump(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def landing_copy(data: Mapping[str, Any]) -> str:
    return dump({"_source": LANDING_SOURCE_NOTE, **data})


# --------------------------------------------------------------------------- #
# Merging a run                                                                #
# --------------------------------------------------------------------------- #


def merge_run(
    data: Mapping[str, Any], run_path: Path, rescored: str | None, root: Path
) -> tuple[dict[str, Any], Path, bytes]:
    """The data with the run merged in, where the raw report will be copied, and its bytes. Writes nothing.

    Raises ``ValueError`` for a run without a complete ``benchmark_summary``.
    """

    raw = run_path.read_bytes()
    try:
        report = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{run_path} is not JSON: {error}") from error
    summary = report.get("benchmark_summary") if isinstance(report, dict) else None
    if not isinstance(summary, dict):
        raise ValueError(f"{run_path} has no benchmark_summary object (see evals/review/published/README.md)")
    kind = "competitor_rescored" if rescored else "lemoncrow"
    wanted = SUMMARY_FIELDS[kind]
    if set(summary) != wanted:
        raise ValueError(
            f"benchmark_summary has the wrong fields: missing {sorted(wanted - set(summary))}, unknown {sorted(set(summary) - wanted)}"
        )
    if rescored and summary["tool_id"] != rescored:
        raise ValueError(f"benchmark_summary.tool_id {summary['tool_id']!r} is not --rescored {rescored!r}")
    name = f"{'rescored-' + rescored if rescored else 'lemoncrow'}-{summary['date']}.json"
    if not (_date(summary["date"], day=True) and re.fullmatch(r"[a-z0-9-]+\.json", name)):
        raise ValueError(f"benchmark_summary.date {summary['date']!r} is not YYYY-MM-DD")
    target = root / RUNS_RELPATH / name
    if target.exists() and target.read_bytes() != raw:
        raise ValueError(
            f"{target.relative_to(root).as_posix()} already exists with other contents; a published run is never overwritten"
        )
    block = {"status": "published", "measured_by": _OURS[0], "scored_by": _OURS[1], **summary}
    block["run_report"] = {"url": RUN_URL_PREFIX + name, "sha256": hashlib.sha256(raw).hexdigest()}
    return {**data, kind: block, "updated": summary["date"]}, target, raw


# --------------------------------------------------------------------------- #
# Command line                                                                 #
# --------------------------------------------------------------------------- #


def load(root: Path = ROOT) -> dict[str, Any]:
    return json.loads((root / DATA_RELPATH).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def check(root: Path = ROOT, landing: Path | None = None) -> list[str]:
    """Problems with the file, and every place that no longer matches it. Empty when all is in step."""

    data = load(root)
    if found := problems(data, root):
        return found
    readme = (root / README_RELPATH).read_text(encoding="utf-8")
    try:
        if splice(readme, render_section(data)) != readme:
            found.append(f"{README_RELPATH}: the benchmark section is stale; run `make benchmark-publish`")
    except ValueError as error:
        found.append(f"{README_RELPATH}: {error}")
    if landing is not None:
        target = landing / LANDING_RELPATH
        if not target.is_file() or target.read_text(encoding="utf-8") != landing_copy(data):
            found.append(f"{target}: is not the current copy of the published file; run `make benchmark-publish`")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the code-review benchmark table into the README and the landing site."
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root (tests use a copy)")
    parser.add_argument(
        "--check", action="store_true", help="write nothing; exit 1 if the file is invalid or a copy is stale"
    )
    parser.add_argument("--landing", type=Path, help="the landing checkout whose data copy is written (or checked)")
    parser.add_argument(
        "--from-run", type=Path, help="a run report with a benchmark_summary, merged into the published file first"
    )
    parser.add_argument(
        "--rescored", metavar="TOOL_ID", help="with --from-run: the run scored this tool's own comments"
    )
    args = parser.parse_args(argv)
    root: Path = args.root

    if args.landing is not None and not (args.landing / LANDING_RELPATH).parent.is_dir():
        print(f"error: {args.landing} is not a landing checkout (no src/data)", file=sys.stderr)
        return 1
    if args.from_run is not None and args.check:
        print("error: --from-run writes; it cannot be combined with --check", file=sys.stderr)
        return 1
    if args.rescored and args.from_run is None:
        print("error: --rescored needs --from-run", file=sys.stderr)
        return 1

    data = load(root)
    copy_to: tuple[Path, bytes] | None = None
    if args.from_run is not None:
        try:
            data, target, raw = merge_run(data, args.from_run, args.rescored, root)
        except (ValueError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        copy_to = (target, raw)
    # A merged run's report is not in the checkout yet; its link and digest are checked once it has been copied.
    if found := problems(data, None if copy_to else root):
        print("the published results cannot be published:", file=sys.stderr)
        print("\n".join(f"  - {line}" for line in found), file=sys.stderr)
        return 1
    if args.check:
        stale = check(root, args.landing)
        if stale:
            print("\n".join(f"  - {line}" for line in stale), file=sys.stderr)
        return 1 if stale else 0

    if copy_to is not None:
        copy_to[0].parent.mkdir(parents=True, exist_ok=True)
        copy_to[0].write_bytes(copy_to[1])
        (root / DATA_RELPATH).write_text(dump(data), encoding="utf-8")
    readme = root / README_RELPATH
    try:
        readme.write_text(splice(readme.read_text(encoding="utf-8"), render_section(data)), encoding="utf-8")
    except ValueError as error:
        print(f"error: {README_RELPATH}: {error}", file=sys.stderr)
        return 1
    ready = published(data["lemoncrow"])
    print(f"{README_RELPATH}: benchmark section {'written' if ready else 'left empty (LemonCrow pending)'}")
    if args.landing is not None:
        target_file = args.landing / LANDING_RELPATH
        target_file.write_text(landing_copy(data), encoding="utf-8")
        print(f"{target_file}: written; the landing site renders {'the table' if ready else 'nothing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
