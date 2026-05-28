"""Tests for ab.pr_replay — PR-01 through PR-06."""

import json
import tempfile
from pathlib import Path

from ab.pr_replay import (
    JUDGE_MODEL,
    fetch_pr_metadata,
    grade_diff_quality,
    parse_pr_url,
    print_pr_comparison_table,
    run_pr_arm,
    score_diff,
)

_SAMPLE_PR_URL = "https://github.com/owner/myrepo/pull/42"

_SAMPLE_GH_RESPONSE = json.dumps(
    {
        "title": "Fix auth bug",
        "body": "Fixes authentication bypass in login flow.",
        "base": {"sha": "abc1234"},
        "head": {"sha": "def5678"},
        "merged_at": "2024-05-28T10:00:00Z",
    }
)

_SAMPLE_DIFF = """\
diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -10,6 +10,7 @@ def login(user, password):
     if not verify(password):
+        raise AuthError("bad password")
         return None
"""


def _mock_gh(responses: dict[str, str]):
    call_count = [0]

    def gh(args: list[str]) -> str:
        idx = call_count[0]
        call_count[0] += 1
        keys = list(responses.keys())
        return responses.get(keys[min(idx, len(keys) - 1)], "")

    return gh


# --------------------------------------------------------------------------- #
# PR-01: URL parsing and metadata fetch                                        #
# --------------------------------------------------------------------------- #


def test_parse_pr_url_extracts_fields():
    """PR-01: parse owner/repo/number from URL."""
    owner, repo, number = parse_pr_url(_SAMPLE_PR_URL)
    assert owner == "owner"
    assert repo == "myrepo"
    assert number == 42


def test_parse_pr_url_raises_for_invalid():
    try:
        parse_pr_url("https://gitlab.com/owner/repo/merge_requests/1")
        raise AssertionError("should have raised")
    except ValueError:
        pass


def test_fetch_pr_metadata_fields():
    """PR-01: fetch returns title, body, base_sha, head_sha, diff."""
    gh = _mock_gh({"api": _SAMPLE_GH_RESPONSE, "diff": _SAMPLE_DIFF})
    meta = fetch_pr_metadata(_SAMPLE_PR_URL, _gh_run=gh)
    assert meta["title"] == "Fix auth bug"
    assert meta["base_sha"] == "abc1234"
    assert meta["head_sha"] == "def5678"
    assert meta["repo"] == "owner/myrepo"
    assert meta["pr_number"] == 42


# --------------------------------------------------------------------------- #
# PR-03: diff scoring                                                          #
# --------------------------------------------------------------------------- #


def test_score_diff_identical_is_1():
    """PR-03: identical diffs → sequence_ratio=1.0, file_overlap=1.0."""
    result = score_diff(_SAMPLE_DIFF, _SAMPLE_DIFF)
    assert result["sequence_ratio"] == 1.0
    assert result["file_overlap"] == 1.0


def test_score_diff_empty_generated():
    """PR-03: empty generated diff → low scores."""
    result = score_diff("", _SAMPLE_DIFF)
    assert result["sequence_ratio"] < 0.1
    assert result["file_overlap"] == 0.0


def test_score_diff_file_overlap_partial():
    """PR-03: file overlap = intersection / ref_files."""
    gen = "diff --git a/src/auth.py b/src/auth.py\n+fix"
    ref = "diff --git a/src/auth.py b/src/auth.py\n+fix\ndiff --git a/src/utils.py b/src/utils.py\n+util"
    result = score_diff(gen, ref)
    assert result["file_overlap"] == 0.5


def test_score_diff_returns_required_fields():
    result = score_diff("", "")
    for field in ("sequence_ratio", "file_overlap", "gen_files", "ref_files"):
        assert field in result


# --------------------------------------------------------------------------- #
# PR-04: LLM-as-judge                                                         #
# --------------------------------------------------------------------------- #


def _mock_judge(correctness=0.8, completeness=0.7, style=0.9, notes="good"):
    def call(prompt: str, model: str) -> str:
        return json.dumps({"correctness": correctness, "completeness": completeness, "style": style, "notes": notes})

    return call


def test_judge_model_is_non_claude():
    """PR-04: judge is pinned non-Claude model."""
    assert "gpt" in JUDGE_MODEL.lower() or "gemini" in JUDGE_MODEL.lower()
    assert "claude" not in JUDGE_MODEL.lower()


def test_grade_diff_quality_returns_fields():
    """PR-04: grader returns weighted_score, verdict, judge_model."""
    result = grade_diff_quality("Fix auth bug", _SAMPLE_DIFF, _SAMPLE_DIFF, _llm_call=_mock_judge())
    assert "weighted_score" in result
    assert "verdict" in result
    assert "judge_model" in result
    assert result["judge_model"] == JUDGE_MODEL


def test_grade_diff_quality_weighted_score():
    """PR-04: weighted = 0.5*correct + 0.35*complete + 0.15*style."""
    result = grade_diff_quality("title", "gen", "ref", _llm_call=_mock_judge(0.8, 0.6, 1.0))
    expected = round(0.5 * 0.8 + 0.35 * 0.6 + 0.15 * 1.0, 4)
    assert result["weighted_score"] == expected


def test_grade_diff_quality_pass_fail():
    result_pass = grade_diff_quality("t", "g", "r", _llm_call=_mock_judge(0.8, 0.8, 0.8))
    assert result_pass["verdict"] == "pass"
    result_fail = grade_diff_quality("t", "g", "r", _llm_call=_mock_judge(0.2, 0.2, 0.2))
    assert result_fail["verdict"] == "fail"


def test_grade_diff_quality_handles_parse_error():
    def bad(prompt, model):
        return "NOT JSON"

    result = grade_diff_quality("t", "g", "r", _llm_call=bad)
    assert "parse error" in result["notes"]
    assert result["verdict"] == "fail"


# --------------------------------------------------------------------------- #
# PR-06: transcript storage                                                    #
# --------------------------------------------------------------------------- #


def test_run_pr_arm_stores_transcript():
    """PR-06: arm execution stores transcript.json."""

    def mock_agent(prompt, mode, repo_root):
        return {"diff": _SAMPLE_DIFF, "cost_usd": 0.01}

    pr_metadata = {
        "title": "Fix bug",
        "body": "body",
        "base_sha": "abc",
        "pr_number": 1,
    }

    with tempfile.TemporaryDirectory() as d:
        out_dir = Path(d) / "run"
        out_dir.mkdir()
        arm_result = run_pr_arm(pr_metadata, "on", out_dir, Path(d), _agent_run=mock_agent)

        transcript_path = Path(arm_result["transcript_path"])
        assert transcript_path.exists()
        data = json.loads(transcript_path.read_text())
        assert data["diff"] == _SAMPLE_DIFF


# --------------------------------------------------------------------------- #
# PR-05: comparison table (smoke test)                                         #
# --------------------------------------------------------------------------- #


def test_print_pr_comparison_table_smoke():
    """PR-05: table prints without error."""
    results = [
        {
            "mode": "on",
            "cost_usd": 0.01,
            "latency_ms": 1200,
            "diff_score": {"sequence_ratio": 0.85},
            "judge_score": {"weighted_score": 0.72},
        },
        {
            "mode": "off",
            "cost_usd": 0.02,
            "latency_ms": 2400,
            "diff_score": {"sequence_ratio": 0.40},
            "judge_score": {"weighted_score": 0.50},
        },
    ]
    # Should not raise
    print_pr_comparison_table(results)
