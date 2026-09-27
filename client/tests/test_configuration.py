"""Configuration: one endpoint, one credential, one writable directory.

These are the settings an IT team provisions and a security review reads back,
so each one is asserted for what it accepts *and* for what it refuses. A
configuration layer that silently accepts a credential in a URL, or a settings
file that can set ``PATH``, is a finding regardless of what the rest of the
package does.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from lemoncrow_client.config import (
    DEFAULT_URL,
    load_config,
    read_settings_file,
    repo_identity,
)
from lemoncrow_client.errors import ClientError, ErrorCode


def _env(state_dir: Path, **extra: str) -> dict[str, str]:
    return {"LEMONCROW_HOME": str(state_dir), "HOME": str(state_dir.parent), **extra}


def test_the_default_endpoint_is_loopback(worktree: Path, state_dir: Path) -> None:
    """ "Local" is a server bound to loopback, not a different code path."""
    config = load_config(_env(state_dir), cwd=worktree)
    assert config.url == DEFAULT_URL
    assert config.install_mode == "local"
    assert config.hosted is False


def test_a_remote_endpoint_defaults_to_hosted_for_pre_mode_installations(worktree: Path, state_dir: Path) -> None:
    config = load_config(_env(state_dir, LEMONCROW_URL="https://lemoncrow.example"), cwd=worktree)
    assert config.install_mode == "hosted"
    assert config.hosted is True


def test_explicit_hosted_mode_is_a_security_boundary_even_on_loopback(worktree: Path, state_dir: Path) -> None:
    config = load_config(
        _env(
            state_dir,
            LEMONCROW_URL="http://127.0.0.1:7420",
            LEMONCROW_INSTALL_MODE="hosted",
        ),
        cwd=worktree,
    )
    assert config.hosted is True


def test_invalid_install_mode_is_refused(worktree: Path, state_dir: Path) -> None:
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_INSTALL_MODE="hybrid"), cwd=worktree)
    assert caught.value.code is ErrorCode.NOT_CONFIGURED


@pytest.mark.parametrize(
    "url",
    ["ftp://host/x", "file:///etc/passwd", "not-a-url", "https://", "gopher://host"],
)
def test_a_non_http_endpoint_is_refused(worktree: Path, state_dir: Path, url: str) -> None:
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_URL=url), cwd=worktree)
    assert caught.value.code is ErrorCode.NOT_CONFIGURED


def test_an_endpoint_with_embedded_credentials_is_refused(worktree: Path, state_dir: Path) -> None:
    """A credential in the endpoint ends up in every log line that names it."""
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_URL="https://user:secret@host/"), cwd=worktree)
    assert "credentials" in caught.value.message


def test_the_origin_is_derived_once_and_defaults_the_port_by_scheme(worktree: Path, state_dir: Path) -> None:
    https = load_config(_env(state_dir, LEMONCROW_URL="https://lemoncrow.corp"), cwd=worktree)
    assert https.endpoint_origin == ("https", "lemoncrow.corp", 443)
    http = load_config(_env(state_dir, LEMONCROW_URL="http://127.0.0.1"), cwd=worktree)
    assert http.endpoint_origin == ("http", "127.0.0.1", 80)


@pytest.mark.parametrize("url", ["http://lemoncrow.corp", "http://10.0.0.8:7420", "http://127.0.0.2:7420"])
def test_plaintext_http_is_refused_outside_literal_loopback(worktree: Path, state_dir: Path, url: str) -> None:
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_URL=url), cwd=worktree)
    assert caught.value.code is ErrorCode.NOT_CONFIGURED
    assert "HTTPS" in caught.value.message


@pytest.mark.parametrize(
    "url",
    ["http://localhost:7420", "http://127.0.0.1:7420", "http://[::1]:7420"],
)
def test_plaintext_http_is_allowed_only_for_literal_loopback(worktree: Path, state_dir: Path, url: str) -> None:
    assert load_config(_env(state_dir, LEMONCROW_URL=url), cwd=worktree).url == url


def test_a_short_token_is_refused_rather_than_sent(worktree: Path, state_dir: Path) -> None:
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_INSTALL_MODE="hosted", LEMONCROW_TOKEN="short"), cwd=worktree)
    assert caught.value.action.value == "reauthenticate"


def test_local_mode_accepts_but_does_not_require_legacy_credentials(worktree: Path, state_dir: Path) -> None:
    (state_dir / "token").write_text("legacy-local-token-0123456789", encoding="utf-8")
    os.chmod(state_dir / "token", 0o600)
    config = load_config(
        _env(state_dir, LEMONCROW_INSTALL_MODE="local", LEMONCROW_TOKEN="another-legacy-token-0123456789"),
        cwd=worktree,
    )
    assert config.authenticated is True
    assert config.token == "another-legacy-token-0123456789"
    assert config.token_source == "LEMONCROW_TOKEN"


def test_an_absent_credential_is_not_a_misconfiguration(worktree: Path, state_dir: Path) -> None:
    """The hook must still be able to report one line and continue."""
    config = load_config(_env(state_dir), cwd=worktree)
    assert config.authenticated is False
    assert config.token_source == ""


def test_the_settings_file_is_read_but_the_environment_wins(worktree: Path, state_dir: Path) -> None:
    (state_dir / "env").write_text("LEMONCROW_URL=https://from-file:1234\n", encoding="utf-8")
    assert load_config(_env(state_dir), cwd=worktree).url == "https://from-file:1234"
    overridden = load_config(_env(state_dir, LEMONCROW_URL="https://from-env:9999"), cwd=worktree)
    assert overridden.url == "https://from-env:9999"


def test_the_settings_file_cannot_set_anything_outside_this_products_prefix(
    state_dir: Path,
) -> None:
    """A settings file that could set PATH would be an escalation primitive."""
    (state_dir / "env").write_text(
        "\n".join(
            [
                "PATH=/tmp/evil",
                "LD_PRELOAD=/tmp/evil.so",
                "# a comment",
                "LEMONCROW_URL=http://ok:1",
                "LEMONCROW_HOME=/tmp/elsewhere",
                "malformed line",
            ]
        ),
        encoding="utf-8",
    )
    assert read_settings_file(state_dir) == {"LEMONCROW_URL": "http://ok:1"}


def test_an_oversized_settings_file_is_ignored(state_dir: Path) -> None:
    (state_dir / "env").write_text("LEMONCROW_URL=http://x:1\n" + "#" * 20000, encoding="utf-8")
    assert read_settings_file(state_dir) == {}


def test_the_worktree_is_discovered_by_the_git_marker(tmp_path: Path, state_dir: Path) -> None:
    root = tmp_path / "repo"
    (root / "a" / "b").mkdir(parents=True)
    (root / ".git").mkdir()
    config = load_config(_env(state_dir), cwd=root / "a" / "b")
    assert config.repo_root == root.resolve()


def test_a_directory_with_no_git_marker_is_its_own_root(tmp_path: Path, state_dir: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert load_config(_env(state_dir), cwd=plain).repo_root == plain.resolve()


def test_a_canonical_scm_identity_is_used_when_provisioned(worktree: Path) -> None:
    claim = repo_identity(worktree, {"LEMONCROW_SCM_PROVIDER": "github", "LEMONCROW_SCM_REPO_ID": "12345"})
    wire = claim.to_wire()
    assert wire["scm_provider"] == "github"
    assert wire["scm_repo_id"] == "12345"
    assert "remote_url" not in wire and "origin_url" not in wire


def test_a_repository_without_scm_identity_gets_a_stable_opaque_fingerprint(
    worktree: Path,
) -> None:
    first = repo_identity(worktree, {}).fingerprint
    assert first and first == repo_identity(worktree, {}).fingerprint
    assert "/" not in first and ":" not in first


def test_the_state_directory_is_the_only_permitted_location_outside_the_repo(worktree: Path, state_dir: Path) -> None:
    config = load_config(_env(state_dir), cwd=worktree)
    assert config.state_dir == state_dir
    assert config.repo_root == worktree.resolve()


def test_a_group_readable_token_file_is_refused(worktree: Path, state_dir: Path) -> None:
    target = state_dir / "token"
    target.write_text("provisioned-token-0123456789", encoding="utf-8")
    os.chmod(target, 0o640)
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_INSTALL_MODE="hosted"), cwd=worktree)
    assert "chmod 600" in caught.value.message


def test_an_explicit_token_file_path_is_honoured(tmp_path: Path, worktree: Path, state_dir: Path) -> None:
    target = tmp_path / "provisioned"
    target.write_text("it-provisioned-token-0123456789", encoding="utf-8")
    os.chmod(target, 0o600)
    config = load_config(
        _env(state_dir, LEMONCROW_INSTALL_MODE="hosted", LEMONCROW_TOKEN_FILE=str(target)), cwd=worktree
    )
    assert config.token == "it-provisioned-token-0123456789"
    assert config.token_source == str(target)


def test_local_fs_is_off_unless_explicitly_enabled(worktree: Path, state_dir: Path) -> None:
    assert load_config(_env(state_dir), cwd=worktree).offer_local_fs is False
    assert load_config(_env(state_dir, LEMONCROW_LOCAL_FS="1"), cwd=worktree).offer_local_fs is True


def test_resolution_cache_budget_can_be_overridden_or_disabled(worktree: Path, state_dir: Path) -> None:
    one_gib = 1024 * 1024 * 1024
    assert (
        load_config(_env(state_dir, LEMONCROW_RESOLUTION_CACHE_BYTES="1G"), cwd=worktree).resolution_cache_bytes
        == one_gib
    )
    assert load_config(_env(state_dir, LEMONCROW_RESOLUTION_CACHE_BYTES="0"), cwd=worktree).resolution_cache_bytes == 0


def test_cache_disabled_overrides_an_explicit_nonzero_cache_budget(worktree: Path, state_dir: Path) -> None:
    """``LEMONCROW_CACHE_DISABLED`` is the one global kill switch, same name the
    private server's own tool-result caches read -- it must win even over an
    explicit ``LEMONCROW_RESOLUTION_CACHE_BYTES`` asking for a real budget.
    """
    env = _env(state_dir, LEMONCROW_RESOLUTION_CACHE_BYTES="1G", LEMONCROW_CACHE_DISABLED="1")
    assert load_config(env, cwd=worktree).resolution_cache_bytes == 0


def test_cache_disabled_is_off_unless_explicitly_enabled(worktree: Path, state_dir: Path) -> None:
    assert load_config(_env(state_dir), cwd=worktree).resolution_cache_bytes > 0


def test_a_nonsense_timeout_falls_back_to_the_default_rather_than_zero(worktree: Path, state_dir: Path) -> None:
    """A zero budget would turn every request into an immediate failure."""
    config = load_config(
        _env(state_dir, LEMONCROW_STARTUP_BUDGET_S="nonsense", LEMONCROW_REQUEST_TIMEOUT_S="-4"),
        cwd=worktree,
    )
    assert config.startup_budget_s == 1.0
    assert config.request_timeout_s == 10.0
