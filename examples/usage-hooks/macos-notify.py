#!/usr/bin/env python3
"""Usage hook: a local macOS notification for each remote tool call.

Run it from the gateway, for example:

    grok-gadgets-gateway service install --lan-listen my-mac.local \\
      --allowed-host gadgets.example.net --oauth \\
      --usage-hook "/ABSOLUTE/PATH/.venv/bin/python /ABSOLUTE/PATH/macos-notify.py"

It reads the gateway's usage lines on stdin (never tokens or arguments) and calls
`osascript` on this Mac. Nothing leaves the computer. Loopback calls are ignored. At most one
notification every few seconds; calls in between are counted in the next one.
"""

import json
import subprocess
import sys
import time

GAP_SECONDS = 3
SCRIPT = (
    "on run argv\n"
    'display notification (item 2 of argv) with title "Grok Gadgets" subtitle (item 1 of argv)\n'
    "end run"
)


def message(record):
    """Subtitle and text for one usage line, or None for a call that is not remote."""
    if record.get("event") != "usage" or record.get("remote") is not True:
        return None
    detail = " ".join(
        str(record[key]) for key in ("capability", "status") if record.get(key) is not None
    )
    outcome = record.get("outcome", "?")
    text = f"{record.get('tool', '?')} {detail}".strip()
    if outcome != "ok":
        text += f" ({outcome})"
    return f"Remote call as {record.get('token') or '?'}", text


def notify(subtitle, text):
    subprocess.run(
        ["osascript", "-e", SCRIPT, subtitle, text],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main(lines, send=notify, clock=time.monotonic):
    last = None
    held = 0
    for line in lines:
        try:
            shown = message(json.loads(line))
        except ValueError:
            continue
        if shown is None:
            continue
        now = clock()
        if last is not None and now - last < GAP_SECONDS:
            held += 1
            continue
        subtitle, text = shown
        if held:
            text += f" (+{held} more)"
        send(subtitle, text)
        last, held = now, 0


if __name__ == "__main__":
    if "--print" in sys.argv[1:]:
        main(sys.stdin, send=lambda subtitle, text: print(f"{subtitle}: {text}", flush=True))
    else:
        main(sys.stdin)
