from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "local_runtime.sh"


def _write_helper(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    runtime_archive = tmp_path / "lemoncrow-runtime-client.tar.gz"
    runtime_archive.write_bytes(b"runtime-artifact")
    runtime = tmp_path / "runtime.sh"
    runtime_log = tmp_path / "runtime.log"
    forbidden_engine_log = tmp_path / "engine.log"

    _write_helper(
        runtime,
        r"""
printf 'ARGS=%s\n' "$*" > "$LC_TEST_RUNTIME_LOG"
case " $* " in
  *" --dry-run "*) ;;
  *)
    root="$LEMONCROW_HOME/runtime/client/bin"
    mkdir -p "$root" "$LEMONCROW_BIN_DIR" "$LEMONCROW_HOME/runtime"
    printf '#!/bin/sh\nexit 0\n' > "$root/lemoncrow-client"
    printf '#!/bin/sh\nexit 0\n' > "$root/lemoncrow-runtime-client"
    chmod +x "$root/lemoncrow-client" "$root/lemoncrow-runtime-client"
    ln -sfn "$root/lemoncrow-runtime-client" "$LEMONCROW_BIN_DIR/lemoncrow-runtime-client"
    printf 'LEMONCROW_INSTALL_MODE=lite\n' > "$LEMONCROW_HOME/runtime/env"
    ;;
esac
""",
    )
    forbidden_engine = tmp_path / "install.sh"
    _write_helper(forbidden_engine, 'printf "CALLED\n" > "$LC_TEST_ENGINE_LOG"; exit 91\n')

    state = tmp_path / "home" / ".lemoncrow"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "LEMONCROW_HOME": str(state),
            "LEMONCROW_BIN_DIR": str(state / "bin"),
            "LEMONCROW_RUNTIME_INSTALLER": str(runtime),
            "LEMONCROW_LOCAL_ENGINE_INSTALLER": str(forbidden_engine),
            "LC_TEST_ENGINE_LOG": str(forbidden_engine_log),
            "LC_TEST_RUNTIME_LOG": str(runtime_log),
        }
    )
    return env, runtime_archive, runtime_log, forbidden_engine_log


def _run(env: dict[str, str], archive: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), "--from-build", str(archive), *extra],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_local_runtime_is_serverless_lite_and_never_calls_full_engine(tmp_path: Path) -> None:
    env, runtime_archive, runtime_log, engine_log = _fixture(tmp_path)

    result = _run(env, runtime_archive, "--port", "7666")

    assert result.returncode == 0, result.stderr
    assert runtime_log.read_text().strip() == f"ARGS=--lite --from-build {runtime_archive}"
    assert not engine_log.exists()
    state = Path(env["LEMONCROW_HOME"])
    bin_dir = Path(env["LEMONCROW_BIN_DIR"])
    assert (bin_dir / "lc").resolve() == (state / "runtime/client/bin/lemoncrow-client").resolve()
    assert not (bin_dir / "lcr").exists()
    assert "Workspaces Lite ready" in result.stdout
    assert (state / "runtime/env").read_text() == "LEMONCROW_INSTALL_MODE=lite\n"


def test_local_runtime_preserves_review_launcher_and_forwards_agent(tmp_path: Path) -> None:
    env, runtime_archive, runtime_log, _engine_log = _fixture(tmp_path)
    state = Path(env["LEMONCROW_HOME"])
    review_bin = state / "review/client/bin"
    review_bin.mkdir(parents=True)
    for name in ("lc", "lcr"):
        path = review_bin / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    result = _run(env, runtime_archive, "--agent", "codex")

    assert result.returncode == 0, result.stderr
    assert runtime_log.read_text().strip() == f"ARGS=--lite --from-build {runtime_archive} --agent codex"
    bin_dir = Path(env["LEMONCROW_BIN_DIR"])
    assert (bin_dir / "lc").resolve() == (review_bin / "lc").resolve()
    assert (bin_dir / "lcr").resolve() == (review_bin / "lcr").resolve()
    assert "review:   preserved" in result.stdout
    assert (
        result.stdout.index("Verify installation")
        < result.stdout.index("Workspaces Lite ready")
        < result.stdout.index("Next:")
    )


def test_local_runtime_dry_run_is_lite_and_needs_no_server(tmp_path: Path) -> None:
    env, runtime_archive, runtime_log, engine_log = _fixture(tmp_path)

    result = _run(env, runtime_archive, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert runtime_log.read_text().strip() == f"ARGS=--lite --from-build {runtime_archive} --dry-run"
    assert not engine_log.exists()


def test_local_runtime_rejects_unknown_agent_before_running_helper(tmp_path: Path) -> None:
    env, runtime_archive, runtime_log, engine_log = _fixture(tmp_path)

    result = _run(env, runtime_archive, "--agent", "unknown")

    assert result.returncode == 2
    assert "unsupported --agent" in result.stderr
    assert not runtime_log.exists()
    assert not engine_log.exists()


def test_local_runtime_migrates_legacy_server_runtime_before_installing_lite(tmp_path: Path) -> None:
    env, runtime_archive, runtime_log, engine_log = _fixture(tmp_path)
    state = Path(env["LEMONCROW_HOME"])
    (state / "runtime").mkdir(parents=True)
    (state / "runtime/env").write_text(
        "LEMONCROW_URL=http://127.0.0.1:7420\nLEMONCROW_INSTALL_MODE=local\n",
        encoding="utf-8",
    )
    uninstall = state / "install/scripts/uninstall_runtime.sh"
    uninstall.parent.mkdir(parents=True)
    migration_log = tmp_path / "migration.log"
    _write_helper(
        uninstall,
        'printf "%s\n" "$*" > "$LC_MIGRATION_LOG"; rm -rf "$LEMONCROW_HOME/runtime"; rm -f "$LEMONCROW_BIN_DIR/lc" "$LEMONCROW_BIN_DIR/lcr"\n',
    )
    env["LC_MIGRATION_LOG"] = str(migration_log)

    result = _run(env, runtime_archive)

    assert result.returncode == 0, result.stderr
    assert migration_log.read_text().strip() == "--keep-hosts"
    assert "Migrating the legacy server-backed install to Workspaces Lite" in result.stdout
    assert runtime_log.exists()
    assert not engine_log.exists()
    assert (state / "runtime/env").read_text() == "LEMONCROW_INSTALL_MODE=lite\n"


def test_local_runtime_fetches_legacy_uninstaller_from_release_when_none_is_local(tmp_path: Path) -> None:
    env, runtime_archive, runtime_log, _engine_log = _fixture(tmp_path)
    state = Path(env["LEMONCROW_HOME"])
    (state / "runtime").mkdir(parents=True)
    (state / "runtime/env").write_text("LEMONCROW_INSTALL_MODE=local\n", encoding="utf-8")
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    script = isolated / "local_runtime.sh"
    script.write_text(INSTALLER.read_text(encoding="utf-8"), encoding="utf-8")
    release = tmp_path / "release"
    release.mkdir()
    migration_log = tmp_path / "migration.log"
    _write_helper(
        release / "uninstall_runtime.sh",
        'printf "%s\n" "$*" > "$LC_MIGRATION_LOG"; rm -rf "$LEMONCROW_HOME/runtime"\n',
    )
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    env.update(
        {
            "LC_MIGRATION_LOG": str(migration_log),
            "LEMONCROW_RELEASE_BASE_URL": release.as_uri(),
            "TMPDIR": str(temp_root),
        }
    )

    result = subprocess.run(
        ["bash", str(script), "--from-build", str(runtime_archive)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert migration_log.read_text().strip() == "--keep-hosts"
    assert runtime_log.exists()
    assert list(temp_root.iterdir()) == []


def test_local_runtime_release_mode_downloads_only_runtime_helper(tmp_path: Path) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    script = isolated / "local_runtime.sh"
    script.write_text(INSTALLER.read_text(encoding="utf-8"), encoding="utf-8")
    script.chmod(0o755)

    fake_runtime = tmp_path / "fake-runtime.sh"
    fake_runtime.write_text(
        '#!/bin/sh\nset -eu\nroot="$LEMONCROW_HOME/runtime/client/bin"\n'
        'mkdir -p "$root" "$LEMONCROW_BIN_DIR" "$LEMONCROW_HOME/runtime"\n'
        "printf '#!/bin/sh\\nexit 0\\n' > \"$root/lemoncrow-client\"\n"
        "printf '#!/bin/sh\\nexit 0\\n' > \"$root/lemoncrow-runtime-client\"\n"
        'chmod +x "$root/lemoncrow-client" "$root/lemoncrow-runtime-client"\n'
        'printf "LEMONCROW_INSTALL_MODE=lite\\n" > "$LEMONCROW_HOME/runtime/env"\n',
        encoding="utf-8",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\nset -eu\nout= url=\n"
        'while [ $# -gt 0 ]; do case "$1" in -o) shift; out=$1 ;; http*) url=$1 ;; esac; shift; done\n'
        'case "$url" in *runtime.sh) cp "$LC_FAKE_RUNTIME" "$out" ;; *) echo "unexpected:$url" >&2; exit 3 ;; esac\n',
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
            "LC_FAKE_RUNTIME": str(fake_runtime),
            "TMPDIR": str(temp_root),
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )

    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert (state / "bin/lc").exists()
    assert not (state / "bin/lcr").exists()
    assert list(temp_root.iterdir()) == []


def test_runtime_lite_installers_never_start_the_dashboard() -> None:
    # The Dashboard runs on the optional local server, started on demand; the
    # Lite installers never install, start or target that server.
    # runtime.sh may only copy local_server.sh into the persisted uninstall
    # suite (so `lc uninstall all` can stop a server another product started).
    suite_line = "    for name in uninstall_product.sh uninstall_review.sh uninstall_runtime.sh uninstall.sh local_server.sh; do\n"
    for name in ("runtime.sh", "local_runtime.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8").replace(suite_line, "")
        assert "local_server.sh" not in text, name
        assert "lemoncrow_server_core" not in text, name
