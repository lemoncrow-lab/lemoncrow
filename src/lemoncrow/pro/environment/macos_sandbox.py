"""Native macOS isolation for revision-pinned builds and local services."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from lemoncrow.pro.environment.host_tools import host_executable

# The PATH inside the sandbox after any named executables' own directories.
_SANDBOX_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
_HOMEBREW_PREFIXES = (Path("/opt/homebrew"), Path("/usr/local"))


def _tool_root(tool: Path) -> Path:
    for parent in tool.parents:
        if parent.name in {"bin", "lib"}:
            return parent.parent
    return tool.parent


def _keg_tool_root(tool: Path, user_home: Path) -> Path | None:
    """``_tool_root`` of a resolved tool, only when it is a Homebrew keg directory or inside one.

    ``<prefix>/Cellar/<formula>/<version>`` is the shallowest root ever returned: a real file
    in ``<prefix>/bin`` would otherwise root at the prefix itself and open the whole of it.
    """

    root = _tool_root(tool)
    for prefix in _HOMEBREW_PREFIXES:
        cellar = prefix / "Cellar"
        if cellar in root.parents and len(root.relative_to(cellar).parts) >= 2 and not _overlaps_home(root, user_home):
            return root
    return None


def _path_tool_roots(names: Sequence[str], path_dirs: Sequence[str], user_home: Path) -> set[Path]:
    """Keg roots of the first ``names`` match on ``path_dirs``, the lookup the sandboxed shell makes."""

    roots: set[Path] = set()
    for name in names:
        found = next(
            (
                candidate
                for directory in path_dirs
                if (candidate := Path(directory) / name).is_file() and os.access(candidate, os.X_OK)
            ),
            None,
        )
        if found and (root := _keg_tool_root(found.resolve(), user_home)):
            roots.add(root)
    return roots


def _overlaps_home(path: Path, user_home: Path) -> bool:
    return path == user_home or user_home in path.parents or path in user_home.parents


def sandbox_command(
    command: Sequence[str],
    *,
    workspace: Path,
    cwd: Path,
    executables: Sequence[str] = (),
    repo_executables: Sequence[str] = (),
    path_executables: Sequence[str] = (),
    read_roots: Sequence[Path] = (),
    writable_workspace: bool = False,
    writable_roots: Sequence[Path] = (),
    loopback_port: int | None = None,
    env: Mapping[str, str] | None = None,
    browser: bool = False,
) -> list[str]:
    """Deny host file access, writes and networking except explicit grants.

    ``executables`` are LemonCrow's own fixed toolchain names. ``repo_executables``
    are names the reviewed configuration chose (a runtime's ``command[0]``): they
    grant their toolchain root only when it lies outside the user's home, so a
    repo naming ``docker`` cannot open ``~/.docker`` (registry auths) to the run.
    ``path_executables`` are names the sandboxed shell itself looks up on the sandbox PATH
    (an interpreter): the first match grants read access to its Homebrew keg directory only,
    never the Cellar-wide grant ``executables`` get, and nothing outside a Homebrew Cellar.
    ``writable_roots`` are run-private directories outside the workspace (a check's fresh report
    directory): read and write, nothing else about them is opened.
    """

    sandbox = host_executable("sandbox-exec")
    if not sandbox:
        raise RuntimeError("review execution requires sandbox-exec on macOS")
    workspace = workspace.resolve()
    cwd = cwd.resolve()
    if cwd != workspace and workspace not in cwd.parents:
        raise ValueError("sandbox cwd escapes review snapshot")
    home = workspace / ".lemoncrow-sandbox-home"
    home.mkdir(exist_ok=True)
    user_home = Path.home().resolve()
    granted_repo = [
        name
        for name in repo_executables
        if (path := host_executable(name))
        and not _overlaps_home(Path(path).absolute(), user_home)
        and not _overlaps_home(_tool_root(Path(path).resolve()), user_home)
    ]
    executables = (*executables, *granted_repo)
    path_dirs = list(
        dict.fromkeys(
            [
                *(str(Path(path).absolute().parent) for name in executables if (path := host_executable(name))),
                *_SANDBOX_PATH,
            ]
        )
    )
    tools = [Path(path).resolve() for name in executables if (path := host_executable(name))]
    roots = {
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library/Apple"),
        Path("/private/var/db/dyld"),
        Path("/dev"),
        workspace,
        *(root.resolve() for root in read_roots),
        *(root.resolve() for root in writable_roots),
    }
    for name in executables:
        if path := host_executable(name):
            original = Path(path).absolute()
            if original.parent.name == "bin" and (original.parent.parent / "pyvenv.cfg").is_file():
                # A venv interpreter resolves outside its own pyvenv.cfg and site-packages.
                roots.add(original.parent.parent)
    for tool in tools:
        roots.add(_tool_root(tool))
    if any(Path("/opt/homebrew") in tool.parents for tool in tools):
        roots.update((Path("/opt/homebrew/Cellar"), Path("/opt/homebrew/opt")))
    roots.update(_path_tool_roots(path_executables, path_dirs, user_home))
    filters = " ".join(f"(subpath {json.dumps(str(root))})" for root in sorted(roots))
    ancestors = " ".join(f"(literal {json.dumps(str(parent))})" for parent in workspace.parents)
    writable = workspace if writable_workspace else home
    write_filters = " ".join(
        f"(subpath {json.dumps(str(root))})" for root in (writable, *(item.resolve() for item in writable_roots))
    )
    profile = (
        "(version 1)(deny default)"
        "(allow process-exec process-fork)"
        "(allow signal (target same-sandbox))"
        "(allow sysctl-read)"
        "(allow file-read-metadata)"
        f"(allow file-read* {ancestors} {filters})"
        f'(allow file-write* {write_filters} (literal "/dev/null"))'
    )
    if loopback_port is not None:
        port = int(loopback_port)
        if not 1024 <= port <= 65535:
            raise ValueError("sandbox loopback port must be between 1024 and 65535")
        profile += (
            # Services commonly bind INADDR_ANY; only loopback inbound traffic is granted.
            f'(allow network-bind (local ip "*:{port}"))'
            f'(allow network-inbound (local ip "localhost:{port}"))'
            f'(allow network-outbound (remote ip "localhost:{port}"))'
        )
    if browser:
        # Chrome children rendezvous only with this capture's browser PID.
        # These grants belong exclusively to the isolated screenshot process.
        profile += (
            '(allow mach-register mach-lookup (global-name (param "CHROME_SERVICE")))'
            "(allow ipc-posix-shm)"
            '(allow iokit-open (iokit-user-client-class "RootDomainUserClient"))'
            '(allow system-info (info-type "vfs.disk-space"))'
        )
    path_env = ":".join(path_dirs)
    environment = {
        "HOME": str(home),
        "TMPDIR": str(home),
        "PATH": path_env,
        "CI": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENSSL_CONF": "/dev/null",
        "npm_config_userconfig": str(home / "user.npmrc"),
        "npm_config_globalconfig": str(home / "global.npmrc"),
        **(env or {}),
    }
    launcher = 'cd "$1" || exit; shift; exec "$@"'
    if browser:
        launcher = (
            'cd "$1" || exit; shift; sandbox="$1"; shift; '
            'exec "$sandbox" -D "CHROME_SERVICE=org.chromium.Chromium.MachPortRendezvousServer.$$" "$@"'
        )
    return [
        "/usr/bin/env",
        "-i",
        *(f"{key}={value}" for key, value in environment.items()),
        "/bin/sh",
        "-c",
        launcher,
        "lc-review",
        str(cwd),
        sandbox,
        "-p",
        profile,
        *command,
    ]
