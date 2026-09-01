from __future__ import annotations

import json
import random
import string
import time

import pytest

from lemoncrow.core.foundation.redaction import (
    _redact_json_values,
    escape_jsonl_line_breaks,
    redact,
    redact_jsonl,
    redact_list,
)


def _parses(line: str) -> bool:
    try:
        json.loads(line)
    except ValueError:
        return False
    return True


def test_redacts_openai_key() -> None:
    assert "sk-" not in redact("token sk-ABCDEFGHIJKLMNOPQRSTUV1234567890")


def test_redacts_credential_pair() -> None:
    assert "<redacted-credential>" in redact("api_key=supersecretthing123")


def test_redacts_chain_of_thought_marker() -> None:
    out = redact("step 1 fine\nchain of thought: secret reasoning here")
    # Single, clean marker -- the previous ``<redacted-marker>`` double-marker
    # was a cosmetic duplication (M4) and must not reappear.
    assert "<redacted-hidden-reasoning>" in out
    assert "<redacted-marker>" not in out
    assert "chain of thought" not in out
    assert "secret reasoning here" not in out


def test_multiword_credential_value_is_fully_redacted() -> None:
    # A bare ``\\S+`` value stops at the first space and leaks the actual
    # secret in ``token: Bearer <secret>`` form (M4). The value is now masked
    # to the end of the line, so the embedded secret cannot leak past the edge.
    out = redact("authorization token: Bearer abc123SECRETvalue")
    assert "abc123SECRETvalue" not in out
    assert "Bearer" not in out
    assert "<redacted-credential>" in out


def test_repeated_secret_is_redacted_globally() -> None:
    # re.sub with no count replaces every occurrence, not just the first.
    secret = "token=s3cr3tVALUE"
    out = redact(f"see {secret}\nand again {secret}\nend")
    assert "s3cr3tVALUE" not in out
    assert out.count("<redacted-credential>") == 2


def test_credential_redaction_stays_on_its_own_line() -> None:
    # End-of-line masking must not bleed across newlines into the next line.
    out = redact("password: hunter2supersecret\nkeep_this_line")
    assert "hunter2supersecret" not in out
    assert "<redacted-credential>" in out
    assert "keep_this_line" in out


def test_ordinary_identifier_is_not_over_redacted() -> None:
    # ``AWS_SECRET`` is a variable name (no word boundary before SECRET); the
    # identifier must survive while its high-entropy value is masked by the
    # dedicated AWS-key pattern. Guards against over-redaction.
    out = redact("AWS_SECRET = 'AKIAIOSFODNN7EXAMPLE'")
    assert "AWS_SECRET" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "<redacted-aws-key>" in out


def test_ordinary_text_with_credential_keyword_is_not_redacted() -> None:
    # No ``[:=]`` delimiter immediately after the keyword -> not a credential.
    prose = "the token economics of LLMs and the tokenizer: BPE design"
    assert redact(prose) == prose


def test_redacts_jwt() -> None:
    jwt = "eyJABCDEFGHIJ.eyJABCDEFGHIJ.signaturepartXYZ"
    assert "<redacted-jwt>" in redact(f"Bearer {jwt}")


def test_redacts_email() -> None:
    out = redact("ping me at pankaj4u4m@gmail.com please")
    assert "pankaj4u4m@gmail.com" not in out
    assert "<redacted-email>" in out


def test_redact_list_applies_per_item() -> None:
    out = redact_list(["clean", "password=hunter2"])
    assert out[0] == "clean"
    assert "<redacted-credential>" in out[1]


def test_redact_does_not_hang_on_large_binary_blob() -> None:
    # Regression for #38: `lc import` hung indefinitely (9-12+ min, CPU
    # pegged) on opencode sessions containing a large tool-output blob (raw
    # PDF text / inline ``data:image/png;base64,...``). Root cause: several
    # patterns pair an unbounded greedy character class with a required
    # literal that never appears in such blobs (e.g. the email pattern's
    # ``[A-Za-z0-9._%+-]+@``). With no ``@`` anywhere, Python's backtracking
    # engine retries the full greedy scan independently at every
    # word-boundary position, which is O(n^2) on a blob this size --
    # several seconds here, tens of minutes at real ~20MB report scale.
    # A correct fix bounds those quantifiers so this stays ~linear.
    random.seed(3)
    alphabet = string.ascii_letters + string.digits + "_-"
    blob = "data:image/png;base64," + "".join(random.choice(alphabet) for _ in range(1_000_000))

    start = time.monotonic()
    redact(blob)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"redact() took {elapsed:.2f}s on a 1M-char blob -- catastrophic backtracking regression"


def test_redact_is_fast_on_densely_repeated_unclosed_think_tags() -> None:
    # #38 follow-up. Bounding ``<think>...</think>`` to a 64KB window made it
    # linear but left a brutal constant: with one unmatched "<think>" every 32
    # bytes, every opener still pays a full 64KB window scan (~11s/MB,
    # extrapolating to ~220s on the 20MB session from the issue). The closing
    # literal is a necessary condition for a match, so a single cheap scan for
    # "</think" lets the whole pattern be skipped.
    blob = ("<think>" + "x" * 25) * 62_500  # ~2MB, zero closing tags

    start = time.monotonic()
    out = redact(blob)
    elapsed = time.monotonic() - start

    assert out == blob, "nothing to redact -- no closing </think> anywhere"
    assert elapsed < 2.0, f"redact() took {elapsed:.2f}s on 2MB of unclosed <think> openers"


def test_redact_is_fast_on_densely_repeated_unclosed_private_key_headers() -> None:
    # Same shape as above for the PEM pattern: "-----BEGIN ... PRIVATE KEY-----"
    # with no END anywhere pays a 16KB window scan per opener without the
    # closing-literal guard.
    blob = ("-----BEGIN A PRIVATE KEY-----" + "xxx") * 62_500  # ~2MB, zero END markers

    start = time.monotonic()
    out = redact(blob)
    elapsed = time.monotonic() - start

    assert out == blob, "nothing to redact -- no -----END ... PRIVATE KEY----- anywhere"
    assert elapsed < 2.0, f"redact() took {elapsed:.2f}s on 2MB of unclosed PEM headers"


def test_redact_jsonl_keeps_every_record_parseable() -> None:
    # Real-store regression: 1.35% of stored artifact lines were unparseable.
    # Two independent ways plain redact() corrupts serialized JSON:
    #   * a match straddling an escape -- "\n@pytest.fixture" hits the email
    #     rule and leaves a bare backslash before the replacement;
    #   * the credential rule masks to end-of-*line*, which in JSONL is the
    #     rest of the record, closing braces included.
    record = {"type": "message", "text": "from x import y\n\n@pytest.fixture\ntoken: hunter2", "id": "m1"}
    line = json.dumps(record)

    assert _parses(redact(line)) is False, "precondition: plain redact() corrupts this record"

    out = redact_jsonl(line)
    decoded = json.loads(out)

    assert decoded["type"] == "message" and decoded["id"] == "m1"
    assert "hunter2" not in out
    assert "<redacted-credential>" in out
    # ...and the structure around the redaction survives, unlike end-of-line masking.
    assert "from x import y" in decoded["text"]


def _reference_redact_jsonl(text: str) -> str:
    """redact_jsonl without its candidate-scan fast path -- the slow but
    obviously-correct version the optimized one must agree with."""
    out = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("{", "[")):
            try:
                decoded = json.loads(stripped)
            except ValueError:
                out.append(escape_jsonl_line_breaks(redact(line)))
                continue
            out.append(escape_jsonl_line_breaks(json.dumps(_redact_json_values(decoded), ensure_ascii=False)))
        else:
            out.append(escape_jsonl_line_breaks(redact(line)))
    return "\n".join(out)


@pytest.mark.parametrize(
    "payload",
    [
        "nothing secret here at all",
        "sk-ABCDEFGHIJKLMNOPQRSTUV12345678",
        "ghp_ABCDEFGHIJKLMNOPQRSTUV12345678",
        "shppa_ABCDEFGHIJKLMNOPQRSTUV12345678",
        "user@example.com",
        "token: Bearer hunter2",
        "AKIA1234567890ABCDEF",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "-----BEGIN RSA PRIVATE KEY-----\\nMIIBOgIBAAJBAK\\n-----END RSA PRIVATE KEY-----",
        "<think>hidden</think>",
        "chain of thought: hidden",
        "internal reasoning: hidden",
    ],
)
def test_redact_jsonl_candidate_scan_never_skips_a_real_match(payload: str) -> None:
    # The fast path must be a pure optimization: identical output to the
    # always-parse reference, for every pattern the module knows about.
    text = "\n".join(
        [
            json.dumps({"type": "message", "text": payload}),
            json.dumps({"type": "noise", "text": "ordinary line"}),
            f"plain text {payload}",
        ]
    )

    assert redact_jsonl(text) == _reference_redact_jsonl(text)


def test_redact_jsonl_escapes_unicode_line_separators() -> None:
    # U+2028 is legal inside a JSON string and json.dumps(ensure_ascii=False)
    # emits it raw -- but str.splitlines() treats it as a line break, so one
    # record silently becomes two unparseable halves for every reader. Seen on
    # real sessions quoting scraped web copy.
    line = json.dumps({"text": "before after"}, ensure_ascii=False)
    assert len(line.splitlines()) == 2, "precondition: raw U+2028 splits the record"

    out = redact_jsonl(line)

    assert len(out.splitlines()) == 1
    assert json.loads(out)["text"] == "before after"


def test_redact_jsonl_masks_secrets_inside_nested_values() -> None:
    line = json.dumps({"parts": [{"text": "key sk-ABCDEFGHIJKLMNOPQRSTUV12345678"}], "meta": {"user": "a@b.com"}})

    decoded = json.loads(redact_jsonl(line))

    assert decoded["parts"][0]["text"] == "key <redacted-openai-key>"
    assert decoded["meta"]["user"] == "<redacted-email>"


def test_redact_jsonl_falls_back_to_text_redaction_off_json() -> None:
    # Mixed content must still be scrubbed: a plain-text line, and a line that
    # only looks like JSON.
    text = "plain token: hunter2\n{not really json, token: hunter3"

    out = redact_jsonl(text)

    assert "hunter2" not in out and "hunter3" not in out
    assert out.count("<redacted-credential>") == 2


def test_redacts_private_key_body_of_any_length() -> None:
    # #38 regression. The first fix bounded the PEM body to ``.{0,16384}?``,
    # which is linear but stops matching past the window -- i.e. a >16KB key
    # body was written to the store verbatim. Redaction must have no size
    # ceiling: a bounded window here is a leak, not a trade-off.
    body = "A" * 200_000
    out = redact(f"head -----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY----- tail")

    assert "AAAA" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "<redacted-private-key>" in out
    assert out.startswith("head ") and out.endswith(" tail")


def test_redacts_reasoning_block_of_any_length() -> None:
    # Same regression for chain-of-thought: ``.{0,65536}?`` leaked every
    # reasoning block longer than 64KB.
    out = redact("pre <think>" + "secret-cot " * 20_000 + "</think> post")

    assert "secret-cot" not in out
    assert "<redacted-hidden-reasoning>" in out
    assert out == "pre <redacted-hidden-reasoning> post"


def test_unclosed_opener_does_not_hide_a_later_closed_block() -> None:
    # Pairing openers to closers must not stop at the first opener that has no
    # closer: ``<think>`` is not closed by ``</thinking>``, so the orphan stays
    # literal while the real block is still redacted.
    out = redact("<think>orphan <thinking>REAL</thinking>")

    assert "REAL" not in out
    assert out == "<think>orphan <redacted-hidden-reasoning>"


def test_missing_closing_literal_does_not_suppress_real_matches() -> None:
    # The no-closer fast path must be exact: once a closing literal exists,
    # both spans still redact. Guards against a fix that trades correctness
    # for speed.
    noise = ("<think>" + "x" * 25) * 20_000  # ~640KB of unmatched openers
    text = f"{noise}<think>the hidden part</think>tail"
    out = redact(text)
    assert "the hidden part" not in out
    assert "<redacted-hidden-reasoning>" in out

    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----"
    key_out = redact(f"prefix\n{key}\nsuffix")
    assert "MIIBOgIBAAJBAK" not in key_out
    assert "<redacted-private-key>" in key_out
    assert "suffix" in key_out
