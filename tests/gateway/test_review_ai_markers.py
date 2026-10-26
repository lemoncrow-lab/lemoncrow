"""AI candidate markers: real path shapes, and a before/after table proving only intended changes.

A marker is a *word* of a path or symbol. ``_``, ``-``, ``.``, ``/`` and a space break words, and so does a camelCase
boundary, so ``user_auth.py``, ``service_secrets.py``, ``review-worker.yaml`` and ``userAuth.ts`` carry a risk signal
exactly as ``auth.py`` does; a word that merely contains a marker (``author``, ``rapid``) carries none.
"""

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


def test_snake_kebab_dotted_and_camel_case_names_are_words() -> None:
    """The names the first real-model run found carrying no signal: an underscore used to be part of a word."""

    for text, category in [
        ("src/user_auth.py", "security"),
        ("service/service_secrets.py", "security"),
        ("services/oauth_token.py", "security"),
        ("lib/token_store.py", "security"),
        ("workers/review_worker.py", "concurrency_state"),
        ("deploy/review-worker.yaml", "concurrency_state"),
        ("src/job.queue.ts", "concurrency_state"),
        ("src/api_v2/views.py", "api_compatibility"),
        ("src/users_api.py", "api_compatibility"),
        ("src/mysql_client.py", "api_compatibility"),
        ("src/db_schema.py", "persistence_migration"),
        ("src/cache_lookup.py", "performance"),
        ("src/secrets.py", "security"),
        ("src/queries.py", "performance"),
        ("src/indexes.py", "performance"),
        ("src/hot_path.py", "performance"),
        ("src/state-machine.ts", "concurrency_state"),
        ("src/userAuth.ts", "security"),
        ("src/UserAuth.java", "security"),
        ("pkg/RBACPolicy.go", "security"),
        ("app/models/oauthToken.rb", "security"),
        ("src/ReviewWorker.java", "concurrency_state"),
        ("src/TokenBucket.go", "security"),
    ]:
        assert category in ai_review._marker_categories(text), (text, category)


def test_markers_do_not_match_lookalikes() -> None:
    for text, category in [
        ("src/rapid/handler.py", "api_compatibility"),
        ("rapid/handler.py", "api_compatibility"),
        ("src/capi/bindings.c", "api_compatibility"),
        ("apidocs/index.md", "api_compatibility"),
        ("src/apiary/views.py", "api_compatibility"),
        ("data/app.sqlite3", "persistence_migration"),
        ("src/mysql_driver.py", "persistence_migration"),
        ("docs/sql/intro.md", "persistence_migration"),
        ("src/immigration/forms.py", "persistence_migration"),
        ("src/immigrations.py", "persistence_migration"),
        ("src/migrationx.py", "persistence_migration"),
        ("src/author_name.py", "security"),
        ("src/authority/roles.py", "security"),
        ("src/Authors.java", "security"),
        ("src/entrust.py", "security"),
        ("src/blocker_list.py", "concurrency_state"),
        ("src/restore_point.py", "concurrency_state"),
        ("src/Racecar.java", "concurrency_state"),
        ("src/indexer.py", "performance"),
        ("src/tokenizer.py", "security"),
        ("src/clientele.py", "api_compatibility"),
    ]:
        assert category not in ai_review._marker_categories(text), (text, category)


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
    "src/user_auth.py",
    "src/oauth_callback.py",
    "src/mysql_client.py",
    "src/api_v2/views.py",
    "src/apis/client.py",
    "src/routes.py",
    "src/tokens.py",
    "src/workers/queue.py",
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
    ("queries/report.sql.j2", "query"): "intended: queries is the plural of query (a word, then a dot)",
    ("src/api_v2/views.py", "api"): "intended: an underscore breaks words, so api_v2 holds the word api",
    ("src/apis/client.py", "api"): "intended: apis is the plural of api",
    ("src/mysql_client.py", "client"): "intended: an underscore breaks words, so mysql_client holds the word client",
    ("src/oauth_callback.py", "oauth"): "intended: an underscore breaks words, so oauth_callback holds the word oauth",
    ("src/routes.py", "route"): "intended: routes is the plural of route",
    ("src/user_auth.py", "auth"): "intended: an underscore breaks words, so user_auth holds the word auth",
    ("src/tokens.py", "token"): "intended: tokens is the plural of token",
    ("src/workers/queue.py", "worker"): "intended: workers is the plural of worker",
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


def test_every_other_marker_matches_as_before_except_across_a_word_break() -> None:
    """Shapes where the marker is a whole word still match, glued-on letters still do not, and the new matches are
    exactly the ones where only an underscore or a plural ``s`` stood between the marker and the edge of its word."""

    same = [
        "{m}",
        "src/{m}/x.py",
        "src/{m}.py",
        "src/pre{m}.py",
        "src/{m}post.py",
        "lib/x.py {m} label",
        "lib/x.py label{m}",
        "{m}/x.py",
    ]
    underscore = ["src/x_{m}.py", "src/{m}_x.py"]
    plural = ["src/{m}s.py"]
    for shape in [*same, *underscore, *plural]:
        for marker in _all_markers():
            text = shape.format(m=marker)
            before, after = (_legacy_match(marker, text), marker in _current_matches(text))
            if marker in CHANGED_MARKERS:
                assert after or not before, f"{marker!r} lost a match on {text!r}"
            elif shape in same:
                assert before == after, f"{marker!r} changed on {text!r}: {before} -> {after}"
            elif shape in underscore:
                assert after, f"{marker!r} no longer matches across an underscore on {text!r}"
            else:
                # A plural of a word marker; a marker that ends in a digit or starts with punctuation has none.
                has_plural = marker[0].isalnum() and marker[-1].isalpha() and marker not in ai_review._PLURAL_FORMS
                assert after == (has_plural or before), f"{marker!r} on {text!r}: {before} -> {after}"
