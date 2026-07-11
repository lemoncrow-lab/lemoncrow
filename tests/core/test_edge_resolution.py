"""WS9 N14 -- tiered tree-sitter -> LSP call-site resolution.

All tests use a STUB resolver; no live language server is required. The tiering
/ residual-selection logic is what is under test.
"""

from __future__ import annotations

from lemoncrow.core.capabilities.code_context.edge_resolution import (
    CallSite,
    LspResolution,
    ResolutionReport,
    compute_residuals,
    resolve_call_sites,
)
from lemoncrow.infra.tree_sitter.tags import Tag, extract_tags_from_text


def _ref(name: str, line: int = 1, file: str = "a.kt") -> Tag:
    return Tag(name=name, kind="reference", file=file, line=line, byte_range=(0, 0))


def _def(name: str, line: int = 1, file: str = "a.kt") -> Tag:
    return Tag(name=name, kind="definition", file=file, line=line, byte_range=(0, 0))


# --------------------------------------------------------------------------- #
# Residual selection.
# --------------------------------------------------------------------------- #
def test_local_definition_resolves_as_tree_sitter() -> None:
    tags = [_def("helper", 1), _ref("helper", 2)]
    resolved, residual = compute_residuals(tags)
    assert residual == []
    assert len(resolved) == 1
    assert resolved[0].provenance == "tree_sitter"
    assert resolved[0].target == "helper"


def test_unresolved_reference_is_residual() -> None:
    tags = [_ref("mysteryCall", 5)]
    resolved, residual = compute_residuals(tags)
    assert resolved == []
    assert residual == [CallSite(name="mysteryCall", file="a.kt", line=5)]


def test_definition_tags_are_not_call_sites() -> None:
    # A definition with no reference must not appear as a residual call site.
    resolved, residual = compute_residuals([_def("onlyDef", 1)])
    assert resolved == []
    assert residual == []


def test_residual_selection_from_real_tree_sitter_extraction() -> None:
    # Real tree-sitter extraction over Kotlin (LSP-only language).
    src = "fun helper() {}\nfun main() { helper(); unknownCall() }\n"
    tags = extract_tags_from_text(src, "a.kt", language="kotlin")
    resolved, residual = compute_residuals(tags)
    resolved_names = {r.name for r in resolved}
    residual_names = {r.name for r in residual}
    # helper is locally defined -> resolved; unknownCall has no local def -> residual.
    assert "helper" in resolved_names
    assert "unknownCall" in residual_names
    assert "unknownCall" not in resolved_names


def test_duplicate_references_deduped() -> None:
    tags = [_ref("x", 2), _ref("x", 2)]
    _, residual = compute_residuals(tags)
    assert residual == [CallSite(name="x", file="a.kt", line=2)]


# --------------------------------------------------------------------------- #
# Fail-open: no resolver -> tree-sitter unchanged, residuals untouched.
# --------------------------------------------------------------------------- #
def test_no_resolver_leaves_residuals_unresolved() -> None:
    tags = [_def("a", 1), _ref("a", 2), _ref("b", 3)]
    report = resolve_call_sites(tags)
    assert isinstance(report, ResolutionReport)
    assert [r.name for r in report.resolved] == ["a"]
    assert [c.name for c in report.residual] == ["b"]
    assert report.resolved[0].provenance == "tree_sitter"


# --------------------------------------------------------------------------- #
# LSP tier with a stub resolver -- only residuals are escalated.
# --------------------------------------------------------------------------- #
def test_lsp_resolver_only_called_for_residuals() -> None:
    calls: list[str] = []

    def stub(site: CallSite) -> LspResolution | None:
        calls.append(site.name)
        return LspResolution(target=f"pkg.{site.name}")

    tags = [_def("a", 1), _ref("a", 2), _ref("b", 3)]
    report = resolve_call_sites(tags, lsp_resolver=stub)
    # 'a' resolved locally -> resolver must NOT be invoked for it.
    assert calls == ["b"]
    provs = {r.name: r.provenance for r in report.resolved}
    assert provs == {"a": "tree_sitter", "b": "lsp_resolved"}
    assert report.residual == []


def test_lsp_dispatch_provenance_tag() -> None:
    def stub(site: CallSite) -> LspResolution | None:
        return LspResolution(target="Base.method", dispatch=True, confidence=0.5)

    report = resolve_call_sites([_ref("polymorphic", 1)], lsp_resolver=stub)
    assert len(report.resolved) == 1
    site = report.resolved[0]
    assert site.provenance == "lsp_dispatch"
    assert site.target == "Base.method"
    assert site.confidence == 0.5


def test_lsp_resolver_returning_none_keeps_residual() -> None:
    def stub(site: CallSite) -> LspResolution | None:
        return None

    report = resolve_call_sites([_ref("nope", 1)], lsp_resolver=stub)
    assert report.resolved == []
    assert [c.name for c in report.residual] == ["nope"]


def test_lsp_resolver_raising_is_swallowed_per_site() -> None:
    def boom(site: CallSite) -> LspResolution | None:
        if site.name == "bad":
            raise RuntimeError("resolver blew up")
        return LspResolution(target=f"ok.{site.name}")

    tags = [_ref("bad", 1), _ref("good", 2)]
    report = resolve_call_sites(tags, lsp_resolver=boom)
    # The raising site stays residual; the good site still resolves.
    assert [c.name for c in report.residual] == ["bad"]
    assert [r.name for r in report.resolved] == ["good"]
    assert report.resolved[0].provenance == "lsp_resolved"


def test_by_provenance_counts() -> None:
    def stub(site: CallSite) -> LspResolution | None:
        return LspResolution(target=site.name) if site.name == "viaLsp" else None

    tags = [
        _def("localFn", 1),
        _ref("localFn", 2),
        _ref("viaLsp", 3),
        _ref("stillUnknown", 4),
    ]
    report = resolve_call_sites(tags, lsp_resolver=stub)
    counts = report.by_provenance()
    assert counts == {"tree_sitter": 1, "lsp_resolved": 1, "residual": 1}
