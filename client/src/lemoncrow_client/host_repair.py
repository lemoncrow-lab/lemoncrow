"""Migrate existing Runtime connections after installing a release wheel.

Only plain LC MCP invocations are replaced. Remote servers, development wrappers,
credentials, disabled flags and unrelated configuration are preserved.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .lite.imports import _parse_jsonc

_EXECUTABLES = {"lc", "lemoncrow", "lemoncrow-client", "lemoncrow-runtime-client"}
_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/|[{}\[\]:,]|[^\s{}\[\]:,]+', re.S)


def _invocation(command: Any, args: Any, executable: str) -> tuple[str, list[str]] | None:
    if not isinstance(command, str) or Path(command).name not in _EXECUTABLES:
        return None
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return None
    normalized = list(args)
    if normalized[:1] == ["mcp"]:
        normalized.insert(0, "runtime")
    if normalized[:2] not in (["runtime", "mcp"], ["runtime", "lite-mcp"]):
        return None
    tail = normalized[2:]
    if tail and not (
        (len(tail) == 2 and tail[0] == "--host" and tail[1])
        or (len(tail) == 1 and tail[0].startswith("--host=") and tail[0][7:])
    ):
        return None  # e.g. dev, gateway/service commands or custom wrappers
    return executable, normalized


def _json_repair(text: str, executable: str) -> str:
    # Validate with the existing JSONC reader. Token offsets let us change only
    # command/args values, keeping comments, whitespace and all other settings.
    _parse_jsonc(text)
    tokens = [m for m in _TOKENS.finditer(text) if not m.group().startswith(("//", "/*"))]
    stack: list[int] = []
    objects: dict[int, int] = {}
    fields: list[tuple[int, str, int, int]] = []
    for i, token in enumerate(tokens):
        token_value = token.group()
        if token_value == "{":
            stack.append(i)
        elif token_value == "}":
            objects[stack.pop()] = i
        elif token_value in ('"command"', '"args"') and stack and i + 2 < len(tokens) and tokens[i + 1].group() == ":":
            start = i + 2
            end = start
            if tokens[start].group() == "[":
                depth = 1
                while depth:
                    end += 1
                    depth += (tokens[end].group() == "[") - (tokens[end].group() == "]")
            fields.append((stack[-1], json.loads(token_value), start, end))
    replacements: list[tuple[int, int, str]] = []
    for parent, key, start, end in fields:
        entry = _parse_jsonc(text[tokens[parent].start() : tokens[objects[parent]].end()])
        command = entry.get("command")
        is_array = isinstance(command, list)
        if is_array:
            if not command:
                continue
            invocation = _invocation(command[0], command[1:], executable)
        else:
            invocation = _invocation(command, entry.get("args"), executable)
        if invocation is None:
            continue
        value: Any = [invocation[0], *invocation[1]] if is_array else invocation[0]
        if key == "args":
            if is_array:
                continue
            value = invocation[1]
        original = _parse_jsonc(text[tokens[start].start() : tokens[end].end()])
        if original != value:
            replacements.append((tokens[start].start(), tokens[end].end(), json.dumps(value)))
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    _parse_jsonc(text)
    return text


def _toml_repair(text: str, executable: str) -> str:
    parsed = tomllib.loads(text)
    entries = parsed.get("mcp_servers", {})
    headers = list(re.finditer(r"(?m)^[ \t]*\[[^\n]+\][ \t]*(?:#[^\n]*)?$", text))
    replacements = []
    for i, header in enumerate(headers):
        try:
            table = tomllib.loads(header.group() + "\ncommand = 'probe'\n").get("mcp_servers", {})
        except tomllib.TOMLDecodeError:
            continue
        if len(table) != 1:
            continue
        name = next(iter(table))
        if table[name] != {"command": "probe"}:
            continue
        entry = entries.get(name, {})
        invocation = _invocation(entry.get("command"), entry.get("args"), executable)
        if invocation is None:
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        section = text[header.end() : end]
        match = re.search(r"""(?m)^[ \t]*command[ \t]*=[ \t]*("(?:\\.|[^"\\])*"|'[^']*')""", section)
        if match is None:
            raise ValueError("cannot locate Codex command; run lc rt enable codex")
        if entry.get("command") != invocation[0]:
            replacements.append((header.end() + match.start(1), header.end() + match.end(1), json.dumps(invocation[0])))
            entry["command"] = invocation[0]
        if invocation[1] != entry.get("args"):
            args_match = re.search(
                r"""(?ms)^[ \t]*args[ \t]*=[ \t]*(\[(?:"(?:\\.|[^"\\])*"|'[^']*'|[^\]"'])*\])""", section
            )
            if args_match is None:
                raise ValueError("cannot locate Codex args; run lc rt enable codex")
            replacements.append(
                (header.end() + args_match.start(1), header.end() + args_match.end(1), json.dumps(invocation[1]))
            )
            entry["args"] = invocation[1]
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    if tomllib.loads(text) != parsed:
        raise ValueError("Codex migration would change unrelated settings")
    return text


def _save(path: Path, before: str, after: str) -> None:
    if before == after:
        return
    path = path.resolve()
    if path.read_text(encoding="utf-8") != before:
        raise RuntimeError("configuration changed during migration; retry installation")
    # One bounded rollback copy, never a growing series of credential-bearing backups.
    backup = path.with_name(path.name + ".before-runtime-migration")
    if not backup.exists():
        with backup.open("x", encoding="utf-8") as output:
            os.chmod(backup, 0o600)
            output.write(before)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(after)
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _repair_legacy_launcher(home: Path) -> None:
    from .cli import _bin_dir, _state_dir

    state = _state_dir()
    launcher = home / ".local" / "bin" / "lc"
    legacy = state / "uv-tools" / "lemoncrow" / "bin"
    if not launcher.is_symlink():
        return
    target = launcher.resolve()
    if target.parent != legacy.resolve() or target.name not in _EXECUTABLES:
        return
    # Update only the obsolete public alias, keeping the full environment for
    # managed commands. Future updates keep using the stable product-owned lc.
    stable = _bin_dir() / "lc"
    if stable.is_file() and launcher.absolute() != stable.absolute():
        with tempfile.TemporaryDirectory(prefix=".lc-runtime-", dir=launcher.parent) as directory:
            temporary = Path(directory) / "lc"
            temporary.symlink_to(stable)
            temporary.replace(launcher)


def repair_existing() -> int:
    from . import pi_integration

    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    executable = str(Path(sys.executable).parent / "lemoncrow-runtime-client")
    if not os.access(executable, os.X_OK):
        raise RuntimeError("run migration with the installed Runtime Python")
    paths = [
        home / ".claude.json",
        home / ".cursor" / "mcp.json",
        codex / "config.toml",
        *[xdg / host / name for host in ("opencode", "lemoncode") for name in ("opencode.json", "opencode.jsonc")],
    ]
    status = 0
    try:
        _repair_legacy_launcher(home)
    except OSError as exc:
        print(f"Could not migrate legacy lc launcher: {exc}", file=sys.stderr)
        status = 1
    for path in paths:
        if not path.is_file():
            continue
        try:
            before = path.read_text(encoding="utf-8")
            after = _toml_repair(before, executable) if path.suffix == ".toml" else _json_repair(before, executable)
            _save(path, before, after)
            if before != after:
                print(f"Updated existing LC connection: {path}")
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"Could not migrate {path}: {exc}", file=sys.stderr)
            status = 1
    if pi_integration.enabled():
        try:
            pi_integration.refresh()
            print("Updated existing Pi Runtime extension")
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"Could not migrate Pi Runtime extension: {exc}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(repair_existing())
