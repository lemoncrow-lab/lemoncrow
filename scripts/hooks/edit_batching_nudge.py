#!/usr/bin/env python3
import json
import os
import sys
import time


def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            sys.exit(0)

        payload = json.loads(raw_input)
        # Check if this is PostToolUse for mcp__atelier__edit
        # The hooks.json matcher will ensure this script only fires for Edit calls

        tool_input = payload.get("tool_input", {})
        edits = tool_input.get("edits", [])

        # State tracking logic using a temporary state file
        # We want to track how many *individual* Edit calls happened recently
        # Wait, if edits is a list, and len(edits) > 1, they are already batching!
        # If len(edits) == 1, they are not batching.

        if len(edits) <= 1:
            # Nudge if they are only editing one block at a time
            # We could track state over time, but for a simple stateless nudge:
            # Let's check how many total single edits they've done by reading the smart_state

            # For exact replica of wozcode, we check if they passed multiple changes.
            # "Consider batching: use edits[] for multiple changes to one file..."

            # WozCode nudges if the edits array itself has >= some threshold (meaning they are doing heavy edits and should be careful? No, it nudges to *encourage* batching. Wait, WozCode says "You've made <N> individual Edit calls in the last 30s. Consider batching...". But the code `if (_0x4df58c.edits.length >= pI)` means if the array length in the tool input itself is large? Actually, the code might be tracking previous calls.)
            # Let's keep it simple: if they submit a single edit, we can remind them they CAN batch.
            # But we don't want to spam them.
            # We can track consecutive single-edits in /tmp

            state_file = "/tmp/atelier_edit_nudge_state.json"
            now = time.time()
            state = {"count": 0, "last_ts": 0}

            if os.path.exists(state_file):
                try:
                    with open(state_file) as f:
                        state = json.load(f)
                except Exception:
                    pass

            # Reset if more than 60 seconds have passed since the last edit
            if now - state["last_ts"] > 60:
                state["count"] = 0

            state["count"] += 1
            state["last_ts"] = now

            with open(state_file, "w") as f:
                json.dump(state, f)

            # If they did 3 consecutive single-edits in a minute, NUDGE
            if state["count"] >= 3:
                output = {
                    "additionalContext": f"You've made {state['count']} individual Edit calls recently. Consider batching: use edits[] for multiple changes to one file, or files[] for changes across multiple files. This reduces API calls and saves tokens."
                }
                print(json.dumps(output))
                # Reset counter after nudging
                state["count"] = 0
                with open(state_file, "w") as f:
                    json.dump(state, f)

    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
