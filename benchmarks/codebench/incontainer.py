"""In-container agent runner for Multi-SWE-bench instances (option A).

The agent (Claude Code, optionally + LemonCrow) runs INSIDE each instance's
Docker image -- which carries the real toolchain -- against the repo checked
out at ``base_sha``. The produced git diff is extracted as the agent's
``fix_patch`` and the run is parsed into a run.py ``ArmResult`` so every
existing savings / report / CSV path applies unchanged.

The two arms differ only in the overlay contents + the claude flags:
  baseline -> vanilla Claude Code (default persona, empty MCP)
  lemoncrow -> Claude Code + the LemonCrow plugin (--plugin-dir, --agent lemoncrow:auto)
That is the vanilla-vs-LemonCrow isolation, same model, same task.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol

from benchmarks.codebench.run import (
    CA_CERT,
    REPO_ROOT,
    ArmResult,
    _free_port,
    _lean_plugin_root,
    _parse_claude_result,
    _wait_port,
)


class RunnableInstance(Protocol):
    """Minimal instance shape the runner needs (Multi-SWE-bench or SWE-bench)."""

    instance_id: str
    image: str
    problem_statement: str


ENTRY_SCRIPT = Path(__file__).parent / "incontainer_entry.sh"
# Pre-warmed tiktoken cache bind-mounted into the LemonCrow container. The LemonCrow
# MCP server loads cl100k_base at import (repo_map.budget); without a warmed
# cache it downloads from openaipublic.blob, which dies under the benchmark proxy
# (mitm CA absent from Python's trust store) and crashes the server -> zero
# LemonCrow tools reach the agent. Warmed by _ensure_tiktoken_cache().
TIKTOKEN_CACHE_HOST = Path(__file__).parent / ".tiktoken-cache"
OVERLAY_NAMESPACE = "codebench-overlay"
_DIFF_BEGIN = "<<<CODEBENCH_DIFF_BEGIN>>>"
_DIFF_END = "<<<CODEBENCH_DIFF_END>>>"

# Persona per arm for the "code" capability (mirrors run.ARM_SPECS).
_ARM_AGENT: dict[str, str | None] = {"baseline": None, "lemoncrow": "lemoncrow:auto"}


# Installed into every overlay: Node + the claude CLI on top of the instance image.
_BASELINE_INSTALL = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends curl ca-certificates gnupg git
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y --no-install-recommends nodejs
npm install -g @anthropic-ai/claude-code
npm cache clean --force
rm -rf /var/lib/apt/lists/*
"""

# LemonCrow arm only: install the LemonCrow CLI from the mounted repo (skip mypyc
# for a fast pure-Python build) onto PATH so the plugin's MCP server
# (`lemoncrow mcp --host claude`) resolves exactly as it does on the host.
# Extras go on the path requirement; UV_TOOL_BIN_DIR puts the entrypoints on PATH.
_LEMONCROW_INSTALL = r"""
set -e
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
LEMONCROW_ENABLE_MYPYC=0 UV_TOOL_BIN_DIR=/usr/local/bin /usr/local/bin/uv tool install --force "/opt/lemoncrow[mcp,smart,parsers,rename]"

# Pre-install the ast-grep binary so the codemod MCP tool works at runtime.
# Download NOW (overlay build time) -- the mitmproxy that runs during the actual
# benchmark uses a CA that Python's ssl module does not trust, so any urllib call
# at runtime fails. ast-grep is a compiled Rust CLI; there is no pip wheel for it.
# Version/URL/SHA must stay in sync with:
#   src/lemoncrow/infra/code_intel/astgrep/binaries.py (_MANAGED_VERSION + _MANAGED_ASSETS)
python3 - <<'PYEOF'
import hashlib, io, platform, stat, sys, urllib.request, zipfile
from pathlib import Path
ARCH = {'amd64': 'x86_64', 'x64': 'x86_64', 'arm64': 'aarch64'}.get(
    platform.machine().lower(), platform.machine().lower())
ASSETS = {
    'x86_64': (
        'https://github.com/ast-grep/ast-grep/releases/download/0.42.2/app-x86_64-unknown-linux-gnu.zip',
        '52aef3ed330a5fb1d9f399b83285bfcf47d92401249803f62711573e83cb47ae'),
    'aarch64': (
        'https://github.com/ast-grep/ast-grep/releases/download/0.42.2/app-aarch64-unknown-linux-gnu.zip',
        'a68d7645d49dbd97b423cc8a64f7839fe5541eedf0b4bb4ab79f4ba5d53f0376'),
}
if ARCH not in ASSETS:
    sys.exit(f'no pinned ast-grep asset for arch {ARCH!r}')
url, sha256 = ASSETS[ARCH]
dest = Path('/opt/lemoncrow-astgrep/ast-grep')
dest.parent.mkdir(parents=True, exist_ok=True)
print(f'Downloading ast-grep ({ARCH}) ...', flush=True)
with urllib.request.urlopen(url, timeout=120) as r:
    data = r.read()
if hashlib.sha256(data).hexdigest() != sha256:
    sys.exit('ast-grep download: sha256 mismatch')
with zipfile.ZipFile(io.BytesIO(data)) as z:
    member = next((n for n in z.namelist() if Path(n).name == 'ast-grep'), None)
    if member is None:
        sys.exit('ast-grep binary not found in zip')
    dest.write_bytes(z.read(member))
dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
print(f'ast-grep installed at {dest}', flush=True)
PYEOF
"""


def _run(cmd: list[str], *, timeout: float | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout, check=False)


def _ensure_tiktoken_cache() -> None:
    """Warm the bind-mounted tiktoken cache (idempotent; a hit is a no-op).

    The in-container LemonCrow MCP server loads cl100k_base at import; with this
    cache present it never reaches the network, which would otherwise crash the
    server under the benchmark proxy. Warms with the LemonCrow venv (which carries
    tiktoken) so a fresh clone / CI run can't silently regress.
    """
    if TIKTOKEN_CACHE_HOST.exists() and any(TIKTOKEN_CACHE_HOST.iterdir()):
        return
    TIKTOKEN_CACHE_HOST.mkdir(parents=True, exist_ok=True)
    venv_py = REPO_ROOT / ".venv" / "bin" / "python3"
    py = str(venv_py) if venv_py.exists() else sys.executable
    subprocess.run(
        [py, "-c", "import tiktoken; tiktoken.get_encoding('cl100k_base')"],
        env={**os.environ, "TIKTOKEN_CACHE_DIR": str(TIKTOKEN_CACHE_HOST)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def image_exists(tag: str) -> bool:
    return _run(["docker", "image", "inspect", tag]).returncode == 0


def _safe(base_image: str) -> str:
    import re

    return re.sub(r"[^a-z0-9_.-]+", "_", base_image.lower()).strip("_")


def overlay_tag(base_image: str, *, lc: bool) -> str:
    return f"{OVERLAY_NAMESPACE}/{_safe(base_image)}:{'lemoncrow' if lc else 'baseline'}"


def ensure_base_image(image: str, *, timeout: float = 1800) -> None:
    if image_exists(image):
        return
    proc = _run(["docker", "pull", image], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"docker pull {image} failed: {proc.stderr[-400:]}")


def _install_zoekt_into(builder: str, *, timeout: float = 600) -> None:
    """Copy Zoekt search binaries from the official pinned image into *builder*.

    Runs on the HOST (not inside the container), so no Docker-in-Docker is
    needed.  The four binaries go to ``/usr/local/bin/`` so
    ``discover_zoekt_binary()`` finds them via PATH (installed mode).
    Version is pinned in
    ``src/lemoncrow/infra/code_intel/zoekt/VERSIONS.toml``.
    """
    import tomllib

    versions_path = REPO_ROOT / "src" / "lemoncrow" / "infra" / "code_intel" / "zoekt" / "VERSIONS.toml"
    try:
        image_ref = tomllib.loads(versions_path.read_text())["zoekt"]["image_ref"]
    except Exception as exc:
        print(f"[zoekt] could not read VERSIONS.toml: {exc} -- skipping", flush=True)
        return

    # Pull image (no-op if already cached on the host).
    if _run(["docker", "pull", image_ref], timeout=timeout).returncode != 0:
        print("[zoekt] image pull failed -- zoekt will be unavailable in benchmarks", flush=True)
        return

    # Create a dormant container to copy from (no entrypoint runs).
    tmp = "zoekt-extract-tmp"
    _run(["docker", "rm", "-f", tmp])
    if _run(["docker", "create", "--name", tmp, image_ref]).returncode != 0:
        print("[zoekt] docker create failed -- skipping", flush=True)
        return

    try:
        # Discover binary paths inside the image.
        locate = _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                image_ref,
                "-c",
                "which zoekt zoekt-index zoekt-git-index zoekt-webserver 2>/dev/null",
            ],
            timeout=30,
        )
        bin_paths = [p.strip() for p in locate.stdout.splitlines() if p.strip()]
        if not bin_paths:
            # Fallback: common location in Sourcegraph images.
            bin_paths = [
                "/usr/local/bin/zoekt",
                "/usr/local/bin/zoekt-index",
                "/usr/local/bin/zoekt-git-index",
                "/usr/local/bin/zoekt-webserver",
            ]

        import tempfile

        with tempfile.TemporaryDirectory() as staging:
            staging_path = Path(staging)
            for src in bin_paths:
                name = Path(src).name
                dest = staging_path / name
                cp = _run(["docker", "cp", f"{tmp}:{src}", str(dest)], timeout=30)
                if cp.returncode != 0:
                    print(f"[zoekt] could not copy {src} -- skipping", flush=True)
                    continue
                # Copy from host staging dir into builder container.
                _run(["docker", "cp", str(dest), f"{builder}:/usr/local/bin/{name}"], timeout=30)
                # Ensure executable bit (docker cp preserves mode, but be explicit).
                _run(["docker", "exec", builder, "chmod", "+x", f"/usr/local/bin/{name}"], timeout=10)

        print(f"[zoekt] installed {len(bin_paths)} binaries from {image_ref[:60]}", flush=True)
    finally:
        _run(["docker", "rm", "-f", tmp])


def ensure_overlay(base_image: str, *, lc: bool, build_timeout: float = 3600) -> str:
    """Build (once, then cache) the harness overlay for *base_image*.

    The LemonCrow overlay layers on the baseline overlay (which already carries
    Node + claude), so node/claude install once per base image and the LemonCrow
    build only adds the LemonCrow CLI.
    """
    tag = overlay_tag(base_image, lc=lc)
    if image_exists(tag):
        return tag
    if lc:
        parent = ensure_overlay(base_image, lc=False)
        install = _LEMONCROW_INSTALL
        mounts = ["-v", f"{REPO_ROOT}:/opt/lemoncrow:ro"]
    else:
        ensure_base_image(base_image)
        parent = base_image
        install = _BASELINE_INSTALL
        mounts = []
    builder = f"overlay_build_{_safe(base_image)}_{'lemoncrow' if lc else 'baseline'}"
    _run(["docker", "rm", "-f", builder])
    # --entrypoint sleep overrides whatever ENTRYPOINT the base image sets (most
    # SWE-bench images have none, so "sleep infinity" runs as the CMD -- but
    # SWE-bench Pro's sweap-images set ENTRYPOINT ["/bin/bash"], which turns an
    # unoverridden "sleep infinity" into `bash sleep infinity` -- bash trying to
    # source a script literally named "sleep" -- and the container exits before
    # the install exec ever runs). Forcing the entrypoint makes the builder
    # container's actual command "sleep infinity" regardless of image config.
    start = ["docker", "run", "-d", "--name", builder, "--entrypoint", "sleep", *mounts, parent, "infinity"]
    proc = _run(start)
    if proc.returncode != 0:
        raise RuntimeError(f"overlay container start failed: {proc.stderr[-400:]}")
    try:
        proc = _run(["docker", "exec", builder, "bash", "-lc", install], timeout=build_timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"overlay install failed for {tag}:\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}")
        # LEMONCROW_ZOEKT_MODE defaults to "off" (lexical/FTS5-only, see zoekt_mode()
        # in src/lemoncrow/infra/code_intel/zoekt/binary.py) and the container never
        # sets it otherwise -- should_route() short-circuits on mode=="off" before
        # ever touching the binaries, so installing them is pure dead weight unless
        # a caller explicitly opts in to test the zoekt path.
        if lc and os.environ.get("CODEBENCH_ZOEKT_MODE", "off") != "off":
            _install_zoekt_into(builder)
        if _run(["docker", "commit", builder, tag]).returncode != 0:
            raise RuntimeError(f"docker commit {tag} failed")
    finally:
        _run(["docker", "rm", "-f", builder])
    return tag


def _start_proxy(port: int, flow_path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT / "benchmarks"),
            "mitmdump",
            "-w",
            str(flow_path),
            # Hermetic egress allowlist: block any host that isn't a model-
            # inference endpoint so an agent/subagent can't fetch the gold
            # patch/test from GitHub/PyPI (SWE-bench tasks are public PRs).
            "-s",
            str(REPO_ROOT / "benchmarks" / "codebench" / "egress_guard.py"),
            "--listen-host",
            "0.0.0.0",
            "--listen-port",
            str(port),
            "-q",
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_proxy(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None:
        return
    proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


def _split_output(stdout: str) -> tuple[str, str]:
    """Split container stdout into (claude_json, diff_text)."""
    idx = stdout.find(_DIFF_BEGIN)
    head = stdout if idx == -1 else stdout[:idx]
    diff = ""
    if idx != -1:
        rest = stdout[idx + len(_DIFF_BEGIN) :]
        end = rest.find(_DIFF_END)
        diff = (rest if end == -1 else rest[:end]).strip("\n")
        if diff:
            # git apply requires a newline-terminated patch; the strip above
            # removes the trailing newline and makes the final hunk unparseable
            # ('corrupt patch at line N'), which silently fails every grade.
            diff += "\n"
    brace = head.find("{")
    claude_json = (head[brace:] if brace != -1 else head).strip()
    return claude_json, diff


def _docker_run_cmd(
    instance: RunnableInstance,
    arm: str,
    *,
    overlay: str,
    model: str,
    max_turns: int,
    proxy_port: int,
    prompt_path: Path,
    agent_env: dict[str, str],
) -> list[str]:
    cmd = [
        "docker",
        "run",
        "--rm",
        # Force the run command to be exactly "bash /mnt/run.sh" regardless of the
        # overlay image's own ENTRYPOINT. Most SWE-bench images have none (Cmd is
        # the default /bin/bash), so the trailing args below simply become CMD --
        # but SWE-bench Pro's sweap-images set ENTRYPOINT ["/bin/bash"], which
        # would otherwise turn the trailing args into `bash bash /mnt/run.sh`
        # (outer bash trying to source a script literally named "bash").
        "--entrypoint",
        "bash",
        "--add-host=host.docker.internal:host-gateway",
        "-v",
        f"{ENTRY_SCRIPT}:/mnt/run.sh:ro",
        "-v",
        f"{prompt_path}:/mnt/prompt.txt:ro",
        "-v",
        f"{CA_CERT}:/mnt/mitm.pem:ro",
    ]
    if arm == "lemoncrow":
        cmd += ["-v", f"{_lean_plugin_root(_ARM_AGENT.get('lemoncrow') or 'lemoncrow:solve')}:/mnt/plugin:ro"]
        cmd += ["-v", f"{TIKTOKEN_CACHE_HOST}:/opt/tiktoken-cache:ro"]
        # Account-free: no host auth files are mounted and no device id is
        # forwarded (device minting removed). The runtime is fully unlocked
        # without an account, so containers run with every tool available.
        # See docs/maintenance-mode-transition.md.
        # Overlay the live repo source onto the baked-in (pure-Python) install so
        # tool-behavior changes take effect without rebuilding 12 overlay images.
        cmd += [
            "-v",
            f"{REPO_ROOT}/src/lemoncrow:/root/.local/share/uv/tools/lemoncrow/lib/python3.13/site-packages/lemoncrow:ro",
        ]
        # Semantic bash-output compaction: bind-mount an rtk binary so
        # external_compactors routes pytest/git/linter output through it inside
        # the container (keeps failures + summary, drops passing noise -- the
        # 5-9k test blobs are the largest remaining tool results). The host's
        # dynamically-linked rtk needs a newer glibc than the SWE-bench images
        # ship (measured: GLIBC_2.39 missing), so prefer the old-glibc build
        # produced in a bullseye container (glibc 2.31, runs on every image):
        #   docker run --rm -v $PWD/benchmarks/codebench/.rtk-linux:/out \
        #     rust:1-bullseye cargo install --git https://github.com/rtk-ai/rtk \
        #     --tag v0.43.0 --root /out
        # Fail-open either way: an incompatible binary fails the lazy version
        # probe and the built-in extractor ladder applies as before.
        _rtk_compat = Path(__file__).parent / ".rtk-linux" / "bin" / "rtk"
        rtk = str(_rtk_compat) if _rtk_compat.exists() else shutil.which("rtk")
        if rtk:
            cmd += ["-v", f"{rtk}:/usr/local/bin/rtk:ro"]
    env: dict[str, str] = {
        "IS_SANDBOX": "1",
        "NODE_EXTRA_CA_CERTS": "/mnt/mitm.pem",
        "HTTPS_PROXY": f"http://host.docker.internal:{proxy_port}",
        "HTTP_PROXY": f"http://host.docker.internal:{proxy_port}",
        "CODEBENCH_ARM": arm,
        "CODEBENCH_MODEL": model,
        "CODEBENCH_MAX_TURNS": str(max_turns),
    }
    # SWE-bench images carry the repo at /testbed; pin it so the entry script
    # never picks a stray .git (e.g. under site-packages). Multi-SWE instances
    # leave this unset and the entry script auto-discovers the repo.
    repo_dir = getattr(instance, "repo_dir", None)
    if repo_dir:
        env["CODEBENCH_REPO_DIR"] = str(repo_dir)
    # SWE-bench Pro instances ship a repo-setup command (reset to base_commit +
    # checkout the one gold test file the task's fail_to_pass exercises) that
    # the harness itself re-applies after grading the candidate patch -- so the
    # agent must start from that exact state too, for both arms alike.
    before_repo_set_cmd = getattr(instance, "before_repo_set_cmd", None)
    if before_repo_set_cmd:
        env["CODEBENCH_BEFORE_REPO_SET_CMD"] = str(before_repo_set_cmd)
    agent = _ARM_AGENT.get(arm)
    if arm == "lemoncrow":
        # Per-run persona override (default lemoncrow:auto). Lets a diagnostic run
        # use e.g. lemoncrow:bare without disturbing a concurrent auto run -- the
        # other process doesn't set this env var, so it keeps the default.
        agent = os.environ.get("CODEBENCH_LEMONCROW_AGENT") or agent
    if agent:
        env["CODEBENCH_AGENT"] = agent
    if arm == "lemoncrow":
        # Point tiktoken at the bind-mounted pre-warmed cache so the MCP server
        # never reaches the network at import (see TIKTOKEN_CACHE_HOST).
        env["TIKTOKEN_CACHE_DIR"] = "/opt/tiktoken-cache"
        # Lean tool surface: hide aux tools the autonomous SWE agent never
        # reaches for (verified ~0 uses), shrinking the per-turn schema the model
        # reasons over. callers/callees/usages are already hidden by default
        # (folded into `explore`), so they need not be repeated here. Visible
        # surface after this: read, edit, code_search, bash (verified via
        # `lemoncrow tools list` under this env) -- ~1k tokens of schema total.
        env["LEMONCROW_HIDE_TOOLS"] = "sql,memory,web_fetch"
        # Point at the pre-installed binary so discover_astgrep_binary() finds it
        # immediately via the env-var path (no runtime download attempt through proxy).
        env["LEMONCROW_AST_GREP_BIN"] = "/opt/lemoncrow-astgrep/ast-grep"
        # Edit-verify gate ON by default (tree-sitter parse + scoped mypy): catches
        # mechanical edit errors in-tool instead of via a shell round-trip, which
        # collapses the edit->test->error->re-edit cycle on iteration-bound tasks
        # (measured -33% to -47% cost at equal correctness). Opt out for control
        # runs with CODEBENCH_EDIT_VERIFY=0.
        env["LEMONCROW_EDIT_VERIFY"] = os.environ.get("CODEBENCH_EDIT_VERIFY", "1")
        # Code search runs lexical (symbol FTS + zoekt) by default -- the shipped
        # default (NullEmbedder, FTS-only). The feature-hashing "local" embedder was
        # removed: RETRIEVAL_EVAL measured it at -0.0004 MRR (net zero, flask -0.16)
        # over 2306 pairs at ~3x latency, and it needed numpy. Opt into a real neural
        # backend (ollama/bge) via CODEBENCH_CODE_EMBEDDER.
        _code_embedder = os.environ.get("CODEBENCH_CODE_EMBEDDER", "")
        if _code_embedder:
            env["LEMONCROW_CODE_EMBEDDER"] = _code_embedder
        # Verify-before-done gate ON for every persona. It is the DETERMINISTIC
        # half of correctness: silent on the happy path (a real test ran), and
        # actionable only on the fail/skip case (edited code, no test runner). This
        # replaces a blanket persona "always iterate against tests" rule, which
        # taxed easy tasks; the gate nudges once, only when a test was actually
        # skipped. Override with CODEBENCH_VERIFY_BEFORE_DONE=0.
        env["LEMONCROW_VERIFY_BEFORE_DONE"] = os.environ.get("CODEBENCH_VERIFY_BEFORE_DONE", "1")
        # code_search outline mode ON by default: large sections become L<start>-L<end>
        # pointers (small ones stay inline; include_source keeps a bounded top-2).
        # Flow-capture attribution (reports/benchmark/swe/20260706T065549Z) measured
        # inline sections as a pure ADDER on the losing pairs: +$1.25 context residency
        # per 10 tasks while the agent read its edit targets anyway (sphinx: 14k chars
        # of sections next to an unchanged read volume). Opt out for control runs with
        # CODEBENCH_CODESEARCH_OUTLINE=0.
        env["LEMONCROW_CODESEARCH_OUTLINE"] = os.environ.get("CODEBENCH_CODESEARCH_OUTLINE", "1")
        # Defer mutating edit-hooks (format/organize-imports) + contract-site re-fires
        # to the Stop hook so the formatter can't reflow files mid-session and break
        # the agent's read anchors. Opt-in via CODEBENCH_DEFER_EDIT_HOOKS=1.
        env["LEMONCROW_DEFER_EDIT_HOOKS"] = os.environ.get("CODEBENCH_DEFER_EDIT_HOOKS", "0")
        # Matches the host default (lexical/FTS5-only); overlay only installs the
        # zoekt binaries when this is set to something other than "off" (see the
        # ensure_overlay() gate above), so this must stay in sync with that check.
        env["LEMONCROW_ZOEKT_MODE"] = os.environ.get("CODEBENCH_ZOEKT_MODE", "off")
    env.update(agent_env)
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    # --entrypoint bash (set above) already IS the executable; these are its argv.
    cmd += [overlay, "/mnt/run.sh"]
    return cmd


def _dump_flow_text(flow_path: Path) -> None:
    """Best-effort: write ``<stem>.flow_dump.txt`` next to the .flow capture.

    Produced inline at rep completion so a run is human-inspectable without a
    separate ``make flow-dump`` pass. Never raises -- a dump failure must not
    perturb the run result. ``extract()`` prints its own "=== path ===" / "N
    turns -> ..." progress lines (meant for its standalone CLI use); silence
    those here so they don't interleave with the per-rep result line that
    actually matters during a live run.
    """
    with contextlib.suppress(Exception):
        if not flow_path.exists() or flow_path.stat().st_size == 0:
            return
        from benchmarks.flowlib.dump import extract

        with contextlib.redirect_stdout(io.StringIO()):
            extract(str(flow_path), str(flow_path.with_suffix(".flow_dump.txt")))


def run_in_container(
    instance: RunnableInstance,
    arm: str,
    rep: int,
    *,
    model: str,
    out_dir: Path,
    timeout: int,
    agent_env: dict[str, str] | None = None,
    max_turns: int = 50,
    overlay: str | None = None,
) -> ArmResult:
    """Run one (instance, arm, rep) in its container; return a run.py ArmResult.

    Side effect: writes ``<id>_<arm>_rep<rep>.patch`` (the agent's diff) and
    ``...flow`` (wire capture) under *out_dir*; the grader reads the patch.
    """
    agent_env = agent_env or {}
    if arm == "lemoncrow":
        _ensure_tiktoken_cache()
    overlay = overlay or ensure_overlay(instance.image, lc=(arm == "lemoncrow"))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{instance.instance_id}_{arm}_rep{rep}"
    flow_path = out_dir / f"{stem}.flow"
    patch_path = out_dir / f"{stem}.patch"
    prompt_path = out_dir / f"{stem}.prompt.txt"
    prompt_path.write_text(instance.problem_statement, encoding="utf-8")

    port = _free_port()
    proxy = _start_proxy(port, flow_path)
    started = time.time()
    timed_out = False
    stdout = ""
    stderr = ""
    try:
        if not _wait_port(port):
            raise RuntimeError("mitmdump did not start")
        cmd = _docker_run_cmd(
            instance,
            arm,
            overlay=overlay,
            model=model,
            max_turns=max_turns,
            proxy_port=port,
            prompt_path=prompt_path,
            agent_env=agent_env,
        )
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    finally:
        _stop_proxy(proxy)
    wall_ms = int((time.time() - started) * 1000)

    claude_json, diff = _split_output(stdout)
    patch_path.write_text(diff, encoding="utf-8")
    result = _parse_claude_result(claude_json, flow_path, instance.instance_id, arm, rep)
    if result.duration_ms == 0:
        result.duration_ms = wall_ms
    if result.duration_api_ms == 0:
        result.duration_api_ms = wall_ms
    result.timed_out = timed_out
    if timed_out:
        result.is_error = True
        result.ok = False
        result.result_excerpt = (f"timed out after {timeout}s\n{stderr.strip()}")[:4000]
    elif not result.ok and stderr.strip():
        result.result_excerpt = (result.result_excerpt + "\n[stderr]\n" + stderr.strip())[-4000:]
    _dump_flow_text(flow_path)
    return result
