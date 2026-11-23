from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.pro.capabilities.review.project_knowledge import (
    CanonicalRefIdentity,
    KnowledgeFact,
    KnowledgeQuery,
    KnowledgeSubject,
    ProjectKnowledgeStore,
    RepositoryGeneration,
    RepositoryIdentity,
    build_baseline_projection,
)
from lemoncrow.pro.capabilities.review.rules import ContentAccessRule
from lemoncrow.pro.capabilities.review.store import ReviewStore


def projection():
    repo = RepositoryIdentity("local", "qualified")
    generation = RepositoryGeneration(CanonicalRefIdentity(repo, "refs/heads/main"), "1" * 40)
    facts = tuple(
        KnowledgeFact(
            KnowledgeSubject("path", f"src/{i}.py"),
            "inventory",
            f"fact {i}",
            (generation.source_ref,),
            source_paths=(f"src/{i}.py",),
        )
        for i in range(6)
    )
    return build_baseline_projection(generation, facts=facts, observed_at="2026-10-06T00:00:00Z")


@pytest.mark.parametrize("backend", ["standalone", "review"])
def test_replay_checks_entire_projection_and_racing_writers(tmp_path: Path, backend: str):
    p = projection()
    storage = tmp_path / "state.sqlite3" if backend == "standalone" else ReviewStore(tmp_path / "review")
    store = ProjectKnowledgeStore(storage)
    store.init()
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: store.apply(p), range(2))) == [False, True]
    before = store.deterministic_digest(p.generation.repository)
    changed = replace(p, entries=(replace(p.entries[0], body="altered"), *p.entries[1:]))
    with pytest.raises(ValueError, match="collision"):
        store.apply(changed)
    changed = replace(p, retire=(p.entries[0].key,))
    with pytest.raises(ValueError, match="collision"):
        store.apply(changed)
    assert store.deterministic_digest(p.generation.repository) == before


def test_bounded_pages_do_not_hide_allowed_rows(tmp_path: Path):
    p = projection()
    store = ProjectKnowledgeStore(tmp_path / "state.sqlite3")
    store.apply(p)
    initial = store.query(p.generation.repository, query=KnowledgeQuery(limit=6)).entries
    denied = ContentAccessRule(
        mode="ai_denied",
        path_scope=tuple(row.subject.key for row in initial[:5]),
        origin="test",
        trust_class="trusted_base_repository",
        source_identity="test",
    )
    page = store.query(p.generation.repository, query=KnowledgeQuery(limit=1, scan_limit=2), content_access=(denied,))
    assert page.entries == ()
    assert page.truncated and page.next_offset == 2 and page.scanned == 2
    rows = []
    while page.next_offset is not None:
        page = store.query(
            p.generation.repository,
            query=KnowledgeQuery(limit=1, scan_limit=2, offset=page.next_offset),
            content_access=(denied,),
        )
        rows.extend(page.entries)
    assert [row.body for row in rows] == [initial[-1].body]


def test_rebuild_bound_and_failure_are_atomic(tmp_path: Path):
    p = projection()
    store = ProjectKnowledgeStore(tmp_path / "state.sqlite3")
    store.apply(p)
    before = store.deterministic_digest(p.generation.repository)
    with pytest.raises(ValueError, match="1000"):
        store.rebuild(p.generation.repository, (p,) * 1001)
    with pytest.raises(ValueError, match="stale or out of order"):
        store.rebuild(p.generation.repository, (p, replace(p, event=replace(p.event, observed_at="later"))))
    assert store.deterministic_digest(p.generation.repository) == before
    assert store.rebuild(p.generation.repository, (p,)) == before
    assert store.rebuild(p.generation.repository, (p,) * 1000) == before
    assert store.purge_paths(p.generation.repository, ("src/0.py",)) == 1
    assert all(row.subject.key != "src/0.py" for row in store.query(p.generation.repository).entries)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def init_git(root: Path) -> str:
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "PK0 Test")
    git(root, "config", "user.email", "pk0@example.invalid")
    (root / "README.md").write_text("Canonical contract.", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    return git(root, "rev-parse", "HEAD")


def test_actual_git_canonical_bootstrap_advance_and_cli(tmp_path: Path):
    from lemoncrow.gateway.cli.commands.knowledge import knowledge_group
    from lemoncrow.pro.capabilities.review.project_knowledge_source import CanonicalGitKnowledge

    root = tmp_path / "repo"
    root.mkdir()
    previous = init_git(root)
    source = CanonicalGitKnowledge(root, RepositoryIdentity("local", "qualified"), "refs/heads/main")
    store = ProjectKnowledgeStore(ReviewStore(tmp_path / "review"))
    first = source.sync(store)
    assert first["kind"] == "baseline"
    assert any(row.body == "Canonical contract." for row in store.query(source.repository).entries)
    git(root, "checkout", "-b", "feature")
    (root / "README.md").write_text("Unmerged feature secret.", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "unmerged")
    feature = git(root, "rev-parse", "HEAD")
    assert source.sync(store)["applied"] is False
    assert all("secret" not in row.body for row in store.query(source.repository).entries)
    with pytest.raises(ValueError, match="canonical"):
        source.sync(store, previous_revision=feature)
    git(root, "checkout", "main")
    (root / "README.md").write_text("Landed contract changed.", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "advance")
    (root / "README.md").write_text("Dirty workspace secret.", encoding="utf-8")
    assert source.sync(store, previous_revision=previous)["kind"] == "source_advance"
    assert any(row.body == "Landed contract changed." for row in store.query(source.repository).entries)
    assert all("secret" not in row.body for row in store.query(source.repository).entries)
    result = CliRunner().invoke(
        knowledge_group,
        [
            "project",
            "query",
            "--repo",
            str(root),
            "--repository-key",
            "qualified",
            "--canonical-ref",
            "refs/heads/main",
        ],
        obj={"root": str(tmp_path / "review")},
    )
    assert result.exit_code == 0, result.output
    assert "Landed contract changed." in result.output


def _knowledge_cli(root: Path, state: Path, *args: str):
    from lemoncrow.gateway.cli.commands.knowledge import knowledge_group

    return CliRunner().invoke(
        knowledge_group,
        [
            "project",
            *args,
            "--repo",
            str(root),
            "--repository-key",
            "qualified",
            "--canonical-ref",
            "refs/heads/main",
        ],
        obj={"root": str(state)},
    )


def test_a_partial_baseline_keeps_documents_first_and_says_what_it_left_out(tmp_path: Path):
    from lemoncrow.pro.capabilities.review.project_knowledge_source import CanonicalGitKnowledge

    init_git(tmp_path)
    for name in ("a.py", "b.py", "z.md"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "more")
    repository = RepositoryIdentity("local", "qualified")
    store = ProjectKnowledgeStore(tmp_path / "state.sqlite3")

    # At the bound nothing is left out; one under it, the code path goes first.
    exact = CanonicalGitKnowledge(tmp_path, repository, "refs/heads/main", max_paths=4)
    assert exact.sync(store, partial_baseline=True)["paths_omitted"] == 0
    bounded = CanonicalGitKnowledge(tmp_path, repository, "refs/heads/main", max_paths=3)
    result = bounded.sync(store, rebuild=True, partial_baseline=True)

    assert result["coverage"] == "partial_documents_first" and result["paths_omitted"] == 1
    assert "baseline limited to 3 of 4 committed paths, documents first" in result["degraded_reasons"]
    assert {row.subject.key for row in store.query(repository).entries} == {"README.md", "a.py", "z.md"}
    first = store.deterministic_digest(repository)
    bounded.sync(store, rebuild=True, partial_baseline=True)
    assert store.deterministic_digest(repository) == first


def test_the_cli_starts_large_repositories_and_finds_its_own_previous_commit(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    init_git(root)
    for index in range(1001):
        (root / f"f{index}.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "large")
    state = tmp_path / "state"

    started = _knowledge_cli(root, state, "sync")
    assert started.exit_code == 0, started.output
    assert '"coverage": "partial_documents_first"' in started.output and '"paths_omitted": 2' in started.output

    (root / "README.md").write_text("Landed contract changed.", encoding="utf-8")
    git(root, "commit", "-am", "advance")
    advanced = _knowledge_cli(root, state, "sync")
    assert advanced.exit_code == 0, advanced.output
    assert '"kind": "source_advance"' in advanced.output
    assert "Landed contract changed." in _knowledge_cli(root, state, "query", "--term", "contract").output


def test_generation_order_comes_from_git_ancestry(tmp_path: Path):
    from lemoncrow.pro.capabilities.review.project_knowledge_source import CanonicalGitKnowledge

    first = init_git(tmp_path)
    source = CanonicalGitKnowledge(tmp_path, RepositoryIdentity("local", "qualified"), "refs/heads/main")
    store = ProjectKnowledgeStore(tmp_path / "state.sqlite3")
    source.sync(store)
    baseline = store.current_generation(source.repository)
    (tmp_path / "README.md").write_text("Second.", encoding="utf-8")
    git(tmp_path, "commit", "-am", "second")
    second = git(tmp_path, "rev-parse", "HEAD")
    source.sync(store, previous_revision=first)
    latest = store.current_generation(source.repository)

    assert source.generation_order({baseline, latest}, first) == {baseline: "predates", latest: "after_base"}
    assert source.generation_order({baseline, latest}, second) == {baseline: "predates", latest: "predates"}
    assert source.generation_order({"generation:" + "0" * 64}, first) == {}
    with pytest.raises(ValueError):
        source.generation_order({baseline}, "f" * 40)


def test_committed_deny_filters_old_context_and_workspace_policy_cannot_relax(tmp_path: Path):
    from lemoncrow.pro.capabilities.review.project_knowledge_source import CanonicalGitKnowledge

    init_git(tmp_path)
    source = CanonicalGitKnowledge(tmp_path, RepositoryIdentity("local", "qualified"), "refs/heads/main")
    store = ProjectKnowledgeStore(tmp_path / "state.sqlite3")
    source.sync(store)
    policy_dir = tmp_path / ".lemoncrow"
    policy_dir.mkdir()
    policy = policy_dir / "review.yaml"
    policy.write_text('ai_content_access:\n  - paths: ["README.md"]\n    mode: ai_denied\n', encoding="utf-8")
    git(tmp_path, "add", ".lemoncrow/review.yaml")
    git(tmp_path, "commit", "-m", "deny")
    policy.write_text("ai_content_access: []", encoding="utf-8")
    assert store.query(source.repository, content_access=source.content_access()).entries == ()
    policy.write_text("ai_content_access: wrong", encoding="utf-8")
    git(tmp_path, "add", ".lemoncrow/review.yaml")
    git(tmp_path, "commit", "-m", "malformed")
    with pytest.raises(ValueError, match="policy is invalid"):
        source.content_access()


def test_metadata_cannot_launder_path_support():
    with pytest.raises(ValueError, match="metadata_only"):
        KnowledgeFact(
            KnowledgeSubject("repository", "root"),
            "context",
            "source content",
            ("source:new:private.md@sha256:" + "a" * 64,),
            content_access_class="metadata_only",
        )


def test_document_bounds_and_rewritten_canonical_ref(tmp_path: Path):
    from lemoncrow.pro.capabilities.review.project_knowledge_source import CanonicalGitKnowledge

    previous = init_git(tmp_path)
    source = CanonicalGitKnowledge(tmp_path, RepositoryIdentity("local", "qualified"), "refs/heads/main")
    store = ProjectKnowledgeStore(tmp_path / "state.sqlite3")
    source.sync(store)
    for name, size in (("exact.txt", 65536), ("over.txt", 65537)):
        (tmp_path / name).write_text("a" * size, encoding="utf-8")
    git(tmp_path, "add", "exact.txt", "over.txt")
    git(tmp_path, "commit", "-m", "bounded docs")
    result = source.sync(store, previous_revision=previous)
    assert "document exceeds 65536 bytes: over.txt" in result["degraded_reasons"]
    assert "document excerpt limited to 12000 characters: exact.txt" in result["degraded_reasons"]
    entries = store.query(source.repository, query=KnowledgeQuery(limit=20)).entries
    assert any(row.subject.key == "exact.txt" and row.knowledge_kind == "documentation" for row in entries)
    assert not any(row.subject.key == "over.txt" and row.knowledge_kind == "documentation" for row in entries)
    previous = git(tmp_path, "rev-parse", "HEAD")
    before = store.deterministic_digest(source.repository)
    git(tmp_path, "checkout", "--orphan", "rewritten")
    git(tmp_path, "commit", "-m", "unrelated history")
    git(tmp_path, "branch", "-f", "main", "HEAD")
    with pytest.raises(ValueError, match="descendant"):
        source.sync(store, previous_revision=previous)
    assert store.deterministic_digest(source.repository) == before


def test_source_bound_rejects_before_writing(tmp_path: Path):
    from lemoncrow.pro.capabilities.review.project_knowledge_source import CanonicalGitKnowledge

    init_git(tmp_path)
    source = CanonicalGitKnowledge(tmp_path, RepositoryIdentity("local", "qualified"), "refs/heads/main", max_paths=1)
    (tmp_path / "other.py").write_text("pass", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "too many")
    store = ProjectKnowledgeStore(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="path bound"):
        source.sync(store)
    assert store.current_generation(source.repository) == ""
