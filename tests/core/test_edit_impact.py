"""Unit tests for edit_impact: substitution-based rename detection + removed-symbol extraction."""

from __future__ import annotations

from lemoncrow.pro.capabilities.tool_supervision.edit_impact import (
    _is_identifier_occurrence,
    _removed_module_symbols,
    _signature_change_params,
    literal_replacements,
    removed_literals,
)


def test_detects_genuine_single_line_rename() -> None:
    edits = [{"old_string": "config['db'] = value", "new_string": "config['database'] = value"}]
    assert literal_replacements(edits) == {"db": "database"}


def test_multiline_rename_detected_after_line_shift() -> None:
    # A delete + an add keeps the line COUNT equal but shifts the middle; the rename
    # must still be found -- by substitution, not positional pairing.
    edits = [
        {
            "old_string": "import old_helper\nrow = fetch('passwd')\nreturn row",
            "new_string": "row = fetch('password')\nlog(row)\nreturn row",
        }
    ]
    assert literal_replacements(edits).get("passwd") == "password"


def test_no_phantom_rename_from_positional_misalignment() -> None:
    # Regression: deleting a top line + adding a lower line nets to equal line count,
    # so positional zip paired d["skeleton"] against d["tokens_saved"] and invented a
    # skeleton->tokens_saved rename -- though BOTH keys survive unchanged in new.
    edits = [
        {
            "old_string": 'import est\nd["skeleton"] = True\nd["tokens_saved"] = s',
            "new_string": 'd["skeleton"] = True\nd["tokens_saved"] = s\nreturn None',
        }
    ]
    result = literal_replacements(edits)
    assert "skeleton" not in result
    assert "tokens_saved" not in result
    assert removed_literals(edits) == []


def test_removed_literal_without_replacement_maps_to_none() -> None:
    edits = [{"old_string": "x = {'legacy_key': 1, 'keep': 2}", "new_string": "x = {'keep': 2}"}]
    assert literal_replacements(edits) == {"legacy_key": None}


def test_removed_module_symbols_flags_renamed_def_and_const() -> None:
    edits = [
        {"old_string": "def compute_total(x):\n    return x", "new_string": "def compute_sum(x):\n    return x"},
        {"old_string": "MAX_RETRIES = 3", "new_string": "MAX_ATTEMPTS = 3"},
    ]
    assert set(_removed_module_symbols(edits)) == {"compute_total", "MAX_RETRIES"}


def test_removed_module_symbols_ignores_indented_and_unchanged() -> None:
    # render/draw are indented methods (not module-level); Widget is unchanged.
    edits = [
        {
            "old_string": "class Widget:\n    def render(self):\n        pass",
            "new_string": "class Widget:\n    def draw(self):\n        pass",
        }
    ]
    assert _removed_module_symbols(edits) == []


def test_is_identifier_occurrence_gate() -> None:
    assert _is_identifier_occurrence("return DEFAULT_TIMEOUT", "DEFAULT_TIMEOUT")
    assert _is_identifier_occurrence("from p.c import DEFAULT_TIMEOUT", "DEFAULT_TIMEOUT")
    assert not _is_identifier_occurrence("# DEFAULT_TIMEOUT is gone", "DEFAULT_TIMEOUT")  # comment
    assert not _is_identifier_occurrence("x = NEW_DEFAULT_TIMEOUT", "DEFAULT_TIMEOUT")  # substring
    assert not _is_identifier_occurrence("obj.DEFAULT_TIMEOUT", "DEFAULT_TIMEOUT")  # attribute of other obj


def test_signature_change_flags_new_required_param() -> None:
    edits = [
        {"old_string": "def render(node):\n    return node", "new_string": "def render(node, theme):\n    return node"}
    ]
    assert _signature_change_params(edits) == {"render": ["theme"]}


def test_signature_change_ignores_new_optional_param() -> None:
    edits = [{"old_string": "def render(node):\n    ...", "new_string": "def render(node, theme=None):\n    ..."}]
    assert _signature_change_params(edits) == {}


def test_signature_change_flags_lost_default() -> None:
    edits = [
        {"old_string": "def render(node, theme=None):\n    ...", "new_string": "def render(node, theme):\n    ..."}
    ]
    assert _signature_change_params(edits) == {"render": ["theme"]}


def test_signature_change_ignores_annotation_commas_and_self() -> None:
    edits = [
        {
            "old_string": "def build(self, opts: dict[str, int]):\n    ...",
            "new_string": "def build(self, opts: dict[str, int], sink: Sink):\n    ...",
        }
    ]
    assert _signature_change_params(edits) == {"build": ["sink"]}
