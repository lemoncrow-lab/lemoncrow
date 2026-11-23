"""Stage B of the sealed analyses: one stdlib-only implementation, two places it runs.

What a shipped artifact contains (member paths, sizes, short content keys,
credential findings by path, rule and line, the exported API surface) and how
regenerated files compare with what is committed are derived here. The host
runs this module on the bytes a sealed build copied out (``inspection:
"host"``). When those bytes do not fit the report, the sealed driver runs the
same source inside the sandbox after the kill sweep, once no reviewed code is
alive and the seal recorded no interference (``inspection:
"sealed_in_sandbox"``), and reports only the derived, bounded result.

The driver embeds this file through :func:`embedded_source`, so everything here
must stay standard library only, free of f-strings, and must not depend on its
annotations, docstrings or ``__all__`` (all three are stripped for the sandbox).
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import io
import json
import re
import tarfile
import zipfile
import zlib
from collections.abc import Callable, Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any, Final

_K: Final[str] = "KEY"
_CONTENT_RULES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("aws-access-key-id", r"\b(?:A" + "KIA|A" + r"SIA)[0-9A-Z]{16}\b"),
        ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
        ("github-fine-grained-token", r"\bgithub_" + r"pat_[A-Za-z0-9_]{22,}"),
        ("private-key", r"-----BEGIN (?:[A-Z]+ )*PRIV" + "ATE " + _K + "-----"),
        ("slack-token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
        ("google-api-key", r"\bAI" + r"za[0-9A-Za-z_-]{35}"),
        ("anthropic-api-key", r"\bsk-" + r"ant-[A-Za-z0-9_-]{20,}"),
        ("openai-api-key", r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}"),
        (
            "credential-assignment",
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|client[_-]?secret|auth[_-]?token|access[_-]?token|passw(?:or)?d)\b"
            r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+_.=-]{12,}",
        ),
    )
)
_PATH_RULES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (name, re.compile(pattern, re.I))
    for name, pattern in (
        ("dotenv-file", r"(?:^|/)\.env(?:\.(?!example$|sample$|template$|dist$)[^/]+)?$"),
        ("private-key-file", r"(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)$|\.(?:pem|key|p12|pfx|jks|keystore)$"),
        (
            "credentials-file",
            r"(?:^|/)(?:credentials(?:\.json)?|\.npmrc|\.pypirc|\.netrc|\.git-credentials|service[-_]?account[^/]*\.json)$",
        ),
        ("terraform-state", r"\.tfstate(?:\.backup)?$"),
    )
)
_JS_EXPORTS: Final[tuple[str, ...]] = (
    r"\bexport\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    r"\bmodule\.exports\.([A-Za-z_$][\w$]*)\s*=",
    r"\bexports\.([A-Za-z_$][\w$]*)\s*=",
)
#: Members listed per inspection (split across its artifacts).
_MAX_MEMBERS: Final[int] = 1500
#: A member is read for inspection up to this size; larger members are listed only.
_MAX_MEMBER_BYTES: Final[int] = 4 << 20
#: Content rules scan members up to this size (path rules apply to every member).
_MAX_SCAN_BYTES: Final[int] = 2 << 20
#: Decompressed bytes read per artifact; past it the artifact is incomplete (UNKNOWN, never PASS).
_MAX_UNPACKED_BYTES: Final[int] = 64 << 20
#: Fewest members a listing keeps when it is shrunk to fit the report.
_MIN_LISTED: Final[int] = 50
#: The committed digest of an output that is not a regular file.
NOT_REGULAR: Final[str] = "not-a-regular-file"
_MAX_DIFFS: Final[int] = 50


def scan_member(path: str, data: bytes | None) -> list[dict[str, Any]]:
    """Credential-shaped files and content of one member: the rule and the line, never the value."""

    hits: list[dict[str, Any]] = [{"rule": name, "line": 0} for name, pattern in _PATH_RULES if pattern.search(path)]
    if data is not None and len(data) <= _MAX_SCAN_BYTES and b"\0" not in data[:4096]:
        body = data.decode("utf-8", "replace")
        for name, pattern in _CONTENT_RULES:
            match = pattern.search(body)
            if match:
                hits.append({"rule": name, "line": body.count("\n", 0, match.start()) + 1})
    return hits


def artifact_kind(name: str, builder: str) -> str:
    base = PurePosixPath(name).name
    if base.endswith(".whl"):
        return "wheel"
    if builder == "python" and base.endswith((".tar.gz", ".zip")):
        return "sdist"
    if builder == "npm" and base.endswith(".tgz"):
        return "npm"
    if builder == "container":
        return "image"
    return re.sub(r"\d+", "N", base)


def _member_path(kind: str, name: str) -> str:
    parts = [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]
    if kind in ("sdist", "npm") and len(parts) > 1:
        parts = parts[1:]
    if kind == "wheel" and parts:
        parts[0] = re.sub(r"-[^-]+\.(dist-info|data)$", r".\1", parts[0])
    return "/".join(parts)


def _python_api(data: bytes) -> list[str] | None:
    if len(data) > 1 << 20:
        return None
    try:
        tree = ast.parse(data.decode("utf-8", "replace"))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    for node in tree.body:
        targets: list[ast.expr] = (
            list(node.targets)
            if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for target in targets:
            value = getattr(node, "value", None)
            if isinstance(target, ast.Name) and target.id == "__all__" and isinstance(value, (ast.List, ast.Tuple)):
                return sorted(
                    {
                        item.value
                        for item in value.elts
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    }
                )[:200]
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return sorted(name for name in names if not name.startswith("_"))[:200]


def _gunzip(data: bytes, cap: int) -> tuple[bytes, bool]:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decompressor.decompress(data, cap)
    except zlib.error:
        return b"", True
    return out, bool(decompressor.unconsumed_tail) or len(out) >= cap


def _archive_members(name: str, data: bytes) -> tuple[list[tuple[str, int, bytes | None]], str]:
    """``(member, size, bytes or None)`` of one artifact, and why the listing is incomplete (or ``""``)."""

    rows: list[tuple[str, int, bytes | None]] = []
    left = _MAX_UNPACKED_BYTES
    if name.endswith((".whl", ".zip", ".jar")) and zipfile.is_zipfile(io.BytesIO(data)):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    body: bytes | None = None
                    cap = min(_MAX_MEMBER_BYTES, left)
                    if info.file_size <= cap:
                        with archive.open(info) as handle:
                            body = handle.read(cap + 1)
                        if len(body) > cap:
                            return rows, info.filename + " unpacks past its declared size"
                        left -= len(body)
                    elif left <= 0:
                        return rows, "unpacked contents exceed the inspection bound"
                    rows.append((info.filename, info.file_size, body))
        except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
            return rows, "the archive is unreadable: " + type(exc).__name__
        return rows, ""
    raw, truncated = (data, False) if data[:2] != b"\x1f\x8b" else _gunzip(data, _MAX_UNPACKED_BYTES)
    is_tar = len(raw) > 262 and raw[257:262] == b"ustar"
    if not is_tar:
        if data[:2] == b"\x1f\x8b" or name.endswith((".tgz", ".tar", ".tar.gz")):
            return rows, "the archive is not a readable tar"
        return [(PurePosixPath(name).name, len(data), data if len(data) <= _MAX_MEMBER_BYTES else None)], ""
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member) if member.size <= _MAX_MEMBER_BYTES else None
                rows.append((member.name, member.size, extracted.read(_MAX_MEMBER_BYTES) if extracted else None))
    except (tarfile.TarError, OSError, EOFError) as exc:
        return rows, (
            "unpacked contents exceed the inspection bound"
            if truncated
            else ("the archive is unreadable: " + str(exc))[:200]
        )
    return rows, "unpacked contents exceed the inspection bound" if truncated else ""


def inspect_artifact(
    name: str, data: bytes, kind: str, cap: int
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Members, credential findings and exported API of one artifact."""

    listed, incomplete = _archive_members(name, data)
    members: list[list[Any]] = []
    secrets: list[dict[str, Any]] = []
    api: dict[str, Any] = {}
    texts: dict[str, str] = {}
    total = unscanned = 0
    for raw_name, size, body in listed:
        member = _member_path(kind, raw_name)
        total += size
        if body is None or len(body) > _MAX_SCAN_BYTES:
            unscanned += 1
        members.append([member, size, hashlib.sha256(body).hexdigest()[:12] if body is not None else ""])
        for hit in scan_member(member, body):
            if len(secrets) < 200:
                secrets.append({"artifact": kind, "path": member, "rule": hit["rule"], "line": hit["line"]})
        if body is not None and kind == "wheel" and member.endswith(".py") and ".dist-info/" not in member:
            module = member[:-3].replace("/", ".")
            module = module[:-9] if module.endswith(".__init__") else module
            parts = module.split(".")
            if len(api) < 300 and not any(part.startswith("_") or part in ("tests", "test") for part in parts):
                names = _python_api(body)
                if names is not None:
                    api[module] = names
        if (
            body is not None
            and kind == "npm"
            and (member == "package.json" or member.endswith((".js", ".mjs", ".cjs", ".d.ts")))
        ):
            texts[member] = body[: 1 << 20].decode("utf-8", "replace")
    members.sort()
    count = len(members)
    row: dict[str, Any] = {
        "kind": kind,
        "file": PurePosixPath(name).name,
        "bytes": len(data),
        "unpacked": total,
        "count": count,
        "members": members[:cap],
        "truncated": count > cap,
        "unscanned": unscanned,
    }
    if count > cap:
        row["last"] = members[cap - 1][0]
    if incomplete:
        row["incomplete"] = incomplete
    if kind == "npm" and "package.json" in texts:
        try:
            manifest = json.loads(texts["package.json"])
        except ValueError:
            manifest = {}
        manifest = manifest if isinstance(manifest, dict) else {}
        surface = {
            key: manifest.get(key)
            for key in ("main", "module", "types", "typings", "exports", "bin")
            if key in manifest
        }
        exported: set[str] = set()
        for key in ("main", "module", "types", "typings"):
            entry = manifest.get(key)
            body_text = texts.get(_member_path("wheel", entry)) if isinstance(entry, str) else None
            if not body_text:
                continue
            for pattern in _JS_EXPORTS:
                exported.update(re.findall(pattern, body_text))
            for group in re.findall(r"\bexport\s*\{([^}]*)\}", body_text):
                exported.update(part.split(" as ")[-1].strip() for part in group.split(",") if part.strip())
        api = {"manifest": json.dumps(surface, sort_keys=True, default=str)[:4000], "exports": sorted(exported)[:500]}
    return row, secrets, api


def inspect_files(files: Iterable[tuple[str, bytes]], builder: str, count: int) -> dict[str, Any]:
    """Stage B over *count* artifacts (name, bytes): their rows, credential findings and API surfaces."""

    artifacts: list[dict[str, Any]] = []
    secrets: list[dict[str, Any]] = []
    surfaces: dict[str, Any] = {}
    cap = max(_MIN_LISTED, _MAX_MEMBERS // max(1, count))
    for name, data in files:
        kind = artifact_kind(name, builder)
        row, found, surface = inspect_artifact(name, data, kind, cap)
        artifacts.append(row)
        secrets += found[: 200 - len(secrets)]
        if surface:
            surfaces[kind] = surface
    return {"artifacts": artifacts, "secrets": secrets, "api": surfaces}


def inspect_container_archive(data: bytes) -> dict[str, Any]:
    """Stage B over one built image's root filesystem archive."""

    return inspect_files([("rootfs.tar", data)], "container", 1)


def _json_size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


def fit_inspection(result: dict[str, Any], budget: int) -> None:
    """Shrink member listings (each keeps its bound) until *result* fits *budget* JSON characters.

    A listing never drops below ``_MIN_LISTED`` members; when the result still
    does not fit, every artifact is marked incomplete (UNKNOWN, never PASS).
    """

    rows = [row for row in result.get("artifacts") or () if isinstance(row, dict)]
    while _json_size(result) > budget:
        longest = max(rows, key=lambda row: len(row["members"]), default=None)
        if longest is None or len(longest["members"]) <= _MIN_LISTED:
            break
        cap = len(longest["members"]) // 2
        longest["members"] = longest["members"][:cap]
        longest["last"] = longest["members"][-1][0]
        longest["truncated"] = True
    if _json_size(result) > budget:
        result["api"] = {}
        for row in rows:
            row["incomplete"] = "the inspection result is larger than the analysis report allows"


def compare_regenerated(
    before: Mapping[str, str],
    files: Mapping[str, bytes],
    omitted: Mapping[str, str],
    committed_text: Callable[[str], str | None],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Regenerated bytes against the committed digests: one row per output, plus bounded diffs."""

    rows: list[dict[str, Any]] = []
    heavy: dict[str, str] = {}
    for path in sorted(set(before) | set(files) | set(omitted))[:limit]:
        old = before.get(path)
        new = hashlib.sha256(files[path]).hexdigest() if path in files else None
        if path in omitted or old == NOT_REGULAR:
            state = "unverifiable"
        elif old == new:
            state = "unchanged"
        elif old is None:
            state = "uncommitted"
        elif new is None:
            state = "orphaned"
        else:
            state = "differs"
        row: dict[str, Any] = {
            "path": path,
            "committed": (old or "")[:16],
            "regenerated": (new or "")[:16],
            "state": state,
        }
        if state == "unverifiable":
            row["why"] = omitted.get(path) or "the committed file is not a regular file"
        if state == "differs" and len(heavy) < _MAX_DIFFS and b"\0" not in files[path][:8192]:
            committed = committed_text(path)
            if committed is not None:
                regenerated = files[path][: 1 << 20].decode("utf-8", "replace")
                lines = list(
                    difflib.unified_diff(
                        committed.splitlines(True),
                        regenerated.splitlines(True),
                        "committed/" + path,
                        "regenerated/" + path,
                        n=2,
                    )
                )
                row["changed_lines"] = sum(1 for line in lines if line[:1] in "+-" and line[:3] not in ("+++", "---"))
                heavy["diff:" + path] = "".join(lines)[:60000]
        rows.append(row)
    return rows, heavy


__all__ = [
    "NOT_REGULAR",
    "artifact_kind",
    "compare_regenerated",
    "fit_inspection",
    "inspect_artifact",
    "inspect_files",
    "scan_member",
]
