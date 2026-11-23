"""Strict part-to-trusted-test mapping; no filename or symbol-name guessing.

Only an explicit trusted-base `check_recipes[].part_targets` declaration can
connect a test recipe to a Review symbol. The mapping makes a test available,
not a coverage claim. An ambiguous symbol or a changed source identity refuses.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from lemoncrow.pro.capabilities.review.recipes import RecipeCatalog
from lemoncrow.pro.capabilities.review.session_models import ReviewUnit


def part_test_mappings(catalog: RecipeCatalog, units: Sequence[ReviewUnit]) -> list[dict[str, str]]:
    if catalog.base_state != "present":
        return []
    indexes: dict[tuple[str, str], list[ReviewUnit]] = {}
    for unit in units:
        if unit.kind == "symbol" and unit.content_fingerprint:
            indexes.setdefault((unit.path, unit.symbol), []).append(unit)
    mapped: list[dict[str, str]] = []
    for recipe in catalog.recipes:
        if recipe.origin != "config" or recipe.kind != "test" or not recipe.part_targets:
            continue
        for path, symbol in recipe.part_targets:
            matched = indexes.get((path, symbol), [])
            if len(matched) != 1:
                continue
            item = matched[0]
            mapped.append(
                {
                    "unit_key": item.unit_key,
                    "path": item.path,
                    "symbol": item.symbol,
                    "fingerprint": item.content_fingerprint,
                    "entry_id": recipe.entry_id,
                    "recipe_id": recipe.id,
                    "recipe_digest": recipe.digest,
                    "title": recipe.title or recipe.id,
                    "mapping_kind": "explicit_trusted_base",
                    "coverage": "not_assessed",
                }
            )
    return mapped


def one_part_mapping(
    catalog: RecipeCatalog, units: Sequence[ReviewUnit], *, unit_key: str, entry_id: str
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in part_test_mappings(catalog, units)
            if row["unit_key"] == unit_key and row["entry_id"] == entry_id
        ),
        None,
    )
