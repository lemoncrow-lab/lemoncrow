"""Runtime context retrieval should merge learned and domain bundle ReasonBlocks."""

from __future__ import annotations

from pathlib import Path

import yaml

from atelier.core.foundation.models import ReasonBlock
from atelier.gateway.adapters.runtime import ContextRuntime


def test_runtime_get_context_merges_learned_and_domain_blocks(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    runtime = ContextRuntime(root=root)

    runtime.store.upsert_block(
        ReasonBlock(
            id="rb-runtime-learned",
            title="Runtime Learned Recovery",
            domain="Agent.shopify.publish",
            triggers=["publish", "shopify"],
            situation="When runtime traces show repeated publish failures",
            dead_ends=["Retry blindly without validating identifiers"],
            procedure=["Confirm Product GID", "Run validation before retry"],
        )
    )

    # Create a user domain bundle directly in <root>/domains/<bundle_id>/
    bundle_dir = root / "domains" / "shopify.publish"
    (bundle_dir / "reasonblocks").mkdir(parents=True)

    (bundle_dir / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "bundle_id": "shopify.publish",
                "domain": "Agent.shopify.publish",
                "description": "Shopify publish domain bundle",
                "author": "Atelier Test",
                "reasonblocks": ["reasonblocks/publish_guard.yaml"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    (bundle_dir / "reasonblocks" / "publish_guard.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "rb-domain-guard",
                "title": "Domain Publish Guard",
                "domain": "Agent.shopify.publish",
                "triggers": ["publish", "gid"],
                "situation": "When publish tasks need domain-level guardrails",
                "procedure": ["Apply domain guard", "Verify publish rubric"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    context = runtime.get_context(
        task="Publish Shopify product and validate GID",
        domain="Agent.shopify.publish",
        max_blocks=10,
    )

    assert "Procedure: Runtime Learned Recovery" in context
    assert "Procedure: Domain Publish Guard" in context
