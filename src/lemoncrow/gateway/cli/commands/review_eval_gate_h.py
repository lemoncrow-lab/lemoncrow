"""``lc dev eval review gate-h``: Gate H behavior-evidence-quality measurement (run, record, report, import)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from lemoncrow_client.review_words import revision_name

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.evidence_qualification import GateHConfig


def _gate_dir_option(func: Any) -> Any:
    return click.option(
        "--gate-dir",
        type=click.Path(path_type=Path, file_okay=False, exists=True),
        default=None,
        help="Directory holding gate-h.yaml (default: the nearest evals/review above the current directory).",
    )(func)


def _config(gate_dir: Path | None) -> tuple[Path, GateHConfig]:
    from lemoncrow.pro.capabilities.review.evaluation.gate_h import GateHHarnessError, find_gate_h_root, load_gate

    try:
        root = gate_dir.resolve() if gate_dir is not None else find_gate_h_root(Path.cwd())
        return root, load_gate(root)
    except GateHHarnessError as exc:
        raise click.ClickException(str(exc)) from exc


def _results_dir_option(func: Any) -> Any:
    return click.option(
        "--results-dir",
        "results_dir_path",
        type=click.Path(path_type=Path, file_okay=False),
        default=None,
        help="Results documents directory (default: the gate-h.yaml results directory). Release qualification "
        "measures into a scratch directory so the committed results stay untouched.",
    )(func)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _rate(value: object) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:.1%}"


@click.group(name="gate-h")
def gate_h() -> None:
    """Gate H: qualify LemonCrow-executed recipes and surfaces on measured runs (report mode until approved)."""


@gate_h.command("run")
@_gate_dir_option
@click.option("--repository", "repositories", multiple=True, metavar="ID", help="Only these corpus repositories.")
@click.option(
    "--part",
    "parts",
    multiple=True,
    type=click.Choice(["flake", "classification", "visual"]),
    help="Only these measurements (default: all).",
)
@click.option(
    "--runs", type=click.IntRange(min=2, max=20), default=None, help="Runs per flake input (default: corpus)."
)
@_results_dir_option
@click.option("--json", "as_json", is_flag=True, help="Print the results documents as JSON.")
def gate_h_run(
    gate_dir: Path | None,
    repositories: tuple[str, ...],
    parts: tuple[str, ...],
    runs: int | None,
    results_dir_path: Path | None,
    as_json: bool,
) -> None:
    """Measure the corpus: repeat runs (flake), labeled cases (accuracy), double renders (determinism).

    Writes one versioned results document per repository under the gate-h.yaml results directory
    (or ``--results-dir``).
    """

    from lemoncrow.pro.capabilities.review.evaluation.gate_h import (
        GateHHarnessError,
        load_corpus,
        results_dir,
        run_repository,
    )

    root, config = _config(gate_dir)
    try:
        corpus = load_corpus(root, config, only=repositories)
        out_dir = results_dir_path.resolve() if results_dir_path is not None else results_dir(root, config)
        documents = []
        for repository in corpus:
            document = run_repository(
                repository,
                source_root=root.parent.parent,
                runs=runs,
                parts=parts or ("flake", "classification", "visual"),
            )
            _write_json(out_dir / f"{repository.id}.json", document)
            documents.append(document)
    except GateHHarnessError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(documents, indent=2, sort_keys=True, ensure_ascii=False))
        return
    for document in documents:
        cost = document.get("cost") or {}
        click.echo(
            f"{document['repository']}: {cost.get('executions')} execution(s), {cost.get('seconds')} s "
            f"({document['executor']})"
        )
        for item in document["measurements"]:
            line = (
                f"  {item['key']['metric']:<14} {item['subject']:<40} "
                f"{item['failures']}/{item['observations']} ({_rate(item['rate'])}) runs={item['runs']}"
            )
            if item.get("unavailable"):
                line += f" unmeasured: {item['unavailable']}"
            click.echo(line)
        click.echo(f"  results: {out_dir / (document['repository'] + '.json')}")


@gate_h.command("record")
@_gate_dir_option
def gate_h_record(gate_dir: Path | None) -> None:
    """Write baseline.json from the current results (commit it with the results)."""

    from lemoncrow.pro.capabilities.review.evaluation.gate_h import GateHHarnessError, record_baseline

    root, config = _config(gate_dir)
    try:
        baseline = record_baseline(root, config)
    except GateHHarnessError as exc:
        raise click.ClickException(str(exc)) from exc
    path = root / config.baseline
    _write_json(path, baseline)
    click.echo(f"baseline: {path} ({len(baseline['repositories'])} repository(ies))")


@gate_h.command("report")
@_gate_dir_option
@_results_dir_option
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
@click.option(
    "--strict",
    is_flag=True,
    help="With --json, also exit 1 on expectation violations (the text report always does; release "
    "qualification reads the JSON and does not take violations into account).",
)
def gate_h_report(gate_dir: Path | None, results_dir_path: Path | None, as_json: bool, strict: bool) -> None:
    """Each measured recipe and surface's qualification under the configured thresholds, vs the baseline.

    Exits 1 when the corpus typing is violated (a negative control that qualifies, an untyped or
    unmeasured subject, ...); the JSON form does so only with ``--strict``.
    """

    from lemoncrow.pro.capabilities.review.evaluation.gate_h import GateHHarnessError, report

    root, config = _config(gate_dir)
    if results_dir_path is not None:
        # An absolute path replaces the gate-h.yaml one (``gate_dir / absolute`` is the absolute path).
        config = replace(config, results=str(results_dir_path.resolve()))
    try:
        result = report(root, config)
    except GateHHarnessError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        if strict and result["expectation_violations"]:
            raise SystemExit(1)
        return
    thresholds = config.thresholds
    state = thresholds.approval_status if thresholds.approved else (thresholds.approval_problem() or "unapproved")
    click.echo(f"Gate H ({result['mode']} mode; thresholds {state})")
    for repo_id, row in sorted(result["repositories"].items()):
        click.echo(f"{repo_id}:")
        for item in row["qualification"]:
            click.echo(f"  {item['state']:<11} {item['subject']}: {item['detail']}")
    for change in result["changes"]:
        click.echo(f"changed: {change['repository']} {change['subject']}: {change['baseline']} -> {change['now']}")
    if result["thresholds_changed_since_baseline"]:
        click.echo("thresholds changed since the baseline was recorded")
    click.echo(
        "expectations checked: "
        + ("yes" if result["expectations_checked"] else "no (no corpus beside the results to type them)")
    )
    for item in result["expectation_violations"]:
        want = item.get("expect") or "untyped"
        click.echo(
            f"expectation: {item['repository']} {item['subject'] or '(repository)'}: expected {want}"
            + (f" ({item['expected_reason']})" if item.get("expected_reason") else "")
            + f", measured {item['state']} ({item['reason']})"
        )
    if result["expectation_violations"]:
        raise SystemExit(1)


@gate_h.command("import")
@_gate_dir_option
@click.option(
    "--review", "review_ref", required=True, help="Review id or reference whose repository the results measure."
)
@click.option(
    "--results",
    "result_paths",
    multiple=True,
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="Results documents to import (default: every one under the results directory).",
)
@click.option(
    "--store-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Local Review store (default: the LemonCrow store).",
)
def gate_h_import(
    gate_dir: Path | None, review_ref: str, result_paths: tuple[Path, ...], store_root: Path | None
) -> None:
    """Store measurements for the Review's repository, where readiness reads them (its latest Snapshot)."""

    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.pro.capabilities.review.evaluation.gate_h import GateHHarnessError, read_results, results_dir
    from lemoncrow.pro.capabilities.review.evidence_qualification import merge_measurements, store_measurements
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    root, config = _config(gate_dir)
    paths = list(result_paths) or sorted(results_dir(root, config).glob("*.json"))
    measurements: list[Any] = []
    try:
        for path in paths:
            document, found = read_results(path)
            source = f"gate-h:{document.get('repository')}@{document.get('measured_at')}"
            measurements.extend(item if item.source else replace(item, source=source) for item in found)
    except (GateHHarnessError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    measurements = merge_measurements(measurements)
    store = ReviewStore(store_root or Path(default_store_root()))
    session = store.resolve_session(review_ref)
    revision = store.latest_revision(session.id) if session is not None else None
    if session is None or revision is None:
        raise click.ClickException(f"no Review with a revision matches {review_ref!r}")
    try:
        count = store_measurements(
            store,
            review_id=session.id,
            revision_id=revision.id,
            repository=str(session.repo_root),
            measurements=measurements,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"imported {count} measurement(s) for {session.repo_root} on {revision_name(revision.revision_number - 1)}"
    )


__all__ = ["gate_h"]
