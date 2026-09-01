"""Invariants of the settings registry itself.

The registry maps dotted CLI keys onto the env vars that actually drive runtime
behavior, so two specs sharing one env var means two unrelated knobs silently
drive the same code path. That is a correctness bug in general and a security
bug when one of the two is a boundary (issue #37: ``retrieval.additional_dirs``
and ``mcp.additional_edit_dirs`` both mapped to ``LEMONCROW_ADDITIONAL_DIRS``,
so setting the read-only retrieval knob widened the edit gate's WRITE scope).
"""

from __future__ import annotations

from collections import defaultdict

from lemoncrow.core.settings_registry import SETTINGS


def test_env_vars_are_unique_across_settings():
    by_env: defaultdict[str, list[str]] = defaultdict(list)
    for spec in SETTINGS:
        if spec.env_var:
            by_env[spec.env_var].append(spec.key)

    collisions = {env: keys for env, keys in by_env.items() if len(keys) > 1}

    assert not collisions, (
        "each setting must own its env var; sharing one makes unrelated keys "
        f"drive the same runtime behavior: {collisions}"
    )


def test_setting_keys_are_unique():
    keys = [spec.key for spec in SETTINGS]

    assert len(keys) == len(set(keys)), "duplicate setting keys make `lc settings set` ambiguous"


def test_retrieval_additional_dirs_does_not_drive_the_edit_gate():
    """A search/indexing knob must never grant write access (issue #37)."""
    by_key = {spec.key: spec for spec in SETTINGS}

    assert by_key["mcp.additional_edit_dirs"].env_var == "LEMONCROW_ADDITIONAL_DIRS"
    assert by_key["retrieval.additional_dirs"].env_var != "LEMONCROW_ADDITIONAL_DIRS"
