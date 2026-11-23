from __future__ import annotations

import hashlib
import io
import json
import re
import sys

import pytest
from lemoncrow_client import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_RELEASE_BASE_URL", "https://example.test/rel")
    # Whether this machine has cosign must not decide which path a test takes.
    monkeypatch.setattr(cli, "_release_verifier", lambda: None)
    return tmp_path


def _review(home, *, engine=False):
    (home / "review" / "client").mkdir(parents=True)
    if engine:
        (home / "review" / "engine").mkdir(parents=True)


def _runtime(home, mode=None):
    exe = home / "runtime" / "client" / "bin" / "lemoncrow-client"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    if mode:
        (home / "runtime" / "env").write_text(f"LEMONCROW_INSTALL_MODE={mode}\n")


def _capture(monkeypatch, code=0):
    calls = []

    def fake(args, sink, *, dry_run=False):
        calls.append((list(args), dry_run))
        return code

    monkeypatch.setattr(cli, "_run_release_installer", fake)
    return calls


def test_nothing_installed_returns_2(home, monkeypatch):
    calls = _capture(monkeypatch)
    sink = io.StringIO()
    assert cli.main(["update"], sink) == 2
    assert calls == []
    assert "--product review" in sink.getvalue()


def test_leftover_full_engine_does_not_block_split_update(home, monkeypatch):
    (home / "uv-tools" / "lemoncrow").mkdir(parents=True)
    _review(home)
    calls = _capture(monkeypatch)
    sink = io.StringIO()
    assert cli.main(["update"], sink) == 0
    assert calls == [(["--product", "review", "--cloud"], False), (["--product", "runtime", "--local"], False)]


def test_legacy_only_install_migrates_runtime(home, monkeypatch):
    (home / "uv-tools" / "lemoncrow").mkdir(parents=True)
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "runtime", "--local"], False)]


def test_review_cloud_passes_an_explicit_hosted_flag(home, monkeypatch):
    # The bare installer is Local Review; a hosted update must say so.
    _review(home)
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--cloud"], False)]


def test_review_with_engine_adds_local(home, monkeypatch):
    _review(home, engine=True)
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--local"], False)]


def test_both_products_runtime_lite_adds_local(home, monkeypatch):
    _review(home)
    _runtime(home, mode="lite")
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--cloud"], False), (["--product", "runtime", "--local"], False)]


def test_runtime_hosted_passes_an_explicit_hosted_flag(home, monkeypatch):
    _runtime(home, mode="hosted")
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "runtime", "--cloud"], False)]


def test_dry_run_prints_commands_without_running(home, monkeypatch):
    _review(home, engine=True)
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    sink = io.StringIO()
    assert cli.main(["update", "--dry-run"], sink) == 0
    assert ran == []
    assert (
        "curl -fLsS --retry 3 --connect-timeout 15 https://example.test/rel/install.sh"
        " | bash -s -- --product review --local"
    ) in sink.getvalue()


def test_failure_code_is_propagated(home, monkeypatch):
    _review(home)
    _capture(monkeypatch, code=7)
    sink = io.StringIO()
    assert cli.main(["update"], sink) == 7
    assert "update failed" in sink.getvalue()


def test_update_rejects_unknown_option(home, monkeypatch):
    calls = _capture(monkeypatch)
    assert cli.main(["update", "--bogus"], io.StringIO()) == 2
    assert calls == []


def test_uninstall_without_product_prints_usage_even_on_tty(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    sink = io.StringIO()
    assert cli.main(["uninstall"], sink) == 2
    assert "usage: lc uninstall review|workspaces|all [--dry-run]" in sink.getvalue()


def test_root_host_verb_is_gone_but_rt_host_remains(monkeypatch):
    sink = io.StringIO()
    assert cli.main(["host", "status"], sink) == 2
    assert "unknown command: host" in sink.getvalue()
    assert "It is a Workspaces command: lc rt host" in sink.getvalue()
    assert "  lemoncrow-client host " not in sink.getvalue()
    monkeypatch.setattr(cli, "_host", lambda args, sink: 0)
    assert cli.main(["rt", "host", "status"], io.StringIO()) == 0


def test_full_engine_verbs_name_the_source_checkout():
    sink = io.StringIO()
    assert cli.main(["doctor"], sink) == 2
    out = sink.getvalue()
    assert "unknown command: doctor" in out
    assert "`uv run lc doctor`" in out
    assert "`lc status` and `lc rt status`" in out
    sink = io.StringIO()
    assert cli.main(["nosuchverb"], sink) == 2
    assert sink.getvalue() == "unknown command: nosuchverb\nRun `lc --help` for the commands this client supports.\n"


def test_rt_telemetry_without_the_full_cli_turns_the_client_counts_off(monkeypatch, tmp_path):
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_POLICY", str(tmp_path / "no-policy.toml"))
    monkeypatch.delenv("LEMONCROW_FULL_RUNTIME_CLI", raising=False)
    monkeypatch.setattr(cli, "_full_runtime_executable", lambda: None)
    sink = io.StringIO()
    assert cli.main(["rt", "telemetry", "remote", "off"], sink) == 0
    assert "remote telemetry: off" in sink.getvalue()
    assert "remote_enabled = false" in (tmp_path / "config" / "lemoncrow" / "telemetry.toml").read_text()


def test_not_installed_messages_give_install_command(monkeypatch, tmp_path):
    monkeypatch.setenv("LEMONCROW_NO_LAZY_INSTALL", "1")
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_RUNTIME_CLIENT", raising=False)
    sink = io.StringIO()
    assert cli._delegate_external_runtime(["x"], sink) == 2
    out = sink.getvalue()
    assert "LemonCrow Workspaces is not installed." in out
    assert "curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --product runtime" in out
    assert "choose" not in out


def test_the_review_launcher_hands_lc_workspace_to_the_runtime_client(monkeypatch, tmp_path):
    # When Review is installed it owns `lc`, and its client does not ship the workspace commands.
    monkeypatch.setitem(sys.modules, "lemoncrow_client.workspace_cli", None)
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    log = tmp_path / "argv"
    runtime = tmp_path / "lemoncrow-client"
    runtime.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log}\nexit 7\n")
    runtime.chmod(0o755)
    monkeypatch.setenv("LEMONCROW_RUNTIME_CLIENT", str(runtime))
    assert cli.main(["workspace", "status", "--root", "/p"], io.StringIO()) == 7
    assert log.read_text().split() == ["workspace", "status", "--root", "/p"]

    # Without Runtime it says what to install.
    monkeypatch.delenv("LEMONCROW_RUNTIME_CLIENT")
    monkeypatch.setenv("LEMONCROW_NO_LAZY_INSTALL", "1")
    sink = io.StringIO()
    assert cli.main(["workspace", "status"], sink) == 2
    assert "lc workspace is part of LemonCrow Workspaces." in sink.getvalue()
    assert "--product runtime" in sink.getvalue()


def test_split_install_with_persisted_uninstall_suite_is_updated(home, monkeypatch):
    # Split installs keep uninstall scripts under install/; that is not the full engine.
    (home / "install" / "scripts").mkdir(parents=True)
    _review(home, engine=True)
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--local"], False)]


def test_hosted_server_url_survives_update_and_lazy_fetch(home, monkeypatch):
    # A non-default hosted server must not fall back to the public cloud URL.
    _review(home)
    (home / "review" / "env").write_text(
        "LEMONCROW_URL=https://lemoncrow.acme.test\nLEMONCROW_INSTALL_MODE=hosted\n", encoding="utf-8"
    )
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    monkeypatch.delenv("LEMONCROW_NO_LAZY_INSTALL", raising=False)
    assert cli._lazy_install_product("runtime", io.StringIO()) is True
    cloud = ["--cloud", "--url", "https://lemoncrow.acme.test"]
    assert calls == [(["--product", "review", *cloud], False), (["--product", "runtime", *cloud], False)]


@pytest.mark.parametrize(
    ("product", "setup", "expected"),
    [
        ("runtime", lambda h: _review(h, engine=True), ["--product", "runtime", "--local"]),
        ("runtime", lambda h: _review(h), ["--product", "runtime", "--cloud"]),
        ("review", lambda h: _runtime(h, "lite"), ["--product", "review", "--local"]),
        ("review", lambda h: _runtime(h, "hosted"), ["--product", "review", "--cloud"]),
    ],
)
def test_lazy_install_matches_installed_deployment(home, monkeypatch, product, setup, expected):
    setup(home)
    monkeypatch.delenv("LEMONCROW_NO_LAZY_INSTALL", raising=False)
    calls = _capture(monkeypatch)
    assert cli._lazy_install_product(product, io.StringIO()) is True
    assert calls == [(expected, False)]


def test_local_review_update_preserves_recorded_port(home, monkeypatch):
    _review(home, engine=True)
    (home / "review" / "env").write_text("LEMONCROW_INSTALL_MODE=local\nLEMONCROW_URL=http://127.0.0.1:7428\n")
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--local", "--port", "7428"], False)]


def test_recorded_hosted_deployment_wins_over_leftover_engine(home, monkeypatch):
    _review(home, engine=True)
    (home / "review" / "env").write_text("LEMONCROW_INSTALL_MODE=hosted\nLEMONCROW_URL=https://lc.example\n")
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--cloud", "--url", "https://lc.example"], False)]


def test_failed_installer_download_is_not_success(home, monkeypatch):
    monkeypatch.setenv("LEMONCROW_RELEASE_BASE_URL", (home / "missing-release").as_uri())
    assert cli._run_release_installer(["--product", "review"], io.StringIO()) != 0


def test_installer_url_is_literal_shell_data(home, monkeypatch):
    marker = home / "injected"
    release = home / f"release$(touch {marker})"
    monkeypatch.setenv("LEMONCROW_RELEASE_BASE_URL", release.as_uri())
    # URI escaping itself is not the protection: use a literal substitution.
    monkeypatch.setenv("LEMONCROW_RELEASE_BASE_URL", f"file://{release}")
    assert cli._run_release_installer([], io.StringIO()) != 0
    assert not marker.exists()


def test_client_initiated_install_cannot_inherit_unverified_download_bypass(home, monkeypatch):
    """Even an explicit shell bypass cannot relax client-managed artifact checks."""
    monkeypatch.setenv("LEMONCROW_ALLOW_UNVERIFIED", "1")
    captured = []

    def execute(args, **kwargs):
        captured.append((args, kwargs))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(cli.subprocess, "run", execute)
    assert cli._run_release_installer(["--product", "review"], io.StringIO()) == 0
    assert captured[0][1]["env"]["LEMONCROW_ALLOW_UNVERIFIED"] == "0"
    assert captured[0][0][:4] == ["bash", "-o", "pipefail", "-c"]


@pytest.fixture
def installs(monkeypatch):
    """Each installer command run, instead of running it; the once-per-run warning starts unsaid."""
    cli._warn_unsigned_installer.cache_clear()
    ran = []

    def execute(args, **_kwargs):
        ran.append(args)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(cli.subprocess, "run", execute)
    yield ran
    cli._warn_unsigned_installer.cache_clear()


def test_without_cosign_update_warns_once_on_stderr_and_continues(home, installs, capsys):
    _review(home)
    _runtime(home)
    sink = io.StringIO()
    assert cli.main(["update"], sink) == 0
    assert len(installs) == 2  # both products still installed
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and err == cli._UNSIGNED_INSTALLER_WARNING  # one line for the whole update
    assert "signature was not checked" in err and "cosign is not installed" in err
    assert "https://docs.sigstore.dev/cosign/system_config/installation/" in err
    assert "signature" not in sink.getvalue()  # stderr only


def test_without_cosign_a_preview_runs_nothing_and_warns_nothing(home, installs, capsys):
    assert cli._run_release_installer(["--product", "runtime"], io.StringIO(), dry_run=True) == 0
    assert installs == [] and capsys.readouterr().err == ""


def test_client_install_preview_does_not_execute_any_downloader(home, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run must never download or install")

    monkeypatch.setattr(cli.subprocess, "run", forbidden)
    assert cli._run_release_installer(["--product", "runtime"], io.StringIO(), dry_run=True) == 0


# --------------------------------------------------------------------------- #
# With cosign installed, the installer runs only once its signature verifies  #
# --------------------------------------------------------------------------- #

ISSUER = "https://token.actions.githubusercontent.com"
RELEASE = "https://github.com/lemoncrow-lab/lemoncrow/.github/workflows/release.yml@refs/tags/"

#: Stands in for cosign: the "bundle" names its signer and the digest it signed, and verify-blob succeeds only when
#: the blob's digest, the issuer and the pinned identity (exact, or the regexp cosign searches with) all match it.
_STUB_COSIGN = """\
import hashlib, json, os, re, sys
args = sys.argv[1:]
with open(os.environ["STUB_COSIGN_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
flags = dict(zip(args[1:-1:2], args[2:-1:2]))
bundle = json.load(open(flags["--bundle"]))
with open(args[-1], "rb") as blob:
    digest = hashlib.sha256(blob.read()).hexdigest()
signer = bundle["identity"]
ok = (
    args[0] == "verify-blob"
    and bundle["sha256"] == digest
    and bundle["issuer"] == flags["--certificate-oidc-issuer"]
    and (
        signer == flags.get("--certificate-identity")
        or bool(re.search(flags.get("--certificate-identity-regexp", "(?!)"), signer))
    )
)
sys.exit(0 if ok else 1)
"""


@pytest.fixture
def signed_release(home, monkeypatch):
    """A release directory served over file://, an installer that records how it ran, and a stub cosign."""
    release = home / "release"
    release.mkdir()
    (release / "install.sh").write_text('printf "%s\\n" "$LEMONCROW_ALLOW_UNVERIFIED" "$@" > "$INSTALL_MARKER"\n')
    cosign = home / "cosign"
    cosign.write_text(f"#!{sys.executable}\n{_STUB_COSIGN}")
    cosign.chmod(0o755)
    monkeypatch.setattr(cli, "_release_verifier", lambda: str(cosign))
    monkeypatch.setenv("LEMONCROW_RELEASE_BASE_URL", release.as_uri())
    monkeypatch.setenv("INSTALL_MARKER", str(home / "ran"))
    monkeypatch.setenv("STUB_COSIGN_LOG", str(home / "cosign.log"))
    monkeypatch.delenv("LEMONCROW_RELEASE_TAG", raising=False)
    return release


def _sign(release, identity, issuer=ISSUER):
    digest = hashlib.sha256((release / "install.sh").read_bytes()).hexdigest()
    bundle = {"identity": identity, "issuer": issuer, "sha256": digest}
    (release / "install.sh.sigstore.json").write_text(json.dumps(bundle))


def _cosign_calls(home):
    log = home / "cosign.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_a_signed_installer_runs_once_cosign_verifies_the_release_workflow_signed_it(home, signed_release):
    _sign(signed_release, RELEASE + "v1.2.3")
    assert cli._run_release_installer(["--product", "review", "--local"], io.StringIO()) == 0
    assert (home / "ran").read_text().splitlines() == ["0", "--product", "review", "--local"]
    [call] = _cosign_calls(home)
    assert call[:3] == ["verify-blob", "--bundle", call[2]] and call[2].endswith("install.sh.sigstore.json")
    assert call[3:7] == [
        "--certificate-identity-regexp",
        cli._RELEASE_SIGNER_ANY_TAG,
        "--certificate-oidc-issuer",
        ISSUER,
    ]


def test_with_cosign_the_installer_runs_without_the_unchecked_warning(home, signed_release, capsys):
    cli._warn_unsigned_installer.cache_clear()
    _sign(signed_release, RELEASE + "v1.2.3")
    assert cli._run_release_installer(["--product", "review"], io.StringIO()) == 0
    assert (home / "ran").exists()
    assert "signature was not checked" not in capsys.readouterr().err


def test_a_tampered_installer_is_refused_and_never_runs(home, signed_release):
    _sign(signed_release, RELEASE + "v1.2.3")
    (signed_release / "install.sh").write_text('touch "$INSTALL_MARKER"\n')
    sink = io.StringIO()
    assert cli._run_release_installer(["--product", "review"], sink) != 0
    assert not (home / "ran").exists()
    assert "is not signed by LemonCrow's release workflow" in sink.getvalue()


@pytest.mark.parametrize(
    ("identity", "issuer"),
    [
        ("https://github.com/someone/lemoncrow/.github/workflows/release.yml@refs/tags/v1.2.3", ISSUER),
        ("https://github.com/lemoncrow-lab/lemoncrow/.github/workflows/tests.yml@refs/tags/v1.2.3", ISSUER),
        (RELEASE.replace("refs/tags/", "refs/heads/") + "main", ISSUER),
        (RELEASE + "v1.2.3", "https://accounts.google.com"),
    ],
    ids=["fork", "other-workflow", "branch", "other-issuer"],
)
def test_a_signature_by_anyone_else_is_refused(home, signed_release, identity, issuer):
    _sign(signed_release, identity, issuer)
    assert cli._run_release_installer(["--product", "review"], io.StringIO()) != 0
    assert not (home / "ran").exists()


def test_a_pinned_tag_accepts_only_that_tags_signature(home, signed_release, monkeypatch):
    monkeypatch.setenv("LEMONCROW_RELEASE_TAG", "v1.2.4")
    _sign(signed_release, RELEASE + "v1.2.3")  # an older release offered as v1.2.4
    assert cli._run_release_installer(["--product", "review"], io.StringIO()) != 0
    assert not (home / "ran").exists()
    _sign(signed_release, RELEASE + "v1.2.4")
    assert cli._run_release_installer(["--product", "review"], io.StringIO()) == 0
    assert _cosign_calls(home)[-1][3:5] == ["--certificate-identity", RELEASE + "v1.2.4"]


def test_a_missing_signature_bundle_is_refused_before_cosign_or_the_installer_runs(home, signed_release):
    sink = io.StringIO()
    assert cli._run_release_installer(["--product", "review"], sink) != 0
    assert not (home / "ran").exists()
    assert _cosign_calls(home) == []
    assert "has no signature bundle" in sink.getvalue()


def test_a_signed_install_preview_prints_the_checks_and_runs_nothing(home, signed_release, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run must never download or install")

    monkeypatch.setattr(cli.subprocess, "run", forbidden)
    sink = io.StringIO()
    assert cli._run_release_installer(["--product", "runtime"], sink, dry_run=True) == 0
    lines = sink.getvalue().splitlines()
    assert "install.sh.sigstore.json" in lines[1]
    assert " verify-blob --bundle " in lines[2]
    assert lines[3].startswith("bash -s -- --product runtime < ")


@pytest.mark.parametrize(
    ("tag", "accepted"),
    [("v1.2.3", True), ("v0.10.0-rc2", True), ("v1.2", False), ("1.2.3", False), ("v1.2.3-beta", False)],
)
def test_latest_accepts_the_release_workflow_at_a_release_tag_only(tag, accepted):
    assert bool(re.search(cli._RELEASE_SIGNER_ANY_TAG, RELEASE + tag)) is accepted
    assert cli._RELEASE_SIGNER == RELEASE
