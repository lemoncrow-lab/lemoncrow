from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "install.sh"

_STUB_INSTALL = r"""
case " $* " in *" --dry-run "*) exit 0 ;; esac
state=${LEMONCROW_HOME:-$HOME/.lemoncrow}
bin=${LEMONCROW_BIN_DIR:-$state/bin}
mkdir -p "$bin"
printf '#!/bin/sh\nexit 0\n' >"$bin/lc"
chmod +x "$bin/lc"
case "$(basename "$0")" in
  hosted.sh|local_review.sh)
    mkdir -p "$state/review"
    mode=hosted
    if [ "$(basename "$0")" = local_review.sh ]; then mode=local; fi
    printf 'LEMONCROW_INSTALL_MODE=%s\n' "$mode" >"$state/review/env"
    printf '#!/bin/sh\nexit 0\n' >"$bin/lcr"
    chmod +x "$bin/lcr"
    mkdir -p "$state/install/scripts"
    printf '#!/bin/sh\nexit 0\n' >"$state/install/scripts/local_server.sh"
    ;;
  runtime.sh|local_runtime.sh)
    mkdir -p "$state/runtime/client/bin"
    printf '#!/bin/sh\nexit 0\n' >"$state/runtime/client/bin/lemoncrow-client"
    chmod +x "$state/runtime/client/bin/lemoncrow-client"
    ;;
esac
"""


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    script = tmp_path / "install.sh"
    shutil.copy2(INSTALLER, script)
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    log = tmp_path / "calls.log"
    for name in ("hosted.sh", "runtime.sh", "local_review.sh", "local_runtime.sh"):
        helper = helpers / name
        helper.write_text(
            '#!/bin/sh\nset -eu\nprintf \'%s|%s\\n\' "$(basename "$0")" "$*" >> "$LC_INSTALL_LOG"\n' + _STUB_INSTALL,
            encoding="utf-8",
        )
        helper.chmod(0o755)
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("LEMONCROW_")},
        "LEMONCROW_INSTALL_HELPER_DIR": str(helpers),
        "LC_INSTALL_LOG": str(log),
        "HOME": str(tmp_path / "home"),
        "SHELL": "/bin/bash",
        "ZDOTDIR": str(tmp_path / "home"),
    }
    return script, env, log


def _run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(("shell", "profile_name"), [("/bin/bash", ".bashrc"), ("/bin/zsh", ".zshrc")])
def test_default_install_persists_activation_without_duplicate_blocks(tmp_path, shell, profile_name) -> None:
    script, env, _log = _fixture(tmp_path)
    env["SHELL"] = shell
    home = Path(env["HOME"])
    home.mkdir()
    profile = home / profile_name
    profile.write_text("echo user-settings\n")
    for _ in range(2):
        result = _run(script, env, "--cloud")
        assert result.returncode == 0, result.stderr
    text = profile.read_text()
    assert text.count("# >>> lemoncrow path setup >>>") == 1
    assert "echo user-settings" in text
    assert "shell-env.sh" in text
    activation = home / ".lemoncrow/shell-env.sh"
    assert activation.exists()
    probe = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; printf "%s\\n" "$LEMONCROW_HOME" "$LEMONCROW_BIN_DIR" "${PATH%%:*}"',
            "probe",
            str(activation),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.splitlines() == [
        str(home / ".lemoncrow"),
        str(home / ".lemoncrow/bin"),
        str(home / ".lemoncrow/bin"),
    ]
    assert "new terminals are ready" in result.stdout


def test_sandbox_activation_does_not_change_default_shell_profile(tmp_path) -> None:
    script, env, _log = _fixture(tmp_path)
    home = Path(env["HOME"])
    home.mkdir()
    profile = home / ".bashrc"
    profile.write_text("echo user-settings\n")
    sandbox = home / ".lemoncrow-oss"
    result = _run(script, {**env, "LEMONCROW_HOME": str(sandbox)}, "--local")
    assert result.returncode == 0, result.stderr
    assert profile.read_text() == "echo user-settings\n"
    assert (sandbox / "shell-env.sh").exists()
    assert "new terminals are ready" not in result.stdout


def test_dry_run_does_not_write_shell_setup(tmp_path) -> None:
    script, env, _log = _fixture(tmp_path)
    result = _run(script, env, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert not Path(env["HOME"]).exists()


def test_public_installer_has_one_summary_and_highlights_commands(tmp_path) -> None:
    script, env, _log = _fixture(tmp_path)
    helper = Path(env["LEMONCROW_INSTALL_HELPER_DIR"]) / "hosted.sh"
    formatter = shlex.quote(str(ROOT / "scripts/lib/terminal.sh"))
    helper.write_text(
        "#!/bin/bash\n"
        + _STUB_INSTALL
        + f"source {formatter}\n"
        + "lc_ui_printf '1;32' 'child install summary\\n'\n"
        + "lc_ui_printf '1;33' 'child warning\\n' >&2\n"
    )
    result = _run(script, {**env, "FORCE_COLOR": "1", "NO_COLOR": ""}, "--cloud")
    assert result.returncode == 0, result.stderr
    assert "child install summary" not in result.stdout
    assert "child warning" in result.stderr
    assert result.stdout.count("Hosted Review ready") == 1
    assert result.stdout.count("Products:") == 0
    assert result.stdout.count("Deployment:") == 0
    assert result.stdout.count("Review endpoint:") == 1
    for command in ("lcr", "lc auth login", "lc runtime enable codex"):
        assert f"\x1b[38;2;155;117;217m{command}\x1b[0m" in result.stdout
    assert "not installed by this installer" not in result.stdout


@pytest.mark.parametrize("deployment", ["--local", "--cloud"])
@pytest.mark.parametrize("product", ["review", "runtime", "both"])
def test_preview_renders_order_without_installing_or_changing_shell(tmp_path, deployment, product) -> None:
    script, env, log = _fixture(tmp_path)
    result = _run(script, env, "--preview", deployment, "--product", product)
    assert result.returncode == 0, result.stderr
    assert "Preview only:" in result.stdout
    assert not log.exists()
    assert not Path(env["HOME"]).exists()
    verified = result.stdout.index("Verification steps previewed; no checks were run.")
    expected = result.stdout.index("Expected result:")
    next_steps = result.stdout.index("After a real installation:")
    assert verified < expected < next_steps
    assert "(not installed)" in result.stdout
    assert "Installation verified" not in result.stdout
    assert " ready" not in result.stdout
    assert "Optional:" in result.stdout
    assert next_steps < result.stdout.index("Optional:")


def test_failed_command_verification_does_not_print_ready_or_next(tmp_path) -> None:
    script, env, _log = _fixture(tmp_path)
    helper = Path(env["LEMONCROW_INSTALL_HELPER_DIR"]) / "hosted.sh"
    helper.write_text(
        "#!/bin/sh\n" + _STUB_INSTALL + '\nprintf "#!/bin/sh\\nexit 1\\n" >"$bin/lcr"\nchmod +x "$bin/lcr"\n'
    )
    result = _run(script, env, "--cloud")
    assert result.returncode != 0
    assert "lcr verification failed" in result.stderr
    assert " ready" not in result.stdout
    assert "Next:" not in result.stdout
    assert not (Path(env["HOME"]) / ".lemoncrow/shell-env.sh").exists()


def test_hosted_configuration_is_verified_by_the_public_installer(tmp_path) -> None:
    script, env, _log = _fixture(tmp_path)
    helper = Path(env["LEMONCROW_INSTALL_HELPER_DIR"]) / "hosted.sh"
    helper.write_text(
        "#!/bin/sh\n" + _STUB_INSTALL + '\nprintf "LEMONCROW_INSTALL_MODE=local\\n" >"$state/review/env"\n'
    )
    result = _run(script, env, "--cloud", "--url", "http://127.0.0.1:3130")
    assert result.returncode != 0
    assert "Hosted Review configuration verification failed" in result.stderr
    assert " ready" not in result.stdout
    assert "Next:" not in result.stdout


def test_nested_installer_dry_run_keeps_the_plan(tmp_path) -> None:
    env = {**os.environ, "LEMONCROW_INSTALL_NESTED": "1", "NO_COLOR": "1"}
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/hosted.sh"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Product: LemonCrow Review" in result.stdout
    assert "Review endpoint:" in result.stdout


def test_review_cloud_delegates_to_review_installer_only(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    result = _run(script, env, "--product", "review", "--cloud", "--non-interactive")
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["hosted.sh|--url https://api.lemoncrow.com"]
    assert "Hosted Review ready" in result.stdout
    assert result.stdout.index("Installation verified") < result.stdout.index("Hosted Review ready")
    assert "lc auth login" in result.stdout
    assert "lcr" in result.stdout
    assert "lc rt status" not in result.stdout


def test_both_local_uses_review_server_then_serverless_runtime_lite(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    result = _run(
        script,
        env,
        "--product",
        "both",
        "--local",
        "--port",
        "7555",
        "--agent",
        "codex",
        "--non-interactive",
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "local_review.sh|--port 7555",
        "local_runtime.sh|--agent codex",
    ]
    assert "Local Review and Workspaces Lite ready" in result.stdout
    assert result.stdout.index("Installation verified") < result.stdout.index("Local Review and Workspaces Lite ready")
    assert "Review endpoint: http://127.0.0.1:7555" in result.stdout
    assert "Workspaces Lite ready" in result.stdout


@pytest.mark.parametrize("product", ["workspaces", "runtime"])
@pytest.mark.parametrize("spelling", ["separate", "equals", "env"])
def test_product_workspaces_is_an_alias_of_runtime(tmp_path: Path, product: str, spelling: str) -> None:
    script, env, log = _fixture(tmp_path)
    if spelling == "separate":
        args: tuple[str, ...] = ("--product", product)
    elif spelling == "equals":
        args = (f"--product={product}",)
    else:
        env["LEMONCROW_PRODUCT"] = product
        args = ()
    result = _run(script, env, *args, "--local", "--agent", "codex", "--non-interactive")
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["local_runtime.sh|--agent codex"]
    assert "Workspaces Lite ready" in result.stdout


def test_product_workspaces_hosted_uses_the_runtime_helper(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    result = _run(script, env, "--product", "workspaces", "--hosted", "--non-interactive")
    assert result.returncode == 0, result.stderr
    assert [line.split("|", 1)[0] for line in log.read_text().splitlines()] == ["runtime.sh"]


def test_help_and_errors_name_the_workspaces_product(tmp_path: Path) -> None:
    script, env, _log = _fixture(tmp_path)
    help_text = _run(script, env, "--help").stdout
    assert "--product review|workspaces|both" in help_text
    assert "--product workspaces --agent codex" in help_text
    bad = _run(script, env, "--product", "nope")
    assert bad.returncode == 2
    assert "--product must be review, workspaces, or both" in bad.stderr
    agent = _run(script, env, "--product", "review", "--agent", "codex")
    assert agent.returncode == 2
    assert "choose --product workspaces or --product both" in agent.stderr


def test_runtime_only_local_never_passes_a_server_port(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    result = _run(script, env, "--product", "runtime", "--local", "--port", "99999", "--non-interactive")
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["local_runtime.sh|"]
    assert "Workspaces Lite ready" in result.stdout
    assert "Review endpoint:" not in result.stdout


def test_bare_install_defaults_to_local_review_without_account_or_prompting(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    result = subprocess.run(
        ["bash", str(script)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["local_review.sh|--port 7420"]
    assert "Local Review ready" in result.stdout
    assert result.stdout.index("Installation verified") < result.stdout.index("Local Review ready")
    assert "lc auth login" not in result.stdout
    assert "--hosted" in result.stdout


@pytest.mark.parametrize("flag", ["--hosted", "--cloud"])
def test_hosted_is_opt_in_and_cloud_stays_an_alias(tmp_path: Path, flag: str) -> None:
    script, env, log = _fixture(tmp_path)
    result = _run(script, env, flag)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["hosted.sh|--url https://api.lemoncrow.com"]
    assert "Hosted Review ready" in result.stdout
    assert "lc auth login" in result.stdout


def test_bare_rerun_keeps_an_existing_hosted_install_hosted(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    review = Path(env["HOME"]) / ".lemoncrow/review"
    review.mkdir(parents=True)
    (review / "env").write_text("LEMONCROW_URL=https://review.example.test\nLEMONCROW_INSTALL_MODE=hosted\n")
    result = _run(script, env)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["hosted.sh|--url https://review.example.test"]
    assert "Keeping the existing Hosted install" in result.stdout


# ---- helper trust: piped copies, release pins (SW-2, SW-15) -----------------

_PIPED_HELPERS = ("hosted.sh", "runtime.sh", "local_review.sh", "local_runtime.sh", "engine_install.sh")
_LOGGED_STUB = (
    "#!/bin/sh\nset -eu\n"
    'printf \'%s|%s|%s|%s\\n\' "$(basename "$0")" "$*" "${LEMONCROW_RELEASE_TAG:-}" "${LEMONCROW_RELEASE_PINS:-}"'
    ' >> "$LC_INSTALL_LOG"\n'
)


def _release_dir(tmp_path: Path) -> Path:
    release = tmp_path / "release"
    release.mkdir()
    for name in _PIPED_HELPERS:
        (release / name).write_text(_LOGGED_STUB + _STUB_INSTALL, encoding="utf-8")
    (release / "lemoncrow-review-client.tar.gz").write_bytes(b"review client archive")
    return release


def _stamp(release: Path, tag: str = "v9.9.9") -> Path:
    installer = release / "install.sh"
    shutil.copy2(INSTALLER, installer)
    subprocess.run(
        ["bash", str(ROOT / "scripts/pin_release_installer.sh"), str(release)],
        env={**os.environ, "LEMONCROW_RELEASE_TAG": tag},
        check=True,
        capture_output=True,
    )
    return installer


def _decoy_cwd(tmp_path: Path) -> tuple[Path, Path]:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    marker = tmp_path / "hijacked"
    for name in (*_PIPED_HELPERS, "review_engine.sh", "local_server.sh", "uninstall.sh"):
        (cwd / name).write_text(f"#!/bin/sh\necho {name} >> {shlex.quote(str(marker))}\n", encoding="utf-8")
    return cwd, marker


def _piped_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    _script, env, _log = _fixture(tmp_path)
    env.pop("LEMONCROW_INSTALL_HELPER_DIR")
    env.update(TMPDIR=str(tmp_path), **extra)
    return env


def _pipe(installer: Path, env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-s", "--", *args],
        input=installer.read_text(encoding="utf-8"),
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("args", "engine", "expected"),
    [
        (("--dry-run",), False, "local_review.sh"),
        (("--hosted",), False, "hosted.sh"),
        (("--product", "both", "--hosted"), False, "runtime.sh"),
        (("--product", "runtime"), False, "local_runtime.sh"),
        (("--local", "--no-hosts"), True, "engine_install.sh"),
    ],
)
def test_piped_install_never_runs_helpers_from_the_current_directory(
    tmp_path: Path, args: tuple[str, ...], engine: bool, expected: str
) -> None:
    release = _release_dir(tmp_path)
    cwd, marker = _decoy_cwd(tmp_path)
    env = _piped_env(tmp_path, LEMONCROW_RELEASE_BASE_URL=release.as_uri())
    if engine:
        env["LEMONCROW_ENGINE_INSTALL"] = "1"
    result = _pipe(INSTALLER, env, cwd, *args)
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), marker.read_text()
    calls = [line.split("|", 1)[0] for line in (tmp_path / "calls.log").read_text().splitlines()]
    assert expected in calls


@pytest.mark.parametrize("helper", ["local_review.sh", "local_runtime.sh"])
def test_piped_product_helpers_never_run_siblings_from_the_current_directory(tmp_path: Path, helper: str) -> None:
    release = _release_dir(tmp_path)
    for name in ("review_engine.sh", "local_server.sh"):
        (release / name).write_text(_LOGGED_STUB, encoding="utf-8")
    cwd, marker = _decoy_cwd(tmp_path)
    env = _piped_env(tmp_path, LEMONCROW_RELEASE_BASE_URL=release.as_uri())
    result = _pipe(ROOT / "scripts" / helper, env, cwd, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), marker.read_text()
    assert (tmp_path / "calls.log").read_text()


def test_unreleased_piped_copy_refuses_to_download_unpinned_helpers(tmp_path: Path) -> None:
    cwd, marker = _decoy_cwd(tmp_path)
    result = _pipe(INSTALLER, _piped_env(tmp_path), cwd)
    assert result.returncode != 0
    assert "carries no release pins" in result.stderr
    assert "https://lemoncrow.com/install.sh" in result.stderr
    assert not marker.exists()
    assert not (tmp_path / "calls.log").exists()
    assert not list(tmp_path.glob("lemoncrow-install.*"))


def test_pinned_installer_runs_verified_helpers_of_its_own_release(tmp_path: Path) -> None:
    release = _release_dir(tmp_path)
    installer = _stamp(release)
    archive_sha = hashlib.sha256((release / "lemoncrow-review-client.tar.gz").read_bytes()).hexdigest()
    cwd, marker = _decoy_cwd(tmp_path)
    env = _piped_env(tmp_path, LEMONCROW_RELEASE_BASE_URL=release.as_uri())
    result = _pipe(installer, env, cwd)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    name, args, tag, pins = (tmp_path / "calls.log").read_text().splitlines()[0].split("|")
    assert (name, args, tag) == ("local_review.sh", "--port 7420", "v9.9.9")
    assert f"lemoncrow-review-client.tar.gz={archive_sha}" in pins.split()
    assert "Local Review ready" in result.stdout
    assert not list(tmp_path.glob("lemoncrow-install.*"))


@pytest.mark.parametrize("piped", [True, False])
def test_pinned_installer_refuses_a_helper_that_does_not_match_its_pin(tmp_path: Path, piped: bool) -> None:
    release = _release_dir(tmp_path)
    installer = _stamp(release)
    (release / "local_review.sh").write_text('#!/bin/sh\necho tampered >> "$LC_INSTALL_LOG"\n')
    env = _piped_env(tmp_path, LEMONCROW_RELEASE_BASE_URL=release.as_uri())
    if piped:
        result = _pipe(installer, env, tmp_path)
    else:
        result = subprocess.run(["bash", str(installer)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "local_review.sh does not match the SHA-256 pinned in this installer" in result.stderr
    assert not (tmp_path / "calls.log").exists()
    assert "ready" not in result.stdout


def test_release_stage_pins_every_staged_asset_into_install_sh(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ("lemoncrow-review-client", "lemoncrow-review-engine", "lemoncrow-runtime-client"):
        (dist / f"{name}.tar.gz").write_text(name)
        (dist / f"{name}.tar.gz.sha256").write_text("fixture")
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    (binaries / "lemoncrow-distribution-linux-x86_64.tar.gz").write_text("engine")
    stage = tmp_path / "stage"
    subprocess.run(
        ["bash", str(ROOT / "scripts/stage_release.sh"), str(stage), str(dist), str(binaries)],
        env={**os.environ, "LEMONCROW_RELEASE_TAG": "v1.2.3"},
        check=True,
        capture_output=True,
    )
    staged = (stage / "install.sh").read_text()
    assert '_PUBLIC_PINNED_TAG="v1.2.3"\n' in staged
    pins = dict(
        entry.split("=", 1) for entry in staged.split('_PUBLIC_PINNED_SHA256S="', 1)[1].split('"', 1)[0].split()
    )
    expected = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [*stage.glob("*.sh"), *stage.glob("*.tar.gz"), *binaries.glob("*.tar.gz")]
        if path.name != "install.sh"
    }
    assert pins == expected
    assert {"hosted.sh", "local_review.sh", "lemoncrow-review-client.tar.gz"} <= set(pins)
    # The source copy stays unpinned; only the staged release copy carries pins.
    assert '_PUBLIC_PINNED_SHA256S=""\n' in INSTALLER.read_text()


def _product_python() -> str:
    for candidate in (sys.executable, shutil.which("python3.13"), shutil.which("python3.12")):
        if (
            candidate
            and subprocess.run(
                [candidate, "-c", "import sys; raise SystemExit(sys.version_info[:2] not in {(3, 12), (3, 13)})"],
                check=False,
            ).returncode
            == 0
        ):
            return candidate
    raise AssertionError("product helpers need Python 3.12 or 3.13")


@pytest.mark.parametrize(
    ("helper", "asset"),
    [
        ("hosted.sh", "lemoncrow-review-client.tar.gz"),
        ("runtime.sh", "lemoncrow-runtime-client.tar.gz"),
        ("review_engine.sh", "lemoncrow-review-engine.tar.gz"),
    ],
)
@pytest.mark.parametrize("pin_matches", [True, False])
def test_archive_pin_replaces_the_same_origin_checksum_sidecar(
    tmp_path: Path, helper: str, asset: str, pin_matches: bool
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    archive = release / asset
    archive.write_bytes(b"not really a tarball")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    # The sidecar served beside the archive agrees with a swapped archive; the pin does not.
    (release / f"{asset}.sha256").write_text((digest if not pin_matches else "0" * 64) + "\n")
    pin = digest if pin_matches else "f" * 64
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("LEMONCROW_")},
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path),
        "LEMONCROW_PYTHON": _product_python(),
        "LEMONCROW_RELEASE_BASE_URL": release.as_uri(),
        "LEMONCROW_RELEASE_PINS": f"other.tar.gz={'a' * 64} {asset}={pin}",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / helper)], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0  # the fixture is not a real archive
    assert ("checksum mismatch" in result.stderr) is not pin_matches, result.stderr


def _engine_stub(path: Path) -> None:
    path.write_text(
        '#!/bin/sh\nprintf \'engine_install.sh|%s\\n\' "$*" >> "$LC_INSTALL_LOG"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_engine_install_flag_delegates_to_sibling_engine_installer(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    _engine_stub(tmp_path / "engine_install.sh")
    result = _run(script, {**env, "LEMONCROW_ENGINE_INSTALL": "1"}, "--local", "--no-hosts")
    assert result.returncode == 0, result.stderr
    # Original arguments reach the engine bootstrap; no product helper runs.
    assert log.read_text().splitlines() == ["engine_install.sh|--local --no-hosts"]
    assert "LemonCrow install" not in result.stdout


def test_engine_install_flag_fetches_engine_installer_from_release_base(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    _engine_stub(release / "engine_install.sh")
    env = {k: v for k, v in env.items() if k != "LEMONCROW_INSTALL_HELPER_DIR"}
    env.update(
        {
            "LEMONCROW_ENGINE_INSTALL": "1",
            "LEMONCROW_RELEASE_BASE_URL": release.as_uri(),
            "TMPDIR": str(tmp_path),
        }
    )
    result = _run(script, env, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["engine_install.sh|--dry-run"]


def test_review_rejects_runtime_agent_option(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    result = _run(
        script,
        env,
        "--product",
        "review",
        "--agent",
        "claude",
        "--non-interactive",
    )
    assert result.returncode == 2
    assert "--agent requires Workspaces" in result.stderr
    assert not log.exists()


def test_help_explains_products_and_safe_runtime_opt_in(tmp_path: Path) -> None:
    script, env, _log = _fixture(tmp_path)
    result = _run(script, env, "--help")
    assert result.returncode == 0
    assert "review      Review code changes" in result.stdout
    assert "workspaces  Workspaces: LemonCrow code intelligence" in result.stdout
    assert "(`runtime` is accepted as an alias)" in result.stdout
    assert "both        Install Review and Workspaces together" in result.stdout
    assert "Workspaces never modifies a coding-agent host unless --agent is" in result.stdout
    assert "lc rt enable <host>" in result.stdout


def test_downloaded_helpers_are_shared_and_cleaned_on_exit(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    env.pop("LEMONCROW_INSTALL_HELPER_DIR")
    env.update(LEMONCROW_RELEASE_BASE_URL=(tmp_path / "helpers").as_uri(), TMPDIR=str(tmp_path))
    result = _run(script, env, "--product", "both")
    assert result.returncode == 0, result.stderr
    assert len(log.read_text().splitlines()) == 2
    assert not list(tmp_path.glob("lemoncrow-install.*"))


def test_remote_hostname_starting_with_localhost_stays_cloud(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    result = _run(script, env, "--url", "http://localhost.example.test:7555")
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["hosted.sh|--url http://localhost.example.test:7555"]


def test_failed_helper_download_fails_and_cleans_temp_dir(tmp_path: Path) -> None:
    script, env, log = _fixture(tmp_path)
    env.pop("LEMONCROW_INSTALL_HELPER_DIR")
    shim = tmp_path / "bin"
    shim.mkdir()
    curl = shim / "curl"
    curl.write_text("#!/bin/sh\nexit 22\n")
    curl.chmod(0o755)
    env.update(PATH=f"{shim}:{os.environ['PATH']}", TMPDIR=str(tmp_path))
    result = _run(script, env, "--product", "both")
    assert result.returncode != 0
    assert not log.exists()
    assert not list(tmp_path.glob("lemoncrow-install.*"))


def test_printed_setup_selects_installation_despite_inherited_environment(tmp_path: Path) -> None:
    script, env, _log = _fixture(tmp_path)
    install_home = tmp_path / "install home"
    install_bin = tmp_path / "custom bin"
    env.update(
        LEMONCROW_HOME=str(install_home),
        LEMONCROW_BIN_DIR=str(install_bin),
        LEMONCROW_URL="http://127.0.0.1:51689",
        PYTHONPATH=str(tmp_path / "checkout"),
    )
    for deployment in ("--local", "--cloud"):
        result = _run(script, env, "--product", "review", deployment)
        assert result.returncode == 0, result.stderr
        setup = next(
            line.partition("Activate in this terminal: ")[2]
            for line in result.stdout.splitlines()
            if "Activate in this terminal: " in line
        )
        probe = subprocess.run(
            [
                "bash",
                "-c",
                setup + '\nprintf "%s\\n" "$LEMONCROW_HOME" "$LEMONCROW_BIN_DIR" '
                '"${PATH%%:*}" "${LEMONCROW_URL-unset}" "${PYTHONPATH-unset}"',
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert probe.stdout.splitlines() == [str(install_home), str(install_bin), str(install_bin), "unset", "unset"]


@pytest.mark.parametrize("product", ["runtime", "both"])
@pytest.mark.parametrize("shell", ["bash", "/bin/bash"])
def test_local_runtime_install_with_no_optional_args(tmp_path: Path, product: str, shell: str) -> None:
    script, env, log = _fixture(tmp_path)
    result = subprocess.run(
        [shell, str(script), "--product", product, "--local"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert "local_runtime.sh|" in calls
    if product == "both":
        assert "local_review.sh|--port 7420" in calls
