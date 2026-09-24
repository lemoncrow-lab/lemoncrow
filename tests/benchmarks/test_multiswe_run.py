from argparse import Namespace
from types import SimpleNamespace

from benchmarks.codebench import incontainer
from benchmarks.codebench.multiswe_run import _effective_parallelism, _write_benchmark_manifest


def test_effective_parallelism_honors_jobs_with_oauth_capacity() -> None:
    assert _effective_parallelism(jobs=2, jobs_per_token=4, token_count=1) == 2


def test_effective_parallelism_is_capped_by_token_capacity() -> None:
    assert _effective_parallelism(jobs=8, jobs_per_token=2, token_count=2) == 4


def test_effective_parallelism_without_tokens_uses_jobs() -> None:
    assert _effective_parallelism(jobs=3, jobs_per_token=4, token_count=0) == 3


def test_claude_overlay_is_version_pinned() -> None:
    version = incontainer.CLAUDE_CODE_VERSION
    assert version == "2.1.197"
    assert f"@anthropic-ai/claude-code@{version}" in incontainer._BASELINE_INSTALL
    assert f"baseline-claude-{version}" in incontainer.overlay_tag("demo:image", lc=False)
    assert f"lemoncrow-{incontainer.LEMONCROW_OVERLAY_REVISION}-claude-{version}" in incontainer.overlay_tag(
        "demo:image", lc=True
    )


def test_benchmark_manifest_records_runtime_provenance(tmp_path) -> None:
    args = Namespace(
        suite="swe-lite",
        dataset=None,
        arms=["lemoncrow"],
        reps=1,
        driver="claude",
        model="claude-opus-4-8",
        max_turns=50,
        timeout=1800,
        jobs=2,
        jobs_per_token=4,
    )
    instances = [SimpleNamespace(instance_id="demo__repo-1")]
    first = _write_benchmark_manifest(
        tmp_path,
        args=args,
        instances=instances,
        grade_label="swebench",
        start_fingerprint="abc123",
    )
    assert first["claude_code_version"] == "2.1.197"
    assert first["runtime_source_fingerprint_start"] == "abc123"
    final = _write_benchmark_manifest(
        tmp_path,
        args=args,
        instances=instances,
        grade_label="swebench",
        start_fingerprint="abc123",
        end_fingerprint="def456",
    )
    assert final["runtime_source_changed_during_run"] is True
    assert final["runtime_source_fingerprint_end"] == "def456"
