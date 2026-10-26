"""The published code-review benchmark table: one JSON file, rendered into the README and the landing site.

The committed file is the real state (LemonCrow pending, so nothing is published). Every published-state test builds a
clearly FAKE LemonCrow block in a temporary copy of the repository; no fake figure is ever written into the checkout.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from scripts import render_code_review_benchmark as bench

ROOT = Path(__file__).resolve().parents[2]
FAKE_DIGEST = "f" * 64
FAKE_COMMIT = "a" * 40

#: The figures as the source published them, in the file's order (precision, recall, F-score). The tools are Augment
#: Code Review, Cursor Bugbot, the vendor of the benchmark's pull-request set, Codex Code Review, CodeRabbit, Claude Code
#: and GitHub Copilot. A change to the file must change this too. (Tests do not spell the vendor's name: a guard forbids it.)
PUBLISHED_FIGURES = [(65, 55, 59), (60, 41, 49), (45, 45, 45), (68, 29, 41), (36, 43, 39), (23, 51, 31), (20, 34, 25)]
TOOLS = [
    "Augment Code Review",
    "Cursor Bugbot",
    None,
    "Codex Code Review",
    "CodeRabbit",
    "Claude Code",
    "GitHub Copilot",
]


def fake_summary(**overrides: Any) -> dict[str, Any]:
    """A FAKE run summary that adds up: 52 of 80 comments matched (65.0%), 50 of 97 answer-key comments (51.5%)."""

    summary: dict[str, Any] = {
        "date": "2031-01-02",
        "prs_reviewed": 50,
        "answer_key_commit": load()["dataset"]["answer_key"]["commit"],
        "precision": 65.0,
        "recall": 51.5,
        "f_score": 57.5,
        "counts": {"tool_comments_scored": 80, "tool_comments_matched": 52, "golden_scored": 97, "golden_matched": 50},
        "judge": {"model": "FAKE-judge-model", "prompt_sha256": FAKE_DIGEST},
        "review": {"model": "FAKE-review-model", "route": "FAKE-route", "depth": "auto"},
        "engine": {"version": "0.0.0-fake", "commit": FAKE_COMMIT},
        "cost": {"median_usd_per_pr": 0.125, "total_usd": 9.99, "median_calls_per_pr": 4, "total_calls": 321},
    }
    summary.update(overrides)
    return summary


def fake_rescored_summary(**overrides: Any) -> dict[str, Any]:
    summary = fake_summary()
    for name in ("review", "engine", "cost"):
        del summary[name]
    summary.update(
        {
            "tool_id": RESCORED_ID,
            "tool": RESCORED_TOOL,
            "comments_source": {
                "description": "FAKE comments",
                "url": "https://example.invalid/fake",
                "collected_on": "2031-01-01",
            },
            "precision": 40.0,
            "recall": 30.9,
            "f_score": 34.9,
            "counts": {
                "tool_comments_scored": 50,
                "tool_comments_matched": 20,
                "golden_scored": 97,
                "golden_matched": 30,
            },
        }
    )
    summary.update(overrides)
    return summary


def load() -> dict[str, Any]:
    return bench.load(ROOT)


#: The tool whose own comments the optional rescored row scores: the third row of the file.
RESCORED_ID = load()["third_party"][2]["id"]
RESCORED_TOOL = load()["third_party"][2]["tool"]


def make_root(tmp_path: Path) -> Path:
    """A temporary repository with the real published file, the real README and the real docs the tests read."""

    for relative in (bench.DATA_RELPATH, bench.README_RELPATH):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


def make_landing(tmp_path: Path) -> Path:
    (tmp_path / "landing/src/data").mkdir(parents=True)
    return tmp_path / "landing"


def run_file(tmp_path: Path, summary: dict[str, Any], name: str = "fake-run.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"benchmark_summary": summary, "reviews": ["FAKE raw report body"]}), encoding="utf-8")
    return path


def verified(data: dict[str, Any]) -> dict[str, Any]:
    """*data* with every source marked as read, as it must be before anything is published."""

    data = copy.deepcopy(data)
    spots = [data["dataset"]["origin"], data["dataset"]["answer_key"], data["method"]["source"]]
    for spot in (*spots, *data["third_party"], *data["vendor_claims"]):
        spot["verification"]["status"] = "primary_read"
    return data


def published_root(tmp_path: Path, *, rescored: bool = False) -> tuple[Path, dict[str, Any]]:
    """A temporary repository whose file has FAKE LemonCrow figures merged in through the real importer."""

    root = make_root(tmp_path / "repo")
    data = verified(bench.load(root))
    (root / bench.DATA_RELPATH).write_text(bench.dump(data), encoding="utf-8")
    assert bench.main(["--root", str(root), "--from-run", str(run_file(tmp_path, fake_summary()))]) == 0
    if rescored:
        report = run_file(tmp_path, fake_rescored_summary(), "fake-rescored.json")
        assert bench.main(["--root", str(root), "--from-run", str(report), "--rescored", RESCORED_ID]) == 0
    return root, bench.load(root)


# --------------------------------------------------------------------------- #
# The committed state                                                          #
# --------------------------------------------------------------------------- #


def test_the_committed_file_is_valid_and_the_readme_is_in_step_with_it() -> None:
    assert bench.problems(load(), ROOT) == []
    assert bench.check(ROOT) == []
    assert bench.main(["--check"]) == 0


def test_while_lemoncrow_is_pending_nothing_is_rendered_and_the_markers_enclose_nothing() -> None:
    data = load()
    assert data["lemoncrow"] == {"status": "pending"}
    assert data["competitor_rescored"] is None
    assert bench.rows(data) == []
    assert bench.render_section(data) == ""
    readme = (ROOT / bench.README_RELPATH).read_text(encoding="utf-8")
    assert readme.count(bench.START) == readme.count(bench.END) == 1
    assert readme.split(bench.START, 1)[1].split(bench.END, 1)[0].strip() == ""
    assert f"{bench.START}\n{bench.END}" in readme  # not even a blank line, so nothing renders
    # No competitor table without us: not one third-party figure reaches the README while we are pending.
    assert "### Code-review benchmark" not in readme
    for tool in data["third_party"]:
        assert tool["source"]["url"] not in readme
        assert f"| {tool['tool']} |" not in readme


def test_a_competitor_figure_alone_is_never_rendered_even_with_a_rescored_block() -> None:
    data = load()
    data["competitor_rescored"] = {"status": "pending"}
    assert bench.problems(data, ROOT) == []
    assert bench.render_section(data) == ""
    # A published rescored block beside a pending LemonCrow block still renders nothing.
    data["competitor_rescored"] = {"status": "published", **fake_rescored_summary()}
    assert bench.render_section(data) == ""


def test_the_third_party_figures_are_the_ones_the_source_published_and_every_row_is_attributed() -> None:
    data = load()
    figures = [(row["precision"], row["recall"], row["f_score"]) for row in data["third_party"]]
    assert figures == PUBLISHED_FIGURES
    assert [row["tool"] for i, row in enumerate(data["third_party"]) if TOOLS[i]] == [t for t in TOOLS if t]
    for row in data["third_party"]:
        assert row["reported_by"] == "Augment Code"
        assert row["source"]["url"].startswith("https://www.augmentcode.com/blog/")
        assert row["source"]["date"] == "2025-12-11"
        assert row["run_date"] == "2025-11-14"
        assert row["settings"] == "default"
    assert [row["tool"] for row in data["third_party"] if row["self_reported"]] == ["Augment Code Review"]
    # Their own claim is recorded, and marked as not comparable.
    (claim,) = data["vendor_claims"]
    assert (claim["value"], claim["comparable"]) == (82, False)


def test_the_dataset_and_the_method_are_the_answer_keys() -> None:
    data = load()
    dataset = data["dataset"]
    assert dataset["pr_count"] == 50
    assert dataset["languages"] == ["Python", "TypeScript", "Go", "Java", "Ruby"]
    key = dataset["answer_key"]
    assert key["repository_url"] == "https://github.com/ai-code-review-evaluations/golden_comments"
    assert re.fullmatch(r"[0-9a-f]{40}", key["commit"])
    assert (key["golden_comments"], key["low_severity"], key["scored_comments"]) == (137, 40, 97)
    assert "Low-severity" in data["method"]["low_severity"]
    assert "same underlying issue" in data["method"]["judge_rule"]


def test_the_schema_readme_documents_every_field_the_validator_knows() -> None:
    text = (ROOT / "evals/review/published/README.md").read_text(encoding="utf-8")
    names = set(bench.TOP_LEVEL) | set(bench.COUNT_FIELDS)
    for fields in bench.PUBLISHED_FIELDS.values():
        names |= set(fields)
    # Every field of a third-party row, a claim and the answer key too.
    data = load()
    names |= (
        set(data["third_party"][0])
        | set(data["vendor_claims"][0])
        | set(data["dataset"]["answer_key"])
        | set(data["method"])
    )
    names |= {
        "median_usd_per_pr",
        "total_usd",
        "median_calls_per_pr",
        "total_calls",
        "prompt_sha256",
        "benchmark_summary",
    }
    missing = sorted(name for name in names if f"`{name}`" not in text and f"`{name}[]`" not in text)
    assert missing == []


def test_make_has_the_publish_target_and_docs_check_runs_this_file() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^benchmark-publish:", makefile, re.M)
    docs_check = makefile.split("docs-check:", 1)[1].split("\n", 2)[1]
    assert "tests/gateway/test_code_review_benchmark.py" in docs_check


# --------------------------------------------------------------------------- #
# Numbers and the README markers                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "shown"), [(59.5, "60%"), (59.4, "59%"), (0.0, "0%"), (100, "100%"), (44.5, "45%"), (61.0, "61%")]
)
def test_percent_rounds_half_up(value: float, shown: str) -> None:
    assert bench.percent(value) == shown


@pytest.mark.parametrize(
    ("value", "shown"), [(1.005, "$1.01"), (0.125, "$0.13"), (0.1, "$0.10"), (12, "$12.00"), (0.004, "$0.00")]
)
def test_dollars_round_half_up_to_the_cent(value: float, shown: str) -> None:
    assert bench.dollars(value) == shown


def test_splice_replaces_only_what_is_between_the_markers_and_empties_cleanly() -> None:
    readme = f"before\n{bench.START}\n{bench.END}\nafter\n"
    filled = bench.splice(readme, "SECTION\n")
    assert filled == f"before\n{bench.START}\n\nSECTION\n\n{bench.END}\nafter\n"
    assert bench.splice(filled, "") == readme
    assert bench.splice(filled, "OTHER\n").count("SECTION") == 0
    for broken in (
        "no markers",
        f"{bench.START}\n",
        f"{bench.END}\n{bench.START}\n",
        f"{bench.START}\n{bench.END}\n{bench.START}\n{bench.END}\n",
    ):
        with pytest.raises(ValueError):
            bench.splice(broken, "x")


# --------------------------------------------------------------------------- #
# The published state, with FAKE figures                                       #
# --------------------------------------------------------------------------- #


def test_a_published_run_renders_a_sorted_table_with_lemoncrow_highlighted_and_every_figure_sourced(
    tmp_path: Path,
) -> None:
    root, data = published_root(tmp_path)
    section = bench.render_section(data)
    table = [line for line in section.splitlines() if line.startswith("|")][2:]
    labels = [line.split("|")[1].strip() for line in table]
    # Highest F-score first: Augment 59, then LemonCrow 58 (FAKE: 57.5), then the published rows in order.
    assert labels == [
        "Augment Code Review",
        "**LemonCrow**",
        "Cursor Bugbot",
        RESCORED_TOOL,
        "Codex Code Review",
        "CodeRabbit",
        "Claude Code",
        "GitHub Copilot",
    ]
    assert table[1] == (
        "| **LemonCrow** | **65%** | **52%** | **58%** | **$0.13** | LemonCrow's judge "
        f"([run report]({data['lemoncrow']['run_report']['url']}), 2031-01-02) |"
    )
    assert "| Augment Code Review | 65% | 55% | 59% | n/a | Augment's judge, its own tool ([source](" in table[0]
    assert (
        f"| {RESCORED_TOOL} | 45% | 45% | 45% | n/a | Augment's judge ([source](https://www.augmentcode.com/blog/"
        in table[3]
    )
    for line in table:  # every row has a link and a date
        assert re.search(r"\]\(https://[^)]+\), \d{4}-\d{2}-\d{2}\) \|$", line), line
    assert "### Code-review benchmark" in section
    # One method paragraph, with the links the reader needs and the facts of the run.
    paragraph = section.rstrip("\n").split("\n\n")[-1]
    assert paragraph.startswith("**Method.**") and "\n" not in paragraph
    for fragment in (
        "golden_comments @ 3f2c8ab",
        "https://github.com/ai-code-review-evaluations/golden_comments/tree/" + data["dataset"]["answer_key"]["commit"],
        "97 comments count, after dropping 40 Low-severity ones",
        "same underlying issue",
        "FAKE-review-model through FAKE-route at depth auto",
        "engine 0.0.0-fake (aaaaaaa)",
        "FAKE-judge-model with a prompt whose sha256 starts ffffffffffff",
        "321 model calls and cost $9.99 in all",
        "[raw run report](" + data["lemoncrow"]["run_report"]["url"] + ")",
        "We did not re-run those tools",
        "reports an 82% catch rate (41 of 50 pull requests)",
        "it cannot be compared with precision-aware F-scores.",
    ):
        assert fragment in paragraph, fragment
    readme = (root / bench.README_RELPATH).read_text(encoding="utf-8")
    assert readme.count(bench.START) == 1 and section in readme
    assert bench.check(root) == []


def test_a_rescored_row_is_labelled_as_our_judge_and_sorted_with_the_rest(tmp_path: Path) -> None:
    _, data = published_root(tmp_path, rescored=True)
    labels = [row.label for row in bench.rows(data)]
    ours = f"{RESCORED_TOOL}, scored by our judge"
    assert ours in labels
    assert labels.index(ours) > labels.index(RESCORED_TOOL)  # 35% sorts below the tool's published 45%
    section = bench.render_section(data)
    assert f"| {ours} | 40% | 31% | 35% | n/a | LemonCrow's judge ([run report](" in section
    assert f'The row marked "scored by our judge" scores {RESCORED_TOOL}\'s own comments (FAKE comments)' in section


def test_ties_put_lemoncrow_first_and_the_order_is_by_f_score_not_by_precision() -> None:
    data = verified(load())
    data["lemoncrow"] = {
        "status": "published",
        "measured_by": "LemonCrow",
        "scored_by": "LemonCrow's judge",
        "run_report": {"url": "u", "sha256": "s"},
    }
    data["lemoncrow"].update(fake_summary(precision=45.0, recall=45.0, f_score=45.0))
    assert [r.label for r in bench.rows(data)][:4] == [
        "Augment Code Review",
        "Cursor Bugbot",
        "LemonCrow",
        RESCORED_TOOL,
    ]


def test_the_landing_copy_is_the_file_plus_a_source_note_and_is_checked(tmp_path: Path) -> None:
    root, data = published_root(tmp_path)
    landing = make_landing(tmp_path)
    assert bench.main(["--root", str(root), "--landing", str(landing)]) == 0
    copied = json.loads((landing / bench.LANDING_RELPATH).read_text(encoding="utf-8"))
    assert copied.pop("_source") == bench.LANDING_SOURCE_NOTE
    assert copied == data
    assert bench.check(root, landing) == []
    (landing / bench.LANDING_RELPATH).write_text("{}", encoding="utf-8")
    assert any("not the current copy" in line for line in bench.check(root, landing))
    assert bench.main(["--root", str(root), "--landing", str(landing), "--check"]) == 1
    # A directory that is not a landing checkout is refused, not created.
    assert bench.main(["--root", str(root), "--landing", str(tmp_path / "elsewhere")]) == 1


def test_a_pending_file_renders_an_empty_readme_section_and_a_pending_landing_copy(tmp_path: Path) -> None:
    root = make_root(tmp_path / "repo")
    landing = make_landing(tmp_path)
    before = (root / bench.README_RELPATH).read_text(encoding="utf-8")
    assert bench.main(["--root", str(root), "--landing", str(landing)]) == 0
    assert (root / bench.README_RELPATH).read_text(encoding="utf-8") == before
    assert json.loads((landing / bench.LANDING_RELPATH).read_text(encoding="utf-8"))["lemoncrow"] == {
        "status": "pending"
    }


def test_a_stale_readme_is_reported_and_the_command_repairs_it(tmp_path: Path) -> None:
    root, _ = published_root(tmp_path)
    readme = root / bench.README_RELPATH
    readme.write_text(readme.read_text(encoding="utf-8").replace("**65%**", "**99%**"), encoding="utf-8")
    assert any("stale" in line for line in bench.check(root))
    assert bench.main(["--root", str(root), "--check"]) == 1
    assert bench.main(["--root", str(root)]) == 0
    assert bench.check(root) == []


# --------------------------------------------------------------------------- #
# Merging a run                                                                #
# --------------------------------------------------------------------------- #


def test_merging_a_run_copies_the_raw_report_links_it_and_fingerprints_it(tmp_path: Path) -> None:
    root, data = published_root(tmp_path)
    block = data["lemoncrow"]
    copied = root / bench.RUNS_RELPATH / "lemoncrow-2031-01-02.json"
    assert copied.is_file()
    assert block["run_report"] == {
        "url": bench.RUN_URL_PREFIX + "lemoncrow-2031-01-02.json",
        "sha256": hashlib.sha256(copied.read_bytes()).hexdigest(),
    }
    assert json.loads(copied.read_text(encoding="utf-8"))["reviews"] == ["FAKE raw report body"]
    assert data["updated"] == "2031-01-02"
    assert (block["measured_by"], block["scored_by"], block["status"]) == (
        "LemonCrow",
        "LemonCrow's judge",
        "published",
    )
    # The same run again is idempotent; the same date with other contents is refused.
    again = run_file(tmp_path, fake_summary(), "again.json")
    assert bench.main(["--root", str(root), "--from-run", str(again)]) == 0
    other = run_file(tmp_path, fake_summary(), "other.json")
    other.write_text(other.read_text(encoding="utf-8").replace("FAKE raw", "CHANGED raw"), encoding="utf-8")
    assert bench.main(["--root", str(root), "--from-run", str(other)]) == 1


def test_a_bad_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path / "repo")
    data = verified(bench.load(root))
    (root / bench.DATA_RELPATH).write_text(bench.dump(data), encoding="utf-8")
    before = {p: p.read_bytes() for p in (root / bench.DATA_RELPATH, root / bench.README_RELPATH)}
    bad = {
        "an unknown field": fake_summary(extra=1),
        "a missing field": {k: v for k, v in fake_summary().items() if k != "cost"},
        "a partial run": fake_summary(prs_reviewed=49),
        "another answer key": fake_summary(answer_key_commit="b" * 40),
        "a precision the counts do not give": fake_summary(precision=70.0),
        "an F-score that is not the harmonic mean": fake_summary(f_score=60.0),
        "a recall the counts do not give": fake_summary(recall=10.0),
        "counts against another answer key size": fake_summary(
            counts={"tool_comments_scored": 80, "tool_comments_matched": 52, "golden_scored": 100, "golden_matched": 50}
        ),
        "a judge without a digest": fake_summary(judge={"model": "FAKE-judge-model", "prompt_sha256": "abc"}),
        "a cost that is not a number": fake_summary(
            cost={"median_usd_per_pr": None, "total_usd": 1, "median_calls_per_pr": 1, "total_calls": 1}
        ),
        "no engine commit": fake_summary(engine={"version": "0.0.0-fake", "commit": "abc"}),
        "a bad date": fake_summary(date="tomorrow"),
    }
    for reason, summary in bad.items():
        assert bench.main(["--root", str(root), "--from-run", str(run_file(tmp_path, summary))]) == 1, reason
    assert not (root / bench.RUNS_RELPATH).exists()
    assert {p: p.read_bytes() for p in before} == before
    capsys.readouterr()
    # No summary at all, and a file that is not JSON.
    plain = tmp_path / "plain.json"
    plain.write_text('{"reviews": []}', encoding="utf-8")
    assert bench.main(["--root", str(root), "--from-run", str(plain)]) == 1
    assert "no benchmark_summary" in capsys.readouterr().err
    plain.write_text("not json", encoding="utf-8")
    assert bench.main(["--root", str(root), "--from-run", str(plain)]) == 1
    assert "is not JSON" in capsys.readouterr().err


def test_a_run_with_unread_sources_is_refused_until_they_are_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_root(tmp_path / "repo")  # the committed file: every third-party row is search_retrieval
    before = (root / bench.README_RELPATH).read_bytes()
    assert bench.main(["--root", str(root), "--from-run", str(run_file(tmp_path, fake_summary()))]) == 1
    err = capsys.readouterr().err
    assert "not read from its source" in err and "third_party augment" in err and "dataset.origin" in err
    assert (root / bench.README_RELPATH).read_bytes() == before
    assert not (root / bench.RUNS_RELPATH).exists()


def test_a_rescored_run_must_name_a_listed_tool(tmp_path: Path) -> None:
    root = make_root(tmp_path / "repo")
    data = verified(bench.load(root))
    (root / bench.DATA_RELPATH).write_text(bench.dump(data), encoding="utf-8")
    assert bench.main(["--root", str(root), "--from-run", str(run_file(tmp_path, fake_summary()))]) == 0
    unknown = run_file(tmp_path, fake_rescored_summary(tool_id="nobody", tool="Nobody"), "unknown.json")
    assert bench.main(["--root", str(root), "--from-run", str(unknown), "--rescored", "nobody"]) == 1
    mismatch = run_file(tmp_path, fake_rescored_summary(), "mismatch.json")
    assert bench.main(["--root", str(root), "--from-run", str(mismatch), "--rescored", "codex"]) == 1
    assert bench.main(["--root", str(root), "--rescored", RESCORED_ID]) == 1  # --rescored needs a run
    ok = run_file(tmp_path, fake_rescored_summary(), "ok.json")
    assert bench.main(["--root", str(root), "--from-run", str(ok), "--rescored", RESCORED_ID]) == 0
    assert (root / bench.RUNS_RELPATH / f"rescored-{RESCORED_ID}-2031-01-02.json").is_file()


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #


def test_a_lemoncrow_figure_cannot_sit_beside_a_pending_status() -> None:
    data = load()
    data["lemoncrow"] = {"status": "pending", "f_score": 61.0}
    assert any("no figure may sit beside it" in line for line in bench.problems(data))


def test_a_published_block_needs_its_run_report_in_the_checkout_with_the_same_digest(tmp_path: Path) -> None:
    root, data = published_root(tmp_path)
    assert bench.problems(data, root) == []
    report = root / bench.RUNS_RELPATH / "lemoncrow-2031-01-02.json"
    report.write_text("{}", encoding="utf-8")
    assert any("does not match" in line for line in bench.problems(data, root))
    report.unlink()
    assert any("is not in the checkout" in line for line in bench.problems(data, root))
    for url in (
        "https://example.com/report.json",
        bench.RUN_URL_PREFIX + "sub/dir.json",
        bench.RUN_URL_PREFIX + "report.txt",
        "report.json",
    ):
        broken = copy.deepcopy(data)
        broken["lemoncrow"]["run_report"]["url"] = url
        assert any("run_report.url" in line for line in bench.problems(broken)), url


def test_a_published_block_cannot_leave_out_or_zero_a_required_field(tmp_path: Path) -> None:
    _, data = published_root(tmp_path)
    for path in (
        ("cost", "total_usd"),
        ("review", "model"),
        ("engine", "commit"),
        ("judge", "prompt_sha256"),
        ("run_report", "sha256"),
        ("counts", "golden_scored"),
    ):
        broken = copy.deepcopy(data)
        del broken["lemoncrow"][path[0]][path[1]]
        assert bench.problems(broken), path
    for name in ("review", "engine", "cost", "judge", "counts", "run_report", "precision", "date"):
        broken = copy.deepcopy(data)
        del broken["lemoncrow"][name]
        assert any(name in line for line in bench.problems(broken)), name


def test_third_party_rows_need_a_source_a_date_and_a_reporter_and_must_add_up() -> None:
    for mutate, needle in (
        (lambda d: d["third_party"][0]["source"].pop("url"), "url"),
        (lambda d: d["third_party"][0]["source"].update(date="December"), "date"),
        (lambda d: d["third_party"][0].pop("reported_by"), "reported_by"),
        (lambda d: d["third_party"][0].update(f_score=10), "harmonic mean"),
        (lambda d: d["third_party"][0].update(precision=101), "0 to 100"),
        (lambda d: d["third_party"][1].update(id=d["third_party"][0]["id"]), "duplicate id"),
        (lambda d: d["third_party"][0]["verification"].update(status="guessed"), "verification.status"),
        (lambda d: d["dataset"]["answer_key"].update(commit="main"), "40-hex"),
        (lambda d: d["dataset"].update(pr_count=49), "sum of the repositories"),
        (lambda d: d.update(unexpected=1), "unknown top-level keys"),
    ):
        data = load()
        mutate(data)
        assert any(needle in line for line in bench.problems(data)), needle


def test_unread_sources_do_not_block_a_pending_file_but_do_block_a_published_one(tmp_path: Path) -> None:
    data = load()
    assert all(row["verification"]["status"] == "search_retrieval" for row in data["third_party"])
    assert bench.problems(data, ROOT) == []
    _, published = published_root(tmp_path)
    assert bench.problems(published) == []
    unread = copy.deepcopy(published)
    unread["third_party"][2]["verification"]["status"] = "search_retrieval"
    unread["vendor_claims"][0]["verification"]["status"] = "search_retrieval"
    found = bench.problems(unread)
    assert [line.split(":")[0] for line in found] == [
        f"third_party {RESCORED_ID}",
        f"vendor_claims {unread['vendor_claims'][0]['id']}",
    ]


def test_the_importer_adds_nothing_the_summary_did_not_carry(tmp_path: Path) -> None:
    _, data = published_root(tmp_path)
    summary = fake_summary()
    block = data["lemoncrow"]
    assert {k: v for k, v in block.items() if k not in {"status", "measured_by", "scored_by", "run_report"}} == summary
    assert set(bench.SUMMARY_FIELDS["lemoncrow"]) == set(summary)
    assert set(bench.SUMMARY_FIELDS["competitor_rescored"]) == set(fake_rescored_summary())
