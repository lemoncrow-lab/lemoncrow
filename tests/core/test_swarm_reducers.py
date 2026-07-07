from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.swarm.models import Finding, SwarmChildState, SwarmRunState, SwarmWaveState
from atelier.core.capabilities.swarm.reducers import WaveContext, get_reducer


def _child(
    tmp_path: Path, child_id: str, *, findings: list[Finding] | None = None, answer: str = "", status: str = "success"
) -> SwarmChildState:
    run_dir = tmp_path / child_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return SwarmChildState(
        child_id=child_id,
        label=child_id,
        wave_index=1,
        status=status,  # type: ignore[arg-type]
        worktree_path=str(run_dir),
        atelier_root=str(run_dir / "atelier-root"),
        run_dir=str(run_dir),
        spec_path=str(run_dir / "program.md"),
        result_path=str(run_dir / "result.json"),
        stdout_path=str(run_dir / "stdout.log"),
        stderr_path=str(run_dir / "stderr.log"),
        metadata_path=str(run_dir / "meta.json"),
        patch_path=str(run_dir / "candidate.patch"),
        findings=findings or [],
        answer=answer,
    )


def _state(tmp_path: Path, *, reducer: str, quorum: int = 0) -> SwarmRunState:
    return SwarmRunState(
        run_id="swarm-red",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        copied_spec_path=str(tmp_path / "program.md"),
        child_command=["true"],
        exec_mode="readonly",
        reducer_name=reducer,
        quorum=quorum,
    )


def test_union_dedups_by_signature(tmp_path: Path) -> None:
    # two strategies find an overlapping bug (same explicit signature) plus distinct ones
    a = _child(
        tmp_path,
        "run-01",
        findings=[
            Finding(kind="bug", file="a.py", line=10, title="NPE", signature="npe-a"),
            Finding(kind="bug", file="b.py", line=20, title="leak", signature="leak-b"),
        ],
    )
    b = _child(
        tmp_path,
        "run-02",
        findings=[
            Finding(kind="bug", file="a.py", line=10, title="NPE", signature="npe-a"),  # duplicate
            Finding(kind="smell", file="c.py", title="dup code"),  # derived signature
        ],
    )
    state = _state(tmp_path, reducer="union")
    wave = SwarmWaveState(wave_index=1)
    ev = get_reducer("union").reduce([a, b], WaveContext(state=state, wave=wave))

    assert sorted(ev.accepted_child_ids) == ["run-01", "run-02"]
    assert len(ev.merged_output) == 3  # npe-a deduped across the two strategies
    assert "npe-a" in {f["signature"] for f in ev.merged_output}
    assert "deduplicated 1" in ev.summary


def test_union_skips_failed_children(tmp_path: Path) -> None:
    ok = _child(tmp_path, "run-01", findings=[Finding(title="x", signature="x")])
    bad = _child(tmp_path, "run-02", findings=[Finding(title="y", signature="y")], status="failed")
    state = _state(tmp_path, reducer="union")
    ev = get_reducer("union").reduce([ok, bad], WaveContext(state=state, wave=SwarmWaveState(wave_index=1)))
    assert ev.accepted_child_ids == ["run-01"]
    assert ev.rejected_child_ids == ["run-02"]
    assert len(ev.merged_output) == 1  # failed child's finding excluded


def test_vote_reaches_consensus_on_majority(tmp_path: Path) -> None:
    children = [
        _child(tmp_path, "run-01", answer="survives"),
        _child(tmp_path, "run-02", answer="Survives"),  # normalized equal
        _child(tmp_path, "run-03", answer="refuted"),
    ]
    state = _state(tmp_path, reducer="vote")  # majority of 3 == 2
    ev = get_reducer("vote").reduce(children, WaveContext(state=state, wave=SwarmWaveState(wave_index=1)))
    assert ev.verdict == "converged"
    assert sorted(ev.accepted_child_ids) == ["run-01", "run-02"]
    assert ev.merged_output["consensus"] == "survives"
    assert ev.merged_output["votes"] == 2


def test_vote_flips_at_quorum_boundary(tmp_path: Path) -> None:
    children = [
        _child(tmp_path, "run-01", answer="yes"),
        _child(tmp_path, "run-02", answer="yes"),
        _child(tmp_path, "run-03", answer="no"),
    ]
    # quorum 2 -> reached (2 'yes')
    ev_ok = get_reducer("vote").reduce(
        children, WaveContext(state=_state(tmp_path, reducer="vote", quorum=2), wave=SwarmWaveState(wave_index=1))
    )
    assert ev_ok.verdict == "converged"
    assert sorted(ev_ok.accepted_child_ids) == ["run-01", "run-02"]
    # quorum 3 -> not reached (top group has only 2)
    ev_no = get_reducer("vote").reduce(
        children, WaveContext(state=_state(tmp_path, reducer="vote", quorum=3), wave=SwarmWaveState(wave_index=1))
    )
    assert ev_no.verdict == "stagnating"
    assert ev_no.accepted_child_ids == []
