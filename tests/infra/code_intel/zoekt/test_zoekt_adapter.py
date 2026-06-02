from __future__ import annotations

from pathlib import Path

from atelier.infra.code_intel.zoekt.adapter import ZoektBackendHealth, ZoektSupervisor
from atelier.infra.code_intel.zoekt.client import ZoektClientMatch, ZoektFileResult


class _FakeClient:
    def __init__(self, matches: list[ZoektFileResult]) -> None:
        self.matches = matches
        self.calls: list[tuple[int, str | None]] = []

    def search(self, _query: str, *, num_matches: int = 50, file_glob: str | None = None) -> list[ZoektFileResult]:
        self.calls.append((num_matches, file_glob))
        return self.matches


class _FakeSupervisor(ZoektSupervisor):
    def __init__(self, repo_root: Path, matches: list[ZoektFileResult]) -> None:
        super().__init__(repo_root)
        self.client = _FakeClient(matches)

    def ensure_started(self) -> _FakeClient:  # type: ignore[override]
        return self.client

    def health(self) -> ZoektBackendHealth:
        return ZoektBackendHealth(
            ok=True,
            backend="zoekt",
            binary_path="/fake/zoekt",
            index_age_seconds=1,
        )


def _file(path: str, *lines: str) -> ZoektFileResult:
    return ZoektFileResult(
        path=path,
        matches=[
            ZoektClientMatch(
                byte_start=index,
                byte_end=index + len(line),
                line_number=index + 1,
                line_text=line,
            )
            for index, line in enumerate(lines)
        ],
    )


def test_zoekt_search_compacts_reranks_and_filters_noise(tmp_path: Path) -> None:
    supervisor = _FakeSupervisor(
        tmp_path,
        [
            _file("benchmarks/fixtures/noisy.py", "def target(): pass"),
            _file("tests/test_noisy.py", "def target(): pass"),
            _file("src/atelier/best.py", "def target(): pass", "target = 1"),
            _file("src/other.py", "def target(): pass"),
        ],
    )

    result = supervisor.search(
        query="target",
        search_path=tmp_path,
        max_files=3,
        max_chars_per_file=80,
        include_outline=False,
    )

    assert [match.path for match in result.matches] == ["src/atelier/best.py", "src/other.py"]
    assert result.matches[0].snippets[0].text == "def target(): pass"
    assert len(result.matches[0].snippets) == 1
    assert result.total_tokens < result.total_tokens + result.tokens_saved_vs_naive


def test_zoekt_search_falls_back_when_all_hits_are_noise(tmp_path: Path) -> None:
    supervisor = _FakeSupervisor(
        tmp_path,
        [_file("tests/test_only.py", "def target(): pass")],
    )

    result = supervisor.search(
        query="target",
        search_path=tmp_path,
        max_files=3,
        max_chars_per_file=80,
        include_outline=False,
    )

    assert [match.path for match in result.matches] == ["tests/test_only.py"]
