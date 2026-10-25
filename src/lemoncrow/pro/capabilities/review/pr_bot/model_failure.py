"""Why a model call failed, in one line a person can act on.

A rejected key, an account out of credit and a provider that is down all end the same way for the review: no
investigation came back. They need different words. A key is the user's to fix, an overloaded provider is not, and
"model calls failed; see the workflow log" tells neither. :func:`classify` reads the error text the model route left
behind (``provider_errors``) and returns a :class:`ModelFailure`: a ``cause`` for the summary comment and the commit
status, and a ``fix`` that goes into the ``::error`` annotation beside it. The ``fix`` and ``short`` talk about the
Action's secrets, inputs and job log; a run in a terminal (``in_actions=False``) gets wording that is true there instead.

The text is only ever matched, never repeated: a provider may echo part of a key in its error, so what reaches a
comment, a status or an annotation is the fixed wording below, and :func:`scrub` removes any secret from the one raw
excerpt the run log keeps. Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

#: The action input that carries each provider's key, as the workflow spells it.
KEY_INPUT = {"anthropic": "anthropic-api-key", "openai": "openai-api-key"}
#: The environment variable a person at a terminal sets for each provider.
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
PROVIDER_NAME = {"anthropic": "Anthropic", "openai": "OpenAI"}


@dataclass(frozen=True)
class ModelFailure:
    """``kind`` is one of ``auth``, ``credit``, ``rate_limit``, ``model``, ``overloaded``, ``json`` or ``other``."""

    kind: str
    #: What happened, no advice.
    cause: str
    #: What to do about it. Together with ``cause`` this is the annotation.
    fix: str
    #: The fix in a few words, for the headline of the summary comment and the commit status description.
    short: str

    @property
    def annotation(self) -> str:
        return f"{self.cause}. {self.fix}"

    @property
    def headline(self) -> str:
        """One line for the summary comment and the status (``Review incomplete: <headline>``): cause, then the fix."""

        return f"{self.cause}; {self.short}"


def _code(*numbers: str) -> str:
    """A status code, only when the text says it is one (``Error code: 401``, ``status_code=429``, ``HTTP 403``)."""

    return rf"(?:error code|status(?:[_ ]code)?|http)\W{{0,3}}(?:{'|'.join(numbers)})\b"


# Order matters: the first pattern that matches wins, and the credit message must beat the rate-limit one (Anthropic
# words an empty balance as a rate_limit_error on some routes).
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "credit",
        re.compile(
            r"credit balance|insufficient[_ ](?:quota|credit|funds)|exceeded your current quota|out of credit"
            r"|billing|payment required|purchase credits|" + _code("402"),
            re.IGNORECASE,
        ),
    ),
    (
        "auth",
        re.compile(
            r"AuthenticationError|authentication_error|invalid[ _-]?x?-?api[ _-]?key|incorrect api key|invalid api key"
            r"|unauthori[sz]ed|PermissionDeniedError|permission_error|api key not valid"
            r"|could not resolve authentication|no api key|" + _code("401", "403"),
            re.IGNORECASE,
        ),
    ),
    ("rate_limit", re.compile(r"RateLimitError|rate[_ ]limit|too many requests|" + _code("429"), re.IGNORECASE)),
    (
        "model",
        re.compile(
            r"NotFoundError|not_found_error|model_not_found|model[^\n]{0,40}(?:does not exist|not found|not supported)"
            r"|unknown model|" + _code("404"),
            re.IGNORECASE,
        ),
    ),
    (
        "overloaded",
        re.compile(
            r"overloaded|InternalServerError|ServiceUnavailable|BadGateway|APIConnectionError|Timeout"
            r"|timed out|connection (?:error|reset|refused)|temporarily unavailable|" + _code(r"5\d\d"),
            re.IGNORECASE,
        ),
    ),
    ("json", re.compile(r"invalid JSON|could not be read as JSON", re.IGNORECASE)),
)
#: Which kinds mean the next run will fail the same way until the person changes something.
PERMANENT = frozenset({"auth", "credit", "model"})


def classify(
    errors: Iterable[str],
    *,
    provider: str = "",
    key_input: str = "",
    model: str = "",
    in_actions: bool = True,
) -> ModelFailure | None:
    """The failure *errors* describe, or ``None`` when there are none.

    *provider* is ``anthropic`` or ``openai`` when the route is one of those (it names the key the fix points at);
    anything else, a LiteLLM route for a cloud provider, gets wording that does not claim a key. *in_actions* is false
    for a run in a terminal, where there is no workflow, secret, input or job log to point at.
    """

    failure = _classify(errors, provider=provider, key_input=key_input, model=model)
    if failure is None or in_actions:
        return failure
    who = PROVIDER_NAME.get(provider, "The model provider")
    key = KEY_ENV.get(provider, "")
    where = f"your API key ({key})" if key else "your model credentials"
    fix, short = {
        "auth": (f"Check {where}: it must be a valid, active key", f"check {where}"),
        "credit": (
            f"Add credit or raise the spend limit in the {who} console, then run the review again",
            f"add credit in the {who} console",
        ),
        "rate_limit": (
            "Run the review again in a few minutes, or raise the key's rate limit with the provider",
            "run again in a few minutes",
        ),
        "model": (
            "Use a model id the key can use, or leave the model unset for the default",
            "use a model the key can use",
        ),
        "overloaded": (
            "Nothing is wrong with your setup; run the review again in a few minutes",
            "run again in a few minutes",
        ),
        "json": ("Run the review again; if it repeats, use a stronger model", "run again"),
    }.get(
        failure.kind,
        (
            "The model's own error is the `model call errors` line printed with this review (add --json to read it "
            "with the rest); run the review again",
            "see the model call errors line",
        ),
    )
    return replace(failure, fix=fix, short=short)


def _classify(errors: Iterable[str], *, provider: str, key_input: str, model: str) -> ModelFailure | None:
    """The failure in the Action's words: secrets, inputs and the job log."""

    text = "\n".join(str(error) for error in errors)
    if not text.strip():
        return None
    who = PROVIDER_NAME.get(provider, "The model provider")
    key = key_input or KEY_INPUT.get(provider, "")
    where = f"the secret passed as `{key}`" if key else "the model credentials"
    kind = next((name for name, pattern in _PATTERNS if pattern.search(text)), "other")
    if kind == "auth":
        forbidden = re.search(r"PermissionDeniedError|permission_error|" + _code("403"), text, re.IGNORECASE)
        return ModelFailure(
            kind,
            f"{who} rejected the API key (HTTP {'403, not allowed' if forbidden else '401'})",
            f"Check {where}: it must be a valid, active key. Update the secret, then re-run the job",
            f"check {where}",
        )
    if kind == "credit":
        return ModelFailure(
            kind,
            f"{who} reports no credit left on this API key",
            f"Add credit or raise the spend limit in the {who} console, then re-run the job",
            f"add credit in the {who} console",
        )
    if kind == "rate_limit":
        return ModelFailure(
            kind,
            f"{who} rate-limited the review (HTTP 429)",
            "Re-run the job in a few minutes, or raise the key's rate limit with the provider",
            "re-run in a few minutes",
        )
    if kind == "model":
        named = f" `{model}`" if model else ""
        return ModelFailure(
            kind,
            f"{who} does not know the model{named} for this key",
            "Set the `model` input to a model id the key can use, or leave it empty for the default",
            "fix the `model` input",
        )
    if kind == "overloaded":
        return ModelFailure(
            kind,
            f"{who} was unavailable or overloaded",
            "Nothing is wrong with the setup; re-run the job in a few minutes",
            "re-run the job",
        )
    if kind == "json":
        return ModelFailure(
            kind,
            "The model did not answer in the format the review needs",
            "Re-run the job; if it repeats, set the `model` input to a stronger model",
            "re-run the job",
        )
    return ModelFailure(
        "other",
        "model calls failed",
        "The first error is in the job log, under `Review the pull request`. Re-run the job",
        "see the workflow log",
    )


_SECRET_SHAPES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xox[abprs]-[A-Za-z0-9-]{8,})"
)


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """*text* with every value in *secrets* and anything shaped like an API key or token replaced by ``***``."""

    for secret in secrets:
        if len(secret) >= 6:
            text = text.replace(secret, "***")
    return _SECRET_SHAPES.sub("***", text)


def key_values(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The credentials in *environ* a log line must never repeat."""

    names = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LEMONCROW_OPENAI_API_KEY", "GH_TOKEN", "GITHUB_TOKEN")
    return tuple(value for name in names if len(value := str(environ.get(name) or "").strip()) >= 6)


def first_error(errors: Sequence[str], secrets: Iterable[str] = ()) -> str:
    """The first error of the run, cut to a log line and scrubbed."""

    return scrub(" ".join(str(errors[0]).split())[:200], secrets) if errors else ""


__all__ = [
    "KEY_INPUT",
    "PERMANENT",
    "ModelFailure",
    "classify",
    "first_error",
    "key_values",
    "scrub",
]
