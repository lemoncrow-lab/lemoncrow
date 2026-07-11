"""Domain bundle loader — reads bundle.yaml and asset files from disk."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from lemoncrow.core.domains.models import DomainBundle, bundle_manifest_path
from lemoncrow.core.foundation.models import Playbook

log = logging.getLogger(__name__)

_REQUIRED_FIELDS = {"bundle_id", "domain", "description"}


class DomainLoader:
    """Loads domain bundles from the filesystem.

    Bundles live at:  <domains_root>/<bundle-id>/bundle.yaml
    The built-in (source-tree) bundles live at:
        src/lemoncrow/domains/builtin/<bundle-id>/bundle.yaml
    """

    # Absolute path to bundled-with-source domain bundles
    BUILTIN_ROOT: Path = Path(__file__).parent / "builtin"

    def load(self, bundle_path: Path | str) -> DomainBundle:
        """Load a single domain bundle from a directory path."""
        path = Path(bundle_path)
        manifest = bundle_manifest_path(path)
        if not manifest.exists():
            raise FileNotFoundError(f"bundle.yaml not found in {path}")
        raw: Any = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"bundle.yaml must be a mapping: {manifest}")
        missing = _REQUIRED_FIELDS - raw.keys()
        if missing:
            raise ValueError(f"bundle.yaml missing required fields {missing}: {manifest}")
        return DomainBundle(**raw)

    def load_builtin(self, bundle_id: str) -> DomainBundle:
        """Load a built-in (source-tree) domain bundle by ID."""
        bundle_path = self.BUILTIN_ROOT / bundle_id
        return self.load(bundle_path)

    def list_builtin(self) -> list[DomainBundle]:
        """Return all built-in domain bundles shipped with LemonCrow."""
        bundles: list[DomainBundle] = []
        if not self.BUILTIN_ROOT.exists():
            return bundles
        for candidate in sorted(self.BUILTIN_ROOT.iterdir()):
            if candidate.is_dir() and bundle_manifest_path(candidate).exists():
                try:
                    bundles.append(self.load(candidate))
                except Exception as exc:
                    logging.exception("Recovered from broad exception handler")
                    log.warning("skipping malformed builtin bundle %s: %s", candidate.name, exc)
        return bundles

    def list_from_root(self, domains_root: Path) -> list[DomainBundle]:
        """Return all domain bundles under the given domains root directory."""
        bundles: list[DomainBundle] = []
        if not domains_root.exists():
            return bundles
        for candidate in sorted(domains_root.iterdir()):
            if candidate.is_dir() and bundle_manifest_path(candidate).exists():
                try:
                    bundles.append(self.load(candidate))
                except Exception as exc:
                    logging.exception("Recovered from broad exception handler")
                    log.warning("skipping malformed bundle %s: %s", candidate.name, exc)
        return bundles

    def load_playbooks(self, bundle_path: Path, bundle: DomainBundle) -> list[Playbook]:
        """Load all Playbooks declared in the bundle's playbooks list."""
        blocks: list[Playbook] = []
        for rel_path in bundle.playbooks:
            candidate = bundle_path / rel_path
            if not candidate.exists():
                log.warning("playbook file not found: %s", candidate)
                continue
            try:
                raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict):
                            blocks.append(Playbook(**item))
                elif isinstance(raw, dict):
                    blocks.append(Playbook(**raw))
            except Exception as exc:
                logging.exception("Recovered from broad exception handler")
                log.warning("failed to load playbook %s: %s", candidate, exc)
        return blocks
