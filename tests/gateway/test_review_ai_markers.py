"""AI candidate markers: real path shapes, and a before/after table proving only intended changes."""

from __future__ import annotations

import re

from lemoncrow.pro.capabilities.review import ai_review

#: The markers whose matching was deliberately changed (they could not match real paths).
CHANGED_MARKERS = frozenset({"/api/", " api ", ".sql", "migration"})


def _legacy_match(marker: str, text: str) -> bool:
    """The matcher before the fix, verbatim."""

    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(marker)}(?![A-Za-z0-9_])", text) is not None


def _current_matches(text: str) -> set[str]:
    return {
        marker
        for patterns in ai_review._MARKER_PATTERNS.values()
        for marker, pattern in patterns
        if pattern.search(text)
    }


def _all_markers() -> list[str]:
    return [marker for markers in ai_review._MARKERS.values() for marker in markers]


def test_markers_match_real_path_shapes() -> None:
    for text, category in [
        ("src/api/users.py", "api_compatibility"),
        ("api/handlers.py", "api_compatibility"),
        ("frontend/src/api/v1/orders.ts", "api_compatibility"),
        ("db/migrations/0001_init.sql", "persistence_migration"),
        ("reports/monthly.sql", "persistence_migration"),
        ("app/migration_helpers.py", "persistence_migration"),
        ("app/db_migration.py", "persistence_migration"),
        ("src/x.py public api change", "api_compatibility"),
    ]:
        assert category in ai_review._marker_categories(text)


def test_markers_do_not_match_lookalikes() -> None:
    for text, category in [
        ("src/rapid/handler.py", "api_compatibility"),
        ("rapid/handler.py", "api_compatibility"),
        ("src/capi/bindings.c", "api_compatibility"),
        ("apidocs/index.md", "api_compatibility"),
        ("src/api_v2/views.py", "api_compatibility"),
        ("data/app.sqlite3", "persistence_migration"),
        ("src/mysql_driver.py", "persistence_migration"),
        ("docs/sql/intro.md", "persistence_migration"),
        ("src/immigration/forms.py", "persistence_migration"),
        ("src/immigrations.py", "persistence_migration"),
        ("src/migrationx.py", "persistence_migration"),
    ]:
        assert category not in ai_review._marker_categories(text)


#: Before/after over a varied path list. Every divergence is listed with its
#: classification; all are intended (the marker could not match this real shape).
TABLE: tuple[str, ...] = (
    "src/api/users.py",
    "api/handlers.py",
    "frontend/src/api/client.ts",
    "src/api.py",
    "src/apis/client.py",
    "src/api_v2/views.py",
    "src/rapid/handler.py",
    "src/capi/bindings.c",
    "apidocs/index.md",
    "db/migrations/0001_init.sql",
    "migrations/0002_drop_email.sql",
    "app/migration_helpers.py",
    "app/db_migration.py",
    "src/migration.py",
    "src/immigration/forms.py",
    "schema/users.sql",
    "queries/report.sql.j2",
    "data/app.sqlite3",
    "src/mysql_client.py",
    "docs/sql/intro.md",
    "src/auth/session.py",
    "src/oauth_callback.py",
    "src/token.py",
    "src/tokens.py",
    "src/crypto/keys.py",
    "src/route.py",
    "src/routes.py",
    "src/wire/format.py",
    "src/workers/queue.py",
    "src/lock.py",
    "src/locking.py",
    "src/payments/transaction.py",
    "src/storage/schema.py",
    "src/postgres/pool.py",
    "src/cache/lookup.py",
    "src/search/index.py",
    "src/n+1/detector.py",
    "src/x.py public api change",
    "src/x.py api",
    "src/x.py hot path state machine",
)
INTENDED: dict[tuple[str, str], str] = {
    ("src/api/users.py", "/api/"): "intended: api/ path segment",
    ("api/handlers.py", "/api/"): "intended: top-level api/ directory",
    ("frontend/src/api/client.ts", "/api/"): "intended: api/ path segment (category already matched by client)",
    ("db/migrations/0001_init.sql", ".sql"): "intended: SQL file",
    ("db/migrations/0001_init.sql", "migration"): "intended: migrations/ directory (plural)",
    ("migrations/0002_drop_email.sql", ".sql"): "intended: SQL file",
    ("migrations/0002_drop_email.sql", "migration"): "intended: migrations/ directory (plural)",
    ("app/migration_helpers.py", "migration"): "intended: snake_case migration module",
    ("app/db_migration.py", "migration"): "intended: snake_case migration module",
    ("schema/users.sql", ".sql"): "intended: SQL file (category already matched by schema)",
    ("queries/report.sql.j2", ".sql"): "intended: templated SQL file",
    ("src/x.py public api change", " api "): "intended: the word api in a target label",
}


def test_before_after_table_diverges_only_where_intended() -> None:
    divergences = {
        (text, marker): (_legacy_match(marker, text), marker in _current_matches(text))
        for text in TABLE
        for marker in _all_markers()
        if _legacy_match(marker, text) != (marker in _current_matches(text))
    }

    assert set(divergences) == set(INTENDED)
    assert all(before is False and after is True for before, after in divergences.values())


def test_every_other_marker_matches_exactly_as_before() -> None:
    for shape in [
        "{m}",
        "src/{m}/x.py",
        "src/{m}.py",
        "src/{m}s.py",
        "src/x_{m}.py",
        "src/{m}_x.py",
        "src/pre{m}.py",
        "src/{m}post.py",
        "lib/x.py {m} label",
        "lib/x.py label{m}",
        "{m}/x.py",
    ]:
        for marker in _all_markers():
            text = shape.format(m=marker)
            before, after = (_legacy_match(marker, text), marker in _current_matches(text))
            if marker in CHANGED_MARKERS:
                assert after or not before, f"{marker!r} lost a match on {text!r}"
            else:
                assert before == after, f"{marker!r} changed on {text!r}: {before} -> {after}"
