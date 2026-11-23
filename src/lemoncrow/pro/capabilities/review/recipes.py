"""Trusted-base check recipes (Phase 9R item 1).

A check recipe is one deterministic command -- test, lint, format, typecheck,
build or pre-commit -- that LemonCrow may execute on both sides of a Snapshot
in the hermetic sandbox. Recipes come only from the trusted base:

* ``.lemoncrow/environment.toml`` ``[[checks]]`` when it defines any: they are
  then the whole set, each run only when the change touches its ``paths``
  (:func:`environment_recipes`);
* otherwise ``.lemoncrow/review.yaml`` ``check_recipes`` rows (explicit
  configuration wins, per recipe id and per kind);
* otherwise discovery over base manifests: pre-commit config, package.json
  scripts, Makefile targets, pyproject/pytest/tox config, Cargo and Go.

Head content never defines what runs. A head-side edit of a recipe (config
row, script body, Makefile, hook config) is *proposed future policy*
(:func:`future_recipe_policy`) and, when the head changes the file that
defines a recipe's command, the head side of that recipe is not executed
(:func:`head_definition_guard`).

Recipe digest (stable, documented; R6 and P11-FED key on
``(base commit, environment fingerprint, recipe digest)``)::

    "recipe:sha256:" + sha256(canonical_json({
        "v": 1,
        "command": [argv exactly as executed],
        "cwd": "<repo-relative working directory>",
        "env": [["CI", "1"], ...],          # LEMONCROW_REPORT_DIR when a report is read
        "report": "junit" | "jest-json" | "go-json" | "cargo" | "",
        "detect_changes": true | false,
        "inputs": {"<path>": "sha256:<hex of trusted-base bytes>" | null},
    }))

``canonical_json`` is UTF-8 JSON with sorted keys and ``(",", ":")``
separators. ``inputs`` always contains the file that defines the command
(package.json, Makefile, .pre-commit-config.yaml, ...) plus the tool
configuration files the recipe reads; ``null`` records a declared input the
base does not have. Ids, titles, kinds and origins are presentation and do not
participate. The fix command of an autofix recipe has its own digest
(``variant: "fix"``) and never changes the check digest.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, Final, Literal, cast

import yaml
from lemoncrow_client.environment import (
    ENVIRONMENT_FILE,
    Check,
    DefinitionError,
    Environment,
    Fingerprint,
    is_attr_pathspec,
    parse_environment,
)

from lemoncrow.pro.environment.execution import REPORT_DIR, REPORT_ENV

RecipeKind = Literal["test", "lint", "format", "typecheck", "build", "precommit"]
RECIPE_KINDS: Final[tuple[str, ...]] = ("test", "lint", "format", "typecheck", "build", "precommit")
RecipeOrigin = Literal["config", "discovered"]
ReportFormat = Literal["", "junit", "jest-json", "go-json", "cargo"]
REPORT_FORMATS: Final[tuple[str, ...]] = ("", "junit", "jest-json", "go-json", "cargo")

#: The static run-plan entry each kind belongs to. ``build`` has none: a build
#: recipe is addressable only as ``checks.recipe:<id>`` (config) .
KIND_ENTRY: Final[Mapping[str, str]] = {
    "test": "checks.test",
    "lint": "checks.lint",
    "format": "checks.format",
    "typecheck": "checks.typecheck",
    "precommit": "checks.precommit",
}
#: The capability-detection fact each kind satisfies (``capability_catalog``).
KIND_FACT: Final[Mapping[str, str]] = {
    "test": "test_command",
    "lint": "lint",
    "format": "format",
    "typecheck": "typecheck",
    "precommit": "precommit",
}
RECIPE_ENTRY_PREFIX: Final[str] = "checks.recipe:"
#: The base-vs-head test behavior diff runs every test recipe on both sides.
TEST_DIFF_ENTRY: Final[str] = "behavior.test_diff"

#: The report file inside the runner's fresh report directory (``REPORT_DIR``).
REPORT_PATH: Final[str] = f"{REPORT_DIR}/report"
REPORT_PLACEHOLDER: Final[str] = "{report}"
#: A standalone ``command`` argument of a test recipe that receives the test files
#: test-impact selection chose (R6, item 19), relative to the recipe ``cwd``. A
#: full run removes it, so the command without arguments is the full suite.
TESTS_PLACEHOLDER: Final[str] = "{tests}"
#: More selected test files than this run the full suite (never a silent subset).
MAX_SELECTED_TEST_ARGS: Final[int] = 512
#: A standalone ``command`` argument of an environment.toml check that receives the
#: Change's paths, from which the command writes the ``$LC_CHANGED`` file.
CHANGED_PLACEHOLDER: Final[str] = "{changed}"
#: More changed-path bytes than this cannot be passed to an environment.toml check;
#: it is not run (never a silent subset).
MAX_CHANGED_BYTES: Final[int] = 256 * 1024
RECIPE_DIGEST_VERSION: Final[int] = 1
REVIEW_CONFIG_PATH: Final[str] = ".lemoncrow/review.yaml"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_RECIPES: Final[int] = 64
_MAX_ARGS: Final[int] = 64
_MAX_ARG_CHARS: Final[int] = 4000
_DEFAULT_TIMEOUT: Final[int] = 300
#: An environment.toml check without ``timeout``: the time limit Review gives it.
_ENVIRONMENT_TIMEOUT: Final[int] = 900
#: Bounds on the trusted base's ``.gitattributes`` files ``attr:`` pathspecs are matched with.
MAX_ATTRIBUTE_FILES: Final[int] = 256
MAX_ATTRIBUTE_BYTES: Final[int] = 256 * 1024
GITATTRIBUTES: Final[str] = ".gitattributes"
_MAX_MANIFESTS: Final[int] = 8


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_digest(text: str | None) -> str | None:
    if text is None:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckRecipe:
    """One trusted-base check command.

    ``command`` is the exact argv executed (the report placeholder already
    substituted). ``definition`` is the repository file whose content defines
    what the command does (Makefile, package.json, hook config); a head-side
    change to it is proposed future policy. ``detect_changes`` makes the check
    pass only when the command leaves the tree unchanged (formatters run in
    write mode, pre-commit). ``fix_command`` (format/lint/pre-commit) produces
    an autofix *proposal*; it never mutates source.

    ``paths`` (environment.toml checks only) are the Git pathspecs the change
    must touch for the recipe to run; such a recipe's command takes the
    changed paths for ``$LC_CHANGED`` (:data:`CHANGED_PLACEHOLDER`).
    ``environment`` and ``check`` are the parsed trusted-base definition it
    came from (its services, dependencies, toolchain and fingerprint).
    """

    id: str
    kind: RecipeKind
    command: tuple[str, ...]
    title: str = ""
    cwd: str = "."
    report: ReportFormat = ""
    inputs: tuple[str, ...] = ()
    definition: str = ""
    fix_command: tuple[str, ...] = ()
    detect_changes: bool = False
    timeout: int = _DEFAULT_TIMEOUT
    origin: RecipeOrigin = "discovered"
    source: str = ""
    input_digests: tuple[tuple[str, str | None], ...] = ()
    """``(path, sha256 of trusted-base bytes | None)`` -- filled by :func:`with_inputs`."""
    paths: tuple[str, ...] = ()
    environment: Environment | None = field(default=None, compare=False, repr=False)
    check: Check | None = field(default=None, compare=False, repr=False)
    part_targets: tuple[tuple[str, str], ...] = ()
    """Explicit (path, exact symbol) mapping from trusted-base config; not coverage proof."""

    @property
    def needs(self) -> tuple[str, ...]:
        """The declared services this check needs up while it runs."""

        return self.check.needs if self.check is not None else ()

    def definition_fingerprint(self, read_lockfile: Callable[[str], bytes | None]) -> Fingerprint | None:
        """The environment fingerprint ``lc verify`` records for this check (``None``: not defined there)."""

        if self.environment is None or self.check is None:
            return None
        return self.environment.fingerprint(self.check, read_lockfile)

    @property
    def entry_id(self) -> str:
        return f"{RECIPE_ENTRY_PREFIX}{self.id}"

    @property
    def selectable(self) -> bool:
        """Test-impact selection can pass test files to this recipe (``{tests}`` argument)."""

        return self.kind == "test" and TESTS_PLACEHOLDER in self.command

    def argv(self, tests: Sequence[str] | None = None, changed: Sequence[str] = ()) -> tuple[str, ...]:
        """The argv executed: ``{tests}`` becomes *tests* (cwd-relative), or nothing for a full run.

        ``command`` stays the template (it is what the recipe digest covers);
        more than :data:`MAX_SELECTED_TEST_ARGS` files run the full suite. An
        environment.toml check's ``{changed}`` becomes *changed*
        (:func:`changed_listing`), one bracketed argument per path, because the
        runner strips whitespace from each argument.
        """

        chosen = list(tests or ()) if tests is not None and len(tests) <= MAX_SELECTED_TEST_ARGS else []
        out: list[str] = []
        for part in self.command:
            if part == TESTS_PLACEHOLDER:
                out.extend(chosen)
            elif part == CHANGED_PLACEHOLDER and self.paths:
                out.extend(f"[{path}]" for path in changed)
            else:
                out.append(part)
        return tuple(out)

    @property
    def kind_entry(self) -> str:
        return KIND_ENTRY.get(self.kind, "")

    @property
    def env(self) -> tuple[tuple[str, str], ...]:
        return (("CI", "1"), (REPORT_ENV, REPORT_DIR)) if self.report else (("CI", "1"),)

    def digest_document(self, *, variant: Literal["check", "fix"] = "check") -> dict[str, Any]:
        document: dict[str, Any] = {
            "v": RECIPE_DIGEST_VERSION,
            "command": list(self.fix_command if variant == "fix" else self.command),
            "cwd": self.cwd,
            "env": [list(pair) for pair in self.env],
            "report": self.report if variant == "check" else "",
            "detect_changes": self.detect_changes if variant == "check" else True,
            "inputs": {path: value for path, value in self.input_digests},
        }
        if self.part_targets:
            document["part_targets"] = [list(row) for row in self.part_targets]
        if variant == "fix":
            document["variant"] = "fix"
        return document

    @property
    def digest(self) -> str:
        return "recipe:sha256:" + hashlib.sha256(canonical_json(self.digest_document())).hexdigest()

    @property
    def fix_digest(self) -> str:
        if not self.fix_command:
            return ""
        return "recipe:sha256:" + hashlib.sha256(canonical_json(self.digest_document(variant="fix"))).hexdigest()

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entry_id": self.entry_id,
            "kind": self.kind,
            "kind_entry": self.kind_entry,
            "title": self.title or self.id,
            "command": list(self.command),
            "cwd": self.cwd,
            "report": self.report,
            "granularity": "test" if self.report else "recipe",
            "inputs": [{"path": path, "digest": value} for path, value in self.input_digests],
            "definition": self.definition,
            "fix_command": list(self.fix_command),
            "detect_changes": self.detect_changes,
            "timeout": self.timeout,
            "origin": self.origin,
            "source": self.source,
            "part_targets": [{"path": path, "symbol": symbol} for path, symbol in self.part_targets],
            "digest": self.digest,
            "fix_digest": self.fix_digest,
        }


@dataclass(frozen=True, slots=True)
class QuarantineRule:
    """One trusted-base quarantined test: excluded from the verdict, always reported."""

    test: str
    recipe: str = ""
    reason: str = ""

    def matches(self, recipe_id: str, test_id: str) -> bool:
        if self.recipe and self.recipe != recipe_id:
            return False
        return fnmatch.fnmatchcase(test_id, self.test)

    def to_wire(self) -> dict[str, str]:
        return {"test": self.test, "recipe": self.recipe, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class CheckPolicy:
    """Trusted-base ``check_policy`` settings."""

    discover: bool = True
    reruns: int = 2
    quarantine: tuple[QuarantineRule, ...] = ()
    adequacy: bool = True
    autofix: bool = True
    #: Per-part proof on capture and in the reviewer's views (``check_policy.part_proof``); off stops it.
    part_proof: bool = True

    def quarantined(self, recipe_id: str, test_id: str) -> QuarantineRule | None:
        return next((rule for rule in self.quarantine if rule.matches(recipe_id, test_id)), None)

    def to_wire(self) -> dict[str, Any]:
        return {
            "discover": self.discover,
            "reruns": self.reruns,
            "quarantine": [rule.to_wire() for rule in self.quarantine],
            "adequacy": self.adequacy,
            "autofix": self.autofix,
            "part_proof": self.part_proof,
        }


@dataclass(frozen=True, slots=True)
class RecipeCatalog:
    """Every recipe the trusted base defines for one Snapshot."""

    recipes: tuple[CheckRecipe, ...] = ()
    policy: CheckPolicy = field(default_factory=CheckPolicy)
    errors: tuple[str, ...] = ()
    base_state: Literal["present", "absent", "provisional"] = "present"
    #: ``(recipe id, why)`` for environment.toml checks whose paths the change does not touch.
    untouched: tuple[tuple[str, str], ...] = ()

    def skipped(self, recipe_id: str) -> str:
        """Why the change does not run *recipe_id* (its paths are untouched), or ``""``."""

        return next((why for found, why in self.untouched if found == recipe_id), "")

    def get(self, recipe_id: str) -> CheckRecipe | None:
        return next((recipe for recipe in self.recipes if recipe.id == recipe_id), None)

    def configured(self) -> tuple[CheckRecipe, ...]:
        return tuple(recipe for recipe in self.recipes if recipe.origin == "config")

    def for_entry(self, entry_id: str) -> tuple[CheckRecipe, ...]:
        """The recipes one run-plan entry executes (``()`` when it names none)."""

        if entry_id.startswith(RECIPE_ENTRY_PREFIX):
            found = self.get(entry_id[len(RECIPE_ENTRY_PREFIX) :])
            return (found,) if found is not None else ()
        if entry_id == TEST_DIFF_ENTRY:
            return tuple(recipe for recipe in self.recipes if recipe.kind == "test")
        return tuple(recipe for recipe in self.recipes if recipe.kind_entry == entry_id)

    def kinds(self) -> frozenset[str]:
        return frozenset(recipe.kind for recipe in self.recipes)

    def to_wire(self) -> dict[str, Any]:
        return {
            "recipes": [recipe.to_wire() for recipe in self.recipes],
            "policy": self.policy.to_wire(),
            "errors": list(self.errors),
            "base_state": self.base_state,
            "untouched": [{"id": recipe_id, "reason": why} for recipe_id, why in self.untouched],
        }


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def _argv(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, str):
        try:
            parts = tuple(shlex.split(value))
        except ValueError:
            return None
    elif isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        parts = tuple(str(item) for item in value)
    else:
        return None
    if not parts or len(parts) > _MAX_ARGS or any(len(part) > _MAX_ARG_CHARS or "\0" in part for part in parts):
        return None
    return tuple(part.replace(REPORT_PLACEHOLDER, REPORT_PATH) for part in parts)


def _relative(value: Any) -> str | None:
    text = str(value or ".").strip() or "."
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\\" in text:
        return None
    normalized = path.as_posix()
    return "." if normalized in {"", "."} else normalized.strip("/")


def _join(cwd: str, name: str) -> str:
    return name if cwd in {"", "."} else f"{cwd}/{name}"


def _config_document(content: str) -> tuple[Mapping[str, Any] | None, str]:
    if not content.strip():
        return None, ""
    try:
        decoded = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return None, f"{REVIEW_CONFIG_PATH} is not valid YAML: {str(exc)[:200]}"
    if decoded is None:
        return None, ""
    if not isinstance(decoded, Mapping):
        return None, f"{REVIEW_CONFIG_PATH} must be a mapping"
    return decoded, ""


def parse_check_policy(content: str) -> tuple[CheckPolicy, tuple[str, ...]]:
    """``check_policy`` from trusted-base config; invalid values fall back to defaults and are reported."""

    decoded, problem = _config_document(content)
    if decoded is None:
        return CheckPolicy(), (problem,) if problem else ()
    raw = decoded.get("check_policy")
    if raw is None:
        return CheckPolicy(), ()
    if not isinstance(raw, Mapping):
        return CheckPolicy(), ("check_policy must be a mapping",)
    errors: list[str] = []
    discover = raw.get("discover", True)
    if not isinstance(discover, bool):
        errors.append("check_policy.discover must be true or false")
        discover = True
    reruns_raw = raw.get("reruns", 2)
    if isinstance(reruns_raw, bool) or not isinstance(reruns_raw, int) or not 0 <= reruns_raw <= 5:
        errors.append("check_policy.reruns must be an integer from 0 to 5")
        reruns_raw = 2
    flags: dict[str, bool] = {}
    for name in ("adequacy", "autofix", "part_proof"):
        value = raw.get(name, True)
        if not isinstance(value, bool):
            errors.append(f"check_policy.{name} must be true or false")
            value = True
        flags[name] = value
    rules: list[QuarantineRule] = []
    quarantine = raw.get("quarantine") or []
    if not isinstance(quarantine, list):
        errors.append("check_policy.quarantine must be a list")
        quarantine = []
    for index, row in enumerate(quarantine[:256]):
        if isinstance(row, str) and row.strip():
            rules.append(QuarantineRule(test=row.strip()[:400]))
        elif isinstance(row, Mapping) and str(row.get("test") or "").strip():
            rules.append(
                QuarantineRule(
                    test=str(row["test"]).strip()[:400],
                    recipe=str(row.get("recipe") or "").strip()[:64],
                    reason=str(row.get("reason") or "").strip()[:400],
                )
            )
        else:
            errors.append(f"check_policy.quarantine[{index}] must be a test pattern or {{test, recipe, reason}}")
    return (
        CheckPolicy(
            discover=discover,
            reruns=reruns_raw,
            quarantine=tuple(rules),
            adequacy=flags["adequacy"],
            autofix=flags["autofix"],
            part_proof=flags["part_proof"],
        ),
        tuple(errors),
    )


def parse_config_recipes(content: str) -> tuple[tuple[CheckRecipe, ...], tuple[str, ...]]:
    """``check_recipes`` rows; an invalid row is reported and never executed."""

    decoded, problem = _config_document(content)
    if decoded is None:
        return (), (problem,) if problem else ()
    raw = decoded.get("check_recipes")
    if raw is None:
        return (), ()
    if not isinstance(raw, list):
        return (), ("check_recipes must be a list",)
    recipes: list[CheckRecipe] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(raw):
        where = f"check_recipes[{index}]"
        if index >= _MAX_RECIPES:
            errors.append(f"check_recipes has more than {_MAX_RECIPES} rows; the rest are ignored")
            break
        if not isinstance(row, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        recipe_id = str(row.get("id") or "").strip()
        if not _ID.fullmatch(recipe_id):
            errors.append(f"{where}.id must match {_ID.pattern}")
            continue
        if recipe_id in seen:
            errors.append(f"{where}.id {recipe_id!r} is duplicated; neither copy runs")
            recipes = [recipe for recipe in recipes if recipe.id != recipe_id]
            continue
        seen.add(recipe_id)
        kind = str(row.get("kind") or "").strip()
        if kind not in RECIPE_KINDS:
            errors.append(f"{where}.kind must be one of {', '.join(RECIPE_KINDS)}")
            continue
        command = _argv(row.get("command"))
        if command is None:
            errors.append(f"{where}.command must be a non-empty argv list or shell-quoted string")
            continue
        placeholders = [part for part in command if TESTS_PLACEHOLDER in part]
        if placeholders and (kind != "test" or len(placeholders) > 1 or placeholders[0] != TESTS_PLACEHOLDER):
            errors.append(f"{where}.command may use {TESTS_PLACEHOLDER} once, as its own argument, in a test recipe")
            continue
        cwd = _relative(row.get("cwd"))
        if cwd is None:
            errors.append(f"{where}.cwd must be a relative path inside the repository")
            continue
        report = str(row.get("report") or "").strip()
        if report not in REPORT_FORMATS:
            errors.append(f"{where}.report must be one of {', '.join(item for item in REPORT_FORMATS if item)}")
            continue
        if report and report not in {"go-json", "cargo"} and not any(REPORT_PATH in part for part in command):
            errors.append(f"{where}.report {report} needs the {REPORT_PLACEHOLDER} placeholder in its command")
            continue
        fix: tuple[str, ...] = ()
        if row.get("fix_command") is not None:
            parsed_fix = _argv(row.get("fix_command"))
            if parsed_fix is not None and any(TESTS_PLACEHOLDER in part for part in parsed_fix):
                errors.append(f"{where}.fix_command cannot use {TESTS_PLACEHOLDER}")
                continue
            if parsed_fix is None:
                errors.append(f"{where}.fix_command must be a non-empty argv list or shell-quoted string")
                continue
            fix = parsed_fix
        inputs_raw = row.get("inputs") or []
        inputs: list[str] = []
        if not isinstance(inputs_raw, list):
            errors.append(f"{where}.inputs must be a list of repository paths")
            continue
        bad_input = False
        for item in inputs_raw[:32]:
            path = _relative(item)
            if path is None or path == ".":
                bad_input = True
                break
            inputs.append(path)
        if bad_input:
            errors.append(f"{where}.inputs must be relative repository file paths")
            continue
        timeout_raw = row.get("timeout", _DEFAULT_TIMEOUT)
        if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int) or not 1 <= timeout_raw <= 900:
            errors.append(f"{where}.timeout must be an integer number of seconds from 1 to 900")
            continue
        detect = row.get("detect_changes", False)
        if not isinstance(detect, bool):
            errors.append(f"{where}.detect_changes must be true or false")
            continue
        target_rows = row.get("part_targets") or []
        part_targets: list[tuple[str, str]] = []
        if target_rows:
            if kind != "test" or not isinstance(target_rows, list) or len(target_rows) > 16:
                errors.append(f"{where}.part_targets must be a list of up to 16 entries on a test recipe")
                continue
            invalid_target = False
            for candidate in target_rows:
                if not isinstance(candidate, Mapping):
                    invalid_target = True
                    break
                path = _relative(candidate.get("path"))
                symbol_raw = candidate.get("symbol")
                symbol = symbol_raw.strip() if isinstance(symbol_raw, str) else ""
                if (
                    path in (None, ".")
                    or not symbol
                    or len(symbol) > 200
                    or any(ord(char) < 32 or char in "*?" for char in symbol)
                ):
                    invalid_target = True
                    break
                part_targets.append((path, symbol))
            if invalid_target:
                errors.append(f"{where}.part_targets requires exact repo-relative paths and symbol names")
                continue
        definition = _command_definition(command, cwd)
        recipes.append(
            CheckRecipe(
                id=recipe_id,
                kind=cast(RecipeKind, kind),
                command=command,
                title=str(row.get("title") or recipe_id).strip()[:200],
                cwd=cwd,
                report=cast(ReportFormat, report),
                inputs=tuple(dict.fromkeys((*inputs, *((definition,) if definition else ())))),
                definition=definition,
                fix_command=fix,
                detect_changes=detect,
                timeout=timeout_raw,
                origin="config",
                source=f"{REVIEW_CONFIG_PATH} {where}",
                part_targets=tuple(dict.fromkeys(part_targets)),
            )
        )
    return tuple(recipes), tuple(errors)


_SCRIPT_RUNNERS: Final[tuple[str, ...]] = ("npm", "pnpm", "yarn", "bun")


def _command_definition(command: Sequence[str], cwd: str) -> str:
    """The repository file whose content defines what *command* does, if any."""

    head = PurePosixPath(command[0]).name if command else ""
    if head == "make":
        return _join(cwd, "Makefile")
    if head == "pre-commit":
        return _join(cwd, ".pre-commit-config.yaml")
    if head == "tox":
        return _join(cwd, "tox.ini")
    if head == "nox":
        return _join(cwd, "noxfile.py")
    if head in _SCRIPT_RUNNERS:
        return _join(cwd, "package.json")
    return ""


# --------------------------------------------------------------------------- #
# environment.toml checks (trusted base only)
# --------------------------------------------------------------------------- #

#: Runs in the same ``sh`` before an environment.toml check's ``run``. As in ``lc verify``,
#: ``$LC_CHANGED`` names a file listing the changed paths that exist in this tree, one per
#: line, and the check sees no arguments. Each argument is ``[path]`` (:meth:`CheckRecipe.argv`).
_CHANGED_PRELUDE: Final[str] = (
    'LC_CHANGED="${TMPDIR:-/tmp}/lc-changed.$$"\n'
    'for lc_arg in "$@"; do\n'
    "  lc_path=${lc_arg#?}\n"
    "  lc_path=${lc_path%?}\n"
    '  if [ -e "$lc_path" ]; then printf \'%s\\n\' "$lc_path"; fi\n'
    'done >"$LC_CHANGED" || exit 125\n'
    "unset lc_arg lc_path\n"
    "export LC_CHANGED\n"
    "set --\n"
)
#: Git's id of the empty blob: the pathspec index needs an object id, never the object.
_EMPTY_BLOB: Final[bytes] = b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def environment_recipes(content: str) -> tuple[tuple[CheckRecipe, ...], tuple[str, ...]]:
    """The ``[[checks]]`` of a trusted-base ``environment.toml`` as recipes, or the errors.

    The file is parsed by ``lemoncrow_client.environment``, the parser ``lc verify`` uses.
    Each check runs ``sh -c <run>`` from the repository root with ``$LC_CHANGED``, and only
    when the change touches its ``paths`` (:func:`touched_checks`). Its ``kind`` places it
    in that kind's entry (``checks.test`` ...); without one it is a ``build`` recipe,
    addressable only as ``checks.recipe:<id>``. Its ``timeout`` (default 900 s) bounds
    each run. A file Review cannot honor whole (invalid, or past a recipe bound) gives no
    recipe, so nothing stands in for it.
    """

    try:
        environment = parse_environment(content, source=ENVIRONMENT_FILE)
    except DefinitionError as exc:
        return (), (str(exc),)
    checks = environment.checks
    if len(checks) > _MAX_RECIPES:
        return (), (f"{ENVIRONMENT_FILE}: {len(checks)} checks; Review runs at most {_MAX_RECIPES}",)
    recipes: list[CheckRecipe] = []
    for index, check in enumerate(checks):
        where = f"{ENVIRONMENT_FILE}: checks[{index}] ({check.id})"
        if not _ID.fullmatch(check.id):
            return (), (f"{where}: Review needs an id of at most 64 characters",)
        script = _CHANGED_PRELUDE + check.run
        if len(script) > _MAX_ARG_CHARS or "\0" in script:
            limit = _MAX_ARG_CHARS - len(_CHANGED_PRELUDE)
            return (), (f"{where}: Review needs a run command of at most {limit} characters, without NUL",)
        try:
            definition = _command_definition(shlex.split(check.run), ".")
        except ValueError:
            definition = ""
        recipes.append(
            CheckRecipe(
                id=check.id,
                # Without a kind, a build recipe: it has no kind entry, so it runs only as checks.recipe:<id>.
                kind=cast(RecipeKind, check.kind or "build"),
                command=("sh", "-c", script, "sh", CHANGED_PLACEHOLDER),
                title=check.id,
                inputs=(ENVIRONMENT_FILE, *((definition,) if definition else ())),
                definition=definition,
                timeout=check.timeout or _ENVIRONMENT_TIMEOUT,
                origin="config",
                source=f"{ENVIRONMENT_FILE} checks[{index}]",
                paths=check.paths,
                environment=environment,
                check=check,
            )
        )
    return tuple(recipes), ()


def environment_definition(content: str | None) -> Environment | None:
    """The parsed trusted-base ``environment.toml``; ``None`` when absent or invalid."""

    if not content:
        return None
    try:
        return parse_environment(content, source=ENVIRONMENT_FILE)
    except DefinitionError:
        return None


def changed_listing(paths: Iterable[str]) -> tuple[str, ...]:
    """The Change's paths as an environment.toml check gets them: each once, sorted (as ``lc verify``)."""

    return tuple(sorted({path for path in paths if path}))


def changed_listing_refusal(paths: Sequence[str]) -> str:
    """Why *paths* (:func:`changed_listing`) cannot be passed to an environment.toml check, or ``""``."""

    size = sum(len(path.encode("utf-8", "surrogatepass")) + 3 for path in paths)
    if size <= MAX_CHANGED_BYTES:
        return ""
    return (
        f"the change lists {len(paths)} paths ({size} bytes as arguments); more than {MAX_CHANGED_BYTES} "
        "bytes cannot be passed for $LC_CHANGED, so the check is not run"
    )


def uses_attributes(recipe: CheckRecipe) -> bool:
    """Whether any of the recipe's pathspecs selects by Git attributes (``:(attr:...)``)."""

    return any(is_attr_pathspec(spec) for spec in recipe.paths)


def attribute_files(paths: Iterable[str] | None, read: Callable[[str], str | None]) -> dict[str, str] | None:
    """The trusted base's ``.gitattributes`` files (path -> text); ``None`` when they cannot all be read."""

    if paths is None:
        return None
    names = sorted(path for path in paths if path == GITATTRIBUTES or path.endswith("/" + GITATTRIBUTES))
    if len(names) > MAX_ATTRIBUTE_FILES:
        return None
    out: dict[str, str] = {}
    for name in names:
        text = read(name)
        if text is None or len(text.encode("utf-8", "surrogatepass")) > MAX_ATTRIBUTE_BYTES:
            return None
        out[name] = text
    return out


def _write_attributes(scratch: str, attributes: Mapping[str, str]) -> None:
    root = os.path.realpath(scratch)
    for name, text in attributes.items():
        parts = name.split("/")
        if parts[-1] != GITATTRIBUTES or any(part in {"", ".", "..", ".git"} for part in parts):
            raise ValueError(f"unusable attributes path {name[:120]!r}")
        target = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.commonpath([root, os.path.realpath(os.path.dirname(target))]) != root:
            raise ValueError(f"unusable attributes path {name[:120]!r}")
        with open(target, "x", encoding="utf-8", errors="surrogateescape") as handle:
            handle.write(text)


def touched_checks(
    recipes: Sequence[CheckRecipe], changed: Iterable[str], attributes: Mapping[str, str] | None = None
) -> tuple[dict[str, bool], str]:
    """Recipe id -> whether the changed paths touch its ``paths``, and ``""``; ``({}, why)`` if Git cannot say.

    Git matches each recipe's pathspecs against an index listing only the changed paths
    (an index needs no objects), so ``:!`` exclusions and every other pathspec form mean
    what they mean to ``lc verify``'s ``git diff --name-only``. Deleted paths are listed,
    so a deletion counts as a touch.

    An ``attr:`` pathspec reads the trusted base's ``.gitattributes`` files
    (*attributes*, see :func:`attribute_files`); never host or global attributes. When
    they are unknown, or the change edits a ``.gitattributes`` file, Git cannot say what
    such a recipe matches: it is left out of the map and *why* says so, so it runs.
    """

    wanted = [recipe for recipe in recipes if recipe.paths]
    names = changed_listing(changed)
    if not wanted or not names:
        return {recipe.id: False for recipe in wanted}, ""
    unknown = ""
    if any(uses_attributes(recipe) for recipe in wanted):
        if attributes is None:
            unknown = "the trusted base's .gitattributes could not be read, so its attr: pathspecs cannot be matched"
        elif any(name == GITATTRIBUTES or name.endswith("/" + GITATTRIBUTES) for name in names):
            unknown = "the change edits .gitattributes, so its attr: pathspecs cannot be matched on the trusted base"
    if unknown:
        wanted = [recipe for recipe in wanted if not uses_attributes(recipe)]
    # One index cannot hold both `a` and `a/b` (a file replaced by a directory). Group by how
    # many other changed paths are parent directories of a path: a group then has no such pair.
    present = set(names)
    groups: dict[int, list[str]] = {}
    for name in names:
        parts = name.split("/")
        depth = sum("/".join(parts[:end]) in present for end in range(1, len(parts)))
        groups.setdefault(depth, []).append(name)
    # Neither host Git settings nor a calling hook's GIT_DIR or GIT_INDEX_FILE steer the match.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    touched = {recipe.id: False for recipe in wanted}
    try:
        with tempfile.TemporaryDirectory(prefix="lc-pathspec-") as scratch:
            # The trusted base's attributes only: core.attributesFile would otherwise default
            # to the host's ~/.config/git/attributes.
            base = ["git", "-C", scratch, "-c", f"core.attributesFile={os.devnull}"]
            if attributes and not unknown:
                _write_attributes(scratch, attributes)

            def git(*args: str, stdin: bytes | None = None) -> bytes:
                done = subprocess.run(
                    [*base, *args], input=stdin, capture_output=True, env=env, timeout=60, check=False
                )
                if done.returncode != 0:
                    detail = done.stderr.decode("utf-8", "replace").strip()[:200]
                    raise RuntimeError(detail or f"git {args[0]} exited {done.returncode}")
                return done.stdout

            git("init", "-q")
            for group in groups.values():
                encoded = [name.encode("utf-8", "surrogateescape") for name in group]
                git("read-tree", "--empty")
                git(
                    "update-index",
                    "-z",
                    "--index-info",
                    stdin=b"".join(b"100644 %s\t%s\0" % (_EMPTY_BLOB, n) for n in encoded),
                )
                # Git skips a path it will not store (`..`, `.git`); unmatched, it would read as untouched.
                listed = set(git("ls-files", "-z").split(b"\0")) - {b""}
                if listed != set(encoded):
                    missing = sorted(set(encoded) - listed)[0].decode("utf-8", "replace")
                    raise ValueError(f"Git will not index the changed path {missing[:120]!r}")
                for recipe in wanted:
                    if not touched[recipe.id]:
                        touched[recipe.id] = bool(git("ls-files", "-z", "--", *recipe.paths))
            return touched, unknown
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return {}, f"Git could not match the paths: {exc}"[:300]


# --------------------------------------------------------------------------- #
# discovery (trusted base only)
# --------------------------------------------------------------------------- #


def _package_scripts(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    try:
        decoded = json.loads(text)
    except ValueError:
        return {}
    scripts = decoded.get("scripts") if isinstance(decoded, Mapping) else None
    return {str(k): str(v) for k, v in scripts.items() if isinstance(v, str)} if isinstance(scripts, Mapping) else {}


_NESTED_SCRIPT = re.compile(r"\b(?:npm|pnpm|yarn|bun)\b|\brun-[sp]\b|\bnpm-run-all\b|\bconcurrently\b")


def script_body_references_scripts(body: str) -> bool:
    """Whether a package.json script body runs other package scripts (head-controlled)."""

    return bool(_NESTED_SCRIPT.search(body))


def _makefile_targets(text: str) -> set[str]:
    return {match.group(1) for match in re.finditer(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", text, re.MULTILINE)}


def _section(text: str, header: str) -> bool:
    return re.search(rf"^\[{re.escape(header)}(\]|\.)", text, re.MULTILINE) is not None


def _script_recipe(
    scripts: Mapping[str, str],
    manifest: str,
    cwd: str,
    prefix: str,
    name: str,
    kind: RecipeKind,
    *,
    fix: str = "",
    detect: bool = False,
    label: str = "",
) -> CheckRecipe | None:
    body = scripts.get(name, "").strip()
    if not body:
        return None
    fix_body = scripts.get(fix, "").strip() if fix else ""
    return CheckRecipe(
        id=f"{prefix}-{label or name}".replace(":", "-")[:64],
        kind=kind,
        title=f"{manifest} scripts.{name}",
        # The trusted base's script body runs, never `npm run <name>`: a head
        # edit to the script is proposed policy, not executed.
        command=("sh", "-c", body),
        cwd=cwd,
        fix_command=("sh", "-c", fix_body) if fix_body else (),
        detect_changes=detect,
        inputs=(manifest,),
        definition=manifest,
        source=f"{manifest} scripts.{name}",
    )


def discover_recipes(paths: Iterable[str], read_base: Callable[[str], str | None]) -> tuple[CheckRecipe, ...]:
    """Recipes derivable from trusted-base manifests (deterministic order).

    Heuristics are deliberately narrow: a recipe exists only where a manifest
    states the tool. ``make`` targets are used only for kinds no ecosystem
    recipe already covers. tox/nox environments are not executed (they need
    their own network installs); their pytest configuration enables pytest.
    """

    names = set(paths)
    found: list[CheckRecipe] = []

    def add(recipe: CheckRecipe) -> None:
        if all(item.id != recipe.id for item in found):
            found.append(recipe)

    def existing(*candidates: str) -> tuple[str, ...]:
        return tuple(path for path in candidates if path in names)

    # ---- Python ----------------------------------------------------------
    pyproject = read_base("pyproject.toml") if "pyproject.toml" in names else None
    setup_cfg = read_base("setup.cfg") if "setup.cfg" in names else None
    tox_ini = read_base("tox.ini") if "tox.ini" in names else None
    py_text = pyproject or ""
    pytest_signals = (
        _section(py_text, "tool.pytest")
        or "pytest.ini" in names
        or "conftest.py" in names
        or bool(tox_ini and re.search(r"^\[(?:tool:)?pytest\]", tox_ini, re.MULTILINE))
        or bool(setup_cfg and re.search(r"^\[tool:pytest\]", setup_cfg, re.MULTILINE))
        or any(re.search(r"(^|/)tests?/test_[^/]+\.py$", path) for path in names)
    )
    if pytest_signals:
        add(
            CheckRecipe(
                id="pytest",
                kind="test",
                title="pytest",
                # Positional test files narrow collection (R6 test-impact selection).
                command=(
                    "python3",
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={REPORT_PATH}",
                    TESTS_PLACEHOLDER,
                ),
                report="junit",
                inputs=existing("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "conftest.py"),
                source="pytest configuration",
            )
        )
    ruff_files = existing("ruff.toml", ".ruff.toml")
    if ruff_files or _section(py_text, "tool.ruff"):
        inputs = (*ruff_files, *existing("pyproject.toml"))
        add(
            CheckRecipe(
                id="ruff-lint",
                kind="lint",
                title="ruff check",
                command=("ruff", "check", "."),
                fix_command=("ruff", "check", "--fix", "."),
                inputs=inputs,
                source="ruff configuration",
            )
        )
        if _section(py_text, "tool.ruff.format"):
            add(
                CheckRecipe(
                    id="ruff-format",
                    kind="format",
                    title="ruff format",
                    command=("ruff", "format", "--check", "."),
                    fix_command=("ruff", "format", "."),
                    inputs=inputs,
                    source="pyproject.toml [tool.ruff.format]",
                )
            )
    if _section(py_text, "tool.black"):
        add(
            CheckRecipe(
                id="black-format",
                kind="format",
                title="black --check",
                command=("black", "--check", "."),
                fix_command=("black", "."),
                inputs=("pyproject.toml",),
                source="pyproject.toml [tool.black]",
            )
        )
    mypy_files = existing("mypy.ini", ".mypy.ini")
    if mypy_files or _section(py_text, "tool.mypy"):
        add(
            CheckRecipe(
                id="mypy",
                kind="typecheck",
                title="mypy",
                command=("mypy", "."),
                inputs=(*mypy_files, *existing("pyproject.toml")),
                source="mypy configuration",
            )
        )
    if "pyrightconfig.json" in names:
        add(
            CheckRecipe(
                id="pyright",
                kind="typecheck",
                title="pyright",
                command=("pyright",),
                inputs=("pyrightconfig.json",),
                source="pyrightconfig.json",
            )
        )
    if ".flake8" in names:
        add(
            CheckRecipe(
                id="flake8", kind="lint", title="flake8", command=("flake8",), inputs=(".flake8",), source=".flake8"
            )
        )

    # ---- JavaScript / TypeScript ----------------------------------------
    manifests = sorted(
        (path for path in names if PurePosixPath(path).name == "package.json" and "node_modules/" not in path),
        key=lambda path: (path.count("/"), path),
    )[:_MAX_MANIFESTS]
    for manifest in manifests:
        cwd = PurePosixPath(manifest).parent.as_posix()
        cwd = "." if cwd in {"", "."} else cwd
        scripts = _package_scripts(read_base(manifest))
        prefix = "npm" if cwd == "." else "npm-" + re.sub(r"[^A-Za-z0-9._-]+", "-", cwd).strip("-")[:40]

        def script(name: str, kind: RecipeKind, *, fix: str = "", detect: bool = False, label: str = "") -> None:
            recipe = _script_recipe(
                scripts, manifest, cwd, prefix, name, kind, fix=fix, detect=detect, label=label  # noqa: B023
            )
            if recipe is not None:
                add(recipe)

        test_body = scripts.get("test", "")
        if test_body and "no test specified" not in test_body:
            script("test", "test")
        script("lint", "lint", fix="lint:fix")
        if scripts.get("typecheck"):
            script("typecheck", "typecheck")
        elif scripts.get("type-check"):
            script("type-check", "typecheck", label="typecheck")
        elif _join(cwd, "tsconfig.json") in names:
            add(
                CheckRecipe(
                    id=f"{prefix}-tsc"[:64],
                    kind="typecheck",
                    title=f"tsc --noEmit ({cwd})",
                    command=("npx", "--no-install", "tsc", "--noEmit"),
                    cwd=cwd,
                    inputs=(_join(cwd, "tsconfig.json"), manifest),
                    source=_join(cwd, "tsconfig.json"),
                )
            )
        check_name = next((name for name in ("format:check", "check:format") if scripts.get(name)), "")
        if check_name:
            script(check_name, "format", fix="format", label="format")
        elif scripts.get("format"):
            body = scripts["format"]
            if "--check" in body:
                script("format", "format")
            elif "--write" in body or re.search(r"\b(prettier|biome|dprint)\b", body):
                # A writer: the check is "running it changes nothing"; its changes are the autofix.
                script("format", "format", fix="format", detect=True)
        script("build", "build")

    # ---- Rust -------------------------------------------------------------
    if "Cargo.toml" in names:
        cargo_inputs = existing("Cargo.toml", "Cargo.lock", "rustfmt.toml", ".rustfmt.toml", "clippy.toml")
        add(
            CheckRecipe(
                id="cargo-test",
                kind="test",
                title="cargo test",
                command=(
                    "sh",
                    "-c",
                    f'cargo test --no-fail-fast >"{REPORT_PATH}" 2>&1; status=$?; tail -c 4000 "{REPORT_PATH}"; '
                    "exit $status",
                ),
                report="cargo",
                inputs=cargo_inputs,
                source="Cargo.toml",
            )
        )
        add(
            CheckRecipe(
                id="cargo-clippy",
                kind="lint",
                title="cargo clippy",
                command=("cargo", "clippy", "--all-targets", "--", "-D", "warnings"),
                inputs=cargo_inputs,
                source="Cargo.toml",
            )
        )
        add(
            CheckRecipe(
                id="cargo-fmt",
                kind="format",
                title="cargo fmt --check",
                command=("cargo", "fmt", "--check"),
                fix_command=("cargo", "fmt"),
                inputs=cargo_inputs,
                source="Cargo.toml",
            )
        )
        add(
            CheckRecipe(
                id="cargo-check",
                kind="typecheck",
                title="cargo check",
                command=("cargo", "check", "--all-targets"),
                inputs=cargo_inputs,
                source="Cargo.toml",
            )
        )

    # ---- Go ---------------------------------------------------------------
    if "go.mod" in names:
        go_inputs = existing("go.mod", "go.sum")
        add(
            CheckRecipe(
                id="go-test",
                kind="test",
                title="go test",
                command=(
                    "sh",
                    "-c",
                    f'go test -json ./... >"{REPORT_PATH}"; status=$?; tail -c 4000 "{REPORT_PATH}"; exit $status',
                ),
                report="go-json",
                inputs=go_inputs,
                source="go.mod",
            )
        )
        add(
            CheckRecipe(
                id="go-vet",
                kind="lint",
                title="go vet",
                command=("go", "vet", "./..."),
                inputs=go_inputs,
                source="go.mod",
            )
        )
        add(
            CheckRecipe(
                id="gofmt",
                kind="format",
                title="gofmt -l",
                command=(
                    "sh",
                    "-c",
                    'unformatted="$(gofmt -l .)"; test -z "$unformatted" || { echo "$unformatted"; exit 1; }',
                ),
                fix_command=("gofmt", "-w", "."),
                inputs=go_inputs,
                source="go.mod",
            )
        )

    # ---- pre-commit ---------------------------------------------------------
    if ".pre-commit-config.yaml" in names:
        add(
            CheckRecipe(
                id="pre-commit",
                kind="precommit",
                title="pre-commit run --all-files",
                command=("pre-commit", "run", "--all-files", "--show-diff-on-failure"),
                # Hooks rewrite files in place: re-running the command in a
                # disposable copy is the autofix proposal.
                fix_command=("pre-commit", "run", "--all-files"),
                inputs=(".pre-commit-config.yaml",),
                definition=".pre-commit-config.yaml",
                source=".pre-commit-config.yaml",
            )
        )

    # ---- Makefile (only where no ecosystem recipe covers the kind) ---------
    if "Makefile" in names:
        targets = _makefile_targets(read_base("Makefile") or "")
        covered: set[str] = {recipe.kind for recipe in found}
        for target, kind in (
            ("test", "test"),
            ("lint", "lint"),
            ("format-check", "format"),
            ("typecheck", "typecheck"),
            ("build", "build"),
        ):
            if target in targets and kind not in covered:
                covered.add(kind)
                add(
                    CheckRecipe(
                        id=f"make-{target}",
                        kind=cast(RecipeKind, kind),
                        title=f"make {target}",
                        command=("make", target),
                        inputs=("Makefile",),
                        definition="Makefile",
                        source=f"Makefile {target} target",
                    )
                )

    return tuple(
        replace(
            recipe, inputs=tuple(dict.fromkeys((*recipe.inputs, *((recipe.definition,) if recipe.definition else ()))))
        )
        for recipe in found
    )


def recipe_digest(recipe: CheckRecipe) -> str:
    """The deterministic recipe digest, ``recipe:sha256:<hex>`` (definition in the module docstring).

    It is computed from the argv exactly as executed, the working directory,
    the fixed environment, the report format, ``detect_changes`` and the
    trusted-base content digest of every input file (``with_inputs``). The
    same trusted base always yields the same digest; any change to the
    command or to a configuration input changes it. Run caches and CI
    equivalence key on ``(base commit, environment fingerprint, recipe digest)``.
    """

    return recipe.digest


def entry_recipe_digest(recipes: Sequence[CheckRecipe]) -> str:
    """One recipe digest for a run-plan entry that runs *recipes*.

    A single recipe's entry carries that recipe's :func:`recipe_digest`; an
    entry over several recipes (``checks.test`` with two test recipes) carries
    ``"recipeset:sha256:" + sha256(canonical_json(sorted recipe digests))``;
    an entry with none carries ``""`` (unknown, which never meets a pinned digest).
    """

    digests = sorted({recipe_digest(recipe) for recipe in recipes})
    if not digests:
        return ""
    if len(digests) == 1:
        return digests[0]
    return "recipeset:sha256:" + hashlib.sha256(canonical_json(digests)).hexdigest()


def entry_recipe_digests(catalog: RecipeCatalog) -> dict[str, str]:
    """Run-plan entry id -> recipe digest for every entry the catalog can publish a result under."""

    out = {recipe.entry_id: recipe_digest(recipe) for recipe in catalog.recipes}
    for kind_entry in sorted({recipe.kind_entry for recipe in catalog.recipes if recipe.kind_entry}):
        out[kind_entry] = entry_recipe_digest(catalog.for_entry(kind_entry))
    return out


def with_inputs(recipe: CheckRecipe, read_base: Callable[[str], str | None]) -> CheckRecipe:
    """Pin the recipe's inputs to trusted-base content digests (part of the recipe digest)."""

    return replace(
        recipe, input_digests=tuple((path, content_digest(read_base(path))) for path in sorted(set(recipe.inputs)))
    )


def build_recipe_catalog(
    config_content: str,
    base_paths: Iterable[str] | None,
    read_base: Callable[[str], str | None],
    *,
    base_state: Literal["present", "absent", "provisional"] = "present",
    environment: str | None = None,
) -> RecipeCatalog:
    """Every recipe of the trusted base: explicit configuration wins over discovery.

    Configuration wins per id and per kind: once the trusted base configures a
    recipe of a kind, discovered recipes of that kind are dropped. A base tree
    that cannot be read (``None``) discovers nothing (provisional), so no
    recipe is implied.

    *environment* is the trusted base's ``environment.toml`` text (``None``
    when absent). When it defines checks they are the only recipes: neither
    ``check_recipes`` nor discovery adds any. When it is invalid there are no
    recipes and its error is reported. ``check_policy`` applies either way.
    """

    policy, policy_errors = parse_check_policy(config_content)
    if environment:
        defined, environment_errors = environment_recipes(environment)
        if defined or environment_errors:
            return RecipeCatalog(
                recipes=tuple(with_inputs(recipe, read_base) for recipe in defined),
                policy=policy,
                errors=(*environment_errors, *policy_errors),
                base_state="provisional" if base_paths is None else base_state,
            )
    configured, config_errors = parse_config_recipes(config_content)
    discovered: tuple[CheckRecipe, ...] = ()
    state = base_state
    if base_paths is None:
        state = "provisional"
    elif policy.discover:
        discovered = discover_recipes(list(base_paths), read_base)
    configured_ids = {recipe.id for recipe in configured}
    configured_kinds = {recipe.kind for recipe in configured}
    merged = (
        *configured,
        *(recipe for recipe in discovered if recipe.id not in configured_ids and recipe.kind not in configured_kinds),
    )
    return RecipeCatalog(
        recipes=tuple(with_inputs(recipe, read_base) for recipe in merged),
        policy=policy,
        errors=(*config_errors, *policy_errors),
        base_state=state,
    )


# --------------------------------------------------------------------------- #
# head-side edits: proposed future policy, never executed
# --------------------------------------------------------------------------- #


def head_definition_guard(
    recipe: CheckRecipe,
    changed_paths: Iterable[str],
    read_base: Callable[[str], str | None],
    read_head: Callable[[str], str | None],
) -> str:
    """Why the head side of *recipe* must not execute, or ``""``.

    A head change to the file defining the command would make the head run a
    command the trusted base never approved. package.json is precise: the
    base script body is what runs, so only a body that calls other package
    scripts is exposed to head edits of those scripts.
    """

    definition = recipe.definition
    if not definition or definition not in set(changed_paths):
        return ""
    if PurePosixPath(definition).name == "package.json":
        body = " ".join(recipe.command[2:]) if recipe.command[:2] == ("sh", "-c") else " ".join(recipe.command)
        uses_scripts = script_body_references_scripts(body)
        if not uses_scripts:
            return ""
        if _package_scripts(read_base(definition)) == _package_scripts(read_head(definition)):
            return ""
    return (
        f"the Change edits {definition}, which defines this recipe; the edit is proposed future policy "
        "for later Reviews and is not executed on the head side"
    )


def future_recipe_policy(base: RecipeCatalog, head: RecipeCatalog) -> dict[str, Any]:
    """Head-side recipe edits as proposed policy that applies only after the Change lands."""

    before = {recipe.id: recipe for recipe in base.recipes}
    after = {recipe.id: recipe for recipe in head.recipes}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        recipe_id
        for recipe_id in set(before) & set(after)
        if before[recipe_id].digest_document()["command"] != after[recipe_id].digest_document()["command"]
        or before[recipe_id].kind != after[recipe_id].kind
        or before[recipe_id].cwd != after[recipe_id].cwd
        or before[recipe_id].report != after[recipe_id].report
    )
    policy_changed = base.policy != head.policy
    any_change = bool(added or removed or changed or policy_changed)
    return {
        "changed": any_change,
        "applies_to": "later Reviews after this Change lands" if any_change else "",
        "added": [after[recipe_id].to_wire() for recipe_id in added],
        "removed": removed,
        "changed_recipes": [
            {"id": recipe_id, "base": before[recipe_id].to_wire(), "head": after[recipe_id].to_wire()}
            for recipe_id in changed
        ],
        "policy": head.policy.to_wire() if policy_changed else None,
        "errors": list(head.errors) if any_change else [],
    }


__all__ = [
    "CHANGED_PLACEHOLDER",
    "ENVIRONMENT_FILE",
    "GITATTRIBUTES",
    "KIND_ENTRY",
    "KIND_FACT",
    "MAX_CHANGED_BYTES",
    "RECIPE_ENTRY_PREFIX",
    "RECIPE_KINDS",
    "REPORT_DIR",
    "REPORT_ENV",
    "REPORT_PATH",
    "TESTS_PLACEHOLDER",
    "TEST_DIFF_ENTRY",
    "CheckPolicy",
    "CheckRecipe",
    "QuarantineRule",
    "RecipeCatalog",
    "attribute_files",
    "build_recipe_catalog",
    "changed_listing",
    "changed_listing_refusal",
    "discover_recipes",
    "entry_recipe_digest",
    "entry_recipe_digests",
    "environment_definition",
    "environment_recipes",
    "future_recipe_policy",
    "head_definition_guard",
    "parse_check_policy",
    "parse_config_recipes",
    "recipe_digest",
    "touched_checks",
    "uses_attributes",
    "with_inputs",
]
