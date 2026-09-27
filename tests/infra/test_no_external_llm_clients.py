from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path("src/lemoncrow")
FORBIDDEN_PROVIDER_IMPORTS = {
    "anthropic",
    "google.generativeai",
    "mistralai",
}
# Allowed provider imports are restricted to explicit infrastructure boundaries.
# - "ollama": LemonCrow's internal-processing module only (WP-36). Any other file
#   importing ollama breaks the boundary rule: no model-client imports on the
#   user's hot path.
# - "openai": Embedding and internal-processing adapters only.
# - "httpx": HTTP implementation details stay in the embedding or local IPC
#   infrastructure adapters, never in gateway orchestration.
# - "litellm": LemonCrow's internal-processing module only. Native multi-provider
#   completion clients are confined so no model-client import lands on the
#   user's hot path.
ALLOWED_PROVIDER_IMPORTS = {
    "ollama": {Path("src/lemoncrow/infra/internal_llm/ollama_client.py")},
    "litellm": {
        Path("src/lemoncrow/infra/internal_llm/litellm_client.py"),
        # TODO(boundary): The owned-agent execution loop in the CLI runtime drives
        # native litellm streaming + tool-call dispatch directly (async
        # litellm.completion with backoff). This is a genuine boundary violation
        # introduced on the `bench` refactor that should be routed through an
        # infra streaming wrapper. Allowlisted here pending CLI-owned refactor.
        Path("src/lemoncrow/gateway/cli/runtime.py"),
    },
    "openai": {
        Path("src/lemoncrow/infra/embeddings/openai_embedder.py"),
        Path("src/lemoncrow/infra/internal_llm/openai_client.py"),
    },
    "httpx": {
        Path("src/lemoncrow/infra/embeddings/openai_embedder.py"),
        Path("src/lemoncrow/infra/ipc/httpx_uds.py"),
    },
}


def _imported_roots(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return set()
    roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".", 1)[0])

    return roots


def test_llm_provider_sdks_are_confined_to_infra_boundaries() -> None:
    violations: list[str] = []

    for path in SOURCE_ROOT.rglob("*.py"):
        imports = _imported_roots(path)
        for provider in FORBIDDEN_PROVIDER_IMPORTS & imports:
            violations.append(f"{path}: forbidden provider import {provider}")
        for provider, allowed_paths in ALLOWED_PROVIDER_IMPORTS.items():
            if provider in imports and path not in allowed_paths:
                violations.append(f"{path}: {provider} import must stay in {sorted(allowed_paths)!r}")

    assert not violations, "\n".join(violations)
