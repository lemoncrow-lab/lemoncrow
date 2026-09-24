"""The test-contract guard shared by every edit surface."""

from __future__ import annotations

from pathlib import Path

from lemoncrow_client.kit.contract_guard import (
    classify_weakening,
    detect_weakening,
    guard_enabled,
    looks_like_test_path,
    weakening_message,
)
from lemoncrow_client.kit.fsio import FileSnapshot


def test_net_assertion_removal_and_new_skips_are_weakening() -> None:
    assert classify_weakening("assert a\nassert b\n", "assert b\n") == "net removal of 1 assertion(s)"
    assert classify_weakening("def t():\n", "@pytest.mark.skip\ndef t():\n") == "added 1 skip/xfail marker(s)"


def test_changing_or_adding_assertions_is_not_weakening() -> None:
    assert classify_weakening("assert a == 1\n", "assert a == 2\n") is None
    assert classify_weakening("assert a\n", "assert a\nassert b\n") is None


def test_test_paths_are_recognized() -> None:
    assert looks_like_test_path("tests/test_x.py")
    assert looks_like_test_path("src/a.spec.ts")
    assert looks_like_test_path("pkg/__tests__/b.js")
    assert not looks_like_test_path("src/app.py")


def _test_file(root: Path, now: str) -> Path:
    path = root / "tests" / "test_a.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(now, encoding="utf-8")
    return path


def test_a_test_only_weakening_is_reported(tmp_path: Path) -> None:
    test_file = _test_file(tmp_path, "assert b\n")
    snapshots = {"tests/test_a.py": FileSnapshot(test_file, True, "assert a\nassert b\n")}

    assert detect_weakening(snapshots, session_created=set()) == [
        {"path": "tests/test_a.py", "reason": "net removal of 1 assertion(s)"}
    ]


def test_a_weakening_next_to_a_production_change_is_allowed(tmp_path: Path) -> None:
    test_file = _test_file(tmp_path, "assert b\n")
    production = tmp_path / "app.py"
    production.write_text("new\n", encoding="utf-8")
    snapshots = {
        "tests/test_a.py": FileSnapshot(test_file, True, "assert a\nassert b\n"),
        "app.py": FileSnapshot(production, True, "old\n"),
    }

    assert detect_weakening(snapshots, session_created=set()) == []


def test_a_test_written_this_session_is_not_a_contract(tmp_path: Path) -> None:
    test_file = _test_file(tmp_path, "assert b\n")
    snapshots = {"tests/test_a.py": FileSnapshot(test_file, True, "assert a\nassert b\n")}

    assert detect_weakening(snapshots, session_created={str(test_file.resolve())}) == []


def test_the_guard_has_an_operator_switch() -> None:
    assert guard_enabled({})
    assert not guard_enabled({"LEMONCROW_TEST_CONTRACT_GUARD": "0"})
    assert not guard_enabled({"LEMONCROW_TEST_CONTRACT_GUARD": "off"})


def test_the_message_names_each_file_and_reason() -> None:
    message = weakening_message([{"path": "tests/test_a.py", "reason": "net removal of 1 assertion(s)"}])
    assert message.startswith("Edit rolled back: it weakened an existing test contract (")
    assert "tests/test_a.py: net removal of 1 assertion(s)" in message
