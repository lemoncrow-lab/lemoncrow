"""The CI environment of a headless pull request review: whether to run, and on which model.

Standard library only, and importable without LemonCrow: ``action.yml`` runs this
file as a script *before* it installs anything, so a skipped run (a fork, a draft,
no key) costs seconds instead of a dependency install. The review command imports
the same functions, so the Action and the CLI cannot disagree.

Gating, in the order it is decided:

* ``pull_request_target`` is refused. That event runs with secrets and a write
  token on behalf of code from the pull request, which is the one place a
  reviewer that reads untrusted code must not run.
* A ``pull_request`` from a fork is skipped (exit 0, a notice): GitHub gives it no
  secrets and a read-only token, so there is no model key to call with and no
  permission to comment with.
* Drafts are skipped unless ``review_drafts``; a push to an open pull request is
  skipped when ``on_push`` is off.
* With no model credential the run stops. Dependabot runs also have no secrets, so
  they skip quietly; anything else is a misconfiguration and fails loudly.

A skip that exits 0 also posts a ``success`` commit status, ``skipped: <reason>``, on the pull request's head
when the token can write statuses, so a ``LemonCrow Review`` required check does not wait on "Expected" forever.
A fork's ``pull_request`` token is read-only, so none is posted for it (:func:`post_skip_status` says so);
a required check on a repository that accepts fork pull requests needs another path (the setup guide's
"Required checks and skipped runs").
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass

BACKEND_ENV = "LEMONCROW_LLM_BACKEND"
MODEL_ENV = "LEMONCROW_REVIEW_MODEL"
PROVIDER_ENV = "LEMONCROW_REVIEW_PROVIDER"
#: Used only for an Anthropic key with no model chosen; a provisional default until
#: review quality and cost per catch are measured. The OpenAI default stays the
#: driver's own (``gpt-4o-mini``).
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
PROVIDERS = ("auto", "anthropic", "openai", "litellm")
#: Backends a user may already have chosen with ``LEMONCROW_LLM_BACKEND``.
_KNOWN_BACKENDS = frozenset({"openai", "openai_compatible", "litellm", "ollama", "claude", "codex", "opencode"})
#: The model variable each API backend reads once the backend is chosen.
_MODEL_VARIABLE = {
    "litellm": "LEMONCROW_LITELLM_MODEL",
    "openai": "LEMONCROW_OPENAI_MODEL",
    "ollama": "LEMONCROW_OLLAMA_MODEL",
}


@dataclass(frozen=True)
class ModelRoute:
    """The model route the environment selects. ``model`` is empty when the driver's own default applies."""

    provider: str
    backend: str
    model: str
    credential: str


def _present(env: Mapping[str, str], name: str) -> bool:
    return bool(str(env.get(name) or "").strip())


def _openai_credential(env: Mapping[str, str]) -> str:
    for name in ("LEMONCROW_OPENAI_API_KEY", "OPENAI_API_KEY", "LEMONCROW_OPENAI_BASE_URL"):
        if _present(env, name):
            return name
    return ""


def resolve_model_route(env: Mapping[str, str], provider: str = "auto") -> ModelRoute | None:
    """The route the environment selects, or ``None`` when it holds no credential for one.

    An explicit ``LEMONCROW_LLM_BACKEND`` is the user's own choice and is never
    replaced. Otherwise ``ANTHROPIC_API_KEY`` selects LiteLLM on an ``anthropic/``
    model and ``OPENAI_API_KEY`` selects the OpenAI route; with both set, Anthropic
    wins unless ``provider`` names the other. ``LEMONCROW_REVIEW_MODEL`` overrides
    the model on either.
    """

    wanted = (provider or "auto").strip().lower()
    review_model = str(env.get(MODEL_ENV) or "").strip()
    backend = str(env.get(BACKEND_ENV) or "").strip().lower()
    if backend in _KNOWN_BACKENDS and wanted in {"auto", ""}:
        canonical = "openai" if backend == "openai_compatible" else backend
        return ModelRoute(canonical, canonical, review_model, BACKEND_ENV)
    if wanted in {"auto", "anthropic"} and _present(env, "ANTHROPIC_API_KEY"):
        model = review_model or DEFAULT_ANTHROPIC_MODEL
        return ModelRoute("anthropic", "litellm", model if "/" in model else f"anthropic/{model}", "ANTHROPIC_API_KEY")
    if wanted in {"auto", "openai"}:
        credential = _openai_credential(env)
        if credential:
            return ModelRoute("openai", "openai", review_model, credential)
    if wanted == "litellm":
        # LiteLLM reads many providers' own variables (AWS, Azure, Vertex); only the user knows which.
        return ModelRoute("litellm", "litellm", review_model, BACKEND_ENV)
    return None


def apply_model_route(route: ModelRoute, env: MutableMapping[str, str]) -> None:
    """Point the review drivers at *route*. A backend or driver model the user already set is kept."""

    env.setdefault(BACKEND_ENV, route.backend)
    if route.backend == "litellm":
        # LiteLLM waits 6000 s for a response unless told otherwise; the review gives each call 120 s everywhere
        # else, and a hung connection must not hold a CI job for hours.
        env.setdefault("REQUEST_TIMEOUT", "120")
    variable = _MODEL_VARIABLE.get(route.backend)
    if variable is None or not route.model:
        return
    if _present(env, MODEL_ENV) or not _present(env, variable):
        env[variable] = route.model


@dataclass(frozen=True)
class Gate:
    """Whether a run goes ahead; when it does not, why and how loudly."""

    run: bool
    reason: str = ""
    level: str = "notice"
    exit_code: int = 0
    #: For a skip that exits 0: the short reason after ``skipped:`` in the commit status posted for it.
    #: Empty when the run stops for another reason (nothing to review, a refusal, a misconfiguration).
    skip: str = ""
    #: Whether GitHub gives this run a token that can write a commit status (a fork's is read-only).
    writable: bool = True


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def load_event(env: Mapping[str, str]) -> Mapping[str, object]:
    """The workflow event payload, ``{}`` when there is none or it cannot be read."""

    path = str(env.get("GITHUB_EVENT_PATH") or "").strip()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return _mapping(json.load(handle))
    except (OSError, ValueError):
        return {}


def event_pull_number(event: Mapping[str, object]) -> int:
    """The pull request number a ``pull_request`` event carries, ``0`` when none."""

    pull = _mapping(event.get("pull_request"))
    number = pull.get("number")
    return number if isinstance(number, int) and not isinstance(number, bool) else 0


def _is_fork(pull: Mapping[str, object]) -> bool:
    head_repo = _mapping(_mapping(pull.get("head")).get("repo"))
    base_repo = _mapping(_mapping(pull.get("base")).get("repo"))
    head_name = str(head_repo.get("full_name") or "").lower()
    base_name = str(base_repo.get("full_name") or "").lower()
    # A deleted fork has no head repository at all: still a fork.
    return not head_name or (bool(base_name) and head_name != base_name)


def decide(
    env: Mapping[str, str],
    *,
    review_drafts: bool = False,
    on_push: bool = True,
    provider: str = "auto",
    pr_number: int = 0,
    require_credentials: bool = True,
    event: Mapping[str, object] | None = None,
) -> Gate:
    """Whether this workflow run should review a pull request, from the CI environment alone."""

    name = str(env.get("GITHUB_EVENT_NAME") or "")
    if name == "pull_request_target":
        return Gate(
            False,
            "LemonCrow Review does not run on pull_request_target: that event runs with secrets and a write token "
            "on behalf of code from the pull request. Use the pull_request event.",
            "error",
            1,
        )
    payload = load_event(env) if event is None else event
    pull = _mapping(payload.get("pull_request"))
    if not (pr_number or event_pull_number(payload)):
        return Gate(False, "No pull request in this event; nothing to review.")
    forked = name == "pull_request" and bool(pull) and _is_fork(pull)
    if forked:
        return Gate(
            False,
            "Pull request from a fork: GitHub gives pull_request runs from forks no secrets and a read-only token, "
            "so there is no model key to call and no permission to comment. Skipping.",
            skip="pull request from a fork",
            writable=False,
        )
    if pull.get("draft") is True and not review_drafts:
        return Gate(
            False, "Draft pull request: skipped. Set review-drafts to review drafts.", skip="draft pull request"
        )
    if str(payload.get("action") or "") == "synchronize" and not on_push:
        return Gate(
            False,
            "Push to an open pull request: skipped because on-push is off.",
            skip="push to an open pull request, on-push is off",
        )
    if require_credentials and resolve_model_route(env, provider) is None:
        if str(env.get("GITHUB_ACTOR") or "") == "dependabot[bot]":
            return Gate(
                False,
                "Dependabot runs cannot read repository secrets, so there is no model key. Skipping. Adding the key "
                "under Dependabot secrets may not be enough: runs Dependabot triggers are understood to get a "
                "read-only GITHUB_TOKEN (not confirmed here; check GitHub's documentation for Dependabot and "
                "GitHub Actions), and a review that cannot comment or set a status has nowhere to report.",
                skip="Dependabot run without a model key",
            )
        return Gate(
            False,
            "No model credentials. Add ANTHROPIC_API_KEY or OPENAI_API_KEY as a repository secret and pass it to "
            "the step as an environment variable (or set LEMONCROW_LLM_BACKEND with its provider variables).",
            "error",
            1,
        )
    return Gate(True)


_LEVEL_COMMAND = {"notice": "notice", "warning": "warning", "error": "error"}
#: The LemonCrow extra (``pyproject.toml`` optional dependencies) each backend's client library comes from.
_EXTRA_FOR_BACKEND = {"litellm": "litellm", "openai": "cloud", "ollama": "smart"}


def _truthy(value: str, default: bool) -> bool:
    text = value.strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


STATUS_CONTEXT = "LemonCrow Review"
_STATUS_TIMEOUT = 20


def _send_status(url: str, body: bytes, headers: Mapping[str, str]) -> None:
    """POST *body* to *url*; raises ``OSError`` (``URLError`` is one) on any failure."""

    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=_STATUS_TIMEOUT):
        return


def post_skip_status(
    env: Mapping[str, str],
    gate: Gate,
    *,
    event: Mapping[str, object] | None = None,
    send: Callable[[str, bytes, Mapping[str, str]], None] = _send_status,
) -> str:
    """Post ``success`` / ``skipped: <reason>`` on the pull request's head; return ``""`` or why nothing was posted.

    Best effort and never raising: a skipped run must stay a skipped run. A fork's token is read-only, so none is
    attempted for it. The status goes on the pull request's head commit, which is what a required check reads, not
    on the merge commit the job checked out.
    """

    if not gate.skip:
        return ""
    if not gate.writable:
        return (
            f"No '{STATUS_CONTEXT}' status was posted: GitHub gives this run a read-only token. "
            "If you require that check, see 'Required checks and skipped runs' in the setup guide."
        )
    token = str(env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or "").strip()
    repository = str(env.get("GITHUB_REPOSITORY") or "").strip()
    payload = load_event(env) if event is None else event
    head = _mapping(_mapping(payload.get("pull_request")).get("head"))
    sha = str(head.get("sha") or "").strip()
    if not (token and repository and sha):
        return f"No '{STATUS_CONTEXT}' status was posted: the run has no token, repository or pull request head."
    api = str(env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    body: dict[str, str] = {
        "state": "success",
        "context": STATUS_CONTEXT,
        "description": f"skipped: {gate.skip}"[:140],
    }
    server, run = str(env.get("GITHUB_SERVER_URL") or "").rstrip("/"), str(env.get("GITHUB_RUN_ID") or "")
    if server and run:
        body["target_url"] = f"{server}/{repository}/actions/runs/{run}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lemoncrow-review-gate",
    }
    try:
        send(f"{api}/repos/{repository}/statuses/{sha}", json.dumps(body).encode("utf-8"), headers)
    except (OSError, ValueError) as exc:
        return (
            f"No '{STATUS_CONTEXT}' status was posted ({type(exc).__name__}): the token may lack statuses: write. "
            "A required check will wait for it."
        )
    return ""


def main(argv: list[str] | None = None) -> int:
    """Script entry for ``action.yml``: decide, print a workflow command, write ``run=`` and ``extra=`` to ``$GITHUB_OUTPUT``."""

    env = os.environ
    raw_number = (argv[0] if argv else str(env.get("LEMONCROW_REVIEW_PR") or "")).strip()
    gate = decide(
        env,
        review_drafts=_truthy(str(env.get("LEMONCROW_REVIEW_DRAFTS") or ""), False),
        on_push=_truthy(str(env.get("LEMONCROW_REVIEW_ON_PUSH") or ""), True),
        provider=str(env.get(PROVIDER_ENV) or "auto"),
        pr_number=int(raw_number) if raw_number.isdigit() else 0,
    )
    if gate.reason:
        print(f"::{_LEVEL_COMMAND.get(gate.level, 'notice')} title=LemonCrow Review::{gate.reason}")
    if not gate.run and gate.exit_code == 0:
        unposted = post_skip_status(env, gate)
        if unposted:
            print(f"::notice title=LemonCrow Review::{unposted}")
    output = str(env.get("GITHUB_OUTPUT") or "").strip()
    if output:
        route = resolve_model_route(env, str(env.get(PROVIDER_ENV) or "auto")) if gate.run else None
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"run={'true' if gate.run else 'false'}\n")
            handle.write(f"extra={_EXTRA_FOR_BACKEND.get(route.backend, '') if route is not None else ''}\n")
    return gate.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
