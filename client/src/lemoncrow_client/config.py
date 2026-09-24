"""Everything the client is allowed to know about its environment.

Four decisions live here, and each one is a claim an enterprise reviewer will
check:

**One product endpoint, one discovered auth origin.** ``LEMONCROW_URL`` is the
only LemonCrow data-plane destination. Hosted interactive login may additionally
contact the Authward issuer advertised by that server; Authward discovery must
repeat the issuer exactly and every OAuth endpoint must remain on its origin.
Each :class:`~lemoncrow_client.transport.HttpTransport` is still bound to one
origin, so neither discovery document can create arbitrary egress.

**One writable directory outside the repository.** ``~/.lemoncrow`` (or
``LEMONCROW_HOME``). It holds three things and nothing else: the IT-provisioned
``token``; an ``env`` file of ``LEMONCROW_*`` settings that ``lemoncrow-client
target`` writes -- the real environment always wins over it, so a session can
override what is on disk and never the other way round -- and
``walk-cache/``, one file per worktree recording which files were unchanged when
they were last hashed (see :mod:`lemoncrow_client.walkcache`). The cache holds
paths and digests, never content; deleting it costs one slow session and nothing
else. No unit directory, no ``~/.claude/settings.json`` rewrite, and no local
index -- an unreachable server degrades to a documented reduced mode rather than
quietly rebuilding one here.

**Credentials have explicit ownership.** ``LEMONCROW_TOKEN`` from the
environment, else an IT-provisioned file, always wins. Hosted interactive
login may additionally persist one managed access/refresh pair below the same
state directory; local mode never reads it. The client refuses group- or
world-readable credential files rather than quietly using them.

**Repository identity is never a git remote.** The server issues ``repo_id``;
what the client may present is a canonical SCM identity or an opaque
fingerprint over the root commit. A remote URL may carry credentials, may be
one of several, and differs between forks, so it is not identity and this
module has nowhere to put one.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from .credentials import managed_credentials_path, read_managed_credentials
from .errors import AgentAction, ClientError, ErrorCode
from .resolution_cache import cache_budget_bytes

__all__ = [
    "DEFAULT_URL",
    "ClientConfig",
    "RepoIdentity",
    "load_config",
    "resolve_token",
]

#: The loopback deployment a solo developer runs. "Local" is a server bound to
#: loopback, not a different code path.
DEFAULT_URL: Final[str] = "http://127.0.0.1:7420"

_TOKEN_ENV: Final[str] = "LEMONCROW_TOKEN"
_TOKEN_FILE_ENV: Final[str] = "LEMONCROW_TOKEN_FILE"
_URL_ENV: Final[str] = "LEMONCROW_URL"
_MODE_ENV: Final[str] = "LEMONCROW_INSTALL_MODE"
_HOME_ENV: Final[str] = "LEMONCROW_HOME"
_MIN_TOKEN_LEN: Final[int] = 16
_MAX_TOKEN_BYTES: Final[int] = 4096
_MAX_SETTINGS_BYTES: Final[int] = 8192
_MAX_SETTINGS_LINES: Final[int] = 64

#: The one file the client writes under the worktree, and only when the
#: operator opted into the same-host optimization. Named so a repository can
#: ``.gitignore`` it once.
LOCAL_FS_PROOF_PATH: Final[str] = ".lemoncrow-local-fs-proof"


@dataclass(frozen=True, slots=True)
class RepoIdentity:
    """What the client may claim about which repository a view belongs to.

    There is deliberately no field for a remote URL. The server refuses a
    request carrying one, so a client that had somewhere to put it would only
    be able to produce refusals.
    """

    scm_provider: str
    scm_repo_id: str
    fingerprint: str

    def to_wire(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self.scm_provider and self.scm_repo_id:
            payload["scm_provider"] = self.scm_provider
            payload["scm_repo_id"] = self.scm_repo_id
        if self.fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True, slots=True)
class ClientConfig:
    """Resolved configuration for one short-lived client process."""

    url: str
    token: str
    repo_root: Path
    state_dir: Path
    #: Explicit runtime topology. ``hosted`` is a security boundary: code using
    #: this config must not start listeners or background LemonCrow processes.
    install_mode: str
    #: Whole-of-MCP-initialize bootstrap budget. Initialization is fail-open:
    #: past this the same stdio process continues with reduced local tools.
    startup_budget_s: float
    #: Per-request ceiling for the sync and tool routes.
    request_timeout_s: float
    #: Longer ceiling for a tool call, which may legitimately take a while.
    tool_timeout_s: float
    #: Memory-only, server-validated response cache. This is an eviction budget,
    #: not a reservation; zero disables it. Automatic sizing is 10% of available
    #: memory, capped by the cache implementation. Forced to 0 by
    #: ``LEMONCROW_CACHE_DISABLED`` -- the same global kill switch the private
    #: server's own tool-result caches read (``lemoncrow.core.environment.cache_disabled``)
    #: -- even when ``LEMONCROW_RESOLUTION_CACHE_BYTES`` asks for a nonzero budget.
    resolution_cache_bytes: int
    #: Offer the same-host read optimization. Off unless explicitly enabled:
    #: it writes a proof file into the worktree, and a client should not do
    #: that because it guessed it was on the same host as the server.
    offer_local_fs: bool
    #: Where the token came from, for the client audit output. Never the token
    #: itself.
    token_source: str
    #: Managed hosted refresh credential. Operator-provided access credentials
    #: intentionally suppress this field so they can never be mixed with a
    #: stale interactive-login family.
    refresh_token: str = ""
    refresh_token_source: str = ""

    @property
    def endpoint_origin(self) -> tuple[str, str, int]:
        """``(scheme, host, port)`` -- the only origin the client may reach."""
        parts = urlsplit(self.url)
        scheme = parts.scheme or "http"
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if scheme == "https" else 80)
        return scheme, host, int(port)

    def endpoint(self, path: str) -> str:
        return f"{self.url.rstrip('/')}{path}"

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    @property
    def hosted(self) -> bool:
        return self.install_mode == "hosted"


def read_settings_file(state_dir: Path) -> dict[str, str]:
    """``LEMONCROW_*`` settings persisted by ``lemoncrow-client target``.

    A flat ``KEY=value`` file, bounded, and filtered to this product's own
    prefix: a settings file that could set ``PATH`` or ``LD_PRELOAD`` would be
    a privilege-escalation primitive dressed as a convenience.
    """
    target = state_dir / "env"
    try:
        if target.stat().st_size > _MAX_SETTINGS_BYTES:
            return {}
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    settings: dict[str, str] = {}
    for line in text.splitlines()[:_MAX_SETTINGS_LINES]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("LEMONCROW_") and key != _HOME_ENV:
            # ``LEMONCROW_HOME`` is how this file was found; letting it point
            # somewhere else from inside itself is a loop, not a setting.
            settings[key] = value.strip().strip("'\"")
    return settings


def _validated_url(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"{_URL_ENV} must be an http(s) URL with a host",
            details={"url": raw},
            action=AgentAction.FIX_REQUEST,
        )
    if parts.username or parts.password:
        # A credential in the endpoint would end up in every log line that
        # records where the client connected.
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"{_URL_ENV} must not embed credentials",
            action=AgentAction.FIX_REQUEST,
        )
    host = parts.hostname.strip("[]").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme == "http" and not loopback:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"{_URL_ENV} must use HTTPS outside literal loopback development",
            details={"host": host},
            action=AgentAction.FIX_REQUEST,
        )
    return raw.rstrip("/")


def _read_token_file(path: Path) -> str:
    try:
        info = path.stat()
    except OSError as exc:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"token file is not readable: {path}",
            action=AgentAction.REAUTHENTICATE,
        ) from exc
    if info.st_size > _MAX_TOKEN_BYTES:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"token file is larger than {_MAX_TOKEN_BYTES} bytes: {path}",
            action=AgentAction.REAUTHENTICATE,
        )
    if info.st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        # The same rule the server applies to its own registry. A silently
        # world-readable credential store is the finding a security review
        # opens with, and using it anyway would make the client complicit.
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"token file must not be group- or world-accessible (chmod 600): {path}",
            details={"path": str(path)},
            action=AgentAction.REAUTHENTICATE,
        )
    try:
        return path.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"token file is not valid UTF-8: {path}",
            action=AgentAction.REAUTHENTICATE,
        ) from exc


def resolve_token(env: dict[str, str], state_dir: Path) -> tuple[str, str]:
    """Resolve operator-owned access credentials only.

    Environment wins over the explicit/default provisioned token file. Managed
    hosted-login credentials are resolved separately after runtime mode is
    known, so they can never override an operator credential or leak into local
    mode.
    """
    from_env = env.get(_TOKEN_ENV, "").strip()
    if from_env:
        if len(from_env) < _MIN_TOKEN_LEN:
            raise ClientError(
                ErrorCode.NOT_CONFIGURED,
                f"{_TOKEN_ENV} is shorter than {_MIN_TOKEN_LEN} characters",
                action=AgentAction.REAUTHENTICATE,
            )
        return from_env, _TOKEN_ENV

    explicit = env.get(_TOKEN_FILE_ENV, "").strip()
    candidates = [Path(explicit)] if explicit else [state_dir / "token"]
    for candidate in candidates:
        if candidate.is_file():
            token = _read_token_file(candidate)
            if token:
                return token, str(candidate)
    return "", ""


def _run_git(repo_root: Path, *args: str, timeout: float = 5.0) -> str:
    """Run one bounded read-only git command. Absent git is not an error."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def discover_repo_root(start: Path) -> Path:
    """The worktree root, by ``.git`` marker, falling back to ``start``."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def repo_identity(repo_root: Path, env: dict[str, str] | None = None) -> RepoIdentity:
    """A claim the server can canonicalize into an org-scoped ``repo_id``.

    Canonical SCM identity when the deployment provisioned one, otherwise the
    root-commit digest: stable across clones, mirrors and forks, and it names
    no host and carries no credential. A repository with neither -- an
    unversioned directory -- gets a digest over its absolute path, which is
    stable for that checkout and meaningless anywhere else.
    """
    environment = env if env is not None else dict(os.environ)
    provider = environment.get("LEMONCROW_SCM_PROVIDER", "").strip()
    scm_repo = environment.get("LEMONCROW_SCM_REPO_ID", "").strip()
    roots = _run_git(repo_root, "rev-list", "--max-parents=0", "HEAD")
    fingerprint = ""
    if roots:
        # A repository with several root commits (a merged history) still has
        # one canonical first line in ``rev-list`` order.
        fingerprint = roots.splitlines()[0].strip()
    if not fingerprint:
        import hashlib

        fingerprint = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()
    return RepoIdentity(scm_provider=provider, scm_repo_id=scm_repo, fingerprint=fingerprint)


def base_revision(repo_root: Path) -> str:
    """The commit the view is based on, or a stable placeholder."""
    head = _run_git(repo_root, "rev-parse", "HEAD")
    return head or "0" * 40


def load_config(
    env: dict[str, str] | None = None,
    *,
    cwd: Path | None = None,
) -> ClientConfig:
    """Resolve configuration. Raises only for a *misconfiguration*.

    An absent token is not a misconfiguration here: the hook must still be able
    to report one line and let the session continue with local tools, so an
    unauthenticated config is a valid object whose :attr:`authenticated` is
    ``False``.
    """
    supplied = env if env is not None else dict(os.environ)
    home = supplied.get(_HOME_ENV, "").strip()
    state_dir = Path(home) if home else Path(supplied.get("HOME", str(Path.home()))) / ".lemoncrow"
    # The process environment wins over the persisted file, so a session can
    # point at a different endpoint for one run without rewriting anything.
    environment = {**read_settings_file(state_dir), **supplied}
    url = _validated_url(environment.get(_URL_ENV, "").strip() or DEFAULT_URL)
    install_mode = _runtime_mode(environment, url)
    repo_root = discover_repo_root(cwd if cwd is not None else Path.cwd())
    token, source = resolve_token(environment, state_dir)
    refresh_token = ""
    refresh_token_source = ""
    if install_mode == "hosted" and not token:
        managed_access, managed_refresh = read_managed_credentials(state_dir)
        if managed_access:
            token = managed_access
            source = str(managed_credentials_path(state_dir))
        if managed_refresh:
            refresh_token = managed_refresh
            refresh_token_source = str(managed_credentials_path(state_dir))
    return ClientConfig(
        url=url,
        token=token,
        repo_root=repo_root,
        state_dir=state_dir,
        install_mode=install_mode,
        startup_budget_s=_float_env(environment, "LEMONCROW_STARTUP_BUDGET_S", 1.0),
        request_timeout_s=_float_env(environment, "LEMONCROW_REQUEST_TIMEOUT_S", 10.0),
        tool_timeout_s=_float_env(environment, "LEMONCROW_TOOL_TIMEOUT_S", 120.0),
        resolution_cache_bytes=(
            0
            if _bool_env(environment, "LEMONCROW_CACHE_DISABLED", False)
            else cache_budget_bytes(environment.get("LEMONCROW_RESOLUTION_CACHE_BYTES"))
        ),
        offer_local_fs=_bool_env(environment, "LEMONCROW_LOCAL_FS", False),
        token_source=source,
        refresh_token=refresh_token,
        refresh_token_source=refresh_token_source,
    )


def _runtime_mode(environment: dict[str, str], url: str) -> str:
    """Resolve local vs hosted, with a compatibility fallback for older installs."""
    explicit = environment.get(_MODE_ENV, "").strip().lower()
    if explicit:
        if explicit not in {"local", "hosted"}:
            raise ClientError(
                ErrorCode.NOT_CONFIGURED,
                f"{_MODE_ENV} must be 'local' or 'hosted', got {explicit!r}",
                action=AgentAction.FIX_REQUEST,
            )
        return explicit
    host = (urlsplit(url).hostname or "").strip("[]").lower()
    return "local" if host in {"127.0.0.1", "localhost", "::1"} else "hosted"


def _float_env(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _bool_env(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}
