"""The example usage hook that posts a local macOS notification (examples/usage-hooks)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from grok_gadgets_gateway.background import serve_arguments
from grok_gadgets_gateway.usage import hook_line

SCRIPT = Path(__file__).parents[1] / "examples" / "usage-hooks" / "macos-notify.py"
spec = importlib.util.spec_from_file_location("macos_notify", SCRIPT)
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)


def usage(**fields):
    base = {"ts": "2026-10-14T08:00:00+00:00", "tool": "gadgets_command", "outcome": "ok"}
    return hook_line({**base, **fields})


def test_only_remote_calls_notify_and_bursts_are_counted():
    sent, now = [], [0.0]
    lines = [
        usage(remote=False, token="default"),
        usage(remote=True, token="grok-bot", capability="display.text", status="executed"),
        usage(remote=True, token="grok-bot", tool="gadgets_get_state"),
        usage(remote=True, token="grok-bot", tool="gadgets_read_events"),
        "not json\n",
    ]
    for line in lines:
        notify.main([line], send=lambda *a: sent.append(a), clock=lambda: now[0])
    assert sent[0] == ("Remote call as grok-bot", "gadgets_command display.text executed")
    # One process sees a burst: the second and third calls fold into the next notification.
    sent.clear()
    clock = iter([0.0, 1.0, 2.0, 10.0])
    notify.main(
        [lines[1], lines[2], lines[3], usage(remote=True, token="grok-bot", outcome="error")],
        send=lambda *a: sent.append(a),
        clock=lambda: next(clock),
    )
    assert sent == [
        ("Remote call as grok-bot", "gadgets_command display.text executed"),
        ("Remote call as grok-bot", "gadgets_command (error) (+2 more)"),
    ]


def test_the_hook_runs_as_a_program_and_never_sees_a_token():
    line = usage(remote=True, token="grok-bot", authorization="Bearer gga_secret", args={"t": 1})
    assert "gga_secret" not in line and '"args"' not in line
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--print"],
        input=line,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "Remote call as grok-bot: gadgets_command\n"
    assert json.loads(line)["remote"] is True


def test_the_service_passes_the_hook_to_serve():
    hook = "/Users/me/.venv/bin/python /Users/me/macos-notify.py"
    args = serve_arguments(None, 8766, 8765, "/logs/r.jsonl", ["gadgets.example.net"], True, hook)
    assert args[-2:] == ["--usage-hook", hook]
    assert "--usage-hook" not in serve_arguments()
