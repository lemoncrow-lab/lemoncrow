"""Which test framework a repository uses, read only from the trusted base commit.

A ``kind: test`` recipe in the base's ``.lemoncrow/review.yaml`` ``check_recipes`` that takes a ``{tests}``
argument comes first. Otherwise a narrow detection: pytest from the trusted-base manifests (the same rule
:func:`recipes.discover_recipes` uses), jest or vitest from ``package.json``, ``go test`` from ``go.mod`` and
``cargo test`` from ``Cargo.toml``. The pull request's own files never choose the command: the plan and run
steps both detect from the merge base.

The model writes only a test file's content. The file's path and the command that runs it are decided here.
"""

from __future__ import annotations

import json
import posixpath
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lemoncrow.pro.capabilities.review.recipes import (
    REPORT_PATH,
    REVIEW_CONFIG_PATH,
    TESTS_PLACEHOLDER,
    discover_recipes,
    parse_config_recipes,
)

LANGUAGE_SUFFIXES: Final[Mapping[str, tuple[str, ...]]] = {
    "python": (".py",),
    "js": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"),
    "go": (".go",),
    "rust": (".rs",),
}
_LANGUAGE_OF_FRAMEWORK: Final[Mapping[str, str]] = {
    "pytest": "python",
    "jest": "js",
    "vitest": "js",
    "go": "go",
    "cargo": "rust",
}
#: Test files a framework runs, by path (relative to the framework's working directory).
_RUNNABLE: Final[Mapping[str, re.Pattern[str]]] = {
    "python": re.compile(r"(^|/)test_[^/]*\.py$|_test\.py$"),
    "js": re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$"),
    "go": re.compile(r"_test\.go$"),
    "rust": re.compile(r"^tests/[^/]+\.rs$"),
}
#: ``go test`` and ``cargo test`` print, they do not write a report file: the script sends the output to the
#: report path (``$0``) and prints its tail, keeping the exit status. Arguments are positional, never interpolated.
_GO_SCRIPT: Final[str] = 'go test -json -count=1 "$@" > "$0" 2>&1; s=$?; tail -c 6000 "$0"; exit $s'
_CARGO_SCRIPT: Final[str] = 'cargo test --no-fail-fast "$@" > "$0" 2>&1; s=$?; tail -c 6000 "$0"; exit $s'
_MAX_BLOB: Final[int] = 1024 * 1024


class GitTree:
    """One commit's tree, read from the object store (never from a working tree)."""

    def __init__(self, repo: Path, sha: str) -> None:
        self.repo = repo
        self.sha = sha
        self._paths: tuple[str, ...] | None = None

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, timeout=60, check=False)

    def paths(self) -> tuple[str, ...]:
        if self._paths is None:
            done = self._git("ls-tree", "-r", "-z", "--name-only", self.sha)
            if done.returncode != 0:
                raise ValueError(f"cannot list commit {self.sha[:12]}: {done.stderr.decode(errors='replace').strip()}")
            self._paths = tuple(sorted(item.decode("utf-8", "replace") for item in done.stdout.split(b"\0") if item))
        return self._paths

    def read(self, path: str) -> str | None:
        """The file's text at this commit, or ``None`` when it is absent (or larger than 1 MiB)."""

        if path not in set(self.paths()):
            return None
        done = self._git("cat-file", "blob", f"{self.sha}:{path}")
        if done.returncode != 0 or len(done.stdout) > _MAX_BLOB:
            return None
        return done.stdout.decode("utf-8", "replace")


@dataclass(frozen=True, slots=True)
class Framework:
    """How one language's tests run in this repository."""

    id: str
    language: str
    #: The argv, with :data:`recipes.TESTS_PLACEHOLDER` where the test arguments go and
    #: :data:`recipes.REPORT_PATH` where the report is written.
    template: tuple[str, ...]
    report: str = ""
    cwd: str = "."
    source: str = ""

    def relative(self, path: str) -> str:
        """*path* (repository-relative) relative to the framework's working directory."""

        return path if self.cwd in {"", "."} else posixpath.relpath(path, self.cwd)

    def covers(self, path: str) -> bool:
        return self.cwd in {"", "."} or path.startswith(self.cwd.rstrip("/") + "/")

    def runnable(self, path: str) -> bool:
        """Whether *path* is a test file this framework runs."""

        return self.covers(path) and bool(_RUNNABLE[self.language].search(self.relative(path)))

    def argv(self, files: Sequence[str], *, names: Sequence[str] = ()) -> tuple[str, ...]:
        """The command for *files* (repository-relative test files); *names* narrows a ``go test`` package run."""

        relative = [self.relative(path) for path in files]
        if self.id == "go":
            folders = (posixpath.dirname(path) for path in relative)
            arguments = list(dict.fromkeys(f"./{folder}" if folder else "." for folder in folders))
            if names:
                arguments += ["-run", "^(" + "|".join(names) + ")$"]
        elif self.id == "cargo":
            arguments = [
                part for path in relative for part in ("--test", posixpath.splitext(posixpath.basename(path))[0])
            ]
        else:
            arguments = relative
        out: list[str] = []
        for part in self.template:
            if part == TESTS_PLACEHOLDER:
                out.extend(arguments)
            else:
                out.append(part)
        return tuple(out)


@dataclass(frozen=True, slots=True)
class Detection:
    frameworks: Mapping[str, Framework]
    notes: tuple[str, ...] = ()

    def for_path(self, path: str) -> Framework | None:
        language = language_of(path)
        return self.frameworks.get(language) if language else None


def language_of(path: str) -> str:
    suffix = posixpath.splitext(path)[1].lower()
    return next((language for language, suffixes in LANGUAGE_SUFFIXES.items() if suffix in suffixes), "")


def _configured_id(command: Sequence[str]) -> str:
    joined = " ".join(command)
    for framework, pattern in (
        ("pytest", r"\bpytest\b"),
        ("vitest", r"\bvitest\b"),
        ("jest", r"\bjest\b"),
        ("go", r"(^|\s|/)go\s+test\b"),
        ("cargo", r"\bcargo\b"),
    ):
        if re.search(pattern, joined):
            return framework
    return ""


def detect(tree: GitTree) -> Detection:
    """The frameworks of the base commit *tree*, by language; the notes say what was set aside and why."""

    found: dict[str, Framework] = {}
    notes: list[str] = []
    config = tree.read(REVIEW_CONFIG_PATH)
    if config:
        configured, problems = parse_config_recipes(config)
        notes.extend(f"{REVIEW_CONFIG_PATH}: {problem}" for problem in problems)
        for recipe in configured:
            if recipe.kind != "test":
                continue
            framework_id = _configured_id(recipe.command)
            if not framework_id:
                notes.append(f"check_recipes {recipe.id}: not a pytest, jest, vitest, go test or cargo test command")
                continue
            if not recipe.selectable:
                notes.append(
                    f"check_recipes {recipe.id}: has no {TESTS_PLACEHOLDER} argument, so it cannot run one file"
                )
                continue
            language = _LANGUAGE_OF_FRAMEWORK[framework_id]
            found.setdefault(
                language,
                Framework(
                    framework_id,
                    language,
                    recipe.command,
                    report=recipe.report,
                    cwd=recipe.cwd or ".",
                    source=f"base {REVIEW_CONFIG_PATH} check_recipes {recipe.id}",
                ),
            )
    paths = tree.paths()
    names = set(paths)
    if "python" not in found:
        pytest = next((recipe for recipe in discover_recipes(paths, tree.read) if recipe.id == "pytest"), None)
        if pytest is not None:
            found["python"] = Framework("pytest", "python", pytest.command, report=pytest.report, source=pytest.source)
    if "js" not in found and "package.json" in names:
        try:
            manifest = json.loads(tree.read("package.json") or "{}")
        except ValueError:
            manifest = {}
            notes.append("package.json at the base does not parse")
        declared: set[str] = set()
        for key in ("dependencies", "devDependencies"):
            block = manifest.get(key) if isinstance(manifest, dict) else None
            if isinstance(block, dict):
                declared.update(str(name) for name in block)
        if "vitest" in declared:
            found["js"] = Framework(
                "vitest",
                "js",
                (
                    "npx",
                    "--no-install",
                    "vitest",
                    "run",
                    "--reporter=json",
                    f"--outputFile={REPORT_PATH}",
                    TESTS_PLACEHOLDER,
                ),
                report="jest-json",
                source="package.json declares vitest",
            )
        elif "jest" in declared:
            found["js"] = Framework(
                "jest",
                "js",
                (
                    "npx",
                    "--no-install",
                    "jest",
                    "--ci",
                    "--runInBand",
                    "--json",
                    f"--outputFile={REPORT_PATH}",
                    TESTS_PLACEHOLDER,
                ),
                report="jest-json",
                source="package.json declares jest",
            )
    if "go" not in found and "go.mod" in names:
        found["go"] = Framework(
            "go", "go", ("sh", "-c", _GO_SCRIPT, REPORT_PATH, TESTS_PLACEHOLDER), report="go-json", source="go.mod"
        )
    if "rust" not in found and "Cargo.toml" in names:
        found["rust"] = Framework(
            "cargo",
            "rust",
            ("sh", "-c", _CARGO_SCRIPT, REPORT_PATH, TESTS_PLACEHOLDER),
            report="cargo",
            source="Cargo.toml",
        )
    return Detection(found, tuple(notes))


def _nearest(directories: Sequence[str], path: str) -> str:
    """The directory of *directories* sharing the longest leading path with *path* (the shortest on a tie)."""

    target = path.split("/")[:-1]

    def shared(directory: str) -> int:
        parts = [] if directory in {"", "."} else directory.split("/")
        common = 0
        for left, right in zip(parts, target, strict=False):
            if left != right:
                break
            common += 1
        return common

    return max(sorted(directories, key=lambda item: (len(item), item)), key=shared)


def existing_tests(framework: Framework, paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(path for path in paths if framework.runnable(path))


def repro_path(framework: Framework, finding_path: str, slug: str, paths: Sequence[str]) -> str:
    """Where the reproduction for a finding in *finding_path* is written (repository-relative)."""

    root = "" if framework.cwd in {"", "."} else framework.cwd.rstrip("/")
    folder = posixpath.dirname(finding_path)
    if framework.language == "python":
        directories = sorted({posixpath.dirname(path) for path in existing_tests(framework, paths)})
        chosen = _nearest(directories, finding_path) if directories else posixpath.join(root, "tests")
        return posixpath.join(chosen, f"test_lemoncrow_proof_{slug}.py")
    if framework.language == "js":
        typed = posixpath.splitext(finding_path)[1].lower() in {".ts", ".tsx", ".mts", ".cts"}
        return posixpath.join(folder, f"lemoncrow-proof-{slug}.test.{'ts' if typed else 'js'}")
    if framework.language == "go":
        return posixpath.join(folder, f"lemoncrow_proof_{slug}_test.go")
    return posixpath.join(root, "tests", f"lemoncrow_proof_{slug}.rs")


def example_test(framework: Framework, finding_path: str, tree: GitTree, *, limit: int = 3000) -> tuple[str, str]:
    """``(path, text)`` of the base's existing test file nearest *finding_path*, for the model to imitate."""

    tests = existing_tests(framework, tree.paths())
    if not tests:
        return "", ""
    folder = _nearest(sorted({posixpath.dirname(path) for path in tests}), finding_path)
    path = next(item for item in tests if posixpath.dirname(item) == folder)
    return path, (tree.read(path) or "")[:limit]


def defines_test(framework: Framework, code: str, name: str) -> bool:
    """Whether *code* defines the test *name* the way *framework* finds tests."""

    escaped = re.escape(name)
    pattern = {
        "python": rf"^\s*(?:async\s+)?def\s+{escaped}\s*\(",
        "js": rf"\b(?:it|test)\s*\(\s*(['\"`]){escaped}\1",
        "go": rf"^func\s+{escaped}\s*\(\s*\w+\s+\*testing\.T\s*\)",
        "rust": rf"#\[test\]\s*(?:#\[[^\]]*\]\s*)*fn\s+{escaped}\s*\(",
    }[framework.language]
    return bool(re.search(pattern, code, re.MULTILINE))


def name_rule(framework: Framework) -> str:
    """What the test's name must look like, in words the prompt uses."""

    return {
        "python": "a pytest function name starting with test_",
        "js": "a title made of letters, digits and underscores (used as the it()/test() title)",
        "go": "a Go test function name starting with Test",
        "rust": "a Rust #[test] function name",
    }[framework.language]


def name_allowed(framework: Framework, name: str) -> bool:
    if framework.language == "python":
        return name.startswith("test_")
    if framework.language == "go":
        return name.startswith("Test")
    return True
