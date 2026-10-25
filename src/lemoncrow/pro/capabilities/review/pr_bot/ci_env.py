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
  they skip quietly; anything else is a misconfiguration and fails loudly: a red job, an ``::error`` that names the
  two ways to pass a key, and (on a pull request) an ``error`` ``LemonCrow Review`` status so a required check does
  not wait forever.

The model key reaches the Action as an input (``anthropic-api-key``, ``openai-api-key``), which ``action.yml`` hands
to the steps that need it as ``INPUT_ANTHROPIC_API_KEY`` / ``INPUT_OPENAI_API_KEY`` and to no other step; a key set
as ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` on the calling step keeps working. :func:`key_environment` merges the
two (the input wins), :func:`mask_commands` makes the runner mask every key value, and nothing here ever prints one.

Chat (G6) is a second mode of the same Action, picked from ``GITHUB_EVENT_NAME``: ``issue_comment`` and
``pull_request_review_comment`` runs are decided by :func:`decide_chat` instead, with its own rules: the comment must
address the bot, come from a person whose ``author_association`` is allowed, and (for a review comment, which GitHub
runs without secrets on a fork) not from a fork. A chat skip never posts a commit status, because a comment is not a
push and has no check to answer.

A skip that exits 0 also posts a ``success`` commit status, ``skipped: <reason>``, on the pull request's head
when the token can write statuses, so a ``LemonCrow Review`` required check does not wait on "Expected" forever.
A fork's ``pull_request`` token is read-only, so none is posted for it (:func:`post_skip_status` says so);
a required check on a repository that accepts fork pull requests needs another path (the setup guide's
"Required checks and skipped runs").
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass

BACKEND_ENV = "LEMONCROW_LLM_BACKEND"
MODEL_ENV = "LEMONCROW_REVIEW_MODEL"
PROVIDER_ENV = "LEMONCROW_REVIEW_PROVIDER"
#: Used only for an Anthropic key with no model chosen; a provisional default until
#: review quality and cost per catch are measured. The OpenAI default stays the
#: driver's own (``gpt-4o-mini``).
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
PROVIDERS = ("auto", "anthropic", "openai", "litellm")
#: The Action's key inputs, as ``action.yml`` passes them to a step, and the standard variable each one stands for.
KEY_INPUTS = {"INPUT_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY", "INPUT_OPENAI_API_KEY": "OPENAI_API_KEY"}
#: What a person does about a missing key; the one wording every "no key" message uses.
NO_KEY_FIX = (
    "Add your model key as a repository secret (Settings > Secrets and variables > Actions) and pass it to the "
    "Action: `with: {anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}}` (or `openai-api-key`). A key set as an "
    "environment variable on the step also works."
)
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


def key_environment(env: Mapping[str, str]) -> dict[str, str]:
    """*env* with each key input copied to the standard variable it stands for.

    The input wins over a variable of the same name: it is the explicit choice, made on the Action itself. An empty
    input changes nothing, so a key set as an environment variable on the calling step keeps working.
    """

    merged = dict(env)
    for source, target in KEY_INPUTS.items():
        if _present(env, source):
            merged[target] = str(env[source]).strip()
    return merged


_MIN_MASK = 6


def mask_commands(env: Mapping[str, str]) -> list[str]:
    """``::add-mask::`` lines for every key *env* holds, so the runner hides the value wherever it would appear.

    Each line of a multi-line value is masked on its own (a workflow command ends at the newline). GitHub already masks
    a ``secrets.*`` value; this covers a key that did not come from there, and costs nothing when it did.
    """

    names = (*KEY_INPUTS, *KEY_INPUTS.values(), "LEMONCROW_OPENAI_API_KEY", "LEMONCROW_ANTHROPIC_API_KEY")
    seen: dict[str, None] = {}
    for name in names:
        for line in str(env.get(name) or "").splitlines():
            # Real keys are long; masking a one-character placeholder would blank that character out of every log.
            if len(line.strip()) >= _MIN_MASK:
                seen[line.strip()] = None
    return [f"::add-mask::{value}" for value in seen]


def _anthropic_credential(env: Mapping[str, str]) -> str:
    """``ANTHROPIC_API_KEY``, else the key ``lc settings set llm.anthropic_api_key`` saved (``LEMONCROW_ANTHROPIC_API_KEY``)."""

    for name in ("ANTHROPIC_API_KEY", "LEMONCROW_ANTHROPIC_API_KEY"):
        if _present(env, name):
            return name
    return ""


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
    anthropic = _anthropic_credential(env)
    if wanted in {"auto", "anthropic"} and anthropic:
        model = review_model or DEFAULT_ANTHROPIC_MODEL
        return ModelRoute("anthropic", "litellm", model if "/" in model else f"anthropic/{model}", anthropic)
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
    if route.credential == "LEMONCROW_ANTHROPIC_API_KEY" and not _present(env, "ANTHROPIC_API_KEY"):
        saved = str(env.get(route.credential) or "").strip()
        if saved:
            env["ANTHROPIC_API_KEY"] = saved  # the SDK and the direct route read the vendor's own name
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
    #: For a failure that stops the run before any review: the description of the ``error`` ``LemonCrow Review`` status
    #: posted for it, so a required check shows why instead of waiting. Empty: no status.
    error_status: str = ""


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
            f"No model key. {NO_KEY_FIX} (Or set LEMONCROW_LLM_BACKEND with its provider variables.)",
            "error",
            1,
            error_status="No model key: pass anthropic-api-key or openai-api-key to the LemonCrow Action",
        )
    return Gate(True)


# --------------------------------------------------------------------------- #
# chat: @mentions in pull request threads
# --------------------------------------------------------------------------- #

CHAT_EVENTS = ("issue_comment", "pull_request_review_comment")
MENTIONS_ENV = "LEMONCROW_REVIEW_MENTIONS"
ASSOCIATIONS_ENV = "LEMONCROW_REVIEW_CHAT_ASSOCIATIONS"
#: The handle the bot answers to. It is plain text, not a GitHub account: the bot posts as ``github-actions[bot]``.
DEFAULT_MENTION = "@lemoncrow"
#: ``author_association`` values GitHub attaches to a comment, widest trust first.
KNOWN_ASSOCIATIONS = (
    "OWNER",
    "MEMBER",
    "COLLABORATOR",
    "CONTRIBUTOR",
    "FIRST_TIME_CONTRIBUTOR",
    "FIRST_TIMER",
    "MANNEQUIN",
    "NONE",
)
DEFAULT_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR")
_HANDLE = re.compile(r"@[A-Za-z0-9][A-Za-z0-9_-]{0,38}")
_FENCE = re.compile(r"(```|~~~).*?(?:\1|\Z)", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_QUOTE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
#: Text the bot writes into its own comments. A comment that carries one is the bot's, whoever the login says posted it.
_OWN_MARKER = "<!-- lemoncrow:"


def chat_mentions(env: Mapping[str, str]) -> tuple[str, ...]:
    """The handles the bot answers to: ``@lemoncrow`` plus the aliases in ``LEMONCROW_REVIEW_MENTIONS`` (comma separated).

    An alias is a word like ``@review-bot``; anything else in the list is ignored. The default stays, so a typo in
    an alias cannot turn chat off. An alias is for a repository where a real account already owns ``@lemoncrow``
    (GitHub would notify it) or where a team prefers a shorter word.
    """

    found = [DEFAULT_MENTION]
    for raw in re.split(r"[,;\n]+", str(env.get(MENTIONS_ENV) or "")):
        token = raw.strip()
        handle = token if token.startswith("@") else f"@{token}"
        if token and _HANDLE.fullmatch(handle) and handle.lower() not in found:
            found.append(handle.lower())
    return tuple(found)


def chat_associations(env: Mapping[str, str]) -> frozenset[str]:
    """The ``author_association`` values allowed to talk to the bot; the default when the variable is empty or unusable."""

    wanted = {
        token.upper()
        for token in re.split(r"[\s,]+", str(env.get(ASSOCIATIONS_ENV) or ""))
        if token.upper() in KNOWN_ASSOCIATIONS
    }
    return frozenset(wanted or DEFAULT_ASSOCIATIONS)


def find_mention(body: str, mentions: Sequence[str] = (DEFAULT_MENTION,)) -> str | None:
    """The text after the first ``@mention`` of the bot in *body*, or ``None`` when it is not addressed.

    Code (fenced or inline), quoted lines and HTML comments are ignored, so documenting the command or quoting the
    bot's own reply never triggers it. The handle must stand alone: ``@lemoncrow-bot`` and ``me@lemoncrow`` are not it.
    """

    text = _HTML_COMMENT.sub(" ", _QUOTE.sub(" ", _INLINE_CODE.sub(" ", _FENCE.sub(" ", body or ""))))
    pattern = re.compile(
        r"(?<![A-Za-z0-9_@/.\-])(?:" + "|".join(re.escape(handle) for handle in mentions) + r")(?![A-Za-z0-9_\-])",
        re.IGNORECASE,
    )
    found = pattern.search(text)
    if found is None:
        return None
    return text[found.end() :].strip()


@dataclass(frozen=True)
class ChatEvent:
    """A comment event that might address the bot, read from the workflow payload."""

    name: str
    action: str
    pull_number: int
    comment_id: int
    body: str
    author: str
    author_type: str
    association: str
    html_url: str = ""
    #: For a review comment: the comment it replies to (``0`` for the first comment of a thread), where it sits,
    #: the diff around it, and whether the pull request comes from a fork.
    reply_to: int = 0
    path: str = ""
    line: int = 0
    start_line: int = 0
    diff_hunk: str = ""
    fork: bool = False

    @property
    def in_thread(self) -> bool:
        return self.name == "pull_request_review_comment"

    @property
    def root_id(self) -> int:
        """The first comment of the review thread this comment belongs to; ``0`` for a conversation comment."""

        return (self.reply_to or self.comment_id) if self.in_thread else 0


def _positive(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def chat_event(name: str, payload: Mapping[str, object]) -> ChatEvent | None:
    """The comment a chat-capable event carries; ``None`` for any other event, or one that is not about a pull request."""

    if name not in CHAT_EVENTS:
        return None
    comment = _mapping(payload.get("comment"))
    user = _mapping(comment.get("user")) or _mapping(payload.get("sender"))
    if name == "issue_comment":
        issue = _mapping(payload.get("issue"))
        if not _mapping(issue.get("pull_request")):
            return None  # a comment on a plain issue
        number, fork = _positive(issue.get("number")), False
    else:
        pull = _mapping(payload.get("pull_request"))
        number, fork = _positive(pull.get("number")), bool(pull) and _is_fork(pull)
    comment_id = _positive(comment.get("id"))
    if not (number and comment_id):
        return None
    return ChatEvent(
        name=name,
        action=str(payload.get("action") or ""),
        pull_number=number,
        comment_id=comment_id,
        body=str(comment.get("body") or ""),
        author=str(user.get("login") or ""),
        author_type=str(user.get("type") or ""),
        association=str(comment.get("author_association") or "").upper(),
        html_url=str(comment.get("html_url") or ""),
        reply_to=_positive(comment.get("in_reply_to_id")),
        path=str(comment.get("path") or ""),
        line=_positive(comment.get("line")) or _positive(comment.get("original_line")),
        start_line=_positive(comment.get("start_line")) or _positive(comment.get("original_start_line")),
        diff_hunk=str(comment.get("diff_hunk") or ""),
        fork=fork,
    )


def is_bot(event: ChatEvent) -> bool:
    """Whether the comment is from a bot or from this bot: neither is ever answered, which is what stops a loop."""

    return (
        event.author_type.lower() == "bot"
        or event.author.lower().endswith("[bot]")
        or _OWN_MARKER in event.body
        or not event.author
    )


def decide_chat(
    env: Mapping[str, str],
    *,
    provider: str = "auto",
    event: Mapping[str, object] | None = None,
    require_credentials: bool = True,
) -> Gate:
    """Whether this comment event should be answered, from the CI environment and the payload alone.

    Most skips are silent (``reason`` empty): a comment that does not address the bot is not an error and not even
    news. A person who is not allowed to talk to the bot, and a fork, get a notice in the run log; nothing is ever
    posted for a skip, and no commit status is set (``skip`` stays empty).
    """

    name = str(env.get("GITHUB_EVENT_NAME") or "")
    payload = load_event(env) if event is None else event
    chat = chat_event(name, payload)
    if chat is None or chat.action != "created" or is_bot(chat):
        return Gate(False)
    if find_mention(chat.body, chat_mentions(env)) is None:
        return Gate(False)
    allowed = chat_associations(env)
    if chat.association not in allowed:
        who = chat.association.lower().replace("_", " ") or "unknown"
        return Gate(
            False,
            f"{chat.author} ({who}) is not allowed to talk to LemonCrow here; the allowed author associations are "
            f"{', '.join(sorted(allowed))}. Nothing was answered.",
        )
    if chat.fork:
        return Gate(
            False,
            "Review comment on a pull request from a fork: GitHub gives pull_request_review_comment runs from forks no "
            "secrets and a read-only token, so there is no model key to call and no permission to reply. Ask in the "
            "pull request's conversation instead (issue_comment runs with this repository's secrets), or have a "
            "maintainer push the branch here.",
        )
    if require_credentials and resolve_model_route(env, provider) is None:
        # A comment has no check to answer, so there is no status to set: the red job and the annotation say it.
        return Gate(
            False,
            f"No model key. {NO_KEY_FIX} (Or set LEMONCROW_LLM_BACKEND with its provider variables.)",
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


def post_status(
    env: Mapping[str, str],
    state: str,
    description: str,
    *,
    writable: bool = True,
    event: Mapping[str, object] | None = None,
    send: Callable[[str, bytes, Mapping[str, str]], None] = _send_status,
) -> str:
    """Post a ``LemonCrow Review`` status on the pull request's head; return ``""`` or why nothing was posted.

    Best effort and never raising: the run it speaks for has already been decided. A fork's token is read-only, so none
    is attempted for it (*writable* false). The status goes on the pull request's head commit, which is what a required
    check reads, not on the merge commit the job checked out. It links to this workflow run.
    """

    if not writable:
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
    body: dict[str, str] = {"state": state, "context": STATUS_CONTEXT, "description": description[:140]}
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


def _get_json(url: str, headers: Mapping[str, str]) -> object:
    """GET *url* as JSON; raises ``OSError`` (``URLError`` is one) or ``ValueError`` on any failure."""

    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(request, timeout=_STATUS_TIMEOUT) as response:
        return json.load(response)


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lemoncrow-review-gate",
    }


#: What a status left `pending` by a job that died says once the Action settles it.
UNFINISHED = "The review job failed or was cancelled before it finished. Re-run it."


def settle_pending_status(
    env: Mapping[str, str],
    *,
    event: Mapping[str, object] | None = None,
    fetch: Callable[[str, Mapping[str, str]], object] = _get_json,
    send: Callable[[str, bytes, Mapping[str, str]], None] = _send_status,
) -> str:
    """Turn a ``LemonCrow Review`` status still ``pending`` on this run's head into ``error``; return a note or ``""``.

    The review sets ``pending`` when it starts and replaces it when it ends. A job that is cancelled or times out never
    gets to, and a required check would wait on the pending status until the next push. ``action.yml`` runs this as its
    last step with ``if: always()``, so no workflow needs a cleanup step of its own. It touches this run's head commit
    only, only when its newest ``LemonCrow Review`` status is ``pending``, and never raises: a cleanup must not
    turn a finished job red. A comment event sets no status, so there is nothing to settle for it.
    """

    if str(env.get("GITHUB_EVENT_NAME") or "") in CHAT_EVENTS:
        return ""
    token = str(env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or "").strip()
    repository = str(env.get("GITHUB_REPOSITORY") or "").strip()
    payload = load_event(env) if event is None else event
    sha = str(_mapping(_mapping(payload.get("pull_request")).get("head")).get("sha") or "").strip()
    api = str(env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    headers = _api_headers(token)
    number = str(env.get("LEMONCROW_REVIEW_PR") or "").strip()
    try:
        if not sha and token and repository and number.isdigit():  # a workflow_dispatch run names its pull request
            pull = _mapping(fetch(f"{api}/repos/{repository}/pulls/{number}", headers))
            sha = str(_mapping(pull.get("head")).get("sha") or "").strip()
        if not (sha and token and repository):
            return ""
        rows = fetch(f"{api}/repos/{repository}/commits/{sha}/statuses?per_page=100", headers)
        ours = (
            [row for row in rows if isinstance(row, dict) and row.get("context") == STATUS_CONTEXT]
            if isinstance(rows, list)
            else []
        )
        if not ours or ours[0].get("state") != "pending":
            return ""
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404}:
            return ""  # the token cannot read statuses, so it cannot have left one pending; the run already said why
        return f"Could not check whether the '{STATUS_CONTEXT}' status was left pending (HTTP {exc.code})."
    except (OSError, ValueError) as exc:
        return f"Could not check whether the '{STATUS_CONTEXT}' status was left pending ({type(exc).__name__})."
    problem = post_status(env, "error", UNFINISHED, event={"pull_request": {"head": {"sha": sha}}}, send=send)
    return problem or f"The '{STATUS_CONTEXT}' status was left pending by a run that did not finish; set it to error."


def post_skip_status(
    env: Mapping[str, str],
    gate: Gate,
    *,
    event: Mapping[str, object] | None = None,
    send: Callable[[str, bytes, Mapping[str, str]], None] = _send_status,
) -> str:
    """Post ``success`` / ``skipped: <reason>`` for a skipped run; return ``""`` or why nothing was posted.

    Only a gate with a ``skip`` reason posts one, and a skipped run must stay a skipped run, so this never raises.
    """

    if not gate.skip:
        return ""
    return post_status(env, "success", f"skipped: {gate.skip}", writable=gate.writable, event=event, send=send)


def post_error_status(
    env: Mapping[str, str],
    gate: Gate,
    *,
    event: Mapping[str, object] | None = None,
    send: Callable[[str, bytes, Mapping[str, str]], None] = _send_status,
) -> str:
    """Post ``error`` for a run the gate stopped because it is misconfigured; return ``""`` or why nothing was posted."""

    if not gate.error_status:
        return ""
    return post_status(env, "error", gate.error_status, writable=gate.writable, event=event, send=send)


def main(argv: list[str] | None = None) -> int:
    """Script entry for ``action.yml``: decide, print a workflow command, write ``run=`` and ``extra=`` to ``$GITHUB_OUTPUT``."""

    if argv and argv[0] == "settle":
        note = settle_pending_status(os.environ)
        if note:
            print(f"::warning title=LemonCrow Review::{note}")
        return 0
    # The key inputs become the standard variables first, and every key is masked before anything else is printed.
    env = key_environment(os.environ)
    for command in mask_commands(env):
        print(command)
    raw_number = (argv[0] if argv else str(env.get("LEMONCROW_REVIEW_PR") or "")).strip()
    if str(env.get("GITHUB_EVENT_NAME") or "") in CHAT_EVENTS:
        gate = decide_chat(env, provider=str(env.get(PROVIDER_ENV) or "auto"))
    else:
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
    if not gate.run and gate.exit_code != 0:
        unposted = post_error_status(env, gate)
        if unposted:
            print(f"::warning title=LemonCrow Review::{unposted}")
    output = str(env.get("GITHUB_OUTPUT") or "").strip()
    if output:
        route = resolve_model_route(env, str(env.get(PROVIDER_ENV) or "auto")) if gate.run else None
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"run={'true' if gate.run else 'false'}\n")
            handle.write(f"extra={_EXTRA_FOR_BACKEND.get(route.backend, '') if route is not None else ''}\n")
    return gate.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
