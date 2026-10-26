"""What a pull request review looks at: the diff of each changed target, and the batches that keep a call within a size.

The pull request reviewer (``ai_review.run_independent_review(pr_fields=True)``) looks at every changed code target,
not only at the ones a keyword marks. That is many targets, so they travel in *batches*: the targets of one file, or of
several small files, go into one investigation call while the call's prompt stays under :data:`BATCH_INPUT_TOKENS`.
Each call is also shown the change itself (the unified diff of its targets, removed lines included), because a removed
guard or a changed condition cannot be seen in the new text alone.

Pure helpers, with no model and no store writes: the packet's file rows in, text and groupings out. The policy that
decides *which* targets are worth a look, and in what order, is in ``ai_review``.
"""

from __future__ import annotations

import contextvars
import difflib
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Final

#: The most input tokens (estimated, four characters a token) one investigation call carries: its instructions, the
#: code of its targets, their diff and their evidence references. A batch is filled up to this and never past it,
#: except that one target alone may be larger (it is cut to :data:`MAX_EXCERPT_CHARS` first). A model-API route also
#: carries a repository-context pack (``repo_context``), which is subtracted from the room a batch has.
BATCH_INPUT_TOKENS: Final[int] = 12_000
#: What a prompt holds besides the code of the targets: the instructions, the schema's rules, the fixed part of the
#: evidence registry, the rules and conventions that apply.
PROMPT_OVERHEAD_TOKENS: Final[int] = 2_500
#: What each target adds besides its code: its two evidence references and its place in the prompt.
TARGET_OVERHEAD_TOKENS: Final[int] = 150
#: A batch always has at least this much room for code, however large a context pack is.
MIN_ROOM_TOKENS: Final[int] = 3_000
#: The most diff text one file contributes to a call. More is cut (the prompt says how many hunks were left out).
MAX_DIFF_CHARS: Final[int] = 16_000
#: Investigation calls that run at the same time.
MAX_PARALLEL_CALLS: Final[int] = 6
#: The batches of a change that run before anything is judged: one full parallel wave. A change of ordinary size
#: (a review is three to five batches at the median) never gets past it, so nothing here changes what it costs.
FIRST_WAVE: Final[int] = MAX_PARALLEL_CALLS
#: Past the first wave a change is covered, best ranked first, in rounds of this many batches (half a wave).
TAIL_ROUND: Final[int] = MAX_PARALLEL_CALLS // 2


def estimate_tokens(text: str) -> int:
    """A local, deterministic token estimate (four characters per token, rounded up). Never a provider count."""

    return (len(text) + 3) // 4


def room_tokens(*, context_tokens: int = 0) -> int:
    """The tokens of code and diff one call may carry, after its fixed part and a *context_tokens* pack."""

    return max(MIN_ROOM_TOKENS, BATCH_INPUT_TOKENS - PROMPT_OVERHEAD_TOKENS - max(context_tokens, 0))


# --------------------------------------------------------------------------- #
# the packet's file rows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HunkText:
    """One ``@@`` block of a file's diff, as the packet holds it. ``body`` is empty when the capture did not keep it."""

    header: str
    body: str
    old_start: int = 0
    old_lines: int = 0
    new_start: int = 0
    new_lines: int = 0


@dataclass(frozen=True, slots=True)
class FileFacts:
    """What the packet knows about one changed file that decides whether and how it is looked at."""

    path: str
    status: str = "modified"
    category: str = "production"
    binary: bool = False
    hunks: tuple[HunkText, ...] = ()


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def file_facts(files: object) -> dict[str, FileFacts]:
    """``{path: FileFacts}`` from a stored packet's ``files`` (anything unreadable is simply absent)."""

    out: dict[str, FileFacts] = {}
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        return out
    for row in files:
        if not isinstance(row, Mapping) or not isinstance(row.get("path"), str) or not row["path"]:
            continue
        raw_hunks = row.get("hunks")
        hunks: list[HunkText] = []
        if isinstance(raw_hunks, Sequence) and not isinstance(raw_hunks, (str, bytes)):
            for hunk in raw_hunks:
                if not isinstance(hunk, Mapping):
                    continue
                hunks.append(
                    HunkText(
                        header=str(hunk.get("header") or ""),
                        body=str(hunk.get("patch") or ""),
                        old_start=_int(hunk.get("old_start")),
                        old_lines=_int(hunk.get("old_lines")),
                        new_start=_int(hunk.get("new_start")),
                        new_lines=_int(hunk.get("new_lines")),
                    )
                )
        out[row["path"]] = FileFacts(
            path=row["path"],
            status=str(row.get("status") or "modified"),
            category=str(row.get("category") or "production"),
            binary=bool(row.get("is_binary")),
            hunks=tuple(hunks),
        )
    return out


# --------------------------------------------------------------------------- #
# the diff a call is shown
# --------------------------------------------------------------------------- #


def _hunk_text(hunk: HunkText) -> str:
    body = hunk.body
    if body and not body.endswith("\n"):
        body += "\n"
    return f"{hunk.header}\n{body}" if hunk.header else body


def _fallback_diff(old: str | None, new: str | None, path: str) -> str:
    """A unified diff computed from the two texts, for a file whose hunk bodies the capture did not keep."""

    if old is None or new is None:
        return ""
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


class DiffView:
    """The diff rows of the files a review looks at, and what they cost in a prompt.

    ``ordinals`` (a target's ``hunk_ordinals``) say which of a file's hunks a target owns; ``None`` or an empty tuple
    means the target is the whole file (a file-level target) and owns every hunk. ``texts`` reads a side of a file
    (``(path, "old" | "new")``) and is used only when a file's hunks carry no body.
    """

    def __init__(
        self,
        facts: Mapping[str, FileFacts],
        *,
        texts: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._facts = facts
        self._texts = texts
        self._fallbacks: dict[str, str] = {}
        self._rows: dict[tuple[str, tuple[int, ...]], dict[str, Any]] = {}

    def _usable(self, path: str) -> bool:
        facts = self._facts.get(path)
        return facts is not None and any(hunk.body for hunk in facts.hunks)

    def _selected(self, path: str, ordinals: Sequence[int]) -> list[int]:
        facts = self._facts.get(path)
        if facts is None:
            return []
        wanted = [index for index in ordinals if 0 <= index < len(facts.hunks)]
        return sorted(set(wanted)) if wanted else list(range(len(facts.hunks)))

    def _fallback(self, path: str) -> str:
        if path not in self._fallbacks:
            old = self._texts(path, "old") if self._texts is not None else None
            new = self._texts(path, "new") if self._texts is not None else None
            self._fallbacks[path] = _fallback_diff(old, new, path)
        return self._fallbacks[path]

    def row(self, path: str, ordinals_per_target: Sequence[Sequence[int]]) -> dict[str, Any]:
        """The prompt row for *path*: its status and the unified diff of the hunks the given targets own."""

        facts = self._facts.get(path)
        status = facts.status if facts is not None else "modified"
        if status == "added":
            # Every line is new: the new text is the diff, and repeating it would only double the cost.
            return {"path": path, "status": "added", "diff": ""}
        if facts is None:
            return {"path": path, "status": status, "diff": "(the diff is not available)"}
        chosen = sorted({index for ordinals in ordinals_per_target for index in self._selected(path, ordinals)})
        key = (path, tuple(chosen))
        if key not in self._rows:
            self._rows[key] = self._render(path, facts, status, chosen)
        return dict(self._rows[key])

    def _render(self, path: str, facts: FileFacts, status: str, chosen: Sequence[int]) -> dict[str, Any]:
        if self._usable(path):
            parts = [_hunk_text(facts.hunks[index]) for index in chosen]
            text, left = "", 0
            for position, part in enumerate(parts):
                if len(text) + len(part) > MAX_DIFF_CHARS:
                    if not text:
                        text = part[:MAX_DIFF_CHARS] + "\n(this hunk is cut)\n"
                        left = len(parts) - position - 1
                    else:
                        left = len(parts) - position
                    break
                text += part
            if left:
                text += f"(+{left} more hunk{'s' if left != 1 else ''} of this file not shown)\n"
            return {"path": path, "status": status, "diff": text}
        fallback = self._fallback(path)
        if len(fallback) > MAX_DIFF_CHARS:
            fallback = fallback[:MAX_DIFF_CHARS] + "\n(the rest of the diff is cut)\n"
        return {"path": path, "status": status, "diff": fallback or "(the diff is not available)"}

    def rows(self, targets: Sequence[Any]) -> list[dict[str, Any]]:
        """Diff rows for *targets* (``ReviewTarget``), one per file, in the order the files first appear."""

        by_path: dict[str, list[Sequence[int]]] = {}
        for target in targets:
            by_path.setdefault(target.path, []).append(tuple(getattr(target, "hunk_ordinals", ()) or ()))
        return [self.row(path, ordinals) for path, ordinals in by_path.items()]

    def tokens(self, targets: Sequence[Any]) -> int:
        return sum(estimate_tokens(str(row["diff"])) + 40 for row in self.rows(targets))

    def window(self, path: str, targets: Sequence[Any]) -> tuple[int, int] | None:
        """The first and last new-side line of the hunks shown for *path* to *targets*, or ``None`` when none is."""

        facts = self._facts.get(path)
        if facts is None or not facts.hunks:
            return None
        ordinals = [tuple(getattr(target, "hunk_ordinals", ()) or ()) for target in targets if target.path == path]
        chosen = sorted({index for item in ordinals for index in self._selected(path, item)})
        starts = [facts.hunks[i].new_start for i in chosen if facts.hunks[i].new_lines > 0]
        ends = [facts.hunks[i].new_start + facts.hunks[i].new_lines - 1 for i in chosen if facts.hunks[i].new_lines > 0]
        return (min(starts), max(ends)) if starts and ends else None


# --------------------------------------------------------------------------- #
# batches
# --------------------------------------------------------------------------- #


def chunks[T](items: Sequence[T], cost: Callable[[Sequence[T]], int], room: int) -> list[list[T]]:
    """*items* in order, cut into the fewest runs whose ``cost`` fits *room*; one item alone may exceed it.

    Greedy and order-preserving, so the targets of a file stay together in line order and the result is the same on
    every run.
    """

    out: list[list[T]] = []
    current: list[T] = []
    for item in items:
        if current and cost([*current, item]) > room:
            out.append(current)
            current = []
        current.append(item)
    if current:
        out.append(current)
    return out


def pack[T](groups: Sequence[T], cost: Callable[[Sequence[T]], int], room: int) -> list[list[T]]:
    """*groups* (each already small enough to travel together, never split) placed first-fit into as few calls as fit *room*.

    The caller orders *groups* (most important first), so the first bins hold them. Deterministic.
    """

    bins: list[list[T]] = []
    for group in groups:
        for existing in bins:
            if cost([*existing, group]) <= room:
                existing.append(group)
                break
        else:
            bins.append([group])
    return bins


# --------------------------------------------------------------------------- #
# running the batches
# --------------------------------------------------------------------------- #


def run_rounds[T, R](
    items: Sequence[T],
    run: Callable[[T], R],
    *,
    empty: Callable[[R], bool],
    stop_on_clean: bool = True,
    protected: int = 0,
    first: int = FIRST_WAVE,
    size: int = TAIL_ROUND,
    workers: int = MAX_PARALLEL_CALLS,
) -> list[R]:
    """Run *run* over *items*, best ranked first, and return the results of the ones it ran: always a prefix of *items*.

    The first ``max(first, protected)`` items run together, *workers* at a time; *protected* is how many leading items
    must run whatever happens (the questions a repository's own rules ask). The rest follow in rounds of *size*, and a
    round in which every result is *empty* (a call that came back clean) ends the run: what is ranked below a clean
    round is not run. The items are ranked by how likely they are to hold a defect, so a round that found nothing says
    the ones below it are less likely to than the ones already looked at. With *stop_on_clean* false every item runs.
    Each call runs in a copy of the caller's context (a bound intelligence policy holds in every thread).
    """

    def go(batch: Sequence[T]) -> list[R]:
        if len(batch) <= 1:
            return [run(item) for item in batch]
        contexts = [contextvars.copy_context() for _ in batch]
        with ThreadPoolExecutor(max_workers=min(len(batch), workers)) as pool:
            return list(pool.map(lambda pair: pair[0].run(run, pair[1]), zip(contexts, batch, strict=True)))

    if not stop_on_clean:
        return go(items)
    ahead = max(first, protected)
    results = go(items[:ahead])
    position = ahead
    while position < len(items):
        batch = items[position : position + size]
        done = go(batch)
        results += done
        position += len(batch)
        if all(empty(result) for result in done):
            break
    return results
