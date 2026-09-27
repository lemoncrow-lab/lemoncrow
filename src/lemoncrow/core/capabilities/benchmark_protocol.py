"""Frozen, zero-spend benchmark protocol validation.

A comparative benchmark is only useful if the protocol is fixed before results
are observed. This module validates a small JSON contract against the current
CodeBench catalog and prompt bytes, fingerprints it, and renders the exact
`lc benchmark codebench` commands that a later paid run would execute.

Validation never clones competitors, starts a model, or mutates a workspace.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_PROTOCOL_SCHEMA_VERSION: Final[int] = 1
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_GIT_SHA_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}")
_SAFE_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ALLOWED_KINDS: Final[frozenset[str]] = frozenset({"qualification", "generalization", "external"})
_ALLOWED_DRIVERS: Final[frozenset[str]] = frozenset({"claude", "codex"})
_ALLOWED_ARMS: Final[frozenset[str]] = frozenset(
    {"baseline", "lemoncrow", "lemoncrow-control", "lemoncrow-shadow", "lemoncrow-candidate"}
)


@dataclass(frozen=True, slots=True)
class FrozenTask:
    id: str
    language: str
    repo: str
    ref: str
    prompt_sha256: str


@dataclass(frozen=True, slots=True)
class FrozenCompetitor:
    path: Path
    sha256: str
    name: str
    repo: str
    ref: str


@dataclass(frozen=True, slots=True)
class FrozenRun:
    id: str
    kind: str
    cli_driver: str
    model: str
    arms: tuple[str, ...]
    reps: int
    timeout: int
    jobs: int
    parallel_scope: str
    mode: str
    require_pass: bool
    judge: bool
    judge_model: str
    cli_version: str
    judge_cli_version: str
    competitors: tuple[FrozenCompetitor, ...] = ()


@dataclass(frozen=True, slots=True)
class CodeBenchProtocol:
    schema_version: int
    id: str
    frozen_at: str
    source_path: Path
    fingerprint: str
    tasks: tuple[FrozenTask, ...]
    runs: tuple[FrozenRun, ...]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"benchmark protocol not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"benchmark protocol is invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("benchmark protocol must be a JSON object")
    return payload


def _canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prompt_path(task_source_dir: Path, task_dir: str) -> Path:
    root = task_source_dir / "tasks" / task_dir
    for name in ("prompt.md", "prompt_hard.md", "prompt_medium.md", "prompt_trivial.md"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    variants = sorted(root.glob("prompt_*.md"))
    if variants:
        return variants[0]
    raise ValueError(f"CodeBench task has no prompt file: {root}")


def _prompt_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def codebench_prompt_sha256(task_source_dir: Path, task_dir: str) -> str:
    """Hash the exact prompt variant CodeBench would resolve for one task."""

    return _prompt_digest(_prompt_path(task_source_dir, task_dir))


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError as exc:
        raise ValueError(f"frozen benchmark artifact not found: {path}") from exc


def _require_safe_id(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(text):
        raise ValueError(f"{field} must be a safe non-empty id, got {text!r}")
    return text


def _require_int(value: object, *, field: str, minimum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer >= {minimum}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an integer >= {minimum}") from exc
    else:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    if parsed < minimum:
        raise ValueError(f"{field} must be >= {minimum}, got {parsed}")
    return parsed


def _validate_model(driver: str, model: str) -> None:
    if not model or model.lower() in {"auto", "default", "sonnet", "opus", "haiku"}:
        raise ValueError(f"run model must be an exact pinned model id, got {model!r}")
    if driver == "claude" and not model.startswith("claude-"):
        raise ValueError(f"Claude protocol run must pin a Claude model id, got {model!r}")
    if driver == "codex" and any(token in model.lower() for token in ("claude", "sonnet", "opus", "haiku")):
        raise ValueError(f"Codex protocol run cannot pin a Claude model id, got {model!r}")


def _validate_competitor_manifest(path: Path, *, expected_sha256: str) -> FrozenCompetitor:
    if not _SHA256_RE.fullmatch(expected_sha256):
        raise ValueError(f"competitor manifest must pin a SHA-256: {path}")
    actual_sha256 = _file_digest(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"competitor manifest drift: protocol={expected_sha256}, current={actual_sha256}, path={path}")
    raw = _read_json(path)
    name = _require_safe_id(raw.get("name"), field=f"{path}: name")
    repo = str(raw.get("repo") or "").strip()
    ref = str(raw.get("ref") or "").strip().lower()
    if not repo:
        raise ValueError(f"competitor manifest is missing repo: {path}")
    if not _GIT_SHA_RE.fullmatch(ref):
        raise ValueError(f"competitor manifest ref must be an immutable 40-char commit SHA: {path}")
    if not any(raw.get(key) for key in ("mcp", "plugin_dir", "skill_file", "agent")):
        raise ValueError(f"competitor manifest has no agent/tool wiring: {path}")
    return FrozenCompetitor(path=path, sha256=expected_sha256, name=name, repo=repo, ref=ref)


def _catalog_by_id(task_catalog: Sequence[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for row in task_catalog:
        task_id = str(row.get("id") or "")
        if task_id:
            result[task_id] = row
    return result


def _load_tasks(
    raw_tasks: object,
    *,
    task_catalog: Sequence[Mapping[str, object]],
    task_source_dir: Path,
) -> tuple[FrozenTask, ...]:
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("benchmark protocol tasks must be a non-empty list")
    catalog = _catalog_by_id(task_catalog)
    tasks: list[FrozenTask] = []
    seen: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise ValueError("benchmark protocol tasks[] must be objects")
        task_id = _require_safe_id(raw.get("id"), field="tasks[].id")
        if task_id in seen:
            raise ValueError(f"duplicate protocol task: {task_id}")
        seen.add(task_id)
        catalog_row = catalog.get(task_id)
        if catalog_row is None:
            raise ValueError(f"protocol task is not present in CodeBench catalog: {task_id}")

        language = str(raw.get("language") or "").strip()
        repo = str(raw.get("repo") or "").strip()
        ref = str(raw.get("ref") or "").strip().lower()
        prompt_sha256 = str(raw.get("prompt_sha256") or "").strip().lower()
        if not language or language != str(catalog_row.get("language") or ""):
            raise ValueError(
                f"task {task_id} language drift: protocol={language!r}, catalog={catalog_row.get('language')!r}"
            )
        if not _GIT_SHA_RE.fullmatch(ref):
            raise ValueError(f"task {task_id} must pin a 40-char repository commit")
        if not _SHA256_RE.fullmatch(prompt_sha256):
            raise ValueError(f"task {task_id} must pin a prompt SHA-256")

        source = catalog_row.get("source")
        if not isinstance(source, list) or len(source) < 3 or source[0] != "repo":
            raise ValueError(f"task {task_id} is not a pinned repository task")
        catalog_repo = str(source[1] or "")
        catalog_ref = str(source[2] or "").lower()
        if repo != catalog_repo or ref != catalog_ref:
            raise ValueError(
                f"task {task_id} source drift: protocol={repo}@{ref}, catalog={catalog_repo}@{catalog_ref}"
            )

        task_dir = str(catalog_row.get("task_dir") or "")
        actual_prompt_sha = codebench_prompt_sha256(task_source_dir, task_dir)
        if prompt_sha256 != actual_prompt_sha:
            raise ValueError(f"task {task_id} prompt drift: protocol={prompt_sha256}, current={actual_prompt_sha}")
        tasks.append(FrozenTask(task_id, language, repo, ref, prompt_sha256))
    return tuple(tasks)


def _load_runs(raw_runs: object, *, protocol_path: Path) -> tuple[FrozenRun, ...]:
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ValueError("benchmark protocol runs must be a non-empty list")
    runs: list[FrozenRun] = []
    seen: set[str] = set()
    for raw in raw_runs:
        if not isinstance(raw, dict):
            raise ValueError("benchmark protocol runs[] must be objects")
        run_id = _require_safe_id(raw.get("id"), field="runs[].id")
        if run_id in seen:
            raise ValueError(f"duplicate protocol run id: {run_id}")
        seen.add(run_id)

        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in _ALLOWED_KINDS:
            raise ValueError(f"run {run_id} has unsupported kind {kind!r}")
        driver = str(raw.get("cli_driver") or "").strip().lower()
        if driver not in _ALLOWED_DRIVERS:
            raise ValueError(f"run {run_id} has unsupported frozen-proof driver {driver!r}")
        model = str(raw.get("model") or "").strip()
        _validate_model(driver, model)

        arms_raw = raw.get("arms")
        if not isinstance(arms_raw, list) or not arms_raw:
            raise ValueError(f"run {run_id} must define arms")
        arms = tuple(str(arm) for arm in arms_raw)
        if len(set(arms)) != len(arms):
            raise ValueError(f"run {run_id} contains duplicate arms")
        unknown = sorted(set(arms) - _ALLOWED_ARMS)
        if unknown:
            raise ValueError(f"run {run_id} contains unsupported built-in arms: {', '.join(unknown)}")

        if kind == "qualification":
            required = {"baseline", "lemoncrow-control", "lemoncrow-shadow", "lemoncrow-candidate"}
            if not required.issubset(arms):
                raise ValueError(f"qualification run {run_id} must include {sorted(required)}")
        elif kind == "generalization":
            required = {"baseline", "lemoncrow-control", "lemoncrow-candidate"}
            if not required.issubset(arms):
                raise ValueError(f"generalization run {run_id} must include {sorted(required)}")
        else:
            required = {"baseline", "lemoncrow"}
            if not required.issubset(arms):
                raise ValueError(f"external run {run_id} must include baseline and lemoncrow")

        min_reps = 5 if kind in {"qualification", "external"} else 3
        reps = _require_int(raw.get("reps"), field=f"run {run_id} reps", minimum=min_reps)
        timeout = _require_int(raw.get("timeout"), field=f"run {run_id} timeout", minimum=300)
        jobs = _require_int(raw.get("jobs"), field=f"run {run_id} jobs", minimum=1)
        parallel_scope = str(raw.get("parallel_scope") or "").strip()
        if parallel_scope != "task":
            raise ValueError(f"run {run_id} must use parallel_scope='task' for paired fairness")
        mode = str(raw.get("mode") or "").strip()
        if mode != "cost":
            raise ValueError(f"run {run_id} must use mode='cost' for the frozen comparative protocol")
        require_pass = bool(raw.get("require_pass", False))
        if kind in {"qualification", "generalization"} and not require_pass:
            raise ValueError(f"run {run_id} must require benchmark/runtime-policy gates")
        judge = bool(raw.get("judge", False))
        judge_model = str(raw.get("judge_model") or "").strip()
        if not judge:
            raise ValueError(f"run {run_id} must enable pairwise judging for publishable quality evidence")
        if not judge_model.startswith("claude-"):
            raise ValueError(f"run {run_id} must pin an explicit Claude judge model")
        cli_version = str(raw.get("cli_version") or "").strip()
        judge_cli_version = str(raw.get("judge_cli_version") or "").strip()
        if not cli_version:
            raise ValueError(f"run {run_id} must pin the host CLI version")
        if not judge_cli_version:
            raise ValueError(f"run {run_id} must pin the judge CLI version")

        competitors_raw = raw.get("competitors", [])
        if not isinstance(competitors_raw, list):
            raise ValueError(f"run {run_id} competitors must be a list")
        competitors_list: list[FrozenCompetitor] = []
        for value in competitors_raw:
            if not isinstance(value, dict):
                raise ValueError(f"run {run_id} competitor entries must pin path + sha256")
            relative_path = str(value.get("path") or "").strip()
            expected_sha256 = str(value.get("sha256") or "").strip().lower()
            if not relative_path:
                raise ValueError(f"run {run_id} competitor entry is missing path")
            manifest_path = (protocol_path.parent / relative_path).resolve()
            competitors_list.append(_validate_competitor_manifest(manifest_path, expected_sha256=expected_sha256))
        competitors = tuple(competitors_list)
        if kind == "external":
            if driver != "claude":
                raise ValueError(f"external competitor run {run_id} must use the Claude driver")
            if not competitors:
                raise ValueError(f"external run {run_id} must pin at least one competitor manifest")
        elif competitors:
            raise ValueError(f"non-external run {run_id} must not include competitor manifests")

        runs.append(
            FrozenRun(
                id=run_id,
                kind=kind,
                cli_driver=driver,
                model=model,
                arms=arms,
                reps=reps,
                timeout=timeout,
                jobs=jobs,
                parallel_scope=parallel_scope,
                mode=mode,
                require_pass=require_pass,
                judge=judge,
                judge_model=judge_model,
                cli_version=cli_version,
                judge_cli_version=judge_cli_version,
                competitors=competitors,
            )
        )
    return tuple(runs)


def load_codebench_protocol(
    path: str | Path,
    *,
    task_catalog: Sequence[Mapping[str, object]],
    task_source_dir: Path,
) -> CodeBenchProtocol:
    protocol_path = Path(path).expanduser().resolve()
    raw = _read_json(protocol_path)
    schema_version = _require_int(raw.get("schema_version"), field="schema_version", minimum=1)
    if schema_version != _PROTOCOL_SCHEMA_VERSION:
        raise ValueError(f"unsupported benchmark protocol schema {schema_version}; expected {_PROTOCOL_SCHEMA_VERSION}")
    protocol_id = _require_safe_id(raw.get("id"), field="id")
    frozen_at = str(raw.get("frozen_at") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", frozen_at):
        raise ValueError("frozen_at must be YYYY-MM-DD")
    tasks = _load_tasks(raw.get("tasks"), task_catalog=task_catalog, task_source_dir=task_source_dir)
    runs = _load_runs(raw.get("runs"), protocol_path=protocol_path)
    drivers = {run.cli_driver for run in runs}
    if not {"claude", "codex"}.issubset(drivers):
        raise ValueError("intelligent-runtime proof protocol must include both Claude and Codex host lanes")
    if not any(run.kind == "external" for run in runs):
        raise ValueError("intelligent-runtime proof protocol must include an external comparator run")
    return CodeBenchProtocol(
        schema_version=schema_version,
        id=protocol_id,
        frozen_at=frozen_at,
        source_path=protocol_path,
        fingerprint=_canonical_fingerprint(raw),
        tasks=tasks,
        runs=runs,
    )


def codebench_protocol_commands(
    protocol: CodeBenchProtocol,
    *,
    task_source_dir: Path,
    output_root: Path | None = None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    commands: list[tuple[str, tuple[str, ...]]] = []
    for run in protocol.runs:
        cmd: list[str] = ["lc", "benchmark", "codebench"]
        for task in protocol.tasks:
            cmd.extend(["--task", task.id])
        for arm in run.arms:
            cmd.extend(["--arm", arm])
        cmd.extend(
            [
                "--reps",
                str(run.reps),
                "--model",
                run.model,
                "--timeout",
                str(run.timeout),
                "--cli-driver",
                run.cli_driver,
                "--jobs",
                str(run.jobs),
                "--parallel-scope",
                run.parallel_scope,
                "--mode",
                run.mode,
                "--task-source-dir",
                str(task_source_dir.resolve()),
                "--judge",
                "--judge-model",
                run.judge_model,
            ]
        )
        for competitor in run.competitors:
            cmd.extend(["--competitor", str(competitor.path)])
        if output_root is not None:
            cmd.extend(["--out", str((output_root / run.id).resolve())])
        if run.require_pass:
            cmd.append("--require-pass")
        commands.append((run.id, tuple(cmd)))
    return tuple(commands)


def verify_codebench_manifest(
    protocol: CodeBenchProtocol,
    *,
    run_id: str,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify one completed CodeBench manifest against a frozen protocol run."""

    run = next((item for item in protocol.runs if item.id == run_id), None)
    if run is None:
        raise ValueError(f"protocol has no run {run_id!r}")
    reasons: list[str] = []
    if manifest.get("suite") != "codebench":
        reasons.append("manifest suite is not codebench")

    corpus = manifest.get("corpus")
    manifest_tasks = corpus.get("tasks") if isinstance(corpus, Mapping) else None
    if not isinstance(manifest_tasks, list):
        reasons.append("manifest has no CodeBench task list")
        manifest_tasks = []
    expected_tasks = list(protocol.tasks)
    if [str(row.get("id") or "") for row in manifest_tasks if isinstance(row, Mapping)] != [
        task.id for task in expected_tasks
    ]:
        reasons.append("manifest task ids/order do not match frozen protocol")
    by_id = {str(row.get("id") or ""): row for row in manifest_tasks if isinstance(row, Mapping) and row.get("id")}
    for task in expected_tasks:
        row = by_id.get(task.id)
        if not isinstance(row, Mapping):
            continue
        source = row.get("source")
        if (
            not isinstance(source, list)
            or len(source) < 3
            or str(source[1] or "") != task.repo
            or str(source[2] or "").lower() != task.ref
        ):
            reasons.append(f"task {task.id} repository source does not match frozen protocol")
        if str(row.get("prompt_sha256") or "").lower() != task.prompt_sha256:
            reasons.append(f"task {task.id} prompt hash does not match frozen protocol")

    manifest_protocol = manifest.get("protocol")
    if not isinstance(manifest_protocol, Mapping):
        reasons.append("manifest has no protocol block")
        manifest_protocol = {}
    baseline = str(manifest_protocol.get("baseline_arm") or "")
    treatments = manifest_protocol.get("treatment_arms")
    actual_arms = [baseline] + [str(value) for value in treatments] if isinstance(treatments, list) else [baseline]
    expected_arms = [*run.arms, *(competitor.name for competitor in run.competitors)]
    if actual_arms != expected_arms:
        reasons.append(f"manifest arms differ: expected {expected_arms}, got {actual_arms}")
    if int(manifest_protocol.get("reps") or 0) != run.reps:
        reasons.append("manifest reps do not match frozen protocol")

    matched = manifest_protocol.get("matched_fields")
    if not isinstance(matched, Mapping):
        reasons.append("manifest has no matched_fields block")
        matched = {}
    expected_fields: dict[str, object] = {
        "model": run.model,
        "cli_driver": run.cli_driver,
        "timeout_seconds": run.timeout,
        "jobs": run.jobs,
        "parallel_scope": run.parallel_scope,
        "mode": run.mode,
        "judge": run.judge,
        "judge_model": run.judge_model,
        "cli_version": run.cli_version,
        "judge_cli_version": run.judge_cli_version,
    }
    for key, expected in expected_fields.items():
        if matched.get(key) != expected:
            reasons.append(f"manifest {key} differs: expected {expected!r}, got {matched.get(key)!r}")

    attribution = manifest.get("runtime_attribution")
    comparators = attribution.get("comparators") if isinstance(attribution, Mapping) else None
    comparator_rows = comparators if isinstance(comparators, Mapping) else {}
    for competitor in run.competitors:
        row = comparator_rows.get(competitor.name)
        if not isinstance(row, Mapping):
            reasons.append(f"manifest is missing comparator metadata for {competitor.name}")
            continue
        if str(row.get("repo") or "") != competitor.repo or str(row.get("ref") or "").lower() != competitor.ref:
            reasons.append(f"comparator {competitor.name} repo/ref differs from frozen protocol")
        if str(row.get("manifest_sha256") or "").lower() != competitor.sha256:
            reasons.append(f"comparator {competitor.name} manifest hash differs from frozen protocol")

    return {
        "protocol_id": protocol.id,
        "protocol_fingerprint": protocol.fingerprint,
        "run_id": run.id,
        "passed": not reasons,
        "reasons": reasons,
    }


def _load_run_json(path: Path, *, label: str, reasons: list[str]) -> dict[str, Any]:
    if not path.is_file():
        reasons.append(f"missing {label}: {path.name}")
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        reasons.append(f"invalid {label}: {path.name}: {exc}")
        return {}
    if not isinstance(payload, dict):
        reasons.append(f"invalid {label}: {path.name} must contain an object")
        return {}
    return payload


def _load_run_jsonl(path: Path, *, reasons: list[str]) -> list[dict[str, Any]]:
    if not path.is_file():
        reasons.append(f"missing benchmark results: {path.name}")
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        reasons.append(f"cannot read benchmark results: {exc}")
        return []
    for index, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            reasons.append(f"invalid results.jsonl line {index}: {exc}")
            continue
        if not isinstance(payload, dict):
            reasons.append(f"invalid results.jsonl line {index}: expected object")
            continue
        rows.append(payload)
    return rows


def verify_codebench_run(
    protocol: CodeBenchProtocol,
    *,
    run_id: str,
    run_dir: Path,
) -> dict[str, Any]:
    """Verify provenance, row completeness, evidence and gates for one frozen run."""

    run = next((item for item in protocol.runs if item.id == run_id), None)
    if run is None:
        raise ValueError(f"protocol has no run {run_id!r}")
    run_dir = run_dir.expanduser().resolve()
    reasons: list[str] = []

    manifest = _load_run_json(run_dir / "benchmark-manifest.json", label="benchmark manifest", reasons=reasons)
    manifest_verification = verify_codebench_manifest(protocol, run_id=run_id, manifest=manifest)
    reasons.extend(str(reason) for reason in manifest_verification.get("reasons", []))

    expected_arms = [*run.arms, *(competitor.name for competitor in run.competitors)]
    expected_keys = {(task.id, arm, rep) for task in protocol.tasks for arm in expected_arms for rep in range(run.reps)}
    results = _load_run_jsonl(run_dir / "results.jsonl", reasons=reasons)
    result_keys = [(str(row.get("task") or ""), str(row.get("arm") or ""), int(row.get("rep") or 0)) for row in results]
    counts = Counter(result_keys)
    duplicate_keys = sorted(key for key, count in counts.items() if count != 1)
    observed_keys = set(result_keys)
    missing_keys = sorted(expected_keys - observed_keys)
    unexpected_keys = sorted(observed_keys - expected_keys)
    invalid_rows = sorted(
        (str(row.get("task") or ""), str(row.get("arm") or ""), int(row.get("rep") or 0))
        for row in results
        if row.get("valid") is False
    )
    if duplicate_keys:
        reasons.append(f"results contain duplicate task/arm/rep rows: {duplicate_keys[:5]}")
    if missing_keys:
        reasons.append(f"results are incomplete; missing {len(missing_keys)} expected rows")
    if unexpected_keys:
        reasons.append(f"results contain {len(unexpected_keys)} rows outside the frozen protocol")
    if invalid_rows:
        reasons.append(f"results contain {len(invalid_rows)} invalid/contaminated rows")

    evidence = _load_run_json(run_dir / "benchmark-evidence.json", label="benchmark evidence", reasons=reasons)
    if evidence and evidence.get("suite") != "codebench":
        reasons.append("benchmark evidence suite is not codebench")
    commit_state = evidence.get("commit_under_test") if isinstance(evidence, Mapping) else None
    if not isinstance(commit_state, Mapping) or not str(commit_state.get("commit") or ""):
        reasons.append("benchmark evidence does not record the LemonCrow commit under test")
    elif commit_state.get("dirty") is not False:
        reasons.append("benchmark evidence says the LemonCrow checkout was dirty")
    evidence_artifacts = evidence.get("artifacts") if isinstance(evidence, Mapping) else None
    required_artifacts = (
        "results_jsonl",
        "results_csv",
        "summary_csv",
        "pairwise_quality_csv",
        "report_txt",
    )
    if not isinstance(evidence_artifacts, Mapping):
        reasons.append("benchmark evidence has no artifact inventory")
    else:
        for name in required_artifacts:
            record = evidence_artifacts.get(name)
            if not isinstance(record, Mapping) or record.get("exists") is not True:
                reasons.append(f"benchmark evidence does not prove required artifact {name}")
                continue
            raw_path = str(record.get("path") or "").strip()
            artifact_path = Path(raw_path) if raw_path else Path()
            if raw_path and not artifact_path.is_absolute():
                artifact_path = run_dir / artifact_path
            if not raw_path or not artifact_path.is_file():
                reasons.append(f"required benchmark artifact is missing on disk: {name}")

    benchmark_gate = _load_run_json(run_dir / "benchmark-gate.json", label="benchmark gate", reasons=reasons)
    comparison_gate = _load_run_json(
        run_dir / "benchmark-comparison-gates.json",
        label="comparison gates",
        reasons=reasons,
    )
    runtime_policy_gate: dict[str, Any] = {}
    fresh_benchmark_gate: dict[str, Any] = {}
    fresh_comparison_gate: dict[str, Any] = {}
    fresh_runtime_policy_gate: dict[str, Any] = {}

    gate_inputs_ready = (
        (run_dir / "benchmark-manifest.json").is_file()
        and (run_dir / "results.jsonl").is_file()
        and (run_dir / "pairwise_quality.csv").is_file()
    )
    if gate_inputs_ready:
        from lemoncrow.core.capabilities.benchmark_gate import (
            evaluate_codebench_comparison_gates,
            evaluate_codebench_gate,
            evaluate_runtime_policy_gate,
        )

        baseline_arm = expected_arms[0]
        candidate_arms = expected_arms[1:]
        fresh_benchmark_gate = evaluate_codebench_gate(
            run_dir,
            baseline_arm=baseline_arm,
            candidate_arm=candidate_arms[0],
            mode=run.mode,
        )
        fresh_comparison_gate = evaluate_codebench_comparison_gates(
            run_dir,
            baseline_arm=baseline_arm,
            candidate_arms=candidate_arms,
            mode=run.mode,
        )
        if benchmark_gate.get("passed") is not fresh_benchmark_gate.get("passed"):
            reasons.append("stored benchmark gate is stale or disagrees with raw benchmark artifacts")
        if comparison_gate.get("passed_all") is not fresh_comparison_gate.get("passed_all"):
            reasons.append("stored comparison gates are stale or disagree with raw benchmark artifacts")

        stored_candidates = comparison_gate.get("candidates")
        fresh_candidates = fresh_comparison_gate.get("candidates")
        if (
            isinstance(stored_candidates, Mapping)
            and isinstance(fresh_candidates, Mapping)
            and set(stored_candidates) != set(fresh_candidates)
        ):
            reasons.append("stored comparison gate candidate set disagrees with recomputed evidence")

        if run.kind in {"qualification", "generalization"}:
            runtime_policy_gate = _load_run_json(
                run_dir / "runtime-policy-gate.json",
                label="runtime policy gate",
                reasons=reasons,
            )
            fresh_runtime_policy_gate = evaluate_runtime_policy_gate(
                run_dir,
                control_arm="lemoncrow-control",
                candidate_arm="lemoncrow-candidate",
            )
            if runtime_policy_gate.get("passed") is not fresh_runtime_policy_gate.get("passed"):
                reasons.append("stored runtime policy gate is stale or disagrees with raw benchmark artifacts")
            if fresh_benchmark_gate.get("passed") is not True:
                reasons.append("benchmark quality/cost gate did not pass")
            if fresh_comparison_gate.get("passed_all") is not True:
                reasons.append("not every frozen candidate comparison gate passed")
            if fresh_runtime_policy_gate.get("passed") is not True:
                reasons.append("runtime policy qualification gate did not pass")
        else:
            expected_candidates = set(expected_arms) - {expected_arms[0]}
            fresh_candidates = fresh_comparison_gate.get("candidates")
            actual_candidates = set(fresh_candidates) if isinstance(fresh_candidates, Mapping) else set()
            if actual_candidates != expected_candidates:
                reasons.append(
                    "external comparison gate candidates differ from frozen protocol: "
                    f"expected {sorted(expected_candidates)}, got {sorted(actual_candidates)}"
                )
    elif run.kind in {"qualification", "generalization"}:
        runtime_policy_gate = _load_run_json(
            run_dir / "runtime-policy-gate.json",
            label="runtime policy gate",
            reasons=reasons,
        )

    return {
        "protocol_id": protocol.id,
        "protocol_fingerprint": protocol.fingerprint,
        "run_id": run.id,
        "run_kind": run.kind,
        "run_dir": str(run_dir),
        "passed": not reasons,
        "reasons": reasons,
        "details": {
            "expected_rows": len(expected_keys),
            "observed_rows": len(results),
            "unique_rows": len(observed_keys),
            "missing_rows": len(missing_keys),
            "unexpected_rows": len(unexpected_keys),
            "duplicate_rows": len(duplicate_keys),
            "invalid_rows": len(invalid_rows),
            "commit_under_test": dict(commit_state) if isinstance(commit_state, Mapping) else {},
            "benchmark_gate_passed": (
                fresh_benchmark_gate.get("passed") is True
                if fresh_benchmark_gate
                else benchmark_gate.get("passed") is True
            ),
            "comparison_gates_passed_all": (
                fresh_comparison_gate.get("passed_all") is True
                if fresh_comparison_gate
                else comparison_gate.get("passed_all") is True
            ),
            "runtime_policy_gate_passed": (
                fresh_runtime_policy_gate.get("passed") is True
                if fresh_runtime_policy_gate
                else (runtime_policy_gate.get("passed") is True if runtime_policy_gate else None)
            ),
        },
    }


def verify_codebench_protocol_hosts(
    protocol: CodeBenchProtocol,
    *,
    current_versions: Mapping[str, str],
) -> dict[str, Any]:
    """Compare frozen host/judge CLI versions with a zero-spend local preflight."""

    reasons: list[str] = []
    checks: list[dict[str, str | bool]] = []
    for run in protocol.runs:
        actual_cli = str(current_versions.get(run.cli_driver) or "")
        cli_ok = actual_cli == run.cli_version
        checks.append(
            {
                "run_id": run.id,
                "role": "driver",
                "tool": run.cli_driver,
                "expected": run.cli_version,
                "actual": actual_cli,
                "passed": cli_ok,
            }
        )
        if not cli_ok:
            reasons.append(
                f"{run.id} {run.cli_driver} version differs: expected {run.cli_version!r}, got {actual_cli!r}"
            )

        actual_judge = str(current_versions.get("claude") or "")
        judge_ok = actual_judge == run.judge_cli_version
        checks.append(
            {
                "run_id": run.id,
                "role": "judge",
                "tool": "claude",
                "expected": run.judge_cli_version,
                "actual": actual_judge,
                "passed": judge_ok,
            }
        )
        if not judge_ok:
            reasons.append(
                f"{run.id} judge CLI version differs: expected {run.judge_cli_version!r}, got {actual_judge!r}"
            )
    return {
        "passed": not reasons,
        "reasons": reasons,
        "current_versions": dict(current_versions),
        "checks": checks,
    }


def verify_codebench_publication(
    protocol: CodeBenchProtocol,
    *,
    run_root: Path,
    current_versions: Mapping[str, str],
) -> dict[str, Any]:
    """Verify every frozen lane as one publication-ready evidence bundle."""

    run_root = run_root.expanduser().resolve()
    host_preflight = verify_codebench_protocol_hosts(protocol, current_versions=current_versions)
    run_verifications: dict[str, dict[str, Any]] = {}
    reasons = list(str(reason) for reason in host_preflight.get("reasons", []))
    commits: dict[str, str] = {}

    for run in protocol.runs:
        run_dir = run_root / run.id
        verification = verify_codebench_run(protocol, run_id=run.id, run_dir=run_dir)
        run_verifications[run.id] = verification
        if verification.get("passed") is not True:
            reasons.append(f"{run.id} failed frozen-run verification")
        details = verification.get("details")
        commit_state = details.get("commit_under_test") if isinstance(details, Mapping) else None
        commit = str(commit_state.get("commit") or "") if isinstance(commit_state, Mapping) else ""
        if commit:
            commits[run.id] = commit

    unique_commits = sorted(set(commits.values()))
    if len(commits) != len(protocol.runs):
        reasons.append("not every frozen run records a commit under test")
    if len(unique_commits) > 1:
        reasons.append(
            "frozen runs do not test the same LemonCrow commit: "
            + ", ".join(f"{run_id}={commit}" for run_id, commit in sorted(commits.items()))
        )

    return {
        "protocol_id": protocol.id,
        "protocol_fingerprint": protocol.fingerprint,
        "run_root": str(run_root),
        "passed": not reasons,
        "reasons": reasons,
        "host_preflight": host_preflight,
        "runs": run_verifications,
        "commit_under_test": unique_commits[0] if len(unique_commits) == 1 else "",
    }


def write_publication_verification(run_root: Path, payload: Mapping[str, Any]) -> Path:
    run_root.mkdir(parents=True, exist_ok=True)
    path = run_root / "publication-readiness.json"
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_protocol_verification(run_dir: Path, payload: Mapping[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "protocol-verification.json"
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")
    return path


def _pairwise_judge_comparisons(protocol: CodeBenchProtocol) -> int:
    total = 0
    for run in protocol.runs:
        if not run.judge:
            continue
        arm_count = len(run.arms) + len(run.competitors)
        if arm_count < 2:
            continue
        primary_baseline = "baseline" if "baseline" in run.arms else run.arms[0]
        comparisons_per_task_rep = arm_count - 1
        if (
            primary_baseline != "lemoncrow-control"
            and "lemoncrow-control" in run.arms
            and "lemoncrow-candidate" in run.arms
        ):
            comparisons_per_task_rep += 1
        total += len(protocol.tasks) * run.reps * comparisons_per_task_rep
    return total


def codebench_protocol_summary(protocol: CodeBenchProtocol) -> dict[str, Any]:
    return {
        "schema_version": protocol.schema_version,
        "id": protocol.id,
        "frozen_at": protocol.frozen_at,
        "protocol_path": str(protocol.source_path),
        "fingerprint": protocol.fingerprint,
        "tasks": [
            {
                "id": task.id,
                "language": task.language,
                "repo": task.repo,
                "ref": task.ref,
                "prompt_sha256": task.prompt_sha256,
            }
            for task in protocol.tasks
        ],
        "runs": [
            {
                "id": run.id,
                "kind": run.kind,
                "cli_driver": run.cli_driver,
                "model": run.model,
                "arms": list(run.arms),
                "reps": run.reps,
                "timeout": run.timeout,
                "jobs": run.jobs,
                "parallel_scope": run.parallel_scope,
                "mode": run.mode,
                "require_pass": run.require_pass,
                "judge": run.judge,
                "judge_model": run.judge_model,
                "cli_version": run.cli_version,
                "judge_cli_version": run.judge_cli_version,
                "competitors": [
                    {
                        "path": str(competitor.path),
                        "sha256": competitor.sha256,
                        "name": competitor.name,
                        "repo": competitor.repo,
                        "ref": competitor.ref,
                    }
                    for competitor in run.competitors
                ],
            }
            for run in protocol.runs
        ],
        "total_agent_rows": sum(
            len(protocol.tasks) * (len(run.arms) + len(run.competitors)) * run.reps for run in protocol.runs
        ),
        "pairwise_judge_comparisons": _pairwise_judge_comparisons(protocol),
    }


__all__ = [
    "CodeBenchProtocol",
    "FrozenCompetitor",
    "FrozenRun",
    "FrozenTask",
    "codebench_prompt_sha256",
    "codebench_protocol_commands",
    "codebench_protocol_summary",
    "load_codebench_protocol",
    "verify_codebench_manifest",
    "verify_codebench_protocol_hosts",
    "verify_codebench_publication",
    "verify_codebench_run",
    "write_protocol_verification",
    "write_publication_verification",
]
