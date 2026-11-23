"""Regenerate and compare generated files (Phase 9R item 31, ``artifacts.regenerate``).

The trusted base declares each generator under ``analyses.regenerate``: the
command, the output globs it owns and the generator files themselves.

Two stages per side. Stage A (sandbox) copies the Snapshot, records the digest
of every committed output before any reviewed code runs, runs the generator
sealed (its processes are children of the driver, which kills whatever they
leave behind and records any interference) and copies the regenerated outputs
out (regular files only, bounded, validated names). Stage B
(``artifact_inspection.compare_regenerated``) compares them with the committed
digests and, for a diff, with the committed text: on the host over the
copied-out bytes when they fit the report, otherwise in the sandbox by the
sealed driver after its kill sweep, accepted only when the seal recorded no
interference. A process of the generator can no longer rewrite the comparison.

Generators come from the trusted base, never from the head alone: the command
is base config, and the declared generator files are overlaid from the base
side onto the head workspace (sources the generator reads still come from the
Change). A generated file the Change edits that does not regenerate
identically is flagged as hand-edited; one the Change did not touch that no
longer regenerates identically is stale; one the base already fails to
regenerate is pre-existing and not attributed to the Change.
"""

from __future__ import annotations

import inspect
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from lemoncrow.pro.capabilities.review import artifact_inspection
from lemoncrow.pro.capabilities.review.analysis_common import (
    MAX_TIMEOUT,
    REGENERATE_ENTRY,
    AnalysisSpec,
    Evaluation,
    Finding,
    Observation,
    SideRun,
    bounded,
    build_driver,
    decode_outputs,
    first_line,
    inspection_sides,
    join_path,
    omitted_outputs,
    relative_path,
    seal_tampered,
    sealed_inspection_problem,
    sealed_problem,
    valid_spec_id,
)
from lemoncrow.pro.capabilities.review.artifact_inspection import NOT_REGULAR, compare_regenerated
from lemoncrow.pro.environment.runners import WorkspaceOverlay

_MAX_ARGS: Final[int] = 64
_MAX_OUTPUTS: Final[int] = 32
_MAX_GENERATOR_FILES: Final[int] = 32
_MAX_FILES: Final[int] = 3000
_STATES: Final[frozenset[str]] = frozenset({"unchanged", "uncommitted", "orphaned", "differs", "unverifiable"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SETUP_HINT: Final[str] = (
    "Declare the generator under analyses.regenerate in the trusted-base .lemoncrow/review.yaml "
    "(command, outputs, generator files); the generator's tools must be on the sandbox PATH and work offline."
)

# Stage A (sandbox): commit digests before reviewed code runs, generate sealed, copy out; or,
# when the outputs do not fit the report, compare here once the seal holds.
DRIVER: Final[str] = build_driver(
    r"""
def listing(root, patterns):
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            full = os.path.join(base, name)
            path = rel(full, root)
            if any(p.match(path) for p in patterns):
                out.append((path, full))
                if len(out) >= 3000:
                    return out
    return out


def digest(full):
    try:
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return "not-a-regular-file"
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return "not-a-regular-file"
        value = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            value.update(chunk)
        return value.hexdigest()


def main():
    seal_start()
    command = [str(item) for item in P.get("command") or []]
    if not command:
        finish(3, "no generator command")
    patterns = [globre(item) for item in P.get("outputs") or []]
    src = copy_root()
    tree = dir_identity(src)
    # Before any reviewed code runs: what the revision commits for every output glob, and
    # (bounded) its bytes, for the diffs of a comparison made in the sandbox.
    committed = listing(src, patterns)
    R["before"] = {path: digest(full) for path, full in committed}
    committed = dict(read_files(committed, src, 64 << 20, limit=3000, bound="inspection")[0])
    if P.get("clean"):
        for path in R["before"]:
            try:
                os.unlink(os.path.join(src, path))
            except OSError:
                pass
    code, out = sealed_run(command, src)
    R["generator"] = {"exit": code, "output": (out or "")[-4000:]}
    if code is None:
        finish(2, out)
    require_same_dir(src, tree)
    found = listing(src, patterns)
    if fits_report(found):
        R["inspection"] = "host"
        R["outputs"], R["omitted"] = copy_out(found, src, copy_budget(), limit=3000)
        finish(0)
    # Too large to copy out: the same comparison runs here, after the kill sweep (no reviewed
    # code is alive). The host accepts it only if the seal recorded no interference at all.
    R["inspection"] = "sealed_in_sandbox"
    regenerated, omitted = read_files(found, src, INSPECT_BYTES, limit=3000, bound="inspection")
    rows, R["heavy"] = compare_regenerated(
        R.pop("before"),
        dict(regenerated),
        {row["path"]: row["why"] for row in omitted},
        lambda path: committed[path].decode("utf-8", "replace") if path in committed else None,
        3000,
    )
    R["files"] = [row for row in rows if row["state"] != "unchanged"]
    R["unchanged_count"] = len(rows) - len(R["files"])
    finish(0)
""",
    sealed=True,
    shared=inspect.getsource(artifact_inspection),
)


def _sandbox_rows(report: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str], int, str]:
    """The comparison a sealed driver made in the sandbox (rows, diffs, unchanged count), shape-checked."""

    rows, heavy, unchanged = report.get("files"), report.get("heavy"), report.get("unchanged_count")
    if not isinstance(rows, list) or len(rows) > _MAX_FILES or not isinstance(unchanged, int) or unchanged < 0:
        return [], {}, 0, "the in-sandbox comparison reported a malformed result"
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("state") not in _STATES or row.get("state") == "unchanged":
            return [], {}, 0, "the in-sandbox comparison reported a malformed row"
        path = str(row.get("path") or "")
        if relative_path(path, allow_root=False) != path:
            return [], {}, 0, f"the in-sandbox comparison reported an unsafe path {path[:200]!r}"
        out.append(dict(row))
    diffs = {str(k): v for k, v in heavy.items() if isinstance(v, str)} if isinstance(heavy, Mapping) else {}
    return out, diffs, unchanged, ""


def compare_outputs(
    spec: AnalysisSpec,
    report: Mapping[str, Any],
    exit_code: int | None,
    committed_text: Callable[[str], str | None],
) -> tuple[dict[str, Any] | None, str]:
    """Stage B: regenerated bytes against the committed digests; ``(None, why)`` if untrusted.

    ``inspection: "host"`` compares the copied-out bytes here, with the
    Snapshot's own text for diffs; ``"sealed_in_sandbox"`` takes the comparison
    the sealed driver made with the same code after its kill sweep, accepted
    only when the seal recorded no interference.
    """

    problem = sealed_problem(report, exit_code)
    if problem:
        return None, problem
    out: dict[str, Any] = {
        key: report[key]
        for key in ("v", "entry", "spec", "exit", "reason", "generator", "notes", "seal")
        if key in report
    }
    if report.get("exit") != 0:
        return out, ""
    unchanged = 0
    if report.get("inspection") == "sealed_in_sandbox":
        problem = sealed_inspection_problem(report)
        if problem:
            return None, problem
        rows, heavy, unchanged, problem = _sandbox_rows(report)
        if problem:
            return None, problem
        out["inspection"] = "sealed_in_sandbox"
    else:
        files, problem = decode_outputs(report, limit=_MAX_FILES)
        if problem:
            return None, problem
        raw_before = report.get("before") if isinstance(report.get("before"), Mapping) else {}
        assert isinstance(raw_before, Mapping)
        before: dict[str, str] = {}
        for path, value in list(raw_before.items())[:_MAX_FILES]:
            if relative_path(path, allow_root=False) != path or not (value == NOT_REGULAR or _SHA256.match(str(value))):
                return None, f"the sealed build recorded a malformed committed digest for {str(path)[:200]!r}"
            before[str(path)] = str(value)
        rows, heavy = compare_regenerated(
            before, files, omitted_outputs(report), lambda path: committed_text(join_path(spec.root, path)), _MAX_FILES
        )
        out["inspection"] = "host"
        tampered = seal_tampered(report)
        if tampered:
            out["tampered"] = tampered
    generator = report.get("generator") if isinstance(report.get("generator"), Mapping) else {}
    assert isinstance(generator, Mapping)
    code = generator.get("exit")
    out.update(files=rows, file_count=len(rows) + unchanged, unchanged_count=unchanged, heavy=heavy)
    if code != 0:
        out.update(exit=4, reason=f"the generator exited {code}")
    else:
        out["exit"] = 1 if any(row["state"] != "unchanged" for row in rows) else 0
    return out, ""


def _argv(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, str):
        try:
            parts = tuple(shlex.split(value))
        except ValueError:
            return None
    elif isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        parts = tuple(str(item) for item in value)
    else:
        return None
    if not parts or len(parts) > _MAX_ARGS or any(len(part) > 2000 or "\0" in part for part in parts):
        return None
    return parts


def parse_specs(rows: Sequence[Any], errors: list[str]) -> list[AnalysisSpec]:
    """``analyses.regenerate`` rows (the only source: generators are never discovered from the head)."""

    specs: list[AnalysisSpec] = []
    for index, row in enumerate(rows):
        where = f"analyses.regenerate[{index}]"
        if not isinstance(row, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        unknown = sorted(set(row) - {"id", "title", "command", "outputs", "generator", "root", "clean", "timeout"})
        if unknown:
            errors.append(f"{where} has unknown keys: {', '.join(unknown)}")
            continue
        spec_id = row.get("id")
        command = _argv(row.get("command"))
        root = relative_path(row.get("root"))
        outputs_raw = row.get("outputs")
        generator_raw = row.get("generator") or []
        outputs = (
            [str(item).strip() for item in outputs_raw if isinstance(item, str) and str(item).strip()]
            if isinstance(outputs_raw, list)
            else []
        )
        generator = (
            [relative_path(item, allow_root=False) for item in generator_raw]
            if isinstance(generator_raw, list)
            else [None]
        )
        try:
            timeout = max(10, min(int(row.get("timeout") or 300), MAX_TIMEOUT))
        except (TypeError, ValueError):
            errors.append(f"{where}.timeout must be a number of seconds")
            continue
        if not valid_spec_id(spec_id):
            errors.append(f"{where}.id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
        elif command is None:
            errors.append(f"{where}.command must be a string or a list of at most {_MAX_ARGS} arguments")
        elif root is None:
            errors.append(f"{where}.root must be a repository-relative directory")
        elif (
            not outputs
            or len(outputs) > _MAX_OUTPUTS
            or any(item.startswith(("/", "-")) or ".." in item for item in outputs)
        ):
            errors.append(f"{where}.outputs must list 1-{_MAX_OUTPUTS} relative globs of the generated files")
        elif any(item is None for item in generator) or len(generator) > _MAX_GENERATOR_FILES:
            errors.append(f"{where}.generator must list at most {_MAX_GENERATOR_FILES} repository-relative files")
        else:
            specs.append(
                AnalysisSpec(
                    entry_id=REGENERATE_ENTRY,
                    id=str(spec_id),
                    tool="command",
                    root=root,
                    title=str(row.get("title") or "")[:200] or f"Regenerate · {spec_id}",
                    origin="configured",
                    source=where,
                    options={"command": list(command), "outputs": outputs, "clean": row.get("clean") is True},
                    timeout=timeout,
                    trusted_inputs=tuple(str(item) for item in generator),
                    setup_hint=_SETUP_HINT,
                )
            )
    return specs


def payload(spec: AnalysisSpec) -> dict[str, Any]:
    return {
        "command": list(spec.options.get("command") or ()),
        "outputs": list(spec.options.get("outputs") or ()),
        "clean": spec.options.get("clean") is True,
    }


def overlay(
    spec: AnalysisSpec, *, base_identity: str, read_base: Callable[[str], str | None]
) -> tuple[WorkspaceOverlay | None, str]:
    """The trusted base's generator files written over the head workspace."""

    files: list[tuple[str, str | None]] = []
    for path in spec.trusted_inputs:
        text = read_base(path)
        if text is None:
            return None, f"generator file {path} does not exist on the trusted base; the head's version is never run"
        files.append((path, text))
    if not files:
        return None, ""
    if not base_identity:
        return None, "the trusted base has no commit identity to take the generator files from"
    return WorkspaceOverlay(source=base_identity, files=tuple(files)), ""


def evaluate(
    spec: AnalysisSpec,
    head: SideRun | None,
    base: SideRun | None,
    *,
    changed_paths: Sequence[str],
    generator_edited: Sequence[str] = (),
) -> Evaluation:
    """Flag hand-edited, stale and uncommitted generated files; pre-existing drift stays quiet."""

    if head is None or head.state in {"not_run", "unavailable"}:
        return Evaluation(
            "NOT_RUN",
            [head.reason if head is not None and head.reason else "the generator did not run"],
            setup_hint=_SETUP_HINT,
        )
    if head.state == "error":
        return Evaluation("UNKNOWN", [head.reason or "the regenerate driver returned no usable result"])
    generator = head.report.get("generator") if isinstance(head.report.get("generator"), Mapping) else {}
    assert isinstance(generator, Mapping)
    if head.state == "failed":
        if base is not None and base.state == "failed":
            return Evaluation(
                "NOT_RUN",
                [
                    "the trusted-base generator fails on the base as well: "
                    + first_line(generator.get("output") or head.reason)
                ],
                setup_hint=_SETUP_HINT,
            )
        return Evaluation(
            "FAIL",
            [f"the generator fails on the Change ({head.reason})"],
            [
                Finding(
                    rule="generator_failed",
                    category="failed",
                    subject=f"{spec.id}::generator",
                    title=f"Generator fails · {spec.title or spec.id}"[:200],
                    summary=bounded(f"{head.reason}: {first_line(generator.get('output'))}"),
                    severity="high",
                    critical=True,
                    facts=(("Command", bounded(" ".join(map(str, spec.options.get("command") or ())), 200)),),
                    base=Observation(base.state, base.reason, "") if base is not None else None,
                    head=Observation("failed", bounded(head.reason, 200), str(generator.get("exit"))),
                )
            ],
        )
    changed = set(changed_paths)
    base_rows: dict[str, Mapping[str, Any]] = {}
    # A base run that interfered with its own analysis is not a baseline for "pre-existing".
    base_tampered = str(base.report.get("tampered") or "") if base is not None and base.ran else ""
    if base is not None and base.ran and not base_tampered:
        base_rows = {str(row.get("path")): row for row in base.report.get("files") or () if isinstance(row, Mapping)}
    findings: list[Finding] = []
    counts: dict[str, int] = {}
    diffs: list[str] = []
    unverifiable: list[str] = []
    for row in head.report.get("files") or ():
        if not isinstance(row, Mapping):
            continue
        path = str(row.get("path") or "")
        state = str(row.get("state") or "")
        repo_path = join_path(spec.root, path)
        if state == "unchanged":
            counts["clean"] = counts.get("clean", 0) + 1
            continue
        if state == "unverifiable":
            counts["unverifiable"] = counts.get("unverifiable", 0) + 1
            unverifiable.append(f"{repo_path} ({row.get('why') or 'not copied out'})")
            continue
        before = base_rows.get(path)
        if repo_path not in changed and before is not None and before.get("state") == state and state != "unchanged":
            counts["pre_existing"] = counts.get("pre_existing", 0) + 1
            continue
        if state == "differs":
            rule = "hand_edited" if repo_path in changed else "stale"
            title = "Hand-edited generated file" if rule == "hand_edited" else "Generated file is stale"
            summary = (
                f"The Change edits {repo_path}, but regenerating it from the Change's sources with the "
                "trusted-base generator produces different content; the edit is lost on the next regeneration."
                if rule == "hand_edited"
                else f"Regenerating {repo_path} from the Change's sources produces different content than is committed."
            )
        elif state == "uncommitted":
            rule, title = "uncommitted", "Generated file not committed"
            summary = f"The generator produces {repo_path}, which the Change does not commit."
        else:
            rule, title = "orphaned", "Committed file no longer generated"
            summary = f"{repo_path} is committed but the generator no longer produces it."
        if generator_edited:
            summary += (
                " The Change also edits the generator ("
                + ", ".join(generator_edited[:3])
                + "); the trusted-base generator ran."
            )
        counts[rule] = counts.get(rule, 0) + 1
        diff = head.heavy.get(f"diff:{path}", "")
        if diff:
            diffs.append(diff)
        findings.append(
            Finding(
                rule=rule,
                category="hand_edited" if rule == "hand_edited" else "generated",
                subject=f"{spec.id}::{path}",
                title=f"{title} · {repo_path}"[:200],
                summary=bounded(summary),
                severity="high" if rule == "hand_edited" else "medium",
                change="added" if rule == "uncommitted" else "removed" if rule == "orphaned" else "changed",
                paths=(repo_path,),
                facts=(
                    ("Generator", spec.title or spec.id),
                    ("State", rule.replace("_", " ")),
                    *((("Changed lines", str(row.get("changed_lines"))),) if row.get("changed_lines") else ()),
                ),
                base=Observation("committed", repo_path, str(row.get("committed") or "")),
                head=Observation("regenerated", rule.replace("_", " "), str(row.get("regenerated") or "")),
            )
        )
    # A comparison made in the sandbox reports regenerated-identical files as a count only.
    unchanged = head.report.get("unchanged_count")
    if isinstance(unchanged, int) and unchanged > 0:
        counts["clean"] = counts.get("clean", 0) + unchanged
    flagged = sum(value for key, value in counts.items() if key not in {"clean", "pre_existing", "unverifiable"})
    tampered = str(head.report.get("tampered") or "")
    if flagged:
        status = "FAIL"
        reasons = [
            ", ".join(
                f"{value} {key.replace('_', ' ')}"
                for key, value in sorted(counts.items())
                if key not in {"clean", "unverifiable"}
            )
            + " generated file(s) do not match the trusted-base generator"
        ]
    elif tampered or unverifiable:
        status = "UNKNOWN"
        reasons = []
    elif counts.get("pre_existing"):
        status = "UNKNOWN"
        reasons = [
            f"{counts['pre_existing']} generated file(s) already differ on the trusted base; not attributed to this Change"
        ]
    else:
        status = "PASS"
        reasons = [f"{counts.get('clean', 0)} generated file(s) regenerate identically"]
    if tampered:
        reasons.insert(
            0,
            f"the generator interfered with the analysis ({tampered}); its outputs are not trusted, "
            "so no generated file is shown as clean",
        )
    if unverifiable:
        reasons.append(
            f"{len(unverifiable)} generated file(s) could not be compared: "
            + "; ".join(unverifiable[:3])
            + (" ..." if len(unverifiable) > 3 else "")
        )
    if base_tampered:
        reasons.append(f"base side not compared: the base generator interfered with the analysis ({base_tampered})")
    return Evaluation(
        status,  # type: ignore[arg-type]
        reasons,
        findings,
        summary={
            "counts": counts,
            "files": int(head.report.get("file_count") or 0),
            "generator_edited": list(generator_edited)[:10],
            "inspection": inspection_sides(head, base),
        },
        heavy={"regenerate.diff": "\n".join(diffs)} if diffs else {},
        setup_hint="",
    )


__all__ = ["DRIVER", "compare_outputs", "evaluate", "overlay", "parse_specs", "payload"]
