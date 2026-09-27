"""Unit tests for edit_impact: substitution-based rename detection + removed-symbol extraction."""

from __future__ import annotations

from lemoncrow.pro.capabilities.tool_supervision.edit_impact import (
    _call_patterns,
    _call_queries,
    _def_signatures,
    _is_call_occurrence,
    _is_identifier_occurrence,
    _method_uncertainty,
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


# --- method signature changes (plan §4.1's own example) ----------------------


def test_def_signatures_qualifies_methods_by_class() -> None:
    """Indentation is the only class signal an edit hunk carries; it is enough."""
    text = (
        "class SessionManager:\n"
        "    def refresh(self, user):\n"
        "        pass\n"
        "\n"
        "    class Inner:\n"
        "        def nested(self, a):\n"
        "            pass\n"
        "\n"
        "\n"
        "def helper(x):\n"
        "    pass\n"
    )
    keys = _def_signatures(text)
    # A sibling class never claims a module-level function that merely follows it.
    assert set(keys) == {"SessionManager.refresh", "Inner.nested", "helper"}
    assert keys["SessionManager.refresh"] == "self, user"


def test_signature_change_keys_a_method_by_its_class() -> None:
    edits = [
        {
            "old_string": "class SessionManager:\n    def refresh(self, user):\n        pass",
            "new_string": "class SessionManager:\n    def refresh(self, user, context):\n        pass",
        }
    ]
    assert _signature_change_params(edits) == {"SessionManager.refresh": ["context"]}


def test_a_method_moved_between_classes_is_not_a_signature_change() -> None:
    """Old and new are matched on the qualified key, so a move is a move.

    Matching on the bare name would report ``refresh`` as having gained ``context``
    on whichever class happened to be compared -- a change no reviewer could verify.
    """
    edits = [
        {
            "old_string": "class A:\n    def refresh(self, user):\n        pass",
            "new_string": "class B:\n    def refresh(self, user, context):\n        pass",
        }
    ]
    assert _signature_change_params(edits) == {}


def test_call_detection_matches_the_shape_each_kind_of_def_is_actually_called_by() -> None:
    """A method is reached as ``obj.name(``; a module-level function never is."""
    assert _call_patterns("SessionManager.refresh") == ["$OBJ.refresh($$$)"]
    assert _call_patterns("refresh_session") == ["refresh_session($$$)"]
    assert _call_queries("SessionManager.refresh") == [".refresh("]
    assert _call_queries("refresh_session") == ["refresh_session"]

    method = "SessionManager.refresh"
    assert _is_call_occurrence("return manager.refresh(user)", method)
    assert _is_call_occurrence("self.refresh(user)", method)
    assert not _is_call_occurrence("# manager.refresh(user)", method)  # comment
    assert not _is_call_occurrence("return refresh(user)", method)  # not through a receiver
    assert not _is_call_occurrence("return manager.refresh_all(user)", method)  # substring

    module_level = "refresh_session"
    assert _is_call_occurrence("return refresh_session(token)", module_level)
    assert not _is_call_occurrence("return api.refresh_session(token)", module_level)  # attribute of another obj


def test_method_uncertainty_states_the_doubt_and_never_invents_certainty() -> None:
    """One definition is proof of ownership; anything else, or no answer, is not."""
    assert _method_uncertainty("SessionManager", "refresh", 1) == ""

    ambiguous = _method_uncertainty("SessionManager", "refresh", 3)
    # A bounded search can only under-report, so the count is stated as a lower bound.
    assert "at least 3 definitions named refresh()" in ambiguous
    assert "SessionManager.refresh" in ambiguous

    # A census that could not run is treated exactly like an ambiguous one.
    unknown = _method_uncertainty("SessionManager", "refresh", None)
    assert unknown and "could not count" in unknown
