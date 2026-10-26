"""The golden-comment benchmark's inputs: a corpus of pull requests, and the answer key that lists the issues in each.

The corpus is an ordinary real-PR corpus (:mod:`real_prs`: pinned ``base_sha``..``head_sha``, ``visibility: private``,
no source code) plus two additions that the real-PR loader ignores:

* ``golden_comments``: where the answer key lives (a git repository), the commit it is pinned to, and the directory of
  its JSON files. The key has no licence, so it is never copied into this repository: it is cloned at run time and a
  checkout at any other commit is refused. A review of the same pull requests against a changed key would not be
  comparable with an earlier one.
* ``golden`` on each pull request: which file of the key and which entry of it (1-based, in file order) lists that pull
  request's issues, the entry's title (checked against the key, so a reordered key cannot silently score the wrong
  pull request), the language, and the pull request's number in the public set the ids came from.

The key's files are ``[{"pr_title": ..., "comments": [{"comment": ..., "severity": ...}, ...]}, ...]``. A comment of
severity ``Low`` is **excluded** from scoring: it is neither a true positive, a false negative nor (when a reviewer's
comment matches it) a false positive. That is the key's own scoring rule, and it is what :attr:`GoldenComment.counted`
says.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from lemoncrow.pro.capabilities.review.evaluation.corpus import CorpusError
from lemoncrow.pro.capabilities.review.evaluation.real_prs import RealPrCorpus, load_real_pr_corpus

GOLDEN_SEVERITIES: Final[tuple[str, ...]] = ("Critical", "High", "Medium", "Low")
#: Severities that count as neither a true positive, a false negative nor a false positive.
EXCLUDED_SEVERITIES: Final[frozenset[str]] = frozenset({"Low"})
_SEVERITY_RANK: Final[Mapping[str, int]] = {name: index for index, name in enumerate(GOLDEN_SEVERITIES)}
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_FILE = re.compile(r"^[\w.-]+\.json$")


class GoldenError(RuntimeError):
    """The answer key cannot be used as asked, for a reason the person running the benchmark can act on."""


@dataclass(frozen=True, slots=True)
class GoldenComment:
    """One issue the key lists for a pull request. ``id`` is ``G<n>`` in the key's own order."""

    id: str
    text: str
    severity: str

    @property
    def counted(self) -> bool:
        return self.severity not in EXCLUDED_SEVERITIES

    @property
    def rank(self) -> int:
        """Lower is more severe; used only to break ties between equally good matches."""

        return _SEVERITY_RANK.get(self.severity, len(_SEVERITY_RANK))


@dataclass(frozen=True, slots=True)
class GoldenSource:
    """Where the answer key is, and the one commit of it this corpus was built against."""

    repository: str
    commit: str
    directory: str
    licence: str = ""


@dataclass(frozen=True, slots=True)
class GoldenRef:
    """Which entry of the key belongs to a pull request."""

    file: str
    index: int
    title: str
    language: str
    source_pull: int = 0


@dataclass(frozen=True, slots=True)
class GoldenCorpus:
    """A real-PR corpus with the answer-key addresses of its pull requests."""

    corpus: RealPrCorpus
    source: GoldenSource
    refs: Mapping[str, GoldenRef]

    def ref(self, pr_id: str) -> GoldenRef:
        return self.refs[pr_id]


def _golden_source(raw: object, problems: list[str]) -> GoldenSource | None:
    if not isinstance(raw, Mapping):
        problems.append("golden_comments: the answer key's repository, commit and directory are required")
        return None
    repository = str(raw.get("repository") or "").strip()
    commit = str(raw.get("commit") or "").strip()
    directory = str(raw.get("directory") or "").strip()
    if not repository.startswith("https://"):
        problems.append("golden_comments.repository must be an https URL")
    if not _COMMIT.fullmatch(commit):
        problems.append("golden_comments.commit must be a full 40-character commit sha (the key is pinned)")
    if not directory or directory.startswith("/") or ".." in Path(directory).parts:
        problems.append("golden_comments.directory must be a relative path inside the key")
    if problems:
        return None
    return GoldenSource(repository, commit, directory, str(raw.get("licence") or ""))


def _golden_ref(raw: object, where: str, problems: list[str]) -> GoldenRef | None:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: golden is required (the key file and entry that list this pull request's issues)")
        return None
    file = str(raw.get("file") or "")
    index = raw.get("index")
    title = str(raw.get("title") or "").strip()
    language = str(raw.get("language") or "").strip()
    pull = raw.get("source_pull", 0)
    ok = True
    if not _FILE.fullmatch(file):
        problems.append(f"{where}: golden.file must be a bare .json file name")
        ok = False
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        problems.append(f"{where}: golden.index must be a 1-based entry number")
        ok = False
    if not title:
        problems.append(f"{where}: golden.title is required (it is checked against the key)")
        ok = False
    if not language:
        problems.append(f"{where}: golden.language is required")
        ok = False
    if not isinstance(pull, int) or isinstance(pull, bool) or pull < 0:
        problems.append(f"{where}: golden.source_pull must be a number")
        ok = False
    if not ok:
        return None
    assert isinstance(index, int) and isinstance(pull, int)
    return GoldenRef(file, index, title, language, pull)


def load_golden_corpus(path: Path, *, pr_ids: Sequence[str] = (), repository_root: Path | None = None) -> GoldenCorpus:
    """Load a golden-comment corpus: the real-PR rules apply, and every pull request must address an entry of the key."""

    corpus = load_real_pr_corpus(path, repository_root=repository_root, pr_ids=pr_ids)
    try:
        manifest = yaml.safe_load(path.resolve().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:  # load_real_pr_corpus read it already; this is a race, not a case
        raise CorpusError(f"{path}: {exc}") from exc
    problems: list[str] = []
    source = _golden_source(manifest.get("golden_comments"), problems)
    raw_by_id = {str(item.get("id")): item for item in manifest.get("prs") or () if isinstance(item, Mapping)}
    refs: dict[str, GoldenRef] = {}
    for pr in corpus.prs:
        ref = _golden_ref(raw_by_id.get(pr.id, {}).get("golden"), f"{pr.id}", problems)
        if ref is not None:
            refs[pr.id] = ref
    seen: dict[tuple[str, int], str] = {}
    for pr_id, ref in refs.items():
        other = seen.setdefault((ref.file, ref.index), pr_id)
        if other != pr_id:
            problems.append(f"{pr_id}: golden entry {ref.file}#{ref.index} is already used by {other}")
    if problems or source is None:
        raise CorpusError(f"golden-comment corpus {path} is invalid", problems)
    return GoldenCorpus(corpus, source, refs)


# --------------------------------------------------------------------------- #
# the answer key
# --------------------------------------------------------------------------- #


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=False, timeout=300
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GoldenError(f"git {' '.join(args[:2])} failed in {root}: {exc}") from exc
    if completed.returncode != 0:
        raise GoldenError(
            f"git {' '.join(args[:2])} failed in {root}: {(completed.stderr or completed.stdout).strip()}"
        )
    return completed.stdout.strip()


class GoldenKey:
    """The answer key's files at the pinned commit. Read-only: nothing here writes into the checkout."""

    def __init__(self, root: Path, source: GoldenSource) -> None:
        self.root = root
        self.source = source
        self._files: dict[str, list[Any]] = {}

    def _file(self, name: str) -> list[Any]:
        if name not in self._files:
            path = self.root / self.source.directory / name
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise GoldenError(f"cannot read the answer key file {path}: {exc}") from exc
            if not isinstance(data, list):
                raise GoldenError(f"{path} is not a list of pull request entries")
            self._files[name] = data
        return self._files[name]

    def comments(self, ref: GoldenRef) -> tuple[GoldenComment, ...]:
        """The issues listed for *ref*, ``G1``.. in the key's order. Refuses an entry whose title is not *ref*'s."""

        entries = self._file(ref.file)
        if ref.index > len(entries):
            raise GoldenError(f"{ref.file} has {len(entries)} entries; the corpus asks for entry {ref.index}")
        entry = entries[ref.index - 1]
        if not isinstance(entry, Mapping) or str(entry.get("pr_title", "")).strip() != ref.title:
            raise GoldenError(
                f"{ref.file} entry {ref.index} is titled {str(entry.get('pr_title', ''))[:80]!r}, not {ref.title!r}: "
                "the answer key is not the one this corpus was built against"
            )
        out: list[GoldenComment] = []
        for position, item in enumerate(entry.get("comments") or (), start=1):
            if not isinstance(item, Mapping):
                raise GoldenError(f"{ref.file} entry {ref.index} comment {position} is not an object")
            text = str(item.get("comment") or "").strip()
            severity = str(item.get("severity") or "").strip()
            if not text or severity not in GOLDEN_SEVERITIES:
                raise GoldenError(
                    f"{ref.file} entry {ref.index} comment {position}: needs text and a severity of {GOLDEN_SEVERITIES}"
                )
            out.append(GoldenComment(f"G{position}", text, severity))
        return tuple(out)


def open_golden_key(source: GoldenSource, directory: Path | None, *, cache_root: Path) -> GoldenKey:
    """The answer key checked out at the pinned commit.

    With *directory*, that checkout is used if it is at the pinned commit and its answer files are unmodified. Without
    one the key is cloned into *cache_root* (network) and checked out at the commit, once. Anything else is an error that
    says what to do; the benchmark never scores against a key it cannot pin.
    """

    if directory is None:
        directory = cache_root / f"golden-key-{source.commit[:12]}"
        if not (directory / ".git").exists():
            directory.parent.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run(
                    ["git", "clone", "-q", source.repository, str(directory)],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=600,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                detail = getattr(exc, "stderr", "") or str(exc)
                raise GoldenError(
                    f"cannot clone the answer key from {source.repository}: {str(detail).strip()}"
                ) from exc
            _git(directory, "checkout", "-q", "--detach", source.commit)
    if not (directory / ".git").exists():
        raise GoldenError(f"{directory} is not a git checkout of the answer key ({source.repository})")
    head = _git(directory, "rev-parse", "HEAD")
    if head != source.commit:
        raise GoldenError(
            f"the answer key at {directory} is at {head[:12]}, but the corpus pins {source.commit[:12]}; "
            f"run: git -C {directory} checkout --detach {source.commit}"
        )
    if _git(directory, "status", "--porcelain", "--", source.directory):
        raise GoldenError(f"the answer key at {directory} has local changes under {source.directory}/")
    return GoldenKey(directory, source)


__all__ = [
    "EXCLUDED_SEVERITIES",
    "GOLDEN_SEVERITIES",
    "GoldenComment",
    "GoldenCorpus",
    "GoldenError",
    "GoldenKey",
    "GoldenRef",
    "GoldenSource",
    "load_golden_corpus",
    "open_golden_key",
]
