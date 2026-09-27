from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.pro.capabilities.review.dependencies import (
    build_dependency_review,
    dependency_capture_gaps,
    derive_dependency_units,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewUnit


def _write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _uv_lock(cryptography: str) -> str:
    return f"""version = 1

[[package]]
name = "authlib"
version = "1.3.1"
source = {{ registry = "https://pypi.org/simple" }}
dependencies = [{{ name = "cryptography" }}]

[[package]]
name = "cryptography"
version = "{cryptography}"
source = {{ registry = "https://pypi.org/simple" }}
dependencies = [{{ name = "cffi" }}]
wheels = [{{ url = "https://example.invalid/cryptography.whl", hash = "sha256:{cryptography.replace('.', '')}" }}]

[[package]]
name = "cffi"
version = "1.17.1"
source = {{ registry = "https://pypi.org/simple" }}
"""


def test_uv_lock_version_change_is_semantic_and_explains_transitive_path(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    manifest = """[project]
name = "demo"
version = "0.1.0"
dependencies = ["authlib>=1.3"]
"""
    _write(old, "pyproject.toml", manifest)
    _write(new, "pyproject.toml", manifest)
    _write(old, "uv.lock", _uv_lock("45.0.4"))
    _write(new, "uv.lock", _uv_lock("46.0.1"))

    review = build_dependency_review(old, new, ("uv.lock",))

    assert review["supported"] is True
    cryptography = next(row for row in review["changes"] if row["name"] == "cryptography")
    assert cryptography["direct"] is False
    assert cryptography["before_version"] == "45.0.4"
    assert cryptography["after_version"] == "46.0.1"
    assert "version" in cryptography["changes"]
    assert cryptography["dependency_paths"] == [["authlib", "cryptography"]]
    assert review["summary"]["semantic_changes"] == 1
    assert review["summary"]["transitive_changes"] == 1


def test_dependency_unit_identity_survives_version_change_but_fingerprint_changes() -> None:
    first = derive_dependency_units(
        {"uv.lock": _uv_lock("45.0.4")},
        {"uv.lock": _uv_lock("46.0.1")},
        ("uv.lock",),
    )
    second = derive_dependency_units(
        {"uv.lock": _uv_lock("46.0.1")},
        {"uv.lock": _uv_lock("47.0.0")},
        ("uv.lock",),
    )

    first_crypto = next(unit for unit in first if unit.symbol == "pypi:cryptography")
    second_crypto = next(unit for unit in second if unit.symbol == "pypi:cryptography")
    assert first_crypto.kind == "dependency"
    assert first_crypto.unit_key == second_crypto.unit_key
    assert first_crypto.content_fingerprint != second_crypto.content_fingerprint
    assert first_crypto.fingerprint_method == "semantic"


def test_same_package_in_two_package_roots_keeps_two_semantic_targets(tmp_path: Path) -> None:
    manifest = """[project]
name = "demo"
version = "0.1.0"
dependencies = ["authlib>=1.3"]
"""
    old_blobs = {
        "pyproject.toml": manifest,
        "uv.lock": _uv_lock("45.0.4"),
        "services/api/pyproject.toml": manifest,
        "services/api/uv.lock": _uv_lock("45.0.4"),
    }
    new_blobs = {
        "pyproject.toml": manifest,
        "uv.lock": _uv_lock("46.0.1"),
        "services/api/pyproject.toml": manifest,
        "services/api/uv.lock": _uv_lock("46.0.2"),
    }
    units = derive_dependency_units(old_blobs, new_blobs, ("uv.lock", "services/api/uv.lock"))
    cryptography = [unit for unit in units if "cryptography" in unit.symbol]

    # One judgment per package root: a monorepo bump in two roots must never
    # collapse into one target and silently drop the other from review.
    assert sorted(unit.path for unit in cryptography) == ["services/api/uv.lock", "uv.lock"]
    assert len({unit.unit_key for unit in cryptography}) == 2
    root_unit = next(unit for unit in cryptography if unit.path == "uv.lock")
    # Root identity is unchanged from the single-root format, preserving judgments.
    assert root_unit.symbol == "pypi:cryptography"

    for root in ("old", "new"):
        for path, text in (old_blobs if root == "old" else new_blobs).items():
            _write(tmp_path / root, path, text)
    review = build_dependency_review(tmp_path / "old", tmp_path / "new", ("uv.lock", "services/api/uv.lock"))
    rows = [row for row in review["changes"] if row["name"] == "cryptography"]
    assert sorted(row["package_root"] for row in rows) == ["", "services/api"]
    assert {row["target_unit_key"] for row in rows} == {unit.unit_key for unit in cryptography}


def test_manifest_only_direct_dependency_change_is_projected(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write(old, "package.json", '{"dependencies":{"react":"18.3.0"}}')
    _write(new, "package.json", '{"dependencies":{"react":"19.0.0"}}')

    review = build_dependency_review(old, new, ("package.json",))

    assert review["supported"] is True
    assert len(review["changes"]) == 1
    react = review["changes"][0]
    assert react["direct"] is True
    assert react["scope"] == "runtime"
    assert react["before_requirement"] == "18.3.0"
    assert react["after_requirement"] == "19.0.0"
    assert "requirement" in react["changes"]


def test_sbom_and_vulnerability_evidence_attach_to_exact_component(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    manifest = """[project]
name = "demo"
version = "0.1.0"
dependencies = ["cryptography>=45"]
"""
    _write(old, "pyproject.toml", manifest)
    _write(new, "pyproject.toml", manifest)
    _write(old, "uv.lock", _uv_lock("45.0.4"))
    _write(new, "uv.lock", _uv_lock("46.0.1"))

    digest = "sha256:" + "a" * 64
    sbom = json.dumps(
        {
            "spdxVersion": "SPDX-2.3",
            "packages": [
                {
                    "name": "cryptography",
                    "versionInfo": "46.0.1",
                    "licenseConcluded": "Apache-2.0",
                    "externalRefs": [
                        {
                            "referenceType": "purl",
                            "referenceLocator": "pkg:pypi/cryptography@46.0.1",
                        }
                    ],
                }
            ],
        }
    ).encode()
    scan = json.dumps(
        [
            {
                "noteName": "CVE-2099-0001",
                "vulnerability": {
                    "effectiveSeverity": "HIGH",
                    "packageIssue": [{"affectedPackage": {"packageName": "cryptography"}}],
                },
            }
        ]
    ).encode()
    evidence = (
        ReviewEvidence(
            id="ev-sbom",
            review_id="r",
            revision_id="rr",
            kind="sbom",
            title="image SBOM",
            content_hash="sbom-digest",
            source="ci",
            source_ref="buildkit",
            detail=json.dumps({"artifact_digest": digest, "observation_source": "BuildKit", "standard": "SPDX"}),
            created_at="2026-09-25T00:00:00+00:00",
        ),
        ReviewEvidence(
            id="ev-scan",
            review_id="r",
            revision_id="rr",
            kind="vulnerability_scan",
            title="image scan",
            content_hash="scan-digest",
            source="ci",
            source_ref="Artifact Analysis",
            detail=json.dumps({"artifact_digest": digest, "observation_source": "Artifact Analysis"}),
            created_at="2026-09-25T00:01:00+00:00",
        ),
    )
    payloads = {"ev-sbom": sbom, "ev-scan": scan}

    review = build_dependency_review(
        old,
        new,
        ("uv.lock",),
        evidence=evidence,
        read_artifact=lambda item: payloads.get(item.id),
    )
    cryptography = next(row for row in review["changes"] if row["name"] == "cryptography")

    assert cryptography["direct"] is True
    assert cryptography["license"] == "Apache-2.0"
    assert cryptography["license_source"] == "sbom"
    assert cryptography["artifacts"][0]["artifact_digest"] == digest
    assert cryptography["vulnerabilities"] == [
        {
            "advisory": "CVE-2099-0001",
            "severity": "HIGH",
            "artifact_digest": digest,
            "source": "Artifact Analysis",
            "observed_at": "2026-09-25T00:01:00+00:00",
            "evidence_id": "ev-scan",
        }
    ]


def _npm_lock(a_version: str, nested_c_resolved: str, nested_c_integrity: str) -> str:
    return json.dumps(
        {
            "name": "app",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "dependencies": {"a": "^1.0.0", "b": "^1.0.0"}},
                "node_modules/a": {
                    "version": a_version,
                    "resolved": f"https://registry.npmjs.org/a/-/a-{a_version}.tgz",
                    "integrity": f"sha512-a{a_version}",
                },
                "node_modules/b": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/b/-/b-1.0.0.tgz",
                    "integrity": "sha512-b",
                    "dependencies": {"c": "^2.0.0"},
                },
                "node_modules/c": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/c/-/c-1.0.0.tgz",
                    "integrity": "sha512-c1",
                },
                "node_modules/b/node_modules/c": {
                    "version": "2.0.0",
                    "resolved": nested_c_resolved,
                    "integrity": nested_c_integrity,
                },
            },
        },
        indent=2,
    )


_GOOD_NESTED_C = ("https://registry.npmjs.org/c/-/c-2.0.0.tgz", "sha512-c2")
_CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"


def _cargo_lock(serde_version: str, syn1_checksum: str) -> str:
    return f"""version = 3

[[package]]
name = "serde"
version = "{serde_version}"
source = "{_CRATES_IO}"
checksum = "serde-{serde_version}"

[[package]]
name = "syn"
version = "1.0.109"
source = "{_CRATES_IO}"
checksum = "{syn1_checksum}"

[[package]]
name = "syn"
version = "2.0.50"
source = "{_CRATES_IO}"
checksum = "syn-2"
"""


def _uv_multi_version_lock(requests_version: str, numpy2_registry: str, requires_python: str = ">=3.10") -> str:
    return f"""version = 1
requires-python = "{requires_python}"

[[package]]
name = "requests"
version = "{requests_version}"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "numpy"
version = "1.26.4"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "numpy"
version = "2.1.0"
source = {{ registry = "{numpy2_registry}" }}
"""


def _unit(units: tuple[ReviewUnit, ...], symbol: str) -> ReviewUnit | None:
    return next((unit for unit in units if unit.symbol == symbol), None)


def test_nested_npm_copy_change_is_its_own_judgment_target() -> None:
    units = derive_dependency_units(
        {"package-lock.json": _npm_lock("1.0.0", *_GOOD_NESTED_C)},
        {"package-lock.json": _npm_lock("1.0.1", "https://evil.example/c-2.0.0.tgz", "sha512-EVIL")},
        ("package-lock.json",),
    )

    # The top-level bump anchors the lockfile, which removes its raw hunks from
    # the review denominator; the nested copy's new tarball must therefore be a
    # semantic target of its own rather than silently disappearing.
    nested = _unit(units, "npm:c")
    assert nested is not None
    assert nested.path == "package-lock.json"
    assert any("node_modules/b/node_modules/c" in reason for reason in nested.reasons)
    assert any("source" in reason for reason in nested.reasons)
    assert any("integrity" in reason for reason in nested.reasons)


def test_a_later_nested_copy_change_reopens_the_same_target() -> None:
    base = {"package-lock.json": _npm_lock("1.0.0", *_GOOD_NESTED_C)}
    first = derive_dependency_units(
        base,
        {"package-lock.json": _npm_lock("1.0.0", "https://mirror.example/c-2.0.0.tgz", "sha512-mirror")},
        ("package-lock.json",),
    )
    second = derive_dependency_units(
        base,
        {"package-lock.json": _npm_lock("1.0.0", "https://evil.example/c-2.0.0.tgz", "sha512-EVIL")},
        ("package-lock.json",),
    )

    before = _unit(first, "npm:c")
    after = _unit(second, "npm:c")
    assert before is not None and after is not None
    assert before.unit_key == after.unit_key
    assert before.content_fingerprint != after.content_fingerprint


def test_cargo_duplicate_crate_versions_do_not_collapse() -> None:
    units = derive_dependency_units(
        {"Cargo.lock": _cargo_lock("1.0.0", "syn-1-good")},
        {"Cargo.lock": _cargo_lock("1.0.1", "syn-1-EVIL")},
        ("Cargo.lock",),
    )

    syn = _unit(units, "cargo:syn")
    assert syn is not None
    assert any("1.0.109" in reason and "integrity" in reason for reason in syn.reasons)
    assert _unit(units, "cargo:serde") is not None


def test_uv_duplicate_package_versions_are_all_covered() -> None:
    units = derive_dependency_units(
        {"uv.lock": _uv_multi_version_lock("2.31.0", "https://pypi.org/simple")},
        {"uv.lock": _uv_multi_version_lock("2.32.0", "https://evil.example/simple")},
        ("uv.lock",),
    )

    numpy = _unit(units, "pypi:numpy")
    assert numpy is not None
    assert any("2.1.0" in reason and "source" in reason for reason in numpy.reasons)


def test_lockfile_metadata_change_is_a_judgment_target() -> None:
    units = derive_dependency_units(
        {"uv.lock": _uv_multi_version_lock("2.31.0", "https://pypi.org/simple")},
        {"uv.lock": _uv_multi_version_lock("2.32.0", "https://pypi.org/simple", requires_python=">=3.8")},
        ("uv.lock",),
    )

    metadata = _unit(units, "pypi:lockfile metadata")
    assert metadata is not None
    assert metadata.path == "uv.lock"
    assert any("requires-python" in reason for reason in metadata.reasons)


def test_reordered_but_identical_lockfile_creates_no_semantic_target() -> None:
    old = _cargo_lock("1.0.0", "syn-1-good")
    blocks = old.split("\n\n")
    reordered = "\n\n".join([blocks[0], *reversed(blocks[1:])])

    assert derive_dependency_units({"Cargo.lock": old}, {"Cargo.lock": reordered}, ("Cargo.lock",)) == ()


def _npm_manifest(left_pad: str) -> str:
    return json.dumps({"name": "demo", "version": "1.0.0", "dependencies": {"left-pad": left_pad}}, indent=2) + "\n"


def _npm_root_lock(root_left_pad: str, resolved: str) -> str:
    return (
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {
                    "": {"name": "demo", "version": "1.0.0", "dependencies": {"left-pad": root_left_pad}},
                    "node_modules/left-pad": {
                        "version": resolved,
                        "resolved": f"https://registry.npmjs.org/left-pad/-/left-pad-{resolved}.tgz",
                        "integrity": f"sha512-{resolved}",
                    },
                },
            },
            indent=2,
        )
        + "\n"
    )


def test_an_npm_root_entry_that_mirrors_the_manifest_is_not_lockfile_metadata() -> None:
    units = derive_dependency_units(
        {"package.json": _npm_manifest("^1.3.0"), "package-lock.json": _npm_root_lock("^1.3.0", "1.3.0")},
        {"package.json": _npm_manifest("^1.3.1"), "package-lock.json": _npm_root_lock("^1.3.1", "1.3.1")},
        ("package.json", "package-lock.json"),
    )

    # The bump is judged once, on its package target; npm restating the same
    # declared range in packages[""] is not a second thing to review.
    assert _unit(units, "npm:left-pad") is not None
    assert _unit(units, "npm:lockfile metadata") is None


def test_an_npm_root_entry_that_disagrees_with_the_manifest_stays_visible() -> None:
    units = derive_dependency_units(
        {"package.json": _npm_manifest("^1.3.0"), "package-lock.json": _npm_root_lock("^1.3.0", "1.3.0")},
        # The lock's root now claims a range package.json never declared.
        {"package.json": _npm_manifest("^1.3.0"), "package-lock.json": _npm_root_lock("^2.0.0", "1.3.0")},
        ("package-lock.json",),
    )

    metadata = _unit(units, "npm:lockfile metadata")
    assert metadata is not None
    assert any("packages['']" in reason for reason in metadata.reasons)


def test_missing_base_text_degrades_instead_of_asserting_additions() -> None:
    new = {"package.json": _npm_manifest("^1.3.1"), "package-lock.json": _npm_root_lock("^1.3.1", "1.3.1")}
    changed = ("package.json", "package-lock.json")

    # Both files were modified, but the capture could not read their base side.
    units = derive_dependency_units({}, new, changed, base_expected=changed, head_expected=changed)

    assert units == ()
    assert dependency_capture_gaps({}, new, changed, base_expected=changed, head_expected=changed) == (
        "package-lock.json",
        "package.json",
    )


def test_missing_head_text_degrades_instead_of_asserting_removals() -> None:
    old = {"package.json": _npm_manifest("^1.3.0"), "package-lock.json": _npm_root_lock("^1.3.0", "1.3.0")}
    # The lockfile's head side exceeded the capture cap and was never read.
    new = {"package.json": _npm_manifest("^1.3.1")}
    changed = ("package.json", "package-lock.json")

    units = derive_dependency_units(old, new, changed, base_expected=changed, head_expected=changed)

    assert units == ()
    assert dependency_capture_gaps(old, new, changed, base_expected=changed, head_expected=changed) == (
        "package-lock.json",
    )


def test_a_newly_added_manifest_is_still_a_dependency_addition() -> None:
    new = {"package.json": _npm_manifest("^1.3.1"), "package-lock.json": _npm_root_lock("^1.3.1", "1.3.1")}
    changed = ("package.json", "package-lock.json")

    # Added files have no base side to read; that is not a capture gap.
    units = derive_dependency_units({}, new, changed, base_expected=(), head_expected=changed)

    left_pad = _unit(units, "npm:left-pad")
    assert left_pad is not None
    assert "direct dependency added" in left_pad.reasons
