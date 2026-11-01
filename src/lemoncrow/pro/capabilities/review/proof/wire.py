"""The plan and results files the proof jobs hand to each other, and their vocabulary.

The plan is written by the plan job, which runs no pull request code, so the report job trusts it (thread ids,
finding ids, the generated test). The results file is written by the run job, which executes pull request code:
in a plain (unsandboxed) runner that code can rewrite the file, so the report job validates every field of it
against this vocabulary and bounds every text before it is shown, and the check says which sandbox ran it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

PLAN_SCHEMA: Final[str] = "lemoncrow.proof.plan/v1"
RESULTS_SCHEMA: Final[str] = "lemoncrow.proof.results/v1"
CHECK_NAME: Final[str] = "LemonCrow Proof"

#: What a reproduction showed about its finding.
REPRO_OUTCOMES: Final[tuple[str, ...]] = ("supported", "contradicted", "inconclusive", "could_not_run")
#: What running one of the pull request's added or changed tests on the base commit showed.
RED_OUTCOMES: Final[tuple[str, ...]] = ("fails_on_base", "passes_on_base", "could_not_run")
#: One recorded command run.
RUN_STATUSES: Final[tuple[str, ...]] = ("passed", "failed", "timeout", "error", "not_run")
SANDBOXES: Final[tuple[str, ...]] = ("bubblewrap", "none")
SIDES: Final[tuple[str, ...]] = ("head", "base")

SHA: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
#: A repository-relative path the proof may write or run: no absolute path, no ``..``, no shell metacharacter.
SAFE_PATH: Final[re.Pattern[str]] = re.compile(r"^(?!/)(?!-)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9_./@+-]{1,300}$")
TEST_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")

MAX_TEXT: Final[int] = 600
MAX_OUTPUT_TAIL: Final[int] = 6000
MAX_COMMAND_ARGS: Final[int] = 80
MAX_ITEMS: Final[int] = 200
#: Results files larger than this are refused outright.
MAX_RESULTS_BYTES: Final[int] = 4 * 1024 * 1024


class ProofFileError(ValueError):
    """A plan or results file that is not one this version wrote; the message says what is wrong."""


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def dump(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def write(path: Path, document: Mapping[str, Any]) -> bytes:
    raw = dump(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def load(path: Path, schema: str, *, limit: int = MAX_RESULTS_BYTES) -> tuple[dict[str, Any], bytes]:
    """The JSON object in *path* and its bytes; :class:`ProofFileError` when it is not a *schema* document."""

    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        raise ProofFileError(f"cannot read {path}: {exc.strerror or exc}") from exc
    if len(raw) > limit:
        raise ProofFileError(f"{path} is larger than {limit} bytes")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProofFileError(f"{path} is not JSON") from exc
    if not isinstance(document, dict) or document.get("schema") != schema:
        raise ProofFileError(f"{path} is not a {schema} document")
    return document, raw


def text(value: object, limit: int = MAX_TEXT) -> str:
    """*value* as bounded text: anything that is not a string reads as empty."""

    return value[:limit] if isinstance(value, str) else ""


def count(value: object, *, ceiling: int = 10**12) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= ceiling else 0


def one_of(value: object, vocabulary: Sequence[str], default: str) -> str:
    return value if isinstance(value, str) and value in vocabulary else default


def rows(value: object, limit: int = MAX_ITEMS) -> list[dict[str, Any]]:
    return [row for row in value[:limit] if isinstance(row, dict)] if isinstance(value, list) else []


def clean_run(raw: object) -> dict[str, Any] | None:
    """One recorded run from an untrusted results file, every field in vocabulary; ``None``: not a run."""

    if not isinstance(raw, dict):
        return None
    side = raw.get("side")
    if side not in SIDES:
        return None
    command = raw.get("command")
    argv = (
        [item[:300] for item in command[:MAX_COMMAND_ARGS] if isinstance(item, str)]
        if isinstance(command, list)
        else []
    )
    exit_code = raw.get("exit_code")
    return {
        "side": side,
        "status": one_of(raw.get("status"), RUN_STATUSES, "not_run"),
        "command": argv,
        "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
        "duration_ms": count(raw.get("duration_ms"), ceiling=10**8),
        "output_tail": text(raw.get("output_tail"), MAX_OUTPUT_TAIL),
        "sandbox": one_of(raw.get("sandbox"), SANDBOXES, "none"),
    }


def recorded(runs: Sequence[Mapping[str, Any]]) -> bool:
    """Whether *runs* holds a run that actually executed (a pass is never claimed without one)."""

    return any(run.get("status") in {"passed", "failed"} and run.get("exit_code") is not None for run in runs)
