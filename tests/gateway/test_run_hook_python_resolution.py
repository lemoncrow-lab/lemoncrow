"""_run_hook.sh: the cached hook-interpreter resolution must self-heal when a
reinstall (make dev / make prod / install.sh) moves `lc` to a new venv.

Regression for a real bug: the cache used to be keyed only on "does `import
lemoncrow` still succeed", which a stale-but-still-valid old install always
satisfies -- so once cached, hook scripts kept running under an old
interpreter forever, even after a fresh reinstall put a newer one on PATH
(surfaced as an ImportError deep in a hook script for a symbol only the new
version has).
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

SCRIPT = Path("integrations/claude/plugin/hooks/_run_hook.sh").resolve()


def _make_fake_install(root: Path, marker: str) -> Path:
    """A fake lemoncrow venv: `bin/python` understands just enough to satisfy
    `_run_hook.sh` -- `-c "import lemoncrow"` succeeds (a marker file stands in
    for a real install), otherwise it prints which install ran it and exits.
    `bin/lemoncrow` is a wrapper script whose shebang IS that python, matching
    the "modern uv tool wrapper" shape `_run_hook.sh` looks for first.
    """
    bin_dir = root / marker / "bin"
    bin_dir.mkdir(parents=True)
    python_path = bin_dir / "python"
    python_path.write_text(
        f'#!/usr/bin/env bash\nif [[ "$1" == "-c" ]]; then exit 0; fi\necho "RAN:{marker}"\n',
        encoding="utf-8",
    )
    python_path.chmod(python_path.stat().st_mode | stat.S_IEXEC)

    lemoncrow_path = bin_dir / "lc"
    lemoncrow_path.write_text(f"#!{python_path}\n", encoding="utf-8")
    lemoncrow_path.chmod(lemoncrow_path.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run(bin_dir: Path, home: Path, hook: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }
    return subprocess.run(["bash", str(SCRIPT), str(hook)], capture_output=True, text=True, env=env, check=False)


def test_switches_interpreter_after_reinstall_moves_lemoncrow(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    hook = tmp_path / "dummy_hook.py"
    hook.write_text("", encoding="utf-8")

    old_bin = _make_fake_install(tmp_path / "installs", "old")
    new_bin = _make_fake_install(tmp_path / "installs", "new")

    # First run: only the old install is on PATH -- resolves + caches it.
    result = _run(old_bin, home, hook)
    assert result.returncode == 0, result.stderr
    assert "RAN:old" in result.stdout

    # Reinstall happened: PATH now points at a new install. The old install is
    # untouched (still on disk, `import lemoncrow` on it would still succeed --
    # this is the exact condition the old cache logic got wrong).
    result = _run(new_bin, home, hook)
    assert result.returncode == 0, result.stderr
    assert (
        "RAN:new" in result.stdout
    ), f"stale interpreter cache was reused after lc moved on PATH (stdout={result.stdout!r})"

    # Third run with the new install still on PATH: cache now matches new, no
    # flip-flopping.
    result = _run(new_bin, home, hook)
    assert result.returncode == 0, result.stderr
    assert "RAN:new" in result.stdout


def test_reuses_cache_when_lemoncrow_location_unchanged(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    hook = tmp_path / "dummy_hook.py"
    hook.write_text("", encoding="utf-8")
    bin_dir = _make_fake_install(tmp_path / "installs", "only")

    first = _run(bin_dir, home, hook)
    second = _run(bin_dir, home, hook)
    assert first.returncode == 0 and second.returncode == 0
    assert "RAN:only" in first.stdout
    assert "RAN:only" in second.stdout

    cache_file = home / ".cache" / "lemoncrow" / "hook_python"
    assert cache_file.exists()
    lines = cache_file.read_text(encoding="utf-8").splitlines()
    assert str(bin_dir / "python") == lines[0]
