"""scripts/mirror.py: `!`-prefixed private denies beat broad allows."""

from __future__ import annotations

import ast
import importlib.util
import posixpath
import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath

import pytest

_MIRROR = Path(__file__).resolve().parents[1] / "scripts" / "mirror.py"
_spec = importlib.util.spec_from_file_location("_mirror_under_test", _MIRROR)
assert _spec and _spec.loader
mirror = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mirror)


def test_mirror_allow_deny_contract() -> None:
    """case: test_deny_beats_allow_regardless_of_order"""
    prefixes = ["src", "!src/lemoncrow/core/capabilities/code_context/renderer.py"]
    assert mirror.is_public("src/lemoncrow/gateway/adapters/mcp_server.py", prefixes) is True
    assert mirror.is_public("src/lemoncrow/core/capabilities/code_context/renderer.py", prefixes) is False
    rev = list(reversed(prefixes))
    assert mirror.is_public("src/lemoncrow/core/capabilities/code_context/renderer.py", rev) is False
    "case: test_subtree_deny"
    prefixes = ["src", "!src/lemoncrow/core/capabilities/source_projection"]
    assert mirror.is_public("src/lemoncrow/core/capabilities/source_projection/minify.py", prefixes) is False
    assert mirror.is_public("src/lemoncrow/core/capabilities/pricing.py", prefixes) is True
    "case: test_no_allow_no_public"
    assert mirror.is_public("internal/secret.py", ["src", "tests"]) is False
    "case: test_plain_allowlist_unchanged"
    prefixes = ["docs", "src"]
    assert mirror.is_public("docs/x.md", prefixes) is True
    assert mirror.is_public("src/a.py", prefixes) is True
    assert mirror.is_public("deploy/x", prefixes) is False
    "case: test_real_allowlist_has_no_whole_src_or_frontend_escape_hatch"
    prefixes = mirror.load_public_prefixes()
    assert "src" not in prefixes
    assert "frontend" not in prefixes
    assert "docs" not in prefixes
    assert "src/lemoncrow/pro" in prefixes
    assert "frontend/src/review" in prefixes
    assert mirror.is_public("src/new-private-package/secret.py", prefixes) is False
    assert mirror.is_public("frontend/src/hosted/SecretPanel.tsx", prefixes) is False
    assert mirror.is_public("docs/planning/2026-09-23-review-product-ux-local-hosted.md", prefixes) is False
    assert mirror.is_public("docs/marketing/strategy.md", prefixes) is False
    assert mirror.is_public("docs/superpowers/specs/hosted-design.md", prefixes) is False
    assert mirror.is_public("docs/planning/roadmap.md", prefixes) is True
    assert mirror.is_public("scripts/hosted.sh", prefixes) is True
    assert mirror.is_public("scripts/stage_release.sh", prefixes) is True
    assert mirror.is_public("frontend/package.json", prefixes) is True
    assert mirror.is_public("frontend/package-lock.json", prefixes) is True
    assert mirror.is_public("frontend/src/code/CodeExplorer.tsx", prefixes) is True
    assert mirror.is_public("frontend/src/control/controlApi.ts", prefixes) is True
    assert mirror.is_public("frontend/src/map/mapModel.ts", prefixes) is True


def test_mirror_private_boundary_contract() -> None:
    """case: test_runtime_owned_execution_never_reaches_public_mirror"""
    prefixes = mirror.load_public_prefixes()
    private_runtime_paths = (
        "src/lemoncrow/gateway/cli/coding_engine.py",
        "src/lemoncrow/gateway/cli/interactive.py",
        "src/lemoncrow/gateway/cli/runtime.py",
        "src/lemoncrow/gateway/cli/commands/_private_run.py",
        "src/lemoncrow/gateway/openai_gateway/app.py",
        "src/lemoncrow/pro/capabilities/owned_agent_session/phase_runner.py",
        "src/lemoncrow/pro/capabilities/owned_agent_session/gemini_cache.py",
        "integrations/pi/managed.mjs",
    )
    assert all(not mirror.is_public(path, prefixes) for path in private_runtime_paths)
    assert mirror.is_public("src/lemoncrow/gateway/cli/commands/run.py", prefixes)
    assert mirror.is_public("src/lemoncrow/pro/capabilities/owned_agent_session/session.py", prefixes)
    assert mirror.is_public("client/src/lemoncrow_client/assets/pi_lite.mjs", prefixes)
    "case: test_private_trees_never_reach_the_public_mirror"
    prefixes = mirror.load_public_prefixes()
    leaked = [
        path
        for path in _tracked_files()
        if path.split("/", 1)[0] in _PRIVATE_TREES and mirror.is_public(path, prefixes)
    ]
    assert not leaked, f"scripts/public-paths.txt would publish private files: {leaked[:10]}"
    "case: test_enterprise_developer_plane_docs_do_not_leak_back_into_public_docs"
    "The public client is self-contained; deployment architecture stays private."
    assert not (_REPO / "docs/planning/2026-09-12-hosted-mcp-thin-client.md").exists()
    client_readme = (_REPO / "client/README.md").read_text(encoding="utf-8")
    assert "docs-internal/" not in client_readme
    assert "enterprise/deploy/" not in client_readme
    assert "enterprise/server" not in client_readme
    "case: test_no_credentials_in_the_files_the_mirror_would_publish"
    "Committed key material must never be inside the public allowlist.\n\n    Replaces the wheel-side guard that used to keep `lemoncrow/pro` source out of\n    releases: the engine is open source on purpose now, so the thing worth\n    guarding is credentials, not source.\n    "
    hits = subprocess.run(
        ["git", "grep", "-lIE", _SECRET_RE, "HEAD", "--", "."], cwd=_REPO, capture_output=True, text=True
    ).stdout.splitlines()
    prefixes = mirror.load_public_prefixes()
    paths = [line.split(":", 1)[1] for line in hits if ":" in line]
    leaked = sorted(p for p in paths if p not in _VERIFIED_SYNTHETIC and mirror.is_public(p, prefixes))
    assert not leaked, f"credential-shaped content inside public paths: {leaked}"
    "case: test_private_submodules_are_not_published"
    prefixes = mirror.load_public_prefixes()
    gitlinks = [
        line.split("\t", 1)[1]
        for line in subprocess.run(
            ["git", "ls-files", "-s"], cwd=_REPO, capture_output=True, text=True, check=True
        ).stdout.splitlines()
        if line.startswith(mirror.GITLINK_MODE + " ")
    ]
    sections = {
        fields.get("path"): fields
        for _, fields in mirror.parse_gitmodules((_REPO / ".gitmodules").read_text(encoding="utf-8"))
    }
    for gitlink in gitlinks:
        if mirror.is_public(gitlink, prefixes):
            assert sections[gitlink].get("url") in mirror.PUBLIC_SUBMODULE_URLS, gitlink
    assert mirror.is_public("landing", prefixes) is False
    "case: test_private_engine_and_private_server_tests_are_never_published"
    prefixes = mirror.load_public_prefixes()
    for path in _tracked_files():
        if path.startswith("tests/private/"):
            assert mirror.is_public(path, prefixes) is False, path
    for name in (
        "test_end_to_end_server.py",
        "test_enterprise_scale_budget.py",
        "test_mcp_startup_budget.py",
        "test_shipped_server_search.py",
    ):
        assert mirror.is_public(f"client/tests/{name}", prefixes) is False
    assert mirror.is_public("tests/core/test_embedder_factory.py", prefixes) is True


def test_mirror_public_build_contract() -> None:
    """case: test_frontend_release_manifests_are_public"""
    "The mirrored repo must be able to execute the frontend release build."
    prefixes = mirror.load_public_prefixes()
    for path in ("frontend/package.json", "frontend/package-lock.json"):
        assert (_REPO / path).is_file(), f"release input is missing: {path}"
        assert mirror.is_public(path, prefixes), f"release input is private: {path}"
    "case: test_public_frontend_relative_imports_stay_inside_public_tree"
    "Every relative TS/TSX dependency of public frontend code must also be mirrored."
    import re

    prefixes = mirror.load_public_prefixes()
    source = _REPO / "frontend" / "src"
    import_re = re.compile("(?:from\\s+|import\\s*\\(\\s*|import\\s*)[\\\"\\'](\\.[^\\\"\\']+)[\\\"\\']")
    extensions = ("", ".ts", ".tsx", ".js", ".jsx", ".css")
    offenders: list[str] = []
    for path in (*source.rglob("*.ts"), *source.rglob("*.tsx")):
        relative_path = path.relative_to(_REPO).as_posix()
        if not mirror.is_public(relative_path, prefixes):
            continue
        text = path.read_text(encoding="utf-8")
        for specifier in import_re.findall(text):
            raw = (path.parent / specifier).resolve()
            candidates = [Path(str(raw) + ext) for ext in extensions]
            candidates.extend(raw / ("index" + ext) for ext in extensions[1:])
            target = next((candidate for candidate in candidates if candidate.is_file()), None)
            if target is None:
                continue
            target_path = target.relative_to(_REPO).as_posix()
            if not mirror.is_public(target_path, prefixes):
                offenders.append(f"{relative_path} -> {specifier} -> {target_path}")
    assert offenders == [], "public frontend imports private/missing mirror dependencies: " + ", ".join(offenders)
    "case: test_every_build_stage_input_is_public"
    "A public release build may not reach back into a private-only path."
    prefixes = mirror.load_public_prefixes()
    build = (_REPO / "scripts" / "build.sh").read_text(encoding="utf-8")
    copied: set[str] = set()
    for raw in build.splitlines():
        line = raw.strip()
        if "cp -a " not in line or "$BUILD_STAGE" not in line:
            continue
        command = line.split("cp -a ", 1)[1].split('"$BUILD_STAGE', 1)[0].strip()
        try:
            words = shlex.split(command)
        except ValueError:
            continue
        for word in words:
            if word.startswith(("$", "-")) or word in {"&&"}:
                continue
            candidate = word.rstrip("/")
            if candidate and (_REPO / candidate).exists():
                copied.add(candidate)
    assert copied, "scripts/build.sh build-stage inputs were not discovered"
    private = sorted(path for path in copied if not mirror.is_public(path, prefixes))
    assert not private, f"scripts/build.sh copies private-only inputs into the public release build: {private}"
    "case: test_public_workflows_are_rewritten_to_github_workflows"
    assert mirror.public_output_path(".github/public-workflows/tests.yml") == ".github/workflows/tests.yml"
    assert mirror.public_output_path(".github/public-workflows") == ".github/workflows"
    assert mirror.public_output_path("src/lemoncrow/__init__.py") == "src/lemoncrow/__init__.py"
    "case: test_public_gitmodules_names_only_published_public_gitlinks"
    source = '[submodule "landing"]\n\tpath = landing\n\turl = git@github.com:lemoncrow-lab/landing.git\n[submodule "lemoncode"]\n\tpath = lemoncode\n\turl = https://github.com/lemoncrow-lab/lemoncode.git\n\tbranch = dev\n'
    rendered = mirror.public_gitmodules(source, {"lemoncode"})
    assert "landing" not in rendered
    assert "url = https://github.com/lemoncrow-lab/lemoncode.git" in rendered
    assert mirror.public_gitmodules(source, set()) == ""


def test_mirror_reference_integrity_contract() -> None:
    """case: test_every_repo_script_a_public_file_references_is_public"""
    "A published test or script may not execute a script the mirror omits."
    prefixes = mirror.load_public_prefixes()
    omitted: dict[str, str] = {}
    for path in _tracked_files():
        if not path.endswith((".py", ".sh")) or not mirror.is_public(path, prefixes):
            continue
        try:
            text = (_REPO / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        targets = [f"scripts/{match.group(1) or match.group(2)}" for match in _SCRIPT_REFERENCE.finditer(text)]
        if path.startswith("scripts/"):
            base = Path(path).parent
            targets += [(base / match.group(1)).as_posix() for match in _SCRIPT_DIR_REFERENCE.finditer(text)]
        for target in targets:
            target = Path(target).as_posix()
            if (_REPO / target).is_file() and (not mirror.is_public(target, prefixes)):
                omitted.setdefault(target, path)
    assert not omitted, f"public files reference scripts the mirror omits: {omitted}"
    "case: test_public_files_never_read_or_link_unpublished_repository_files"
    "A published file may not depend on a tracked file the mirror omits.\n\n    Covers ``Path(__file__)``-rooted path expressions in Python (tests reading\n    fixtures, docs, workflows or deploy files) and relative Markdown links. Paths\n    that are not tracked (build output such as ``frontend/dist``) are out of scope.\n    "
    prefixes = mirror.load_public_prefixes()
    tracked = _tracked_files()
    public = [path for path in tracked if mirror.is_public(path, prefixes)]
    published = {mirror.public_output_path(path) for path in public}
    published_dirs = {parent.as_posix() for path in published for parent in PurePosixPath(path).parents}
    tracked_files = set(tracked)
    tracked_dirs = {parent.as_posix() for path in tracked for parent in PurePosixPath(path).parents}
    violations: list[str] = []
    for path in public:
        if path.endswith(".py"):
            try:
                source = (_REPO / path).read_text(encoding="utf-8")
                references = _file_rooted_paths(source, path) if "__file__" in source else []
                references += _sibling_module_imports(source, path)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
        elif path.endswith(".md"):
            try:
                references = _markdown_targets((_REPO / path).read_text(encoding="utf-8"), path)
            except (OSError, UnicodeDecodeError):
                continue
        else:
            continue
        for line, target in references:
            target = target.rstrip("/")
            if (path, target) in _GUARDED_UNPUBLISHED_REFERENCES:
                continue
            unpublished_file = target in tracked_files and target not in published
            unpublished_dir = target in tracked_dirs and target not in published_dirs
            if unpublished_file or unpublished_dir:
                violations.append(f"{path}:{line} -> {target}")
    assert not violations, "public files depend on unpublished repository files:\n" + "\n".join(violations)
    "case: test_unpublished_reference_guard_sees_path_chains_and_links"
    source = 'from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\nCHART = ROOT / "enterprise" / "deploy"\nDEMO = ROOT.parent / "x"\nVALUES = CHART / "values.yaml"\n'
    assert _file_rooted_paths(source, "tests/gateway/test_x.py") == [
        (3, "enterprise/deploy"),
        (5, "enterprise/deploy/values.yaml"),
    ]
    text = "See [plan](../planning/plan.md#top), [site](https://x.dev) and [up](../../../out.md)."
    assert _markdown_targets(text, "docs/reference/a.md") == [(1, "docs/planning/plan.md")]


_PRIVATE_TREES = (
    "enterprise",
    "services",
    "tools",
    "deploy",
    "experiments",
    "docs-internal",
    "signatures",
    "release",
    "reports",
    "lemoncode",
)
_SECRET_RE = "BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-ant-[A-Za-z0-9-]{40,}|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,}|AIza[0-9A-Za-z_-]{35}"
_VERIFIED_SYNTHETIC = frozenset(
    {
        # Secret-scanner fixtures: a PRIVATE KEY header over the word "danger",
        # AKIA0123..., sk-proj-0123456789abcdef...; none is a real credential.
        "client/tests/test_review_delta.py",
        "client/tests/test_review_model_broker.py",
        "tests/core/test_output_redaction_wiring.py",
        "tests/core/test_redaction.py",
        "tests/gateway/test_cli_memory_commands.py",
    }
)
_REPO = Path(__file__).resolve().parents[1]


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=_REPO, capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\x00") if p]


@pytest.mark.parametrize("gitlink", ["landing", "vendor/undeclared"])
def test_public_gitmodules_refuses_private_or_undeclared_gitlinks(gitlink: str) -> None:
    source = '[submodule "landing"]\n\tpath = landing\n\turl = git@github.com:lemoncrow-lab/landing.git\n'
    with pytest.raises(mirror.MirrorBoundaryError):
        mirror.public_gitmodules(source, {gitlink})


_SCRIPT_REFERENCE = re.compile(
    "[\"']scripts[\"']\\s*/\\s*[\"']([\\w.-]+\\.(?:py|sh|txt))[\"']|(?<![\\w.-])scripts/([\\w.-]+\\.(?:py|sh|txt))\\b"
)
_SCRIPT_DIR_REFERENCE = re.compile("\\$\\{?SCRIPT_DIR\\}?/([\\w-]+\\.(?:py|sh))")
_GUARDED_UNPUBLISHED_REFERENCES = frozenset(
    {
        ("benchmarks/review_live_runtime.py", "enterprise/server/src"),
        ("tests/gateway/test_review_engine_artifact.py", ".github/public-workflows"),
        ("tests/gateway/test_review_gate_h_hardening.py", "enterprise/server"),
        ("tests/gateway/test_review_gate_h_hardening.py", "enterprise/server/Dockerfile"),
        ("tests/gateway/test_trust_inventory.py", "docs/status/capabilities.toml"),
        ("tests/test_make_lifecycle.py", ".github/public-workflows/release.yml"),
    }
)
_MARKDOWN_LINK = re.compile("\\]\\(([^)\\s]+)\\)")


def _file_rooted_paths(source: str, path: str) -> list[tuple[int, str]]:
    """Repo-relative paths a module derives from ``Path(__file__)`` (``ROOT / "a" / "b"``)."""
    tree = ast.parse(source)
    here = PurePosixPath(path)
    bindings: dict[str, PurePosixPath] = {}

    def evaluate(node: ast.AST) -> PurePosixPath | None:
        if isinstance(node, ast.Name):
            return here if node.id == "__file__" else bindings.get(node.id)
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id in {"Path", "PurePath", "PurePosixPath"}
                and (len(node.args) == 1)
            ):
                return evaluate(node.args[0])
            if isinstance(func, ast.Attribute) and func.attr in {"resolve", "absolute"} and (not node.args):
                return evaluate(func.value)
            return None
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            base = evaluate(node.value)
            return base.parent if base is not None and base != PurePosixPath(".") else None
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and (node.value.attr == "parents"):
            base, index = (evaluate(node.value.value), node.slice)
            if base is None or not (isinstance(index, ast.Constant) and isinstance(index.value, int)):
                return None
            return base.parents[index.value] if index.value < len(base.parents) else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base = evaluate(node.left)
            if base is None or not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
                return None
            return base / node.right.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and (targets[0].id not in bindings):
                value = evaluate(node.value)
                if value is not None:
                    bindings[targets[0].id] = value
    inner = {id(node.left) for node in ast.walk(tree) if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)}
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and (id(node) not in inner):
            value = evaluate(node)
            if value is not None and value != PurePosixPath("."):
                found.append((node.lineno, value.as_posix()))
    return found


def _sibling_module_imports(source: str, path: str) -> list[tuple[int, str]]:
    """Sibling modules a test imports by bare name (``from test_x import fixture``)."""
    here = PurePosixPath(path).parent
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        for name in names:
            found.append((node.lineno, (here / f"{name.split('.', 1)[0]}.py").as_posix()))
    return found


def _markdown_targets(text: str, path: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for match in _MARKDOWN_LINK.finditer(text):
        target = match.group(1).split("#", 1)[0]
        if not target or re.match("^[a-z][a-z0-9+.-]*:|^/", target):
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        if not resolved.startswith(".."):
            found.append((text.count("\n", 0, match.start()) + 1, resolved))
    return found
