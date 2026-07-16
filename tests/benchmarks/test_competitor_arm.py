"""Tests for BYO-competitor arm support (benchmarks/codebench/competitor.py).

Covers manifest parsing/validation and the clone+install+resolve pipeline against
a real *local* git repo (offline -- no network), including ``${CLONE}`` expansion,
single-server MCP wrapping, and install idempotency.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.codebench import competitor as comp  # noqa: E402


def _write_manifest(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text("# Rival skill\nBe concise.\n", encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, env={**env})
    return path


# --- manifest parsing ------------------------------------------------------


def test_load_spec_minimal(tmp_path: Path) -> None:
    m = _write_manifest(tmp_path / "c.json", {"name": "rival", "repo": "https://x/y"})
    spec = comp.load_competitor_spec(m)
    assert spec.name == "rival"
    assert spec.repo == "https://x/y"
    assert spec.install == ()
    assert spec.mcp is None


def test_load_spec_full(tmp_path: Path) -> None:
    m = _write_manifest(
        tmp_path / "c.json",
        {
            "name": "rival-tool",
            "repo": "https://x/y",
            "ref": "v1.2.3",
            "install": ["npm ci", "npm run build"],
            "mcp": {"command": "node", "args": ["${CLONE}/dist/server.js"]},
            "agent": "rival:main",
            "env": {"RIVAL_HOME": "${CLONE}"},
        },
    )
    spec = comp.load_competitor_spec(m)
    assert spec.install == ("npm ci", "npm run build")
    assert spec.ref == "v1.2.3"
    assert spec.agent == "rival:main"
    assert spec.env == {"RIVAL_HOME": "${CLONE}"}


def test_load_spec_install_scalar(tmp_path: Path) -> None:
    m = _write_manifest(tmp_path / "c.json", {"name": "r", "repo": "u", "install": "make"})
    assert comp.load_competitor_spec(m).install == ("make",)


@pytest.mark.parametrize(
    "data, msg",
    [
        ({"repo": "u"}, "missing 'name'"),
        ({"name": "r"}, "missing 'repo'"),
        ({"name": "bad name", "repo": "u"}, "safe arm token"),
        ({"name": "baseline", "repo": "u"}, "built-in arm"),
        ({"name": "lemoncrow", "repo": "u"}, "built-in arm"),
    ],
)
def test_load_spec_rejects_bad(tmp_path: Path, data: dict, msg: str) -> None:
    m = _write_manifest(tmp_path / "c.json", data)
    with pytest.raises(ValueError, match=msg):
        comp.load_competitor_spec(m)


def test_load_spec_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        comp.load_competitor_spec(p)


# --- prepare (clone + install + resolve) -----------------------------------


def test_prepare_clone_install_resolve(tmp_path: Path) -> None:
    src = _make_git_repo(tmp_path / "src")
    root = tmp_path / "cache"
    spec = comp.CompetitorSpec(
        name="rival",
        repo=str(src),
        install=("echo hi > ${CLONE}/installed.txt",),
        mcp={"command": "node", "args": ["${CLONE}/server.js"]},
        plugin_dir="${CLONE}",
        skill_file="${CLONE}/SKILL.md",
        agent="rival:main",
        env={"HOME_DIR": "${CLONE}/x"},
    )
    prepared = comp.prepare_competitor(spec, root)
    clone = root / "rival"
    assert (clone / ".git").is_dir()
    # install ran and its ${CLONE} expanded
    assert (clone / "installed.txt").is_file()
    # single-server mcp got wrapped under the arm name, ${CLONE} expanded absolute
    assert prepared.mcp_config == {"mcpServers": {"rival": {"command": "node", "args": [f"{clone}/server.js"]}}}
    assert prepared.plugin_dir == str(clone)
    assert prepared.system_prompt is not None and "Be concise." in prepared.system_prompt
    assert prepared.agent == "rival:main"
    assert prepared.env == {"HOME_DIR": f"{clone}/x"}


def test_prepare_is_idempotent(tmp_path: Path) -> None:
    src = _make_git_repo(tmp_path / "src")
    root = tmp_path / "cache"
    calls: list[str] = []
    real_run = subprocess.run

    def counting_run(cmd, *a, **k):  # type: ignore[no-untyped-def]
        calls.append(" ".join(cmd) if isinstance(cmd, list) else str(cmd))
        return real_run(cmd, *a, **k)

    spec = comp.CompetitorSpec(name="rival", repo=str(src), install=("true",))
    comp.prepare_competitor(spec, root, runner=counting_run)
    first = list(calls)
    calls.clear()
    comp.prepare_competitor(spec, root, runner=counting_run)
    # second run: repo already cloned + install marker present -> no clone, no install
    assert any("git clone" in c for c in first)
    assert not any("git clone" in c for c in calls)
    assert not any(c == "true" for c in calls)


def test_prepare_full_mcp_config_passthrough(tmp_path: Path) -> None:
    src = _make_git_repo(tmp_path / "src")
    root = tmp_path / "cache"
    spec = comp.CompetitorSpec(
        name="rival",
        repo=str(src),
        mcp={"mcpServers": {"custom": {"command": "x", "args": ["${CLONE}/a"]}}},
    )
    prepared = comp.prepare_competitor(spec, root)
    clone = root / "rival"
    assert prepared.mcp_config == {"mcpServers": {"custom": {"command": "x", "args": [f"{clone}/a"]}}}


def test_prepare_missing_skill_file_errors(tmp_path: Path) -> None:
    src = _make_git_repo(tmp_path / "src")
    spec = comp.CompetitorSpec(name="rival", repo=str(src), skill_file="${CLONE}/nope.md")
    with pytest.raises(RuntimeError, match="skill_file not found"):
        comp.prepare_competitor(spec, tmp_path / "cache")


def test_prepared_maps_to_armspec_fields(tmp_path: Path) -> None:
    """A PreparedCompetitor's fields line up with the ArmSpec the runner builds."""
    from benchmarks.codebench.run import ArmSpec

    src = _make_git_repo(tmp_path / "src")
    spec = comp.CompetitorSpec(name="rival", repo=str(src), skill_file="${CLONE}/SKILL.md", agent="rival:main")
    prepared = comp.prepare_competitor(spec, tmp_path / "cache")
    arm = ArmSpec(
        {"code": prepared.agent},
        strip_mcp=True,
        heavy=True,
        mcp_config=prepared.mcp_config,
        competitor_plugin_dir=prepared.plugin_dir,
        append_system_prompt=prepared.system_prompt,
        competitor_env=prepared.env or None,
    )
    assert arm.persona_by_capability == {"code": "rival:main"}
    assert arm.append_system_prompt is not None
    assert arm.plugin is False  # competitor never triggers LemonCrow pre-index / MCP
