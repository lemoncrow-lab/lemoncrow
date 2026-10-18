"""The thin-client entry points.

``runtime``
    Explicit Runtime integration commands, including the MCP stdio process.
    Host registration is never implied by Review.
``target``
    Print, or write into the installed product's owned settings file, the endpoint the client uses.
``audit``
    Print the resolved runtime claims for enterprise review.

Updates and first-use product installs run only through the explicit CLI paths;
there is no SessionStart helper, and nothing runs in the background by default.
The one listener, tunnel and pair of user services belong to the opt-in remote MCP gateway (``runtime mcp serve``,
:mod:`lemoncrow_client.remote`), which only this module reaches, lazily.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TextIO
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .lite.index import LiteIndexProgress, ProgressCallback

from . import CLIENT_NAME, CLIENT_VERSION
from .auth_cli import auth_main
from .config import load_config, product_settings_path
from .errors import ClientError
from .protocol import CLIENT_CAPABILITIES, PROTOCOL_VERSION

__all__ = ["main", "review_entrypoint"]

_USAGE: Final[str] = f"""{CLIENT_NAME} {CLIENT_VERSION}

usage:
  {CLIENT_NAME} review [OPTIONS]    freeze exact Git sides and open a local or hosted Review
  {CLIENT_NAME} code ...            use LemonCrow's Hosted coding agent
  {CLIENT_NAME} with HOST [...]     use Runtime Lite with claude, codex, opencode, or pi
  {CLIENT_NAME} runtime COMMAND     manage Runtime integration and transports
  {CLIENT_NAME} rt COMMAND          shorthand for runtime (rt host manages LemonCode/Pi host binaries)
  {CLIENT_NAME} run ...             managed/full Runtime only; not part of Runtime Lite
  {CLIENT_NAME} session ...         managed/full Runtime only; not part of Runtime Lite
  {CLIENT_NAME} context ...         managed/full Runtime only; not part of Runtime Lite
  {CLIENT_NAME} status              show installed products and next actions
  {CLIENT_NAME} update [--dry-run]  re-run the release installer for installed products
  {CLIENT_NAME} uninstall PRODUCT   remove review, runtime, or all
  {CLIENT_NAME} auth [COMMAND]      sign in to Hosted Runtime/Review
  {CLIENT_NAME} target [URL]        show or set a Hosted Runtime/Review endpoint
  {CLIENT_NAME} audit               print what this client does and does not do

runtime commands:
  code [OPTIONS]                 LemonCrow Hosted coding agent
  index [OPTIONS]                build/refresh local Runtime Lite index
  savings ...                    managed/full Runtime Local Savings controls
  model ...                      managed/full Runtime local-model registry
  work ...                       managed/full Runtime verified-work diagnostics
  status [NAME]                 report Runtime host registration
  enable NAME                   persist local Runtime Lite
  enable NAME --hosted          explicitly use Hosted Runtime instead
  enable NAME --dry-run         print native host commands without changing host config
  disable NAME [--dry-run]      remove Runtime registration where the host CLI supports it
  lite-mcp [--host NAME]        internal Local Lite stdio process; no listener or daemon
  mcp [--host NAME]             explicit hosted/thin-client Runtime MCP stdio process
  mcp serve [--persistent]      opt-in remote MCP for chat apps (loopback gateway + tunnel)
  mcp service COMMAND           manage the persistent remote MCP gateway and tunnel
  routes                        print the tool execution matrix

environment:
  LEMONCROW_URL                 Hosted Runtime endpoint (unused by Runtime Lite)
  LEMONCROW_TOKEN               explicit operator-provided bearer override
  LEMONCROW_REFRESH_TOKEN       explicit operator-provided refresh override
  LEMONCROW_HOME                state directory (default ~/.lemoncrow)
  LEMONCROW_LOCAL_FS            offer the same-host read optimization (default off)
"""

_PROMOTED_RUNTIME_COMMANDS: Final[frozenset[str]] = frozenset({"run", "session", "context", "model", "work"})
# Runtime commands of the full LemonCrow CLI (a source checkout) that this client
# only delegates, so `lc rt telemetry` says where it lives instead of "unknown".
_FULL_ENGINE_RUNTIME_COMMANDS: Final[frozenset[str]] = frozenset({"telemetry", "map", "dashboard"})
# Root verbs people type from older docs or the full CLI, with the command to use here.
_RUNTIME_VERB_HINTS: Final[dict[str, str]] = {
    "host": "lc rt host",
    "index": "lc runtime index",
    "enable": "lc rt enable",
    "disable": "lc rt disable",
    "mcp": "lc rt mcp",
    "routes": "lc rt routes",
    "install": "lc rt enable",
    "telemetry": "lc rt telemetry",
}
_FULL_ENGINE_VERBS: Final[frozenset[str]] = frozenset({"usage", "doctor", "settings", "completions"})
_REMOTE_MCP_COMMANDS: Final[frozenset[str]] = frozenset({"serve", "client", "service", "gateway"})


def _full_runtime_executable() -> Path | None:
    """Return the full local Runtime CLI, never the thin Runtime client itself."""
    explicit = os.environ.get("LEMONCROW_FULL_RUNTIME_CLI", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None
    home = os.environ.get("LEMONCROW_HOME", "").strip()
    state_dir = Path(home).expanduser() if home else Path(os.environ.get("HOME", str(Path.home()))) / ".lemoncrow"
    candidate = state_dir / "bin" / "lemoncrow"
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _delegate_full_runtime(arguments: Sequence[str], sink: TextIO) -> int:
    executable = _full_runtime_executable()
    if executable is None:
        if arguments and arguments[0] == "runtime" and len(arguments) > 1:
            command = f"runtime {arguments[1]}"
        else:
            command = arguments[0] if arguments else "this command"
        sink.write(
            f"lc {command} is not part of serverless Runtime Lite; it requires the optional managed/full Runtime CLI.\n"
        )
        sink.write("Local Lite itself does not require or start a LemonCrow server.\n")
        sink.write(
            "Run it from a LemonCrow source checkout (`uv run lc ...`), or set LEMONCROW_FULL_RUNTIME_CLI "
            "to an installed full CLI.\n"
        )
        return 2
    if sink is sys.stdout:
        return subprocess.run([str(executable), *arguments], check=False).returncode
    completed = subprocess.run([str(executable), *arguments], capture_output=True, text=True, check=False)
    sink.write(completed.stdout)
    if completed.stderr:
        sink.write(completed.stderr)
    return completed.returncode


_HOSTED_CODE_USAGE: Final[str] = f"""usage:
  {CLIENT_NAME} code [-p PROMPT] [--resume SESSION]

`lc code` is LemonCrow's Hosted coding agent. The owned agent loop is a Hosted/
Enterprise capability; it is not the open-source local Runtime Lite launcher.

For local Runtime Lite, use `lc with claude`, `lc with codex`, `lc with opencode`,
`lc with pi`, or an installed editor integration. Public LemonCode/Pi host lifecycle
is under `lc rt host`.
"""


def _code(rest: Sequence[str], sink: TextIO) -> int:
    if not _module_available("lemoncrow_client.host_trial") or not _module_available("lemoncrow_client.lite.index"):
        return _delegate_external_runtime(["code", *rest], sink)

    arguments = list(rest)
    if arguments and arguments[0] in {"-h", "--help", "help"}:
        sink.write(_HOSTED_CODE_USAGE)
        return 0

    # The open-source client intentionally contains no owned coding loop. When a
    # full Runtime distribution is present, hand the Hosted command to it.
    if _full_runtime_executable() is not None:
        return _delegate_full_runtime(["code", *arguments], sink)

    # Do not keep compatibility parsing for the retired local `--pro` mode. Any
    # old flag is simply not part of this Hosted command surface.
    allowed_value_flags = {"-p", "--prompt", "--resume"}
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in allowed_value_flags:
            index += 1
            if index >= len(arguments):
                sink.write(f"code {argument} requires a value\n")
                return 2
        elif argument.startswith("--prompt=") or argument.startswith("--resume="):
            pass
        else:
            sink.write(f"unknown Hosted code option: {argument}\n")
            sink.write(_HOSTED_CODE_USAGE)
            return 2
        index += 1

    sink.write("lc code is LemonCrow's Hosted coding agent.\n")
    sink.write("This OSS Runtime client does not contain the Hosted owned-agent execution loop.\n")
    sink.write("The Hosted coding loop is not available yet. For Hosted Runtime (Pro), run\n")
    sink.write("`lc rt enable <host> --hosted`, then `lc auth login`. Guide:\n")
    sink.write("https://docs.lemoncrow.com/setup/pro-hosted-runtime\n")
    return 2


def _runtime_store_root() -> Path:
    configured = os.environ.get("LEMONCROW_HOME", "").strip()
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".lemoncrow").resolve()


def _host(rest: Sequence[str], sink: TextIO) -> int:
    usage = (
        f"usage: {CLIENT_NAME} runtime host status|install|update|build|remove "
        "[--engine pi|lemoncode] [--json] [--source DIR] [--yes]\n"
    )
    arguments = list(rest)
    if not arguments or arguments[0] in {"-h", "--help", "help"}:
        sink.write(usage)
        sink.write("Pi lifecycle is fully available in serverless Runtime Lite.\n")
        sink.write("LemonCode lifecycle delegates to the managed/full CLI when installed.\n")
        return 0 if arguments else 2
    action = arguments.pop(0)
    if action not in {"status", "install", "update", "build", "remove"}:
        sink.write(f"unknown host command: {action}\n")
        sink.write(usage)
        return 2

    engine = "lemoncode"
    as_json = False
    yes = False
    source: Path | None = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--engine":
            index += 1
            if index >= len(arguments):
                sink.write("host --engine requires a value\n")
                return 2
            engine = arguments[index].strip().lower()
        elif argument.startswith("--engine="):
            engine = argument.partition("=")[2].strip().lower()
        elif argument == "--json":
            as_json = True
        elif argument == "--yes":
            yes = True
        elif argument == "--source":
            index += 1
            if index >= len(arguments):
                sink.write("host build --source requires a value\n")
                return 2
            source = Path(arguments[index])
        elif argument.startswith("--source="):
            source = Path(argument.partition("=")[2])
        elif argument in {"-h", "--help", "help"}:
            sink.write(usage)
            return 0
        else:
            sink.write(f"unknown host option: {argument}\n")
            return 2
        index += 1

    if engine not in {"pi", "lemoncode"}:
        sink.write("host --engine must be pi or lemoncode\n")
        return 2
    if engine == "lemoncode":
        if _full_runtime_executable() is not None:
            return _delegate_full_runtime(["host", action, *arguments], sink)
        sink.write("LemonCode host lifecycle requires the managed/full CLI in this build; Pi is available directly.\n")
        return 2

    if not _module_available("lemoncrow_client.pi_host"):
        return _delegate_external_runtime(["runtime", "host", *rest], sink)
    try:
        from . import pi_host, pi_integration

        root = _runtime_store_root()
        if action == "status":
            value = pi_host.host_status(root)
            if as_json:
                sink.write(json.dumps(value, sort_keys=True) + "\n")
            else:
                sink.write("Pi\n")
                sink.write(f"  Installed: {value['installed']}\n")
                sink.write(f"  Resolved:  {value['resolved_path'] or 'not installed'}\n")
                sink.write(f"  Policy:    {value['update_policy']}\n")
            return 0
        if action in {"install", "update"}:
            path = pi_host.install_host_release(root)
            settings = pi_integration.install()
            sink.write(f"Pi integration: {settings}\n")
            sink.write(f"Pi host {'installed' if action == 'install' else 'updated'}: {path}\n")
            return 0
        if action == "build":
            if source is None:
                sink.write("host build --engine pi requires --source DIR\n")
                return 2
            path = pi_host.build_host_from_source(root, source)
            pi_integration.install()
            sink.write(f"Pi host built and installed: {path}\n")
            return 0
        if not yes:
            sink.write("host remove --engine pi requires --yes in the thin client\n")
            return 2
        try:
            removed = pi_integration.remove()
        except RuntimeError as exc:
            # Malformed Pi settings must not block removing the host binary.
            sink.write(f"Pi integration left in place: {exc}\n")
            removed = False
        removed = pi_host.remove_host(root) or removed
        sink.write("Pi host removed.\n" if removed else "Pi host was not installed.\n")
        return 0
    except (OSError, RuntimeError) as exc:
        sink.write(f"Pi host error: {exc}\n")
        return 2


def _runtime_index(arguments: Sequence[str], sink: TextIO) -> int:
    if not _module_available("lemoncrow_client.lite.index"):
        return _delegate_external_runtime(["runtime", "index", *arguments], sink)
    repo_root = Path.cwd()
    force = False
    as_json = False
    args = list(arguments)
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in {"-h", "--help", "help"}:
            sink.write(
                f"usage: {CLIENT_NAME} runtime index [--repo-root DIR] [--reindex] [--json]\n"
                "Build or refresh the local open-source Runtime Lite repository index.\n"
            )
            return 0
        if argument == "--json":
            as_json = True
        elif argument == "--reindex":
            force = True
        elif argument in {"--no-stats"}:
            pass
        elif argument == "--repo-root":
            index += 1
            if index >= len(args):
                sink.write("runtime index --repo-root requires a value\n")
                return 2
            repo_root = Path(args[index])
        elif argument.startswith("--repo-root="):
            repo_root = Path(argument.partition("=")[2])
        else:
            sink.write(f"unknown runtime index option: {argument}\n")
            return 2
        index += 1

    try:
        from .lite.index import LiteIndex

        render = None if as_json else _index_progress_line(sys.stderr)
        report = LiteIndex(repo_root.expanduser().resolve()).prepare(force=force, progress=render)
    except (OSError, RuntimeError) as exc:
        sink.write(f"could not prepare Runtime Lite index: {exc}\n")
        return 2
    finally:
        if not as_json and sys.stderr.isatty():
            sys.stderr.write("\r\033[2K")
            sys.stderr.flush()
    payload = report.to_dict()
    if as_json:
        sink.write(json.dumps(payload, sort_keys=True) + "\n")
    else:
        sink.write("Runtime Lite index ready\n")
        sink.write(f"  path:  {payload.get('index_path', '')}\n")
        sink.write(
            f"  files: {report.files_seen:,} seen, {report.files_changed:,} indexed, "
            f"{report.files_deleted:,} deleted in {report.elapsed_s:.1f}s\n"
        )
    return 0


_INDEX_PHASES: Final[dict[str, str]] = {
    "seed": "Preparing",
    "lock": "Waiting for another lc process to finish indexing",
    "scan": "Indexing",
    "prune": "Removing deleted files",
    "commit": "Saving index",
    "done": "Indexed",
}


def _index_progress_line(stream: TextIO) -> ProgressCallback | None:
    """A dependency-free one-line progress renderer, or ``None`` off a terminal."""

    if not stream.isatty():
        return None

    def render(seen: LiteIndexProgress) -> None:
        label = _INDEX_PHASES.get(seen.phase, seen.phase)
        tail = f" · {seen.changed:,} changed" if seen.changed else ""
        tail += f" · {seen.elapsed_s:.0f}s"
        if seen.total:
            counts = f" {seen.done:,}/{seen.total:,} files ({seen.done * 100 // seen.total}%)"
            # The bar takes the rest of the line, whatever the terminal's width.
            columns = shutil.get_terminal_size((80, 24)).columns
            width = max(columns - len(label) - len(counts) - len(tail) - 4, 10)
            filled = width * seen.done // seen.total
            text = f"{label} [{'#' * filled}{'-' * (width - filled)}]{counts}"
        else:
            text = label + "..."
        stream.write(f"\r\033[2K{text}{tail}")
        stream.flush()

    return render


def _module_available(name: str) -> bool:
    try:
        from importlib.util import find_spec

        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _primary_product() -> str:
    """Product that owns this installed ``lc`` executable.

    Separate Review and Runtime wheels contain only their own implementation.
    The legacy combined thin client ships as part of the Runtime distribution,
    so Runtime owns its mutable settings/auth behavior. Review remains available
    through ``lcr`` / ``lc review`` without recreating shared product state.
    """
    review = _module_available("lemoncrow_client.review")
    runtime = _module_available("lemoncrow_client.mcpserver")
    if review and runtime:
        return "runtime"
    if review:
        return "review"
    if runtime:
        return "runtime"
    return "core"


def _orientation() -> str:
    lines = ["LemonCrow", ""]
    if _module_available("lemoncrow_client.review"):
        lines.append("  Review    lcr  /  lc review")
    lite_runtime = _module_available("lemoncrow_client.lite.mcp") or _external_runtime_executable() is not None
    full_runtime = _full_runtime_executable() is not None
    if lite_runtime:
        lines.extend(
            [
                "  Code      lc code              (Hosted LemonCrow agent)",
                "  Try       lc with claude  /  codex  /  opencode",
            ]
        )
    if full_runtime:
        lines.extend(["  Run       lc run", "  Sessions  lc session"])
    if _module_available("lemoncrow_client.mcpserver") or _external_runtime_executable() is not None:
        lines.append("  Runtime   lc rt")
    lines.extend(["", "  Status    lc status", "  Help      lc --help", ""])
    return "\n".join(lines)


def review_entrypoint() -> int:
    # Permanent lcr entrypoint: exactly the hosted Review command surface.
    from .review import review_main

    return review_main(sys.argv[1:], sink=sys.stdout)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    sink = stdout if stdout is not None else sys.stdout
    if not arguments:
        sink.write(_orientation())
        return 0
    if arguments[0] in {"-h", "--help", "help"}:
        sink.write(_USAGE)
        if os.environ.get("LEMONCROW_SHOW_ALL") == "1":
            sink.write("\ninternal commands:\n  lc dev ...    isolated checkout development profiles\n")
        return 0
    verb, rest = arguments[0], arguments[1:]
    if verb == "dev":
        from .development import dev_main

        return dev_main(rest, sink)
    if verb == "--version":
        sink.write(f"{CLIENT_NAME} {CLIENT_VERSION}\n")
        return 0
    if verb == "with":
        return _with_host(rest, sink)
    if verb == "code":
        return _code(rest, sink)
    if verb in _PROMOTED_RUNTIME_COMMANDS:
        return _delegate_full_runtime([verb, *rest], sink)
    if verb in {"runtime", "rt"}:
        return _runtime(rest, sink)
    if verb == "status":
        return _status(rest, sink)
    if verb == "update":
        return _update(rest, sink)
    if verb == "uninstall":
        return _uninstall(rest, sink)
    if verb == "auth":
        return auth_main(rest, sink=sink, product=_primary_product())
    if verb == "review":
        try:
            from .review import review_main
        except ModuleNotFoundError:
            if not _lazy_install_product("review", sink):
                sink.write("LemonCrow Review is not installed. Runtime is available and unaffected.\n")
                sink.write(f"Install it with: {_install_hint('review')}\n")
                return 2
            return _delegate_review_after_install(rest, sink)
        return review_main(rest, sink=sink)
    if verb == "target":
        return _target(rest, sink)
    if verb == "audit":
        return _audit(sink)
    return _unknown_command(verb, sink)


def _unknown_command(verb: str, sink: TextIO) -> int:
    """One line naming the unknown verb, a pointer when we know one, and where help is."""
    if verb.startswith("-"):
        # A root option this client does not take: the full usage is the answer.
        sink.write(_USAGE)
        return 2
    sink.write(f"unknown command: {verb}\n")
    if verb in _RUNTIME_VERB_HINTS:
        sink.write(f"It is a Runtime command: {_RUNTIME_VERB_HINTS[verb]}\n")
    elif verb in _FULL_ENGINE_VERBS:
        sink.write(f"lc {verb} is part of the full LemonCrow CLI in a source checkout (`uv run lc {verb}`).\n")
        if verb == "doctor":
            sink.write("Check this installation with `lc status` and `lc rt status`.\n")
    sink.write("Run `lc --help` for the commands this client supports.\n")
    return 2


_RUNTIME_USAGE: Final[str] = f"""usage:
  {CLIENT_NAME} runtime code [OPTIONS]
  {CLIENT_NAME} runtime index [--repo-root DIR] [--reindex] [--json]
  {CLIENT_NAME} runtime host COMMAND
  {CLIENT_NAME} runtime status [NAME] [--json]
  {CLIENT_NAME} runtime enable NAME [--dry-run] [--hosted]
  {CLIENT_NAME} runtime disable NAME [--dry-run]
  {CLIENT_NAME} runtime lite-mcp [--host NAME]
  {CLIENT_NAME} runtime mcp [--host NAME]
  {CLIENT_NAME} runtime mcp serve|client|service ...
  {CLIENT_NAME} runtime routes

NAME:
  claude | codex | opencode | auto

Runtime integration is explicit. Local Lite is the default and uses no server.
Review never enables or modifies coding-agent hosts.
"""


_UNINSTALL_USAGE: Final[str] = "usage: lc uninstall review|runtime|all [--dry-run]\n"


def _uninstall(rest: Sequence[str], sink: TextIO) -> int:
    arguments = list(rest)
    product = ""
    if arguments and arguments[0] in {"review", "runtime", "all"}:
        product = arguments.pop(0)
    elif arguments and arguments[0] in {"-h", "--help", "help"}:
        sink.write(_UNINSTALL_USAGE)
        return 0
    elif arguments:
        sink.write("uninstall product must be review, runtime, or all\n")
        sink.write(_UNINSTALL_USAGE)
        return 2

    if not product:
        sink.write(_UNINSTALL_USAGE)
        return 2

    state_home = os.environ.get("LEMONCROW_HOME", "").strip()
    state_dir = (
        Path(state_home).expanduser() if state_home else Path(os.environ.get("HOME", str(Path.home()))) / ".lemoncrow"
    )
    script = state_dir / "install" / "scripts" / "uninstall_product.sh"
    if not script.is_file():
        sink.write(f"product uninstaller is missing: {script}\n")
        sink.write("Re-run the LemonCrow installer to repair installation metadata.\n")
        return 2
    completed = subprocess.run(
        ["bash", str(script), product, *arguments], capture_output=sink is not sys.stdout, text=True, check=False
    )
    if sink is not sys.stdout:
        sink.write(completed.stdout or "")
        sink.write(completed.stderr or "")
    return completed.returncode


def _status(rest: Sequence[str], sink: TextIO) -> int:
    if rest not in ([], ["--json"]):
        sink.write("usage: lemoncrow-client status [--json]\n")
        return 2
    as_json = rest == ["--json"]
    review_installed = _module_available("lemoncrow_client.review")
    runtime_client_installed = (
        _module_available("lemoncrow_client.mcpserver") or _external_runtime_executable() is not None
    )

    lite_runtime = _module_available("lemoncrow_client.lite.mcp") or _external_runtime_executable() is not None
    full_runtime = _full_runtime_executable() is not None

    def product_config(product: str, installed: bool) -> dict[str, object]:
        if not installed:
            return {"installed": False, "deployment": None, "authenticated": None}
        try:
            config = load_config(product=product)
            payload: dict[str, object] = {
                "installed": True,
                "deployment": config.install_mode,
                "authenticated": config.authenticated,
            }
            if config.install_mode != "lite":
                payload["endpoint"] = config.url
            return payload
        except ClientError:
            return {"installed": True, "deployment": "unknown", "authenticated": False}

    review = product_config("review", review_installed)
    runtime = product_config("runtime", runtime_client_installed)
    runtime["local_lite"] = lite_runtime
    runtime["managed_cli"] = full_runtime
    if as_json:
        sink.write(json.dumps({"review": review, "runtime": runtime}, sort_keys=True) + "\n")
        return 0

    sink.write(f"LemonCrow {CLIENT_VERSION}\n\n")
    sink.write("Review\n")
    if review_installed:
        sink.write("  Installed      yes\n")
        sink.write(f"  Deployment     {review.get('deployment', 'unknown')}\n")
        if review.get("deployment") == "hosted":
            sink.write(f"  Signed in      {'yes' if review.get('authenticated') else 'no'}\n")
        sink.write("  Open           lcr\n")
    else:
        sink.write("  Installed      no\n")
        sink.write("  Add            lemoncrow.com → Review\n")

    sink.write("\nRuntime\n")
    if runtime_client_installed:
        sink.write("  Installed      yes\n")
        sink.write(f"  Deployment     {runtime.get('deployment', 'unknown')}\n")
        sink.write(f"  Local Lite     {'yes' if lite_runtime else 'no'}\n")
        sink.write(f"  Managed CLI    {'yes' if full_runtime else 'no'}\n")
        if _module_available("lemoncrow_client.host_install"):
            import io

            from .host_install import runtime_status

            host_sink = io.StringIO()
            runtime_status("auto", sink=host_sink)
            for line in host_sink.getvalue().splitlines():
                sink.write(f"  {line}\n")
        sink.write("  Manage         lc rt status\n")
    else:
        sink.write("  Installed      no\n")
        sink.write("  Add            lemoncrow.com → Runtime\n")
    return 0


def _delegate_review_after_install(arguments: Sequence[str], sink: TextIO) -> int:
    # The running interpreter cannot see the freshly installed wheel; re-exec the installed lc.
    installed = _bin_dir() / "lc"
    executable = str(installed) if installed.is_file() and os.access(installed, os.X_OK) else sys.argv[0]
    return subprocess.run([executable, "review", *arguments], check=False).returncode


def _external_runtime_executable() -> Path | None:
    explicit = os.environ.get("LEMONCROW_RUNTIME_CLIENT", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None
    home = os.environ.get("LEMONCROW_HOME", "").strip()
    state_dir = Path(home).expanduser() if home else Path(os.environ.get("HOME", str(Path.home()))) / ".lemoncrow"
    candidate = state_dir / "runtime" / "client" / "bin" / "lemoncrow-client"
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _runtime_not_installed(sink: TextIO) -> int:
    sink.write("LemonCrow Runtime is not installed. Review is available and unaffected.\n")
    sink.write(f"Install it with: {_install_hint('runtime')}\n")
    return 2


_INSTALL_URL: Final[str] = "https://lemoncrow.com/install.sh"


def _install_hint(product: str) -> str:
    return f"curl -fsSL {_INSTALL_URL} | bash -s -- --product {product}"


def _state_dir() -> Path:
    home = os.environ.get("LEMONCROW_HOME", "").strip()
    return Path(home).expanduser() if home else Path(os.environ.get("HOME", str(Path.home()))) / ".lemoncrow"


def _bin_dir() -> Path:
    configured = os.environ.get("LEMONCROW_BIN_DIR", "").strip()
    return Path(configured).expanduser() if configured else _state_dir() / "bin"


def _release_base_url() -> str:
    tag = os.environ.get("LEMONCROW_RELEASE_TAG", "").strip() or "latest"
    return os.environ.get("LEMONCROW_RELEASE_BASE_URL", "").strip().rstrip("/") or (
        "https://github.com/lemoncrow-lab/lemoncrow/releases/"
        + ("latest/download" if tag == "latest" else f"download/{tag}")
    )


def _run_release_installer(args: Sequence[str], sink: TextIO, *, dry_run: bool = False) -> int:
    """Pipe the release ``install.sh`` into bash with ``args``; the shared fetch path."""
    script = (
        f"curl -fLsS --retry 3 --connect-timeout 15 {shlex.quote(_release_base_url() + '/install.sh')}"
        f" | bash -s -- {' '.join(shlex.quote(arg) for arg in args)}"
    )
    if dry_run:
        sink.write(f"{script}\n")
        return 0
    try:
        return subprocess.run(
            ["bash", "-o", "pipefail", "-c", script],
            check=False,
            # A client-triggered install must always use verified release assets,
            # even if the caller's shell has opted into an installer-only bypass.
            env={**os.environ, "LEMONCROW_ALLOW_UNVERIFIED": "0"},
            stdout=sys.stderr if sink is sys.stderr else None,
        ).returncode
    except OSError:
        return 1


def _lazy_install_product(product: str, sink: TextIO) -> bool:
    """Fetch ``product`` once via the public installer; True when it is now present."""
    if os.environ.get("LEMONCROW_NO_LAZY_INSTALL", "") == "1":
        return False
    # Match the deployment of the product already installed: local Review gets
    # Local Lite Runtime and vice versa; a hosted one keeps its server URL.
    installed = "review" if product == "runtime" else "runtime"
    state_dir = _state_dir()
    present = (
        (state_dir / "review" / "client").is_dir()
        if installed == "review"
        else (state_dir / "runtime" / "client" / "bin" / "lemoncrow-client").is_file()
    )
    deployment = _installed_deployment_args(state_dir, installed) if present else _deployment_args(state_dir, installed)
    sink.write(f"Fetching LemonCrow {product.title()} (one time)...\n")
    return _run_release_installer(["--product", product, *deployment], sink) == 0


def _deployment_args(state_dir: Path, product: str) -> list[str]:
    """Installer flags that reproduce ``product``'s recorded deployment.

    ``--local`` for the local Review engine or Local Lite Runtime, and
    ``--cloud --url URL`` for a hosted client, so updates and lazy fetches keep
    a non-default server instead of falling back to the public cloud. The bare
    installer defaults to Local Review, so a hosted install is always explicit.
    """
    engine = (state_dir / "review" / "engine").is_dir()
    values: dict[str, str] = {}
    try:
        for line in (state_dir / product / "env").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    mode = values.get("LEMONCROW_INSTALL_MODE", "")
    url = values.get("LEMONCROW_URL", "")
    if mode == "hosted":
        return ["--cloud", "--url", url] if url else ["--cloud"]
    if mode in {"lite", "local"} or (product == "review" and engine):
        args = ["--local"]
        if product == "review" and url:
            try:
                port = urlsplit(url).port
            except ValueError:
                port = None
            if port is not None:
                args.extend(["--port", str(port)])
        return args
    return ["--local"] if engine else []


def _installed_deployment_args(state_dir: Path, product: str) -> list[str]:
    """``_deployment_args`` for an installed product; an unrecorded one is hosted.

    The bare installer is Local Review, so a client with no local engine and no
    recorded local mode (a hosted install) must ask for hosted explicitly.
    """
    return _deployment_args(state_dir, product) or ["--cloud"]


def _update(rest: Sequence[str], sink: TextIO) -> int:
    """Re-run the release installer for each installed split product."""
    arguments = list(rest)
    if arguments in (["-h"], ["--help"], ["help"]):
        sink.write(f"usage: {CLIENT_NAME} update [--dry-run]\n")
        return 0
    if arguments not in ([], ["--dry-run"]):
        sink.write(f"usage: {CLIENT_NAME} update [--dry-run]\n")
        return 2
    dry_run = bool(arguments)
    state_dir = _state_dir()
    plan: list[list[str]] = []
    if (state_dir / "review" / "client").is_dir():
        plan.append(["--product", "review", *_installed_deployment_args(state_dir, "review")])
    runtime_client = state_dir / "runtime" / "client" / "bin" / "lemoncrow-client"
    if runtime_client.is_file():
        plan.append(["--product", "runtime", *_installed_deployment_args(state_dir, "runtime")])
    if not runtime_client.is_file() and (state_dir / "uv-tools" / "lemoncrow").exists():
        # Migrate legacy Runtime even when Review is already split. Keep the old
        # environment available for managed commands and rollback.
        product = "review" if (state_dir / "review" / "client").is_dir() else "runtime"
        deployment = _deployment_args(state_dir, product) or ["--local"]
        plan.append(["--product", "runtime", *deployment])
    if not plan:
        sink.write("No LemonCrow Review or Runtime client is installed.\n")
        sink.write(f"Install one with: {_install_hint('review')}\n")
        return 2
    status = 0
    for args in plan:
        product = args[1]
        sink.write(f"Updating LemonCrow {product.title()}...\n")
        code = _run_release_installer(args, sink, dry_run=dry_run)
        if code != 0:
            sink.write(f"LemonCrow {product.title()} update failed (exit {code}).\n")
            status = code
    return status


def _delegate_external_runtime(arguments: Sequence[str], sink: TextIO) -> int:
    executable = _external_runtime_executable()
    installer_sink = sys.stderr if list(arguments[:2]) in (["runtime", "mcp"], ["runtime", "lite-mcp"]) else sink
    if executable is None and _lazy_install_product("runtime", installer_sink):
        executable = _external_runtime_executable()
    if executable is None:
        return _runtime_not_installed(installer_sink)
    if executable.parent.resolve() == Path(sys.executable).parent.resolve():
        installer_sink.write("The installed Runtime package is incomplete; reinstall the Runtime product.\n")
        return 2
    if sink is sys.stdout:
        return subprocess.run([str(executable), *arguments], check=False).returncode
    completed = subprocess.run(
        [str(executable), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    sink.write(completed.stdout)
    if completed.stderr:
        sink.write(completed.stderr)
    return completed.returncode


def _with_host(rest: Sequence[str], sink: TextIO) -> int:
    if not rest or rest[0] in {"-h", "--help", "help"}:
        sink.write("usage: lemoncrow-client with claude|codex|opencode|pi [HOST ARGS...]\n")
        sink.write("Launch your existing coding agent with LemonCrow tools for this process only.\n")
        return 0 if rest else 2
    host, arguments = rest[0], list(rest[1:])
    if host not in {"claude", "codex", "opencode", "pi"}:
        sink.write(f"unsupported host {host!r}; choose claude, codex, opencode, or pi\n")
        return 2
    try:
        from .host_trial import HostTrialError, run_trial_host
    except ModuleNotFoundError:
        return _delegate_external_runtime(["with", *rest], sink)
    try:
        return run_trial_host(host, arguments)
    except HostTrialError as exc:
        sink.write(f"{exc}\n")
        return 2


def serve_runtime_mcp(sink: TextIO, arguments: Sequence[str] = ()) -> int:
    """``runtime mcp``: Runtime Lite in this process unless Runtime targets a hosted server.

    The local server does not execute Runtime tools, so the server-backed thin
    client serves only a hosted target; every other install runs Runtime Lite.
    """
    try:
        lite = load_config(product="runtime").install_mode == "lite"
    except ClientError:
        lite = False  # the thin client reports the misconfiguration
    if lite:
        try:
            from .lite.mcp import serve as serve_lite
        except ModuleNotFoundError:
            return _delegate_external_runtime(["runtime", "mcp", *arguments], sink)
        return serve_lite()
    try:
        from .mcpserver import serve
    except ModuleNotFoundError:
        return _delegate_external_runtime(["runtime", "mcp", *arguments], sink)
    return serve()


def _runtime(rest: Sequence[str], sink: TextIO) -> int:
    if not rest or rest[0] in {"-h", "--help", "help"}:
        sink.write(_RUNTIME_USAGE)
        return 0 if rest else 2

    action, arguments = rest[0], list(rest[1:])
    if action == "code":
        return _code(arguments, sink)
    if action == "index":
        return _runtime_index(arguments, sink)
    if action == "host":
        return _host(arguments, sink)
    if action in _PROMOTED_RUNTIME_COMMANDS or action == "savings":
        return _delegate_full_runtime(["runtime", action, *arguments], sink)
    if action in _FULL_ENGINE_RUNTIME_COMMANDS:
        if action == "telemetry" and _full_runtime_executable() is None:
            sink.write('This client sends no product telemetry; `lc audit` reports "product_telemetry": false.\n')
        return _delegate_full_runtime(["runtime", action, *arguments], sink)
    if action == "mcp" and arguments and arguments[0] in _REMOTE_MCP_COMMANDS:
        # Opt-in remote MCP (listener, tunnel, user services) ships only with Runtime.
        try:
            from .remote.cli import remote_main
        except ModuleNotFoundError:
            return _delegate_external_runtime(["runtime", "mcp", *arguments], sink)
        return remote_main(arguments, sink)
    if action in {"mcp", "lite-mcp"}:
        lite = action == "lite-mcp"
        if arguments in (["-h"], ["--help"], ["help"]):
            sink.write(f"usage: {CLIENT_NAME} runtime {action} [--host NAME]\n")
            if lite:
                sink.write("Run the local-only Runtime Lite MCP stdio process.\n")
            else:
                sink.write("Run one short-lived Runtime MCP stdio process for the configured target.\n")
            return 0
        host = ""
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument == "--host":
                index += 1
                if index >= len(arguments):
                    sink.write(f"runtime {action} --host requires a value\n")
                    return 2
                host = arguments[index].strip()
            elif argument.startswith("--host="):
                host = argument.partition("=")[2].strip()
            else:
                sink.write(f"unknown runtime {action} option: {argument}\n")
                return 2
            index += 1
        if host:
            os.environ["LEMONCROW_AGENT"] = host
        if lite:
            try:
                from .lite.mcp import serve as serve_lite
            except ModuleNotFoundError:
                return _delegate_external_runtime(["runtime", "lite-mcp", *arguments], sink)
            return serve_lite()
        return serve_runtime_mcp(sink, arguments)
    if action == "routes":
        if arguments:
            sink.write("runtime routes accepts no arguments\n")
            return 2
        return _routes(sink)

    try:
        from .host_install import SUPPORTED_AGENTS, register_agents, runtime_status, unregister_agents
    except ModuleNotFoundError:
        return _delegate_external_runtime(["runtime", *rest], sink)

    if action == "status":
        as_json = False
        hosts: list[str] = []
        for argument in arguments:
            if argument == "--json":
                as_json = True
            else:
                hosts.append(argument)
        if len(hosts) > 1:
            sink.write("runtime status accepts at most one host name\n")
            return 2
        return runtime_status(hosts[0] if hosts else "auto", sink=sink, as_json=as_json)
    if action not in {"enable", "disable"}:
        sink.write(f"unknown runtime command: {action}\n")
        sink.write(_RUNTIME_USAGE)
        return 2
    if not arguments:
        sink.write(f"runtime {action} requires an explicit host name (or 'auto')\n")
        return 2
    agent = arguments.pop(0)
    dry_run = False
    hosted = False
    for argument in arguments:
        if argument == "--dry-run":
            dry_run = True
        elif action == "enable" and argument == "--hosted":
            hosted = True
        else:
            sink.write(f"unknown runtime {action} option: {argument}\n")
            return 2
    if agent not in {*SUPPORTED_AGENTS, "auto"}:
        sink.write(f"unsupported agent {agent!r}; choose auto, claude, codex, or opencode\n")
        return 2
    if action == "enable":
        return register_agents(agent, dry_run=dry_run, sink=sink, hosted=hosted)
    return unregister_agents(agent, dry_run=dry_run, sink=sink)


def _target(rest: Sequence[str], sink: TextIO) -> int:
    """Show or persist the endpoint in the installed product's owned state.

    The legacy shared ``~/.lemoncrow/env`` remains a compatibility read only;
    current clients write ``review/env`` or ``runtime/env`` exclusively.
    """
    product = _primary_product()
    if product == "core":
        sink.write("No Review or Runtime product capability is installed.\n")
        return 2
    try:
        config = load_config(product=product)
    except ClientError as exc:
        sink.write(f"{exc.message}\n")
        return 2
    if not rest:
        sink.write(f"{config.url}\n")
        return 0
    url = rest[0].strip()
    target = product_settings_path(config.state_dir, product)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        line
        for line in (target.read_text(encoding="utf-8").splitlines() if target.is_file() else [])
        if not line.startswith("LEMONCROW_URL=")
    ]
    lines.append(f"LEMONCROW_URL={url}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    sink.write(f"wrote {product} LEMONCROW_URL={url} to {target}\n")
    return 0


def _audit(sink: TextIO) -> int:
    """Resolve and print the claims an enterprise review checks."""
    try:
        config = load_config(product=_primary_product())
    except ClientError as exc:
        sink.write(f"configuration error: {exc.message}\n")
        return 2
    routes: Any
    try:
        from .routing import ROUTES
    except ModuleNotFoundError:
        routes = {}
        runtime_installed = _external_runtime_executable() is not None
    else:
        routes = ROUTES
        runtime_installed = True
    review_installed = _module_available("lemoncrow_client.review")
    payload: dict[str, Any] = {
        "client": {"name": CLIENT_NAME, "version": CLIENT_VERSION, "protocol": PROTOCOL_VERSION},
        "dependencies": _declared_dependencies(),
        "endpoint": config.url,
        "endpoint_origin": "{}://{}:{}".format(*config.endpoint_origin),
        "install_mode": config.install_mode,
        "token_source": config.token_source or ("LEMONCROW_TOKEN" if config.token else "none"),
        "refresh_token_source": config.refresh_token_source or "none",
        "state_dir": str(config.state_dir),
        "worktree": str(config.repo_root),
        "capabilities_offered": sorted(CLIENT_CAPABILITIES),
        "products": {"core": True, "review": review_installed, "runtime": runtime_installed},
        "local_fs_offered": config.offer_local_fs,
        "guarantees": {
            "binds_no_port": True,
            "host_registration_uses_native_cli": True,
            "single_lemoncrow_process_per_host_session": True,
            "separate_session_start_process": False,
            "spawns_mcp_child_server": False,
            "spawns_no_background_process": False,  # Explicit lcr device serve has a bounded hint observer.
            "installs_no_unit": False,  # Only explicit lcr service install can write a user unit.
            "review_service_opt_in_only": True,
            "review_service_has_no_local_listener": True,
            "review_service_runs_no_agent_jobs": True,
            "downloads_nothing": False,
            "downloads_no_binary_or_model": True,
            "public_page_egress": "client-side web_fetch only",
            "self_update": False,
            "product_telemetry": False,
            "writes_outside_repo_and_state_dir": False,
        },
        "tools": {
            "client": sorted(name for name, route in routes.items() if route.local),
            "server": sorted(name for name, route in routes.items() if route.remote),
            "sync": sorted(name for name, route in routes.items() if not route.local and not route.remote),
        },
    }
    sink.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


def _routes(sink: TextIO) -> int:
    try:
        from .routing import ROUTES
    except ModuleNotFoundError:
        return _delegate_external_runtime(["runtime", "routes"], sink)
    width = max(len(name) for name in ROUTES)
    for name in sorted(ROUTES):
        route = ROUTES[name]
        sink.write(f"{name:<{width}}  {route.site.value:<26}  {route.reason}\n")
    return 0


def _declared_dependencies() -> list[str]:
    """The installed distribution's ``Requires-Dist``. Expected: none."""
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - stdlib since 3.8
        return []
    try:
        requires = metadata.requires("lemoncrow-client")
    except metadata.PackageNotFoundError:
        # Running from a source checkout rather than an installed wheel.
        return _dependencies_from_pyproject()
    return [entry for entry in (requires or []) if "extra ==" not in entry]


def _dependencies_from_pyproject() -> list[str]:
    candidate = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not candidate.is_file():
        return []
    import tomllib

    with candidate.open("rb") as handle:
        parsed = tomllib.load(handle)
    raw = parsed.get("project", {}).get("dependencies", [])
    return [str(entry) for entry in raw]
