"""Tests for the OpenCode prompt-time Atelier nudge plugin."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "integrations" / "opencode" / "plugins"


def test_opencode_nudge_helper_emits_no_multi_file_context(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["ATELIER_ROOT"] = str(tmp_path / ".atelier")
    result = subprocess.run(
        [sys.executable, str(PLUGINS / "atelier_nudge.py")],
        input=json.dumps({"session_id": "s1", "prompt": "Update auth.py and billing.py together"}),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    # The nudge may emit a stale-agent nudge for optional agents installed on
    # this system (e.g. "explore installed, never used — remove: ...").  The
    # key invariant: no multi-file context message is emitted for this prompt.
    if result.stdout:
        data = json.loads(result.stdout)
        assert "uiMessage" in data, f"unexpected output: {result.stdout}"
        assert "multi-file" not in data["uiMessage"].lower()


def test_opencode_javascript_plugin_leaves_multi_file_prompt_unchanged(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["ATELIER_ROOT"] = str(tmp_path / ".atelier")
    script = f"""
import {{ AtelierNudge }} from {json.dumps((PLUGINS / "atelier-nudge.js").as_uri())}
const client = {{ tui: {{ showToast: async () => true }} }}
const hooks = await AtelierNudge({{ client, directory: process.cwd() }})
const output = {{ parts: [{{ type: 'text', text: 'Update auth.py and billing.py together' }}] }}
await hooks['chat.message']({{ sessionID: 's1' }}, output)
console.log(JSON.stringify(output))
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    output = json.loads(result.stdout)
    assert output["parts"][0]["text"] == "Update auth.py and billing.py together"


def test_opencode_nudge_helper_surfaces_stale_agent_nudge_once_per_day(tmp_path: Path) -> None:
    """An installed OPTIONAL agent role that's never been used surfaces a
    staleness nudge via the same uiMessage channel the compaction notice
    uses, gated to at most once per calendar day per item (marker file under
    ATELIER_ROOT/opencode_stale_nudge_shown/, mirroring the Claude statusline
    tip's once-a-day marker pattern).
    """
    opencode_config = tmp_path / "opencode_config"
    (opencode_config / "agents").mkdir(parents=True)
    (opencode_config / "agents" / "atelier.explore.md").write_text("body", encoding="utf-8")

    env = os.environ.copy()
    env["ATELIER_ROOT"] = str(tmp_path / ".atelier")
    env["OPENCODE_CONFIG_HOME"] = str(opencode_config)
    payload = json.dumps({"session_id": "s1", "prompt": "hello"})

    first = subprocess.run(
        [sys.executable, str(PLUGINS / "atelier_nudge.py")],
        input=payload,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    out = json.loads(first.stdout)
    assert "explore installed, never used" in out["uiMessage"]
    assert "/atelier remove explore" in out["uiMessage"]

    # Same calendar day, second prompt: cooldown suppresses the repeat.
    second = subprocess.run(
        [sys.executable, str(PLUGINS / "atelier_nudge.py")],
        input=payload,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    assert second.stdout == ""


def test_opencode_repeated_failure_injects_rescue_on_next_prompt(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["ATELIER_ROOT"] = str(tmp_path / ".atelier")
    script = f"""
    import {{ AtelierNudge }} from {json.dumps((PLUGINS / "atelier-nudge.js").as_uri())}
    const client = {{ tui: {{ showToast: async () => true }} }}
    const hooks = await AtelierNudge({{ client, directory: process.cwd() }})
    const input = {{ tool: 'bash', sessionID: 's1', callID: 'c1', args: {{ command: 'make test' }} }}
    const failure = {{ title: 'failed', output: 'Error: same failure', metadata: {{ exitCode: 1 }} }}
    await hooks['tool.execute.after'](input, failure)
    await hooks['tool.execute.after']({{ ...input, callID: 'c2' }}, failure)
    const output = {{ parts: [{{ type: 'text', text: 'Try again' }}] }}
    await hooks['chat.message']({{ sessionID: 's1' }}, output)
    console.log(JSON.stringify(output))
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    output = json.loads(result.stdout)
    assert "Call 'rescue' before any retry" in output["parts"][0]["text"]
