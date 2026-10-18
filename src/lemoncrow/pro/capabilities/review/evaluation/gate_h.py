"""Gate H measurement harness (``lc dev eval review gate-h run|record|report|import``).

Measures what :mod:`evidence_qualification` qualifies on, from real runs:

* **flake rate** -- each recipe runs ``runs`` times on each fixed input
  commit; an input is flaky when its runs disagree on the recipe status or on
  any per-test status (:func:`measure_flake`);
* **classification accuracy** -- labeled seeded cases (introduced failure,
  pre-existing failure, fixed, flaky, new, removed, newly skipped, unchanged)
  run with the R5 attempt policy and are classified by R5
  (:func:`differential.classify_recipe`, injectable); accuracy and the
  confusion matrix are per report-format classifier
  (:func:`measure_classification`);
* **visual determinism** -- an R7 surface input is rendered twice per pair
  with the real sandboxed browser (injectable driver) and protected exactly as
  R7 stores it; a pair differs when R7 would raise any aspect as a delta
  (:func:`measure_visual`). Without a usable browser the measurement is
  recorded ``unavailable`` (unmeasured), never fabricated.

Every measurement records the environment class, recipe/surface digest, run
count and cost (wall seconds). Results are versioned under
``evals/review/gate-h/results/<repository>.json``; ``record`` writes the
baseline (``evals/review/gate-h/baseline.json``) like Gate I's.

The corpus is ``evals/review/gate-h/corpus/<repository>/repository.yaml``:
a ``synthetic`` repository (commits written from the manifest) or a
``checkout`` subset of this source checkout at a pinned revision, with
optional seeded edits. Bounds below are refused, never truncated.

Every recipe and surface of a corpus repository is *typed* by an ``expectations``
entry (a manifest with none is untyped, and :func:`report` names each of its subjects): ``qualify`` (it is meant to qualify and counts as qualifying evidence),
``advisory`` (it cannot be honestly qualified yet, and why) or ``negative_control``
(it is nondeterministic or flaky on purpose and must stay disqualified for exactly
the stated reason). :func:`report` checks each expectation against the measured
qualification, so a negative control can never silently start to qualify, count
as qualifying evidence, or be disqualified for an unrelated reason.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import yaml

from lemoncrow.pro.capabilities.review.differential import (
    DIFFERENTIAL_CLASSES,
    AttemptOutcome,
    RecipeDifferential,
    TestReport,
    classify_recipe,
    parse_report,
)
from lemoncrow.pro.capabilities.review.evidence_qualification import (
    GATE_H_CONFIG,
    GateHConfig,
    GateHError,
    Measurement,
    QualificationSubject,
    QualificationThresholds,
    SubjectKey,
    classifier_key,
    environment_class,
    load_gate_h_config,
    qualify,
    render_subject_wire,
)
from lemoncrow.pro.capabilities.review.recipes import (
    REPORT_DIR,
    REPORT_ENV,
    CheckPolicy,
    CheckRecipe,
    parse_config_recipes,
    with_inputs,
)

CORPUS_VERSION: Final[int] = 1
RESULTS_VERSION: Final[int] = 1
GATE_H_DIR_RELATIVE: Final[Path] = Path("evals") / "review"
RECIPE_LABEL: Final[str] = "@recipe"
#: Predicted class for a labeled unit the classifier did not report at all.
ABSENT: Final[str] = "absent"

# Bounds (refused whole when exceeded; never truncated).
# lc-debt: fixed per-corpus ceilings sized for a CI-time measurement run.
# Upgrade: per-repository budgets from gate-h.yaml when organization corpora land.
MAX_REPOSITORIES: Final[int] = 16
MAX_RECIPES: Final[int] = 16
MAX_RUNS_PER_INPUT: Final[int] = 20
MAX_FLAKE_INPUTS: Final[int] = 16
MAX_CASES: Final[int] = 64
MAX_LABELS_PER_CASE: Final[int] = 500
MAX_SURFACES: Final[int] = 8
MAX_RENDER_PAIRS: Final[int] = 10
MAX_COMMITS: Final[int] = 32
MAX_FILE_BYTES: Final[int] = 256 * 1024
MAX_RERUNS: Final[int] = 5

ExecutorKind = Literal["sandbox", "host"]
Classifier = Callable[..., RecipeDifferential]


class GateHHarnessError(ValueError):
    pass


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_gate_h_root(start: Path) -> Path:
    """The nearest ``evals/review`` directory holding ``gate-h.yaml``, walking up from *start*."""

    for directory in (start.resolve(), *start.resolve().parents):
        candidate = directory / GATE_H_DIR_RELATIVE
        if (candidate / GATE_H_CONFIG).is_file():
            return candidate
    raise GateHHarnessError(f"no {GATE_H_DIR_RELATIVE}/{GATE_H_CONFIG} found above {start}; pass --gate-dir")


# --------------------------------------------------------------------------- #
# corpus
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Edit:
    path: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class CommitSpec:
    id: str
    parent: str = ""
    files: Mapping[str, str | None] = field(default_factory=dict)
    edits: tuple[Edit, ...] = ()
    message: str = ""


@dataclass(frozen=True, slots=True)
class CaseSpec:
    id: str
    recipes: tuple[str, ...]
    base: str
    head: str
    labels: Mapping[str, str]
    default: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class SurfaceSpec:
    id: str
    entry: str
    input: str
    render: Mapping[str, Any]
    pairs: int


EXPECTATIONS: Final[tuple[str, ...]] = ("qualify", "advisory", "negative_control")
#: The qualification reasons an ``advisory`` or ``negative_control`` expectation may name.
EXPECTED_REASONS: Final[tuple[str, ...]] = (
    "insufficient_measurement",
    "flaky",
    "classification_inaccurate",
    "visual_nondeterministic",
)
#: What can disqualify a negative control. ``insufficient_measurement`` is not one: a control "met" by never
#: being measured proves nothing.
CONTROL_REASONS: Final[tuple[str, ...]] = tuple(item for item in EXPECTED_REASONS if item != "insufficient_measurement")


@dataclass(frozen=True, slots=True)
class ExpectationSpec:
    """What a corpus subject is for: it qualifies, is not yet qualifiable (and why), or is a negative control."""

    subject: str
    expect: str
    reason: str = ""
    why: str = ""


@dataclass(frozen=True, slots=True)
class HostSpec:
    """An unsandboxed executor for a source-checkout corpus (its results never match a sandboxed class)."""

    path: tuple[str, ...] = ()
    which: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    links: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 600.0


@dataclass(frozen=True, slots=True)
class CorpusRepository:
    id: str
    path: Path
    description: str
    kind: Literal["synthetic", "checkout"]
    commits: tuple[CommitSpec, ...]
    trusted_base: str
    executor: ExecutorKind
    runs: int
    reruns: int
    recipes: tuple[CheckRecipe, ...]
    flake_inputs: Mapping[str, tuple[str, ...]]
    cases: tuple[CaseSpec, ...]
    surfaces: tuple[SurfaceSpec, ...]
    expectations: tuple[ExpectationSpec, ...] = ()
    source: str = ""
    source_revs: tuple[tuple[str, str], ...] = ()
    source_paths: tuple[str, ...] = ()
    host: HostSpec = field(default_factory=HostSpec)
    digest: str = ""

    def recipe(self, recipe_id: str) -> CheckRecipe:
        found = next((item for item in self.recipes if item.id == recipe_id), None)
        if found is None:
            raise GateHHarnessError(f"{self.id}: unknown recipe {recipe_id!r}")
        return found


def _need(raw: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise GateHHarnessError(f"{where}: {key} is required")
    return raw[key]


def _bounded(items: Sequence[Any], limit: int, what: str) -> Sequence[Any]:
    if len(items) > limit:
        raise GateHHarnessError(f"{what}: {len(items)} exceeds the limit of {limit}")
    return items


def _int(raw: Mapping[str, Any], key: str, default: int, low: int, high: int, where: str) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise GateHHarnessError(f"{where}: {key} must be an integer from {low} to {high}")
    return value


def _commit(raw: object, where: str) -> CommitSpec:
    if not isinstance(raw, Mapping):
        raise GateHHarnessError(f"{where} must be a mapping")
    files_raw = raw.get("files") or {}
    if not isinstance(files_raw, Mapping):
        raise GateHHarnessError(f"{where}.files must be a mapping of path -> content (null deletes)")
    files: dict[str, str | None] = {}
    for path, content in files_raw.items():
        text = None if content is None else str(content)
        if text is not None and len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise GateHHarnessError(f"{where}.files[{path}] exceeds {MAX_FILE_BYTES} bytes")
        files[_relative(str(path), f"{where}.files")] = text
    edits = tuple(
        Edit(
            _relative(str(_need(item, "path", f"{where}.edits")), f"{where}.edits"),
            str(_need(item, "old", f"{where}.edits")),
            str(item.get("new") or ""),
        )
        for item in raw.get("edits") or ()
        if isinstance(item, Mapping)
    )
    return CommitSpec(
        id=str(_need(raw, "id", where)),
        parent=str(raw.get("parent") or ""),
        files=files,
        edits=edits,
        message=str(raw.get("message") or ""),
    )


def _relative(path: str, where: str) -> str:
    clean = Path(path)
    if clean.is_absolute() or ".." in clean.parts or not path.strip():
        raise GateHHarnessError(f"{where}: {path!r} must be a repository-relative path")
    return clean.as_posix()


def measured_manifest_text(text: str) -> str:
    """The manifest text with its top-level ``expectations`` block removed.

    Expectations type the evidence (what each subject is for); they do not change what was measured, so
    they stay out of :func:`corpus_digest` and typing a subject never makes recorded results look stale.
    The block runs from the ``expectations:`` line over its indented (or ``- ``) lines and blank lines.
    """

    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.startswith("expectations:")), None)
    if start is None:
        return text
    end = start + 1
    while end < len(lines) and (
        not lines[end].strip() or lines[end][0] in " \t" or lines[end].startswith("- ") or lines[end].startswith("-\n")
    ):
        end += 1
    head, tail = "".join(lines[:start]).rstrip("\n"), "".join(lines[end:])
    return head + "\n" + (tail if tail.strip() else "") if head else tail


def corpus_digest(text: str) -> str:
    """``corpus:sha256:`` of the manifest as measured (everything but its ``expectations`` block)."""

    return "corpus:sha256:" + hashlib.sha256(measured_manifest_text(text).encode("utf-8")).hexdigest()


def _expectations(raw: object, where: str, subjects: set[str]) -> tuple[ExpectationSpec, ...]:
    """Typed expectations: one per recipe and surface, none unknown, a reason and a why where one is owed.

    A manifest written before expectations existed has none (loads untyped: :func:`check_expectations`
    reports every one of its subjects); one that types some must type all.
    """

    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise GateHHarnessError(f"{where}: expectations must list one entry per recipe and surface")
    found: dict[str, ExpectationSpec] = {}
    for index, item in enumerate(_bounded(raw, MAX_RECIPES + MAX_SURFACES, f"{where} expectations")):
        spot = f"{where}.expectations[{index}]"
        if not isinstance(item, Mapping):
            raise GateHHarnessError(f"{spot} must be a mapping")
        subject, expect = str(_need(item, "subject", spot)), str(_need(item, "expect", spot))
        reason, why = str(item.get("reason") or ""), str(item.get("why") or "").strip()
        if expect not in EXPECTATIONS:
            raise GateHHarnessError(f"{spot}: expect must be one of {', '.join(EXPECTATIONS)}")
        if subject not in subjects:
            raise GateHHarnessError(f"{spot}: unknown subject {subject!r}")
        if subject in found:
            raise GateHHarnessError(f"{spot}: {subject} is typed twice")
        if expect == "qualify" and (reason or why):
            raise GateHHarnessError(f"{spot}: a qualify expectation names no reason or why")
        allowed = EXPECTED_REASONS if expect == "advisory" else CONTROL_REASONS
        if expect != "qualify" and (reason not in allowed or not why):
            raise GateHHarnessError(f"{spot}: {expect} needs reason (one of {', '.join(allowed)}) and a why")
        found[subject] = ExpectationSpec(subject, expect, reason, why)
    untyped = sorted(subjects - set(found))
    if untyped:
        raise GateHHarnessError(f"{where}: untyped subject(s): {', '.join(untyped)}")
    return tuple(found[key] for key in sorted(found))


def load_repository(path: Path) -> CorpusRepository:
    """One ``repository.yaml``; anything invalid or over a bound is refused."""

    manifest = path / "repository.yaml"
    try:
        text = manifest.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise GateHHarnessError(f"{manifest}: {exc}") from exc
    where = str(manifest)
    if not isinstance(raw, Mapping) or raw.get("version") != CORPUS_VERSION:
        raise GateHHarnessError(f"{where}: a mapping with version: {CORPUS_VERSION} is required")
    repo_id = str(_need(raw, "id", where))
    repository = raw.get("repository") if isinstance(raw.get("repository"), Mapping) else {}
    assert isinstance(repository, Mapping)
    kind = repository.get("kind")
    if kind not in {"synthetic", "checkout"}:
        raise GateHHarnessError(f"{where}: repository.kind must be synthetic or checkout")
    commits = tuple(
        _commit(item, f"{where}.repository.commits[{index}]")
        for index, item in enumerate(_bounded(list(repository.get("commits") or ()), MAX_COMMITS, f"{where} commits"))
    )
    revs_raw = repository.get("revs") if isinstance(repository.get("revs"), Mapping) else {}
    assert isinstance(revs_raw, Mapping)
    if kind == "checkout" and not revs_raw and repository.get("rev"):
        revs_raw = {"source": repository["rev"]}
    source_revs = tuple((str(name), str(rev)) for name, rev in revs_raw.items()) if kind == "checkout" else ()
    _bounded(list(source_revs), MAX_COMMITS, f"{where} revs")
    ids = [*(name for name, _rev in source_revs), *(item.id for item in commits)]
    if len(set(ids)) != len(ids):
        raise GateHHarnessError(f"{where}: revision and commit ids must be unique")
    known = {name for name, _rev in source_revs}
    for item in commits:
        if item.parent and item.parent not in known:
            raise GateHHarnessError(f"{where}: commit {item.id} names unknown or later parent {item.parent!r}")
        if kind == "synthetic" and not item.parent and known:
            raise GateHHarnessError(f"{where}: only the first synthetic commit may have no parent")
        known.add(item.id)
    if kind == "synthetic" and not commits:
        raise GateHHarnessError(f"{where}: a synthetic repository needs commits")
    executor = raw.get("executor", "sandbox")
    if executor not in {"sandbox", "host"}:
        raise GateHHarnessError(f"{where}: executor must be sandbox or host")
    recipes_raw = _bounded(list(raw.get("recipes") or ()), MAX_RECIPES, f"{where} recipes")
    recipes, errors = parse_config_recipes(yaml.safe_dump({"check_recipes": recipes_raw}))
    if errors:
        raise GateHHarnessError(f"{where}: invalid recipes: " + "; ".join(errors))
    flake_raw = raw.get("flake") if isinstance(raw.get("flake"), Mapping) else {}
    assert isinstance(flake_raw, Mapping)
    recipe_ids = {item.id for item in recipes}
    flake_inputs: dict[str, tuple[str, ...]] = {}
    for recipe_id, inputs in flake_raw.items():
        if str(recipe_id) not in recipe_ids:
            raise GateHHarnessError(f"{where}: flake names unknown recipe {recipe_id!r}")
        chosen = tuple(str(item) for item in _bounded(list(inputs or ()), MAX_FLAKE_INPUTS, f"{where} flake inputs"))
        unknown = [item for item in chosen if item not in known]
        if unknown:
            raise GateHHarnessError(f"{where}: flake inputs name unknown commits {unknown}")
        flake_inputs[str(recipe_id)] = chosen
    cases: list[CaseSpec] = []
    for index, item in enumerate(_bounded(list(raw.get("cases") or ()), MAX_CASES, f"{where} cases")):
        spot = f"{where}.cases[{index}]"
        if not isinstance(item, Mapping):
            raise GateHHarnessError(f"{spot} must be a mapping")
        labels = item.get("labels") or {}
        if not isinstance(labels, Mapping):
            raise GateHHarnessError(f"{spot}.labels must map a unit to its expected class")
        _bounded(list(labels), MAX_LABELS_PER_CASE, f"{spot} labels")
        allowed = set(DIFFERENTIAL_CLASSES)
        bad = sorted({str(value) for value in labels.values()} - allowed)
        default = str(item.get("default") or "")
        if bad or (default and default not in allowed):
            raise GateHHarnessError(f"{spot}: unknown class(es) {bad or [default]}")
        case_recipes = item.get("recipes") or ([item["recipe"]] if item.get("recipe") else [])
        case = CaseSpec(
            id=str(_need(item, "id", spot)),
            recipes=tuple(str(value) for value in case_recipes),
            base=str(_need(item, "base", spot)),
            head=str(_need(item, "head", spot)),
            labels={str(key): str(value) for key, value in labels.items()},
            default=default,
            note=str(item.get("note") or ""),
        )
        if not case.recipes or set(case.recipes) - recipe_ids or {case.base, case.head} - known:
            raise GateHHarnessError(f"{spot}: names unknown recipes or commits")
        cases.append(case)
    surfaces: list[SurfaceSpec] = []
    for index, item in enumerate(_bounded(list(raw.get("surfaces") or ()), MAX_SURFACES, f"{where} surfaces")):
        spot = f"{where}.surfaces[{index}]"
        if not isinstance(item, Mapping) or not isinstance(item.get("render"), Mapping):
            raise GateHHarnessError(f"{spot} needs a render block")
        entry = str(item.get("entry") or "visual.dom_a11y")
        if entry not in {"visual.dom_a11y", "visual.matrix"}:
            raise GateHHarnessError(f"{spot}: entry must be visual.dom_a11y or visual.matrix")
        surface = SurfaceSpec(
            id=str(_need(item, "id", spot)),
            entry=entry,
            input=str(_need(item, "input", spot)),
            render=dict(item["render"]),
            pairs=_int(item, "pairs", 3, 1, MAX_RENDER_PAIRS, spot),
        )
        if surface.input not in known:
            raise GateHHarnessError(f"{spot}: unknown input commit {surface.input!r}")
        surfaces.append(surface)
    expectations = _expectations(
        raw.get("expectations"),
        where,
        {f"checks.recipe:{item.id}" for item in recipes} | {f"surface:{item.id}" for item in surfaces},
    )
    host_raw = raw.get("host") if isinstance(raw.get("host"), Mapping) else {}
    assert isinstance(host_raw, Mapping)
    host = HostSpec(
        path=tuple(str(item) for item in host_raw.get("path") or ()),
        which=tuple(str(item) for item in host_raw.get("which") or ()),
        env={str(key): str(value) for key, value in (host_raw.get("env") or {}).items()},
        links={
            _relative(str(key), f"{where}.host.links"): str(value)
            for key, value in (host_raw.get("links") or {}).items()
        },
        timeout=float(host_raw.get("timeout") or 600.0),
    )
    source_paths = tuple(_relative(str(item), f"{where}.repository.paths") for item in repository.get("paths") or ())
    if kind == "checkout" and (not source_revs or not source_paths):
        raise GateHHarnessError(f"{where}: a checkout repository needs revs (or rev) and paths")
    trusted = str(raw.get("trusted_base") or (source_revs[0][0] if kind == "checkout" else commits[0].id))
    if trusted not in known:
        raise GateHHarnessError(f"{where}: trusted_base names unknown commit {trusted!r}")
    return CorpusRepository(
        id=repo_id,
        path=path,
        description=str(raw.get("description") or ""),
        kind=kind,
        commits=commits,
        trusted_base=trusted,
        executor=executor,
        runs=_int(raw, "runs", 5, 2, MAX_RUNS_PER_INPUT, where),
        reruns=_int(raw, "reruns", 2, 0, MAX_RERUNS, where),
        recipes=recipes,
        flake_inputs=flake_inputs,
        cases=tuple(cases),
        surfaces=tuple(surfaces),
        expectations=expectations,
        source=str(repository.get("source") or "."),
        source_revs=source_revs,
        source_paths=source_paths,
        host=host,
        digest=corpus_digest(text),
    )


def load_corpus(gate_dir: Path, config: GateHConfig, *, only: Sequence[str] = ()) -> list[CorpusRepository]:
    root = gate_dir / config.corpus
    paths = sorted(item for item in root.iterdir() if (item / "repository.yaml").is_file()) if root.is_dir() else []
    _bounded(paths, MAX_REPOSITORIES, f"{root} repositories")
    repositories = [load_repository(item) for item in paths]
    if only:
        missing = sorted(set(only) - {item.id for item in repositories})
        if missing:
            raise GateHHarnessError(f"unknown corpus repositories: {', '.join(missing)}")
        repositories = [item for item in repositories if item.id in set(only)]
    return repositories


# --------------------------------------------------------------------------- #
# materialization
# --------------------------------------------------------------------------- #

_GIT_ENV: Final[Mapping[str, str]] = {
    "GIT_AUTHOR_NAME": "Gate H",
    "GIT_AUTHOR_EMAIL": "gate-h@lemoncrow.invalid",
    "GIT_COMMITTER_NAME": "Gate H",
    "GIT_COMMITTER_EMAIL": "gate-h@lemoncrow.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        check=False,
        env={**os.environ, **_GIT_ENV},
    )
    if proc.returncode != 0:
        raise GateHHarnessError(f"git {' '.join(args[:3])} failed: {proc.stderr.decode(errors='replace')[:400]}")
    return proc.stdout.decode("utf-8", errors="replace").strip()


@dataclass(frozen=True, slots=True)
class MaterializedRepository:
    root: Path
    shas: Mapping[str, str]

    def sha(self, commit: str) -> str:
        return self.shas[commit]

    def read(self, commit: str, path: str) -> str | None:
        proc = subprocess.run(
            ["git", "show", f"{self.shas[commit]}:{path}"], cwd=self.root, capture_output=True, check=False
        )
        return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else None

    def export(self, commit: str, target: Path) -> Path:
        """The commit's tree as plain files under *target*."""

        target.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(["git", "archive", self.shas[commit]], cwd=self.root, capture_output=True, check=False)
        if archive.returncode != 0:
            raise GateHHarnessError(f"git archive {commit} failed")
        subprocess.run(["tar", "-x", "-C", str(target)], input=archive.stdout, check=True)
        return target


def materialize(repository: CorpusRepository, work: Path, *, source_root: Path) -> MaterializedRepository:
    """A fresh git repository holding the corpus commits (a checkout subset is exported first)."""

    root = work / repository.id
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    shas: dict[str, str] = {}
    if repository.kind == "checkout":
        # Each pinned revision is one commit holding exactly that revision's exported paths.
        source = (source_root / repository.source).resolve()
        for name, rev in repository.source_revs:
            full = _git(source, "rev-parse", "--verify", f"{rev}^{{commit}}")
            archive = subprocess.run(
                ["git", "archive", full, "--", *repository.source_paths], cwd=source, capture_output=True, check=False
            )
            if archive.returncode != 0:
                raise GateHHarnessError(f"{repository.id}: git archive of {full} failed: {archive.stderr[:300]!r}")
            for child in root.iterdir():
                if child.name != ".git":
                    shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
            subprocess.run(["tar", "-x", "-C", str(root)], input=archive.stdout, check=True)
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "--allow-empty", "-m", f"{name} {full}")
            shas[name] = _git(root, "rev-parse", "HEAD")
    for commit in repository.commits:
        if commit.parent:
            _git(root, "checkout", "-q", "--detach", shas[commit.parent])
        elif shas:
            raise GateHHarnessError(f"{repository.id}: commit {commit.id} needs a parent")
        for path, content in commit.files.items():
            target = root / path
            if content is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for edit in commit.edits:
            target = root / edit.path
            text = target.read_text(encoding="utf-8") if target.is_file() else ""
            if text.count(edit.old) != 1:
                raise GateHHarnessError(
                    f"{repository.id}: commit {commit.id} edit of {edit.path} must match exactly once"
                )
            target.write_text(text.replace(edit.old, edit.new), encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "--allow-empty", "-m", commit.message or commit.id)
        shas[commit.id] = _git(root, "rev-parse", "HEAD")
    return MaterializedRepository(root, shas)


# --------------------------------------------------------------------------- #
# executors
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: Literal["passed", "failed", "not_run"]
    report: TestReport | None
    environment: str
    executor_class: str
    image_digest: str
    seconds: float
    reason: str = ""
    toolchain: Mapping[str, str] = field(default_factory=dict)

    @property
    def environment_class(self) -> str:
        return environment_class(self.executor_class, self.image_digest)

    def statuses(self) -> dict[str, str]:
        return dict(self.report.statuses()) if self.report is not None and self.report.complete else {}


class RecipeExecutor(Protocol):
    name: str

    def run(self, recipe: CheckRecipe, repository: MaterializedRepository, commit: str) -> RunOutcome: ...


class SandboxExecutor:
    """The product's sandboxed command runner (bubblewrap, no network): what R5 executes locally."""

    name = "sandbox"

    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch

    def run(self, recipe: CheckRecipe, repository: MaterializedRepository, commit: str) -> RunOutcome:
        from lemoncrow.pro.capabilities.review.check_runs import recipe_runtime
        from lemoncrow.pro.capabilities.review.execution import ExecutionResult
        from lemoncrow.pro.capabilities.review.runtimes import CommandRunner
        from lemoncrow.pro.capabilities.review.session_models import ReviewRevision
        from lemoncrow.pro.capabilities.review.surfaces import ReviewSurfaceConfig, SurfaceContext

        sha = repository.sha(commit)
        revision = ReviewRevision(
            id=hashlib.sha256(f"gate-h:{sha}".encode()).hexdigest()[:32],
            review_id="0" * 32,
            revision_number=1,
            range_mode="commit_range",
            tree_fingerprint=sha,
            packet_schema_version=1,
            base_sha=sha,
            head_sha=sha,
        )
        context = SurfaceContext(
            repo_root=repository.root,
            store_root=self.scratch / "store",
            revision=revision,
            changed_paths=(),
            config=ReviewSurfaceConfig(),
            source_root=repository.root,
        )
        runtime, surface = recipe_runtime(recipe)
        started = time.monotonic()
        result = CommandRunner().run(context, runtime, surface, side="new")
        seconds = time.monotonic() - started
        data = result.data if isinstance(result.data, Mapping) else {}
        environment, executor, image, toolchain = "", "", "", {}
        raw = data.get("execution")
        if isinstance(raw, Mapping):
            try:
                parsed = ExecutionResult.from_wire(raw)
                environment = parsed.environment_fingerprint
                executor = parsed.executor_class.value
                image = parsed.verification.image_digest
                toolchain = dict(parsed.fingerprint.toolchain)
            except (TypeError, ValueError):
                pass
        if result.status == "unavailable":
            return RunOutcome(
                "not_run", None, environment, executor, image, seconds, str(result.summary)[:300], toolchain
            )
        report_text = data.get("report")
        report = parse_report(
            recipe.report,
            report_text if isinstance(report_text, str) else None,
            truncated=bool(data.get("report_truncated")),
        )
        status: Literal["passed", "failed"] = "passed" if result.status == "passed" else "failed"
        return RunOutcome(status, report, environment, executor, image, seconds, "", toolchain)


class HostExecutor:
    """Runs a recipe unsandboxed on this host in an exported tree (a source-checkout corpus).

    Its environment class is ``host_unsandboxed@...``: a measurement taken this
    way can never qualify a sandboxed product result.
    """

    name = "host"
    executor_class = "host_unsandboxed"

    def __init__(self, spec: HostSpec, *, source_root: Path, scratch: Path) -> None:
        self.spec = spec
        self.source_root = source_root
        self.scratch = scratch
        self._trees: dict[tuple[str, str], Path] = {}
        self._toolchain: dict[str, str] | None = None

    def _tree(self, repository: MaterializedRepository, commit: str) -> Path:
        key = (str(repository.root), commit)
        if key not in self._trees:
            tree = repository.export(commit, self.scratch / "trees" / f"{repository.root.name}-{commit}")
            for inside, outside in self.spec.links.items():
                link = tree / inside
                link.parent.mkdir(parents=True, exist_ok=True)
                if not link.exists():
                    link.symlink_to((self.source_root / outside).resolve())
            self._trees[key] = tree
        return self._trees[key]

    def toolchain(self) -> dict[str, str]:
        if self._toolchain is None:
            found: dict[str, str] = {}
            for name in ("python", "node"):
                binary = shutil.which(name, path=self._path())
                if binary:
                    proc = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False)
                    found[name] = (proc.stdout or proc.stderr).strip()[:80]
            locks = [self.source_root / name for name in ("uv.lock", "frontend/package-lock.json")]
            found["lockfiles"] = _digest([_file_digest(path) for path in locks])
            self._toolchain = found
        return self._toolchain

    def _path(self) -> str:
        parts = [str((self.source_root / item).resolve()) for item in self.spec.path]
        for name in self.spec.which:
            binary = shutil.which(name)
            if binary:
                parts.append(str(Path(binary).parent))
        return os.pathsep.join([*parts, "/usr/bin", "/bin"])

    def run(self, recipe: CheckRecipe, repository: MaterializedRepository, commit: str) -> RunOutcome:
        from lemoncrow.pro.capabilities.review.hermetic import runtime_image_digest

        tree = self._tree(repository, commit)
        reports = Path(tempfile.mkdtemp(prefix="gate-h-report-", dir=self.scratch))
        home = Path(tempfile.mkdtemp(prefix="gate-h-home-", dir=self.scratch))
        argv = [part.replace(REPORT_DIR, str(reports)) for part in recipe.argv(None)]
        env = {
            "PATH": self._path(),
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            **{key: value.replace(REPORT_DIR, str(reports)) for key, value in recipe.env},
            REPORT_ENV: str(reports),
            **{key: value.replace("{tree}", str(tree)) for key, value in self.spec.env.items()},
        }
        cwd = tree / recipe.cwd if recipe.cwd not in {"", "."} else tree
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                capture_output=True,
                timeout=min(self.spec.timeout, float(recipe.timeout or self.spec.timeout)),
                check=False,
            )
            code: int | None = proc.returncode
            reason = ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, reason = None, f"could not run: {exc}"[:300]
        seconds = time.monotonic() - started
        image = runtime_image_digest()
        toolchain = self.toolchain()
        environment = "hostenv:sha256:" + _digest({"image": image, "toolchain": toolchain, "recipe": recipe.digest})
        try:
            if code is None:
                return RunOutcome("not_run", None, environment, self.executor_class, image, seconds, reason, toolchain)
            report_file = reports / "report"
            text = report_file.read_text(encoding="utf-8", errors="replace") if report_file.is_file() else None
            report = parse_report(recipe.report, text)
            status: Literal["passed", "failed"] = "passed" if code == 0 else "failed"
            return RunOutcome(status, report, environment, self.executor_class, image, seconds, "", toolchain)
        finally:
            shutil.rmtree(reports, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "absent"


# --------------------------------------------------------------------------- #
# measurements
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Cost:
    executions: int = 0
    seconds: float = 0.0

    def add(self, outcome: RunOutcome) -> None:
        self.executions += 1
        self.seconds += outcome.seconds

    def to_wire(self) -> dict[str, Any]:
        return {"executions": self.executions, "seconds": round(self.seconds, 3)}


def _fingerprints(outcomes: Sequence[RunOutcome]) -> tuple[str, ...]:
    return tuple(sorted({item.environment for item in outcomes if item.environment}))


def _single_class(outcomes: Sequence[RunOutcome], what: str) -> str:
    classes = {item.environment_class for item in outcomes if item.status != "not_run"}
    if len(classes) > 1:
        raise GateHHarnessError(f"{what}: runs crossed environment classes {sorted(classes)}")
    return next(iter(classes), "")


def measure_flake(
    recipe: CheckRecipe,
    inputs: Sequence[str],
    *,
    runs: int,
    executor: RecipeExecutor,
    repository: MaterializedRepository,
    cost: Cost | None = None,
) -> tuple[Measurement, dict[str, Any]]:
    """Run *recipe* ``runs`` times per input; an input is flaky when its executed runs disagree."""

    _bounded(list(inputs), MAX_FLAKE_INPUTS, f"{recipe.id} flake inputs")
    if not 2 <= runs <= MAX_RUNS_PER_INPUT:
        raise GateHHarnessError(f"runs must be from 2 to {MAX_RUNS_PER_INPUT}")
    spent = Cost()
    outcomes_all: list[RunOutcome] = []
    detail: dict[str, Any] = {}
    observed = flaky = 0
    min_runs = 0
    unavailable: list[str] = []
    for commit in inputs:
        outcomes = [executor.run(recipe, repository, commit) for _ in range(runs)]
        for item in outcomes:
            spent.add(item)
        outcomes_all.extend(outcomes)
        executed = [item for item in outcomes if item.status != "not_run"]
        signatures = {json.dumps([item.status, item.statuses()], sort_keys=True) for item in executed}
        disagree = len(signatures) > 1
        if len(executed) >= 2:
            observed += 1
            flaky += int(disagree)
            min_runs = len(executed) if min_runs == 0 else min(min_runs, len(executed))
        else:
            unavailable.append(next((item.reason for item in outcomes if item.reason), "fewer than 2 runs executed"))
        unstable = sorted(
            {
                test
                for item in executed
                for test in item.statuses()
                if len({other.statuses().get(test, "missing") for other in executed}) > 1
            }
        )
        detail[commit] = {
            "sha": repository.sha(commit),
            "runs": len(outcomes),
            "executed": len(executed),
            "statuses": [item.status for item in outcomes],
            "tests": len(executed[0].statuses()) if executed else 0,
            "disagree": disagree,
            "unstable_tests": unstable[:50],
            "seconds": round(sum(item.seconds for item in outcomes), 3),
        }
    if cost is not None:
        cost.executions += spent.executions
        cost.seconds += spent.seconds
    env = _single_class(outcomes_all, recipe.id)
    measurement = Measurement(
        key=SubjectKey("flake", recipe.digest, env),
        subject=f"recipe:{recipe.id}",
        observations=observed,
        failures=flaky,
        runs=len(outcomes_all),
        min_runs_per_observation=min_runs,
        unavailable="" if observed else ("; ".join(dict.fromkeys(unavailable)) or "no input was measured")[:500],
        breakdown={"inputs": detail},
        fingerprints=_fingerprints(outcomes_all),
        cost_seconds=spent.seconds,
        measured_at=_now(),
    )
    return measurement, {"recipe": recipe.id, "digest": recipe.digest, "environment_class": env, **detail}


def _attempts(
    recipe: CheckRecipe,
    side: Literal["old", "new"],
    commit: str,
    *,
    reruns: int,
    executor: RecipeExecutor,
    repository: MaterializedRepository,
    spent: Cost,
) -> tuple[list[AttemptOutcome], list[RunOutcome]]:
    """R5's attempt policy (``check_runs._Run.side_attempts``): run, then rerun while failed."""

    attempts: list[AttemptOutcome] = []
    outcomes: list[RunOutcome] = []
    while True:
        outcome = executor.run(recipe, repository, commit)
        spent.add(outcome)
        outcomes.append(outcome)
        attempts.append(
            AttemptOutcome(
                side=side,
                attempt=len(attempts) + 1,
                status=outcome.status,
                reason=outcome.reason,
                duration_ms=int(outcome.seconds * 1000),
                environment=outcome.environment,
                executor_class=outcome.executor_class,
                report=outcome.report,
            )
        )
        if outcome.status != "failed" or len(attempts) > reruns:
            return attempts, outcomes


def _match_labels(case: CaseSpec, units: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """``unit -> expected`` for every labeled unit (a key matches an id or its ``::name`` suffix)."""

    expected: dict[str, str] = {}
    problems: list[str] = []
    for key, klass in case.labels.items():
        if key == RECIPE_LABEL:
            expected[RECIPE_LABEL] = klass
            continue
        hits = [unit for unit in units if unit == key or unit.endswith("::" + key)]
        if len(hits) > 1:
            raise GateHHarnessError(f"case {case.id}: label {key!r} is ambiguous ({hits[:3]})")
        if not hits:
            expected[key] = klass  # labeled but not reported: predicted "absent"
            continue
        expected[hits[0]] = klass
    if case.default:
        for unit in units:
            if unit != RECIPE_LABEL:
                expected.setdefault(unit, case.default)
    return expected, problems


def measure_classification(
    repository_spec: CorpusRepository,
    *,
    executor: RecipeExecutor,
    repository: MaterializedRepository,
    classifier: Classifier = classify_recipe,
    cost: Cost | None = None,
) -> tuple[list[Measurement], list[dict[str, Any]]]:
    """Classify each labeled case with R5 and compare with its labels, per report-format classifier."""

    policy = CheckPolicy(reruns=repository_spec.reruns)
    per_classifier: dict[str, dict[str, Any]] = {}
    details: list[dict[str, Any]] = []
    for case in repository_spec.cases:
        for recipe_id in case.recipes:
            recipe = repository_spec.recipe(recipe_id)
            spent = Cost()
            head, head_runs = _attempts(
                recipe, "new", case.head, reruns=policy.reruns, executor=executor, repository=repository, spent=spent
            )
            base, base_runs = _attempts(
                recipe, "old", case.base, reruns=policy.reruns, executor=executor, repository=repository, spent=spent
            )
            if cost is not None:
                cost.executions += spent.executions
                cost.seconds += spent.seconds
            differential = classifier(recipe, [*base, *head], policy=policy, sides=("old", "new"))
            units = {unit.subject: unit.klass for unit in differential.tests}
            units[RECIPE_LABEL] = differential.recipe_unit.klass
            expected, _problems = _match_labels(case, units)
            key = classifier_key(recipe.report)
            bucket = per_classifier.setdefault(
                key,
                {"labeled": 0, "wrong": 0, "unlabeled": 0, "confusion": {}, "runs": 0, "seconds": 0.0, "envs": set()},
            )
            rows = []
            for unit, klass in sorted(expected.items()):
                predicted = units.get(unit, ABSENT)
                bucket["labeled"] += 1
                bucket["wrong"] += int(predicted != klass)
                row = bucket["confusion"].setdefault(klass, {})
                row[predicted] = row.get(predicted, 0) + 1
                rows.append({"unit": unit, "expected": klass, "predicted": predicted})
            bucket["unlabeled"] += sum(1 for unit in units if unit not in expected)
            bucket["runs"] += spent.executions
            bucket["seconds"] += spent.seconds
            bucket["envs"].update(item.environment for item in [*head_runs, *base_runs] if item.environment)
            details.append(
                {
                    "case": case.id,
                    "recipe": recipe.id,
                    "classifier": key,
                    "granularity": differential.granularity,
                    "head_attempts": [item.status for item in head],
                    "base_attempts": [item.status for item in base],
                    "units": rows,
                    "wrong": [row for row in rows if row["expected"] != row["predicted"]],
                    "unlabeled": sorted(unit for unit in units if unit not in expected)[:50],
                    "runs": spent.executions,
                    "seconds": round(spent.seconds, 3),
                }
            )
    measurements = [
        Measurement(
            key=SubjectKey("classification", key),
            subject=key,
            observations=bucket["labeled"],
            failures=bucket["wrong"],
            runs=bucket["runs"],
            confusion=bucket["confusion"],
            breakdown={"unlabeled_units": bucket["unlabeled"], "repository": repository_spec.id},
            fingerprints=tuple(sorted(bucket["envs"])),
            cost_seconds=bucket["seconds"],
            measured_at=_now(),
        )
        for key, bucket in sorted(per_classifier.items())
    ]
    return measurements, details


class RenderDriver(Protocol):
    def environment(self) -> Any: ...

    def capture(
        self, site: Path, cells: Sequence[Any], controls: Any, *, spa_fallback: bool, timeout: float
    ) -> list[Any]: ...


def measure_visual(
    surface: SurfaceSpec,
    *,
    repository: MaterializedRepository,
    driver: RenderDriver,
    scratch: Path,
    executor_class: str = "author_machine",
    image_digest: str | None = None,
    timeout: float = 180.0,
) -> tuple[Measurement, dict[str, Any]]:
    """Render one surface input twice per pair, protected as R7 stores it; any R7 delta is nondeterminism."""

    from lemoncrow.pro.capabilities.review.hermetic import runtime_image_digest
    from lemoncrow.pro.capabilities.review.render_diff import (
        RouteSelection,
        page_changes,
        parse_render_config,
        plan_cells,
        protect_page,
    )
    from lemoncrow.pro.capabilities.review.render_driver import PageCapture

    if not 1 <= surface.pairs <= MAX_RENDER_PAIRS:
        raise GateHHarnessError(f"{surface.id}: pairs must be from 1 to {MAX_RENDER_PAIRS}")
    config = parse_render_config(surface.render)
    if config.errors:
        raise GateHHarnessError(f"{surface.id}: invalid render block: {'; '.join(config.errors)}")
    environment = driver.environment()
    image = image_digest if image_digest is not None else runtime_image_digest()
    subject = render_subject_wire(
        surface.entry,
        config.digest,
        browser=str(getattr(environment, "browser", "") or ""),
        fonts_digest=str(getattr(environment, "fonts_digest", "") or ""),
        sandbox=str(getattr(environment, "sandbox", "") or ""),
        executor_class=executor_class,
        image_digest=image,
    )
    digest = subject["digest"] or "surface:unavailable:" + _digest({"entry": surface.entry, "config": config.digest})
    key = SubjectKey("visual", digest, subject["environment_class"])
    if not getattr(environment, "available", False):
        reason = str(getattr(environment, "reason", "") or "the renderer is unavailable")
        return (
            Measurement(key, f"surface:{surface.id}", 0, 0, unavailable=reason[:500], measured_at=_now()),
            {"surface": surface.id, "unavailable": reason, "subject": subject},
        )
    selection = RouteSelection(routes=config.routes, mapping="known")
    plan = plan_cells(config, selection, entry=surface.entry, budget_seconds=None)  # type: ignore[arg-type]
    tree = repository.export(surface.input, scratch / f"site-{surface.id}")
    site = tree / config.static_root if config.static_root else tree
    observed = differing = runs = 0
    seconds = 0.0
    pairs: list[dict[str, Any]] = []
    failures: list[str] = []

    def render() -> dict[str, Any]:
        nonlocal runs, seconds
        started = time.monotonic()
        captures = driver.capture(site, plan.cells, config.controls, spa_fallback=config.spa, timeout=timeout)
        seconds += time.monotonic() - started
        runs += 1
        by_id = {capture.cell_id: capture for capture in captures}
        return {
            cell.id: protect_page(
                by_id.get(cell.id) or PageCapture(cell.id, "failed", "the driver returned no capture"),
                cell,
                review_id="gate-h",
                artifacts=None,
                policy=None,
            )
            for cell in plan.cells
        }

    for index in range(surface.pairs):
        first, second = render(), render()
        for cell in plan.cells:
            one, two = first[cell.id], second[cell.id]
            if one.status != "captured" and two.status != "captured":
                failures.append(one.reason or two.reason or "capture failed")
                continue
            changes = page_changes(one, two)
            observed += 1
            differing += int(bool(changes))
            pairs.append({"pair": index + 1, "cell": cell.id, "differs": sorted(changes)})
    unavailable = "" if observed else ("; ".join(dict.fromkeys(failures)) or "nothing was captured")[:500]
    measurement = Measurement(
        key=key,
        subject=f"surface:{surface.id}",
        observations=observed,
        failures=differing,
        runs=runs,
        min_runs_per_observation=2,
        unavailable=unavailable,
        breakdown={"cells": [cell.id for cell in plan.cells], "pairs": pairs},
        fingerprints=(digest,),
        cost_seconds=seconds,
        measured_at=_now(),
    )
    return measurement, {"surface": surface.id, "subject": subject, "pairs": pairs, "runs": runs}


# --------------------------------------------------------------------------- #
# one repository, results and the baseline
# --------------------------------------------------------------------------- #


@contextmanager
def _scratch(prefix: str) -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def run_repository(
    repository: CorpusRepository,
    *,
    source_root: Path,
    driver: RenderDriver | None = None,
    classifier: Classifier = classify_recipe,
    executor: RecipeExecutor | None = None,
    runs: int | None = None,
    parts: Sequence[str] = ("flake", "classification", "visual"),
) -> dict[str, Any]:
    """Measure one corpus repository and return its versioned result document."""

    with _scratch("lemoncrow-gate-h-") as work:
        materialized = materialize(repository, work / "repos", source_root=source_root)
        recipes = tuple(
            with_inputs(recipe, lambda path: materialized.read(repository.trusted_base, path))
            for recipe in repository.recipes
        )
        spec = replace(repository, recipes=recipes)
        chosen: RecipeExecutor = executor or (
            HostExecutor(repository.host, source_root=source_root, scratch=work)
            if repository.executor == "host"
            else SandboxExecutor(work)
        )
        cost = Cost()
        measurements: list[Measurement] = []
        flake: list[dict[str, Any]] = []
        classification: list[dict[str, Any]] = []
        visual: list[dict[str, Any]] = []
        if "flake" in parts:
            for recipe in recipes:
                inputs = spec.flake_inputs.get(recipe.id, ())
                if not inputs:
                    continue
                measured, detail = measure_flake(
                    recipe, inputs, runs=runs or spec.runs, executor=chosen, repository=materialized, cost=cost
                )
                measurements.append(measured)
                flake.append(detail)
        if "classification" in parts and spec.cases:
            measured_list, classification = measure_classification(
                spec, executor=chosen, repository=materialized, classifier=classifier, cost=cost
            )
            measurements.extend(measured_list)
        if "visual" in parts and spec.surfaces:
            if driver is None:
                from lemoncrow.pro.capabilities.review.render_driver import ChromeCdpDriver

                driver = ChromeCdpDriver()
            for surface in spec.surfaces:
                measured, detail = measure_visual(surface, repository=materialized, driver=driver, scratch=work)
                cost.executions += measured.runs
                cost.seconds += measured.cost_seconds
                measurements.append(measured)
                visual.append(detail)
        executor_name = chosen.name
    return {
        "v": RESULTS_VERSION,
        "gate": "H",
        "repository": repository.id,
        "description": repository.description,
        "corpus_digest": repository.digest,
        "source": (
            {"revs": dict(repository.source_revs), "paths": list(repository.source_paths)}
            if repository.kind == "checkout"
            else {"synthetic": True}
        ),
        "executor": executor_name,
        "host": {"platform": platform.platform(), "machine": platform.machine()},
        "measured_at": _now(),
        "recipes": [{"id": recipe.id, "digest": recipe.digest, "report": recipe.report} for recipe in recipes],
        "measurements": [item.to_wire() for item in measurements],
        "flake": flake,
        "classification": classification,
        "visual": visual,
        "cost": cost.to_wire(),
    }


def read_results(path: Path) -> tuple[dict[str, Any], list[Measurement]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateHHarnessError(f"{path}: {exc}") from exc
    if not isinstance(document, Mapping) or document.get("v") != RESULTS_VERSION:
        raise GateHHarnessError(f"{path}: not a Gate H results document (v{RESULTS_VERSION})")
    return dict(document), [Measurement.from_wire(item) for item in document.get("measurements") or ()]


def qualify_measurements(
    measurements: Sequence[Measurement], thresholds: QualificationThresholds, recipes: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Qualification of every measured recipe (flake + its classifier) and surface, under *thresholds*."""

    by_key = {item.key: item for item in measurements}
    formats = {str(row.get("digest")): str(row.get("report") or "") for row in recipes}
    names = {str(row.get("digest")): str(row.get("id")) for row in recipes}
    out: list[dict[str, Any]] = []
    for item in measurements:
        if item.key.metric == "flake":
            subject = QualificationSubject(
                "recipe",
                f"checks.recipe:{names.get(item.key.digest, '?')}",
                digest=item.key.digest,
                environment_class=item.key.environment_class,
                classifier=classifier_key(formats.get(item.key.digest, "")),
            )
        elif item.key.metric == "visual":
            subject = QualificationSubject(
                "surface", item.subject, digest=item.key.digest, environment_class=item.key.environment_class
            )
        else:
            continue
        verdict = qualify(subject, by_key.get, thresholds)
        out.append(
            {
                "subject": subject.id,
                "state": verdict.state,
                "reason": verdict.reason,
                "detail": verdict.detail,
            }
        )
    return out


def summarize(document: Mapping[str, Any], measurements: Sequence[Measurement]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for item in measurements:
        rows[f"{item.key.metric}:{item.subject}"] = {
            "metric": item.key.metric,
            "digest": item.key.digest,
            "environment_class": item.key.environment_class,
            "observations": item.observations,
            "failures": item.failures,
            "rate": None if item.rate is None else round(item.rate, 6),
            "runs": item.runs,
            "unavailable": item.unavailable,
            **({"confusion": {k: dict(v) for k, v in item.confusion.items()}} if item.confusion else {}),
        }
    return {
        "corpus_digest": document.get("corpus_digest"),
        "executor": document.get("executor"),
        "measured_at": document.get("measured_at"),
        "cost": document.get("cost"),
        "measurements": rows,
    }


def load_gate(gate_dir: Path) -> GateHConfig:
    try:
        return load_gate_h_config(gate_dir)
    except GateHError as exc:
        raise GateHHarnessError(str(exc)) from exc


def results_dir(gate_dir: Path, config: GateHConfig) -> Path:
    return gate_dir / config.results


def record_baseline(gate_dir: Path, config: GateHConfig) -> dict[str, Any]:
    """``baseline.json`` from every results document (what ``report`` compares against)."""

    directory = results_dir(gate_dir, config)
    repositories: dict[str, Any] = {}
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        document, measurements = read_results(path)
        repositories[str(document.get("repository"))] = {
            **summarize(document, measurements),
            "qualification": qualify_measurements(measurements, config.thresholds, list(document.get("recipes") or ())),
        }
    return {
        "v": RESULTS_VERSION,
        "gate": "H",
        "thresholds": config.thresholds.to_wire(),
        "thresholds_digest": config.thresholds.digest,
        "repositories": repositories,
    }


def check_expectations(
    repositories: Mapping[str, Mapping[str, Any]], corpus: Mapping[str, CorpusRepository]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Each subject against its typed expectation: ``(rows by repository, violations)``.

    ``qualify`` is met by ``qualified``; ``advisory`` and ``negative_control`` are met only by
    ``advisory`` for the *named* reason, so a control disqualified for an unrelated reason (or one
    that starts to qualify) is a violation, never silently counted. Over the union of the corpus and
    the results: a measured repository the corpus does not hold (``no_corpus``), a corpus repository
    with no results (``no_results``), a measured subject nobody typed, and a typed subject with no
    measurement (``no_measurement``) are violations.
    """

    rows: dict[str, list[dict[str, Any]]] = {}
    violations: list[dict[str, Any]] = []
    for repo_id in sorted(set(repositories) | set(corpus)):
        repository = corpus.get(repo_id)
        measured = list((repositories.get(repo_id) or {}).get("qualification") or ())
        typed = {item.subject: item for item in repository.expectations} if repository is not None else {}
        checked: list[dict[str, Any]] = []
        if repository is None or repo_id not in repositories:
            outcome = {
                "subject": "",
                "expect": None,
                "met": False,
                "state": "unmeasured",
                "reason": "no_corpus" if repository is None else "no_results",
            }
            checked.append(outcome)
            violations.append({"repository": repo_id, **outcome})
        for item in measured:
            expected = typed.get(str(item["subject"]))
            if expected is None:
                outcome = {"subject": item["subject"], "expect": None, "met": False}
            elif expected.expect == "qualify":
                outcome = {"subject": expected.subject, "expect": "qualify", "met": item["state"] == "qualified"}
            else:
                met = item["state"] == "advisory" and item["reason"] == expected.reason
                outcome = {
                    "subject": expected.subject,
                    "expect": expected.expect,
                    "expected_reason": expected.reason,
                    "met": met,
                }
            outcome.update(state=item["state"], reason=item["reason"])
            checked.append(outcome)
            if not outcome["met"]:
                violations.append({"repository": repo_id, **outcome})
        for subject in sorted(set(typed) - {str(item["subject"]) for item in measured}):
            outcome = {
                "subject": subject,
                "expect": typed[subject].expect,
                "met": False,
                "state": "unmeasured",
                "reason": "no_measurement",
            }
            checked.append(outcome)
            violations.append({"repository": repo_id, **outcome})
        rows[repo_id] = checked
    return rows, violations


def _corpus_by_id(gate_dir: Path, config: GateHConfig) -> dict[str, CorpusRepository] | None:
    """The corpus repositories by id, or ``None`` when there is no corpus to read (a results-only directory)."""

    if not (gate_dir / config.corpus).is_dir():
        return None
    return {item.id: item for item in load_corpus(gate_dir, config)}


def report(gate_dir: Path, config: GateHConfig, thresholds: QualificationThresholds | None = None) -> dict[str, Any]:
    """Current results under *thresholds* (default: the config's), against the recorded baseline.

    Also types the subjects: ``expectations`` (per repository, from the corpus) and
    ``expectation_violations``. ``thresholds_changed_since_baseline`` is ``None`` for a baseline
    recorded before it carried the thresholds digest.
    """

    chosen = thresholds or config.thresholds
    current = record_baseline(gate_dir, replace(config, thresholds=chosen))
    baseline_path = gate_dir / config.baseline
    baseline: Mapping[str, Any] = {}
    if baseline_path.is_file():
        try:
            loaded = json.loads(baseline_path.read_text(encoding="utf-8"))
            baseline = loaded if isinstance(loaded, Mapping) else {}
        except (OSError, ValueError):
            baseline = {}
    changes: list[dict[str, Any]] = []
    for repo_id, row in current["repositories"].items():
        before = (baseline.get("repositories") or {}).get(repo_id) or {}
        old = {item["subject"]: item["state"] for item in before.get("qualification") or ()}
        for item in row["qualification"]:
            if old.get(item["subject"]) != item["state"]:
                changes.append(
                    {
                        "repository": repo_id,
                        "subject": item["subject"],
                        "baseline": old.get(item["subject"]),
                        "now": item["state"],
                    }
                )
    corpus = _corpus_by_id(gate_dir, config)
    expectations, violations = check_expectations(current["repositories"], corpus) if corpus is not None else ({}, [])
    recorded = baseline.get("thresholds_digest")
    return {
        **current,
        "mode": config.mode,
        "baseline": str(baseline_path),
        "changes": changes,
        "thresholds_changed_since_baseline": None if not isinstance(recorded, str) else recorded != chosen.digest,
        "expectations_checked": corpus is not None,
        "expectations": expectations,
        "expectation_violations": violations,
    }


__all__ = [
    "ABSENT",
    "MAX_RENDER_PAIRS",
    "MAX_RUNS_PER_INPUT",
    "RECIPE_LABEL",
    "CaseSpec",
    "CorpusRepository",
    "Cost",
    "ExpectationSpec",
    "GateHHarnessError",
    "HostExecutor",
    "MaterializedRepository",
    "RunOutcome",
    "SandboxExecutor",
    "SurfaceSpec",
    "check_expectations",
    "find_gate_h_root",
    "load_corpus",
    "load_gate",
    "load_repository",
    "materialize",
    "measure_classification",
    "measure_flake",
    "measure_visual",
    "qualify_measurements",
    "read_results",
    "record_baseline",
    "report",
    "results_dir",
    "run_repository",
]
