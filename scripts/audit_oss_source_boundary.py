#!/usr/bin/env python3
"""Audit the public OSS source tree for advanced/private code intelligence.

Ownership (docs/architecture/oss-private-code-intelligence.md): public OSS owns
Runtime Lite, local Review, local execution primitives, neutral contracts and
auditable hosted clients. Advanced commercial intelligence -- the code-context
engine, learned/model embedders, cross-encoder rerankers, ANN indexes, learned
ranking assets, graph/lineage -- and hosted organization/service behaviour are
private (``enterprise/``, ``services/``) and are never mirrored.

The audit is content-aware rather than a list of known filenames. It inspects
every file the public mirror would publish (``scripts/public-paths.txt``
evaluated with ``scripts/mirror.py``'s own rules; every file in a checkout
without that list) and fails on:

``model-library``
    an import of a model, ANN or learned-ranking library;
``model-class``
    a reference to a cross-encoder / sentence-transformer model class;
``private-import``
    an import (static, relative or ``import_module``/``__import__`` literal) of
    a private distribution, or of a first-party module the public tree does not
    contain (for example the private engine contributed into ``lemoncrow.pro``);
``private-module-string``
    a string naming a private distribution module (``monkeypatch`` targets,
    dynamic imports through a constant) outside the reviewed provider seams;
``ann-index``
    a module, class or function whose name says it implements an ANN/vector
    index, outside a short reviewed allowlist;
``model-asset``
    a trained model or learned ranking asset;
``advanced-module``
    an extracted advanced module name reappearing in public ``code_context``.

In the private monorepo it also verifies the private side still provides the
extracted modules and the reranker asset.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

ROOT = Path(os.environ.get("LEMONCROW_OSS_AUDIT_ROOT", str(Path(__file__).resolve().parents[1]))).expanduser().resolve()
PUBLIC_CODE_CONTEXT = "src/lemoncrow/pro/capabilities/code_context"
PRIVATE_CODE_CONTEXT = ROOT / "enterprise" / "runtime" / "src" / "lemoncrow" / "pro" / "capabilities" / "code_context"
PRIVATE_MODEL = (
    ROOT
    / "enterprise"
    / "runtime"
    / "src"
    / "lemoncrow_enterprise_runtime"
    / "code_intelligence"
    / "explore_reranker_model.json"
)

ADVANCED_MODULES = {
    "ann_symbol_index.py",
    "cache.py",
    "call_graph.py",
    "call_graph_centrality.py",
    "edge_resolution.py",
    "edge_synthesis.py",
    "embedding.py",
    "engine.py",
    "eval_harness.py",
    "generated_files.py",
    "intel_store.py",
    "lsp_resolver.py",
    "markdown.py",
    "output_policy.py",
    "ppl_rank.py",
    "ranking.py",
    "rerank.py",
    "workspace_router.py",
}

#: Substrates whose only consumers are the private engine or the hosted server.
#: They moved private (enterprise/runtime, enterprise/server); a public checkout
#: must not carry them again.
PRIVATE_ONLY_PUBLIC_PATHS = (
    "src/lemoncrow/infra/code_intel/lsp",
    "src/lemoncrow/infra/code_intel/markdown.py",
    "src/lemoncrow/pro/code_intel/cross_lang",
    "src/lemoncrow/pro/code_intel/git_history/blame.py",
    "src/lemoncrow/pro/code_intel/git_history/embedder.py",
    "src/lemoncrow/pro/code_intel/git_history/summarizer.py",
    "src/lemoncrow/gateway/mcp_connectors.py",
    "src/lemoncrow/pro/capabilities/review/change_edits.py",
)

# Model / ANN / learned-ranking libraries. Public code may use neutral numeric
# libraries (numpy) but never load or train models or build ANN indexes.
MODEL_LIBRARIES = frozenset(
    {
        "accelerate",
        "annoy",
        "catboost",
        "faiss",
        "FlagEmbedding",
        "hnswlib",
        "jax",
        "lightgbm",
        "nmslib",
        "onnxruntime",
        "optimum",
        "peft",
        "pynndescent",
        "scann",
        "sentence_transformers",
        "sklearn",
        "tensorflow",
        "tokenizers",
        "torch",
        "transformers",
        "usearch",
        "voyager",
        "xgboost",
    }
)
MODEL_CLASSES = frozenset({"CrossEncoder", "SentenceTransformer"})

# Distributions that are private by construction. Discovered from enterprise/*
# in the monorepo; this static floor keeps the rule effective in a public
# checkout where enterprise/ does not exist.
PRIVATE_TOP_LEVEL_FLOOR = frozenset({"lemoncrow_server"})
PRIVATE_TOP_LEVEL_PREFIXES = ("lemoncrow_enterprise",)

# The only public modules allowed to name a private module: the provider seams
# that resolve the private implementation at runtime and degrade explicitly
# (ProEngineUnavailable / IndexLayerState.UNAVAILABLE) when it is absent. Keyed
# by module so the rule holds in the source tree and inside built wheels alike.
PROVIDER_SEAMS = {
    "lemoncrow.core.capabilities.code_intelligence_provider": frozenset(
        {"lemoncrow_enterprise_runtime.code_intelligence.provider", "lemoncrow_enterprise_runtime"}
    ),
    "lemoncrow.core.capabilities.private_runtime_provider": frozenset({"lemoncrow.gateway.cli.coding_engine"}),
    # The command registry adds `lc workspaces` (the private Workspaces server) only when it imports; a public
    # build has no such command.
    "lemoncrow.gateway.cli.commands": frozenset({"lemoncrow.gateway.cli.commands.workspaces_server"}),
    "lemoncrow_server_core.index.semantic": frozenset({"lemoncrow_server.semantic", "lemoncrow_server"}),
    # Hosted Workspaces verifies its entitlement with the private plane, and refuses to start configured without it.
    "lemoncrow.pro.workspaces.entitlement": frozenset({"lemoncrow_enterprise_licensing"}),
}
# Release/source audits must name the private distributions they reject. Their
# imports are still checked; only the private-module-string rule is waived.
BOUNDARY_GUARDS = frozenset({"scripts/audit_oss_source_boundary.py", "scripts/audit_release_boundary.py"})
# The audit's own regression suite is built from violating fixtures; it is the
# only file exempt from the content rules (exact path, reviewed).
AUDIT_FIXTURES = frozenset({"tests/test_oss_source_boundary_audit.py"})
# Calls whose string argument only probes for or matches a module name.
_PROBE_CALLS = frozenset({"find_spec", "startswith", "endswith"})

# Tokens that mark an approximate-nearest-neighbour / vector-index
# implementation when they appear in a module, class or function name.
ANN_TOKENS = frozenset({"ann", "annoy", "faiss", "hnsw", "ivf", "knn", "scann", "usearch"})
# Reviewed exceptions: a whole module/file, or one definition ("<module>::name").
ANN_ALLOWLIST = {
    "lemoncrow.pro.capabilities.archival_recall.ann": (
        "memory recall: exact brute-force cosine over stored passages; the HNSW index was removed"
    ),
    "tests/core/test_archival_ann.py": "tests of the exact memory-recall fallback above",
    "lemoncrow.pro.capabilities.archival_recall.capability::_ann_persist_path": "memory recall storage path",
    "lemoncrow.pro.capabilities.context_reuse.capability::_apply_ann_reranking": (
        "cosine re-rank of reused memory blocks"
    ),
    "lemoncrow.core.capabilities.session_replay_render::_ann_tokens": "'ann' = compaction annotation",
    "lemoncrow_server_core.index.ops::semantic_ann": "metric recorder; the semantic ANN is private",
}
ANN_NAME_ALLOWLIST = frozenset(
    {
        "visit_AnnAssign",  # ast.NodeVisitor hook for annotated assignments
    }
)

# Distribution names (PEP 503-normalised) of the libraries above, for public
# dependency manifests (pyproject.toml, requirements*.txt).
MODEL_DISTRIBUTIONS = frozenset(
    {
        "accelerate",
        "annoy",
        "catboost",
        "faiss-cpu",
        "faiss-gpu",
        "flagembedding",
        "hnswlib",
        "jax",
        "lightgbm",
        "nmslib",
        "onnxruntime",
        "onnxruntime-gpu",
        "optimum",
        "peft",
        "pynndescent",
        "scann",
        "scikit-learn",
        "sentence-transformers",
        "tensorflow",
        "tokenizers",
        "torch",
        "transformers",
        "usearch",
        "voyager",
        "xgboost",
    }
)
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

MODEL_ASSET_SUFFIXES = frozenset(
    {".ckpt", ".gguf", ".h5", ".joblib", ".mlmodel", ".npy", ".npz", ".onnx", ".pt", ".pth", ".safetensors", ".tflite"}
)
MODEL_ASSET_NAME = re.compile(r"(reranker|lambdamart|embedder|cross_encoder)[\w.-]*\.(json|bin|pkl|txt|model)$", re.I)

_PRUNE_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"})
_DOTTED = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    rule: str
    detail: str

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where} [{self.rule}] {self.detail}"


# --------------------------------------------------------------------------
# Which files are public
# --------------------------------------------------------------------------


def _load_mirror(root: Path) -> ModuleType | None:
    for candidate in (root / "scripts" / "mirror.py", Path(__file__).resolve().parent / "mirror.py"):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("_lemoncrow_mirror_rules", candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


def _candidate_files(root: Path) -> list[str]:
    """Tracked plus untracked-unignored files (git), else a filesystem walk."""

    if (root / ".git").exists():
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            names = {name for name in proc.stdout.decode("utf-8", "replace").split("\0") if name}
            return sorted(name for name in names if (root / name).is_file())
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for filename in filenames:
            found.append(Path(dirpath, filename).relative_to(root).as_posix())
    return sorted(found)


def public_files(root: Path) -> list[str]:
    files = _candidate_files(root)
    paths_file = root / "scripts" / "public-paths.txt"
    mirror = _load_mirror(root)
    if not paths_file.is_file() or mirror is None:
        return files
    prefixes = [
        line.strip().rstrip("/")
        for line in paths_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return [name for name in files if mirror.is_public(name, prefixes)]


# --------------------------------------------------------------------------
# Module universe
# --------------------------------------------------------------------------


def _module_of(path: str) -> tuple[str, bool] | None:
    """``(dotted module, is_package)`` for a .py file under a ``src/`` root."""

    parts = path.split("/")
    if not path.endswith(".py") or "src" not in parts:
        return None
    tail = parts[len(parts) - 1 - parts[::-1].index("src") + 1 :]
    if not tail:
        return None
    if tail[-1] == "__init__.py":
        return ".".join(tail[:-1]), True
    return ".".join([*tail[:-1], tail[-1].removesuffix(".py")]), False


@dataclass
class ModuleUniverse:
    public: set[str]
    public_packages: set[str]
    private: set[str]
    private_top_level: set[str]
    first_party_top_level: set[str]

    def is_private_top(self, module: str) -> bool:
        top = module.split(".", 1)[0]
        return top in self.private_top_level or top.startswith(PRIVATE_TOP_LEVEL_PREFIXES)

    def is_public_module(self, module: str) -> bool:
        return module in self.public or module in self.public_packages


def build_universe(root: Path, public: Iterable[str]) -> ModuleUniverse:
    public_modules: set[str] = set()
    public_packages: set[str] = set()
    for path in public:
        resolved = _module_of(path)
        if resolved is None:
            continue
        module, _ = resolved
        public_modules.add(module)
        parts = module.split(".")
        for i in range(1, len(parts)):
            public_packages.add(".".join(parts[:i]))  # namespace packages count
    private_modules: set[str] = set()
    private_top: set[str] = set(PRIVATE_TOP_LEVEL_FLOOR)
    for base in (root / "enterprise", root / "services"):
        if not base.is_dir():
            continue
        for src in base.glob("*/src"):
            for py in src.rglob("*.py"):
                if _PRUNE_DIRS.intersection(py.parts):
                    continue
                resolved = _module_of(py.relative_to(root).as_posix())
                if resolved is None:
                    continue
                module, _ = resolved
                if module not in public_modules:
                    private_modules.add(module)
                top = module.split(".", 1)[0]
                if top not in {m.split(".", 1)[0] for m in public_modules}:
                    private_top.add(top)
    first_party = {m.split(".", 1)[0] for m in public_modules} | private_top
    return ModuleUniverse(public_modules, public_packages, private_modules, private_top, first_party)


# --------------------------------------------------------------------------
# Per-file analysis
# --------------------------------------------------------------------------


def _name_tokens(name: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return {token.lower() for token in re.split(r"[_\W]+", spaced) if token}


def _resolve_relative(current: str | None, is_package: bool, level: int, module: str | None) -> str | None:
    if current is None:
        return None
    base = current.split(".") if is_package else current.split(".")[:-1]
    if level - 1 > len(base):
        return None
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join([*base, module] if module else base) or None


def _matching_constants(tree: ast.AST) -> set[int]:
    """ids of string constants that only probe for / match a module name.

    ``find_spec("x")``, ``name.startswith("x.")`` and comparison operands
    (``"x" not in text``, ``name == "x"``) name a module without using it.
    """

    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            exempt.update(id(item) for item in operands if isinstance(item, ast.Constant))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in _PROBE_CALLS:
                for arg in node.args:
                    items = arg.elts if isinstance(arg, ast.Tuple) else [arg]
                    exempt.update(id(item) for item in items if isinstance(item, ast.Constant))
    return exempt


class _FileAudit(ast.NodeVisitor):
    def __init__(
        self,
        path: str,
        universe: ModuleUniverse,
        current: tuple[str, bool] | None,
        *,
        line_offset: int = 0,
        exempt_constants: set[int] | None = None,
    ) -> None:
        self.path = path
        self.universe = universe
        self.current_module, self.current_is_package = current if current else (None, False)
        self.allow_keys = {path} | ({self.current_module} if self.current_module else set())
        self.allowed_private = frozenset().union(*(PROVIDER_SEAMS.get(key, frozenset()) for key in self.allow_keys))
        self.line_offset = line_offset
        self.exempt_constants = exempt_constants or set()
        self.file_allowlisted_ann = bool(self.allow_keys & ANN_ALLOWLIST.keys())
        self.violations: list[Violation] = []

    def _add(self, node: ast.AST, rule: str, detail: str) -> None:
        line = getattr(node, "lineno", 0)
        self.violations.append(Violation(self.path, line + self.line_offset if line else 0, rule, detail))

    # -- imports -------------------------------------------------------------
    def _check_module(self, node: ast.AST, module: str, *, names: Iterable[str] = ()) -> None:
        top = module.split(".", 1)[0]
        if top in MODEL_LIBRARIES:
            self._add(node, "model-library", f"imports {module}")
            return
        if self.universe.is_private_top(module):
            if module not in self.allowed_private:
                self._add(node, "private-import", f"imports private distribution module {module}")
            return
        if top not in self.universe.first_party_top_level:
            return
        if module in self.allowed_private:
            return
        if not self.universe.is_public_module(module):
            self._add(node, "private-import", f"imports {module}, which the public tree does not contain")
            return
        for name in names:
            candidate = f"{module}.{name}"
            if candidate in self.universe.private and not self.universe.is_public_module(candidate):
                self._add(node, "private-import", f"imports private module {candidate}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(node, alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = [alias.name for alias in node.names if alias.name != "*"]
        if node.level:
            module = _resolve_relative(self.current_module, self.current_is_package, node.level, node.module)
            if module is None:
                return
        else:
            module = node.module or ""
        if module:
            for name in names:
                if name in MODEL_CLASSES:
                    self._add(node, "model-class", f"imports {name}")
            self._check_module(node, module, names=names)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name in {"import_module", "__import__"} and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and _DOTTED.match(first.value):
                self._check_module(node, first.value)
        self.generic_visit(node)

    # -- names ---------------------------------------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in MODEL_CLASSES:
            self._add(node, "model-class", f"references {node.id}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in MODEL_CLASSES:
            self._add(node, "model-class", f"references {node.attr}")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if not isinstance(value, str):
            return
        if "import " in value:
            self._audit_embedded_code(node, value)
        if not _DOTTED.match(value) or id(node) in self.exempt_constants or self.path in BOUNDARY_GUARDS:
            return
        if self.universe.is_private_top(value) and value not in self.allowed_private:
            self._add(node, "private-module-string", f"names private module {value!r}")

    def _audit_embedded_code(self, node: ast.Constant, value: str) -> None:
        """Audit Python source carried in a string (``python -c`` helpers)."""

        try:
            embedded = ast.parse(value)
        except (SyntaxError, ValueError):
            return
        if not any(isinstance(item, (ast.Import, ast.ImportFrom)) for item in ast.walk(embedded)):
            return
        nested = _FileAudit(
            self.path,
            self.universe,
            None,
            line_offset=self.line_offset + node.lineno - 1,
            exempt_constants=_matching_constants(embedded),
        )
        nested.visit(embedded)
        self.violations.extend(
            Violation(item.path, item.line, item.rule, f"{item.detail} (embedded code)") for item in nested.violations
        )

    def _check_definition(self, node: ast.AST, name: str) -> None:
        if self.file_allowlisted_ann or name in ANN_NAME_ALLOWLIST:
            return
        if any(f"{key}::{name}" in ANN_ALLOWLIST for key in self.allow_keys):
            return
        if _name_tokens(name) & ANN_TOKENS:
            self._add(node, "ann-index", f"defines {name}")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_definition(node, node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_definition(node, node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check_definition(node, node.name)
        self.generic_visit(node)


_IMPORT_LINE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))")


def audit_python(path: str, source: str, universe: ModuleUniverse) -> list[Violation]:
    current = _module_of(path)
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        # Unparseable fixture: fall back to a line scan so it cannot hide imports.
        audit = _FileAudit(path, universe, current)
        for number, line in enumerate(source.splitlines(), start=1):
            match = _IMPORT_LINE.match(line)
            if match:
                node = ast.Pass(lineno=number, col_offset=0)
                audit._check_module(node, match.group(1) or match.group(2))
        return audit.violations
    audit = _FileAudit(path, universe, current, exempt_constants=_matching_constants(tree))
    audit.visit(tree)
    return audit.violations


def _normalise_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def audit_manifest(path: str, text: str) -> list[Violation]:
    """Public dependency manifests must not pull model / ANN libraries."""

    requirements: list[tuple[int, str]] = []
    if path.rsplit("/", 1)[-1] == "pyproject.toml":
        import tomllib

        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return [Violation(path, 0, "model-library", "unparseable pyproject.toml")]
        project = data.get("project", {})
        specs: list[str] = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            specs.extend(group)
        for group in data.get("dependency-groups", {}).values():
            specs.extend(item for item in group if isinstance(item, str))
        lines = text.splitlines()
        for spec in specs:
            line = next((i for i, raw in enumerate(lines, start=1) if spec in raw), 0)
            requirements.append((line, spec))
    else:
        for number, raw in enumerate(text.splitlines(), start=1):
            stripped = raw.split("#", 1)[0].strip()
            if stripped and not stripped.startswith("-"):
                requirements.append((number, stripped))
    violations: list[Violation] = []
    for line, spec in requirements:
        match = _REQUIREMENT_NAME.match(spec)
        if match and _normalise_distribution(match.group(1)) in MODEL_DISTRIBUTIONS:
            violations.append(Violation(path, line, "model-library", f"depends on {match.group(1)}"))
    return violations


def audit_paths(root: Path, files: Iterable[str]) -> list[Violation]:
    files = list(files)
    universe = build_universe(root, files)
    violations: list[Violation] = []
    for path in files:
        if path in AUDIT_FIXTURES:
            continue
        name = path.rsplit("/", 1)[-1]
        suffix = Path(name).suffix.lower()
        if suffix in MODEL_ASSET_SUFFIXES or MODEL_ASSET_NAME.search(name):
            violations.append(Violation(path, 0, "model-asset", "trained model or learned ranking asset"))
        if path.startswith(PUBLIC_CODE_CONTEXT + "/") and name in ADVANCED_MODULES:
            violations.append(Violation(path, 0, "advanced-module", "extracted advanced engine module"))
        if any(path == owned or path.startswith(owned + "/") for owned in PRIVATE_ONLY_PUBLIC_PATHS):
            violations.append(Violation(path, 0, "private-only-module", "dependency owned by a private distribution"))
        if name == "pyproject.toml" or (name.startswith("requirements") and suffix == ".txt"):
            try:
                violations.extend(audit_manifest(path, (root / path).read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                pass
            continue
        if suffix != ".py":
            continue
        module = _module_of(path)
        allowlisted = path in ANN_ALLOWLIST or (module is not None and module[0] in ANN_ALLOWLIST)
        if module and not allowlisted and _name_tokens(module[0].rsplit(".", 1)[-1]) & ANN_TOKENS:
            violations.append(Violation(path, 0, "ann-index", "module name says it implements an ANN index"))
        try:
            source = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        violations.extend(audit_python(path, source, universe))
    return sorted(set(violations))


def audit_wheel(members: dict[str, bytes]) -> list[Violation]:
    """Apply the content rules to a built wheel (``{archive name: bytes}``).

    Used by scripts/audit_release_boundary.py so released artifacts are held to
    the same rules as the source tree, independent of how they were assembled.
    Wheel members are analysed as if under a ``src/`` root and reported by their
    archive names.
    """

    py_names = [name for name in members if name.endswith(".py")]
    modules = {resolved[0] for name in py_names if (resolved := _module_of(f"src/{name}")) is not None}
    packages = {".".join(m.split(".")[:i]) for m in modules for i in range(1, len(m.split(".")))}
    universe = ModuleUniverse(modules, packages, set(), set(PRIVATE_TOP_LEVEL_FLOOR), set(PRIVATE_TOP_LEVEL_FLOOR))
    violations: list[Violation] = []
    for name, payload in members.items():
        base = name.rsplit("/", 1)[-1]
        suffix = Path(base).suffix.lower()
        if suffix in MODEL_ASSET_SUFFIXES or MODEL_ASSET_NAME.search(base):
            violations.append(Violation(name, 0, "model-asset", "trained model or learned ranking asset"))
        if name.endswith(".dist-info/METADATA"):
            for number, line in enumerate(payload.decode("utf-8", "replace").splitlines(), start=1):
                if line.startswith("Requires-Dist:"):
                    match = _REQUIREMENT_NAME.match(line.removeprefix("Requires-Dist:"))
                    if match and _normalise_distribution(match.group(1)) in MODEL_DISTRIBUTIONS:
                        violations.append(Violation(name, number, "model-library", f"depends on {match.group(1)}"))
            continue
        if suffix != ".py":
            continue
        module = _module_of(f"src/{name}")
        if module and module[0] not in ANN_ALLOWLIST and _name_tokens(module[0].rsplit(".", 1)[-1]) & ANN_TOKENS:
            violations.append(Violation(name, 0, "ann-index", "module name says it implements an ANN index"))
        for item in audit_python(f"src/{name}", payload.decode("utf-8", "replace"), universe):
            violations.append(Violation(name, item.line, item.rule, item.detail))
    return sorted(set(violations))


def _private_side_problems() -> list[str]:
    if not PRIVATE_CODE_CONTEXT.parent.exists():
        return []
    problems = [
        f"private code-intelligence module missing after extraction: {(PRIVATE_CODE_CONTEXT / name).relative_to(ROOT)}"
        for name in sorted(ADVANCED_MODULES)
        if not (PRIVATE_CODE_CONTEXT / name).is_file()
    ]
    if not PRIVATE_MODEL.is_file():
        problems.append(f"private reranker asset missing: {PRIVATE_MODEL.relative_to(ROOT)}")
    return problems


def iter_report(violations: list[Violation]) -> Iterator[str]:
    for violation in violations:
        yield violation.render()


def main() -> int:
    files = public_files(ROOT)
    violations = audit_paths(ROOT, files)
    if violations:
        print("advanced/private code intelligence leaked into public source:", file=sys.stderr)
        for line in iter_report(violations):
            print(f"  {line}", file=sys.stderr)
        return 1
    problems = _private_side_problems()
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(f"✓ OSS/private source boundary audit passed ({len(files)} public files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
