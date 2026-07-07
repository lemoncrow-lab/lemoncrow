from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def test_json_large_nested_reaches_treesitter(tmp_path: Path) -> None:
    """A large, deeply nested .json file yields a tree-sitter outline.

    DLS-OUTLINE-05: JSON structure is buried inside ``document → object``
    wrappers. After the 17-01 ``unwrap`` + ``keep_first_line`` generalization,
    a large nested object clears the 25% savings guard because the nested values
    are dropped — only the first line of each top-level ``pair`` is kept.
    """
    source = """
{
  "name": "deeply-nested-config",
  "version": "3.2.1",
  "settings": {
    "logging": {
      "level": "debug",
      "handlers": ["console", "file", "syslog"],
      "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
      "rotation": {
        "when": "midnight",
        "backupCount": 14,
        "deeply_nested_scalar": "do-not-leak-this-value"
      }
    },
    "database": {
      "host": "db.internal.example.com",
      "port": 5432,
      "pool": {
        "minSize": 4,
        "maxSize": 32,
        "timeoutSeconds": 30,
        "retries": {
          "attempts": 5,
          "backoffMs": 250
        }
      }
    }
  },
  "features": {
    "experimental": {
      "enableNewParser": true,
      "enableCaching": true,
      "shadowTraffic": {
        "percentage": 10,
        "targets": ["alpha", "beta", "gamma", "delta"]
      }
    }
  },
  "metadata": {
    "owner": "platform-team",
    "tags": ["service", "config", "production"],
    "contact": {
      "email": "platform@example.com",
      "slack": "#platform-support"
    }
  }
}
""".strip()
    path = tmp_path / "config.json"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    assert payload["language"] == "json"
    assert payload["mode"] == "outline"

    outline = payload["outline"]
    assert isinstance(outline, dict)
    # The payoff: tree-sitter outline, NOT the generic regex fallback.
    assert outline["kind"] == "treesitter"

    text = outline["text"]
    # Top-level keys are present.
    assert "name" in text
    assert "settings" in text
    assert "features" in text
    assert "metadata" in text
    # Deeply nested scalar values are excluded.
    assert "do-not-leak-this-value" not in text


def test_json_small_flat_degrades_via_guard(tmp_path: Path) -> None:
    """A small, flat .json file degrades cleanly via the 25% savings guard.

    DLS-OUTLINE-05: This degradation is INTENDED behavior, not a bug. A flat
    5-key JSON object has no nested content to trim, so the tree-sitter outline
    is not >=25% smaller than the source. ``capability.smart_read``'s
    ``len(text) <= 0.75 * len(source)`` guard therefore rejects the dedicated
    outline and falls back to the generic path (``kind == "generic"``) or to the
    full read (``mode == "full"``) — whichever the pipeline produces.
    """
    source = """
{"first_name": "Alexander", "last_name": "Hamilton", \
"email": "alex.hamilton@example.com", "role": "administrator", \
"department": "platform-engineering"}
""".strip()
    path = tmp_path / "flat.json"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    # Designed degradation: the guard rejects the thin dedicated outline.
    outline = payload.get("outline")
    degraded_to_generic = isinstance(outline, dict) and outline.get("kind") == "generic"
    degraded_to_full = payload.get("mode") == "full"
    assert degraded_to_generic or degraded_to_full
