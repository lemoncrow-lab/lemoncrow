"""Dump human-readable conversation text from .flow files.

Usage: uv run --project benchmarks python -m benchmarks.flowlib.dump <path>

Outputs <file>.flow_dump.txt alongside each .flow file.
Only shows the text/tool content - skips all raw request/response payloads.

The transcript is dumped IN FULL (no truncation) so dumps are usable for
offline analysis (savings accounting, context reconstruction), not just
skimming. Secrets (API keys, bearer tokens, credential assignments) are
scrubbed from every emitted line; nothing else is altered.
"""

import base64
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from mitmproxy.exceptions import FlowReadException
from mitmproxy.io import FlowReader

from benchmarks.flowlib.usage_parser import extract_usage

# Sensitive-value scrubbing: bare tokens are replaced wholesale; key/value
# assignments keep the key and redact only the value.
_TOKEN_RES = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
]
_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd)"
    r"(\s*[:=]\s*[\"']?)[^\s\"',;]{8,}"
)


def _scrub(text: str) -> str:
    """Strip sensitive values from an emitted line; everything else untouched."""
    for pat in _TOKEN_RES:
        text = pat.sub("<redacted>", text)
    return _KV_RE.sub(r"\1\2<redacted>", text)


def _is_messages_request(url: str) -> bool:
    return "v1/messages" in url or "invoke" in url


def _iter_text_from_sse(raw: bytes) -> Iterator[str]:
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        if not line.startswith("data: "):
            continue
        try:
            chunk = json.loads(line[6:])
        except (json.JSONDecodeError, ValueError):
            continue
        t = chunk.get("type", "")
        if t == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "text_delta":
                yield delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                yield delta.get("partial_json", "")
        elif t == "content_block_start":
            cb = chunk.get("content_block", {})
            if cb.get("type") == "tool_use":
                yield f"[tool_use: {cb['name']}] "


def _iter_text_from_bedrock_stream(raw: bytes) -> Iterator[str]:
    for b64 in re.findall(rb'"bytes":"([A-Za-z0-9+/=]+)"', raw):
        try:
            chunk = json.loads(base64.b64decode(b64))
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            continue
        t = chunk.get("type", "")
        if t == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "text_delta":
                yield delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                yield delta.get("partial_json", "")
        elif t == "content_block_start":
            cb = chunk.get("content_block", {})
            if cb.get("type") == "tool_use":
                yield f"[tool_use: {cb['name']}] "


def extract(path: str, output_file: str) -> None:
    print(f"=== {path} ===")
    interactions = 0
    last_req_hash = None
    with open(path, "rb") as f, open(output_file, "w") as out:
        reader = FlowReader(f)
        try:
            flows = list(reader.stream())
        except FlowReadException as e:
            print(f"  SKIP (not a mitmproxy flow file): {path}")
            print(f"    reason: {e}")
            return
        for flow in flows:
            if not flow.request or not flow.response:
                continue
            url = flow.request.url
            if not _is_messages_request(url):
                continue

            interactions += 1

            # Detect repeated requests
            import hashlib

            req_hash = hashlib.md5(flow.request.content or b"").hexdigest()
            repeat_suffix = " (REPEAT)" if req_hash == last_req_hash else ""
            last_req_hash = req_hash

            out.write(f"\n=== Turn {interactions}{repeat_suffix} ===\n")

            # Extract last user message from request
            try:
                if flow.request.content is None:
                    out.write("[user] (empty request)\n")
                else:
                    req = json.loads(flow.request.content.decode("utf-8", errors="ignore"))
                    msgs = req.get("messages", [])
                    if msgs:
                        last = msgs[-1]
                        role = last.get("role", "user")
                        content = last.get("content", "")
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            parts = []
                            for b in content:
                                if b.get("type") == "text":
                                    parts.append(b.get("text", ""))
                                elif b.get("type") == "tool_result":
                                    inner = b.get("content", "")
                                    if isinstance(inner, str):
                                        parts.append(f"[tool_result] {inner}")
                                    elif isinstance(inner, list):
                                        parts.append(
                                            "[tool_result] "
                                            + " ".join(i.get("text", "") for i in inner if i.get("type") == "text")
                                        )
                            text = "\n".join(parts)
                        else:
                            text = ""
                        # Full text, no truncation -- the dump IS the transcript.
                        out.write(f"[{role}] {_scrub(text)}\n")
            except json.JSONDecodeError:
                pass

            # Decode gzip/br transfer encoding before parsing JSON or SSE.
            flow.response.decode(strict=False)
            resp_raw = flow.response.content
            if resp_raw is None:
                out.write("[assistant] (no response captured)\n")
                continue

            resp_text = ""
            try:
                resp = json.loads(resp_raw.decode("utf-8", errors="ignore"))
                for block in resp.get("content", []):
                    if block.get("type") == "text":
                        resp_text += block.get("text", "")
                    elif block.get("type") == "tool_use":
                        resp_text += f"[tool_use: {block['name']}({json.dumps(block.get('input', {}))})] "
            except json.JSONDecodeError:
                if b"data: " in resp_raw:
                    resp_text = "".join(_iter_text_from_sse(resp_raw))
                else:
                    resp_text = "".join(_iter_text_from_bedrock_stream(resp_raw))

            if resp_text:
                out.write(f"[assistant] {_scrub(resp_text)}\n")
            else:
                out.write(f"[assistant] (empty response, status={flow.response.status_code})\n")
            # Per-request measured token usage -- the ground truth for offline
            # context/cost reconstruction (savings accounting validation).
            usage = extract_usage(flow.response.headers.get("content-type", ""), resp_raw)
            if not usage.is_empty():
                out.write(
                    f"[usage] input={usage.input_tokens} cache_read={usage.cache_read_input_tokens} "
                    f"cache_write={usage.cache_creation_input_tokens} output={usage.output_tokens}\n"
                )

    print(f"  {interactions} turns -> {output_file}")


def process_path(target_path: str) -> None:
    path = Path(target_path)
    if path.is_dir():
        for fp in sorted(path.iterdir()):
            if fp.is_file() and fp.suffix == ".flow":
                extract(str(fp), str(fp.with_suffix(".flow_dump.txt")))
    elif path.exists():
        extract(str(path), str(path.with_suffix(".flow_dump.txt")))
    else:
        print(f"Path not found: {target_path}")


if __name__ == "__main__":
    process_path(sys.argv[1])
