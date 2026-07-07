"""G8 — live tool-output credential redaction wired into the output path.

These tests prove that secrets present in source/command output are masked
*before* the result text reaches the model, on the read/grep render path
(``native_search``) and the bash stdout/stderr path (``bash_exec``), and that
the ``ATELIER_OUTPUT_REDACTION`` kill-switch restores raw output. They also
guard against over-broad masking (legitimate content must survive).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities.tool_supervision.bash_exec import _compact_result
from atelier.core.capabilities.tool_supervision.native_search import search_workspace
from atelier.core.foundation.redaction import output_redaction_enabled, redact_tool_output

_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_OPENAI_KEY = "sk-" + "A" * 40
_JWT = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20


def _texts(result: dict[str, Any]) -> list[str]:
    return [
        str(item.get("text", "")) for item in result["content"] if isinstance(item, dict) and item.get("type") == "text"
    ]


def test_redact_tool_output_masks_known_secrets() -> None:
    raw = f"aws={_AWS_KEY} openai={_OPENAI_KEY} jwt={_JWT}"
    out = redact_tool_output(raw)
    assert _AWS_KEY not in out
    assert _OPENAI_KEY not in out
    assert _JWT not in out
    assert "<redacted-aws-key>" in out
    assert "<redacted-openai-key>" in out
    assert "<redacted-jwt>" in out


def test_redact_tool_output_preserves_ordinary_text() -> None:
    raw = "def handler():\n    return compute_total(items)  # no secrets here\n"
    assert redact_tool_output(raw) == raw


def test_kill_switch_disables_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_OUTPUT_REDACTION", "0")
    assert output_redaction_enabled() is False
    assert redact_tool_output(f"key={_AWS_KEY}") == f"key={_AWS_KEY}"


def test_default_on_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_OUTPUT_REDACTION", raising=False)
    assert output_redaction_enabled() is True


def test_native_search_grep_render_is_redacted(tmp_path: Path) -> None:
    (tmp_path / "conf.py").write_text(
        f"DB = 'ok'\nAWS_SECRET = '{_AWS_KEY}'\n",
        encoding="utf-8",
    )
    result = search_workspace(
        path=".",
        content_regex="AWS_SECRET",
        file_glob_patterns=["**/*.py"],
        repo_root=tmp_path,
    )
    text = "\n".join(_texts(result))
    assert "AWS_SECRET" in text  # the matched identifier is preserved
    assert _AWS_KEY not in text  # the secret value is masked
    assert "<redacted-aws-key>" in text


def test_native_search_range_read_is_redacted(tmp_path: Path) -> None:
    secret_file = tmp_path / "creds.py"
    # Bare value (no key=val prefix) so the dedicated openai-key marker fires.
    secret_file.write_text(f"line1 = 1\nkey = '{_OPENAI_KEY}'\nline3 = 3\n", encoding="utf-8")
    result = search_workspace(path=".", file_glob_patterns=["creds.py:L1-L3"], repo_root=tmp_path)
    text = "\n".join(_texts(result))
    assert _OPENAI_KEY not in text
    assert "<redacted-openai-key>" in text
    assert "line1" in text and "line3" in text


def test_native_search_respects_kill_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_OUTPUT_REDACTION", "off")
    (tmp_path / "conf.py").write_text(f"AWS_SECRET = '{_AWS_KEY}'\n", encoding="utf-8")
    result = search_workspace(
        path=".",
        content_regex="AWS_SECRET",
        file_glob_patterns=["**/*.py"],
        repo_root=tmp_path,
    )
    assert _AWS_KEY in "\n".join(_texts(result))


def test_bash_compact_result_redacts_stdout_and_stderr() -> None:
    result = _compact_result(
        command="printenv",
        raw_stdout=f"AWS_ACCESS_KEY_ID={_AWS_KEY}\n",
        raw_stderr=f"warning token={_JWT}\n",
        exit_code=0,
        duration_ms=1,
        max_lines=200,
    )
    assert _AWS_KEY not in result.stdout
    assert "<redacted-aws-key>" in result.stdout
    # The credential-pair pattern (token=...) masks the whole pair first; what
    # matters is the secret value is gone from stderr before reaching the model.
    assert _JWT not in result.stderr
    assert "<redacted" in result.stderr


def test_bash_compact_result_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_OUTPUT_REDACTION", "no")
    result = _compact_result(
        command="printenv",
        raw_stdout=f"AWS_ACCESS_KEY_ID={_AWS_KEY}\n",
        raw_stderr="",
        exit_code=0,
        duration_ms=1,
        max_lines=200,
    )
    assert _AWS_KEY in result.stdout
