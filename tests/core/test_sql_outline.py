from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def test_sql_outline_reaches_treesitter(tmp_path: Path) -> None:
    """A .sql file resolves to `sql` and yields a tree-sitter outline.

    DLS-OUTLINE-02: SQL declarations sit inside transparent ``statement``
    wrapper nodes. After the 17-01 ``unwrap`` engine generalization, the
    ``statement`` wrapper is descended and schema-level constructs
    (``CREATE TABLE`` / ``CREATE VIEW`` / ``CREATE INDEX`` / ``CREATE FUNCTION``)
    are surfaced as signatures with their bodies stripped. A multi-line
    function body and wide tables provide substantial trimmable content so the
    outline clears the 25% savings guard (~67% of source).
    """
    source = """
CREATE TABLE customers (
    id            BIGINT PRIMARY KEY,
    first_name    VARCHAR(255) NOT NULL,
    last_name     VARCHAR(255) NOT NULL,
    email_address VARCHAR(512) NOT NULL UNIQUE,
    phone_number  VARCHAR(64),
    street_line1  VARCHAR(512),
    street_line2  VARCHAR(512),
    city          VARCHAR(255),
    region        VARCHAR(255),
    postal_code   VARCHAR(64),
    country_code  VARCHAR(8),
    created_at    TIMESTAMP NOT NULL DEFAULT now(),
    updated_at    TIMESTAMP NOT NULL DEFAULT now(),
    is_active     BOOLEAN NOT NULL DEFAULT true,
    lifetime_value NUMERIC(18, 2) NOT NULL DEFAULT 0
);

CREATE VIEW active_customers AS
    SELECT id, first_name, last_name, email_address, lifetime_value
    FROM customers
    WHERE is_active = true
    ORDER BY lifetime_value DESC;

CREATE INDEX idx_customers_email
    ON customers (email_address);

CREATE FUNCTION customer_full_name(customer_id BIGINT)
RETURNS VARCHAR AS $$
DECLARE
    sentinel_body_token VARCHAR;
BEGIN
    SELECT first_name || ' ' || last_name
        INTO sentinel_body_token
        FROM customers
        WHERE id = customer_id;
    RETURN sentinel_body_token;
END;
$$ LANGUAGE plpgsql;
""".strip()
    path = tmp_path / "schema.sql"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    # Canonical registry resolves the .sql extension to the "sql" key.
    assert payload["language"] == "sql"
    assert payload["mode"] == "outline"

    outline = payload["outline"]
    assert isinstance(outline, dict)
    # The payoff: tree-sitter outline, NOT the generic regex fallback.
    assert outline["kind"] == "treesitter"

    text = outline["text"]
    # All four schema-construct names are surfaced.
    assert "customers" in text
    assert "active_customers" in text
    assert "idx_customers_email" in text
    assert "customer_full_name" in text
    # Function-body statement token is stripped (bodies removed).
    assert "sentinel_body_token" not in text
