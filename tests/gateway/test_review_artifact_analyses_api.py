"""Artifact and infrastructure analyses through the Review API (Phase 9R R9: items 15, 31, 32, 33).

Real sandboxed runs (bubblewrap) cover SQL migrations, regenerate-and-compare,
Python packaging and Terraform; helm/kustomize/npm runs are gated on their
binaries. Run-plan gating, budget, reuse and poisoning use a scripted runner.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.gateway.test_review_check_runs_api import HEADERS, _compute_runs, _new_snapshot, _review
from tests.gateway.test_review_hermetic import needs_bwrap

DEEP = "review:\n  effort: deep\n"
# Assembled at runtime: no credential-shaped literal is ever committed.
AWS_KEY = "AKIA" + "QWERTYUIOPASDFGH"


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


@pytest.fixture
def hosted_report_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local runs with the hosted executor's report bound: only about 96 KB can be copied out."""

    from lemoncrow.pro.capabilities.review import artifact_analysis_api
    from lemoncrow.pro.capabilities.review.analysis_common import REPORT_LIMIT

    monkeypatch.setattr(artifact_analysis_api, "LOCAL_REPORT_LIMIT", REPORT_LIMIT)


def _blobs(folder: str, count: int = 4, size: int = 250_000) -> dict[str, str]:
    """Incompressible data generated at runtime: large enough to keep packed artifacts over the copy-out bound."""

    return {f"{folder}/blob{index}.txt": random.Random(index).randbytes(size).hex() + "\n" for index in range(count)}


def _sandbox_has(binary: str) -> bool:
    """The analyses run with the sandbox's system PATH; a host-only binary does not count."""

    return any(Path(directory, binary).is_file() for directory in ("/usr/local/bin", "/usr/bin", "/bin"))


def _sandbox_python_has(module: str) -> bool:
    if not Path("/usr/bin/python3").is_file():
        return False
    probe = subprocess.run(["/usr/bin/python3", "-c", f"import {module}"], capture_output=True, check=False)
    return probe.returncode == 0


needs_terraform = pytest.mark.skipif(not _sandbox_has("terraform"), reason="terraform is not installed for the sandbox")
needs_helm = pytest.mark.skipif(not _sandbox_has("helm"), reason="helm is not installed for the sandbox")
needs_kustomize = pytest.mark.skipif(
    not (_sandbox_has("kustomize") or _sandbox_has("kubectl")),
    reason="kustomize/kubectl is not installed for the sandbox",
)
needs_npm = pytest.mark.skipif(not _sandbox_has("npm"), reason="npm is not installed for the sandbox")
needs_setuptools = pytest.mark.skipif(
    not _sandbox_python_has("setuptools.build_meta"), reason="the sandbox python3 has no setuptools build backend"
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _analyses(review: SimpleNamespace, revision_id: str = "") -> dict[str, Any]:
    response = review.client.get(
        f"/api/reviews/{review.review_id}/artifact-analyses",
        headers=HEADERS,
        params={"revision_id": revision_id} if revision_id else {},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _run(review: SimpleNamespace, entry: str, *, revision_id: str = "", pick: bool = False) -> Any:
    return review.client.post(
        f"/api/reviews/{review.review_id}/artifact-analyses/run",
        headers=HEADERS,
        json={"revision_id": revision_id or review.revision_id, "entry": entry, "pick": pick},
    )


def _ok(review: SimpleNamespace, entry: str, **kwargs: Any) -> dict[str, Any]:
    response = _run(review, entry, **kwargs)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _record(review: SimpleNamespace, entry: str, spec: str = "") -> dict[str, Any]:
    rows = [row for row in _analyses(review)["results"] if row["entry_id"] == entry]
    if spec:
        rows = [row for row in rows if row["spec"]["id"] == spec]
    assert len(rows) == 1, rows
    return dict(rows[0])


def _rules(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f"{row['rule']}:{row['subject'].rsplit('::', 1)[-1]}": row for row in record["findings"]}


def _behavior(review: SimpleNamespace) -> list[dict[str, Any]]:
    response = review.client.get(f"/api/reviews/{review.review_id}/behavior", headers=HEADERS)
    assert response.status_code == 200, response.text
    payload = response.json()["behavior"] or {}
    degraded = payload.get("degraded") or []
    assert not [row for row in degraded if "artifact_analyses" in row], degraded
    return list(payload.get("targets") or [])


def _artifact(review: SimpleNamespace, record: dict[str, Any], name: str) -> dict[str, Any]:
    response = review.client.get(
        f"/api/reviews/{review.review_id}/artifact-analyses/artifact",
        headers=HEADERS,
        params={"evidence_id": record["evidence_id"], "name": name, "revision_id": review.revision_id},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _stored(review: SimpleNamespace) -> bytes:
    """Every byte LemonCrow stored as evidence or run artifact for this Review (not the Snapshot's own source)."""

    blob = b""
    for item in review.store.list_evidence(review.review_id):
        blob += (review.store.read_evidence_artifact(item) or b"") + item.detail.encode() + item.title.encode()
    for root, _dirs, files in os.walk(review.store_root / "review" / "run-artifacts"):
        for name in files:
            blob += Path(root, name).read_bytes()
    return blob


def _plan_entry(review: SimpleNamespace, entry_id: str) -> dict[str, Any] | None:
    response = review.client.get(f"/api/reviews/{review.review_id}/run-plan", headers=HEADERS)
    assert response.status_code == 200, response.text
    return next((row for row in response.json()["plan"]["entries"] if row["id"] == entry_id), None)


def _settings_ids(review: SimpleNamespace) -> set[str]:
    payload = review.client.get(f"/api/reviews/{review.review_id}/run-plan", headers=HEADERS).json()
    return {row["id"] for depth in payload["depths"]["depths"].values() for row in depth["entries"]}


def _reproduce_head(review: SimpleNamespace, record: dict[str, Any], tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.hermetic import REPRODUCE_COMMAND
    from lemoncrow.pro.capabilities.review.reproduce import load_execution_record, reproduce_execution

    assert record["reproduce"]["command"] == REPRODUCE_COMMAND
    head = next(row for row in record["attempts"] if row["side"] == "new")
    item = review.store.get_evidence(head["evidence_id"])
    attempt = json.loads(review.store.read_evidence_artifact(item))
    assert attempt["data"]["execution"]["reproduce"] == REPRODUCE_COMMAND
    outcome = reproduce_execution(
        load_execution_record(attempt), repo=review.repo_root, store_root=tmp_path / "reproduce"
    )
    assert outcome.reproduced, outcome.to_wire()


# --------------------------------------------------------------------------- #
# item 15: migrations
# --------------------------------------------------------------------------- #

MIGRATION_BASE = {
    ".lemoncrow/review.yaml": DEEP,
    "db/migrations/0001_init.up.sql": (
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, legacy TEXT);\n"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, total INTEGER);\n"
    ),
    "db/migrations/0001_init.down.sql": "DROP TABLE orders;\nDROP TABLE users;\n",
    "app.py": "print('app')\n",
}


@needs_bwrap
def test_destructive_and_lock_heavy_migrations_become_critical_behavior_deltas(tmp_path: Path) -> None:
    head = {
        "db/migrations/0002_tenant.up.sql": (
            "ALTER TABLE users DROP COLUMN legacy;\n"
            "ALTER TABLE users ADD COLUMN tenant_id INTEGER NOT NULL;\n"
            "CREATE INDEX orders_user ON orders(user_id);\n"
        ),
        "db/migrations/0002_tenant.down.sql": (
            "DROP INDEX orders_user;\nALTER TABLE users DROP COLUMN tenant_id;\nALTER TABLE users ADD COLUMN legacy TEXT;\n"
        ),
    }
    review = _review(tmp_path, base=MIGRATION_BASE, head=head)
    entries = {row["id"]: row for row in _analyses(review)["entries"]}
    # Item 41: the migration directory is also a persistence contract surface for mixed-version pairings.
    assert set(entries) == {"migrations.execute", "compat.mixed_version"}
    assert [spec["id"] for spec in entries["migrations.execute"]["specs"]] == ["db-migrations"]

    report = _ok(review, "migrations.execute")
    assert report["verdict"] == "PASS" and report["executions"] == 1  # head replays the trusted base itself
    record = _record(review, "migrations.execute")
    rules = _rules(record)
    assert rules["drop_column:users.legacy"]["critical"] is True
    assert rules["drop_column:users.legacy"]["paths"] == ["db/migrations/0002_tenant.up.sql"]
    assert rules["not_null_without_default:users.tenant_id"]["category"] == "lock_heavy"
    assert rules["non_concurrent_index:orders"]["category"] == "lock_heavy"
    assert record["summary"]["rollback"] == "verified"
    assert record["summary"]["schema_diff"]["columns_removed"] == ["users.legacy"]
    assert any("emulated" in reason for reason in record["reasons"])
    # Evidence before judgment: critical migration targets in the canonical queue, citing the record.
    targets = [row for row in _behavior(review) if row["kind"] == "migration"]
    by_title = {row["title"]: row for row in targets}
    dropped = by_title["Drops a column · users.legacy"]
    assert dropped["critical"] is True
    detail = review.client.get(
        f"/api/reviews/{review.review_id}/behavior/targets/{dropped['behavior_key']}", headers=HEADERS
    )
    assert detail.status_code == 200, detail.text
    refs = [ref["ref"] for ref in detail.json()["target"]["evidence"]["refs"]]
    assert f"artifact:{record['evidence_id']}" in refs
    # Heavy artifacts: the schema diff is stored redacted and readable while referenced or fresh.
    diff = _artifact(review, record, "schema.diff")
    assert diff["expired"] is False and "tenant_id" in diff["text"]
    assert _compute_runs(review) == {"migrations.execute@local": 1}
    verdict = review.store.get_evidence(report["verdict_evidence"])
    assert (verdict.stable_key, verdict.verification_status, verdict.executor_class) == (
        "migrations.execute",
        "PASS",
        "author_machine",
    )
    _reproduce_head(review, record, tmp_path)


@needs_bwrap
def test_a_safe_additive_migration_is_a_schema_diff_delta_only(tmp_path: Path) -> None:
    head = {
        "db/migrations/0002_audit.up.sql": (
            "CREATE TABLE audit (id INTEGER PRIMARY KEY, note TEXT);\nALTER TABLE users ADD COLUMN nickname TEXT;\n"
        ),
        "db/migrations/0002_audit.down.sql": "ALTER TABLE users DROP COLUMN nickname;\nDROP TABLE audit;\n",
    }
    review = _review(tmp_path, base=MIGRATION_BASE, head=head)
    assert _ok(review, "migrations.execute")["verdict"] == "PASS"
    record = _record(review, "migrations.execute")
    assert [row["rule"] for row in record["findings"]] == ["schema_diff"]
    assert record["summary"]["destructive"] == 0 and record["summary"]["lock_heavy"] == 0
    targets = [row for row in _behavior(review) if row["kind"] == "migration"]
    assert len(targets) == 1 and targets[0]["critical"] is False and targets[0]["severity"] == "low"


@needs_bwrap
def test_a_broken_down_migration_fails_rollback_verification(tmp_path: Path) -> None:
    head = {
        "db/migrations/0002_note.up.sql": "ALTER TABLE users ADD COLUMN note TEXT;\n",
        # The down migration targets a table that does not exist: no engine can roll this back.
        "db/migrations/0002_note.down.sql": "ALTER TABLE customers DROP COLUMN note;\n",
    }
    review = _review(tmp_path, base=MIGRATION_BASE, head=head)
    assert _ok(review, "migrations.execute")["verdict"] == "FAIL"
    record = _record(review, "migrations.execute")
    assert record["summary"]["rollback"] == "failed"
    assert _rules(record)["rollback_failed:rollback"]["critical"] is True

    mismatch = _review(
        tmp_path / "mismatch",
        base=MIGRATION_BASE,
        head={
            "db/migrations/0002_note.up.sql": "ALTER TABLE users ADD COLUMN note TEXT;\n",
            "db/migrations/0002_note.down.sql": "SELECT 1;\n",
        },
    )
    assert _ok(mismatch, "migrations.execute")["verdict"] == "FAIL"
    assert _record(mismatch, "migrations.execute")["summary"]["rollback"] == "mismatch"


def test_nothing_to_act_on_is_neither_run_nor_shown(tmp_path: Path) -> None:
    # Notes under migrations/ and a reusable Terraform module are not something to execute.
    base = {
        ".lemoncrow/review.yaml": DEEP,
        "docs/migrations/2026-upgrade.md": "# notes\n",
        "modules/net/main.tf": 'resource "terraform_data" "x" {\n  input = 1\n}\n',
        "app.py": "print(1)\n",
    }
    review = _review(tmp_path, base=base, head={"app.py": "print(2)\n"})
    assert _analyses(review)["entries"] == []
    for entry in ("migrations.execute", "artifacts.regenerate", "artifacts.shipped", "infra.render"):
        planned = _plan_entry(review, entry)
        assert planned is None or planned["status"] == "inapplicable", planned
        refused = _run(review, entry)
        assert refused.status_code == 409, refused.text
    assert not _settings_ids(review) & {
        "migrations.execute",
        "artifacts.regenerate",
        "artifacts.shipped",
        "infra.render",
    }
    assert not [row for row in _behavior(review) if row["kind"] in {"migration", "artifact", "infra"}]


def test_detected_frameworks_without_an_executor_are_honestly_not_available(tmp_path: Path) -> None:
    base = {
        ".lemoncrow/review.yaml": DEEP,
        "app/alembic/versions/0001_init.py": "revision = '0001'\n",
        "Dockerfile": "FROM scratch\n",
    }
    review = _review(tmp_path, base=base, head={"app/alembic/versions/0002_more.py": "revision = '0002'\n"})
    entries = {row["id"]: row for row in _analyses(review)["entries"]}
    assert set(entries) == {"migrations.execute", "artifacts.shipped", "compat.mixed_version"}
    report = _ok(review, "migrations.execute")
    assert report["verdict"] == "NOT_RUN" and report["executions"] == 0
    result = report["results"][0]
    assert result["status"] == "NOT_RUN" and "Alembic" in result["setup_hint"]
    shipped = _ok(review, "artifacts.shipped")
    assert shipped["verdict"] == "NOT_RUN" and "Container" in shipped["results"][0]["setup_hint"]
    # Never PASS without having run.
    verdicts = {row["stable_key"]: row["status"] for row in _analyses(review)["verdicts"]}
    assert verdicts == {"migrations.execute": "NOT_RUN", "artifacts.shipped": "NOT_RUN"}


# --------------------------------------------------------------------------- #
# item 31: regenerate and compare
# --------------------------------------------------------------------------- #

GEN = (
    "import json, pathlib\n"
    "names = json.loads(pathlib.Path('schema.json').read_text())\n"
    "pathlib.Path('generated').mkdir(exist_ok=True)\n"
    "for name in names:\n"
    "    pathlib.Path('generated/' + name + '.py').write_text('NAME = %r\\n' % name)\n"
)
REGEN_CONFIG = (
    DEEP + "analyses:\n  regenerate:\n    - id: models\n      command: [python3, gen.py]\n"
    "      outputs: ['generated/*.py']\n      generator: [gen.py]\n"
)


@needs_bwrap
def test_a_hand_edited_generated_file_is_flagged_and_an_untouched_one_is_not(tmp_path: Path) -> None:
    base = {
        ".lemoncrow/review.yaml": REGEN_CONFIG,
        "gen.py": GEN,
        "schema.json": '["a", "b"]',
        "generated/a.py": "NAME = 'a'\n",
        "generated/b.py": "NAME = 'b'\n",
    }
    # The head also rewrites the generator to emit its hand edit: only the trusted-base generator runs.
    head = {
        "generated/a.py": "NAME = 'a'  # tweaked by hand\n",
        "gen.py": GEN.replace("'NAME = %r\\n' % name", "'NAME = %r  # tweaked by hand\\n' % name"),
    }
    review = _review(tmp_path, base=base, head=head)
    report = _ok(review, "artifacts.regenerate")
    assert report["verdict"] == "FAIL" and report["executions"] == 2
    record = _record(review, "artifacts.regenerate")
    flagged = {row["paths"][0]: row for row in record["findings"]}
    assert set(flagged) == {"generated/a.py"}
    assert flagged["generated/a.py"]["rule"] == "hand_edited"
    assert record["summary"]["generator_edited"] == ["gen.py"]
    targets = [row for row in _behavior(review) if row["kind"] == "artifact"]
    assert [row["title"] for row in targets] == ["Hand-edited generated file · generated/a.py"]
    assert "tweaked by hand" in _artifact(review, record, "regenerate.diff")["text"]
    _reproduce_head(review, record, tmp_path)


@needs_bwrap
def test_generated_files_that_match_their_generator_raise_nothing(tmp_path: Path) -> None:
    base = {
        ".lemoncrow/review.yaml": REGEN_CONFIG,
        "gen.py": GEN,
        "schema.json": '["a"]',
        "generated/a.py": "NAME = 'a'\n",
    }
    head = {"schema.json": '["a", "b"]', "generated/b.py": "NAME = 'b'\n"}
    review = _review(tmp_path, base=base, head=head)
    assert _ok(review, "artifacts.regenerate")["verdict"] == "PASS"
    assert _record(review, "artifacts.regenerate")["findings"] == []


# (c) The trusted-base generator imports a module the head may change. That module leaves a
# process behind which, once the generator exited, makes the hand-edited file look regenerated:
# by rewriting the regenerated output, or by forging the report and killing the launcher.
MODEL_HIDES_HAND_EDIT = r"""
import base64, hashlib, json, os, signal, time

NAMES = ["a"]
HAND_EDIT = b"NAME = 'a'  # tweaked by hand\n"
MODE = {mode!r}

if not os.path.exists("/tmp/hide-hand-edit"):
    open("/tmp/hide-hand-edit", "w").close()
    step, launcher, cwd = os.getpid(), os.getppid(), os.getcwd()
    if os.fork() == 0:
        os.setsid()
        os.kill(launcher, signal.SIGSTOP)
        while True:
            try:
                with open("/proc/%d/stat" % step) as handle:
                    if handle.read().rsplit(")", 1)[1].split()[0] == "Z":
                        break
            except OSError:
                break
            time.sleep(0.01)
        if MODE == "rewrite":
            with open(os.path.join(cwd, "generated", "a.py"), "wb") as handle:
                handle.write(HAND_EDIT)
            os.kill(launcher, signal.SIGCONT)
        else:
            digest = hashlib.sha256(HAND_EDIT).hexdigest()
            report = {{
                "v": 1, "exit": 0, "seal": {{"v": 1, "started": True, "steps": 1, "tampered": []}},
                "before": {{"generated/a.py": digest}}, "generator": {{"exit": 0, "output": ""}},
                "outputs": [{{"path": "generated/a.py", "size": len(HAND_EDIT), "sha256": digest,
                              "b64": base64.b64encode(HAND_EDIT).decode()}}],
                "omitted": [], "notes": [], "heavy": {{}},
            }}
            with open("/tmp/lemoncrow-reports/report", "w") as handle:
                handle.write(json.dumps(report).encode().hex())
            os.kill(launcher, signal.SIGKILL)
        os._exit(0)
"""
GEN_IMPORTS_MODEL = (
    "import pathlib\n"
    "import model\n"
    "pathlib.Path('generated').mkdir(exist_ok=True)\n"
    "for name in model.NAMES:\n"
    "    pathlib.Path('generated/' + name + '.py').write_text('NAME = %r\\n' % name)\n"
)


@needs_bwrap
@pytest.mark.parametrize(
    ("mode", "reason"),
    [("rewrite", "the generator interfered with the analysis"), ("forge", "but its report claims 0")],
)
def test_a_generator_input_cannot_make_a_hand_edited_file_look_regenerated(
    tmp_path: Path, mode: str, reason: str
) -> None:
    base = {
        ".lemoncrow/review.yaml": REGEN_CONFIG,
        "gen.py": GEN_IMPORTS_MODEL,
        "model.py": 'NAMES = ["a"]\n',
        "generated/a.py": "NAME = 'a'\n",
    }
    head = {"generated/a.py": "NAME = 'a'  # tweaked by hand\n", "model.py": MODEL_HIDES_HAND_EDIT.format(mode=mode)}
    review = _review(tmp_path, base=base, head=head)
    report = _ok(review, "artifacts.regenerate")
    record = _record(review, "artifacts.regenerate")
    assert report["verdict"] == "UNKNOWN", record
    assert any(reason in row for row in record["reasons"]), record["reasons"]
    verdicts = {row["stable_key"]: row["status"] for row in _analyses(review)["verdicts"]}
    assert verdicts["artifacts.regenerate"] == "UNKNOWN"


@needs_bwrap
def test_a_hand_edit_is_flagged_when_the_generated_outputs_are_too_large_to_copy_out(
    tmp_path: Path, hosted_report_limit: None
) -> None:
    big = "DATA = %r\n" % ("0123456789abcdef" * 20000)  # about 320 KB, regenerated identically
    gen = GEN + "pathlib.Path('generated/big.py').write_text('DATA = %r\\n' % ('0123456789abcdef' * 20000))\n"
    base = {
        ".lemoncrow/review.yaml": REGEN_CONFIG,
        "gen.py": gen,
        "schema.json": '["a"]',
        "generated/a.py": "NAME = 'a'\n",
        "generated/big.py": big,
    }
    review = _review(tmp_path, base=base, head={"generated/a.py": "NAME = 'a'  # tweaked by hand\n"})
    report = _ok(review, "artifacts.regenerate")
    record = _record(review, "artifacts.regenerate")
    assert report["verdict"] == "FAIL", record["reasons"]
    assert record["summary"]["inspection"] == {"head": "sealed_in_sandbox", "base": "sealed_in_sandbox"}
    flagged = {row["paths"][0]: row["rule"] for row in record["findings"]}
    assert flagged == {"generated/a.py": "hand_edited"}
    assert record["summary"]["counts"]["clean"] == 1 and record["summary"]["files"] == 2
    assert "tweaked by hand" in _artifact(review, record, "regenerate.diff")["text"]


def test_head_only_generators_are_never_configured(tmp_path: Path) -> None:
    base = {".lemoncrow/review.yaml": DEEP, "gen.py": GEN, "schema.json": '["a"]'}
    head = {".lemoncrow/review.yaml": REGEN_CONFIG, "generated/a.py": "NAME = 'a'\n"}
    review = _review(tmp_path, base=base, head=head)
    assert _analyses(review)["entries"] == []
    assert _run(review, "artifacts.regenerate", pick=True).status_code == 409


# --------------------------------------------------------------------------- #
# item 32: shipped artifacts
# --------------------------------------------------------------------------- #

PYPROJECT = (
    '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n\n'
    '[project]\nname = "demo-pkg"\nversion = "0.1.0"\n\n[tool.setuptools]\npackages = ["demo_pkg"]\n'
)


@needs_bwrap
@needs_setuptools
def test_shipped_python_artifacts_show_new_files_api_and_never_store_secret_values(tmp_path: Path) -> None:
    base = {
        ".lemoncrow/review.yaml": DEEP,
        "pyproject.toml": PYPROJECT,
        "demo_pkg/__init__.py": '__all__ = ["alpha", "beta"]\n\n\ndef alpha():\n    return 1\n\n\ndef beta():\n    return 2\n',
    }
    head = {
        "demo_pkg/__init__.py": '__all__ = ["alpha", "gamma"]\n\n\ndef alpha():\n    return 1\n\n\ndef gamma():\n    return 3\n',
        "demo_pkg/settings.py": f'AWS_KEY = "{AWS_KEY}"\n',
        "MANIFEST.in": "include demo_pkg/.env\ninclude demo_pkg/fixtures/sample.json\n",
        "demo_pkg/.env": f"DEPLOY_KEY={AWS_KEY}\n",
        "demo_pkg/fixtures/sample.json": '{"rows": [1, 2, 3]}\n' * 40,
    }
    review = _review(tmp_path, base=base, head=head)
    report = _ok(review, "artifacts.shipped")
    assert report["verdict"] == "FAIL" and report["executions"] == 2
    record = _record(review, "artifacts.shipped")
    secrets = {
        (row["paths"][0], row["subject"].rsplit("::", 1)[-1]) for row in record["findings"] if row["rule"] == "secret"
    }
    assert secrets == {
        ("demo_pkg/.env", "dotenv-file"),
        ("demo_pkg/.env", "aws-access-key-id"),
        ("demo_pkg/settings.py", "aws-access-key-id"),
    }
    new_files = {row["paths"][0] for row in record["findings"] if row["rule"] == "new_file"}
    assert new_files == {"demo_pkg/fixtures/sample.json"}
    api = {row["rule"]: row for row in record["findings"] if row["rule"].startswith("api_")}
    assert set(api) == {"api_removed", "api_added"} and "demo_pkg.beta" in api["api_removed"]["summary"]
    sizes = {row["kind"]: row for row in record["summary"]["artifacts"]}
    assert set(sizes) == {"sdist", "wheel"}
    assert sizes["wheel"]["bytes"]["head"] > sizes["wheel"]["bytes"]["base"]
    targets = [row for row in _behavior(review) if row["kind"] == "artifact"]
    assert sum(1 for row in targets if row["critical"]) == 3
    # Shown by path + rule, never the value: nothing LemonCrow stored or serves carries it.
    assert AWS_KEY.encode() not in _stored(review)
    assert AWS_KEY not in json.dumps(_analyses(review)) and AWS_KEY not in json.dumps(_behavior(review))
    assert AWS_KEY not in _artifact(review, record, "contents.diff")["text"]
    _reproduce_head(review, record, tmp_path)


# Reviewed build code that tries to hide a shipped .env from the analysis. It freezes the
# analysis launcher (its parent), waits for the build step to exit, then alters what the
# analysis would read. The launcher is never trusted to have been left alone.
_FREEZE_AND_STRIP = r"""
import base64, hashlib, io, json, os, signal, tarfile, time, zipfile


def frozen_until_exit(launcher, step):
    os.kill(launcher, signal.SIGSTOP)
    while True:
        try:
            with open("/proc/%d/stat" % step) as handle:
                if handle.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return
        except OSError:
            return
        time.sleep(0.01)


def strip_env(path):
    if path.endswith(".whl"):
        with zipfile.ZipFile(path) as old:
            rows = [(info, old.read(info)) for info in old.infolist() if not info.filename.endswith(".env")]
        with zipfile.ZipFile(path, "w") as new:
            for info, data in rows:
                new.writestr(info, data)
    elif path.endswith(".tar.gz"):
        with tarfile.open(path) as old:
            rows = [(m, old.extractfile(m).read() if m.isfile() else None) for m in old.getmembers()
                    if not m.name.endswith(".env")]
        with tarfile.open(path, "w:gz") as new:
            for member, data in rows:
                new.addfile(member, io.BytesIO(data) if data is not None else None)


def forged_report(outputs):
    return json.dumps({
        "v": 1, "entry": "artifacts.shipped", "spec": "", "exit": 0, "builder": "python",
        "build": {"exit": 0, "output": ""}, "seal": {"v": 1, "started": True, "steps": 2, "tampered": []},
        "outputs": outputs, "omitted": [], "notes": [], "heavy": {},
    }).encode().hex()


def copied(folder):
    rows = []
    for name in sorted(os.listdir(folder)):
        data = open(os.path.join(folder, name), "rb").read()
        rows.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                     "b64": base64.b64encode(data).decode()})
    return rows


# setuptools runs setup.py inside the PEP 517 hook process: the step; its parent launched it.
STEP, LAUNCHER = os.getpid(), os.getppid()
DIST = open("/proc/%d/cmdline" % STEP, "rb").read().split(b"\0")[-2].decode()
"""

# (a) A background process strips the .env from the built archives once the build exited.
SETUP_HIDES_ENV = _FREEZE_AND_STRIP + r"""
if not os.path.exists("/tmp/hide-env"):
    open("/tmp/hide-env", "w").close()
    if os.fork() == 0:
        os.setsid()
        frozen_until_exit(LAUNCHER, STEP)
        for name in os.listdir(DIST):
            strip_env(os.path.join(DIST, name))
        os.kill(LAUNCHER, signal.SIGCONT)
        os._exit(0)

from setuptools import setup
setup()
"""

# (b) The build prints a forged report, and a background process writes one (clean archives,
# a claimed seal) where the runner reads reports, then kills the launcher before it writes its own.
SETUP_FORGES_REPORT = _FREEZE_AND_STRIP + r"""
print(forged_report([]))
print(json.dumps({"v": 1, "exit": 0, "artifacts": [], "secrets": [], "api": {}}))
if not os.path.exists("/tmp/forge-report"):
    open("/tmp/forge-report", "w").close()
    if os.fork() == 0:
        os.setsid()
        frozen_until_exit(LAUNCHER, STEP)
        for name in os.listdir(DIST):
            strip_env(os.path.join(DIST, name))
        with open("/tmp/lemoncrow-reports/report", "w") as handle:
            handle.write(forged_report(copied(DIST)))
        os.kill(LAUNCHER, signal.SIGKILL)
        os._exit(0)

from setuptools import setup
setup()
"""


def _hiding_review(tmp_path: Path, setup_py: str, *, large: bool = False) -> SimpleNamespace:
    data = "recursive-include demo_pkg/data *.txt\n" if large else ""
    base = {
        ".lemoncrow/review.yaml": DEEP,
        "pyproject.toml": PYPROJECT,
        "demo_pkg/__init__.py": "def alpha():\n    return 1\n",
        **({"MANIFEST.in": data, **_blobs("demo_pkg/data")} if large else {}),
    }
    head = {"setup.py": setup_py, "MANIFEST.in": data + "include demo_pkg/.env\n", "demo_pkg/.env": "DEBUG=1\n"}
    return _review(tmp_path, base=base, head=head)


@needs_bwrap
@needs_setuptools
def test_a_package_too_large_to_copy_out_is_inspected_in_the_sealed_sandbox(
    tmp_path: Path, hosted_report_limit: None
) -> None:
    manifest = "recursive-include demo_pkg/data *.txt\n"
    base = {
        ".lemoncrow/review.yaml": DEEP,
        "pyproject.toml": PYPROJECT,
        "MANIFEST.in": manifest,
        "demo_pkg/__init__.py": '__all__ = ["alpha", "beta"]\n\n\ndef alpha():\n    return 1\n\n\ndef beta():\n    return 2\n',
        **_blobs("demo_pkg/data"),
    }
    head = {
        "demo_pkg/__init__.py": '__all__ = ["alpha", "gamma"]\n\n\ndef alpha():\n    return 1\n\n\ndef gamma():\n    return 3\n',
        "demo_pkg/settings.py": f'AWS_KEY = "{AWS_KEY}"\n',
        "MANIFEST.in": manifest + "include demo_pkg/.env\n",
        "demo_pkg/.env": f"DEPLOY_KEY={AWS_KEY}\n",
    }
    review = _review(tmp_path, base=base, head=head)
    report = _ok(review, "artifacts.shipped")
    record = _record(review, "artifacts.shipped")
    assert report["verdict"] == "FAIL", record["reasons"]
    # Both sides were inspected by the sealed driver, and the record says so.
    assert record["summary"]["inspection"] == {"head": "sealed_in_sandbox", "base": "sealed_in_sandbox"}
    sizes = {row["kind"]: row["bytes"] for row in record["summary"]["artifacts"]}
    assert set(sizes) == {"sdist", "wheel"} and min(size["head"] for size in sizes.values()) > 1_000_000
    secrets = {
        (row["paths"][0], row["subject"].rsplit("::", 1)[-1]) for row in record["findings"] if row["rule"] == "secret"
    }
    assert secrets == {
        ("demo_pkg/.env", "dotenv-file"),
        ("demo_pkg/.env", "aws-access-key-id"),
        ("demo_pkg/settings.py", "aws-access-key-id"),
    }
    api = {row["rule"]: row for row in record["findings"] if row["rule"].startswith("api_")}
    assert set(api) == {"api_removed", "api_added"} and "demo_pkg.beta" in api["api_removed"]["summary"]
    assert AWS_KEY.encode() not in _stored(review)


@needs_bwrap
@needs_setuptools
def test_a_build_process_cannot_hide_a_shipped_env_from_the_in_sandbox_inspection(
    tmp_path: Path, hosted_report_limit: None
) -> None:
    review = _hiding_review(tmp_path, SETUP_HIDES_ENV, large=True)
    report = _ok(review, "artifacts.shipped")
    record = _record(review, "artifacts.shipped")
    assert report["verdict"] == "UNKNOWN", record
    stated = [reason for reason in record["reasons"] if "in-sandbox inspection is not trusted" in reason]
    assert stated and "interfered with the analysis" in stated[0], record["reasons"]


@needs_bwrap
@needs_setuptools
def test_a_build_process_that_alters_the_artifacts_after_the_build_cannot_hide_a_shipped_env(tmp_path: Path) -> None:
    review = _hiding_review(tmp_path, SETUP_HIDES_ENV)
    report = _ok(review, "artifacts.shipped")
    record = _record(review, "artifacts.shipped")
    # The .env bytes are gone from what was copied out, but the run is never clean.
    assert report["verdict"] == "UNKNOWN", record
    assert any("interfered with the analysis" in reason for reason in record["reasons"]), record["reasons"]
    verdicts = {row["stable_key"]: row["status"] for row in _analyses(review)["verdicts"]}
    assert verdicts["artifacts.shipped"] == "UNKNOWN"


@needs_bwrap
@needs_setuptools
def test_a_forged_report_from_the_build_is_ignored(tmp_path: Path) -> None:
    review = _hiding_review(tmp_path, SETUP_FORGES_REPORT)
    report = _ok(review, "artifacts.shipped")
    record = _record(review, "artifacts.shipped")
    assert report["verdict"] == "UNKNOWN", record
    assert any("but its report claims 0" in reason for reason in record["reasons"]), record["reasons"]


@needs_bwrap
@needs_npm
def test_shipped_npm_package_shows_bundle_size_and_exports(tmp_path: Path) -> None:
    manifest = '{"name": "demo-lib", "version": "1.0.0", "main": "index.js"}\n'
    base = {
        ".lemoncrow/review.yaml": DEEP,
        "package.json": manifest,
        "index.js": "export function alpha() { return 1; }\n",
    }
    head = {
        "index.js": "export function alpha() { return 1; }\nexport function beta() { return 2; }\n" + "// pad\n" * 4000,
        "test/fixtures/big.json": "{}\n",
    }
    review = _review(tmp_path, base=base, head=head)
    report = _ok(review, "artifacts.shipped")
    assert report["verdict"] == "PASS", report
    record = _record(review, "artifacts.shipped")
    rules = {row["rule"] for row in record["findings"]}
    assert {"size", "api_added", "new_file"} <= rules, record["findings"]
    assert record["summary"]["inspection"] == {"head": "host", "base": "host"}


@needs_bwrap
@needs_npm
def test_an_npm_tarball_too_large_to_copy_out_is_inspected_in_the_sealed_sandbox(
    tmp_path: Path, hosted_report_limit: None
) -> None:
    manifest = '{"name": "demo-lib", "version": "1.0.0", "main": "index.js"}\n'
    base = {
        ".lemoncrow/review.yaml": DEEP,
        "package.json": manifest,
        "index.js": "export function alpha() { return 1; }\n",
        **_blobs("data"),
    }
    head = {
        "index.js": "export function alpha() { return 1; }\nexport function beta() { return 2; }\n",
        "test/fixtures/big.json": "{}\n",
    }
    review = _review(tmp_path, base=base, head=head)
    report = _ok(review, "artifacts.shipped")
    record = _record(review, "artifacts.shipped")
    assert report["verdict"] == "PASS", record["reasons"]
    assert record["summary"]["inspection"] == {"head": "sealed_in_sandbox", "base": "sealed_in_sandbox"}
    assert record["summary"]["artifacts"][0]["bytes"]["head"] > 1_000_000
    rules = {row["rule"]: row for row in record["findings"]}
    assert {"api_added", "new_file"} <= set(rules), record["findings"]
    assert "beta" in rules["api_added"]["summary"]
    assert rules["new_file"]["paths"] == ["test/fixtures/big.json"]


# --------------------------------------------------------------------------- #
# item 33: infrastructure
# --------------------------------------------------------------------------- #

TF_BASE = 'variable "leak" {\n  type    = string\n  default = "none"\n}\n\nresource "terraform_data" "web" {\n  input = "v1"\n}\n'


@needs_bwrap
@needs_terraform
def test_terraform_plans_credential_free_never_applies_and_strips_host_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TF_VAR_leak", "host-tf-var-value")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "host-aws-secret-value")
    monkeypatch.setenv("TF_CLI_ARGS", "-auto-approve")
    head = {
        "infra/main.tf": TF_BASE.replace('"v1"', '"v2-${var.leak}"')
        + '\nresource "terraform_data" "db" {\n  input = "x"\n}\n',
    }
    review = _review(tmp_path, base={".lemoncrow/review.yaml": DEEP, "infra/main.tf": TF_BASE}, head=head)
    report = _ok(review, "infra.render")
    assert report["verdict"] == "PASS" and report["executions"] == 2
    record = _record(review, "infra.render")
    assert record["summary"]["applied"] is False and record["summary"]["actions"] == {"create": 2}
    assert record["summary"]["cost"]["label"] == "estimate"
    titles = {row["title"] for row in _behavior(review) if row["kind"] == "infra"}
    assert titles == {"Planned resource changed · terraform_data.web", "Planned resource added · terraform_data.db"}
    plan = _artifact(review, record, "plan.txt")["text"]
    assert "v2-none" in plan  # the host's TF_VAR_leak never reached the sandbox
    stored = _stored(review)
    assert b"host-tf-var-value" not in stored and b"host-aws-secret-value" not in stored
    # The executed command lines: the closed plan-only table, never apply/destroy.
    for attempt in record["attempts"]:
        item = review.store.get_evidence(attempt["evidence_id"])
        steps = json.loads(review.store.read_evidence_artifact(item))["data"]["analysis"]["report"]["steps"]
        assert [step[0] for step in steps] == ["version", "init", "plan", "show"]
        assert not {"apply", "destroy", "-auto-approve"} & {part for step in steps for part in step}
    _reproduce_head(review, record, tmp_path)


@needs_bwrap
@needs_terraform
def test_terraform_without_a_provider_mirror_is_not_available_not_pass(tmp_path: Path) -> None:
    main = 'provider "aws" {\n  region = "us-east-1"\n}\n\nresource "aws_instance" "web" {\n  instance_type = "t3.micro"\n}\n'
    review = _review(
        tmp_path,
        base={".lemoncrow/review.yaml": DEEP, "infra/main.tf": main},
        head={"infra/main.tf": main.replace("t3.micro", "m5.large")},
    )
    report = _ok(review, "infra.render")
    assert report["verdict"] == "NOT_RUN"
    result = report["results"][0]
    assert result["status"] == "NOT_RUN" and "provider_mirror" in result["setup_hint"]


CHART = {
    "chart/Chart.yaml": "apiVersion: v2\nname: demo\nversion: 0.1.0\n",
    "chart/values.yaml": "replicas: 1\n",
    "chart/templates/deploy.yaml": (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: demo\nspec:\n  replicas: {{ .Values.replicas }}\n"
    ),
}


@needs_bwrap
@needs_helm
def test_helm_change_shows_a_rendered_diff(tmp_path: Path) -> None:
    review = _review(
        tmp_path, base={".lemoncrow/review.yaml": DEEP, **CHART}, head={"chart/values.yaml": "replicas: 3\n"}
    )
    assert _ok(review, "infra.render")["verdict"] == "PASS"
    record = _record(review, "infra.render")
    assert "replicas: 3" in _artifact(review, record, "rendered.diff")["text"]


@needs_bwrap
@needs_kustomize
def test_kustomize_change_shows_a_rendered_diff(tmp_path: Path) -> None:
    base = {
        ".lemoncrow/review.yaml": DEEP,
        "deploy/kustomization.yaml": "resources:\n  - cm.yaml\n",
        "deploy/cm.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app\ndata:\n  mode: base\n",
    }
    review = _review(tmp_path, base=base, head={"deploy/cm.yaml": base["deploy/cm.yaml"].replace("base", "head")})
    assert _ok(review, "infra.render")["verdict"] == "PASS"
    assert "mode: head" in _artifact(review, _record(review, "infra.render"), "rendered.diff")["text"]


# --------------------------------------------------------------------------- #
# planned execution, budget, reuse and poisoning (scripted runner)
# --------------------------------------------------------------------------- #

KUSTOMIZE_BASE = {
    "deploy/kustomization.yaml": "resources:\n  - cm.yaml\n",
    "deploy/cm.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app\ndata:\n  mode: base\n",
    "app.py": "print(1)\n",
}


def _rendered(mode: str) -> str:
    return f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app\ndata:\n  mode: {mode}\n"


class _Scripted:
    """A ``command`` runner returning a scripted analysis report with a typed execution."""

    runner_id = "command"

    def __init__(self, script: Callable[[Any, str, int], tuple[int, dict[str, Any]]]) -> None:
        self.script = script
        self.calls: list[tuple[str, str]] = []

    def setup_candidates(self, _repo_root: Path) -> tuple[()]:
        return ()

    def run(self, context: Any, runtime: Any, surface: Any, *, side: str) -> Any:
        from lemoncrow.pro.environment.execution import ExecutionSubject, StageOutcome
        from lemoncrow.pro.environment.runners import SurfaceRunResult
        from tests.gateway.test_review_execution_contract import _result

        self.calls.append((runtime.id, side))
        exit_code, report = self.script(runtime, side, len(self.calls))
        execution = _result(
            subject=ExecutionSubject(
                review_id=context.revision.review_id, revision_id=context.revision.id, side=side, snapshot_identity="t"
            ),
            stages=(
                StageOutcome("run", "passed" if exit_code == 0 else "failed", exit_code=exit_code, duration_ms=1000),
            ),
        ).to_wire()
        return SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            "passed" if exit_code == 0 else "failed",
            "scripted",
            self.runner_id,
            runtime.id,
            exit_code=exit_code,
            duration_ms=100,
            data={"execution": execution, "report": json.dumps(report).encode().hex(), "report_truncated": False},
        )


def _render_script(runtime: Any, side: str, _count: int) -> tuple[int, dict[str, Any]]:
    return 0, {"tool": "kustomize", "heavy": {"rendered.yaml": _rendered("base" if side == "old" else "head")}}


def _scripted_review(
    tmp_path: Path, script: Any = _render_script, *, config: str = DEEP, head: dict[str, str | None] | None = None
) -> tuple[SimpleNamespace, _Scripted]:
    from lemoncrow.pro.environment.runners import RunnerRegistry

    runner = _Scripted(script)
    review = _review(
        tmp_path,
        base={".lemoncrow/review.yaml": config, **KUSTOMIZE_BASE},
        head=head if head is not None else {"deploy/cm.yaml": _rendered("head")},
        runner_registry=RunnerRegistry((runner,)),
    )
    return review, runner


def test_analyses_run_only_as_planned_and_a_pick_runs_as_an_amendment(tmp_path: Path) -> None:
    review, runner = _scripted_review(tmp_path / "auto", config="review:\n  effort: auto\n")
    assert _run(review, "infra.unknown").status_code == 422
    # Auto does not schedule infrastructure renders: refused until a reviewer picks it.
    unplanned = _run(review, "infra.render")
    assert unplanned.status_code == 409 and "not in the effective run plan" in unplanned.json()["detail"]
    assert runner.calls == [] and _compute_runs(review) == {}
    entry = next(row for row in _analyses(review)["entries"] if row["id"] == "infra.render")
    assert entry["status"] in {"not_planned", "on_request"}
    picked = _ok(review, "infra.render", pick=True)
    assert picked["plan_entry"]["status"] == "run" and picked["plan_entry"]["origin"] == "manual"
    assert picked["verdict"] == "PASS"
    assert sorted(side for _runtime, side in runner.calls) == ["new", "old"]
    assert _compute_runs(review) == {"infra.render@local": 2}
    record = _record(review, "infra.render")
    assert [row["title"] for row in record["findings"]] == ["Rendered resource changed · v1/ConfigMap/app"]

    # A per-Review pick through Run specific… names the analysis follow-up action.
    other, _other_runner = _scripted_review(tmp_path / "specific", config="review:\n  effort: auto\n")
    specific = other.client.post(
        f"/api/reviews/{other.review_id}/run-plan/run-specific",
        headers=HEADERS,
        json={"entries": ["infra.render"], "confirm": True, "expected_revision_id": other.revision_id},
    )
    assert specific.status_code == 200, specific.text
    assert specific.json()["next"] == [{"entry": "infra.render", "action": "artifact_analysis", "depth": "auto"}]
    assert _ok(other, "infra.render")["plan_entry"]["origin"] == "manual"


def test_budget_exhaustion_is_limited_by_budget_never_pass(tmp_path: Path) -> None:
    limited, runner = _scripted_review(tmp_path / "limited", config=DEEP + "  budget:\n    per_review_minutes: 1\n")
    capped = _run(limited, "infra.render")
    assert capped.status_code == 409 and "Limited by budget" in capped.json()["detail"]
    assert runner.calls == [] and _compute_runs(limited) == {}
    entry = next(row for row in _analyses(limited)["entries"] if row["id"] == "infra.render")
    assert entry["status"] == "limited_by_budget"


def test_a_budget_spent_mid_run_stops_the_remaining_sides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import artifact_analyses, artifact_analysis_api

    review, runner = _scripted_review(tmp_path)
    # A ceiling with no slack beyond the plan: the first side spends the entry's whole estimate.
    clock = {"now": 0.0}

    def tick() -> float:
        clock["now"] += 400.0
        return clock["now"]

    monkeypatch.setattr(artifact_analyses, "time", SimpleNamespace(monotonic=tick))
    monkeypatch.setattr(artifact_analysis_api, "analysis_budget_seconds", lambda _plan, _entry: 300.0)
    report = _ok(review, "infra.render")
    assert runner.calls == [(runner.calls[0][0], "old")]
    result = report["results"][0]
    assert result["status"] == "NOT_RUN" and result["reasons"][0].startswith("Limited by budget")
    head = next(row for row in result["attempts"] if row["side"] == "new")
    assert head["state"] == "not_run" and head["reason"].startswith("Limited by budget")
    assert report["verdict"] == "NOT_RUN"


def test_reruns_reuse_exact_snapshot_attempts_and_a_head_report_cannot_claim_the_base_side(tmp_path: Path) -> None:
    def claiming(runtime: Any, side: str, _count: int) -> tuple[int, dict[str, Any]]:
        # Reviewed code writes the report: it claims to be the base run of another entry.
        return 0, {
            "side": "old",
            "entry": "migrations.execute",
            "spec": "other",
            "heavy": {"rendered.yaml": _rendered("base" if side == "old" else "head")},
        }

    review, runner = _scripted_review(tmp_path, claiming)
    first = _ok(review, "infra.render")
    assert first["executions"] == 2 and first["reused"] == 0
    attempts = {row["side"]: row for row in first["results"][0]["attempts"]}
    for side, row in attempts.items():
        item = review.store.get_evidence(row["evidence_id"])
        stored = json.loads(review.store.read_evidence_artifact(item))
        assert stored["side"] == side == stored["request"]["side"]
        analysis = stored["data"]["analysis"]
        assert (analysis["entry"], analysis["spec"]) == ("infra.render", row["evidence_ref"].split(":")[4])
        assert "side" not in analysis["report"] and analysis["report"]["entry"] == "infra.render"
        assert item.verification_status == ""  # a raw attempt is never itself a check result
    second = _ok(review, "infra.render")
    assert second["executions"] == 0 and second["reused"] == 2 and len(runner.calls) == 2
    reused = {row["side"]: row["evidence_id"] for row in second["results"][0]["attempts"]}
    assert reused == {side: row["evidence_id"] for side, row in attempts.items()}
    # A new Snapshot never reads the previous Snapshot's attempts (base included).
    revision = _new_snapshot(review, {"deploy/cm.yaml": _rendered("again")})
    third = _ok(review, "infra.render", revision_id=revision)
    assert third["executions"] == 2 and third["reused"] == 0


def test_unavailable_attempts_are_retried_and_unreferenced_heavy_artifacts_expire(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.run_evidence import RunArtifactStore, review_reference_resolver

    def flaky_tool(runtime: Any, side: str, count: int) -> tuple[int, dict[str, Any]]:
        if count <= 2:
            return 2, {"reason": "kustomize is not installed in the sandbox"}
        return _render_script(runtime, side, count)

    review, runner = _scripted_review(tmp_path, flaky_tool)
    missing = _ok(review, "infra.render")
    assert missing["verdict"] == "NOT_RUN"
    assert "kustomize" in missing["results"][0]["setup_hint"]
    rerun = _ok(review, "infra.render")
    assert rerun["executions"] == 2 and rerun["verdict"] == "PASS"
    record = _record(review, "infra.render")
    assert _artifact(review, record, "rendered.diff")["expired"] is False

    def closer(now: float) -> tuple[str, ...]:
        artifacts = RunArtifactStore.for_store(
            review.store_root, resolver=review_reference_resolver(review.store), clock=lambda: now
        )
        return artifacts.expire(resolver=artifacts.resolver)

    assert closer(time.time()) == ()  # inside the window nothing expires
    removed = closer(time.time() + 10 * 365 * 86400.0)
    assert removed and _artifact(review, record, "rendered.diff")["expired"] is True
    # The record and verdict stay; a rerun re-executes rather than compare without heavy artifacts.
    again = _ok(review, "infra.render")
    assert again["executions"] == 2 and again["verdict"] == "PASS" and len(runner.calls) == 6


def test_a_judgment_citing_the_analysis_keeps_its_heavy_artifacts(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.run_evidence import RunArtifactStore, review_reference_resolver
    from lemoncrow.pro.capabilities.review.sources.local import annotate

    review, _runner = _scripted_review(tmp_path)
    _ok(review, "infra.render")
    record = _record(review, "infra.render")
    session = review.store.get_session(review.review_id)
    revision = review.store.get_revision(review.revision_id)
    annotate(
        review.store,
        session,
        revision,
        path="deploy/cm.yaml",
        start_line=1,
        end_line=1,
        body="This render change needs a rollout note.",
        evidence=(record["evidence_id"],),
        new_text=_rendered("head"),
    )
    artifacts = RunArtifactStore.for_store(
        review.store_root,
        resolver=review_reference_resolver(review.store),
        clock=lambda: time.time() + 10 * 365 * 86400.0,
    )
    artifacts.expire(resolver=artifacts.resolver)
    assert _artifact(review, record, "rendered.diff")["expired"] is False


def test_rendered_secret_values_are_never_stored_even_when_the_render_does_not_parse(tmp_path: Path) -> None:
    value = "plain-sekrit-" + "7731"

    def secret(_runtime: Any, side: str, _count: int) -> tuple[int, dict[str, Any]]:
        doc = (
            _rendered("base" if side == "old" else "head")
            + "---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: db\nstringData:\n  conn: "
            + value
            + ("-head" if side == "new" else "")
            + "\n"
        )
        return 0, {"tool": "kustomize", "heavy": {"rendered.yaml": doc}}

    review, _runner = _scripted_review(tmp_path / "valid", secret)
    assert _ok(review, "infra.render")["verdict"] == "PASS"
    record = _record(review, "infra.render")
    assert "v1/Secret/db" not in json.dumps(record["findings"])  # a value-only change is invisible by design
    assert value.encode() not in _stored(review)
    assert value not in _artifact(review, record, "rendered.diff")["text"]

    def broken(_runtime: Any, side: str, _count: int) -> tuple[int, dict[str, Any]]:
        doc = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\nstringData: {conn: " + value + "\n"
        return 0, {"tool": "kustomize", "heavy": {"rendered.yaml": doc}}

    unparsed, _runner = _scripted_review(tmp_path / "broken", broken)
    report = _ok(unparsed, "infra.render")
    assert report["verdict"] == "UNKNOWN"
    assert any("not valid YAML" in reason for reason in report["results"][0]["reasons"])
    assert value.encode() not in _stored(unparsed)


def test_credentials_reviewed_code_writes_into_its_report_are_redacted_before_storage(tmp_path: Path) -> None:
    def leaky(_runtime: Any, side: str, _count: int) -> tuple[int, dict[str, Any]]:
        rendered = _rendered("base" if side == "old" else "head") + "  key: " + AWS_KEY + "\n"
        return 0, {"tool": "kustomize", "version": "v5 " + AWS_KEY, "heavy": {"rendered.yaml": rendered}}

    review, _runner = _scripted_review(tmp_path, leaky)
    assert _ok(review, "infra.render")["verdict"] == "PASS"
    assert AWS_KEY.encode() not in _stored(review)
    assert AWS_KEY not in json.dumps(_analyses(review))


def test_the_artifact_view_serves_only_what_an_analysis_on_this_snapshot_cites(tmp_path: Path) -> None:
    review, _runner = _scripted_review(tmp_path)
    _ok(review, "infra.render")
    record = _record(review, "infra.render")
    base = f"/api/reviews/{review.review_id}/artifact-analyses/artifact"
    attempt = record["attempts"][0]["evidence_id"]
    for params in (
        {"evidence_id": record["evidence_id"], "name": "plan.txt"},
        {"evidence_id": attempt, "name": "rendered.yaml"},
        {"evidence_id": "evd-missing", "name": "rendered.yaml"},
    ):
        response = review.client.get(base, headers=HEADERS, params=params)
        assert response.status_code == 404, (params, response.text)


def test_a_stale_snapshot_run_cannot_publish_current_results(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}

    def advancing(runtime: Any, side: str, count: int) -> tuple[int, dict[str, Any]]:
        if side == "new" and "advanced" not in holder:
            holder["advanced"] = _new_snapshot(holder["review"], {"deploy/cm.yaml": _rendered("later")})
        return _render_script(runtime, side, count)

    review, _runner = _scripted_review(tmp_path, advancing)
    holder["review"] = review
    response = _run(review, "infra.render")
    assert response.status_code == 409 and "not published as current" in response.json()["detail"]
    assert _analyses(review, review.revision_id)["verdicts"] == []
    assert _analyses(review)["verdicts"] == []
