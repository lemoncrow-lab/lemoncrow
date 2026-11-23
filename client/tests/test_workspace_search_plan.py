"""Workspace planner: cheap metadata, explicit coverage and calibrated ranking."""

from __future__ import annotations

from pathlib import Path

from lemoncrow_client.lite.workspace_search_plan import choose_first_wave, hints, normalize_member_ranks
from lemoncrow_client.workspace_members import WorkspaceRepo


def _repo(name: str) -> WorkspaceRepo:
    return WorkspaceRepo(root=Path("/test-workspace") / name, relpath=name)


def test_code_search_skips_large_data_roots_unless_named_by_query() -> None:
    repos = [_repo("Beseam/pipelines/shop-corpus"), _repo("Authward"), _repo("Beseam"), _repo("Billing")]
    assert choose_first_wave("OAuth token handling", repos, cap=2) == ("Authward", "Beseam")
    assert choose_first_wave("shop corpus dataset", repos, cap=1) == ("Beseam/pipelines/shop-corpus",)
    assert [item.role for item in hints("find the billing handler", repos) if item.repo.relpath == "Billing"] == [
        "code"
    ]


def test_cross_repo_ranking_preserves_stronger_file_evidence_over_repo_name_prior() -> None:
    unrelated = _repo("Zed")
    billing = _repo("Billing")
    grouped = [
        (unrelated, [{"path": "Zed/huge.py", "score": 90000.0}, {"path": "Zed/other.py", "score": 50000.0}]),
        (billing, [{"path": "Billing/payments.py", "score": 19.0}, {"path": "Billing/tests.py", "score": 8.0}]),
    ]
    ranked = normalize_member_ranks(grouped, "billing payments")
    assert [item["path"] for item in ranked] == [
        "Zed/huge.py",
        "Zed/other.py",
        "Billing/payments.py",
        "Billing/tests.py",
    ]
    assert ranked[0]["score"] == 90000.0
    assert all(0 <= item["workspace_score"] < 1 for item in ranked)
    assert all(ranked[i]["workspace_score"] >= ranked[i + 1]["workspace_score"] for i in range(3))


def test_workspace_ranking_does_not_promote_weak_fts_hits_over_precise_symbols() -> None:
    weak, strong = _repo("Alpha"), _repo("Zed")
    ranked = normalize_member_ranks(
        [
            (weak, [{"path": "Alpha/docs.md", "score": 41.0, "channels": ["fts"]}]),
            (strong, [{"path": "Zed/implementation.py", "score": 184.0, "channels": ["symbol"]}]),
        ],
        "alpha implementation",
    )
    assert ranked[0]["path"] == "Zed/implementation.py"
    assert ranked[0]["workspace_score"] > ranked[1]["workspace_score"]


def test_ranking_respects_per_repo_order_and_deterministic_paths_when_scores_tie() -> None:
    first, second = _repo("A"), _repo("B")
    grouped = [
        (first, [{"path": "A/z.py", "score": 30.0}, {"path": "A/a.py", "score": 8.0}]),
        (second, [{"path": "B/a.py", "score": 30.0}]),
    ]
    result = normalize_member_ranks(grouped, "unrelated symbol")
    assert [item["path"] for item in result] == ["A/z.py", "B/a.py", "A/a.py"]
    assert result == normalize_member_ranks(grouped[::-1], "unrelated symbol")


def test_bad_or_nonfinite_scores_do_not_break_fusion() -> None:
    result = normalize_member_ranks(
        [(_repo("A"), [{"path": "A/a", "score": float("nan")}, {"path": "A/b", "score": -42}])],
        "anything",
    )
    assert [item["path"] for item in result] == ["A/a", "A/b"]
    assert all(isinstance(item["workspace_score"], float) for item in result)


def test_first_wave_uses_bounded_local_descriptions_without_reading_application_code(tmp_path: Path) -> None:
    from lemoncrow_client.lite.workspace_search_plan import choose_first_wave

    roots = [tmp_path / f"repo{i:02}" for i in range(12)]
    for root in roots:
        root.mkdir()
        (root / "README.md").write_text("Generic software repository.\n", encoding="utf-8")
    (roots[11] / "README.md").write_text(
        "Outbound campaigns and email warmup with automatic recovery.\n", encoding="utf-8"
    )
    candidates = [WorkspaceRepo(root=root, relpath=root.name) for root in roots]
    assert "repo11" in choose_first_wave("email warmup campaign recovery", candidates)
    assert "repo11" not in choose_first_wave("email warmup campaign recovery", candidates[:11])
    (roots[11] / "README.md").write_text("Another generic README.\n", encoding="utf-8")
    assert "repo11" not in choose_first_wave("email warmup campaign recovery", candidates)
    # The README can change without a persistent index rebuild.
    (roots[11] / "README.md").write_text("email warmup recovery outreach.\n", encoding="utf-8")
    assert "repo11" in choose_first_wave("email warmup campaign recovery", candidates)


def test_first_wave_rejects_symlinked_readme_and_keeps_source_unread(tmp_path: Path) -> None:
    from lemoncrow_client.lite.workspace_search_plan import choose_first_wave

    source = tmp_path / "private.txt"
    source.write_text("super secret account linking token\n", encoding="utf-8")
    others = []
    for i in range(9):
        repo = tmp_path / f"repo{i:02}"
        repo.mkdir()
        others.append(WorkspaceRepo(root=repo, relpath=repo.name))
    (others[8].root / "README.md").symlink_to(source)
    assert "repo08" not in choose_first_wave("super secret account linking token", others)


def test_large_readme_is_not_loaded_for_routing(tmp_path: Path) -> None:
    from lemoncrow_client.lite.workspace_search_plan import choose_first_wave

    repos = []
    for i in range(9):
        repo = tmp_path / f"repo{i:02}"
        repo.mkdir()
        repos.append(WorkspaceRepo(root=repo, relpath=repo.name))
    (repos[-1].root / "README.md").write_text("special keyword\n" + "x" * 2_000_001)
    assert "repo08" not in choose_first_wave("special keyword", repos)


def test_readme_swap_to_symlink_between_stat_and_open_cannot_escape_repo(tmp_path: Path) -> None:
    from lemoncrow_client.lite.workspace_search_plan import _description_terms

    # Force the vulnerable path in a deterministic race: a regular README at
    # stat time is replaced by a symlink right before the read.
    public = tmp_path / "checkout"
    public.mkdir()
    readme = public / "README.md"
    readme.write_text("ordinary content\n")
    metadata = readme.stat()
    private = tmp_path / "outside-private.txt"
    private.write_text("private_routing_keyword\n")
    readme.unlink()
    readme.symlink_to(private)
    assert _description_terms.__wrapped__(readme, metadata.st_mtime_ns, metadata.st_size) == (frozenset(), frozenset())


def test_readme_swap_to_fifo_cannot_block_or_read_source(tmp_path: Path) -> None:
    import os

    import pytest
    from lemoncrow_client.lite.workspace_search_plan import _description_terms

    if not hasattr(os, "mkfifo"):
        pytest.skip("requires a POSIX filesystem")
    readme = tmp_path / "README.md"
    readme.write_text("simple\n")
    metadata = readme.stat()
    readme.unlink()
    os.mkfifo(readme)
    assert _description_terms.__wrapped__(readme, metadata.st_mtime_ns, metadata.st_size) == (frozenset(), frozenset())
