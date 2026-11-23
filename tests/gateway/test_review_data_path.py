"""P10-7: Review's experimental data path (``review/data_path.py``).

Rules, the separate engine pin, finding and verifying the engine, staging, every cap at and past its bound, the
(source, sink) delta, the honesty fields, the stored report and its Review API wiring, and a real-binary run over
the fixture repositories in ``tests/fixtures/data_path`` (skipped, with the reason, without the pinned binary).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review import data_path as dp
from lemoncrow.pro.capabilities.review.pr_bot import opengrep_install
from lemoncrow.pro.capabilities.review.pr_bot.security import BIN_ENV as SCAN_BIN_ENV
from lemoncrow.pro.capabilities.review.pr_bot.security import ProcessResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "data_path"
#: A pinned platform; tests point its pin at a fake engine script so no real binary is needed.
SYSTEM, MACHINE = "Darwin", "arm64"
DJANGO = "from django.http import JsonResponse\n"
# 1 import, 4 def, 5 read, 6 write
VIEWS = DJANGO + "\n\ndef create(request):\n    name = request.POST['name']\n    Widget.objects.create(name=name)\n"
NEVER = ("no flow changes", "no flows changed", "no data flow changes", "no changes")


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


# --------------------------------------------------------------------------- #
# helpers: a fake pinned engine, a fake runner that writes the engine's JSON
# --------------------------------------------------------------------------- #


@pytest.fixture
def engine_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """An environment naming a fake ``opengrep`` whose digest is (for this test) the v2.0.0 pin."""

    script = tmp_path / "bin" / "opengrep"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho 2.0.0\n", encoding="utf-8")
    script.chmod(0o755)
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    key = opengrep_install.platform_key(SYSTEM, MACHINE)
    monkeypatch.setitem(opengrep_install.DATA_PATH_PINS, key, opengrep_install.Pin("opengrep_osx_arm64", digest))
    # The fake runner never executes the command: the sandbox wrapping is left out so the engine's own arguments
    # are what the tests read (test_contained_refuses_anything_but_a_sandbox covers the wrapping).
    monkeypatch.setattr(dp, "contained", lambda args, _isolation, **_kwargs: list(args))
    return {"PATH": "/usr/bin:/bin", dp.BIN_ENV: str(script), "GITHUB_TOKEN": "secret-token"}


class FakeRunner:
    """Stands in for ``security.run_process``: records the call and writes the ``--json-output`` document."""

    def __init__(
        self,
        documents: Callable[[str, str], Any] | None = None,
        *,
        returncode: int | Callable[[str], int] = 0,
        error: BaseException | None = None,
        on_run: Callable[[str], None] | None = None,
        stderr: str = "",
    ) -> None:
        self.documents = documents or (lambda _side, _framework: {"results": [], "errors": []})
        self.returncode = returncode
        self.stderr = stderr
        self.error = error
        self.on_run = on_run
        self.calls: list[SimpleNamespace] = []

    def __call__(self, args: Any, cwd: Path, env: Mapping[str, str], timeout: float) -> ProcessResult:
        argv = list(args)
        side = Path(cwd).name
        framework = Path(argv[argv.index("--config") + 1]).stem
        self.calls.append(
            SimpleNamespace(side=side, framework=framework, args=argv, cwd=Path(cwd), env=dict(env), timeout=timeout)
        )
        if self.on_run is not None:
            self.on_run(side)
        if self.error is not None:
            raise self.error
        output = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--json-output=")))
        document = self.documents(side, framework)
        if isinstance(document, dict) and "paths" not in document:
            # As the engine does: every file of the rules' languages under its working directory was scanned.
            suffixes = dp._BY_ID[framework].suffixes
            scanned = [rel for rel in _files(Path(cwd)) if Path(rel).suffix in suffixes]
            document = {**document, "paths": {"scanned": scanned}}
        if document is not None:
            output.write_bytes(document if isinstance(document, bytes) else json.dumps(document).encode())
        code = self.returncode(side) if callable(self.returncode) else self.returncode
        return ProcessResult(code, self.stderr)


def _write(root: Path, files: Mapping[str, str | bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
    return root


def _run(
    tmp_path: Path,
    env: Mapping[str, str],
    base: Mapping[str, str | bytes],
    head: Mapping[str, str | bytes],
    *,
    runner: FakeRunner | None = None,
    bounds: dp.Bounds = dp.DEFAULT_BOUNDS,
    changed: tuple[str, ...] = (),
    clock: Callable[[], float] | None = None,
    prepare: Callable[[Path, Path], None] | None = None,
) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(dir=tmp_path))
    scratch = root / "scratch"
    scratch.mkdir()
    base_tree, head_tree = _write(root / "base", base), _write(root / "head", head)
    if prepare is not None:
        prepare(base_tree, head_tree)
    return dp.run_data_path(
        base_tree,
        head_tree,
        changed_paths=changed,
        environ=env,
        bounds=bounds,
        runner=runner or FakeRunner(),
        system=SYSTEM,
        machine=MACHINE,
        isolation=dp.Isolation("sandbox-exec", "test"),
        scratch_root=scratch,
        **({"clock": clock} if clock is not None else {}),
    )


def _result(
    path: str,
    line: int,
    callee: str,
    *,
    framework: str = "django",
    kind: str = "db",
    source: tuple[str, int, str] | None = None,
    middle: tuple[tuple[str, int, str], ...] = (),
) -> dict[str, Any]:
    src_path, src_line, src_text = source or (path, line - 1, "request.POST")
    trace = [
        {"kind": "source", "location": {"path": src_path, "start": {"line": src_line}}, "content": src_text},
        *({"kind": "through", "location": {"path": p, "start": {"line": n}}, "content": text} for p, n, text in middle),
        {"kind": "sink", "location": {"path": path, "start": {"line": line}}, "content": f"{callee}(name=name)"},
    ]
    return {
        "check_id": f"lemoncrow.data-path.{framework}.{kind}",
        "path": path,
        "start": {"line": line, "col": 5},
        "extra": {"metavars": {"$F": {"abstract_content": callee}}, "dataflow_trace": trace, "lines": "SECRET"},
    }


def _head_results(*results: dict[str, Any]) -> Callable[[str, str], dict[str, Any]]:
    return lambda side, _framework: {"results": list(results) if side == "head" else [], "errors": []}


def _caps(report: Mapping[str, Any]) -> set[str]:
    return {str(row["cap"]) for row in report["caps"]}


def _text(report: Mapping[str, Any]) -> str:
    return json.dumps(report).lower()


# --------------------------------------------------------------------------- #
# rules: LemonCrow's own, one per sink kind, sharing sources and propagators
# --------------------------------------------------------------------------- #

LANGUAGES = {"django": ["python"], "express": ["typescript", "javascript"], "spring": ["java"]}


def _rules(framework: str) -> list[dict[str, Any]]:
    data = yaml.safe_load((dp.RULES_DIR / f"{framework}.yaml").read_text(encoding="utf-8"))
    assert set(data) == {"rules"}
    return list(data["rules"])


@pytest.mark.parametrize("framework", sorted(LANGUAGES))
def test_rules_are_interfile_taint_rules_one_per_sink_kind(framework: str) -> None:
    rules = _rules(framework)
    assert [rule["id"] for rule in rules] == [f"lemoncrow.data-path.{framework}.{kind}" for kind in dp.KINDS]
    for rule, kind in zip(rules, dp.KINDS, strict=True):
        assert rule["mode"] == "taint"
        assert rule["options"] == {"interfile": True}
        assert rule["languages"] == LANGUAGES[framework]
        assert rule["metadata"] == {"category": "data-path", "framework": framework, "sink_kind": kind}
        assert rule["pattern-sources"] and rule["pattern-sinks"] and rule["pattern-sanitizers"]
        assert rule["pattern-propagators"]


@pytest.mark.parametrize("framework", sorted(LANGUAGES))
def test_rules_of_a_file_share_sources_propagators_and_sanitizers(framework: str) -> None:
    rules = _rules(framework)
    for key in ("pattern-sources", "pattern-propagators", "pattern-sanitizers", "languages", "options"):
        assert all(rule[key] == rules[0][key] for rule in rules), key
    assert len({json.dumps(rule["pattern-sinks"], sort_keys=True) for rule in rules}) == len(rules)
    # Opengrep rejects YAML anchors, so none may creep in.
    text = (dp.RULES_DIR / f"{framework}.yaml").read_text(encoding="utf-8")
    assert "&id" not in text and "*id" not in text
    assert "\n# SPDX-License-Identifier: Apache-2.0\n" in text.split("\nrules:", 1)[0]


def test_rules_carry_the_orm_lookup_and_dispatch_propagators() -> None:
    def text(framework: str, key: str) -> str:
        return json.dumps(_rules(framework)[0][key])

    assert "objects.get(" in text("django", "pattern-propagators")
    assert "get_object_or_404(" in text("django", "pattern-propagators")
    assert "perform_" in text("django", "pattern-sources") and "get_object()" in text("django", "pattern-sources")
    assert "Object.assign(" in text("express", "pattern-propagators")
    assert ".param(" in text("express", "pattern-sources")
    assert ".find(" in text("spring", "pattern-propagators")
    assert "getReferenceById(" in text("spring", "pattern-propagators")
    assert "PathVariable" in text("spring", "pattern-sources")


def test_rules_readme_says_they_are_lemoncrows_own() -> None:
    readme = (dp.RULES_DIR / "README.md").read_text(encoding="utf-8")
    assert "written for LemonCrow" in readme
    assert "Semgrep Rules License" in readme and "None of them is copied" in readme
    assert sorted(path.name for path in dp.RULES_DIR.glob("*.yaml")) == sorted(p.name for p in dp.rule_files())


def test_rules_digest_covers_every_rule_file_and_names_the_stamp() -> None:
    digest = hashlib.sha256()
    for path in dp.rule_files():
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    assert dp.rules_digest() == digest.hexdigest()
    assert dp.stamp() == f"data-path-v{dp.SCHEMA_VERSION}.v2.0.0.{digest.hexdigest()[:16]}"


# --------------------------------------------------------------------------- #
# the engine pin: separate from the security scan's, same mechanism
# --------------------------------------------------------------------------- #


def test_data_path_pin_is_separate_and_the_scan_pin_did_not_move() -> None:
    assert opengrep_install.VERSION == "v1.32.0"
    assert opengrep_install.DATA_PATH_VERSION == "v2.0.0" == dp.ENGINE_VERSION
    assert set(opengrep_install.DATA_PATH_PINS) == set(opengrep_install.PINS)
    for key, pin in opengrep_install.DATA_PATH_PINS.items():
        assert isinstance(pin, opengrep_install.Pin)
        assert opengrep_install.is_sha256(pin.sha256) or pin.sha256 == opengrep_install.CHECKSUM_TODO
        assert pin.sha256 != opengrep_install.PINS[key].sha256
    scan, _ = opengrep_install.pin_for("Linux", "x86_64")
    flow, _ = opengrep_install.pin_for("Linux", "x86_64", pins=opengrep_install.DATA_PATH_PINS)
    assert scan is opengrep_install.PINS[("linux", "x86_64")]
    assert flow is opengrep_install.DATA_PATH_PINS[("linux", "x86_64")]


def test_smoke_test_checks_the_version_it_is_asked_for(tmp_path: Path) -> None:
    script = tmp_path / "opengrep"
    script.write_text("#!/bin/sh\necho 2.0.0\n", encoding="utf-8")
    script.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin"}
    assert opengrep_install.smoke_test(script, env, version="v2.0.0") == ""
    assert opengrep_install.smoke_test(script, env) == "the binary did not report v1.32.0"


# --------------------------------------------------------------------------- #
# finding the engine: local only, verified by digest, never downloaded
# --------------------------------------------------------------------------- #


@pytest.fixture
def no_download(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a review must never download the engine")

    monkeypatch.setattr(opengrep_install, "fetch_https", refuse)
    monkeypatch.setattr(opengrep_install, "install", refuse)
    monkeypatch.setattr("urllib.request.urlopen", refuse)


def test_no_engine_is_not_available_with_the_setup_reason(tmp_path: Path, no_download: None) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    runner = FakeRunner()
    report = _run(tmp_path, {"PATH": str(empty)}, {"app/views.py": VIEWS}, {"app/views.py": VIEWS}, runner=runner)
    assert runner.calls == []
    assert report["state"] == "not_available"
    assert report["summary"] == "Data path not available: Opengrep v2.0.0 is not installed (no opengrep on PATH)"
    pin = opengrep_install.DATA_PATH_PINS[opengrep_install.platform_key(SYSTEM, MACHINE)]
    url = opengrep_install.RELEASE_URL.format(version="v2.0.0", asset=pin.asset)
    assert url in report["setup"] and pin.sha256 in report["setup"] and dp.BIN_ENV in report["setup"]
    assert {"scope": "all flows", "reason": "engine unavailable: " + report["summary"].split(": ", 1)[1]} in report[
        "not_measured"
    ]
    assert report["disclosure"] == dp.DISCLOSURE and report["experimental"] is True
    assert report["counts"] == {"added": 0, "removed": 0, "rerouted": 0, "unchanged": 0}
    assert not any(phrase in _text(report) for phrase in NEVER)


def test_named_engine_must_be_an_executable_file(tmp_path: Path, no_download: None) -> None:
    plain = tmp_path / "opengrep"
    plain.write_text("not a binary", encoding="utf-8")
    engine, why = dp.locate_engine({dp.BIN_ENV: str(plain)}, tmp_path / "s", system=SYSTEM, machine=MACHINE)
    assert engine is None and why == f"{dp.BIN_ENV} does not name an executable file"
    engine, why = dp.locate_engine({SCAN_BIN_ENV: str(plain)}, tmp_path / "s", system=SYSTEM, machine=MACHINE)
    assert (
        engine is None and why == f"Opengrep v2.0.0 is not installed ({SCAN_BIN_ENV} does not name an executable file)"
    )
    assert str(tmp_path) not in why


def test_an_engine_that_is_not_the_pinned_release_is_refused(tmp_path: Path, no_download: None) -> None:
    script = tmp_path / "opengrep"
    script.write_text("#!/bin/sh\necho 2.0.0\n", encoding="utf-8")
    script.chmod(0o755)
    engine, why = dp.locate_engine({dp.BIN_ENV: str(script)}, tmp_path / "s", system=SYSTEM, machine=MACHINE)
    assert engine is None
    assert why == (
        f"the Opengrep found through {dp.BIN_ENV} is not the pinned v2.0.0 release (opengrep_osx_arm64): "
        "its SHA-256 differs"
    )


def test_the_verified_private_copy_runs_not_the_original(
    tmp_path: Path, engine_env: dict[str, str], no_download: None
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    engine, why = dp.locate_engine(engine_env, scratch, system=SYSTEM, machine=MACHINE)
    assert why == "" and engine is not None
    assert engine.path == scratch / "engine" / "opengrep"
    assert stat.S_IMODE(engine.path.stat().st_mode) == 0o500
    assert engine.sha256 == hashlib.sha256(Path(engine_env[dp.BIN_ENV]).read_bytes()).hexdigest()
    # Swapping the original after the check cannot change what runs.
    Path(engine_env[dp.BIN_ENV]).write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")
    assert hashlib.sha256(engine.path.read_bytes()).hexdigest() == engine.sha256


def test_the_security_scan_variable_is_a_fallback(tmp_path: Path, engine_env: dict[str, str]) -> None:
    env = {"PATH": "/usr/bin:/bin", SCAN_BIN_ENV: engine_env[dp.BIN_ENV]}
    engine, why = dp.locate_engine(env, tmp_path / "s", system=SYSTEM, machine=MACHINE)
    assert why == "" and engine is not None


def test_a_platform_without_a_verified_pin_is_not_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_env: dict[str, str]
) -> None:
    engine, why = dp.locate_engine(engine_env, tmp_path / "a", system="Linux", machine="riscv64")
    assert engine is None and why == "no Opengrep release is pinned for linux riscv64"
    key = opengrep_install.platform_key(SYSTEM, MACHINE)
    monkeypatch.setitem(
        opengrep_install.DATA_PATH_PINS, key, opengrep_install.Pin("opengrep_osx_arm64", opengrep_install.CHECKSUM_TODO)
    )
    engine, why = dp.locate_engine(engine_env, tmp_path / "b", system=SYSTEM, machine=MACHINE)
    assert engine is None and opengrep_install.CHECKSUM_TODO in why


# --------------------------------------------------------------------------- #
# running: offline arguments, the allow-listed environment, one run per side
# --------------------------------------------------------------------------- #


def test_engine_runs_offline_with_interfile_taint_and_the_allow_list(
    tmp_path: Path, engine_env: dict[str, str]
) -> None:
    runner = FakeRunner()
    _run(tmp_path, engine_env, {"app/views.py": VIEWS}, {"app/views.py": VIEWS}, runner=runner)
    assert [(call.framework, call.side) for call in runner.calls] == [("django", "base"), ("django", "head")]
    args = runner.calls[0].args
    for flag in (
        "--enable-interfile",
        "--interfile-depth=4",
        "--dataflow-traces",
        "--disable-version-check",
        "--disable-nosem",
        "--no-rewrite-rule-ids",
        f"--interfile-timeout={dp.DEFAULT_BOUNDS.run_seconds}",
        f"--max-memory={dp.DEFAULT_BOUNDS.memory_mb}",
        f"--max-target-bytes={dp.DEFAULT_BOUNDS.file_bytes}",
    ):
        assert flag in args
    config = Path(args[args.index("--config") + 1])
    assert config.name == "django.yaml" and config.parent.name == "rules" and dp.RULES_DIR not in config.parents
    env = runner.calls[0].env
    assert "GITHUB_TOKEN" not in env and set(env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP"}
    assert Path(env["HOME"]).name == ".lemoncrow-sandbox-home"
    # The verified private copy runs (behind the memory and time ceiling prefix), never the named original.
    assert [arg for arg in args if arg.endswith("/engine/opengrep")] == [args[args.index("scan") - 1]]
    assert engine_env[dp.BIN_ENV] not in args


def test_typescript_runs_at_depth_two_with_the_flattened_tsconfig(tmp_path: Path, engine_env: dict[str, str]) -> None:
    seen: dict[str, Any] = {}

    def documents(side: str, _framework: str) -> dict[str, Any]:
        seen[side] = json.loads((runner.calls[-1].cwd / "tsconfig.json").read_text(encoding="utf-8"))
        return {"results": [], "errors": []}

    runner = FakeRunner(documents)
    tree = {rel: (FIXTURES / "express" / "base" / rel).read_text() for rel in _files(FIXTURES / "express" / "base")}
    report = _run(tmp_path, engine_env, tree, tree, runner=runner)
    assert "--interfile-depth=2" in runner.calls[0].args
    assert seen["base"] == {"compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["src/lib/*"]}}}
    assert report["coverage"]["frameworks"][0]["framework"] == "express"


def _files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


# --------------------------------------------------------------------------- #
# staging: covered source files only, never through a link, bounded
# --------------------------------------------------------------------------- #


def test_staging_copies_only_regular_covered_utf8_files(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, {"secret.py": "TOKEN = 'x'\n"})
    tree = _write(
        tmp_path / "tree",
        {
            "app/views.py": VIEWS,
            "README.md": "# not source\n",
            "node_modules/pkg/index.js": "require('express')\n",
            "app/nul.py": b"x = 1\0\n",
            "app/latin.py": b"x = '\xe9'\n",
        },
    )
    os.symlink(outside / "secret.py", tree / "app" / "link.py")
    os.symlink(outside, tree / "linked")
    staged = dp.stage_side(tree, tmp_path / "staged", dp.DEFAULT_BOUNDS)
    assert sorted(staged.files) == ["app/views.py"] and staged.lines == {"app/views.py": 6}
    assert staged.detected == {"django"}
    assert staged.excluded == {
        "app/latin.py": "not UTF-8 text",
        "app/link.py": "a symbolic link: not followed",
        "app/nul.py": "contains a NUL byte",
    }
    assert staged.excluded_dirs == {"linked/": "a symbolic link: not followed"}
    assert staged.skipped == ["node_modules/"]
    copied = tmp_path / "staged" / "app" / "views.py"
    assert copied.read_text() == VIEWS and stat.S_IMODE(copied.stat().st_mode) == 0o600
    assert not any(path.is_symlink() for path in (tmp_path / "staged").rglob("*"))
    # The empty .semgrepignore turns off the engine's own ignore list (tests/, build/ ...).
    assert _files(tmp_path / "staged") == [".semgrepignore", "app/views.py"]
    assert (tmp_path / "staged" / ".semgrepignore").read_bytes() == b""


def test_frameworks_are_detected_by_their_imports(tmp_path: Path) -> None:
    tree = _write(
        tmp_path / "tree",
        {
            "a.py": "import os\n",
            "web/server.ts": 'import express from "express";\n',
            "Svc.java": "import org.springframework.stereotype.Service;\n",
        },
    )
    staged = dp.stage_side(tree, tmp_path / "staged", dp.DEFAULT_BOUNDS)
    assert staged.detected == {"express", "spring"}
    assert staged.counts == {"django": 1, "express": 1, "spring": 1}


# --------------------------------------------------------------------------- #
# tsconfig: Opengrep reads `paths` from the root file and does not follow `extends`
# --------------------------------------------------------------------------- #


def test_jsonc_comments_and_trailing_commas() -> None:
    text = '{\n  // line\n  "a": "http://x/*y*/", /* block */\n  "b": [1, 2,],\n  "c": "q\\"//",\n}\n'
    assert dp.parse_jsonc(text) == {"a": "http://x/*y*/", "b": [1, 2], "c": 'q"//'}


def test_flatten_follows_relative_extends_and_rebases_targets() -> None:
    root = FIXTURES / "express" / "base"
    flattened, notes = dp.flatten_tsconfig(lambda rel: (root / rel).read_text(), ["tsconfig.json"])
    assert flattened == {"compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["src/lib/*"]}}}
    assert notes == []


def test_flatten_nested_configs_drops_escapes_and_names_what_it_did_not_follow() -> None:
    files = {
        "tsconfig.json": '{"extends": "@tsconfig/node18/tsconfig.json", "compilerOptions": {"paths": '
        '{"@up/*": ["../../etc/*"], "bad alias": ["src/*"], "@ok/*": ["src/ok/*"]}}}',
        "packages/web/tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"~/*": ["src/*"]}}}',
        "packages/api/tsconfig.json": '{"extends": "../../../outside.json"}',
    }
    flattened, notes = dp.flatten_tsconfig(files.get, sorted(files, key=lambda rel: (rel.count("/"), rel)))
    assert flattened["compilerOptions"]["paths"] == {
        "*": ["packages/web/*"],
        "@ok/*": ["src/ok/*"],
        "~/*": ["packages/web/src/*"],
    }
    assert notes == [
        "an alias or target with unexpected characters was dropped",
        "packages/api/tsconfig.json extends a file outside the repository: not followed",
        "tsconfig.json extends a package: not followed",
        "tsconfig.json: a path target outside the repository was dropped",
    ]


def test_no_tsconfig_is_named_as_not_measured(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"server.ts": 'import express from "express";\n'}
    report = _run(tmp_path, engine_env, tree, tree)
    reasons = [row["reason"] for row in report["not_measured"]]
    assert "no tsconfig: there is no tsconfig.json, so only relative imports were followed across files" in reasons


# --------------------------------------------------------------------------- #
# bounds: every cap at its bound (nothing named) and one past it (named, never dropped silently)
# --------------------------------------------------------------------------- #


def _replace(**changes: int) -> dp.Bounds:
    return replace(dp.DEFAULT_BOUNDS, **changes)


def test_cap_files(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {f"app/v{index}.py": VIEWS for index in range(3)}
    at = _run(tmp_path, engine_env, tree, tree, bounds=_replace(files=3))
    assert "files" not in _caps(at)
    past = _run(tmp_path, engine_env, tree, tree, bounds=_replace(files=2))
    assert {"cap": "files", "limit": 2, "side": "base", "skipped": 1} in past["caps"]
    assert {"scope": "head: 1 source files past the first 2: app/v2.py", "reason": "cap files hit"} in past[
        "not_measured"
    ]
    assert past["state"] == "partial"


def test_cap_file_bytes(tmp_path: Path, engine_env: dict[str, str]) -> None:
    size = len(VIEWS.encode())
    tree = {"app/views.py": VIEWS}
    at = _run(tmp_path, engine_env, tree, tree, bounds=_replace(file_bytes=size))
    assert "file_bytes" not in _caps(at) and at["coverage"]["frameworks"][0]["files"] == {"base": 1, "head": 1}
    past = _run(tmp_path, engine_env, tree, tree, bounds=_replace(file_bytes=size - 1))
    assert {"cap": "file_bytes", "limit": size - 1, "side": "head", "skipped": 1, "paths": ["app/views.py"]} in past[
        "caps"
    ]
    assert any(row["reason"] == "cap file_bytes hit" for row in past["not_measured"])


def test_cap_total_bytes(tmp_path: Path, engine_env: dict[str, str]) -> None:
    size = len(VIEWS.encode())
    tree = {"app/a.py": VIEWS, "app/b.py": VIEWS}
    at = _run(tmp_path, engine_env, tree, tree, bounds=_replace(total_bytes=2 * size))
    assert "total_bytes" not in _caps(at)
    past = _run(tmp_path, engine_env, tree, tree, bounds=_replace(total_bytes=2 * size - 1))
    assert {"cap": "total_bytes", "limit": 2 * size - 1, "side": "base", "skipped": 1} in past["caps"]
    assert any(row["reason"] == "cap total_bytes hit" for row in past["not_measured"])


def test_cap_tsconfigs(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {
        "server.ts": 'import express from "express";\n',
        "tsconfig.json": "{}",
        "pkg/tsconfig.json": "{}",
    }
    at = _run(tmp_path, engine_env, tree, tree, bounds=_replace(tsconfigs=2))
    assert "tsconfigs" not in _caps(at)
    past = _run(tmp_path, engine_env, tree, tree, bounds=_replace(tsconfigs=1))
    assert {"cap": "tsconfigs", "limit": 1, "side": "head", "skipped": 1} in past["caps"]
    assert any(row["reason"] == "cap tsconfigs hit" for row in past["not_measured"])


def test_cap_run_seconds(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}
    runner = FakeRunner()
    at = _run(tmp_path, engine_env, tree, tree, runner=runner, bounds=_replace(run_seconds=7))
    assert at["state"] == "ran" and "run_seconds" not in _caps(at)
    assert "--interfile-timeout=7" in runner.calls[0].args
    assert runner.calls[0].timeout == 7 + (15 if sys.platform == "darwin" else 0)
    for runner in (FakeRunner(error=TimeoutError("killed")), FakeRunner(returncode=124)):
        past = _run(tmp_path, engine_env, tree, tree, runner=runner, bounds=_replace(run_seconds=7))
        assert {"cap": "run_seconds", "limit": 7, "framework": "django", "side": "base"} in past["caps"]
        assert past["state"] == "not_measured"
        assert past["summary"] == (
            "Data path not measured: Django and Django REST framework: the time cap of one engine run (7 s) was hit "
            "on base"
        )
        assert past["coverage"]["frameworks"][0]["measured"] is False


def test_cap_memory_mb(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}
    runner = FakeRunner()
    at = _run(tmp_path, engine_env, tree, tree, runner=runner, bounds=_replace(memory_mb=512))
    assert at["state"] == "ran" and "--max-memory=512" in runner.calls[0].args
    for code in (137, -9):
        past = _run(
            tmp_path, engine_env, tree, tree, runner=FakeRunner(returncode=code), bounds=_replace(memory_mb=512)
        )
        assert {"cap": "memory_mb", "limit": 512, "framework": "django", "side": "base"} in past["caps"]
        assert any("the memory cap (512 MiB) was hit on base" in row["reason"] for row in past["not_measured"])


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_cap_total_seconds(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}
    for spent, measured in ((9.0, True), (10.0, False)):
        clock = Clock()

        def advance(_side: str, clock: Clock = clock, spent: float = spent) -> None:
            clock.now += spent

        runner = FakeRunner(on_run=advance)
        report = _run(tmp_path, engine_env, tree, tree, runner=runner, bounds=_replace(total_seconds=10), clock=clock)
        assert report["coverage"]["frameworks"][0]["measured"] is measured
        if measured:
            assert "total_seconds" not in _caps(report)
            assert "--interfile-timeout=1" in runner.calls[1].args  # what the whole cap left for head
        else:
            assert len(runner.calls) == 1
            assert {"cap": "total_seconds", "limit": 10, "framework": "django", "side": "head"} in report["caps"]


def _padded(size: int) -> bytes:
    head, tail = b'{"results": [], "errors": [], "paths": {"scanned": ["app/views.py"]}, "pad": "', b'"}'
    return head + b"x" * (size - len(head) - len(tail)) + tail


def test_cap_json_bytes(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}
    at = _run(
        tmp_path, engine_env, tree, tree, runner=FakeRunner(lambda *_: _padded(1000)), bounds=_replace(json_bytes=1000)
    )
    assert at["state"] == "ran" and "json_bytes" not in _caps(at)
    past = _run(
        tmp_path, engine_env, tree, tree, runner=FakeRunner(lambda *_: _padded(1001)), bounds=_replace(json_bytes=1000)
    )
    assert {"cap": "json_bytes", "limit": 1000, "framework": "django", "side": "base"} in past["caps"]


def test_cap_results(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}

    def rows(count: int) -> FakeRunner:
        return FakeRunner(lambda *_: {"results": [{}] * count, "errors": []})

    at = _run(tmp_path, engine_env, tree, tree, runner=rows(3), bounds=_replace(results=3))
    assert at["state"] == "ran" and "results" not in _caps(at)
    past = _run(tmp_path, engine_env, tree, tree, runner=rows(4), bounds=_replace(results=3))
    assert {"cap": "results", "limit": 3, "framework": "django", "side": "base"} in past["caps"]


def test_cap_flows_per_kind(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}
    runner = FakeRunner(_head_results(*(_result("app/views.py", 6, f"store{index}") for index in range(3))))
    at = _run(tmp_path, engine_env, {}, tree, runner=runner, bounds=_replace(flows_per_kind=3))
    assert at["counts"]["added"] == 3 and len(at["flows"]["added"]) == 3 and at["omitted"]["added"] == 0
    assert "flows_per_kind" not in _caps(at)
    past = _run(tmp_path, engine_env, {}, tree, runner=runner, bounds=_replace(flows_per_kind=2))
    assert past["counts"]["added"] == 3 and len(past["flows"]["added"]) == 2 and past["omitted"]["added"] == 1
    assert {"cap": "flows_per_kind", "limit": 2, "change": "added", "omitted": 1} in past["caps"]
    assert past["summary"] == "3 added · 0 removed · 0 rerouted"


def test_cap_steps(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}
    middle = (("app/views.py", 5, "name"),) * 3
    runner = FakeRunner(_head_results(_result("app/views.py", 6, "Widget.objects.create", middle=middle)))
    at = _run(tmp_path, engine_env, {}, tree, runner=runner, bounds=_replace(steps=5))
    route = at["flows"]["added"][0]["head"]
    assert route["omitted_steps"] == 0 and len(route["steps"]) == 5 and "steps" not in _caps(at)
    past = _run(tmp_path, engine_env, {}, tree, runner=runner, bounds=_replace(steps=4))
    route = past["flows"]["added"][0]["head"]
    assert route["omitted_steps"] == 1 and len(route["steps"]) == 4
    assert route["steps"][-1] == {"at": "app/views.py:6", "kind": "sink", "label": "Widget.objects.create"}
    assert {"cap": "steps", "limit": 4, "routes": 1} in past["caps"]


# --------------------------------------------------------------------------- #
# the delta: keyed on (source symbol, sink symbol), the path is detail
# --------------------------------------------------------------------------- #


def _flow(source: str, sink: str, path: tuple[str, ...] = (), *, line: int = 10, file: str = "a.py") -> dp.Flow:
    source_fn, label = source.split("|")
    sink_fn, callee = sink.split("|")
    first = dp.Step(file, line, "source", label)
    last = dp.Step(file, line + 2, "sink", callee)
    return dp.Flow(
        kind="db",
        framework="django",
        source_symbol=source,
        sink_symbol=sink,
        entry_point=source_fn,
        source=first,
        sink=last,
        callee=callee,
        path=path or (source_fn, sink_fn),
        steps=(first, last),
    )


def test_delta_added_removed_rerouted_unchanged() -> None:
    kept = _flow("a.py::log|request.data", "a.py::log|logger.info")
    gone = _flow("a.py::delete|pk", "a.py::delete|widget.delete")
    new = _flow("a.py::rename|pk", "a.py::rename|order.save")
    before = _flow("a.py::post|request.data", "s.py::create|Widget.objects.create")
    after = _flow(
        "a.py::post|request.data", "s.py::create|Widget.objects.create", ("a.py::post", "s.py::clean", "s.py::create")
    )
    changes, unchanged = dp.diff_flows([kept, gone, before], [kept, new, after])
    assert unchanged == 1
    assert [(change.kind, change.flow.key) for change in changes] == [
        ("added", new.key),
        ("removed", gone.key),
        ("rerouted", after.key),
    ]
    rerouted = dp.change_wire(changes[2], depth=4, steps_cap=32)
    assert rerouted["base"]["path"] == ["a.py::post", "s.py::create"]
    assert rerouted["head"]["path"] == ["a.py::post", "s.py::clean", "s.py::create"]
    assert rerouted["found_by"] == "Opengrep v2.0.0 taint, depth 4"


def test_delta_ignores_code_that_only_moved() -> None:
    base = _flow("a.py::post|request.data", "a.py::post|Widget.objects.create", line=10)
    head = _flow("a.py::post|request.data", "a.py::post|Widget.objects.create", line=40)
    assert dp.diff_flows([base], [head]) == ([], 1)


def test_delta_counts_duplicate_results_of_one_flow_once() -> None:
    flow = _flow("a.py::update|serializer", "a.py::update|serializer.save")
    changes, _ = dp.diff_flows([], [flow, flow])
    assert len(changes) == 1


def test_delta_is_deterministic() -> None:
    base = [_flow(f"a.py::f{index}|request.data", f"a.py::f{index}|db.save") for index in range(6)]
    head = [_flow(f"a.py::g{index}|request.data", f"a.py::g{index}|db.save") for index in range(6)]
    head.append(_flow("a.py::f0|request.data", "a.py::f0|db.save", ("a.py::f0", "b.py::x", "a.py::f0")))
    expected = [dp.change_wire(change, depth=4, steps_cap=32) for change in dp.diff_flows(base, head)[0]]
    for seed in range(5):
        rng = random.Random(seed)
        shuffled_base, shuffled_head = base[:], head[:]
        rng.shuffle(shuffled_base)
        rng.shuffle(shuffled_head)
        changes, _ = dp.diff_flows(shuffled_base, shuffled_head)
        assert [dp.change_wire(change, depth=4, steps_cap=32) for change in changes] == expected


def test_steps_are_path_line_in_their_own_side(tmp_path: Path, engine_env: dict[str, str]) -> None:
    base = {"app/views.py": VIEWS}
    head = {"app/views.py": "# moved\n" * 3 + VIEWS}

    def documents(side: str, _framework: str) -> dict[str, Any]:
        shift = 3 if side == "head" else 0
        middle = (("app/views.py", 5 + shift, "name"),) if side == "head" else ()
        return {"results": [_result("app/views.py", 6 + shift, "Widget.objects.create", middle=middle)]}

    report = _run(tmp_path, engine_env, base, head, runner=FakeRunner(documents))
    # Same key, same function path: moved lines and a longer trace inside one function are not a reroute.
    assert report["counts"] == {"added": 0, "removed": 0, "rerouted": 0, "unchanged": 1}
    assert report["summary"] == dp.NO_FLOWS


# --------------------------------------------------------------------------- #
# engine output is data: paths must be staged files, text must be identifier-like
# --------------------------------------------------------------------------- #


def test_results_outside_the_staged_files_or_rules_are_dropped(tmp_path: Path, engine_env: dict[str, str]) -> None:
    results = [
        _result("../../etc/passwd", 2, "open"),
        _result("/etc/hosts", 2, "open"),
        _result("app/missing.py", 2, "open"),
        {**_result("app/views.py", 6, "open"), "check_id": "other.rule.db"},
        _result("app/views.py", 6, "open", framework="spring"),
    ]
    report = _run(tmp_path, engine_env, {}, {"app/views.py": VIEWS}, runner=FakeRunner(_head_results(*results)))
    assert report["counts"]["added"] == 0


def test_symbol_text_is_identifier_like_or_a_digest() -> None:
    assert dp.expression_text("request.data") == "request.data"
    assert dp.expression_text("request.POST['name']") == "request.POST['name']"
    assert dp.expression_text("x + `$(rm -rf /)`").startswith("expr#")
    assert dp.callee_text("Widget.objects.filter(pk=pk).update") == "Widget.objects.filter().update"
    assert dp.callee_text("a(b(c(d)))") == "a()"
    assert dp.callee_text("<script>alert(1)</script>").startswith("call#")


def test_engine_text_never_reaches_the_report_beyond_symbols(tmp_path: Path, engine_env: dict[str, str]) -> None:
    result = _result(
        "app/views.py", 6, "Widget.objects.create", source=("app/views.py", 5, "request.POST['name'] + '<b>'")
    )
    result["extra"]["dataflow_trace"].insert(
        1, {"kind": "through", "location": {"path": "app/views.py", "start": {"line": 5}}, "content": "a = '<img>'"}
    )
    report = _run(tmp_path, engine_env, {}, {"app/views.py": VIEWS}, runner=FakeRunner(_head_results(result)))
    text = json.dumps(report)
    assert "SECRET" not in text and "<b>" not in text and "<img>" not in text
    flow = report["flows"]["added"][0]
    assert flow["source"]["label"].startswith("expr#") and flow["head"]["steps"][1] == {
        "at": "app/views.py:5",
        "kind": "through",
    }


def test_a_deeply_nested_trace_does_not_recurse() -> None:
    node: dict[str, Any] = {"kind": "sink", "location": {"path": "a.py", "start": {"line": 1}}}
    for _ in range(50_000):
        node = {"kind": "through", "location": {"path": "a.py", "start": {"line": 1}}, "children": [node]}
    out: list[dict[str, Any]] = []
    dp._trace([node], out)
    assert len(out) == 50_001


def test_engine_output_is_never_read_through_a_link(tmp_path: Path, engine_env: dict[str, str]) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"results": [], "errors": []}), encoding="utf-8")

    def plant(_side: str) -> None:
        call = runner.calls[-1]
        output = Path(next(arg.split("=", 1)[1] for arg in call.args if arg.startswith("--json-output=")))
        os.symlink(elsewhere, output)

    runner = FakeRunner(lambda *_: None, on_run=plant)
    tree = {"app/views.py": VIEWS}
    report = _run(tmp_path, engine_env, tree, tree, runner=runner)
    assert report["state"] == "not_measured" and "wrote no readable output" in report["summary"]


def test_each_run_gets_a_fresh_writable_home(tmp_path: Path, engine_env: dict[str, str]) -> None:
    seen: list[list[str]] = []

    def plant(_side: str) -> None:
        home = Path(runner.calls[-1].env["HOME"])
        seen.append(sorted(path.name for path in home.iterdir()))
        (home / ".opengrep-settings.yml").write_text("planted", encoding="utf-8")

    runner = FakeRunner(on_run=plant)
    tree = {"app/views.py": VIEWS}
    _run(tmp_path, engine_env, tree, tree, runner=runner)
    assert seen == [[], []]


def test_a_failure_names_the_error_type_never_the_engines_text(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}
    documents = lambda *_: {  # noqa: E731
        "results": [],
        "errors": [{"type": ["PartialParsing", [{"path": "x"}]], "message": "near `password = 'hunter2'`"}],
    }
    runner = FakeRunner(documents, returncode=2, stderr="stderr quoting password = 'hunter2'")
    report = _run(tmp_path, engine_env, tree, tree, runner=runner)
    assert report["summary"] == (
        "Data path not measured: Django and Django REST framework: Opengrep exited with code 2: PartialParsing on base"
    )
    assert "hunter2" not in json.dumps(report)


def test_unreadable_engine_output_is_a_failure_not_an_empty_diff(tmp_path: Path, engine_env: dict[str, str]) -> None:
    nested = b"[" * 200_000 + b"]" * 200_000
    tree = {"app/views.py": VIEWS}
    for document in (b"not json", nested, {"results": "nope"}):
        report = _run(tmp_path, engine_env, tree, tree, runner=FakeRunner(lambda *_, d=document: d))
        assert report["state"] == "not_measured" and report["summary"].startswith("Data path not measured: ")
        assert "wrote no readable output" in report["summary"]


# --------------------------------------------------------------------------- #
# function names: tree-sitter over the staged bytes
# --------------------------------------------------------------------------- #


def _parser_or_skip(language: str) -> None:
    from lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast import tree_sitter_parser

    if tree_sitter_parser(language) is None:
        pytest.skip(f"the tree-sitter {language} grammar is not installed here")


def test_function_names_for_python_typescript_and_java() -> None:
    for language in ("python", "typescript", "java"):
        _parser_or_skip(language)
    python = b"class V:\n    def post(self, request):\n        return 1\n\n\ndef helper():\n    pass\n"
    assert [name for *_lines, name in dp.function_index(python, "a.py") or []] == ["V.post", "helper"]
    typescript = (
        b'app.post("/api/users", async (req, res) => {\n  save(req.body);\n});\n'
        b"export function clean(value: string) {\n  return value.trim();\n}\n"
        b"const handler = () => 1;\n"
    )
    names = [name for *_lines, name in dp.function_index(typescript, "server.ts") or []]
    assert names == ['post("/api/users")', "clean", "handler"]
    java = b"class C {\n  void a(long id) {\n    run(() -> id);\n  }\n}\n"
    assert [name for *_lines, name in dp.function_index(java, "C.java") or []] == ["C.a", "C.a.<anonymous1>"]
    assert dp.function_index(b"fn main() {}", "main.rs") is None


# --------------------------------------------------------------------------- #
# honesty: what it covered, what it did not, and never "no flow changes"
# --------------------------------------------------------------------------- #


def test_the_disclosure_is_fixed_and_says_it_finds_a_minority() -> None:
    assert "minority" in dp.DISCLOSURE and "17 %" in dp.DISCLOSURE and "Experimental" in dp.DISCLOSURE


def test_an_empty_diff_is_no_flows_found_with_its_coverage(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}
    report = _run(tmp_path, engine_env, tree, tree)
    assert report["state"] == "ran" and report["summary"] == dp.NO_FLOWS == "No flows found"
    assert report["detail"] == dp.NO_FLOWS_DETAIL
    assert report["coverage"] == {
        "languages": ["Python"],
        "frameworks": [
            {
                "framework": "django",
                "title": "Django and Django REST framework",
                "language": "Python",
                "depth": 4,
                "files": {"base": 1, "head": 1},
                "measured": True,
            }
        ],
    }
    assert report["engine"]["version"] == "v2.0.0" and len(report["engine"]["sha256"]) == 64
    assert report["rules"] == {"digest": dp.rules_digest(), "files": ["django.yaml", "express.yaml", "spring.yaml"]}
    assert report["disclosure"] == dp.DISCLOSURE and report["experimental"] is True
    assert report["limits"] == [
        "Django and Django REST framework: generic views that write without a handler method of their own are not "
        "followed"
    ]
    assert report["isolation"] == {"mode": "sandbox-exec", "note": "test"}
    assert report["not_measured"] == [] and report["caps"] == []
    assert not any(phrase in _text(report) for phrase in NEVER)


def test_languages_and_frameworks_not_covered_are_named(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"svc/main.go": "package main\n", "app/util.py": "import os\n"}
    runner = FakeRunner()
    report = _run(tmp_path, engine_env, tree, tree, runner=runner, changed=("svc/main.go", "app/util.py"))
    assert runner.calls == []
    assert report["summary"] == dp.NO_FLOWS and report["coverage"] == {"languages": [], "frameworks": []}
    assert report["not_measured"] == [
        {"scope": "Go (1 changed file)", "reason": "language not covered: there are no data path rules for Go"},
        {
            "scope": "Python (1 changed file)",
            "reason": "framework not covered: no Django and Django REST framework import found, and the Python "
            "rules cover Django and Django REST framework only",
        },
        {
            "scope": "all flows",
            "reason": "framework not covered: no Django, Express or Spring source found on either side",
        },
    ]


def test_a_side_that_failed_is_not_diffed(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}
    documents = lambda side, _framework: {  # noqa: E731
        "results": [_result("app/views.py", 6, "Widget.objects.create")] if side == "base" else [],
        "errors": [{"type": "Fatal error"}],
    }
    report = _run(
        tmp_path, engine_env, tree, tree, runner=FakeRunner(documents, returncode=lambda side: 2 * (side == "head"))
    )
    # The base flow would read as removed; nothing is diffed instead.
    assert report["state"] == "not_measured" and report["counts"]["removed"] == 0
    assert report["summary"] == (
        "Data path not measured: Django and Django REST framework: Opengrep exited with code 2: Fatal error on head"
    )
    assert {
        "scope": "Django and Django REST framework flows",
        "reason": "not diffed: Opengrep exited with code 2: Fatal error on head",
    } in report["not_measured"]


def test_per_file_engine_errors_are_named(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {"app/views.py": VIEWS}
    documents = lambda *_: {"results": [], "errors": [{"type": "Timeout"}, {"type": "OutOfMemory"}]}  # noqa: E731
    report = _run(tmp_path, engine_env, tree, tree, runner=FakeRunner(documents))
    assert {
        "scope": "Django and Django REST framework on base: 1 file or rule errors",
        "reason": "Opengrep's per-file time limit (10 s)",
    } in report["not_measured"]
    assert {
        "scope": "Django and Django REST framework on head: 1 file or rule errors",
        "reason": "Opengrep's memory cap",
    } in report["not_measured"]
    # A file the engine gave up on may hold a flow: never a plain "No flows found".
    assert report["state"] == "partial"
    assert report["summary"] == (
        "Partial: no flows found in what was measured (engine errors on base; engine errors on head)"
    )


# --------------------------------------------------------------------------- #
# storage and the projection the Map lens reads
# --------------------------------------------------------------------------- #


class MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}

    def put_derived_results(self, rows: list[Any]) -> None:
        for row in rows:
            self.rows[(row.kind, row.revision_id, row.scope_key)] = (row.stamp, row.payload)

    def derived_results(self, kind: str, revision_ids: list[str]) -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
        return {
            (rev, scope): value for (k, rev, scope), value in self.rows.items() if k == kind and rev in revision_ids
        }


def test_projection_is_not_run_until_requested_and_stale_reports_are_not_served() -> None:
    store, revision = MemoryStore(), SimpleNamespace(id="rev-1", review_id="rev-review")
    assert dp.projection(store, revision) == {
        "schema": dp.SCHEMA_VERSION,
        "entry": "data_path.diff",
        "state": "not_run",
        "experimental": True,
        "summary": "Not run: the data path runs only when requested",
        "disclosure": dp.DISCLOSURE,
    }
    report = dp.not_available("x")
    dp.store_report(store, revision, report)
    assert dp.projection(store, revision) == report
    store.rows[("data_path", "rev-1", "current")] = ("data-path-v1.v2.0.0.other-rules", report)
    assert dp.projection(store, revision)["state"] == "not_run"


# --------------------------------------------------------------------------- #
# Review API: an on-request entry; the report reaches /map as `data_path`
# --------------------------------------------------------------------------- #


def _fixture_files(framework: str, side: str) -> dict[str, str]:
    root = FIXTURES / framework / side
    return {rel: (root / rel).read_text(encoding="utf-8") for rel in _files(root)}


def _api_review(tmp_path: Path, framework: str = "django") -> SimpleNamespace:
    from tests.gateway.test_review_check_runs_api import _review

    base = _fixture_files(framework, "base")
    head: dict[str, str | None] = {**_fixture_files(framework, "head")}
    head.update({rel: None for rel in base if rel not in head})
    return _review(tmp_path, base=base, head=head)


def _map(review: SimpleNamespace) -> dict[str, Any]:
    from tests.gateway.test_review_check_runs_api import HEADERS

    response = review.client.get(f"/api/reviews/{review.review_id}/map", headers=HEADERS)
    assert response.status_code == 200, response.text
    return dict(response.json()["data_path"])


def _post(review: SimpleNamespace, **body: Any) -> Any:
    from tests.gateway.test_review_check_runs_api import HEADERS

    return review.client.post(f"/api/reviews/{review.review_id}/data-path/run", headers=HEADERS, json=body)


def test_api_runs_only_on_request_and_map_reads_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_download: None
) -> None:
    monkeypatch.setenv(dp.BIN_ENV, str(tmp_path / "no-opengrep"))
    monkeypatch.setattr(dp, "detect_isolation", lambda: dp.Isolation("sandbox-exec", "test"))
    review = _api_review(tmp_path)
    assert _map(review)["state"] == "not_run"
    assert _post(review).status_code == 400
    refused = _post(review, revision_id=review.revision_id)
    assert refused.status_code == 409 and "data_path.diff is not in the effective run plan" in refused.text
    ran = _post(review, revision_id=review.revision_id, pick=True)
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["state"] == "not_available" and body["plan_entry"]["id"] == "data_path.diff"
    assert body["summary"] == f"Data path not available: {dp.BIN_ENV} does not name an executable file"
    stored = _map(review)
    assert stored["state"] == "not_available" and stored["setup"] == body["setup"]
    assert stored["disclosure"] == dp.DISCLOSURE


def test_deep_offers_the_data_path_on_request_only() -> None:
    from lemoncrow.pro.capabilities.review.capability_catalog import PRESET_SELECTIONS, STATIC_CATALOG
    from lemoncrow.pro.capabilities.review.run_plan import execution_class

    entry = STATIC_CATALOG["data_path.diff"]
    assert entry.executor == "data_path" and entry.loci == ("local",)
    # It runs a third-party binary over the Snapshot: an organization's execution block stops it.
    assert execution_class("data_path.diff") == "runtime"
    assert "LEMONCROW_DATA_PATH_OPENGREP_BIN" in entry.setup_hint
    deep = {row.id: row for row in PRESET_SELECTIONS["deep"]}
    assert deep["data_path.diff"].when == "manual" and deep["data_path.diff"].sides == "base+head"
    for preset in ("quick", "auto", "thorough"):
        assert "data_path.diff" not in {row.id for row in PRESET_SELECTIONS[preset]}


# --------------------------------------------------------------------------- #
# the adversarial review of the first cut (X4): one test per finding
# --------------------------------------------------------------------------- #


def test_tsconfig_extends_fan_out_reads_and_walks_each_file_once() -> None:
    # Finding 1: five self-extends, recursed eight deep, were 488,281 reads.
    reads: list[str] = []
    texts = {
        "tsconfig.json": json.dumps({"extends": ["./a.json"] * 5}),
        "a.json": json.dumps({"extends": ["./a.json"] * 5, "compilerOptions": {"paths": {"@a/*": ["a/*"]}}}),
    }

    def read(rel: str) -> str | None:
        reads.append(rel)
        return texts.get(rel)

    flattened, notes = dp.flatten_tsconfig(read, ["tsconfig.json"])
    assert reads == ["tsconfig.json", "a.json"]
    assert flattened == {"compilerOptions": {"baseUrl": ".", "paths": {"@a/*": ["a/*"]}}} and notes == []


def test_tsconfig_extends_lists_are_capped_and_notes_are_listed_once() -> None:
    parents = [f"./p{index}.json" for index in range(40)]
    texts = {
        "tsconfig.json": json.dumps({"extends": parents}),
        "pkg/tsconfig.json": json.dumps({"extends": ["@scope/base"] * 12}),
        **{rel.lstrip("./"): "{}" for rel in parents},
    }
    reads: list[str] = []

    def read(rel: str) -> str | None:
        reads.append(rel)
        return texts.get(rel)

    _flattened, notes = dp.flatten_tsconfig(read, ["tsconfig.json", "pkg/tsconfig.json"], budget=100)
    assert len(reads) == 1 + 16 + 1
    assert notes == [
        "pkg/tsconfig.json extends a package: not followed",
        "tsconfig.json extends more than 16 files: the rest were not followed",
    ]


def test_tsconfig_files_extended_count_under_the_tsconfigs_cap(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {
        "server.ts": 'import express from "express";\n',
        "tsconfig.json": '{"extends": "./a.json"}',
        "a.json": '{"extends": "./b.json"}',
        "b.json": '{"compilerOptions": {"paths": {"@x/*": ["x/*"]}}}',
    }
    seen: list[Any] = []

    def documents(_side: str, _framework: str) -> dict[str, Any]:
        seen.append(json.loads((runner.calls[-1].cwd / "tsconfig.json").read_text(encoding="utf-8")))
        return {"results": [], "errors": []}

    runner = FakeRunner(documents)
    at = _run(tmp_path, engine_env, tree, tree, runner=runner, bounds=_replace(tsconfigs=3))
    assert "tsconfigs" not in _caps(at) and seen[-1]["compilerOptions"]["paths"] == {"@x/*": ["x/*"]}
    past = _run(tmp_path, engine_env, tree, tree, runner=runner, bounds=_replace(tsconfigs=2))
    assert {"cap": "tsconfigs", "limit": 2, "side": "head", "skipped": 1} in past["caps"]
    assert "paths" not in seen[-1]["compilerOptions"]
    assert past["state"] == "partial" and "tsconfig aliases cut on head" in past["summary"]


class Ticking:
    """A clock that moves one second each time it is read."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def test_staging_is_charged_to_the_whole_time_cap(tmp_path: Path, engine_env: dict[str, str]) -> None:
    tree = {f"app/v{index}.py": VIEWS for index in range(30)}
    runner = FakeRunner()
    report = _run(tmp_path, engine_env, tree, tree, runner=runner, bounds=_replace(total_seconds=10), clock=Ticking())
    assert runner.calls == []
    assert {"cap": "total_seconds", "limit": 10, "side": "base", "stage": "staging"} in report["caps"]
    assert report["state"] == "not_measured"
    assert report["summary"] == (
        "Data path not measured: Django and Django REST framework: the time cap of the whole data path (10 s) was "
        "hit copying base"
    )


def test_the_engine_skips_nothing_staged_and_what_it_did_not_scan_is_named(
    tmp_path: Path, engine_env: dict[str, str]
) -> None:
    # Finding 2: Opengrep's own ignore list skipped tests/ silently.
    tree = {"app/views.py": VIEWS, "app/tests/test_views.py": VIEWS}
    ignore: list[bytes] = []

    def documents(_side: str, _framework: str) -> dict[str, Any]:
        ignore.append((runner.calls[-1].cwd / ".semgrepignore").read_bytes())
        return {"results": [], "errors": [], "paths": {"scanned": ["app/views.py"]}}

    runner = FakeRunner(documents)
    report = _run(tmp_path, engine_env, tree, tree, runner=runner)
    assert ignore == [b"", b""]
    assert {
        "scope": "Django and Django REST framework on base: 1 staged files: app/tests/test_views.py",
        "reason": "the engine did not scan them",
    } in report["not_measured"]
    assert report["state"] == "partial"
    assert report["summary"] == (
        "Partial: no flows found in what was measured (1 file on base not scanned; 1 file on head not scanned)"
    )


def test_an_engine_document_that_does_not_list_what_it_scanned_is_not_diffed(
    tmp_path: Path, engine_env: dict[str, str]
) -> None:
    tree = {"app/views.py": VIEWS}
    report = _run(tmp_path, engine_env, tree, tree, runner=FakeRunner(lambda *_: {"results": [], "paths": {}}))
    assert report["state"] == "not_measured"
    assert report["summary"].endswith("Opengrep did not list the files it scanned on base")


def test_vendored_linked_and_unreadable_paths_are_named(tmp_path: Path, engine_env: dict[str, str]) -> None:
    outside = _write(tmp_path / "outside", {"x.py": VIEWS})
    tree = {
        "app/views.py": VIEWS,
        "shop/vendor/evil.py": VIEWS,
        "build/gen.py": VIEWS,
        "static/app.min.js": "require('express')\n",
    }

    def links(_base: Path, head: Path) -> None:
        os.symlink(outside / "x.py", head / "app" / "link.py")
        os.symlink(outside, head / "lib")

    report = _run(tmp_path, engine_env, tree, tree, prepare=links)
    rows = report["not_measured"]
    for side in ("base", "head"):
        assert {
            "scope": f"{side}: 3 paths not read: build/, shop/vendor/, static/app.min.js",
            "reason": "vendored, generated or minified paths are not analysed (alike on both sides)",
        } in rows
    assert {"scope": "head: 2 paths: app/link.py, lib/", "reason": "a symbolic link: not followed"} in rows
    assert report["state"] == "partial"
    assert report["summary"] == (
        "Partial: no flows found in what was measured (3 vendored or generated paths on base not read; "
        "2 paths on head not read; 3 vendored or generated paths on head not read)"
    )


@pytest.mark.parametrize("exclusion", ["utf8", "nul", "file_bytes", "link"])
def test_a_file_one_side_did_not_read_is_never_added_or_removed(
    tmp_path: Path, engine_env: dict[str, str], exclusion: str
) -> None:
    # Finding 3: one \xff byte on head read as "0 added · 3 removed".
    views = VIEWS.encode()
    other = "from django.db import models\n\n\ndef keep(request):\n    name = request.POST['k']\n    Widget.objects.create(name=name)\n"
    base: dict[str, str | bytes] = {"app/views.py": views, "app/other.py": other}
    head: dict[str, str | bytes] = {"app/other.py": other}
    if exclusion == "utf8":
        head["app/views.py"] = views + b"# \xff\n"
    elif exclusion == "nul":
        head["app/views.py"] = views + b"# \0\n"
    elif exclusion == "file_bytes":
        head["app/views.py"] = views + b"#" * 200 + b"\n"
    outside = _write(tmp_path / "outside", {"views.py": views})

    def link(_base: Path, head_tree: Path) -> None:
        if exclusion == "link":
            os.symlink(outside / "views.py", head_tree / "app" / "views.py")

    def documents(side: str, _framework: str) -> dict[str, Any]:
        rows = [_result("app/other.py", 6, "Widget.objects.create")]
        if side == "base":  # a flow from views.py through other.py, and one inside views.py
            rows += [
                _result("app/views.py", 6, "Widget.objects.create"),
                _result("app/other.py", 6, "Widget.objects.update", source=("app/views.py", 5, "request.POST")),
            ]
        return {"results": rows, "errors": []}

    bounds = _replace(file_bytes=len(views) + 50)
    report = _run(tmp_path, engine_env, base, head, runner=FakeRunner(documents), bounds=bounds, prepare=link)
    assert report["counts"] == {"added": 0, "removed": 0, "rerouted": 0, "unchanged": 1}
    assert report["state"] == "partial"
    assert report["summary"].startswith("Partial: no flows found in what was measured (")
    assert "2 flows not diffed" in report["summary"]
    assert any(row["scope"].endswith("app/views.py") for row in report["not_measured"])
    assert {
        "scope": "Django and Django REST framework: 2 flows through files the other side did not read or scan",
        "reason": "not diffed: neither added nor removed is known",
    } in report["not_measured"]


def test_repository_content_never_crashes_staging(tmp_path: Path) -> None:
    # Finding 4: a folder named tsconfig.json and a nesting past the parser's stack were a 500.
    tree = _write(
        tmp_path / "tree",
        {
            "server.ts": 'import express from "express";\n',
            "tsconfig.json/a.ts": "export const a = 1;\n",
            "app/tsconfig.json": "[" * 200_000,
            ".semgrepignore/b.ts": "export const b = 1;\n",
        },
    )
    staged = dp.stage_side(tree, tmp_path / "staged", dp.DEFAULT_BOUNDS)
    assert sorted(staged.files) == [".semgrepignore/b.ts", "server.ts", "tsconfig.json/a.ts"]
    assert staged.tsconfig_written is False
    assert "app/tsconfig.json is not valid JSON" in staged.tsconfig_notes
    assert "a staged folder is named tsconfig.json: the flattened aliases were not used" in staged.tsconfig_notes
    assert staged.notes and "engine applied its own ignore list" in staged.notes[0]


def test_a_folder_named_tsconfig_json_makes_the_report_partial(tmp_path: Path, engine_env: dict[str, str]) -> None:
    base = {"src/server.ts": 'import express from "express";\n', "tsconfig.json": '{"compilerOptions": {}}'}
    head = {
        "src/server.ts": base["src/server.ts"],
        "tsconfig.json/a.ts": "export const a = 1;\n",
        "app/tsconfig.json": "{}",
    }
    report = _run(tmp_path, engine_env, base, head)
    assert report["state"] == "partial"
    assert report["summary"] == "Partial: no flows found in what was measured (tsconfig aliases not used on head)"
    assert {
        "scope": "head: tsconfig",
        "reason": "a staged folder is named tsconfig.json: the flattened aliases were not used",
    } in report["not_measured"]


@pytest.fixture
def platform_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake engine pinned for this machine, named through the environment the Review API runs with."""

    script = tmp_path / "bin" / "opengrep"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho 2.0.0\n", encoding="utf-8")
    script.chmod(0o755)
    key = opengrep_install.platform_key(platform.system(), platform.machine())
    pin = opengrep_install.Pin("opengrep_test", hashlib.sha256(script.read_bytes()).hexdigest())
    monkeypatch.setitem(opengrep_install.DATA_PATH_PINS, key, pin)
    monkeypatch.setenv(dp.BIN_ENV, str(script))
    monkeypatch.setattr(dp, "detect_isolation", lambda: dp.Isolation("sandbox-exec", "test"))
    return script


def test_an_unexpected_error_is_a_stored_report_naming_only_its_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_engine: Path
) -> None:
    from lemoncrow.pro.capabilities.review import planned_execution

    def explode(*_args: Any, **_kwargs: Any) -> dp.Staged:
        raise RecursionError("near password = 'hunter2'")

    recorded: list[str] = []
    record = planned_execution.record_execution_compute

    def counting(*args: Any, **kwargs: Any) -> Any:
        recorded.append(str(kwargs.get("entry_id")))
        return record(*args, **kwargs)

    monkeypatch.setattr(dp, "stage_side", explode)
    monkeypatch.setattr(planned_execution, "record_execution_compute", counting)
    review = _api_review(tmp_path)
    ran = _post(review, revision_id=review.revision_id, pick=True)
    assert ran.status_code == 200, ran.text
    summary = "Data path not measured: the run stopped on an unexpected error (RecursionError)"
    assert ran.json()["state"] == "not_measured" and ran.json()["summary"] == summary
    assert "hunter2" not in ran.text
    assert _map(review)["summary"] == summary and recorded == ["data_path.diff"]


def test_snapshot_errors_are_stored_without_their_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_engine: Path
) -> None:
    from lemoncrow.pro.capabilities.review import snapshot

    def unavailable(*_args: Any, **_kwargs: Any) -> Path:
        raise snapshot.ReviewSnapshotUnavailable("Git tree contains an unsafe path: '<b>x</b>'")

    review = _api_review(tmp_path)
    monkeypatch.setattr(snapshot, "cached_review_side", unavailable)
    ran = _post(review, revision_id=review.revision_id, pick=True)
    assert ran.status_code == 200, ran.text
    assert ran.json()["summary"] == "Data path not available: the Snapshot's source trees are not readable"
    assert "<b>" not in ran.text


def test_without_a_sandbox_nothing_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_env: dict[str, str]
) -> None:
    # Finding 5 (owner: refuse): Linux without working bubblewrap, macOS without sandbox-exec, anything else.
    from lemoncrow.pro.environment import host_tools

    located: list[str] = []

    def locate(*_args: Any, **_kwargs: Any) -> tuple[None, str]:
        located.append("x")
        return None, "x"

    monkeypatch.setattr(dp, "locate_engine", locate)
    monkeypatch.setattr(dp, "_probe_bubblewrap", lambda: "bubblewrap is not installed")
    monkeypatch.setattr(host_tools, "host_executable", lambda _name: None)
    detect = dp.detect_isolation
    runner = FakeRunner()
    expected = {
        "linux": ("bubblewrap is not installed", "sudo apt-get install bubblewrap"),
        "darwin": ("sandbox-exec is missing", "sandbox-exec required"),
        "win32": ("there is no sandbox for the engine on this platform", "macOS (sandbox-exec) or on Linux"),
    }
    for name, (note, setup) in expected.items():
        monkeypatch.setattr(dp, "detect_isolation", lambda name=name: detect(name))
        tree = _write(tmp_path / name, {"app/views.py": VIEWS})
        report = dp.run_data_path(tree, tree, environ=engine_env, runner=runner, scratch_root=tmp_path)
        assert report["state"] == "not_available" and report["isolation"]["mode"] == "none"
        assert report["summary"] == (
            f"Data path not available: {note}, and the data path never runs the engine unsandboxed"
        )
        assert setup in report["setup"]
    assert runner.calls == [] and located == []


def test_contained_refuses_anything_but_a_sandbox(tmp_path: Path) -> None:
    for mode in ("none", "", "plain"):
        with pytest.raises(ValueError, match="never runs the engine unsandboxed"):
            dp.contained(["opengrep"], dp.Isolation(mode, "x"), work=tmp_path, cwd=tmp_path, output=tmp_path / "o")


def test_unsafe_file_names_are_not_staged_and_are_named_by_digest(tmp_path: Path, engine_env: dict[str, str]) -> None:
    # Finding 6: control characters and markup in a path reached the stored report.
    head = {"app/views.py": VIEWS, "app/a\x1b[31m<b>b.py": VIEWS, "app/d‮ir/x.py": VIEWS}
    runner = FakeRunner()
    report = _run(tmp_path, engine_env, {}, head, runner=runner)
    text = json.dumps(report)
    assert "\\u001b" not in text and "<b>" not in text and "\\u202e" not in text
    row = next(row for row in report["not_measured"] if row["reason"] == dp._UNSAFE_NAME)
    assert row["scope"].startswith("head: 2 paths: path#")
    assert report["state"] == "partial"


def test_function_names_from_strings_or_computed_keys_are_digests() -> None:
    _parser_or_skip("typescript")
    source = (
        b"class C {\n"
        b"  '<img src=x onerror=alert(1)>'(req: any) { return 1; }\n"
        b'  ["\\u001b[31mRED"](req: any) { return 2; }\n'
        b"  'plain'(req: any) { return 3; }\n"
        b"  #hidden(req: any) { return 4; }\n"
        b"}\n"
    )
    names = [name for *_lines, name in dp.function_index(source, "x.ts") or []]
    assert len(names) == 4 and names[2:] == ["C.plain", "C.#hidden"]
    assert all(name.startswith("C.name#") for name in names[:2])
    assert dp.symbol_name("<script>") == dp._digest_text("name", "<script>")


def test_extends_values_and_unsafe_paths_never_reach_a_note() -> None:
    texts = {
        "tsconfig.json": json.dumps({"extends": "<script>alert(1)</script>"}),
        "pkg/tsconfig.json": json.dumps({"extends": "./<img src=x>.json"}),
    }
    _flattened, notes = dp.flatten_tsconfig(texts.get, ["tsconfig.json", "pkg/tsconfig.json"])
    assert len(notes) == 2 and "tsconfig.json extends a package: not followed" in notes
    unsafe = next(note for note in notes if note.startswith("path#"))
    assert unsafe.endswith(" could not be read")
    assert not any("<" in note or "script" in note for note in notes)


def test_callee_text_is_bounded_before_it_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 7: emptying 4,000 nested argument lists took half a second per call.
    passes: list[int] = []
    pattern = dp._ARGUMENTS

    class Counting:
        def sub(self, replacement: str, text: str) -> str:
            passes.append(len(text))
            return pattern.sub(replacement, text)

    monkeypatch.setattr(dp, "_ARGUMENTS", Counting())
    assert dp.callee_text("f(" * 4000 + ")" * 4000).startswith("call#") and passes == []
    assert dp.callee_text("a(b(c(d)))") == "a()" and passes


def _innermost(rows: list[tuple[int, int, str]], line: int) -> str | None:
    best: tuple[int, int, str] | None = None
    for first, last, name in rows:
        if first <= line <= last and (best is None or last - first < best[1] - best[0]):
            best = (first, last, name)
    return best[2] if best else None


def test_function_lookup_matches_the_innermost_span_of_a_linear_scan() -> None:
    rng = random.Random(7)
    assert dp.FunctionLines([]).name(3) is None
    for _ in range(400):
        rows = []
        for index in range(rng.randint(1, 25)):
            first = rng.randint(1, 60)
            rows.append((first, first + rng.choice((0, 0, 1, 3, 10, 40)), f"f{index}"))
        rows.sort(key=lambda row: (row[0], -row[1], row[2]))
        lookup = dp.FunctionLines(rows)
        for line in range(1, 110):
            assert lookup.name(line) == _innermost(rows, line), (rows, line)


def test_reading_flows_stops_at_the_whole_time_cap(
    tmp_path: Path, engine_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    callee = dp.callee_text

    def slow(text: str) -> str:
        clock.now += 6
        return callee(text)

    monkeypatch.setattr(dp, "callee_text", slow)
    runner = FakeRunner(_head_results(*(_result("app/views.py", 6, f"store{index}") for index in range(3))))
    report = _run(
        tmp_path, engine_env, {}, {"app/views.py": VIEWS}, runner=runner, bounds=_replace(total_seconds=10), clock=clock
    )
    assert {"cap": "total_seconds", "limit": 10, "framework": "django", "side": "head", "stage": "flows"} in report[
        "caps"
    ]
    assert report["state"] == "not_measured" and report["counts"]["added"] == 0


def test_engine_output_and_staged_reads_never_block_on_a_pipe(tmp_path: Path) -> None:
    # Finding 8: a FIFO planted where the output goes blocked the open for ever.
    fifo = tmp_path / "out.json"
    os.mkfifo(fifo)
    got: list[Any] = []

    def read() -> None:
        got.append(dp._read_output(fifo, 100))
        got.append(dp._read_regular(fifo, 100))

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout=5)
    blocked = thread.is_alive()
    while thread.is_alive():  # release a blocked open so the thread ends
        with open(fifo, "wb"):
            pass
        thread.join(timeout=1)
    assert not blocked and got == [(None, False), None]


def test_result_lines_past_the_end_of_a_staged_file_are_dropped_and_named(
    tmp_path: Path, engine_env: dict[str, str]
) -> None:
    results = [
        _result("app/views.py", 99, "Widget.objects.create"),
        _result("app/views.py", 6, "Widget.objects.update", middle=(("app/views.py", 500, "name"),)),
    ]
    report = _run(tmp_path, engine_env, {}, {"app/views.py": VIEWS}, runner=FakeRunner(_head_results(*results)))
    assert report["counts"]["added"] == 1
    assert [step["at"] for step in report["flows"]["added"][0]["head"]["steps"]] == ["app/views.py:5", "app/views.py:6"]
    assert {
        "scope": "Django and Django REST framework on head: 1 engine result",
        "reason": "dropped: outside the staged files or past a file's last line",
    } in report["not_measured"]
    assert report["state"] == "partial"


def test_scratch_left_by_a_killed_run_is_swept_by_the_next(tmp_path: Path, engine_env: dict[str, str]) -> None:
    scratch = tmp_path / "scratch"
    old, fresh, other = scratch / "lc-data-path-old", scratch / "lc-data-path-fresh", scratch / "unrelated-old"
    for folder in (old, fresh, other):
        folder.mkdir(parents=True)
        (folder / "f").write_text("x", encoding="utf-8")
    target = _write(tmp_path / "elsewhere", {"keep": "k"})
    link = scratch / "lc-data-path-link"
    os.symlink(target, link)
    past = time.time() - 7 * 3600
    for folder in (old, other):
        os.utime(folder, (past, past))
    os.utime(link, (past, past), follow_symlinks=False)
    tree = _write(tmp_path / "tree", {"app/views.py": VIEWS})
    dp.run_data_path(
        tree,
        tree,
        environ=engine_env,
        runner=FakeRunner(),
        system=SYSTEM,
        machine=MACHINE,
        isolation=dp.Isolation("sandbox-exec", "test"),
        scratch_root=scratch,
    )
    assert not old.exists() and fresh.exists() and other.exists()
    assert link.is_symlink() and (target / "keep").exists()
    assert sorted(path.name for path in scratch.iterdir()) == [
        "lc-data-path-fresh",
        "lc-data-path-link",
        "unrelated-old",
    ]


# --------------------------------------------------------------------------- #
# the real binary over the fixture repositories
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def real_env() -> Iterator[dict[str, str]]:
    env = dict(os.environ)
    with tempfile.TemporaryDirectory() as scratch:
        engine, why = dp.locate_engine(env, Path(scratch), system=platform.system(), machine=platform.machine())
    if engine is None:
        pytest.skip(
            f"the pinned Opengrep {dp.ENGINE_VERSION} is not available ({why}); set {dp.BIN_ENV} to the release "
            "binary to run the real-binary tests"
        )
    isolation = dp.detect_isolation()
    if isolation.mode == "none":
        pytest.skip(f"the engine runs only sandboxed: {isolation.note}")
    yield env


J = "src/main/java/com/example/items/"
EXPECTED: dict[str, dict[str, list[tuple[str, str]]]] = {
    "django": {
        "added": [("shop/views.py::rename|pk", "shop/views.py::rename|order.save")],
        "removed": [("shop/views.py::WidgetView.delete|pk", "shop/views.py::WidgetView.delete|widget.delete")],
        "rerouted": [
            ("shop/views.py::WidgetView.post|request.data", "shop/services.py::create_widget|Widget.objects.create")
        ],
    },
    "express": {
        "added": [
            ('src/server.ts::put("/api/users/:id")|req.params', "src/lib/users.ts::renameUser|dataSource.manager.save")
        ],
        "removed": [
            (
                'src/server.ts::delete("/api/users/:id")|req.params',
                "src/lib/users.ts::removeUser|dataSource.manager.delete",
            )
        ],
        "rerouted": [('src/server.ts::post("/api/users")|req.body', "src/lib/users.ts::saveUser|manager.save")],
    },
    "spring": {
        "added": [
            (
                f"{J}ItemController.java::ItemController.rename|id",
                f"{J}ItemController.java::ItemController.rename|itemRepository.save",
            )
        ],
        "removed": [
            (
                f"{J}ItemController.java::ItemController.delete|id",
                f"{J}ItemController.java::ItemController.delete|itemRepository.delete",
            )
        ],
        "rerouted": [
            (
                f"{J}ItemController.java::ItemController.create|body",
                f"{J}ItemService.java::ItemService.create|itemRepository.save",
            )
        ],
    },
}
REROUTED_THROUGH = {
    "django": "shop/services.py::normalize",
    "express": "src/lib/text.ts::clean",
    "spring": f"{J}Names.java::Names.clean",
}


@pytest.mark.parametrize("framework", sorted(EXPECTED))
def test_real_binary_finds_one_added_removed_and_rerouted_flow(framework: str, real_env: dict[str, str]) -> None:
    base, head = FIXTURES / framework / "base", FIXTURES / framework / "head"
    report = dp.run_data_path(base, head, environ=real_env)
    assert report["state"] == "ran", report["summary"]
    found = {
        kind: [(row["source"]["symbol"], row["sink"]["symbol"]) for row in report["flows"][kind]]
        for kind in dp.CHANGE_KINDS
    }
    assert found == EXPECTED[framework]
    assert report["counts"] == {"added": 1, "removed": 1, "rerouted": 1, "unchanged": 1}
    assert report["summary"] == "1 added · 1 removed · 1 rerouted"
    assert report["caps"] == [] and report["engine"]["version"] == "v2.0.0"
    if sys.platform == "darwin":
        assert report["isolation"]["mode"] == "sandbox-exec"
    rerouted = report["flows"]["rerouted"][0]
    assert REROUTED_THROUGH[framework] in rerouted["head"]["path"]
    assert REROUTED_THROUGH[framework] not in rerouted["base"]["path"]
    for kind in dp.CHANGE_KINDS:
        for row in report["flows"][kind]:
            for side, root in (("base", base), ("head", head)):
                for step in row.get(side, {}).get("steps", ()):
                    rel, line = step["at"].rsplit(":", 1)
                    assert 1 <= int(line) <= len((root / rel).read_text().splitlines()), (side, step)


PROBES: dict[str, tuple[dict[str, str], set[str]]] = {
    "django": (
        {
            "app/views.py": (
                "from rest_framework import generics\n"
                "class ItemDetail(generics.RetrieveUpdateAPIView):\n"
                "    def perform_update(self, serializer):\n"
                "        serializer.save()\n"
                "    def patch(self, request, *args, **kwargs):\n"
                "        item = self.get_object()\n"
                "        item.archived = True\n"
                "        item.save()\n"
                "    def put(self, request, pk):\n"
                "        n = len(request.data)\n"
                "        audit_log.info(n)\n"
            )
        },
        {"serializer.save", "item.save"},
    ),
    "express": (
        {
            "app.ts": (
                'import express from "express";\n'
                "const router = express.Router();\n"
                'router.param("orgId", async (req, res, next, orgId) => {\n'
                '  await db.query("select 1 where id = ?", [orgId]);\n'
                "  next();\n"
                "});\n"
                'router.post("/x", async (req, res) => {\n'
                "  const row = new Row();\n"
                "  Object.assign(row, req.body);\n"
                "  await repo.save(row);\n"
                "});\n"
            )
        },
        {"db.query", "repo.save"},
    ),
    "spring": (
        {
            "C.java": (
                "import org.springframework.web.bind.annotation.*;\n"
                "public class C {\n"
                '    @PostMapping("/a/{id}")\n'
                "    public void a(@PathVariable long id) {\n"
                "        Item item = em.find(Item.class, id);\n"
                "        itemRepository.delete(item);\n"
                "    }\n"
                '    @PostMapping("/b/{id}")\n'
                "    public void b(@PathVariable long id) {\n"
                "        Item item = itemRepository.getReferenceById(id);\n"
                "        entityManager.remove(item);\n"
                "    }\n"
                "}\n"
            )
        },
        {"itemRepository.delete", "entityManager.remove"},
    ),
}


@pytest.mark.parametrize("framework", sorted(PROBES))
def test_real_binary_follows_selector_lookups_dispatch_and_sanitizers(
    framework: str, real_env: dict[str, str], tmp_path: Path
) -> None:
    files, callees = PROBES[framework]
    report = dp.run_data_path(_write(tmp_path / "base", {}), _write(tmp_path / "head", files), environ=real_env)
    assert report["state"] == "ran", report["summary"]
    # `audit_log.info(len(request.data))` is sanitized: the length no longer carries the request's data.
    assert {row["sink"]["callee"] for row in report["flows"]["added"]} == callees


def test_real_binary_through_the_review_api(
    tmp_path: Path, real_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (dp.BIN_ENV, SCAN_BIN_ENV, "PATH"):
        if key in real_env:
            monkeypatch.setenv(key, real_env[key])
    review = _api_review(tmp_path, "django")
    ran = _post(review, revision_id=review.revision_id, pick=True)
    assert ran.status_code == 200, ran.text
    assert ran.json()["summary"] == "1 added · 1 removed · 1 rerouted"
    stored = _map(review)
    assert stored["counts"] == {"added": 1, "removed": 1, "rerouted": 1, "unchanged": 1}
    assert [row["sink"]["symbol"] for row in stored["flows"]["added"]] == ["shop/views.py::rename|order.save"]


EVIL = (
    "from django.http import HttpResponse\n"
    "from shop.models import Widget\n\n\n"
    "def evil(request):\n"
    "    Widget.objects.create(name=request.POST['x'])\n"
    "    return HttpResponse('ok')\n"
)


def _django_head(tmp_path: Path, files: Mapping[str, str | bytes]) -> Path:
    head = tmp_path / "head"
    shutil.copytree(FIXTURES / "django" / "base", head)
    return _write(head, files)


def test_real_binary_scans_the_folders_its_own_ignore_list_would_skip(real_env: dict[str, str], tmp_path: Path) -> None:
    head = _django_head(tmp_path, {"shop/tests/__init__.py": "", "shop/tests/evil.py": EVIL})
    report = dp.run_data_path(FIXTURES / "django" / "base", head, environ=real_env, scratch_root=tmp_path)
    assert report["state"] == "ran", report["summary"]
    added = [(row["source"]["symbol"], row["sink"]["symbol"]) for row in report["flows"]["added"]]
    assert added == [("shop/tests/evil.py::evil|request.POST", "shop/tests/evil.py::evil|Widget.objects.create")]
    assert report["summary"] == "1 added · 0 removed · 0 rerouted"


def test_real_binary_never_removes_the_flows_of_a_file_head_did_not_read(
    real_env: dict[str, str], tmp_path: Path
) -> None:
    views = (FIXTURES / "django" / "base" / "shop" / "views.py").read_bytes()
    head = _django_head(tmp_path, {"shop/views.py": views + b"\n# \xff\n" + EVIL.split("\n\n\n", 1)[1].encode()})
    report = dp.run_data_path(FIXTURES / "django" / "base", head, environ=real_env, scratch_root=tmp_path)
    assert report["state"] == "partial", report["summary"]
    assert report["counts"]["removed"] == 0 and report["counts"]["added"] == 0
    assert report["summary"].startswith("Partial: no flows found in what was measured (")
    assert report["not_measured"] == [
        {"scope": "head: 1 paths: shop/views.py", "reason": "not UTF-8 text"},
        {
            "scope": "Django and Django REST framework: 3 flows through files the other side did not read or scan",
            "reason": "not diffed: neither added nor removed is known",
        },
    ]
