from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
VERBS = ("dev", "prod", "up", "down", "status", "reset", "ui", "ui-build", "test")


def _make_dry_run(*args: str) -> str:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("LEMONCROW_", "FAST", "OSS_", "HOSTED_", "API_PORT"))
    }
    result = subprocess.run(
        ["make", "-n", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def _targets(prefix: str) -> set[str]:
    return {match for match in re.findall(rf"^{prefix}-([a-z-]+):", MAKEFILE, re.MULTILINE)}


def _install_line(output: str) -> str:
    """The recipe line that runs the staged public installer."""
    lines = output.splitlines()
    index = next(i for i, line in enumerate(lines) if 'bash "dist/release/install.sh"' in line)
    return "\n".join(lines[index - 1 : index + 1])


def test_oss_and_hosted_have_exactly_the_same_verbs() -> None:
    assert _targets("oss") == _targets("hosted") == set(VERBS)


def test_oss_dev_installs_the_staged_release_into_the_oss_sandbox() -> None:
    dev = _make_dry_run("oss-dev")

    assert "FAST=1" in dev
    assert "bash scripts/stage_release.sh" in dev
    install = _install_line(dev)
    assert 'LEMONCROW_RELEASE_BASE_URL="file://' in install and "/dist/release" in install
    assert '.lemoncrow-oss"' in install
    assert 'LEMONCROW_LOCAL_SERVER_PORT="7428"' in install
    assert install.endswith("--product review --local")


def test_hosted_dev_installs_the_staged_release_into_the_hosted_sandbox_against_hosted_up() -> None:
    dev = _make_dry_run("hosted-dev")

    up = dev.index("start hosted")
    install = _install_line(dev)
    assert up < dev.index(install)
    assert "FAST=1" in dev
    assert '.lemoncrow-hosted"' in install
    assert "LEMONCROW_LOCAL_SERVER_PORT" not in install
    assert install.endswith("--product review --cloud --url http://127.0.0.1:3130")
    # The public installer owns verification and the final summary.
    assert dev.rstrip().endswith("--product review --cloud --url http://127.0.0.1:3130")


def test_prod_rebuilds_and_installs_into_the_live_home_on_both_sides() -> None:
    oss = _make_dry_run("oss-prod")
    assert "FAST= " in oss  # never reuse artifacts
    assert 'REHEARSE_HOME="$HOME/.lemoncrow"' in oss
    assert 'REHEARSE_PORT="7420"' in oss
    assert ".lemoncrow-oss" not in oss and "7428" not in oss
    # OSS prod installs Runtime as well as Review, without configuring agent hosts.
    assert _install_line(oss).endswith("--product both --local")
    assert _install_line(_make_dry_run("oss-dev")).endswith("--product review --local")
    assert "systemctl --user restart lemoncrow-mcp-gateway.service" in oss
    assert "systemctl --user is-active --quiet lemoncrow-mcp-gateway.service" in oss
    assert oss.index("--product both --local") < oss.index("systemctl --user restart")

    hosted = _make_dry_run("hosted-prod")
    build = hosted.index("bash scripts/build_review_client.sh")
    artifact = hosted.index("bash scripts/hosted.sh --from-build dist/lemoncrow-review-client.tar.gz")
    client_suite = hosted.index("PYTHONPATH=client/src uv run python -m pytest -q")
    artifact_e2e = hosted.index("LEMONCROW_REVIEW_E2E_PYTHON=")
    install = _install_line(hosted)
    assert build < artifact < client_suite < artifact_e2e < hosted.index(install)
    assert f'LEMONCROW_HOME="{os.environ["HOME"]}/.lemoncrow"' in install
    assert install.endswith("--product review --cloud")
    assert _install_line(_make_dry_run("hosted-prod", "HOSTED_URL=https://lc.example")).endswith(
        "--cloud --url https://lc.example"
    )


@pytest.mark.parametrize(
    ("gateway_state", "expected_calls", "expected_rc"),
    [
        ("configured", ["cat", "restart", "is-active"], 0),
        ("absent", ["cat"], 0),
        ("restart-fails", ["cat", "restart"], 1),
        ("health-fails", ["cat", "restart", "is-active"], 1),
    ],
)
def test_oss_prod_refreshes_configured_gateway_without_touching_unconfigured_one(
    tmp_path: Path, gateway_state: str, expected_calls: list[str], expected_rc: int
) -> None:
    """Use a mocked installer and systemctl: never touch a live install in tests."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$LC_TEST_SYSTEMCTL_LOG"\n'
        'case "$*" in\n'
        '  "--user cat lemoncrow-mcp-gateway.service") [ "$LC_TEST_GATEWAY_STATE" != absent ] ;;\n'
        '  "--user restart lemoncrow-mcp-gateway.service") [ "$LC_TEST_GATEWAY_STATE" != restart-fails ] ;;\n'
        '  "--user is-active --quiet lemoncrow-mcp-gateway.service") [ "$LC_TEST_GATEWAY_STATE" != health-fails ] ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    systemctl.chmod(0o755)
    calls = tmp_path / "systemctl.log"
    result = subprocess.run(
        ["make", "oss-prod", "MAKE=true"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "LC_TEST_SYSTEMCTL_LOG": str(calls),
            "LC_TEST_GATEWAY_STATE": gateway_state,
        },
    )
    assert (result.returncode == 0) is (expected_rc == 0), result.stderr
    assert [line.split()[1] for line in calls.read_text().splitlines()] == expected_calls


@pytest.mark.parametrize("target", ["oss-prod", "hosted-prod"])
def test_production_target_ignores_activated_sandbox_settings(target, tmp_path) -> None:
    sandbox = tmp_path / ".lemoncrow-oss"
    env = {
        **os.environ,
        "LEMONCROW_HOME": str(sandbox),
        "LEMONCROW_BIN_DIR": str(sandbox / "bin"),
        "LEMONCROW_LOCAL_SERVER_PORT": "7428",
    }
    result = subprocess.run(
        ["make", "-n", target],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    install = _install_line(result.stdout)
    live = Path(env["HOME"]) / ".lemoncrow"
    assert f'LEMONCROW_HOME="{live}"' in install
    assert f'LEMONCROW_BIN_DIR="{live}/bin"' in install
    assert str(sandbox) not in install
    if target == "oss-prod":
        assert 'LEMONCROW_LOCAL_SERVER_PORT="7420"' in install


def test_up_down_status_reset_stay_in_the_sandbox() -> None:
    for verb in ("up", "down", "status"):
        output = _make_dry_run(f"oss-{verb}")
        assert '.lemoncrow-oss" LEMONCROW_LOCAL_SERVER_PORT="7428" bash scripts/local_server.sh' in output
    assert "start hosted" in _make_dry_run("hosted-up")
    assert "stop hosted" in _make_dry_run("hosted-down")
    assert "stop all" not in _make_dry_run("hosted-down")
    for side in ("oss", "hosted"):
        reset = _make_dry_run(f"{side}-reset")
        guard = reset.index("not a sandbox")
        assert guard < reset.index("rm -rf")
        assert f'rm -rf "{os.environ["HOME"]}/.lemoncrow-{side}"' in reset


@pytest.mark.parametrize("side", ["oss", "hosted"])
@pytest.mark.parametrize("verb", ["up", "status"])
def test_sandbox_setup_hints_clear_inherited_endpoint_and_pythonpath(side: str, verb: str) -> None:
    output = _make_dry_run(f"{side}-{verb}")
    assert f"Use this sandbox: export LEMONCROW_HOME={os.environ['HOME']}/.lemoncrow-{side}" in output
    assert "unset LEMONCROW_URL PYTHONPATH" in output


def test_ui_ports_never_collide() -> None:
    assert "http://127.0.0.1:7428" in _make_dry_run("oss-ui")
    assert '--port "3131"' in _make_dry_run("hosted-ui")
    assert "npm --prefix frontend run build" in _make_dry_run("oss-ui-build")
    assert "npm --prefix enterprise/frontend run build" in _make_dry_run("hosted-ui-build")


def test_oss_test_isolates_user_home_and_host_overrides(tmp_path: Path) -> None:
    import json

    real_home = tmp_path / "real-user"
    real_home.mkdir()
    config = real_home / ".claude.json"
    config.write_text('{"keep":"real-user-config"}')
    report = tmp_path / "installer-env.json"
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "install.sh").write_text("""#!/bin/sh
python3 - <<'PY'
import json, os
from pathlib import Path
keys = [
    "HOME", "LEMONCROW_HOME", "LEMONCROW_BIN_DIR",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR",
    "PYTHONPATH", "PYTHONHOME", "LEMONCROW_RUNTIME_INSTALL_ROOT",
    "LEMONCROW_REVIEW_INSTALL_ROOT", "LEMONCROW_HOSTED_INSTALL_ROOT", "LEMONCROW_REVIEW_ENGINE_ROOT",
]
Path(os.environ["LC_TEST_ENV_REPORT"]).write_text(json.dumps({key: os.environ.get(key) for key in keys}))
# Model the old package's unsafe write to its caller's host configuration.
(Path.home() / ".claude.json").write_text("temporary runtime path")
PY
""")
    env = {
        **os.environ,
        "HOME": str(real_home),
        "LC_TEST_ENV_REPORT": str(report),
        "PYTHONPATH": str(real_home / "python"),
    }
    for key in (
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "PI_CODING_AGENT_DIR",
        "LEMONCROW_RUNTIME_INSTALL_ROOT",
        "LEMONCROW_REVIEW_INSTALL_ROOT",
        "LEMONCROW_HOSTED_INSTALL_ROOT",
        "LEMONCROW_REVIEW_ENGINE_ROOT",
    ):
        env[key] = str(real_home / key)
    subprocess.run(
        ["make", "oss-test", "MAKE=true", f"RELEASE_STAGE={stage}"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    observed = json.loads(report.read_text())
    assert config.read_text() == '{"keep":"real-user-config"}'
    isolated_root = Path(observed["HOME"]).parent
    assert "lemoncrow-oss-test." in isolated_root.name
    for key, value in observed.items():
        if key in ("PYTHONPATH", "PYTHONHOME") or key.endswith(("_INSTALL_ROOT", "_ENGINE_ROOT")):
            assert value is None, (key, value)
        else:
            assert Path(value).is_relative_to(isolated_root), (key, value)
    assert not isolated_root.exists(), "temporary installation must be cleaned up"


def test_oss_test_installs_both_products_into_a_temp_home_and_cleans_up() -> None:
    output = _make_dry_run("oss-test")

    assert "bash scripts/stage_release.sh" in output
    assert "--product both --local" in output
    assert 'LEMONCROW_LOCAL_SERVER_PORT="7429"' in output
    assert "mktemp -d" in output
    assert "local_server.sh" in output and "stop" in output
    assert "trap cleanup EXIT" in output
    qualify = MAKEFILE.split("\nrelease-qualify:", 1)[1].split("\nrelease:", 1)[0]
    assert "oss-test" in qualify and "hosted-test" in qualify


@pytest.mark.parametrize(
    "target",
    [
        "dev",
        "prod",
        "ui",
        "hosted",
        "hosted-build",
        "frontend-build",
        "dev-up",
        "dev-down",
        "dev-status",
        "server-start",
        "server-stop",
        "server-restart",
        "server-status",
        "release-rehearse",
        "_local-install",
        "release/build",
        "ui-build",
        "ui-dev",
        "local-review",
        "local-runtime",
        "start",
        "stop",
        "restart",
        "status",
    ],
)
def test_removed_make_targets_are_gone(target: str) -> None:
    assert re.search(rf"^{re.escape(target)}:", MAKEFILE, re.MULTILINE) is None
    assert target not in MAKEFILE.split(".PHONY:", 1)[1].split("\n\n", 1)[0].split()


def test_release_stage_replaces_only_its_own_assets(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ("lemoncrow-review-client", "lemoncrow-review-engine", "lemoncrow-runtime-client"):
        for suffix in (".tar.gz", ".tar.gz.sha256"):
            (dist / f"{name}{suffix}").write_text("fixture")
    stage = tmp_path / "stage"
    command = ["bash", str(ROOT / "scripts/stage_release.sh"), str(stage), str(dist)]
    subprocess.run(command, check=True, capture_output=True)
    subprocess.run(command, check=True, capture_output=True)
    sentinel = stage / "unrelated.txt"
    sentinel.write_text("keep")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2
    assert sentinel.read_text() == "keep"
    assert "refusing to replace unrelated output" in result.stderr


@pytest.mark.parametrize(
    ("color_env", "colored"),
    [
        ({}, False),
        ({"FORCE_COLOR": "1"}, True),
        ({"CLICOLOR_FORCE": "1"}, True),
        ({"FORCE_COLOR": "0"}, False),
        ({"FORCE_COLOR": "1", "NO_COLOR": "1"}, False),
        ({"FORCE_COLOR": "1", "TERM": "dumb"}, True),
        ({"TERM": "dumb"}, False),
    ],
)
def test_terminal_messages_respect_color_environment(color_env: dict[str, str], colored: bool) -> None:
    env = {k: v for k, v in os.environ.items() if k not in {"FORCE_COLOR", "CLICOLOR_FORCE", "NO_COLOR", "TERM"}}
    env.update(color_env)
    result = subprocess.run(
        ["sh", str(ROOT / "scripts/lib/terminal.sh"), "step", "Build assets"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    assert ("\x1b[" in result.stdout) is colored
    assert "◆  Build assets" in result.stdout
    assert re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).endswith("Build assets\n")


def test_terminal_messages_automatically_color_a_terminal() -> None:
    import pty

    master, slave = pty.openpty()
    env = {k: v for k, v in os.environ.items() if k not in {"FORCE_COLOR", "CLICOLOR_FORCE", "NO_COLOR"}}
    env["TERM"] = "xterm"
    try:
        subprocess.run(
            ["sh", str(ROOT / "scripts/lib/terminal.sh"), "success", "Ready"],
            stdout=slave,
            check=True,
            env=env,
        )
        assert b"\x1b[32m\xe2\x9c\x93\x1b[0m  Ready" in os.read(master, 4096)
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    "script", ["install", "hosted", "review_engine", "local_review", "local_server", "runtime", "local_runtime"]
)
@pytest.mark.parametrize("disabled", [False, True])
def test_standalone_helpers_color_errors_without_changing_exit_status(script, disabled, tmp_path) -> None:
    # Published helpers must also work alone, without the checkout's lib directory.
    standalone = tmp_path / f"{script}.sh"
    standalone.write_bytes((ROOT / "scripts" / f"{script}.sh").read_bytes())
    env = {**os.environ, "FORCE_COLOR": "1", "NO_COLOR": "1" if disabled else ""}
    result = subprocess.run(["bash", str(standalone), "--not-a-real-option"], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert ("\x1b[1;31m" in result.stderr) is not disabled
    assert "unknown option" in result.stderr or "usage:" in result.stderr


@pytest.mark.parametrize("script", ["hosted", "runtime", "review_engine"])
def test_installer_plan_colors_preserve_the_plan_text(script) -> None:
    env = {**os.environ, "FORCE_COLOR": "1", "NO_COLOR": ""}
    command = ["bash", str(ROOT / "scripts" / f"{script}.sh"), "--dry-run"]
    colored = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    plain = subprocess.run(command, env={**env, "NO_COLOR": "1"}, capture_output=True, text=True, check=True)
    assert "\x1b[" in colored.stdout
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", colored.stdout).replace("│     ", "")
    assert cleaned == plain.stdout
    assert "\x1b[" not in plain.stdout


@pytest.mark.parametrize(("status", "verbose"), [(0, False), (37, False), (0, True)])
def test_build_runner_hides_success_and_preserves_failure_diagnostics(tmp_path, status, verbose) -> None:
    env = {
        **os.environ,
        "TMPDIR": str(tmp_path),
        "NO_COLOR": "1",
        "LEMONCROW_VERBOSE": "1" if verbose else "0",
    }
    command = [
        "sh",
        "-c",
        '. "$1"; lc_run sh -c \'echo stdout-detail; echo stderr-detail >&2; exit "$1"\' child "$2"',
        "runner",
        str(ROOT / "scripts/lib/terminal.sh"),
        str(status),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == status
    if status:
        assert "stdout-detail" in result.stderr
        assert "stderr-detail" in result.stderr
        assert "Command failed:" in result.stderr
    elif verbose:
        assert "stdout-detail" in result.stdout
        assert "stderr-detail" in result.stderr
    else:
        assert not result.stdout
        assert not result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("formatter", ["scripts/lib/terminal.sh", "scripts/install.sh"])
def test_terminal_rows_share_marker_and_text_columns(formatter) -> None:
    env = {**os.environ, "FORCE_COLOR": "1", "NO_COLOR": ""}
    script = """
formatter=$1
set -- --help
. "$formatter" >/dev/null
lc_ui_printf '1;36' '\\n◆ Install\\n'
lc_ui_printf progress 'Installing client...\\n'
lc_ui_printf '1;32' '✓ Ready\\n'
lc_ui_printf '36' '  Endpoint: localhost\\n'
lc_ui_printf '1;36' '\\nNext:\\n'
lc_ui_printf command '  Open Review:' 'lcr'
"""
    result = subprocess.run(
        ["bash", "-c", script, "formatter-test", str(ROOT / formatter)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    rows = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).splitlines()
    rows = [row for row in rows if row != "│"]
    for index, (row, text) in enumerate(
        zip(rows, ["Install", "Installing client...", "Ready", "Endpoint:", "Next:", "Open Review:"], strict=True)
    ):
        if index in (0, 4):
            assert row.startswith("◆")
            assert row.index(text) == 3
        else:
            assert row.startswith("│")
            assert row.index(text) == 6
    assert rows[0][0] == rows[4][0] == "◆"
    assert rows[1][3] == "◇"
    assert rows[2][3] == "✓"


def test_verification_reports_progress_and_server_failure(tmp_path) -> None:
    home = tmp_path / "home"
    bin_dir = home / "bin"
    helper_dir = home / "install/scripts"
    bin_dir.mkdir(parents=True)
    helper_dir.mkdir(parents=True)
    for name in ("lc", "lcr"):
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\necho internal-help-or-version\n")
        executable.chmod(0o755)
    helper = helper_dir / "local_server.sh"
    helper.write_text(
        "#!/bin/sh\n"
        + 'if [ "$1" = status ]; then exit 1; fi\n'
        + 'echo "server diagnostics: port already occupied" >&2\n'
    )
    script = """
installer=$1
home_hint=$2
bin_hint=$3
set -- --help
. "$installer" >/dev/null
product=review
deployment=local
port=7555
preview=0
_public_verify_install
"""
    result = subprocess.run(
        ["bash", "-c", script, "verify-fixture", str(ROOT / "scripts/install.sh"), str(home), str(bin_dir)],
        env={**os.environ, "NO_COLOR": "1", "LEMONCROW_LOCAL_SERVER_START_TIMEOUT": "1"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    for message in (
        "Checking lc",
        "Checking lcr",
        "Checking local Review server on port 7555",
        "Waiting for Review server",
    ):
        assert message in result.stdout
    assert "did not remain healthy" in result.stderr
    assert "server diagnostics: port already occupied" in result.stderr
    assert "internal-help-or-version" not in result.stdout


def test_make_help_uses_terminal_formatter() -> None:
    env = {**os.environ, "FORCE_COLOR": "1", "NO_COLOR": ""}
    result = subprocess.run(["make", "help"], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    assert "\x1b[38;2;155;117;217m◆  LemonCrow" in result.stdout


def test_release_stage_uses_the_single_asset_list_and_release_workflow_matches() -> None:
    assert 'bash scripts/stage_release.sh "dist/release"' in _make_dry_run("release-stage")
    # The private monorepo keeps the workflow under public-workflows/; the public mirror publishes it as workflows/.
    candidates = [ROOT / ".github/public-workflows/release.yml", ROOT / ".github/workflows/release.yml"]
    workflow_path = next((path for path in candidates if path.is_file()), None)
    if workflow_path is None:
        pytest.skip("release workflow not present")
    workflow = workflow_path.read_text(encoding="utf-8")
    assert "scripts/stage_release.sh" in workflow
    assert "scripts/install.sh scripts/hosted.sh" not in workflow
    assert "install-rehearsal" in workflow
    assert "needs: [hosted-client-build, install-rehearsal, binary-build, upgrade-smoke]" in workflow
