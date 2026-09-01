from __future__ import annotations

import random
import string
import time

from lemoncrow.core.foundation.redaction import redact, redact_list


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


def test_closing_literal_guard_does_not_suppress_real_matches() -> None:
    # The guard must be a pure fast-path: once the closing literal exists, both
    # patterns still redact. Guards against a fix that trades correctness for
    # speed.
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
