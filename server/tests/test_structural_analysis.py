from __future__ import annotations

import time

import pytest
from lemoncrow_server_core.index.analysis import analyze, analyze_foreground, profile_for_path
from lemoncrow_server_core.index.contracts import sha256_hex
from lemoncrow_server_core.index.structural import STRUCTURAL_ANALYZER_VERSION, analyze_structural


@pytest.mark.parametrize(
    ("profile", "source", "definitions", "call", "import_module"),
    (
        (
            "python-2",
            b"from pkg.client import Client\nclass Service:\n    def run(self, client):\n        return client.send()\n",
            {"Service", "run"},
            "send",
            "pkg.client",
        ),
        (
            "typescript-2",
            b"import { Client } from './client';\nexport class Service { run(client: Client) { return client.send(); } }\n",
            {"Service", "run"},
            "send",
            "./client",
        ),
        (
            "go-2",
            b'import "net/http"\nfunc Exported(client Client) { client.Send() }\n',
            {"Exported"},
            "Send",
            "net/http",
        ),
        (
            "rust-2",
            b"use crate::client::Client;\npub struct Service;\nimpl Service { pub fn run(&self) { helper(); } }\nfn helper() {}\n",
            {"Service", "run", "helper"},
            "helper",
            "crate::client",
        ),
    ),
)
def test_structural_profiles_expose_definitions_calls_and_imports(
    profile: str, source: bytes, definitions: set[str], call: str, import_module: str
) -> None:
    artifact = analyze(sha256_hex(source), profile, source)
    assert definitions <= {item.name for item in artifact.definitions}
    assert any(item.name == call and item.is_call for item in artifact.references)
    assert import_module in {item.module for item in artifact.imports}


def test_typescript_v2_finds_class_method_that_regex_profile_misses() -> None:
    source = b"export class Service { run() { return helper(); } }\n"
    old = analyze(sha256_hex(source), "typescript-1", source)
    new = analyze(sha256_hex(source), "typescript-2", source)
    assert "run" not in {item.name for item in old.definitions}
    assert "run" in {item.name for item in new.definitions}
    assert any(item.name == "helper" and item.is_call for item in new.references)


def test_structural_analyzer_is_pathless_deterministic_content_analysis() -> None:
    source = b"pub struct Widget;\nfn build() { make_widget(); }\n"
    first = analyze_structural(source, "rust")
    second = analyze_structural(source, "rust")
    assert first == second
    assert first is not None
    assert first.analyzer_version == STRUCTURAL_ANALYZER_VERSION
    assert first.language == "rust"


def test_new_client_profile_hints_select_versioned_structural_profiles() -> None:
    assert profile_for_path("pkg/a.py") == "python-3"
    assert profile_for_path("web/a.tsx") == "typescript-3"
    assert profile_for_path("cmd/a.go") == "go-3"
    assert profile_for_path("src/a.rs") == "rust-3"


def test_generated_typescript_export_default_does_not_stall_import_analysis() -> None:
    source = (
        b"// GENERATED CODE -- DO NOT EDIT!\n"
        b"import { decode } from './codec';\n"
        b"export { helper } from './helper';\n"
        b"export default __toBinary(`" + (b"A" * 1_000_000) + b"`);\n"
    )
    started = time.perf_counter()
    artifact = analyze_foreground(sha256_hex(source), "typescript-3", source)
    elapsed = time.perf_counter() - started

    assert {item.module for item in artifact.imports} == {"./codec", "./helper"}
    assert elapsed < 5.0
