from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "local_review.sh"


def _write_helper(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path, Path, Path]:
    engine_archive = tmp_path / "lemoncrow-review-engine.tar.gz"
    engine_archive.write_bytes(b"review-engine-artifact")
    review_archive = tmp_path / "lemoncrow-review-client.tar.gz"
    review_archive.write_bytes(b"review-client-artifact")
    engine_helper = tmp_path / "review_engine.sh"
    review_helper = tmp_path / "hosted.sh"
    server_helper = tmp_path / "local_server.sh"
    engine_log = tmp_path / "engine.log"
    review_log = tmp_path / "review.log"
    server_log = tmp_path / "server.log"

    _write_helper(
        engine_helper,
        r"""
printf 'HOME=%s\n' "${LEMONCROW_HOME:-}" > "$LC_TEST_ENGINE_LOG"
printf 'ARGS=%s\n' "$*" >> "$LC_TEST_ENGINE_LOG"
case " $* " in
  *" --dry-run "*) ;;
  *)
    root="$LEMONCROW_HOME/review/engine/bin"
    mkdir -p "$root"
    printf '#!/bin/sh\nexit 0\n' > "$root/python"
    chmod +x "$root/python"
    ;;
esac
""",
    )
    _write_helper(
        review_helper,
        r"""
printf 'HOME=%s\n' "${LEMONCROW_HOME:-}" > "$LC_TEST_REVIEW_LOG"
printf 'BIN=%s\n' "${LEMONCROW_BIN_DIR:-}" >> "$LC_TEST_REVIEW_LOG"
printf 'ARGS=%s\n' "$*" >> "$LC_TEST_REVIEW_LOG"
case " $* " in
  *" --dry-run "*) ;;
  *)
    mkdir -p "$LEMONCROW_BIN_DIR"
    printf '#!/bin/sh\nexit 0\n' > "$LEMONCROW_BIN_DIR/lc"
    printf '#!/bin/sh\nexit 0\n' > "$LEMONCROW_BIN_DIR/lcr"
    chmod +x "$LEMONCROW_BIN_DIR/lc" "$LEMONCROW_BIN_DIR/lcr"
    ;;
esac
""",
    )
    _write_helper(
        server_helper,
        r"""
printf 'HOME=%s\n' "${LEMONCROW_HOME:-}" >> "$LC_TEST_SERVER_LOG"
printf 'TARGET=%s\n' "${LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT:-}" >> "$LC_TEST_SERVER_LOG"
printf 'PORT=%s\n' "${LEMONCROW_LOCAL_SERVER_PORT:-}" >> "$LC_TEST_SERVER_LOG"
printf 'ARGS=%s\n' "$*" >> "$LC_TEST_SERVER_LOG"
mkdir -p "$LEMONCROW_HOME/server"
printf '%s\n' "${LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT:-}" > "$LEMONCROW_HOME/server/target-product"
""",
    )

    state = tmp_path / "home" / ".lemoncrow"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "LEMONCROW_HOME": str(state),
            "LEMONCROW_BIN_DIR": str(state / "bin"),
            "LEMONCROW_REVIEW_ENGINE_INSTALLER": str(engine_helper),
            "LEMONCROW_REVIEW_INSTALLER": str(review_helper),
            "LEMONCROW_LOCAL_SERVER_SCRIPT": str(server_helper),
            "LC_TEST_ENGINE_LOG": str(engine_log),
            "LC_TEST_REVIEW_LOG": str(review_log),
            "LC_TEST_SERVER_LOG": str(server_log),
        }
    )
    return env, engine_archive, review_archive, engine_log, review_log, server_log


def _build_argv(engine_archive: Path, review_archive: Path, *extra: str) -> list[str]:
    return [
        "bash",
        str(INSTALLER),
        "--from-build",
        "--engine-artifact",
        str(engine_archive),
        "--review-artifact",
        str(review_archive),
        *extra,
    ]


def test_local_review_composes_isolated_engine_client_and_server(tmp_path: Path) -> None:
    env, engine_archive, review_archive, engine_log, review_log, server_log = _fixture(tmp_path)
    result = subprocess.run(
        _build_argv(engine_archive, review_archive, "--port", "7555"),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"ARGS=--from-build {engine_archive}" in engine_log.read_text()
    assert f"ARGS=--url http://127.0.0.1:7555 --from-build {review_archive}" in review_log.read_text()
    server = server_log.read_text()
    assert "TARGET=review" in server
    assert "PORT=7555" in server
    assert "ARGS=stop" in server
    assert "ARGS=start" in server
    state = Path(env["LEMONCROW_HOME"])
    assert (state / "review/engine/bin/python").is_file()
    assert (Path(env["LEMONCROW_BIN_DIR"]) / "lc").is_file()
    assert (Path(env["LEMONCROW_BIN_DIR"]) / "lcr").is_file()
    assert (state / "install/scripts/local_server.sh").is_file()
    assert "runtime:  not installed or enabled by this installer" not in result.stdout
    assert result.stdout.count("endpoint:") == 1
    phases = ["Installing Review engine...", "Installing Review client...", "Starting local Review server..."]
    assert all(phase in result.stdout for phase in phases)
    assert [result.stdout.index(phase) for phase in phases] == sorted(result.stdout.index(phase) for phase in phases)
    assert "add Workspaces: lc runtime enable codex" in result.stdout
    assert "add later: local_runtime.sh" not in result.stdout


def test_nested_install_keeps_progress_without_repeating_summary(tmp_path: Path) -> None:
    env, engine_archive, review_archive, _engine_log, _review_log, _server_log = _fixture(tmp_path)
    result = subprocess.run(
        _build_argv(engine_archive, review_archive),
        cwd=ROOT,
        env={**env, "LEMONCROW_INSTALL_NESTED": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Installing Review engine..." in result.stdout
    assert "Installing Review client..." in result.stdout
    assert "Starting local Review server..." in result.stdout
    assert "Local Review ready" not in result.stdout
    assert "endpoint:" not in result.stdout


def test_local_review_dry_run_propagates_without_starting_server(tmp_path: Path) -> None:
    env, engine_archive, review_archive, engine_log, review_log, server_log = _fixture(tmp_path)
    result = subprocess.run(
        _build_argv(engine_archive, review_archive, "--dry-run"),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--dry-run" in engine_log.read_text()
    assert "--dry-run" in review_log.read_text()
    assert not server_log.exists()
    assert "[dry-run] local server target: review" in result.stdout
    assert not (Path(env["LEMONCROW_BIN_DIR"]) / "lc").exists()


def test_local_review_rejects_invalid_port_before_running_helpers(tmp_path: Path) -> None:
    env, engine_archive, review_archive, engine_log, review_log, server_log = _fixture(tmp_path)
    result = subprocess.run(
        _build_argv(engine_archive, review_archive, "--port", "70000"),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "invalid port" in result.stderr
    assert not engine_log.exists()
    assert not review_log.exists()
    assert not server_log.exists()


def test_local_review_targets_both_when_existing_runtime_is_local(tmp_path: Path) -> None:
    env, engine_archive, review_archive, _engine_log, _review_log, server_log = _fixture(tmp_path)
    state = Path(env["LEMONCROW_HOME"])
    runtime_client = state / "runtime/client/bin/lemoncrow-client"
    runtime_client.parent.mkdir(parents=True)
    runtime_client.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime_client.chmod(0o755)
    (state / "runtime/env").write_text("LEMONCROW_INSTALL_MODE=local\n", encoding="utf-8")

    result = subprocess.run(
        _build_argv(engine_archive, review_archive),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "TARGET=both" in server_log.read_text()


def test_local_review_reinstall_is_idempotent(tmp_path: Path) -> None:
    env, engine_archive, review_archive, _engine_log, _review_log, server_log = _fixture(tmp_path)
    argv = _build_argv(engine_archive, review_archive)
    for _ in range(2):
        result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert (Path(env["LEMONCROW_BIN_DIR"]) / "lc").is_file()
        assert (Path(env["LEMONCROW_BIN_DIR"]) / "lcr").is_file()
    assert "TARGET=review" in server_log.read_text()


def test_local_review_release_mode_downloads_three_helpers_and_cleans_temp(tmp_path: Path) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    script = isolated / "local_review.sh"
    script.write_text(INSTALLER.read_text(encoding="utf-8"), encoding="utf-8")
    script.chmod(0o755)

    fake_engine = tmp_path / "fake-review-engine.sh"
    fake_engine.write_text(
        '#!/bin/sh\nset -eu\nroot="$LEMONCROW_HOME/review/engine/bin"\nmkdir -p "$root"\n'
        'printf \'#!/bin/sh\\nexit 0\\n\' > "$root/python"\nchmod +x "$root/python"\n',
        encoding="utf-8",
    )
    fake_review = tmp_path / "fake-hosted.sh"
    fake_review.write_text(
        '#!/bin/sh\nset -eu\nmkdir -p "$LEMONCROW_BIN_DIR"\n'
        "printf '#!/bin/sh\\nexit 0\\n' > \"$LEMONCROW_BIN_DIR/lc\"\n"
        "printf '#!/bin/sh\\nexit 0\\n' > \"$LEMONCROW_BIN_DIR/lcr\"\n"
        'chmod +x "$LEMONCROW_BIN_DIR/lc" "$LEMONCROW_BIN_DIR/lcr"\n',
        encoding="utf-8",
    )
    fake_server = tmp_path / "fake-local-server.sh"
    fake_server.write_text("#!/bin/sh\nset -eu\nexit 0\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\nset -eu\nout= url=\n"
        'while [ $# -gt 0 ]; do case "$1" in -o) shift; out=$1 ;; http*) url=$1 ;; esac; shift; done\n'
        'case "$url" in '
        '*review_engine.sh) cp "$LC_FAKE_ENGINE" "$out" ;; '
        '*hosted.sh) cp "$LC_FAKE_REVIEW" "$out" ;; '
        '*local_server.sh) cp "$LC_FAKE_SERVER" "$out" ;; '
        "*) exit 3 ;; esac\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    state = tmp_path / "home/.lemoncrow"
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "LEMONCROW_HOME": str(state),
            "LEMONCROW_BIN_DIR": str(state / "bin"),
            "LC_FAKE_ENGINE": str(fake_engine),
            "LC_FAKE_REVIEW": str(fake_review),
            "LC_FAKE_SERVER": str(fake_server),
            "TMPDIR": str(temp_root),
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )

    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert (state / "review/engine/bin/python").is_file()
    assert (state / "bin/lc").is_file()
    assert (state / "bin/lcr").is_file()
    assert (state / "install/scripts/local_server.sh").is_file()
    assert list(temp_root.iterdir()) == []


@pytest.mark.parametrize("shell", ["bash", "/bin/bash"])
def test_release_install_accepts_empty_engine_args(tmp_path: Path, shell: str) -> None:
    # /bin/bash is Bash 3.2 on macOS; an empty array under nounset must
    # contribute zero arguments, while build-mode paths retain their quoting.
    env, _, _, engine_log, review_log, server_log = _fixture(tmp_path)
    result = subprocess.run(
        [shell, str(INSTALLER)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ARGS=\n" in engine_log.read_text()
    assert "ARGS=--url http://127.0.0.1:7420" in review_log.read_text()
    assert "ARGS=start" in server_log.read_text()
