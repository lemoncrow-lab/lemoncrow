"""P6-1: ``lc review verify`` names the same outcomes the Verify dialog does, as exit codes.

0 valid, 1 not valid, 2 unsigned, 3 key not allowed, 4 code changed, 5 retracted (the signed retraction is given
with --retraction, or read from the server for a record link).
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.record_outcome import OUTCOME_EXIT_CODES
from lemoncrow.pro.capabilities.review.record_signing import key_set_document, public_key_entry, sign_review_record
from tests.gateway.test_review_record_golden import ISSUED_AT, KEY_ID, _context, _decisions, _signer


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "one")
    return root, _git(root, "rev-parse", "HEAD")


def _record(tmp_path: Path, *, head: str, dirty: bool = False, signed: bool = True) -> Path:
    context = _context(tmp_path)
    context = replace(context, revision=replace(context.revision, head_sha=head, dirty=dirty))
    record = sign_review_record(
        context, _decisions()["landing"], signer=_signer() if signed else None, issued_at=ISSUED_AT, persist=False
    )
    path = tmp_path / f"record-{head[:7]}-{dirty}-{signed}.json"
    path.write_text(json.dumps(dict(record.envelope)))
    return path


def _keys(tmp_path: Path) -> Path:
    public = _signer()._private_key.public_key()
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(key_set_document([public_key_entry(public, key_id=KEY_ID, issuer=KEY_ID)], issuer=KEY_ID))
    )
    return path


def _verify(tmp_path: Path, *args: str) -> Any:
    return CliRunner().invoke(
        cli, ["--root", str(tmp_path / "root"), "review", "verify", *args], catch_exceptions=False
    )


def test_the_exit_codes_are_the_ones_the_dialog_outcomes_map_to() -> None:
    assert OUTCOME_EXIT_CODES == {
        "valid": 0,
        "not_valid": 1,
        "unsigned": 2,
        "key_not_allowed": 3,
        "code_changed": 4,
        "retracted": 5,
    }


def test_a_record_over_this_checkout_is_valid(tmp_path: Path, repo: tuple[Path, str]) -> None:
    root, head = repo
    result = _verify(
        tmp_path, str(_record(tmp_path, head=head)), "--keys", str(_keys(tmp_path)), "--repo-root", str(root)
    )
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == f"Valid · signed by key {KEY_ID} at 09:00 UTC · matches {head[:7]}"


def test_a_checkout_that_moved_on_is_code_changed(tmp_path: Path, repo: tuple[Path, str]) -> None:
    root, head = repo
    record = _record(tmp_path, head=head)
    (root / "a.txt").write_text("two\n")
    _git(root, "commit", "-q", "-am", "two")
    now = _git(root, "rev-parse", "HEAD")
    result = _verify(tmp_path, str(record), "--keys", str(_keys(tmp_path)), "--repo-root", str(root))
    assert result.exit_code == 4, result.output
    assert result.output.splitlines()[0] == f"Not valid · the code changed after signing (now {now[:7]})"


def test_a_record_of_a_working_tree_says_the_code_was_not_checked(tmp_path: Path, repo: tuple[Path, str]) -> None:
    root, head = repo
    record = _record(tmp_path, head=head, dirty=True)
    (root / "a.txt").write_text("two\n")
    _git(root, "commit", "-q", "-am", "two")
    result = _verify(tmp_path, str(record), "--keys", str(_keys(tmp_path)), "--repo-root", str(root))
    assert result.exit_code == 0 and result.output.splitlines()[0].endswith(" · code not checked")


def test_a_checkout_of_a_different_repository_is_not_compared(tmp_path: Path, repo: tuple[Path, str]) -> None:
    root, _head = repo
    result = _verify(
        tmp_path, str(_record(tmp_path, head="c" * 40)), "--keys", str(_keys(tmp_path)), "--repo-root", str(root)
    )
    assert result.exit_code == 0 and result.output.splitlines()[0].endswith(" · code not checked")


def test_a_key_that_is_not_trusted_is_exit_3_and_says_which_key(tmp_path: Path, repo: tuple[Path, str]) -> None:
    root, head = repo
    empty = tmp_path / "empty-keys.json"
    empty.write_text(json.dumps(key_set_document([], issuer="nobody")))
    result = _verify(tmp_path, str(_record(tmp_path, head=head)), "--keys", str(empty), "--repo-root", str(root))
    assert result.exit_code == 3
    assert result.output.splitlines()[0] == f"Can't verify · key {KEY_ID} is not in your trusted signing keys"


def test_an_unsigned_record_is_exit_2_and_an_altered_one_exit_1(tmp_path: Path, repo: tuple[Path, str]) -> None:
    root, head = repo
    keys = str(_keys(tmp_path))
    unsigned = _verify(
        tmp_path, str(_record(tmp_path, head=head, signed=False)), "--keys", keys, "--repo-root", str(root)
    )
    assert unsigned.exit_code == 2 and unsigned.output.splitlines()[0] == "Can't verify · this record is not signed"
    altered = json.loads(_record(tmp_path, head=head).read_text())
    statement = json.loads(base64.b64decode(altered["payload"]))
    statement["predicate"]["decision"]["outcome"] = "break_glass"
    altered["payload"] = base64.b64encode(
        json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    path = tmp_path / "altered.json"
    path.write_text(json.dumps(altered))
    result = _verify(tmp_path, str(path), "--keys", keys, "--repo-root", str(root))
    assert result.exit_code == 1 and result.output.splitlines()[0].startswith("Not valid")


def test_json_output_carries_the_outcome_and_the_signature_verdict(tmp_path: Path, repo: tuple[Path, str]) -> None:
    root, head = repo
    result = _verify(
        tmp_path, str(_record(tmp_path, head=head)), "--keys", str(_keys(tmp_path)), "--repo-root", str(root), "--json"
    )
    body = json.loads(result.output)
    assert (body["outcome"], body["exit_code"], body["code"]) == ("valid", 0, "matches")
    assert body["verification"]["status"] == "verified"


def test_a_record_link_is_read_through_the_review_session(
    tmp_path: Path, repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, head = repo
    document = json.loads(_record(tmp_path, head=head).read_text())
    asked: list[tuple[str, str]] = []

    def fake_session_request(_config: Any) -> Any:
        def request(method: str, path: str, _body: Any) -> Any:
            from lemoncrow_client.errors import ClientError, ErrorCode

            asked.append((method, path))
            if path.endswith("/retraction"):
                raise ClientError(ErrorCode.UNKNOWN, "this record has not been retracted")
            return document

        return request

    monkeypatch.setattr("lemoncrow_client.review_terminal.session_request", fake_session_request)
    monkeypatch.setattr("lemoncrow_client.config.load_config", lambda **_kwargs: object())
    link = "http://127.0.0.1:48080/api/reviews/0abc/records/rec_123"
    result = _verify(tmp_path, link, "--keys", str(_keys(tmp_path)), "--repo-root", str(root))
    assert result.exit_code == 0, result.output
    assert asked == [
        ("GET", "/api/reviews/0abc/records/rec_123"),
        ("GET", "/api/reviews/0abc/records/rec_123/retraction"),
    ]


def test_a_record_link_checks_the_retraction_the_server_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record_file, retraction_file = _retracted_files(tmp_path)
    documents = {"": json.loads(record_file.read_text()), "/retraction": json.loads(retraction_file.read_text())}

    def fake_session_request(_config: Any) -> Any:
        return lambda _method, path, _body: documents["/retraction" if path.endswith("/retraction") else ""]

    monkeypatch.setattr("lemoncrow_client.review_terminal.session_request", fake_session_request)
    monkeypatch.setattr("lemoncrow_client.config.load_config", lambda **_kwargs: object())
    link = "http://127.0.0.1:48080/api/reviews/0abc/records/rec_123"
    result = _verify(tmp_path, link, "--keys", str(_keys(tmp_path)))
    assert result.exit_code == 5, result.output
    assert result.output.splitlines()[0] == "Retracted \u00b7 by @srivera at 10:00 UTC"


def test_the_client_verb_runs_the_full_package_command(tmp_path: Path, repo: tuple[Path, str]) -> None:
    from lemoncrow_client import review_verbs

    root, head = repo
    sink = io.StringIO()
    code = review_verbs.verbs_main(
        "verify",
        [str(_record(tmp_path, head=head)), "--keys", str(_keys(tmp_path)), "--repo-root", str(root)],
        sink=sink,
    )
    assert code == 0 and sink.getvalue().startswith("Valid · signed by")
    missing = io.StringIO()
    assert review_verbs.verbs_main("verify", [], sink=missing) == 12 and "Usage: lc review verify" in missing.getvalue()


def test_the_client_alone_says_so_and_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client import review_verbs

    monkeypatch.setitem(sys.modules, "lemoncrow.gateway.cli.commands.review_record_cli", None)
    sink = io.StringIO()
    assert review_verbs.verbs_main("verify", ["record.json"], sink=sink) == 10
    assert sink.getvalue() == (
        "lc review verify needs the full LemonCrow package, which checks the signature. "
        "Install it, or run `lc review verify` from it. Nothing was changed.\n"
    )


def test_input_and_usage_errors_never_use_an_outcome_code(tmp_path: Path, repo: tuple[Path, str]) -> None:
    root, _head = repo
    missing = _verify(tmp_path, str(tmp_path / "no-such-record.json"), "--repo-root", str(root))
    assert missing.exit_code == 11, missing.output
    bad_keys = _verify(
        tmp_path,
        str(_record(tmp_path, head="c" * 40)),
        "--keys",
        str(tmp_path / "no-keys.json"),
        "--repo-root",
        str(root),
    )
    assert bad_keys.exit_code == 11
    no_target = CliRunner().invoke(cli, ["--root", str(tmp_path / "root"), "review", "verify"], catch_exceptions=False)
    assert no_target.exit_code == 12
    # The client verb keeps the three apart from each other and from the six outcomes.
    from lemoncrow_client import review_verbs

    sink = io.StringIO()
    assert review_verbs.verbs_main("verify", [str(tmp_path / "no-such-record.json")], sink=sink) == 11


def test_only_the_viewers_own_key_reads_you_in_the_cli(
    tmp_path: Path, repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, head = repo
    record = _record(tmp_path, head=head)
    # --keys is somebody else's published set: never "you".
    named = _verify(tmp_path, str(record), "--keys", str(_keys(tmp_path)), "--repo-root", str(root))
    assert f"signed by key {KEY_ID}" in named.output.splitlines()[0]


# --------------------------------------------------------------------------- #
# P6-2: a retraction given with --retraction
# --------------------------------------------------------------------------- #


def _retracted_files(tmp_path: Path, *, hours: float = 1, key: str = "same") -> tuple[Path, Path]:
    from datetime import timedelta

    from lemoncrow.pro.capabilities.review.record_retraction import sign_retraction
    from tests.gateway.test_review_record_retraction import _at, _other_signer, _record

    record = _record(tmp_path)
    retraction = sign_retraction(
        record,
        _signer() if key == "same" else _other_signer(),
        retracted_by="srivera",
        retracted_by_type="human",
        retracted_at=_at(timedelta(hours=hours)),
    )
    record_file, retraction_file = tmp_path / "verdict.json", tmp_path / "verdict.retraction.json"
    record_file.write_text(json.dumps(dict(record.envelope)))
    retraction_file.write_text(json.dumps(dict(retraction.envelope)))
    return record_file, retraction_file


def test_a_record_with_its_signed_retraction_is_retracted_exit_5(tmp_path: Path) -> None:
    record_file, retraction_file = _retracted_files(tmp_path)
    result = _verify(
        tmp_path, str(record_file), "--retraction", str(retraction_file), "--keys", str(_keys(tmp_path)), "--json"
    )
    assert result.exit_code == 5, result.output
    document = json.loads(result.output)
    assert (document["outcome"], document["retraction"]["by"]) == ("retracted", "srivera")
    assert document["headline"] == "Retracted \u00b7 by @srivera at 10:00 UTC"


def test_the_same_record_without_its_retraction_is_valid(tmp_path: Path) -> None:
    record_file, _retraction = _retracted_files(tmp_path)
    result = _verify(tmp_path, str(record_file), "--keys", str(_keys(tmp_path)))
    assert result.exit_code == 0, result.output


def test_a_retraction_signed_by_another_key_is_not_valid_exit_1(tmp_path: Path) -> None:
    from tests.gateway.test_review_record_retraction import _keys as both_keys
    from tests.gateway.test_review_record_retraction import _other_signer

    record_file, retraction_file = _retracted_files(tmp_path)
    forged = tmp_path / "forged.json"
    forged.write_text(retraction_file.read_text())
    keys = tmp_path / "both.json"
    other = _other_signer()
    keys.write_text(
        json.dumps(
            key_set_document(
                [
                    public_key_entry(_signer()._private_key.public_key(), key_id=KEY_ID, issuer=KEY_ID),
                    public_key_entry(
                        other._private_key.public_key(), key_id=other.identity.key_id, issuer=other.identity.key_id
                    ),
                ],
                issuer=KEY_ID,
            )
        )
    )
    del both_keys
    # Tampered signature bytes: the retraction no longer checks.
    envelope = json.loads(forged.read_text())
    envelope["signatures"][0]["sig"] = base64.b64encode(b"x" * 64).decode("ascii")
    forged.write_text(json.dumps(envelope))
    result = _verify(tmp_path, str(record_file), "--retraction", str(forged), "--keys", str(keys))
    assert result.exit_code == 1, result.output
    assert result.output.splitlines()[0] == "Not valid \u00b7 the retraction has a signature that is not valid"


def test_an_unreadable_retraction_file_is_an_input_error(tmp_path: Path) -> None:
    record_file, _retraction = _retracted_files(tmp_path)
    missing = _verify(
        tmp_path, str(record_file), "--retraction", str(tmp_path / "nope.json"), "--keys", str(_keys(tmp_path))
    )
    assert missing.exit_code == 11
