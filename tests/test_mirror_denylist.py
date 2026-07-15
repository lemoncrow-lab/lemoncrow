"""scripts/mirror.py: `!`-prefixed private denies beat broad allows."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MIRROR = Path(__file__).resolve().parents[1] / "scripts" / "mirror.py"
_spec = importlib.util.spec_from_file_location("_mirror_under_test", _MIRROR)
assert _spec and _spec.loader
mirror = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mirror)


def test_deny_beats_allow_regardless_of_order() -> None:
    prefixes = ["src", "!src/lemoncrow/core/capabilities/code_context/renderer.py"]
    assert mirror.is_public("src/lemoncrow/gateway/adapters/mcp_server.py", prefixes) is True
    assert mirror.is_public("src/lemoncrow/core/capabilities/code_context/renderer.py", prefixes) is False
    # deny listed BEFORE the allow still wins
    rev = list(reversed(prefixes))
    assert mirror.is_public("src/lemoncrow/core/capabilities/code_context/renderer.py", rev) is False


def test_subtree_deny() -> None:
    prefixes = ["src", "!src/lemoncrow/core/capabilities/source_projection"]
    assert mirror.is_public("src/lemoncrow/core/capabilities/source_projection/minify.py", prefixes) is False
    assert mirror.is_public("src/lemoncrow/core/capabilities/licensing/models.py", prefixes) is True


def test_no_allow_no_public() -> None:
    assert mirror.is_public("internal/secret.py", ["src", "tests"]) is False


def test_plain_allowlist_unchanged() -> None:
    prefixes = ["docs", "src"]
    assert mirror.is_public("docs/x.md", prefixes) is True
    assert mirror.is_public("src/a.py", prefixes) is True
    assert mirror.is_public("deploy/x", prefixes) is False


def test_public_workflows_are_rewritten_to_github_workflows() -> None:
    assert mirror.public_output_path(".github/public-workflows/tests.yml") == ".github/workflows/tests.yml"
    assert mirror.public_output_path(".github/public-workflows") == ".github/workflows"
    assert mirror.public_output_path("src/lemoncrow/__init__.py") == "src/lemoncrow/__init__.py"
