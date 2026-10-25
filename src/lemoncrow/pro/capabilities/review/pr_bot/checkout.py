#!/usr/bin/env python3
"""Make the workspace hold the pull request, with the history its merge base needs. Standard library only.

``action.yml`` runs this file as a script right after the gate and before anything is installed, so a workflow needs
no ``actions/checkout`` step and no ``fetch-depth: 0``:

* **No checkout in the workspace.** The Action makes one: an empty repository whose ``origin`` is this repository, the
  pull request's head and its base branch fetched, the base branch's tip checked out (detached). The review reads the
  pull request from git, so no file of the pull request's author is ever on disk in a job that holds secrets.
* **A checkout that is not shallow.** Nothing to do; the review fetches the pull request itself.
* **A shallow checkout** (``actions/checkout``'s default is one commit). The Action deepens it: the head and the base
  branch are fetched with 100 commits of history, then 500, then 2,500, then all of it, and it stops at the first depth
  at which the two share an ancestor. Fetching deeper than that is the slow part for a large repository, so it is
  done only as far as the merge base needs.

When the merge base cannot be found even with all of the history, or a fetch is refused, the run stops with an
``::error`` that says what to do (``fetch-depth: 0``, or ``permissions: contents: read``) and, on a pull request,
sets the ``LemonCrow Review`` status to ``error`` so a required check shows why instead of waiting.

Credentials. ``actions/checkout`` leaves the workflow token in ``.git/config`` unless told not to; this script uses
that when it is there and otherwise gives each ``git`` call the workflow token through the environment, so a checkout
made with ``persist-credentials: false`` works too and the token is never written to disk. The review step does the
same (``action.yml``).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

try:  # imported by the package (tests, the installed CLI)
    from lemoncrow.pro.capabilities.review.pr_bot import ci_env
except ImportError:  # run as a script before anything is installed: the sibling file
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ci_env  # type: ignore[no-redef]

#: Commits of history to fetch before looking for the merge base again; after the last, everything.
HISTORY_LADDER: tuple[int, ...] = (100, 500, 2500)
GIT_TIMEOUT_S = 900
API_TIMEOUT_S = 30
WORKDIR_ENV = "LEMONCROW_WORKDIR"
#: Where the fetched pull request lives in the repository, apart from the user's own branches.
REF_NAMESPACE = "refs/lemoncrow"


class CheckoutError(RuntimeError):
    """The workspace cannot be made ready; the message says what to change."""


@dataclass(frozen=True)
class Outcome:
    """``action`` is ``existing`` (nothing to do), ``created`` (no checkout was there) or ``deepened`` (it was shallow)."""

    action: str
    commits: int = 0
    note: str = ""


Git = Callable[[Sequence[str], Path, Mapping[str, str]], "subprocess.CompletedProcess[str]"]
ApiGet = Callable[[str, str], object]


def run_git(args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=dict(env), capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False
    )


def api_get(url: str, token: str) -> object:
    """GET *url* as JSON with the workflow token; raises ``OSError`` or ``ValueError`` on any failure."""

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "lemoncrow-review-checkout",
        },
    )
    with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
        return json.load(response)


def auth_value(token: str) -> str:
    """The base64 part of the ``Authorization`` header ``actions/checkout`` itself would persist."""

    return base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")


def with_credentials(env: Mapping[str, str], *, server: str, token: str) -> dict[str, str]:
    """*env* plus a git configuration (in the environment, not on disk) that sends the workflow token to *server*."""

    merged = dict(env)
    merged["GIT_TERMINAL_PROMPT"] = "0"
    if not (server and token):
        return merged
    try:
        count = int(merged.get("GIT_CONFIG_COUNT") or "0")
    except ValueError:
        count = 0
    merged[f"GIT_CONFIG_KEY_{count}"] = f"http.{server.rstrip('/')}/.extraheader"
    merged[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: basic {auth_value(token)}"
    merged["GIT_CONFIG_COUNT"] = str(count + 1)
    return merged


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _out(done: subprocess.CompletedProcess[str]) -> str:
    return (done.stdout or "").strip()


def _text(done: subprocess.CompletedProcess[str], secrets: Sequence[str]) -> str:
    detail = " ".join(((done.stderr or "") + " " + (done.stdout or "")).split())
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "***")
    return detail[:300]


def _is_repository(run: Git, directory: Path, env: Mapping[str, str]) -> bool:
    if not directory.is_dir():
        return False
    done = run(["rev-parse", "--is-inside-work-tree"], directory, env)
    return done.returncode == 0 and _out(done) == "true"


def _is_shallow(run: Git, directory: Path, env: Mapping[str, str]) -> bool:
    return _out(run(["rev-parse", "--is-shallow-repository"], directory, env)) == "true"


def _has_persisted_credentials(run: Git, directory: Path, env: Mapping[str, str], server: str) -> bool:
    done = run(["config", "--get-all", f"http.{server.rstrip('/')}/.extraheader"], directory, env)
    return done.returncode == 0 and bool(_out(done))


def _pull_request(env: Mapping[str, str], event: Mapping[str, object], api: ApiGet) -> tuple[int, str, str]:
    """``(number, base branch, head sha)`` of the pull request this run is about; the API fills what the event lacks."""

    raw = str(env.get("LEMONCROW_REVIEW_PR") or "").strip()
    chat = ci_env.chat_event(str(env.get("GITHUB_EVENT_NAME") or ""), event)
    number = int(raw) if raw.isdigit() else (chat.pull_number if chat is not None else ci_env.event_pull_number(event))
    if number <= 0:
        raise CheckoutError("No pull request in this event; there is nothing to check out.")
    pull = _mapping(event.get("pull_request"))
    base = str(_mapping(pull.get("base")).get("ref") or "")
    head = str(_mapping(pull.get("head")).get("sha") or "")
    if ci_env.event_pull_number(event) != number or not (base and head):
        repository = str(env.get("GITHUB_REPOSITORY") or "")
        root = str(env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        token = str(env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or "")
        try:
            found = _mapping(api(f"{root}/repos/{repository}/pulls/{number}", token))
        except (OSError, ValueError) as exc:
            raise CheckoutError(
                f"Could not read pull request #{number} from GitHub ({type(exc).__name__}). The workflow token needs "
                "`contents: read` or `pull-requests: read`; the one-line fix is "
                "`permissions: { contents: read, pull-requests: write, statuses: write }`."
            ) from exc
        base = str(_mapping(found.get("base")).get("ref") or "")
        head = str(_mapping(found.get("head")).get("sha") or "")
    if not (base and head):
        raise CheckoutError(f"GitHub did not report the base branch and head of pull request #{number}.")
    return number, base, head


def _fetch(
    run: Git,
    directory: Path,
    env: Mapping[str, str],
    refspecs: Sequence[str],
    *,
    depth: int | None,
    secrets: Sequence[str],
) -> None:
    args = ["fetch", "--no-tags", "--no-recurse-submodules", "--quiet"]
    if depth is None:
        if _is_shallow(run, directory, env):
            args.append("--unshallow")
    else:
        args.append(f"--depth={depth}")
    done = run([*args, "origin", *refspecs], directory, env)
    if done.returncode != 0:
        raise CheckoutError(_text(done, secrets) or f"git fetch exited {done.returncode}")


def _merge_base(run: Git, directory: Path, env: Mapping[str, str], base_ref: str, head_ref: str) -> bool:
    return run(["merge-base", base_ref, head_ref], directory, env).returncode == 0


def _commit_count(run: Git, directory: Path, env: Mapping[str, str], ref: str) -> int:
    count = _out(run(["rev-list", "--count", ref], directory, env))
    return int(count) if count.isdigit() else 0


def prepare(
    env: Mapping[str, str],
    *,
    run: Git = run_git,
    api: ApiGet = api_get,
    event: Mapping[str, object] | None = None,
) -> Outcome:
    """Leave the workspace holding the pull request and enough history to find its merge base, or raise."""

    workspace = Path(str(env.get("GITHUB_WORKSPACE") or ".")).resolve()
    directory = (workspace / (str(env.get(WORKDIR_ENV) or ".").strip() or ".")).resolve()
    server = str(env.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    repository = str(env.get("GITHUB_REPOSITORY") or "").strip()
    token = str(env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or "").strip()
    secrets = (token, auth_value(token)) if token else ()
    plain = dict(env)
    plain["GIT_TERMINAL_PROMPT"] = "0"

    present = _is_repository(run, directory, plain)
    if present and not _is_shallow(run, directory, plain):
        return Outcome("existing")

    payload = ci_env.load_event(env) if event is None else event
    number, base, head = _pull_request(env, payload, api)
    created = not present
    if created:
        if not repository:
            raise CheckoutError("GITHUB_REPOSITORY is not set, so the repository to check out is unknown.")
        directory.mkdir(parents=True, exist_ok=True)
        for args in (["init", "--quiet"], ["remote", "add", "origin", f"{server}/{repository}.git"]):
            done = run(args, directory, plain)
            if done.returncode != 0:
                raise CheckoutError(f"git {args[0]} failed in {directory}: {_text(done, secrets)}")
    if created or not _has_persisted_credentials(run, directory, plain, server):
        # No checkout of ours to carry the token, or persist-credentials: false: give git the workflow token for these
        # calls through the environment.
        plain = with_credentials(plain, server=server, token=token)

    head_ref, base_ref = f"{REF_NAMESPACE}/pr-{number}-head", f"{REF_NAMESPACE}/pr-{number}-base"
    refspecs = [f"+refs/pull/{number}/head:{head_ref}", f"+refs/heads/{base}:{base_ref}"]
    found = False
    for depth in (*HISTORY_LADDER, None):
        try:
            _fetch(run, directory, plain, refspecs, depth=depth, secrets=secrets)
        except CheckoutError as exc:
            raise CheckoutError(_explain_fetch_failure(str(exc), created=created, server=server)) from exc
        if _merge_base(run, directory, plain, base_ref, head_ref):
            found = True
            break
    if not found:
        raise CheckoutError(
            f"Pull request #{number} shares no history with {base} even with the full history fetched, so there is "
            "nothing to diff against. Check that its base branch is the one it was opened against."
        )
    if created:
        # The base branch, not the pull request: the review reads the pull request's commits from git, never from the
        # working tree, so nothing the pull request's author wrote is ever on disk in a job that holds secrets.
        for args in (["config", "advice.detachedHead", "false"], ["checkout", "--quiet", "--detach", base_ref]):
            done = run(args, directory, plain)
            if done.returncode != 0:
                raise CheckoutError(
                    f"git {args[0]} failed in {directory}: {_text(done, secrets)}. If the workspace already holds "
                    "files that are not a git checkout, add an `actions/checkout` step before the Action."
                )
    commits = _commit_count(run, directory, plain, head_ref)
    return Outcome("created" if created else "deepened", commits, f"pull request #{number} at {head[:7]}")


def _explain_fetch_failure(detail: str, *, created: bool, server: str) -> str:
    lowered = detail.lower()
    if "403" in lowered or "denied" in lowered or "authentication failed" in lowered or "401" in lowered:
        return (
            f"GitHub refused to send the pull request's history ({detail}). The workflow token needs "
            "`contents: read`; the one-line fix is "
            "`permissions: { contents: read, pull-requests: write, statuses: write }`."
        )
    if created:
        return (
            f"The Action could not fetch the pull request from {server} ({detail}). Check that the runner can reach "
            "it, or add an `actions/checkout` step with `fetch-depth: 0` before the Action."
        )
    return (
        f"The checkout is shallow and could not be deepened ({detail}). Set `fetch-depth: 0` on your "
        "`actions/checkout` step, or remove that step and let the Action check out the pull request itself."
    )


_SANITIZE = re.compile(r"[\r\n]+")


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    token = str(env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or "").strip()
    if token:
        print(f"::add-mask::{auth_value(token)}")
    try:
        outcome = prepare(env)
    except CheckoutError as exc:
        message = _SANITIZE.sub(" ", str(exc))
        print(f"::error title=LemonCrow Review::{message}")
        if str(env.get("GITHUB_EVENT_NAME") or "") == "pull_request":
            unposted = ci_env.post_status(env, "error", f"Could not check out the pull request: {message}"[:140])
            if unposted:
                print(f"::warning title=LemonCrow Review::{unposted}")
        return 1
    output = str(env.get("GITHUB_OUTPUT") or "").strip()
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"checkout={outcome.action}\n")
    if outcome.action == "created":
        print(f"Made a checkout of the base branch and fetched {outcome.note} ({outcome.commits} commits of history).")
    elif outcome.action == "deepened":
        print(
            f"The checkout was shallow; fetched the history of {outcome.note} down to its merge base "
            f"({outcome.commits} commits)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
