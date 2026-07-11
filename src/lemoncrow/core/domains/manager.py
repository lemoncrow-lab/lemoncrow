"""Domain bundle manager — top-level facade for listing and accessing bundles.

This replaces PackManager with a much simpler interface: there is no install,
no uninstall, no publish, no dependency resolution.  Bundles are either
built-in (shipped with LemonCrow source) or stored in the user's domains root.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from lemoncrow.core.domains.loader import DomainLoader
from lemoncrow.core.domains.models import DomainBundle, DomainBundleRef, bundle_manifest_path
from lemoncrow.core.foundation.models import Playbook

logger = logging.getLogger(__name__)


class DomainManager:
    """Facade for accessing domain bundles.

    Bundles are resolved in this priority order:
      1. User bundles  — <lemoncrow_root>/domains/
      2. Built-in bundles — shipped with the LemonCrow source tree
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.domains_root = self.root / "domains"
        self.loader = DomainLoader()

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_bundles(self) -> list[DomainBundleRef]:
        """Return refs for all available domain bundles (user + builtin)."""
        seen: set[str] = set()
        refs: list[DomainBundleRef] = []

        for bundle in self.loader.list_from_root(self.domains_root):
            path = self.domains_root / bundle.bundle_id
            ref = DomainBundleRef.from_bundle(bundle, path)
            seen.add(bundle.bundle_id)
            refs.append(ref)

        for bundle in self.loader.list_builtin():
            if bundle.bundle_id not in seen:
                path = DomainLoader.BUILTIN_ROOT / bundle.bundle_id
                refs.append(DomainBundleRef.from_bundle(bundle, path))

        return refs

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def info(self, bundle_id: str) -> dict[str, Any] | None:
        """Return detailed info for a bundle by ID, or None if not found."""
        bundle, path = self._resolve(bundle_id)
        if bundle is None:
            return None
        return {
            "bundle_id": bundle.bundle_id,
            "domain": bundle.domain,
            "description": bundle.description,
            "author": bundle.author,
            "path": str(path),
            "playbooks": bundle.playbooks,
            "rubrics": bundle.rubrics,
            "environments": bundle.environments,
            "evals": bundle.evals,
            "benchmarks": bundle.benchmarks,
        }

    # ------------------------------------------------------------------
    # Playbook access (used by runtime adapter)
    # ------------------------------------------------------------------

    def load_playbooks(self, bundle_id: str) -> list[Playbook]:
        """Load all Playbooks declared by the given bundle."""
        bundle, path = self._resolve(bundle_id)
        if bundle is None or path is None:
            return []
        return self.loader.load_playbooks(path, bundle)

    def all_playbooks(self) -> list[Playbook]:
        """Load Playbooks from all available bundles."""
        blocks: list[Playbook] = []
        for ref in self.list_bundles():
            bundle, path = self._resolve(ref.bundle_id)
            if bundle is not None and path is not None:
                blocks.extend(self.loader.load_playbooks(path, bundle))
        return blocks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve(self, bundle_id: str) -> tuple[DomainBundle | None, Path | None]:
        """Find a bundle by ID, checking user root first then builtins."""
        user_path = self.domains_root / bundle_id
        if bundle_manifest_path(user_path).exists():
            try:
                return self.loader.load(user_path), user_path
            except Exception:  # noqa: BLE001 - fall through to builtin bundle on any load failure
                logger.warning("Failed to load domain bundle %s from %s", bundle_id, user_path, exc_info=True)

        builtin_path = DomainLoader.BUILTIN_ROOT / bundle_id
        if bundle_manifest_path(builtin_path).exists():
            try:
                return self.loader.load(builtin_path), builtin_path
            except Exception:  # noqa: BLE001 - unresolved bundle is reported as (None, None)
                logger.warning("Failed to load domain bundle %s from %s", bundle_id, builtin_path, exc_info=True)

        return None, None
