from __future__ import annotations

from atelier.core.foundation.redaction import redact, redact_list


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
