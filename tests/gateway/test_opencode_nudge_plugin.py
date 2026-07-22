"""Tests for the OpenCode prompt-time LemonCrow nudge plugin."""

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
    env["LEMONCROW_ROOT"] = str(tmp_path / ".lemoncrow")
    result = subprocess.run(
        [sys.executable, str(PLUGINS / "lemoncrow_nudge.py")],
        input=json.dumps({"session_id": "s1", "prompt": "Update auth.py and billing.py together"}),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    # No multi-file context message is emitted for this prompt.
    if result.stdout:
        data = json.loads(result.stdout)
        assert "uiMessage" in data, f"unexpected output: {result.stdout}"
        assert "multi-file" not in data["uiMessage"].lower()


def test_opencode_javascript_plugin_leaves_multi_file_prompt_unchanged(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(tmp_path / ".lemoncrow")
    script = f"""
import {{ LemonCrowNudge }} from {json.dumps((PLUGINS / "lemoncrow-nudge.js").as_uri())}
const client = {{ tui: {{ showToast: async () => true }} }}
const hooks = await LemonCrowNudge({{ client, directory: process.cwd() }})
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


def test_opencode_repeated_failure_injects_rescue_on_next_prompt(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(tmp_path / ".lemoncrow")
    script = f"""
    import {{ LemonCrowNudge }} from {json.dumps((PLUGINS / "lemoncrow-nudge.js").as_uri())}
    const client = {{ tui: {{ showToast: async () => true }} }}
    const hooks = await LemonCrowNudge({{ client, directory: process.cwd() }})
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


def test_opencode_required_arg_error_shows_toast_on_first_occurrence(tmp_path: Path) -> None:
    """A required-argument TypeError never repeats identically (each guessed
    value produces a different error), so it must fire on the FIRST failure --
    unlike the repeat-threshold rescue nudge above, which needs 2 calls.
    """
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(tmp_path / ".lemoncrow")
    env["LEMONCROW_REQUIRED_ARG_NUDGE"] = "1"
    # Pin to this checkout's own venv: the JS plugin's python-resolution
    # fallback otherwise finds a globally `uv tool install`-ed lemoncrow (a
    # separate build), silently missing any uncommitted/dev-only source
    # change -- exactly the feature this test exists to exercise.
    env["LEMONCROW_PYTHON"] = str(ROOT / ".venv" / "bin" / "python3")
    script = f"""
    import {{ LemonCrowNudge }} from {json.dumps((PLUGINS / "lemoncrow-nudge.js").as_uri())}
    const toasts = []
    const client = {{ tui: {{ showToast: async (toast) => toasts.push(toast) }} }}
    const hooks = await LemonCrowNudge({{ client, directory: process.cwd() }})
    const input = {{ tool: 'bash', sessionID: 's1', callID: 'c1', args: {{ command: 'python3 run.py' }} }}
    const failure = {{ title: 'failed', output: "TypeError: encode() missing 1 required keyword-only argument: 'task_name'", metadata: {{ exitCode: 1 }} }}
    await hooks['tool.execute.after'](input, failure)
    console.log(JSON.stringify(toasts))
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    toasts = json.loads(result.stdout)
    assert any("read what it controls" in toast["body"]["message"].lower() for toast in toasts)


def test_opencode_required_arg_nudge_off_by_default(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(tmp_path / ".lemoncrow")
    env["LEMONCROW_PYTHON"] = str(ROOT / ".venv" / "bin" / "python3")
    env.pop("LEMONCROW_REQUIRED_ARG_NUDGE", None)
    script = f"""
    import {{ LemonCrowNudge }} from {json.dumps((PLUGINS / "lemoncrow-nudge.js").as_uri())}
    const toasts = []
    const client = {{ tui: {{ showToast: async (toast) => toasts.push(toast) }} }}
    const hooks = await LemonCrowNudge({{ client, directory: process.cwd() }})
    const input = {{ tool: 'bash', sessionID: 's1', callID: 'c1', args: {{ command: 'python3 run.py' }} }}
    const failure = {{ title: 'failed', output: "TypeError: encode() missing 1 required keyword-only argument: 'task_name'", metadata: {{ exitCode: 1 }} }}
    await hooks['tool.execute.after'](input, failure)
    console.log(JSON.stringify(toasts))
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    toasts = json.loads(result.stdout)
    assert not any("read what it controls" in (toast["body"].get("message") or "").lower() for toast in toasts)


def test_opencode_idle_event_shows_session_status(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(tmp_path / ".lemoncrow")
    script = f"""
    import {{ LemonCrowNudge }} from {json.dumps((PLUGINS / "lemoncrow-nudge.js").as_uri())}
    const toasts = []
    const client = {{ tui: {{ showToast: async (toast) => toasts.push(toast) }} }}
    const hooks = await LemonCrowNudge({{ client, directory: process.cwd() }})
    await hooks['chat.message']({{ sessionID: 's1' }}, {{ parts: [{{ type: 'text', text: 'inspect it' }}] }})
    await hooks['tool.execute.after']({{ tool: 'lc_read', sessionID: 's1', args: {{ files: ['a.py'] }} }}, {{ output: 'ok', metadata: {{ exitCode: 0 }} }})
    await hooks.event({{ event: {{ type: 'session.idle', properties: {{ sessionID: 's1' }} }} }})
    console.log(JSON.stringify(toasts))
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    toasts = json.loads(result.stdout)
    assert any(toast["body"]["title"] == "lc status" for toast in toasts)
