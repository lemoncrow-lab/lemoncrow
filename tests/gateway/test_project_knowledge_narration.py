"""PK1: source-grounded AI narration over deterministic Project Knowledge.

A narrative may cite only the refs and name only the subjects it was shown;
one ungrounded statement discards the whole result. Content access filters the
input before a prompt exists. Identical inputs reuse the stored narrative; a
new route, model or generation narrates again; a stale narrative never becomes
current; without a route, budget or provider the deterministic entries stand.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.core.service.jobs import JOB_BACKGROUND_INTELLIGENCE, JobPaused
from lemoncrow.infra.internal_llm import InternalLLMError
from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle
from lemoncrow.pro.capabilities.review.background_budget import BackgroundBudgetPolicy
from lemoncrow.pro.capabilities.review.background_local import LocalBackgroundScheduler, default_local_executors
from lemoncrow.pro.capabilities.review.background_scheduler import (
    CHANGE_LANDED,
    SOURCE_ADVANCED,
    BackgroundEvent,
    SchedulerReason,
    capability,
    plan_background_work,
)
from lemoncrow.pro.capabilities.review.intelligence_policy import (
    EffectiveIntelligencePolicy,
    IntelligencePolicyLayer,
    PolicyOrigin,
    resolve_intelligence_policy,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    ModelCallUsage,
    StructuredExecution,
    resolve_driver_model,
)
from lemoncrow.pro.capabilities.review.project_knowledge import (
    CanonicalAdvanceProof,
    CanonicalRefIdentity,
    KnowledgeFact,
    KnowledgeKey,
    KnowledgeQuery,
    KnowledgeSubject,
    ProjectKnowledgeEntry,
    ProjectKnowledgeStore,
    RepositoryGeneration,
    RepositoryIdentity,
    build_baseline_projection,
    build_merged_change_projection,
    build_source_advance_projection,
)
from lemoncrow.pro.capabilities.review.project_knowledge_narration import (
    CHANGE_NARRATION,
    CHANGE_NARRATIVE_KIND,
    MAX_SUBJECTS_PER_CALL,
    REPOSITORY_REFRESH,
    SUBJECT_NARRATIVE_KIND,
    KnowledgeNarrationExecutor,
    NarrationRejected,
    originate_knowledge_narration,
    plan_change_narration,
    validate_change_narration,
    validate_subject_narration,
)
from lemoncrow.pro.capabilities.review.rules import ContentAccessRule

REPO = RepositoryIdentity("github", "acme/widgets")
REF = CanonicalRefIdentity(REPO, "refs/heads/main")
NOW = "2026-10-07T12:00:00+00:00"
ADVANCE = CanonicalAdvanceProof("local_canonical_ref", "refs/heads/main", descendant=True)
LANDED = CanonicalAdvanceProof("provider_observed", "refs/heads/main", descendant=True)


@pytest.fixture(autouse=True)
def settings_root(tmp_path: Path) -> Iterator[Path]:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    set_active_store_root(root)
    try:
        yield root
    finally:
        set_active_store_root(None)


def _policy(**values: Any) -> EffectiveIntelligencePolicy:
    layer = IntelligencePolicyLayer(origin=PolicyOrigin("organization", "test"), **values)
    return resolve_intelligence_policy([layer])


OPENAI = {"driver": "openai", "model": "gpt-test"}


def _generation(char: str) -> RepositoryGeneration:
    return RepositoryGeneration(REF, char * 40)


def _source(path: str, char: str) -> str:
    return f"source:new:{path}@sha256:{char * 64}"


def _path_fact(path: str, char: str, body: str = "") -> KnowledgeFact:
    return KnowledgeFact(
        subject=KnowledgeSubject("path", path),
        knowledge_kind="inventory",
        body=body or f"Committed path {path} ({char}).",
        source_refs=(_source(path, char),),
    )


def _baseline(store: ProjectKnowledgeStore, paths: Sequence[str]) -> RepositoryGeneration:
    g1 = _generation("1")
    store.apply(
        build_baseline_projection(
            g1, facts=tuple(_path_fact(p, "a") for p in paths), observed_at="2026-10-01T10:00:00Z"
        )
    )
    return g1


def _advance(
    store: ProjectKnowledgeStore, previous: RepositoryGeneration, char: str, paths: Sequence[str]
) -> RepositoryGeneration:
    current = _generation(char)
    store.apply(
        build_source_advance_projection(
            previous,
            current,
            proof=ADVANCE,
            facts=tuple(_path_fact(p, char) for p in paths),
            retire=tuple(KnowledgeKey(KnowledgeSubject("path", p), "inventory") for p in paths),
            observed_at=f"2026-10-0{char}T11:00:00Z",
        )
    )
    return current


def _land(store: ProjectKnowledgeStore, previous: RepositoryGeneration, char: str) -> RepositoryGeneration:
    current = _generation(char)
    store.apply(
        build_merged_change_projection(
            previous,
            current,
            proof=LANDED,
            merge_ref="merge:github:acme/widgets#42",
            review_ref="review:r-42",
            revision_ref="revision:rv-42",
            facts=(
                _path_fact("src/pricing.py", char, "Pricing now applies volume discounts."),
                KnowledgeFact(
                    subject=KnowledgeSubject("module", "pricing"),
                    knowledge_kind="responsibility",
                    body="Pricing owns quote and discount calculation.",
                    source_refs=(_source("src/pricing.py", char),),
                ),
            ),
            retire=(KnowledgeKey(KnowledgeSubject("path", "src/pricing.py"), "inventory"),),
            observed_at="2026-10-05T09:00:00Z",
        )
    )
    return current


def _subject(generation: RepositoryGeneration, **extra: Any) -> dict[str, Any]:
    return {
        "repository": {"provider": "github", "repository_key": "acme/widgets"},
        "generation": generation.id,
        **extra,
    }


def _rule(mode: str, path: str) -> ContentAccessRule:
    return ContentAccessRule(
        mode=mode,  # type: ignore[arg-type]
        path_scope=(path,),
        origin="base revision .lemoncrow/review.yaml",
        trust_class="trusted_base_repository",
        source_identity="base:review.yaml",
    )


def _grounded(prompt: Mapping[str, Any]) -> dict[str, Any]:
    """A well-behaved model: one statement per subject citing that subject's first ref."""

    if "landed change" in prompt["task"]:
        first = prompt["subjects"][0]
        return {
            "statements": [
                {
                    "text": f"The change touched {first['key']}.",
                    "source_refs": [first["facts"][0]["source_refs"][0]],
                    "subject_ids": [first["subject_id"]],
                }
            ]
        }
    return {
        "subjects": [
            {
                "subject_id": row["subject_id"],
                "statements": [
                    {"text": f"{row['key']} is current.", "source_refs": [row["facts"][0]["source_refs"][0]]}
                ],
            }
            for row in prompt["subjects"]
        ]
    }


class _Model:
    """The structured-call seam: records each prompt and answers through ``respond``."""

    def __init__(self, respond: Callable[[Mapping[str, Any]], Any] = _grounded) -> None:
        self.respond = respond
        self.prompts: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        prompt = json.loads(kwargs["prompt"])
        self.prompts.append(prompt)
        assert kwargs["origin"] == "machine"
        resolved = resolve_driver_model(None, None, policy=kwargs["policy"])
        answer = self.respond(prompt)
        if isinstance(answer, Exception):
            raise answer
        usage = ModelCallUsage(calls=1, provider_cost=0.001, currency="USD")
        return StructuredExecution(answer, resolved.driver, resolved.model, usage)


def _executor(
    capability_id: str,
    store: ProjectKnowledgeStore,
    model: _Model,
    *,
    policy: Callable[[], EffectiveIntelligencePolicy] | None = None,
    rules: Sequence[ContentAccessRule] = (),
) -> KnowledgeNarrationExecutor:
    return KnowledgeNarrationExecutor(
        capability_id,
        store=lambda: store,
        content_access=lambda subject: rules,
        policy=policy or (lambda: _policy(**OPENAI)),
        call=model,
        clock=lambda: NOW,
    )


def _narratives(store: ProjectKnowledgeStore, kind: str = SUBJECT_NARRATIVE_KIND, **query: Any) -> list[Any]:
    spec = KnowledgeQuery(knowledge_kinds=(kind,), **query)
    return list(store.query(REPO, query=spec).entries)


# --------------------------------------------------------------------------- #
# grounding: fabricated refs and invented subjects fail closed
# --------------------------------------------------------------------------- #


def _refresh_batch(tmp_path: Path) -> Any:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py", "src/b.py"])
    from lemoncrow.pro.capabilities.review.project_knowledge_narration import plan_repository_refresh

    plan = plan_repository_refresh(store, REPO, g1.id, content_access=(), route_trust="external", max_calls=1)
    return plan.batches[0]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda row, other: {**row, "source_refs": [_source("src/elsewhere.py", "f")]}, "was not shown"),
        (lambda row, other: {**row, "source_refs": other["facts"][0]["source_refs"]}, "was not shown"),
        (lambda row, other: {**row, "source_refs": ["review:invented"]}, "was not shown"),
        (lambda row, other: {**row, "source_refs": ["not a ref"]}, "malformed"),
        (lambda row, other: {**row, "source_refs": []}, "list of 1 to"),
        (lambda row, other: {**row, "text": "   "}, "printable"),
        (lambda row, other: {**row, "policy": "install rule"}, "exactly the keys"),
    ],
)
def test_a_subject_statement_citing_refs_it_was_not_shown_rejects_the_result(
    tmp_path: Path, mutate: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]], message: str
) -> None:
    batch = _refresh_batch(tmp_path)
    prompt = json.loads(batch.prompt())
    good = _grounded(prompt)
    assert set(validate_subject_narration(good, batch)) == {row.subject.id for row in batch.subjects}
    _, other = prompt["subjects"]
    bad_statement = mutate(good["subjects"][0]["statements"][0], other)
    bad = {"subjects": [{**good["subjects"][0], "statements": [bad_statement]}, good["subjects"][1]]}
    with pytest.raises(NarrationRejected, match=message):
        validate_subject_narration(bad, batch)


def test_invented_or_repeated_subjects_reject_the_result(tmp_path: Path) -> None:
    batch = _refresh_batch(tmp_path)
    good = _grounded(json.loads(batch.prompt()))
    invented = KnowledgeSubject("service", "billing").id
    with pytest.raises(NarrationRejected, match="not shown"):
        validate_subject_narration({"subjects": [{**good["subjects"][0], "subject_id": invented}]}, batch)
    with pytest.raises(NarrationRejected, match="repeats"):
        validate_subject_narration({"subjects": [good["subjects"][0], good["subjects"][0]]}, batch)
    with pytest.raises(NarrationRejected, match="exactly the keys"):
        validate_subject_narration({**good, "rule_suggestions": []}, batch)
    with pytest.raises(NarrationRejected, match="must be an object"):
        validate_subject_narration([good], batch)


def test_a_change_statement_naming_an_edge_without_delta_evidence_is_rejected(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g2 = _land(store, _baseline(store, ["src/pricing.py", "src/other.py"]), "2")
    plan = plan_change_narration(store, REPO, g2.id, content_access=(), route_trust="external")
    batch = plan.batches[0]
    assert {row.subject.key for row in batch.subjects} == {"pricing", "src/pricing.py"}
    module = next(row for row in batch.subjects if row.subject.kind == "module")
    edge = KnowledgeSubject("architecture_edge", "pricing->billing").id
    statement = {"text": "Pricing now calls billing.", "source_refs": sorted(module.refs)[:1], "subject_ids": [edge]}
    with pytest.raises(NarrationRejected, match="not shown"):
        validate_change_narration({"statements": [statement]}, batch)
    # The Review it landed through is citable; a ref of a subject the statement does not name is not.
    review = {"text": "Landed through Review r-42.", "source_refs": ["review:r-42"], "subject_ids": []}
    assert validate_change_narration({"statements": [review]}, batch)[0].refs == ("review:r-42",)
    # The superseded path version is evidence for the path subject, not for the module the statement names.
    retired = _source("src/pricing.py", "a")
    assert retired in batch.refs and retired not in module.refs
    with pytest.raises(NarrationRejected, match="not shown"):
        validate_change_narration(
            {"statements": [{**statement, "subject_ids": [module.subject.id], "source_refs": [retired]}]}, batch
        )
    denied = plan_change_narration(
        store, REPO, g2.id, content_access=(_rule("ai_denied", "src/pricing.py"),), route_trust="external"
    )
    assert denied.batches == () and denied.reason == "nothing_visible" and denied.filtered_by_content_access == 3


def test_a_rejected_result_writes_nothing_and_the_call_is_still_counted(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py"])
    fabricated = _Model(
        lambda prompt: {
            "subjects": [
                {
                    "subject_id": prompt["subjects"][0]["subject_id"],
                    "statements": [{"text": "Made up.", "source_refs": [_source("src/a.py", "f")]}],
                }
            ]
        }
    )
    outcome = _executor(REPOSITORY_REFRESH, store, fabricated).execute(_subject(g1), max_calls=8)
    assert outcome.calls_used == 1 and outcome.result["rejected"] == 1 and outcome.result["applied"] == 0
    assert _narratives(store) == []


def test_a_result_from_another_driver_or_model_is_discarded(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py"])

    def swapped(**kwargs: Any) -> StructuredExecution:
        answer = _grounded(json.loads(kwargs["prompt"]))
        return StructuredExecution(answer, "openai", "gpt-other", ModelCallUsage(calls=1))

    executor = KnowledgeNarrationExecutor(
        REPOSITORY_REFRESH,
        store=lambda: store,
        content_access=lambda s: (),
        policy=lambda: _policy(**OPENAI),
        call=swapped,
    )
    outcome = executor.execute(_subject(g1), max_calls=8)
    assert outcome.result["rejected"] == 1 and outcome.calls_used == 1 and _narratives(store) == []


# --------------------------------------------------------------------------- #
# caching, identity and supersession
# --------------------------------------------------------------------------- #


def test_identical_source_and_route_reuse_the_stored_narrative(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py", "src/b.py"])
    model = _Model()
    executor = _executor(REPOSITORY_REFRESH, store, model)
    first = executor.execute(_subject(g1), max_calls=8)
    assert first.result["status"] == "completed" and first.result["applied"] == 2
    assert first.calls_used == 1 and first.cost_micros == 1000
    narratives = _narratives(store)
    assert {row.subject.key for row in narratives} == {"src/a.py", "src/b.py"}
    assert all(row.support == "ai_narrated" and row.state == "current" for row in narratives)
    assert all(row.synthesis_identity.startswith("synthesis:") for row in narratives)
    again = executor.execute(_subject(g1), max_calls=8)
    assert again.result["status"] == "reused" and again.calls_used == 0 and again.cost_micros == 0
    assert len(model.prompts) == 1


def test_a_new_model_route_or_source_generation_narrates_again(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py", "src/b.py"])
    model = _Model()
    _executor(REPOSITORY_REFRESH, store, model).execute(_subject(g1), max_calls=8)
    first = {row.subject.key: row.synthesis_identity for row in _narratives(store)}

    other_model = _executor(REPOSITORY_REFRESH, store, model, policy=lambda: _policy(driver="openai", model="gpt-2"))
    assert other_model.execute(_subject(g1), max_calls=8).calls_used == 1
    moved = _narratives(store)
    assert len(moved) == 2, "the new narration superseded the old one per subject"
    assert all(row.synthesis_identity != first[row.subject.key] for row in moved)
    superseded = _narratives(store, states=("superseded",))
    assert {row.synthesis_identity for row in superseded} == set(first.values())

    # A newer generation supersedes the narrative of the subject it changed, and only that one.
    g2 = _advance(store, g1, "2", ["src/a.py"])
    assert {row.subject.key for row in _narratives(store)} == {"src/b.py"}
    refresh = _executor(REPOSITORY_REFRESH, store, model, policy=lambda: _policy(driver="openai", model="gpt-2"))
    assert not refresh.is_current(_subject(g1)) and refresh.is_current(_subject(g2))
    assert refresh.execute(_subject(g1), max_calls=8).result["status"] == "superseded"
    outcome = refresh.execute(_subject(g2), max_calls=8)
    assert outcome.result["applied"] == 1 and outcome.calls_used == 1
    keys = [subject["key"] for subject in model.prompts[-1]["subjects"]]
    assert keys == ["src/a.py"]
    current = {row.subject.key: row for row in _narratives(store)}
    assert set(current) == {"src/a.py", "src/b.py"} and current["src/a.py"].valid_from_generation == g2.id


def test_stale_synthesis_cannot_become_current_after_a_newer_generation(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py"])
    newer: list[RepositoryGeneration] = []

    def race(prompt: Mapping[str, Any]) -> Any:
        # The canonical ref advances while the model is answering.
        newer.append(_advance(store, g1, "2", ["src/a.py"]))
        return _grounded(prompt)

    outcome = _executor(REPOSITORY_REFRESH, store, _Model(race)).execute(_subject(g1), max_calls=8)
    assert outcome.result["status"] == "superseded" and outcome.result["stale"] == 1 and outcome.calls_used == 1
    assert _narratives(store) == [] and _narratives(store, states=("superseded",)) == []
    assert store.current_generation(REPO) == newer[0].id


def test_the_store_accepts_narratives_only_for_a_current_source_backed_subject(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py"])
    digest = store.deterministic_digest(REPO)

    def narrative(subject: KnowledgeSubject, generation: str, **extra: Any) -> ProjectKnowledgeEntry:
        row: dict[str, Any] = {
            "repository_id": REPO.id,
            "canonical_ref_id": REF.id,
            "canonical_ref": REF.name,
            "subject": subject,
            "knowledge_kind": SUBJECT_NARRATIVE_KIND,
            "body": "Narrated.",
            "valid_from_generation": generation,
            "last_observed_generation": generation,
            "state": "current",
            "support": "ai_narrated",
            "source_refs": (_source("src/a.py", "a"),),
            "synthesis_identity": "synthesis:" + "0" * 64,
            "updated_at": NOW,
        }
        return ProjectKnowledgeEntry(**{**row, **extra})

    path = KnowledgeSubject("path", "src/a.py")
    assert store.apply_narration(narrative(path, g1.id)) == "applied"
    assert store.apply_narration(narrative(path, g1.id)) == "unchanged"
    assert store.apply_narration(narrative(path, _generation("9").id)) == "stale"
    with pytest.raises(ValueError, match="source-backed"):
        store.apply_narration(narrative(KnowledgeSubject("service", "invented"), g1.id))
    with pytest.raises(ValueError, match="only AI-narrated"):
        store.apply_narration(narrative(path, g1.id, support="deterministic", synthesis_identity=""))
    # A historical narrative needs the generation's evolution event; a narrative is never deterministic state.
    assert store.apply_narration(narrative(path, _generation("8").id, state="historical")) == "stale"
    assert store.deterministic_digest(REPO) == digest
    assert store.generation_delta(REPO, g1.id).introduced == tuple(
        row for row in store.query(REPO).entries if row.support != "ai_narrated"
    )


# --------------------------------------------------------------------------- #
# deterministic knowledge stands without a route, budget or provider
# --------------------------------------------------------------------------- #


def test_provider_failure_keeps_deterministic_knowledge_and_counts_the_call(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py"])
    before = store.query(REPO).entries
    outcome = _executor(REPOSITORY_REFRESH, store, _Model(lambda prompt: InternalLLMError("provider down"))).execute(
        _subject(g1), max_calls=8
    )
    assert outcome.result["status"] == "provider_unavailable"
    assert outcome.calls_used == 1 and outcome.cost_micros is None
    assert store.query(REPO).entries == before and _narratives(store) == []


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (lambda: _policy(enabled=False), "route_unavailable"),
        (lambda: _policy(driver="claude"), "agent_cli_route"),
    ],
)
def test_no_usable_route_means_deterministic_only_with_zero_calls(
    tmp_path: Path, policy: Callable[[], EffectiveIntelligencePolicy], reason: str
) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py"])
    model = _Model()
    outcome = _executor(REPOSITORY_REFRESH, store, model, policy=policy).execute(_subject(g1), max_calls=8)
    assert outcome.result["status"] == "deterministic_only" and outcome.result["reason"].startswith(reason)
    assert outcome.calls_used == 0 and model.prompts == []


def test_unresolvable_content_policy_sends_nothing(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py"])
    model = _Model()

    def broken(subject: Mapping[str, Any]) -> Sequence[ContentAccessRule]:
        raise ValueError("canonical content policy is invalid or unavailable")

    executor = KnowledgeNarrationExecutor(
        REPOSITORY_REFRESH, store=lambda: store, content_access=broken, policy=lambda: _policy(**OPENAI), call=model
    )
    outcome = executor.execute(_subject(g1), max_calls=8)
    assert outcome.result["status"] == "deterministic_only" and model.prompts == []


def test_an_exhausted_background_budget_pauses_before_any_call(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/a.py"])
    root = tmp_path / "jobs"
    bundle = build_sqlite_store_bundle(root)
    bundle.init()
    model = _Model()
    executor = _executor(REPOSITORY_REFRESH, store, model)
    spent = BackgroundBudgetPolicy(monthly_model_calls=0, monthly_managed_micros=None)
    from lemoncrow.pro.capabilities.review.background_budget_store import LocalBackgroundBudgetStore

    scheduler = LocalBackgroundScheduler(
        bundle.jobs,
        budget=LocalBackgroundBudgetStore(root, policy=spent),
        policy=lambda: _policy(**OPENAI),
        executors={REPOSITORY_REFRESH: executor},
    )
    origination = scheduler.originate(REPOSITORY_REFRESH, BackgroundEvent(SOURCE_ADVANCED, g1.id), _subject(g1))
    assert origination.enqueued
    payload = next(
        row["payload"]
        for row in bundle.jobs.list_jobs(job_type=JOB_BACKGROUND_INTELLIGENCE, limit=10)
        if str(row["id"]) == origination.job_id
    )
    with pytest.raises(JobPaused):
        scheduler.run_job(payload)
    assert model.prompts == [] and _narratives(store) == []
    assert len(store.query(REPO).entries) == 1


# --------------------------------------------------------------------------- #
# bounded affected context and content access before dispatch
# --------------------------------------------------------------------------- #


def test_one_small_advance_reads_only_the_affected_subject(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    paths = [f"src/m{index:02d}.py" for index in range(40)]
    g1 = _baseline(store, paths)
    g2 = _advance(store, g1, "2", ["src/m07.py"])
    model = _Model()
    outcome = _executor(REPOSITORY_REFRESH, store, model).execute(_subject(g2), max_calls=8)
    assert outcome.calls_used == 1 and outcome.result["plan"]["subjects"] == 1
    (prompt,) = model.prompts
    assert [row["key"] for row in prompt["subjects"]] == ["src/m07.py"]
    text = json.dumps(prompt)
    assert all(path not in text for path in paths if path != "src/m07.py")
    assert len(prompt["subjects"][0]["facts"]) == 1, "the superseded version is not current context"


class _CountingStore(ProjectKnowledgeStore):
    """Records the subjects each query reads."""

    queried: list[str]

    def query(self, repository: RepositoryIdentity, **kwargs: Any) -> Any:
        spec = kwargs.get("query")
        self.queried.extend(subject.key for subject in (spec.subjects if spec is not None else ()))
        return super().query(repository, **kwargs)


def test_a_large_advance_is_bounded_by_the_call_grant(tmp_path: Path) -> None:
    store = _CountingStore(tmp_path / "pk.sqlite3")
    store.queried = []
    paths = [f"src/m{index:02d}.py" for index in range(30)]
    g1 = _baseline(store, paths)
    g2 = _advance(store, g1, "2", paths)
    model = _Model()
    outcome = _executor(REPOSITORY_REFRESH, store, model).execute(_subject(g2), max_calls=2)
    assert outcome.calls_used == 2 and len(model.prompts) == 2
    assert all(len(prompt["subjects"]) <= MAX_SUBJECTS_PER_CALL for prompt in model.prompts)
    assert outcome.result["plan"]["omitted_subjects"] == 30 - 2 * MAX_SUBJECTS_PER_CALL
    assert outcome.result["applied"] == 2 * MAX_SUBJECTS_PER_CALL
    # Subjects past the bound are counted, never read.
    assert len(store.queried) == 2 * MAX_SUBJECTS_PER_CALL


def test_content_access_filters_entries_before_the_prompt_and_hides_old_narratives(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/public.py", "src/secret.py"])
    model = _Model()
    denied = (_rule("ai_denied", "src/secret.py"),)
    outcome = _executor(REPOSITORY_REFRESH, store, model, rules=denied).execute(_subject(g1), max_calls=8)
    assert outcome.result["plan"]["filtered_by_content_access"] >= 1
    assert "src/secret.py" not in json.dumps(model.prompts)
    assert {row.subject.key for row in _narratives(store)} == {"src/public.py"}

    # Narrated while allowed, then denied: the narrative carries the paths it was built from and is hidden.
    allowed_model = _Model()
    other = ProjectKnowledgeStore(tmp_path / "other.sqlite3")
    g = _baseline(other, ["src/public.py", "src/secret.py"])
    _executor(REPOSITORY_REFRESH, other, allowed_model).execute(_subject(g), max_calls=8)
    narrated = list(other.query(REPO, query=KnowledgeQuery(knowledge_kinds=(SUBJECT_NARRATIVE_KIND,))).entries)
    assert all(set(row.source_paths) == {"src/public.py", "src/secret.py"} for row in narrated)
    hidden = other.query(
        REPO,
        query=KnowledgeQuery(knowledge_kinds=(SUBJECT_NARRATIVE_KIND,)),
        content_access=denied,
        route_trust="external",
    )
    assert hidden.entries == () and hidden.filtered_by_content_access == 2


def test_a_landed_change_gets_a_historical_narrative_linked_to_its_review(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store, ["src/pricing.py", "src/other.py"])
    g2 = _land(store, g1, "2")
    model = _Model()
    executor = _executor(CHANGE_NARRATION, store, model)
    assert executor.is_current(_subject(g2)) and not executor.is_current(_subject(g1))
    # A zero grant spends nothing, even for the single change batch.
    starved = executor.execute(_subject(g2), max_calls=0)
    assert starved.result["status"] == "limited" and starved.calls_used == 0 and model.prompts == []
    outcome = executor.execute(_subject(g2), max_calls=2)
    assert outcome.result["applied"] == 1 and outcome.calls_used == 1
    (narrative,) = _narratives(store, CHANGE_NARRATIVE_KIND)
    assert narrative.state == "historical" and narrative.subject == KnowledgeSubject("repository", "acme/widgets")
    assert narrative.review_refs == ("review:r-42",) and narrative.merge_refs == ("merge:github:acme/widgets#42",)
    assert "src/other.py" not in json.dumps(model.prompts), "only the change's own delta is shown"
    relations = {fact["relation"] for subject in model.prompts[0]["subjects"] for fact in subject["facts"]}
    assert relations == {"introduced", "retired"}
    # Later generations leave the historical narrative alone; the same change narrates once.
    _advance(store, g2, "3", ["src/pricing.py"])
    assert [row.entry_id for row in _narratives(store, CHANGE_NARRATIVE_KIND)] == [narrative.entry_id]
    assert executor.execute(_subject(g2), max_calls=2).result["status"] == "reused"
    # A direct source advance is not a landed Change.
    assert executor.execute(_subject(_generation("3")), max_calls=2).result["status"] == "superseded"


# --------------------------------------------------------------------------- #
# BI2 wiring and the opt-in CLI origination
# --------------------------------------------------------------------------- #


def test_the_registry_runs_knowledge_work_locally_only(tmp_path: Path) -> None:
    for capability_id in (CHANGE_NARRATION, REPOSITORY_REFRESH):
        entry = capability(capability_id)
        assert entry is not None and entry.loci == ("local",) and entry.to_wire()["executor"] == "available"
        event = BackgroundEvent(CHANGE_LANDED, "generation:x")
        hosted = plan_background_work(
            capability_id, event, policy=_policy(**OPENAI), envelope=BackgroundBudgetPolicy(), offered=("managed",)
        )
        assert hosted.reason == SchedulerReason.ROUTE_UNAVAILABLE.value
    assert default_local_executors() == {}
    executors = default_local_executors(tmp_path)
    # DS0's thread summary executor rides beside them.
    assert set(executors) == {CHANGE_NARRATION, REPOSITORY_REFRESH, "review.thread_summary"}
    assert all(
        isinstance(executors[capability_id], KnowledgeNarrationExecutor)
        for capability_id in (CHANGE_NARRATION, REPOSITORY_REFRESH)
    )


def test_origination_maps_the_current_event_and_duplicates_converge(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    root = tmp_path / "jobs"
    bundle = build_sqlite_store_bundle(root)
    bundle.init()
    scheduler = LocalBackgroundScheduler.for_root(bundle.jobs, root, policy=lambda: _policy(**OPENAI))
    kwargs: dict[str, Any] = {
        "repository": REPO,
        "canonical_ref": REF.name,
        "repo_root": tmp_path,
        "access_digest": "rules-x",
    }
    assert originate_knowledge_narration(scheduler, store, **kwargs) == []
    g1 = _baseline(store, ["src/pricing.py"])
    (bootstrap,) = originate_knowledge_narration(scheduler, store, **kwargs)
    assert bootstrap.decision.capability == REPOSITORY_REFRESH and bootstrap.enqueued
    _land(store, g1, "2")
    landed = originate_knowledge_narration(scheduler, store, **kwargs)
    assert [row.decision.capability for row in landed] == [CHANGE_NARRATION, REPOSITORY_REFRESH]
    assert all(row.enqueued for row in landed)
    again = originate_knowledge_narration(scheduler, store, **kwargs)
    assert [row.job_id for row in again] == [row.job_id for row in landed] and not any(row.enqueued for row in again)
    payload = next(
        row["payload"]
        for row in bundle.jobs.list_jobs(job_type=JOB_BACKGROUND_INTELLIGENCE, limit=10)
        if str(row["id"]) == landed[0].job_id
    )
    assert payload["subject"]["repo_root"] == str(tmp_path.resolve())
    assert payload["event"]["content_access_digest"] == "rules-x"


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def test_sync_queues_narration_only_when_asked(tmp_path: Path) -> None:
    from lemoncrow.gateway.cli.commands.knowledge import knowledge_group

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "PK1 Test")
    _git(repo, "config", "user.email", "pk1@example.invalid")
    (repo / "README.md").write_text("Canonical contract.", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    state = tmp_path / "state"
    args = ["project", "sync", "--repo", str(repo), "--repository-key", "pk1", "--canonical-ref", "refs/heads/main"]
    plain = CliRunner().invoke(knowledge_group, args, obj={"root": str(state)})
    assert plain.exit_code == 0, plain.output
    assert "narration" not in json.loads(plain.output)
    asked = CliRunner().invoke(knowledge_group, [*args, "--narrate"], obj={"root": str(state)})
    assert asked.exit_code == 0, asked.output
    (queued,) = json.loads(asked.output)["narration"]["queued"]
    # Built-in BI0/BI1 defaults enable background work; sync itself never calls a model.
    assert queued["capability"] == REPOSITORY_REFRESH and queued["reason"] == SchedulerReason.SCHEDULED.value
    assert queued["enqueued"] is True and queued["locus"] == "local"
    bundle = build_sqlite_store_bundle(state)
    bundle.init()
    jobs = bundle.jobs.list_jobs(job_type=JOB_BACKGROUND_INTELLIGENCE, limit=10)
    assert [str(row["id"]) for row in jobs] == [queued["job_id"]]
    again = CliRunner().invoke(knowledge_group, [*args, "--narrate"], obj={"root": str(state)})
    assert json.loads(again.output)["narration"]["queued"][0]["enqueued"] is False, "a repeat converges"
