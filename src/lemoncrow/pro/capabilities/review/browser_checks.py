"""Snapshot-bound browser/UI regression evidence for Review.

Execution stays where it already lives: configured Playwright/browser tests,
CI, an external browser agent, or a local verification hook. Review owns only
the provider-neutral normalization and the binding of each observed run to one
exact immutable Snapshot side.

The verdict is computed here from inspectable, deterministic checks (scenario
completion, assertions, navigation outcome, console/runtime errors, and a
visual threshold only when the repository explicitly configured one). A
reporter's or model's "looks correct" never becomes PASS by itself; free-form
observations are kept as labelled evidence, not as a verdict.

Policy (which scenarios exist, which are required, trusted preview origins)
is read from ``.lemoncrow/review.yaml`` of the Change's *base* side first, so a
Change cannot weaken the browser checks that gate its own landing.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any
from urllib.parse import urlparse

import yaml

from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision

BROWSER_CHECK_MEDIA_TYPE = "application/vnd.lemoncrow.browser-check+json"
BROWSER_SOURCE_REF_PREFIX = "browser:"

_SCENARIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_VIEWPORT = re.compile(r"^(?P<w>\d{2,5})x(?P<h>\d{2,5})$")
_STATUSES = frozenset({"PASS", "FAIL", "NOT_RUN", "UNKNOWN"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ARTIFACT_ROLES: Mapping[str, frozenset[str]] = {
    "screenshot": frozenset({"screenshot", "image"}),
    "diff": frozenset({"image", "screenshot"}),
    "trace": frozenset({"playwright_trace"}),
    "video": frozenset({"video"}),
}
_MAX_ASSERTIONS = 200
_MAX_CONSOLE = 100
_MAX_RUNTIME_ERRORS = 50
_MAX_OBSERVATIONS = 20


#: Saved step actions ``lc review qa run`` replays, in the order written.
BROWSER_STEP_ACTIONS = ("goto", "click", "fill", "expect_text", "expect_visible", "expect_url", "screenshot")
_SECRET_REF = re.compile(r"\$\{secrets\.([A-Za-z_][A-Za-z0-9_]{0,63})\}")
_SCREENSHOT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ARIA_ROLE = re.compile(r"^[a-z]{2,30}$")
_MAX_STEPS = 50
_MAX_STEP_TEXT = 500


@dataclass(frozen=True)
class BrowserStep:
    """One saved, deterministic step of a browser scenario.

    A target is an accessible ``role`` (+ optional accessible ``name``), a
    visible ``text`` or a form ``label``. ``value`` (``fill`` only) may hold
    ``${secrets.NAME}`` references that are resolved from the runner's
    environment at run time and never stored or printed.
    """

    action: str
    path: str = ""
    role: str = ""
    name: str = ""
    text: str = ""
    label: str = ""
    value: str = ""
    screenshot: str = ""

    def secrets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_SECRET_REF.findall(self.value)))

    def target(self) -> str:
        if self.role:
            return f'{self.role} "{self.name}"' if self.name else self.role
        if self.label:
            return f'label "{self.label}"'
        return f'text "{self.text}"'

    def describe(self) -> str:
        """Human-readable step; a secret reference is shown as written, never resolved."""

        if self.action in {"goto", "expect_url"}:
            return f"{'goto' if self.action == 'goto' else 'expect URL'} {self.path}"
        if self.action == "expect_text":
            return f'expect text "{self.text}"'
        if self.action == "screenshot":
            return f"screenshot {self.screenshot}"
        if self.action == "fill":
            shown = self.value if self.secrets() else f'"{self.value}"'
            return f"fill {self.target()} with {shown}"
        if self.action == "expect_visible":
            return f"expect visible {self.target()}"
        return f"click {self.target()}"

    def to_wire(self) -> dict[str, str]:
        return {f.name: value for f in fields(self) if (value := getattr(self, f.name))}


@dataclass(frozen=True)
class BrowserScenario:
    """One repository-configured browser regression scenario."""

    id: str
    title: str
    route: str
    paths: tuple[str, ...] = ()
    always: bool = False
    required: bool = False
    viewports: tuple[str, ...] = ()
    visual_threshold: float | None = None
    allow_console_errors: bool = False
    outcome: str = ""
    """Plain-language user outcome the steps prove, e.g. "a new user can sign up"."""
    steps: tuple[BrowserStep, ...] = ()
    """Saved steps ``lc review qa run`` replays; empty when another runner reports this scenario."""

    def secrets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for step in self.steps for name in step.secrets()))


@dataclass(frozen=True)
class BrowserPolicy:
    scenarios: tuple[BrowserScenario, ...] = ()
    trusted_origins: tuple[str, ...] = ()
    source: str = ""
    """Human-readable origin of the policy, e.g. ``base Snapshot .lemoncrow/review.yaml``."""
    error: str = ""
    """Visible degradation when the configuration could not be parsed."""

    def scenario(self, scenario_id: str) -> BrowserScenario | None:
        return next((item for item in self.scenarios if item.id == scenario_id), None)


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _normalize_route(value: str) -> str:
    route = value.strip()
    if not route.startswith("/") or route.startswith("//") or len(route) > 512:
        raise ValueError("browser route must be an application-relative path such as /checkout")
    if any(ord(char) < 33 or ord(char) == 127 for char in route) or "\\" in route:
        raise ValueError("browser route contains control characters, whitespace, or backslashes")
    return route


def _route_identity(route: str) -> str:
    path = route.split("?", 1)[0].split("#", 1)[0]
    return path.rstrip("/") or "/"


def viewport_key(value: object) -> str:
    """``1280x800`` for a viewport mapping/string, or ``""`` when unspecified."""

    if isinstance(value, str):
        text = value.strip().lower().replace("\u00d7", "x")
        if not text:
            return ""
        if not _VIEWPORT.fullmatch(text):
            raise ValueError("viewport must look like 1280x800")
        return text
    if isinstance(value, Mapping):
        if value.get("width") is None and value.get("height") is None:
            return ""
        try:
            width = int(value.get("width") or 0)
            height = int(value.get("height") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("viewport width/height must be integers") from exc
        if not (10 <= width <= 20000 and 10 <= height <= 20000):
            raise ValueError("viewport width/height are out of range")
        return f"{width}x{height}"
    if value is None:
        return ""
    raise ValueError("viewport must be a mapping or WIDTHxHEIGHT string")


def _threshold(value: object, *, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number between 0 and 1") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be a number between 0 and 1")
    return number


def _origin(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"trusted browser origin must be an http(s) origin: {value!r}")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"trusted browser origin must not contain a path, query, or credentials: {value!r}")
    return value.strip().rstrip("/").lower()


def _step_text(value: object, *, where: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} needs a non-empty {field}")
    if len(value) > _MAX_STEP_TEXT:
        raise ValueError(f"{where} {field} is longer than {_MAX_STEP_TEXT} characters")
    if "${secrets." in value:
        raise ValueError(f"{where}: ${{secrets.NAME}} is allowed only in a fill value")
    return value.strip()


def _parse_step(raw: object, *, where: str) -> BrowserStep:
    if not isinstance(raw, Mapping) or len(raw) != 1:
        raise ValueError(f"{where} must be a mapping with exactly one action, such as {{goto: /login}}")
    ((action, arg),) = raw.items()
    action = str(action)
    if action not in BROWSER_STEP_ACTIONS:
        raise ValueError(f"{where} has unknown action {action!r}; expected one of {', '.join(BROWSER_STEP_ACTIONS)}")
    if action in {"goto", "expect_url"}:
        return BrowserStep(action=action, path=_normalize_route(_step_text(arg, where=where, field="path")))
    if action == "expect_text":
        return BrowserStep(action=action, text=_step_text(arg, where=where, field="text"))
    if action == "screenshot":
        name = _step_text(arg, where=where, field="screenshot name")
        if not _SCREENSHOT_NAME.fullmatch(name):
            raise ValueError(f"{where} screenshot name must be letters, digits, . _ -")
        return BrowserStep(action=action, screenshot=name)
    # click / fill / expect_visible: a role (+ name), a visible text or a label.
    if isinstance(arg, str) and action != "fill":
        return BrowserStep(action=action, text=_step_text(arg, where=where, field="text"))
    if not isinstance(arg, Mapping):
        raise ValueError(f"{where} {action} needs {{role, name}}, {{text}} or {{label}}")
    allowed = {"role", "name", "text", "label"} | ({"value"} if action == "fill" else set())
    unknown = sorted(str(key) for key in arg if key not in allowed)
    if unknown:
        raise ValueError(f"{where} {action} has unknown field(s) {', '.join(unknown)}")
    targets = [key for key in ("role", "text", "label") if key in arg]
    if len(targets) != 1:
        raise ValueError(f"{where} {action} needs exactly one of role, text or label")
    role = _step_text(arg["role"], where=where, field="role") if "role" in arg else ""
    if role and not _ARIA_ROLE.fullmatch(role):
        raise ValueError(f"{where} role must be an ARIA role such as button, link, textbox or heading")
    if "name" in arg and not role:
        raise ValueError(f"{where} name is the accessible name of a role; use text or label otherwise")
    value = ""
    if action == "fill":
        raw_value = arg.get("value")
        if not isinstance(raw_value, str) or len(raw_value) > _MAX_STEP_TEXT:
            raise ValueError(f"{where} fill needs a string value (a literal or ${{secrets.NAME}})")
        if "${secrets." in _SECRET_REF.sub("", raw_value):
            raise ValueError(f"{where} fill value has a malformed secret reference; write ${{secrets.NAME}}")
        value = raw_value
    return BrowserStep(
        action=action,
        role=role,
        name=_step_text(arg["name"], where=where, field="name") if "name" in arg else "",
        text=_step_text(arg["text"], where=where, field="text") if "text" in arg else "",
        label=_step_text(arg["label"], where=where, field="label") if "label" in arg else "",
        value=value,
    )


def _parse_steps(raw: object, *, scenario_id: str) -> tuple[BrowserStep, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > _MAX_STEPS:
        raise ValueError(f"browser scenario {scenario_id} steps must be a list of at most {_MAX_STEPS} steps")
    steps = tuple(
        _parse_step(item, where=f"browser scenario {scenario_id} step {index + 1}") for index, item in enumerate(raw)
    )
    if steps and steps[0].action != "goto":
        raise ValueError(f"browser scenario {scenario_id} steps must start with goto")
    return steps


def parse_browser_policy(text: str, *, source: str) -> BrowserPolicy:
    """Parse the ``browser_checks`` section of ``.lemoncrow/review.yaml``.

    Accepts either a list of scenarios or a mapping with ``scenarios`` and
    optional ``trusted_origins``. Invalid configuration is reported as a
    visible policy error rather than silently dropping required checks.
    """

    try:
        decoded = yaml.safe_load(text) if text.strip() else None
    except yaml.YAMLError as exc:
        return BrowserPolicy(source=source, error=f"invalid review configuration: {exc}")
    if decoded is None:
        return BrowserPolicy(source=source)
    if not isinstance(decoded, Mapping):
        return BrowserPolicy(source=source, error="review configuration must be a mapping")
    section = decoded.get("browser_checks")
    if section is None:
        return BrowserPolicy(source=source)
    raw_scenarios: object
    raw_origins: object = ()
    if isinstance(section, Mapping):
        raw_scenarios = section.get("scenarios") or []
        raw_origins = section.get("trusted_origins") or ()
    else:
        raw_scenarios = section
    if not isinstance(raw_scenarios, list):
        return BrowserPolicy(source=source, error="browser_checks scenarios must be a list")
    try:
        origins = tuple(dict.fromkeys(_origin(item) for item in _strings(raw_origins)))
        scenarios: list[BrowserScenario] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_scenarios):
            if not isinstance(raw, Mapping):
                raise ValueError(f"browser scenario #{index + 1} must be a mapping")
            scenario_id = str(raw.get("id") or "").strip()
            if not _SCENARIO_ID.fullmatch(scenario_id):
                raise ValueError(f"browser scenario #{index + 1} needs an id of letters, digits, . _ : -")
            if scenario_id in seen:
                raise ValueError(f"duplicate browser scenario id {scenario_id!r}")
            seen.add(scenario_id)
            route = _normalize_route(str(raw.get("route") or ""))
            viewports = tuple(viewport_key(item) for item in _strings(raw.get("viewports")))
            scenarios.append(
                BrowserScenario(
                    id=scenario_id,
                    title=str(raw.get("title") or scenario_id).strip()[:120] or scenario_id,
                    route=route,
                    paths=_strings(raw.get("paths")),
                    always=raw.get("always") is True,
                    required=raw.get("required") is True,
                    viewports=tuple(item for item in viewports if item),
                    visual_threshold=_threshold(
                        raw.get("visual_threshold"), field=f"browser scenario {scenario_id} visual_threshold"
                    ),
                    allow_console_errors=raw.get("allow_console_errors") is True,
                    outcome=str(raw.get("outcome") or "").strip()[:500],
                    steps=_parse_steps(raw.get("steps"), scenario_id=scenario_id),
                )
            )
    except ValueError as exc:
        return BrowserPolicy(source=source, error=str(exc))
    return BrowserPolicy(scenarios=tuple(scenarios), trusted_origins=origins, source=source)


def scenario_applicability(
    scenario: BrowserScenario,
    changed_paths: Sequence[str],
    web_routes: Sequence[str] | None,
) -> tuple[str, str]:
    """Return ``(applies|not_applicable|unknown, factual reason)``.

    Only configured, factual signals are used: changed-path globs, a Web route
    the Change exposes, or an explicit ``always``. No heuristic guess about
    "UI-looking" code can make a scenario applicable.
    """

    if scenario.always:
        return "applies", "configured to run for every Change"
    for pattern in scenario.paths:
        for path in changed_paths:
            if fnmatch.fnmatchcase(path, pattern):
                return "applies", f"changed path {path} matches {pattern}"
    if web_routes is not None:
        wanted = _route_identity(scenario.route)
        if any(_route_identity(route) == wanted for route in web_routes):
            return "applies", f"this Change exposes Web route {scenario.route}"
        return "not_applicable", ""
    if scenario.paths:
        return "not_applicable", ""
    return "unknown", "Web route discovery is unavailable, so route applicability could not be established"


# --------------------------------------------------------------------------- #
# target safety
# --------------------------------------------------------------------------- #


def _origin_trusted(parsed: Any, trusted_origins: Sequence[str]) -> bool:
    scheme = str(parsed.scheme).lower()
    host = str(parsed.hostname or "").lower()
    port = parsed.port
    for origin in trusted_origins:
        trusted = urlparse(origin)
        if trusted.scheme != scheme:
            continue
        trusted_host = str(trusted.hostname or "").lower()
        trusted_port = trusted.port
        if trusted_port != port:
            continue
        if trusted_host.startswith("*."):
            suffix = trusted_host[1:]
            if host.endswith(suffix) and host != suffix.lstrip("."):
                return True
        elif host == trusted_host:
            return True
    return False


def validate_browser_target(target_url: str, trusted_origins: Sequence[str]) -> str:
    """Accept only loopback previews or repository-trusted preview origins.

    Review never navigates to this URL; it records what the reporter tested.
    Refusing arbitrary targets keeps Review from presenting (or later being
    asked to open) internal or third-party URLs as Snapshot verification.
    """

    value = target_url.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("browser target_url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("browser target_url must not contain credentials")
    host = str(parsed.hostname).lower()
    if host in _LOOPBACK_HOSTS:
        return value
    try:
        if ipaddress.ip_address(host).is_loopback:
            return value
    except ValueError:
        pass
    if _origin_trusted(parsed, trusted_origins):
        return value
    raise ValueError(
        "untrusted browser target: only loopback previews or origins listed in "
        "browser_checks.trusted_origins of the base review configuration are accepted"
    )


# --------------------------------------------------------------------------- #
# normalization + verdict
# --------------------------------------------------------------------------- #


def _text(value: object, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


#: Runner statuses (case- and underscore-insensitive) that mean the scenario
#: stopped before it finished, as Playwright's ``timedOut``/``interrupted``.
_INCOMPLETE_RUNNER_STATUSES = frozenset({"timedout", "interrupted"})


def _status(value: object) -> str:
    text = str(value or "").strip().upper()
    aliases = {"PASSED": "PASS", "OK": "PASS", "FAILED": "FAIL", "ERROR": "FAIL", "SKIPPED": "NOT_RUN"}
    text = aliases.get(text, text)
    return text if text in _STATUSES else "UNKNOWN"


def _rows(value: object, limit: int, field: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return [item for item in value[:limit] if isinstance(item, Mapping)]


def _playwright_assertions(raw: Mapping[str, Any], title: str) -> tuple[list[dict[str, Any]], bool, str]:
    """Map one Playwright JSON-reporter test result into deterministic checks."""

    status = str(raw.get("status") or "").strip()
    rows: list[dict[str, Any]] = []
    if status == "skipped":
        return rows, False, "NOT_RUN"
    completed = status not in {"timedOut", "interrupted"}
    rows.append(
        {
            "name": f"Playwright test {title}".strip(),
            "status": "PASS" if status == "passed" else "FAIL",
            "expected": "passed",
            "actual": status or "unknown",
            "detail": "",
        }
    )
    for error in _rows(raw.get("errors"), 5, "playwright.errors"):
        rows.append(
            {
                "name": "Playwright error",
                "status": "FAIL",
                "expected": "",
                "actual": "",
                "detail": _text(error.get("message") or error.get("value"), 2000),
            }
        )
    return rows, completed, ""


def normalize_browser_run(
    body: Mapping[str, Any],
    *,
    policy: BrowserPolicy,
    snapshot: ReviewRevision,
    resolve_artifact: Callable[[str], ReviewEvidence | None],
) -> dict[str, Any]:
    """Validate one provider report and compute its inspectable verdict.

    Raises ``ValueError`` for malformed or unsafe reports.
    """

    scenario_id = _text(body.get("scenario"), 80)
    if not _SCENARIO_ID.fullmatch(scenario_id):
        raise ValueError("scenario must be an id of letters, digits, . _ : -")
    configured = policy.scenario(scenario_id)
    side = _text(body.get("side") or "new", 8)
    if side not in {"new", "old"}:
        raise ValueError("side must be new (the Snapshot under review) or old (its baseline)")
    route = _normalize_route(str(body.get("route") or (configured.route if configured else "")))
    if configured is not None and _route_identity(route) != _route_identity(configured.route):
        raise ValueError(f"scenario {scenario_id!r} is configured for route {configured.route}, not {route}")
    target_url = validate_browser_target(_text(body.get("target_url"), 2048), policy.trusted_origins)
    raw_viewport = body.get("viewport")
    key = viewport_key(raw_viewport)
    viewport: dict[str, Any] = {}
    if isinstance(raw_viewport, Mapping) and key:
        viewport = {
            "width": int(raw_viewport.get("width") or 0),
            "height": int(raw_viewport.get("height") or 0),
            "device": _text(raw_viewport.get("device"), 80),
            "device_scale_factor": _threshold_free_number(raw_viewport.get("device_scale_factor")),
        }
    elif key:
        width, height = key.split("x", 1)
        viewport = {"width": int(width), "height": int(height), "device": "", "device_scale_factor": None}
    raw_environment = body.get("environment")
    environment = (
        {_text(name, 40): _text(value, 200) for name, value in list(raw_environment.items())[:12]}
        if isinstance(raw_environment, Mapping)
        else {}
    )
    title = _text(body.get("title"), 120) or (configured.title if configured else scenario_id)

    reported = _status(body.get("status")) if _text(body.get("status"), 40) else ""
    completed = body.get("completed") is not False
    if _text(body.get("status"), 40).replace("_", "").lower() in _INCOMPLETE_RUNNER_STATUSES:
        # The runner's own word that it stopped early outranks a defaulted
        # ``completed``; the nested Playwright result is read the same way.
        completed = False
    assertions: list[dict[str, Any]] = []
    playwright = body.get("playwright")
    if isinstance(playwright, Mapping):
        rows, pw_completed, pw_status = _playwright_assertions(playwright, _text(playwright.get("title"), 200))
        assertions.extend(rows)
        completed = completed and pw_completed
        if pw_status:
            reported = pw_status
    for raw in _rows(body.get("assertions"), _MAX_ASSERTIONS, "assertions"):
        assertions.append(
            {
                "name": _text(raw.get("name"), 200) or "assertion",
                "status": _status(raw.get("status")),
                "expected": _text(raw.get("expected"), 1000),
                "actual": _text(raw.get("actual"), 1000),
                "detail": _text(raw.get("detail"), 2000),
            }
        )
    assertions = assertions[:_MAX_ASSERTIONS]

    navigation: dict[str, Any] = {}
    raw_navigation = body.get("navigation")
    if isinstance(raw_navigation, Mapping):
        nav_status = raw_navigation.get("status")
        navigation = {
            "status": (
                int(nav_status) if isinstance(nav_status, (int, float)) and not isinstance(nav_status, bool) else None
            ),
            "ok": raw_navigation.get("ok") if isinstance(raw_navigation.get("ok"), bool) else None,
            "final_url": _text(raw_navigation.get("final_url"), 2048),
        }
    console_reported = body.get("console") is not None
    console = [
        {
            "level": _text(item.get("level") or item.get("type") or "log", 20).lower(),
            "text": _text(item.get("text") or item.get("message"), 2000),
            "location": _text(item.get("location"), 500),
        }
        for item in _rows(body.get("console"), _MAX_CONSOLE, "console")
    ]
    runtime_reported = body.get("runtime_errors") is not None
    runtime_errors = [
        {"message": _text(item.get("message"), 2000), "stack": _text(item.get("stack"), 4000)}
        for item in _rows(body.get("runtime_errors"), _MAX_RUNTIME_ERRORS, "runtime_errors")
    ]
    raw_visual = body.get("visual")
    diff_ratio = None
    if isinstance(raw_visual, Mapping):
        diff_ratio = _threshold(raw_visual.get("diff_ratio"), field="visual.diff_ratio")
    configured_threshold = configured.visual_threshold if configured is not None else None
    observations = [_text(item, 1000) for item in _strings(body.get("observations"))[:_MAX_OBSERVATIONS]]

    artifacts: dict[str, dict[str, Any]] = {}
    raw_artifacts = body.get("artifacts")
    if raw_artifacts is not None and not isinstance(raw_artifacts, Mapping):
        raise ValueError("artifacts must map screenshot/trace/video/diff to evidence ids")
    for role, raw_id in dict(raw_artifacts or {}).items():
        if role not in _ARTIFACT_ROLES:
            raise ValueError(f"unknown browser artifact role {role!r}; expected screenshot, trace, video, or diff")
        evidence_id = _text(raw_id, 120)
        artifact = resolve_artifact(evidence_id) if evidence_id else None
        if artifact is None:
            raise ValueError(f"browser {role} artifact {evidence_id!r} was not uploaded to this Review")
        if artifact.review_id != snapshot.review_id or artifact.revision_id != snapshot.id:
            raise ValueError(f"browser {role} artifact must be uploaded against the same exact Snapshot")
        if artifact.kind not in _ARTIFACT_ROLES[role] or not artifact.content_hash:
            raise ValueError(f"browser {role} artifact has kind {artifact.kind!r}")
        artifacts[role] = {
            "evidence_id": artifact.id,
            "digest": artifact.content_hash,
            "mime_type": artifact.mime_type,
            "bytes": artifact.bytes,
            "observed_at": artifact.created_at,
        }

    status, reasons, deterministic = browser_verdict(
        reported=reported,
        completed=completed,
        not_run_reason=_text(body.get("not_run_reason"), 500),
        assertions=assertions,
        navigation=navigation,
        console=console if console_reported else None,
        runtime_errors=runtime_errors if runtime_reported else None,
        diff_ratio=diff_ratio,
        threshold=configured_threshold,
        allow_console_errors=bool(configured and configured.allow_console_errors),
    )
    return {
        "v": 1,
        "scenario": scenario_id,
        "configured": configured is not None,
        "title": title,
        "name": browser_check_name(title, key),
        "route": route,
        "target_url": target_url,
        "side": side,
        "snapshot_id": snapshot.id,
        "tree_fingerprint": snapshot.tree_fingerprint,
        "viewport_key": key,
        "viewport": viewport,
        "environment": environment,
        "provider": _text(body.get("provider"), 40) or "unspecified",
        "run_ref": _text(body.get("run_ref") or body.get("source_ref"), 500),
        "started_at": _text(body.get("started_at"), 64),
        "finished_at": _text(body.get("finished_at"), 64),
        "completed": completed,
        "navigation": navigation,
        "assertions": assertions,
        "console": console,
        "runtime_errors": runtime_errors,
        "visual": {
            "diff_ratio": diff_ratio,
            "threshold": configured_threshold,
            "threshold_source": "configuration" if configured_threshold is not None else "",
        },
        "observations": observations,
        "artifacts": artifacts,
        "reported_status": reported,
        "status": status,
        "status_reasons": reasons,
        "deterministic_checks": deterministic,
    }


def _threshold_free_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 < number <= 16 else None


def browser_verdict(
    *,
    reported: str,
    completed: bool,
    not_run_reason: str,
    assertions: Sequence[Mapping[str, Any]],
    navigation: Mapping[str, Any],
    console: Sequence[Mapping[str, Any]] | None,
    runtime_errors: Sequence[Mapping[str, Any]] | None,
    diff_ratio: float | None,
    threshold: float | None,
    allow_console_errors: bool,
) -> tuple[str, list[str], int]:
    """Deterministic verdict: FAIL wins, PASS only from inspectable checks."""

    if reported == "NOT_RUN":
        # A runner that says it did not run did not run, whatever else the
        # report defaults; nothing it attached can stand in for the run.
        return "NOT_RUN", [not_run_reason or "the browser runner reported that this scenario did not run"], 0
    failures: list[str] = []
    unresolved: list[str] = []
    deterministic = 0
    if not completed:
        failures.append("the scenario did not complete")
    nav_status = navigation.get("status")
    nav_ok = navigation.get("ok")
    if nav_status is not None or nav_ok is not None:
        deterministic += 1
        if (isinstance(nav_status, int) and nav_status >= 400) or nav_ok is False:
            failures.append(f"navigation failed (HTTP {nav_status if nav_status is not None else 'error'})")
    failed_assertions = [row for row in assertions if row.get("status") == "FAIL"]
    unknown_assertions = [row for row in assertions if row.get("status") in {"UNKNOWN", "NOT_RUN"}]
    deterministic += len(assertions)
    if failed_assertions:
        failures.append(f"{len(failed_assertions)} assertion(s) failed")
    if unknown_assertions:
        unresolved.append(f"{len(unknown_assertions)} assertion(s) have no PASS/FAIL result")
    if console is not None:
        deterministic += 1
        errors = [row for row in console if row.get("level") == "error"]
        if errors and not allow_console_errors:
            failures.append(f"{len(errors)} console error(s)")
    if runtime_errors is not None:
        deterministic += 1
        if runtime_errors and not allow_console_errors:
            failures.append(f"{len(runtime_errors)} uncaught runtime error(s)")
    if threshold is not None and diff_ratio is not None:
        deterministic += 1
        if diff_ratio > threshold:
            failures.append(f"visual difference {diff_ratio:.4f} exceeds the configured threshold {threshold:.4f}")
    if reported == "FAIL" and not failures:
        failures.append("the browser runner reported FAIL")
    if reported == "UNKNOWN":
        # An unrecognized runner status (cancelled, flaky, ...) is not a
        # passing run, however clean the attached checks look.
        unresolved.append("the browser runner reported a status that is not PASS, FAIL or NOT_RUN")
    if failures:
        return "FAIL", failures, deterministic
    if deterministic == 0:
        return (
            "UNKNOWN",
            ["no inspectable deterministic check was reported; observations alone are not verification"],
            0,
        )
    if unresolved:
        return "UNKNOWN", unresolved, deterministic
    return "PASS", [f"{deterministic} deterministic check(s) passed"], deterministic


def browser_check_name(title: str, key: str) -> str:
    return f"{title} · {key}" if key else title


def browser_source_ref(record: Mapping[str, Any]) -> str:
    return ":".join(
        (
            BROWSER_SOURCE_REF_PREFIX.rstrip(":"),
            str(record.get("scenario") or ""),
            str(record.get("viewport_key") or "default"),
            str(record.get("side") or "new"),
        )
    )


def browser_summary(record: Mapping[str, Any]) -> str:
    """Compact human-readable detail stored beside the structured record."""

    parts = [str(record.get("route") or "")]
    assertions = list(record.get("assertions") or ())
    if assertions:
        passed = sum(1 for row in assertions if row.get("status") == "PASS")
        parts.append(f"{passed}/{len(assertions)} assertions passed")
    console = [row for row in record.get("console") or () if row.get("level") == "error"]
    if console:
        parts.append(f"{len(console)} console error(s)")
    reasons = [str(item) for item in record.get("status_reasons") or ()]
    if record.get("status") != "PASS" and reasons:
        parts.append(reasons[0])
    environment = record.get("environment") or {}
    browser = str(environment.get("browser") or "") if isinstance(environment, Mapping) else ""
    if browser or record.get("viewport_key"):
        parts.append(" ".join(item for item in (browser, str(record.get("viewport_key") or "")) if item))
    return " · ".join(item for item in parts if item)[:1200]


def is_browser_evidence(item: ReviewEvidence) -> bool:
    return (
        item.kind == "verification"
        and item.source_ref.startswith(BROWSER_SOURCE_REF_PREFIX)
        and item.mime_type == BROWSER_CHECK_MEDIA_TYPE
    )


#: Lane of a run that reached Review untyped (the browser-checks endpoint): its
#: reporter is not an attested provider.
BROWSER_REPORTED_LANE = "reported"
_HUMAN_LANES = frozenset({"human_observation", "human_waiver"})
_ATTESTED_LANES = frozenset({"lemoncrow_verification", "provider_ci", "trusted_security_feed"})


def browser_attestation(item: ReviewEvidence) -> str:
    """The attestation lane of one recorded run; part of its row identity.

    A run reported through the browser-checks endpoint is untyped
    (``reported``) unless a human recorded it. A human-observed run is a
    different check from a reported run of the same scenario: it never
    supersedes one and never stands in for a required scenario.
    """

    if item.attestation_class:
        return item.attestation_class
    return "human_observation" if item.source == "human" else BROWSER_REPORTED_LANE


def browser_check_key(attestation: str, scenario_id: str, viewport: str) -> str:
    """Typed identity of one browser row: attestation lane, scenario id, viewport.

    The scenario id is the stable key repository policy defines. The display
    title is presentation only, so two scenarios sharing a title are two rows.
    """

    return "|".join(("browser", attestation, scenario_id, viewport))


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #


def _run_payload(
    item: ReviewEvidence,
    record: Mapping[str, Any],
    *,
    numbers: Mapping[str, int],
) -> dict[str, Any]:
    number = numbers.get(item.revision_id)
    artifacts = {
        role: {**dict(value), "snapshot_id": item.revision_id, "snapshot_number": number}
        for role, value in dict(record.get("artifacts") or {}).items()
        if isinstance(value, Mapping)
    }
    return {
        "evidence_id": item.id,
        "record_digest": item.content_hash,
        "snapshot_id": item.revision_id,
        "snapshot_number": number,
        "side": str(record.get("side") or "new"),
        "status": str(record.get("status") or "UNKNOWN"),
        "status_reasons": list(record.get("status_reasons") or ()),
        "observed_at": item.created_at,
        "source": item.source,
        "provider": str(record.get("provider") or ""),
        "run_ref": str(record.get("run_ref") or ""),
        "target_url": str(record.get("target_url") or ""),
        "viewport": dict(record.get("viewport") or {}),
        "environment": dict(record.get("environment") or {}),
        "completed": bool(record.get("completed")),
        "navigation": dict(record.get("navigation") or {}),
        "assertions": [dict(row) for row in record.get("assertions") or ()],
        "console": [dict(row) for row in record.get("console") or ()],
        "runtime_errors": [dict(row) for row in record.get("runtime_errors") or ()],
        "visual": dict(record.get("visual") or {}),
        "observations": [str(row) for row in record.get("observations") or ()],
        "artifacts": artifacts,
        "started_at": str(record.get("started_at") or ""),
        "finished_at": str(record.get("finished_at") or ""),
    }


def build_browser_projection(
    *,
    evidence: Sequence[ReviewEvidence],
    revision: ReviewRevision,
    revisions: Sequence[ReviewRevision],
    policy: BrowserPolicy | None,
    changed_paths: Sequence[str],
    web_routes: Sequence[str] | None,
    read_record: Callable[[ReviewEvidence], Mapping[str, Any] | None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Browser check rows for one Snapshot plus a compact summary.

    Only a ``new``-side result recorded against *this* Snapshot is current.
    ``old``-side runs are the baseline for the same Snapshot, and results from
    earlier Snapshots are shown stale; neither can bless the current source.
    """

    policy = policy or BrowserPolicy()
    display = {item.id: max(0, item.revision_number - 1) for item in revisions}
    order = {item.id: item.revision_number for item in revisions}
    current_order = revision.revision_number
    rows = sorted(
        (
            item
            for item in evidence
            if is_browser_evidence(item) and order.get(item.revision_id, current_order + 1) <= current_order
        ),
        key=lambda item: (order.get(item.revision_id, 0), item.created_at, item.id),
        reverse=True,
    )
    # Identity is (scenario id, viewport, attestation lane), never the title.
    Key = tuple[str, str, str]
    current: dict[Key, tuple[ReviewEvidence, Mapping[str, Any]]] = {}
    base: dict[Key, tuple[ReviewEvidence, Mapping[str, Any]]] = {}
    prior: dict[Key, tuple[ReviewEvidence, Mapping[str, Any]]] = {}
    for item in rows:
        record = read_record(item)
        if not isinstance(record, Mapping):
            continue
        key = (
            str(record.get("scenario") or ""),
            str(record.get("viewport_key") or ""),
            browser_attestation(item),
        )
        side = str(record.get("side") or "new")
        if item.revision_id == revision.id:
            bucket = current if side == "new" else base
        elif side == "new":
            bucket = prior
        else:
            continue
        bucket.setdefault(key, (item, record))
    # Only a non-human run can satisfy a configured scenario's slot; a human
    # observation supports review beside it (verification_gate's rule).
    covered = {(scenario_id, vp) for scenario_id, vp, lane in current if lane not in _HUMAN_LANES}

    applicability: dict[str, tuple[str, str]] = {
        scenario.id: scenario_applicability(scenario, changed_paths, web_routes) for scenario in policy.scenarios
    }

    def required(scenario_id: str) -> bool:
        scenario = policy.scenario(scenario_id)
        return bool(scenario and scenario.required and applicability.get(scenario_id, ("",))[0] != "not_applicable")

    def check_row(
        *,
        name: str,
        status: str,
        state: str,
        scenario_id: str,
        viewport: str,
        attestation: str,
        route: str,
        detail: str,
        run: Mapping[str, Any] | None,
        baseline: Mapping[str, Any] | None,
        item: ReviewEvidence | None,
        reason: str,
    ) -> dict[str, Any]:
        snapshot_id = item.revision_id if item is not None else revision.id
        human = attestation in _HUMAN_LANES
        return {
            "name": name,
            "kind": "browser",
            "status": status,
            "state": state,
            "snapshot_id": snapshot_id,
            "snapshot_number": display.get(snapshot_id),
            "source": item.source if item is not None else "policy",
            "source_ref": item.source_ref if item is not None else f"browser:{scenario_id}",
            "lane": "browser",
            # Typed identity; a policy placeholder holds the reported slot it owes.
            "check_key": browser_check_key(attestation, scenario_id, viewport),
            "stable_key": scenario_id,
            "attestation_class": "" if attestation == BROWSER_REPORTED_LANE else attestation,
            "authority": (
                ""
                if item is None
                else "human" if human else "attested" if attestation in _ATTESTED_LANES else "reported"
            ),
            "path": "",
            "detail": detail,
            "observed_at": item.created_at if item is not None else "",
            "artifact": (
                {
                    "evidence_id": item.id,
                    "kind": item.kind,
                    "digest": item.content_hash,
                    "mime_type": item.mime_type,
                    "url": "",
                }
                if item is not None
                else None
            ),
            "required": required(scenario_id) if state == "current" and not human else False,
            "browser": {
                "scenario": scenario_id,
                "route": route,
                "configured": policy.scenario(scenario_id) is not None,
                "applicability": reason,
                "run": dict(run) if run is not None else None,
                "baseline": dict(baseline) if baseline is not None else None,
            },
        }

    checks: list[dict[str, Any]] = []
    for key, (item, record) in current.items():
        scenario_id, viewport, attestation = key
        run = _run_payload(item, record, numbers=display)
        baseline_row: dict[str, Any] | None = None
        if key in base:
            base_item, base_record = base[key]
            baseline_row = {**_run_payload(base_item, base_record, numbers=display), "relation": "base"}
        elif key in prior:
            prior_item, prior_record = prior[key]
            baseline_row = {**_run_payload(prior_item, prior_record, numbers=display), "relation": "previous"}
        state_reason = applicability.get(scenario_id, ("", ""))[1]
        checks.append(
            check_row(
                name=str(record.get("name") or item.title),
                status=str(item.verification_status or record.get("status") or "UNKNOWN"),
                state="current",
                scenario_id=scenario_id,
                viewport=viewport,
                attestation=attestation,
                route=str(record.get("route") or ""),
                detail=item.detail,
                run=run,
                baseline=baseline_row,
                item=item,
                reason=state_reason,
            )
        )

    covered_scenarios = {scenario_id for scenario_id, _ in covered}
    applicable_ids: list[str] = []
    for scenario in policy.scenarios:
        state, reason = applicability[scenario.id]
        if state == "not_applicable":
            continue
        applicable_ids.append(scenario.id)
        if scenario.id in covered_scenarios and not scenario.viewports:
            continue
        status = "NOT_RUN" if state == "applies" else "UNKNOWN"
        # A scenario pinned to viewports is owed at each one: a result at one
        # layout says nothing about another.
        for vp in scenario.viewports or ("",):
            if (scenario.id, vp) in covered:
                continue
            detail = (
                f"Configured browser scenario applies ({reason}) but no "
                f"{f'{vp} ' if vp else ''}result was recorded for this Snapshot."
                if state == "applies"
                else f"Configured browser scenario applicability is unknown: {reason}."
            )
            checks.append(
                check_row(
                    name=browser_check_name(scenario.title, vp),
                    status=status,
                    state="current",
                    scenario_id=scenario.id,
                    viewport=vp,
                    attestation=BROWSER_REPORTED_LANE,
                    route=scenario.route,
                    detail=detail,
                    run=None,
                    baseline=None,
                    item=None,
                    reason=reason,
                )
            )

    for key, (item, record) in prior.items():
        if key in current:
            continue
        checks.append(
            check_row(
                name=str(record.get("name") or item.title),
                status=str(item.verification_status or record.get("status") or "UNKNOWN"),
                state="stale",
                scenario_id=key[0],
                viewport=key[1],
                attestation=key[2],
                route=str(record.get("route") or ""),
                detail=item.detail,
                run=_run_payload(item, record, numbers=display),
                baseline=None,
                item=item,
                reason="",
            )
        )

    exposed_routes = list(dict.fromkeys(web_routes or ()))
    if exposed_routes and not applicable_ids and not current:
        shown = ", ".join(exposed_routes[:3]) + ("…" if len(exposed_routes) > 3 else "")
        checks.append(
            {
                **check_row(
                    name="Browser regression",
                    status="NOT_RUN",
                    state="current",
                    scenario_id="",
                    viewport="",
                    attestation=BROWSER_REPORTED_LANE,
                    route=exposed_routes[0],
                    detail=(
                        f"This Change exposes Web route(s) {shown}, but no browser regression scenario "
                        "is configured or reported for this Snapshot. Nothing was verified in a browser."
                    ),
                    run=None,
                    baseline=None,
                    item=None,
                    reason="Web review surface",
                ),
                "required": False,
            }
        )
    current_rows = [row for row in checks if row["state"] == "current"]
    summary = {
        "configured": len(policy.scenarios),
        "applicable": len(applicable_ids),
        "required": sum(1 for row in current_rows if row["required"]),
        "pass": sum(1 for row in current_rows if row["status"] == "PASS"),
        "fail": sum(1 for row in current_rows if row["status"] == "FAIL"),
        "not_run": sum(1 for row in current_rows if row["status"] == "NOT_RUN"),
        "unknown": sum(1 for row in current_rows if row["status"] == "UNKNOWN"),
        "stale": sum(1 for row in checks if row["state"] == "stale"),
        "policy_source": policy.source,
        "policy_error": policy.error,
        "web_routes": exposed_routes[:12] if web_routes is not None else None,
    }
    return checks, summary


__all__ = [
    "BROWSER_CHECK_MEDIA_TYPE",
    "BROWSER_SOURCE_REF_PREFIX",
    "BROWSER_STEP_ACTIONS",
    "BrowserPolicy",
    "BrowserScenario",
    "BrowserStep",
    "browser_check_name",
    "browser_source_ref",
    "browser_summary",
    "browser_verdict",
    "build_browser_projection",
    "is_browser_evidence",
    "normalize_browser_run",
    "parse_browser_policy",
    "scenario_applicability",
    "validate_browser_target",
    "viewport_key",
]
