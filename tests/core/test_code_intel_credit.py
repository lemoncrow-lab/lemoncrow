"""Unit tests for the honest code-intel avoided-read credit (pure functions).

These exercise the counterfactual directly: a snippet-bearing reference that is
never read earns exactly one deferred credit; a snippet-less reference, or one
that is later read, earns nothing.
"""

from __future__ import annotations

from atelier.core.capabilities import code_intel_credit as cic

# --- extract_credited_paths: snippet gating ---------------------------------


def test_node_with_inline_source_is_credited() -> None:
    # node returns a single definition; its full body lives under `source`.
    result = {"name": "helper", "path": "src/h.py", "source": "def helper(): ..."}
    assert cic.extract_credited_paths("node", result) == ["src/h.py"]


def test_node_without_inline_source_earns_nothing() -> None:
    assert cic.extract_credited_paths("node", {"name": "helper", "path": "src/h.py"}) == []
    # Whitespace-only source is not real source.
    assert cic.extract_credited_paths("node", {"name": "helper", "path": "src/h.py", "source": "   "}) == []


def test_explore_inline_file_source_is_credited() -> None:
    result = {
        "files": [
            {"file_path": "src/main.py", "source_sections": [{"content": "1\tdef main(): ..."}]},
            {"file_path": "src/empty.py", "source_sections": [{"content": ""}]},  # no real source -> skip
        ]
    }
    assert cic.extract_credited_paths("explore", result) == ["src/main.py"]


def test_explore_relationship_snippets_are_not_credited() -> None:
    # Conservative contract: explore credits ONLY inline file source, never the
    # partial caller/usage line-snippets carried under `relationships`.
    result = {
        "files": [
            {"file_path": "src/main.py", "source_sections": [{"content": "def main(): ..."}]},
        ],
        "relationships": {
            "callers": [{"symbol_name": "main", "related": [{"path": "src/caller.py", "snippet": "main()"}]}],
            "usages": [{"symbol_name": "main", "references": [{"path": "src/use.py", "snippet": "main()"}]}],
        },
    }
    paths = cic.extract_credited_paths("explore", result)
    assert paths == ["src/main.py"]
    assert "src/caller.py" not in paths
    assert "src/use.py" not in paths


def test_partial_snippet_tools_are_not_credited() -> None:
    # callers/callees/usages/impact return partial line-snippets that do not
    # reliably substitute for a full read -> deliberately NOT credit-eligible,
    # even when a snippet is present.
    related = {"related": [{"name": "run", "path": "src/a.py", "snippet": "def run(): ..."}]}
    refs_flat = {"references": [{"path": "src/x.py", "line": 10, "snippet": "foo()"}]}
    refs_grouped = {"references": {"src/c.py": [{"path": "src/c.py", "snippet": "charge()"}]}}
    assert cic.extract_credited_paths("callers", related) == []
    assert cic.extract_credited_paths("callees", related) == []
    assert cic.extract_credited_paths("usages", refs_flat) == []
    assert cic.extract_credited_paths("usages", refs_grouped) == []


def test_non_credit_eligible_tools_return_empty() -> None:
    snippet_ref = {"related": [{"path": "src/a.py", "snippet": "x"}]}
    for tool in ("read", "search", "grep", "callers", "usages"):
        assert cic.extract_credited_paths(tool, snippet_ref) == []


def test_non_dict_result_returns_empty() -> None:
    assert cic.extract_credited_paths("node", None) == []
    assert cic.extract_credited_paths("explore", "oops") == []
    assert cic.extract_credited_paths("node", []) == []


def test_extract_distinct_per_file() -> None:
    # explore with two source sections for one file -> a single credit.
    result = {
        "files": [
            {"file_path": "src/a.py", "source_sections": [{"content": "one"}, {"content": "two"}]},
            {"file_path": "src/b.py", "source_sections": [{"content": "three"}]},
        ]
    }
    assert cic.extract_credited_paths("explore", result) == ["src/a.py", "src/b.py"]


# --- pending lifecycle ------------------------------------------------------


def test_record_then_read_then_no_credit() -> None:
    state: dict = {}
    state = cic.record_pending(state, "callers", ["src/a.py"])
    assert len(state["code_intel_pending"]) == 1
    # The agent reads the file -> the read happened, credit must be canceled.
    state = cic.consume_reads(state, ["src/a.py"])
    assert state["code_intel_pending"] == []
    # Tick to/past the threshold: no credit.
    for _ in range(10):
        state, credits = cic.tick_and_credit(state, threshold=8)
        assert credits == []


def test_record_never_read_credits_exactly_once() -> None:
    state: dict = {}
    state = cic.record_pending(state, "usages", ["src/x.py"])
    # Below threshold: nothing yet.
    for _ in range(7):
        state, credits = cic.tick_and_credit(state, threshold=8)
        assert credits == []
    # The 8th tick reaches age>=threshold -> exactly one credit.
    state, credits = cic.tick_and_credit(state, threshold=8)
    assert credits == [{"tool": "usages", "path": "src/x.py"}]
    assert state["code_intel_pending"] == []
    # Further ticks earn nothing (entry removed).
    state, credits = cic.tick_and_credit(state, threshold=8)
    assert credits == []


def test_duplicate_surfacing_single_pending_entry() -> None:
    state: dict = {}
    state = cic.record_pending(state, "callers", ["src/a.py"])
    state = cic.record_pending(state, "usages", ["src/a.py"])  # same path again
    assert len(state["code_intel_pending"]) == 1
    # Earliest tool that surfaced it wins.
    assert state["code_intel_pending"][0]["tool"] == "callers"


def test_reset_pending_clears() -> None:
    state: dict = {}
    state = cic.record_pending(state, "node", ["src/a.py", "src/b.py"])
    assert len(state["code_intel_pending"]) == 2
    state = cic.reset_pending(state)
    assert state["code_intel_pending"] == []


# --- defensiveness / totality ------------------------------------------------


def test_functions_tolerate_garbage_state() -> None:
    # Non-dict state must round-trip without raising.
    assert cic.record_pending(None, "callers", ["x"]) is None  # type: ignore[arg-type]
    assert cic.consume_reads(None, ["x"]) is None  # type: ignore[arg-type]
    bad, credits = cic.tick_and_credit(None, threshold=8)  # type: ignore[arg-type]
    assert bad is None and credits == []
    # Garbage pending list is tolerated.
    state = {"code_intel_pending": "not-a-list"}
    state, credits = cic.tick_and_credit(state, threshold=8)
    assert credits == []


def test_tick_with_bad_threshold_is_noop() -> None:
    state: dict = {}
    state = cic.record_pending(state, "callers", ["src/a.py"])
    state, credits = cic.tick_and_credit(state, threshold="oops")  # type: ignore[arg-type]
    assert credits == []
    # Pending untouched.
    assert len(state["code_intel_pending"]) == 1
