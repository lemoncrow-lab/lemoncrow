"""``lc review config import PATH``: bring another review tool's config over and say what became of each setting (G10).

One path in, ``.lemoncrow/review.yaml`` out. The folder is never found by name (no product is named anywhere in
LemonCrow); pointing at it is the whole discovery, and the command writes the file that makes it found from then on.
The fixtures are tiny and synthetic.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.local_cli import main as engine_main

FOLDER = "team-review"
KEY = f"imported:{FOLDER}/config.json"
CONFIG: dict[str, Any] = {
    "strictness": 3,
    "commentTypes": ["logic", "style"],
    "ignorePatterns": "# generated\ndist/\n*.lock\n!dist/keep.js\n",
    "instructions": "Be direct. Do not comment on trailing commas.",
    "fileChangeLimit": 60,
    "labels": ["skip-review"],
    "frobnicate": True,
    "rules": [
        {
            "id": "no-raw-sql",
            "rule": "Use the query builder, never string-built SQL.",
            "scope": ["src/db/**"],
            "severity": "high",
        },
        {"id": "use-logger", "rule": "Use the logger, not print().", "severity": "low"},
        {"id": "ask-p0", "rule": "Wants the top.", "severity": "critical"},
        {"id": "odd", "rule": "Odd severity.", "severity": "urgent"},
        {"id": "off", "rule": "Switched off.", "enabled": False, "severity": "high"},
    ],
}
RULES_MD = "## Database\n- Every migration must be reversible\n"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    (path / FOLDER).mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / FOLDER / "config.json").write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
    (path / FOLDER / "rules.md").write_text(RULES_MD, encoding="utf-8")
    return path


def _run(tmp_path: Path, repo: Path, *args: str, ok: bool = True) -> Any:
    result = CliRunner().invoke(
        cli, ["--root", str(tmp_path / "root"), "review", "config", "import", *args, "--repo", str(repo)]
    )
    if ok:
        assert result.exit_code == 0, result.output
    return result


def _review_yaml(repo: Path) -> dict[str, Any]:
    document = yaml.safe_load((repo / ".lemoncrow" / "review.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _effective(tmp_path: Path, repo: Path) -> dict[str, Any]:
    result = CliRunner().invoke(
        cli, ["--root", str(tmp_path / "root"), "review", "config", "--effective", "--json", "--repo", str(repo)]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["effective"]  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# the one command
# --------------------------------------------------------------------------- #


def test_one_command_writes_review_yaml_and_the_instructions_file(tmp_path: Path, repo: Path) -> None:
    out = _run(tmp_path, repo, str(repo / FOLDER)).output
    document = _review_yaml(repo)
    assert document["review"]["strictness"] == 3
    assert document["review"]["ignore"] == ["dist/**", "**/dist/**", "*.lock"]
    assert document["review"]["max_files"] == 60
    assert document["review"]["rules"] == {
        f"{KEY}:no-raw-sql": {"severity": "p1"},
        f"{KEY}:use-logger": {"severity": "p2"},
        f"{KEY}:ask-p0": {"severity": "p1"},  # asked for the top, written as p1
    }
    assert document["import"] == {
        "rules_files": [f"{FOLDER}/config.json", f"{FOLDER}/rules.md"],
        "agent_docs_globs": [".lemoncrow/review-instructions.md"],
    }
    brief = (repo / ".lemoncrow" / "review-instructions.md").read_text(encoding="utf-8")
    assert brief == "# Review instructions\n\nBe direct. Do not comment on trailing commas.\n"
    assert "Wrote .lemoncrow/review.yaml" in out and "Wrote .lemoncrow/review-instructions.md" in out
    assert out.startswith("Imported review config from team-review/ (config.json, rules.md)")


def test_the_report_says_what_was_carried_mapped_and_dropped_and_why(tmp_path: Path, repo: Path) -> None:
    out = _run(tmp_path, repo, str(repo / FOLDER)).output
    carried, rest = out.split("Mapped to the closest LemonCrow setting", 1)
    mapped, dropped = rest.split("Not carried", 1)
    assert "Carried over (3)" in carried
    assert "strictness  ->  review.strictness: 3" in carried
    assert "ignorePatterns  ->  review.ignore: 3 globs" in carried
    assert "commentTypes: logic  ->  built in" in carried
    assert "instructions  ->  .lemoncrow/review-instructions.md, listed in import.agent_docs_globs" in mapped
    assert "is not enforced" in mapped
    assert "fileChangeLimit  ->  review.max_files: 60" in mapped
    assert "severity: high (1 rule: no-raw-sql)  ->  review.rules.<key>.severity: p1" in mapped
    assert "Asked for P0, written as p1" in mapped
    assert "no rule severity below P2" in mapped
    # Everything that was not carried says why, and what is closest.
    assert "commentTypes: style" in dropped and "posts no style comments" in dropped
    assert "Negation (!dist/keep.js) cannot be expressed" in dropped
    assert "labels" in dropped and "Closest: branches:, paths-ignore:" in dropped
    assert "frobnicate" in dropped and "LemonCrow does not read this key" in dropped
    assert "severity (1 rule: odd 'urgent')" in dropped
    assert "Rules: 5 read through import.rules_files (config.json, rules.md) as advisory rules" in out
    assert "Next: commit these to your default branch" in out


def test_the_written_config_is_what_a_review_will_use_and_only_the_leftovers_warn(tmp_path: Path, repo: Path) -> None:
    _run(tmp_path, repo, str(repo / FOLDER))
    effective = _effective(tmp_path, repo)
    settings = {row["key"]: row for row in effective["settings"]}
    assert (
        settings["strictness"]["value"].startswith("3") and settings["strictness"]["source"] == ".lemoncrow/review.yaml"
    )
    assert settings["ignore"]["value"] == "dist/**, **/dist/**, *.lock"
    assert settings["max_files"]["value"] == "60"
    assert settings[f"rules.{KEY}:no-raw-sql.severity"]["value"] == "p1"
    assert settings[f"rules.{KEY}:use-logger.severity"]["value"] == "p2"
    rules = {row["key"]: row for row in effective["rules"]}
    assert set(rules) == {
        f"{KEY}:no-raw-sql",
        f"{KEY}:use-logger",
        f"{KEY}:ask-p0",
        f"{KEY}:odd",
        f"{FOLDER}/rules.md".join(["imported:", ":every-migration-must-be-reversible"]),
    }
    assert all(row["severity"] == "advisory" for row in rules.values())  # an import is never a gate by itself
    assert [row["path"] for row in effective["conventions"]] == [".lemoncrow/review-instructions.md"]
    warnings = sorted(row["message"].split(":")[0] for row in effective["notes"] if row["kind"] == "warning")
    # The settings the import carried are not warned about again; what LemonCrow cannot take still is.
    assert warnings == [
        "commentTypes",
        "ignorePatterns",
        "labels",
        "rule 'odd' severity",
        "unknown key 'frobnicate' ignored",
    ]
    carried = {row["message"] for row in effective["notes"] if row["kind"] == "info"}
    assert "strictness: carried by .lemoncrow/review.yaml" in carried
    assert "rule 'no-raw-sql' severity: carried by .lemoncrow/review.yaml" in carried


def test_nothing_is_read_until_a_person_points_at_it(tmp_path: Path, repo: Path) -> None:
    """No discovery by name: the folder is invisible to a review until `import` (or a hand-written pointer) names it."""

    effective = _effective(tmp_path, repo)
    assert effective["rules"] == [] and effective["conventions"] == []
    assert not any(FOLDER in row["path"] for row in effective["sources"])
    _run(tmp_path, repo, str(repo / FOLDER))
    assert any(FOLDER in row["path"] for row in _effective(tmp_path, repo)["sources"])


# --------------------------------------------------------------------------- #
# safe to run: dry run, again, and a file that is already there
# --------------------------------------------------------------------------- #


def test_a_dry_run_prints_the_report_and_writes_nothing(tmp_path: Path, repo: Path) -> None:
    out = _run(tmp_path, repo, str(repo / FOLDER), "--dry-run").output
    assert "Would write .lemoncrow/review.yaml" in out and "Would write .lemoncrow/review-instructions.md" in out
    assert "Next: commit" not in out
    assert not (repo / ".lemoncrow").exists()


def test_running_it_again_changes_nothing(tmp_path: Path, repo: Path) -> None:
    _run(tmp_path, repo, str(repo / FOLDER))
    before = {path.name: path.read_text(encoding="utf-8") for path in (repo / ".lemoncrow").iterdir()}
    out = _run(tmp_path, repo, str(repo / FOLDER)).output
    assert (
        "Already up to date: .lemoncrow/review.yaml" in out
        and "Already up to date: .lemoncrow/review-instructions.md" in out
    )
    assert {path.name: path.read_text(encoding="utf-8") for path in (repo / ".lemoncrow").iterdir()} == before


def test_an_existing_review_yaml_without_those_blocks_keeps_its_text_and_gains_them(tmp_path: Path, repo: Path) -> None:
    existing = (
        "# our policy\nrules:\n  - stable_key: auth-required   # keep this comment\n    instruction: Check identity.\n"
    )
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "review.yaml").write_text(existing, encoding="utf-8")
    out = _run(tmp_path, repo, str(repo / FOLDER)).output
    text = (repo / ".lemoncrow" / "review.yaml").read_text(encoding="utf-8")
    assert text.startswith(existing.rstrip("\n"))  # not a character of it changed, comments included
    document = yaml.safe_load(text)
    assert document["rules"][0]["stable_key"] == "auth-required" and document["review"]["strictness"] == 3
    assert "Added to .lemoncrow/review.yaml" in out


def test_an_existing_review_yaml_with_a_review_block_is_left_alone_and_the_block_is_printed(
    tmp_path: Path, repo: Path
) -> None:
    existing = "review:\n  max_files: 10\n"
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "review.yaml").write_text(existing, encoding="utf-8")
    result = _run(tmp_path, repo, str(repo / FOLDER), ok=False)
    assert result.exit_code == 1
    assert (repo / ".lemoncrow" / "review.yaml").read_text(encoding="utf-8") == existing
    assert not (
        repo / ".lemoncrow" / "review-instructions.md"
    ).exists()  # nothing is written when anything is held back
    assert "Left alone: .lemoncrow/review.yaml" in result.output
    assert "Run again with --force" in result.output and "Or add this to it yourself:" in result.output
    assert "strictness: 3" in result.output  # the block to add by hand


def test_force_merges_into_it_and_your_own_values_win(tmp_path: Path, repo: Path) -> None:
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "review.yaml").write_text(
        "review:\n  max_files: 10\n  finding_threshold: medium\nimport:\n  rules_files: [docs/mine.md]\n  critical: [a]\n",
        encoding="utf-8",
    )
    out = _run(tmp_path, repo, str(repo / FOLDER), "--force").output
    document = _review_yaml(repo)
    assert document["review"]["max_files"] == 10  # yours won
    assert "strictness" not in document["review"] and document["review"]["finding_threshold"] == "medium"
    assert document["review"]["ignore"] == ["dist/**", "**/dist/**", "*.lock"]  # and the rest was added
    assert document["import"]["rules_files"] == ["docs/mine.md", f"{FOLDER}/config.json", f"{FOLDER}/rules.md"]
    assert document["import"]["critical"] == ["a"]
    assert "Kept from your existing file:" in out and "review.max_files: your 10 was kept" in out
    assert "review.finding_threshold: your 'medium' was kept; the import had strictness 3" in out
    assert "Merged into .lemoncrow/review.yaml" in out


def test_a_different_instructions_file_is_not_replaced_without_force(tmp_path: Path, repo: Path) -> None:
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "review-instructions.md").write_text("mine\n", encoding="utf-8")
    result = _run(tmp_path, repo, str(repo / FOLDER), ok=False)
    assert (
        result.exit_code == 1
        and (repo / ".lemoncrow" / "review-instructions.md").read_text(encoding="utf-8") == "mine\n"
    )
    assert not (repo / ".lemoncrow" / "review.yaml").exists()
    assert "Left alone: .lemoncrow/review-instructions.md" in result.output
    _run(tmp_path, repo, str(repo / FOLDER), "--force")
    assert "Be direct" in (repo / ".lemoncrow" / "review-instructions.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# what can be pointed at
# --------------------------------------------------------------------------- #


def test_a_single_settings_file_a_relative_path_and_a_rules_only_folder(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    _run(tmp_path, repo, f"{FOLDER}/config.json")
    assert _review_yaml(repo)["import"]["rules_files"] == [f"{FOLDER}/config.json"]  # no rules.md was named
    other = repo / "markdown-only"
    other.mkdir()
    (other / "rules.md").write_text("- One rule\n", encoding="utf-8")
    (repo / ".lemoncrow" / "review.yaml").unlink()
    out = _run(tmp_path, repo, "markdown-only").output
    assert _review_yaml(repo) == {"import": {"rules_files": ["markdown-only/rules.md"]}}
    assert "No settings were found in it." in out and "Rules: 1 read" in out


def test_a_context_file_list_has_no_counterpart_and_is_reported(tmp_path: Path, repo: Path) -> None:
    (repo / FOLDER / "files.json").write_text(json.dumps({"files": [{"path": "docs/arch.md", "scope": ["src/**"]}]}))
    out = _run(tmp_path, repo, str(repo / FOLDER), "--dry-run").output
    assert "files.json" in out and "keeps no per-file scope or description" in out


def test_the_json_form_carries_the_same_report(tmp_path: Path, repo: Path) -> None:
    document = json.loads(_run(tmp_path, repo, str(repo / FOLDER), "--json", "--dry-run").output)
    assert document["written"] is False and document["dry_run"] is True
    assert document["rules_imported"] == 5 and document["rules_files"] == [
        f"{FOLDER}/config.json",
        f"{FOLDER}/rules.md",
    ]
    assert set(document["summary"]) == {"carried", "mapped", "dropped"} and document["summary"]["dropped"] >= 5
    assert {row["outcome"] for row in document["report"]} == {"carried", "mapped", "dropped"}
    assert {item["path"]: item["action"] for item in document["files"]} == {
        ".lemoncrow/review.yaml": "create",
        ".lemoncrow/review-instructions.md": "create",
    }
    assert "strictness: 3" in document["review_yaml_addition"]


def test_it_refuses_what_it_cannot_read(tmp_path: Path, repo: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "config.json").write_text("{}")
    assert "is outside the repository" in _run(tmp_path, repo, str(outside), ok=False).output
    empty = repo / "empty"
    empty.mkdir()
    assert "holds none of config.json, rules.md or files.json" in _run(tmp_path, repo, str(empty), ok=False).output
    assert "does not exist" in _run(tmp_path, repo, str(repo / "missing"), ok=False).output
    (repo / "notes.txt").write_text("x")
    assert "expected a folder" in _run(tmp_path, repo, str(repo / "notes.txt"), ok=False).output
    (repo / "bad.json").write_text("{not json")
    assert "not valid JSON" in _run(tmp_path, repo, str(repo / "bad.json"), ok=False).output
    assert not (repo / ".lemoncrow").exists()


def test_it_is_reachable_from_the_installed_engine_and_the_help_lists_it(tmp_path: Path, repo: Path) -> None:
    result = CliRunner().invoke(
        engine_main.review_cmd, ["config", "import", str(repo / FOLDER), "--repo", str(repo), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Imported review config from team-review/" in result.output
    helped = CliRunner().invoke(cli, ["--root", str(tmp_path / "root"), "review", "config", "--help"]).output
    assert "import PATH" in helped
    # `config` without `import` is still the view.
    assert CliRunner().invoke(cli, ["--root", str(tmp_path / "root"), "review", "config"]).exit_code == 2
