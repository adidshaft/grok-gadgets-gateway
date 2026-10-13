"""Usage records, summary, estimate and the opt-in usage hook (gateway #132)."""

import io
import json
import shlex
import sys
import time

import pytest
from test_http import TOKEN, free_port, write_local_config
from test_mcp_tokens import call_tools

from grok_gadgets_gateway import cli
from grok_gadgets_gateway.operator import OperatorError, issue_mcp_token
from grok_gadgets_gateway.service import serve_gateway
from grok_gadgets_gateway.usage import (
    HOOK_FIELDS,
    UsageHook,
    calibrate,
    hook_line,
    read_calls,
    read_rate,
    summarize,
)

TUNNEL = "gadgets.example.net"


def recorder(path):
    """A hook command that copies its stdin to `path`."""
    code = "import sys,shutil; shutil.copyfileobj(sys.stdin.buffer, open(sys.argv[1], 'wb', 0), 1)"
    return [sys.executable, "-c", code, str(path)]


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


async def test_tool_calls_record_usage_and_feed_the_hook(tmp_path):
    credentials, mcp_token = write_local_config(tmp_path)
    tokens = tmp_path / "mcp-tokens.json"
    named = issue_mcp_token("grok-bot", tokens)
    log_path = tmp_path / "requests.jsonl"
    hook_path = tmp_path / "hook.jsonl"
    hook = UsageHook(recorder(hook_path))
    async with serve_gateway(
        credentials,
        mcp_token,
        simulator=True,
        port=free_port(),
        device_port=free_port(),
        allowed_hosts=[TUNNEL],
        mcp_tokens=tokens,
        log_stream=io.StringIO(),
        request_log=log_path,
        usage_hook=hook,
    ) as running:
        await call_tools(running.url, named, {"Host": TUNNEL})
        await call_tools(running.url, TOKEN)
    assert hook.close() == {"sent": 2, "dropped": 0, "starts": 1}

    calls = read_calls([log_path])
    assert [(c["token"], c["remote"]) for c in calls] == [("grok-bot", True), ("default", False)]
    assert all(c["bytes_in"] == 2 and c["bytes_out"] > 20 for c in calls)

    lines = [json.loads(line) for line in hook_path.read_text().splitlines()]
    assert [line["token"] for line in lines] == ["grok-bot", "default"]
    for line in lines:
        assert line["event"] == "usage" and line["tool"] == "gadgets_list_devices"
        assert set(line) <= {"event", *HOOK_FIELDS}
    text = hook_path.read_text()
    assert named not in text and TOKEN not in text and "args_sha256" not in text


def test_hook_lines_never_carry_arguments_or_state():
    line = json.loads(
        hook_line(
            {
                "ts": "2026-10-13T00:00:00+00:00",
                "tool": "gadgets_command",
                "token": "grok-bot",
                "arguments": {"r": 1},
                "args_sha256": "abc",
                "state": {"on": True},
                "user_agent": "x",
                "device_id": "sim-c124",
                "client": None,
            }
        )
    )
    assert line == {
        "event": "usage",
        "ts": "2026-10-13T00:00:00+00:00",
        "tool": "gadgets_command",
        "token": "grok-bot",
        "device_id": "sim-c124",
    }


def test_a_slow_hook_drops_lines_and_never_blocks():
    # This hook never reads stdin, so its pipe fills and the writer thread blocks.
    hook = UsageHook([sys.executable, "-c", "import time; time.sleep(30)"], queue_size=4)
    started = time.monotonic()
    for _ in range(5000):
        hook.send({"tool": "gadgets_list_devices", "token": "grok-bot", "pad": "x" * 200})
    assert time.monotonic() - started < 1.0
    counts = hook.close(timeout=0.5)
    assert counts["dropped"] > 0


def test_a_missing_or_dying_hook_only_drops_lines():
    missing = UsageHook(["/nonexistent/usage-hook"])
    missing.send({"tool": "gadgets_list_devices"})
    assert wait_for(lambda: missing.dropped == 1)
    assert missing.close()["starts"] == 0

    dying = UsageHook([sys.executable, "-c", "pass"])
    for _ in range(50):
        dying.send({"tool": "gadgets_list_devices"})
    counts = dying.close()
    # It restarts at most every 10 seconds, so one start serves this whole burst.
    assert counts["starts"] == 1 and counts["sent"] + counts["dropped"] == 50

    with pytest.raises(OperatorError):
        UsageHook("")


def write_log(path, calls):
    rows = [{"event": "serve_started", "ts": "2026-10-13T00:00:00+00:00"}]
    for ts, token, device, capability, tool, outcome in calls:
        row = {
            "ts": ts,
            "event": "mcp_tool_call",
            "token": token,
            "tool": tool,
            "outcome": outcome,
            "duration_ms": 10.0,
            "bytes_in": 5,
            "bytes_out": 15,
        }
        if device:
            row["device_id"] = device
        if capability:
            row["capability"] = capability
        rows.append(row)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")


CALLS = [
    ("2026-10-13T09:00:00+00:00", "grok-bot", None, None, "gadgets_list_devices", "ok"),
    ("2026-10-13T09:00:05+00:00", "grok-bot", "sim-c124", "rgb.set", "gadgets_command", "ok"),
    ("2026-10-13T09:00:09+00:00", "grok-bot", "sim-c124", None, "gadgets_get_state", "ok"),
    ("2026-10-14T10:00:00+00:00", "default", "esp-1", "text.show", "gadgets_command", "busy"),
]


def test_summary_groups_by_token_device_capability_and_day(tmp_path):
    log = tmp_path / "requests.jsonl"
    write_log(log, CALLS)
    summary = summarize(read_calls([log]))
    assert summary["total"] == {
        "calls": 4,
        "errors": 1,
        "commands": 2,
        "duration_ms": 40.0,
        "bytes": 80,
    }
    assert summary["token"]["grok-bot"]["calls"] == 3
    assert summary["device"]["sim-c124"]["calls"] == 2 and summary["device"]["-"]["calls"] == 1
    assert summary["capability"]["text.show"]["errors"] == 1
    assert list(summary["day"]) == ["2026-10-13", "2026-10-14"]
    assert "estimate" not in summary

    from grok_gadgets_gateway.usage import parse_time

    one_day = read_calls([log], parse_time("2026-10-13"), parse_time("2026-10-13", end=True))
    assert len(one_day) == 3
    assert len(read_calls([log], token="default")) == 1


def test_estimates_are_labelled_and_calibration_is_saved(tmp_path):
    log = tmp_path / "requests.jsonl"
    write_log(log, CALLS)
    calls = read_calls([log], token="grok-bot")
    summary = summarize(calls, {"rate": 0.5, "unit": "% weekly usage"})
    assert summary["total"]["estimate"] == 1.5
    assert summary["estimate"]["label"] == "estimate, not a Grok Bot reading"

    from grok_gadgets_gateway.usage import parse_time

    rate_file = tmp_path / "usage-rate.json"
    since, until = parse_time("2026-10-13T08:00"), parse_time("2026-10-13T10:00")
    rate = calibrate(calls, 3.0, "% weekly usage", since, until, rate_file)
    assert rate["rate"] == 1.0 and "3.0 % weekly usage measured over 3 calls" in rate["source"]
    assert read_rate(rate_file)["rate"] == 1.0
    assert (rate_file.stat().st_mode & 0o777) == 0o600
    with pytest.raises(OperatorError, match="no tool calls"):
        calibrate([], 1.0, "%", since, until, rate_file)
    rate_file.write_text("{")
    with pytest.raises(OperatorError, match="damaged"):
        read_rate(rate_file)


def test_usage_command(tmp_path, capsys, private_home):
    log = tmp_path / "requests.jsonl"
    write_log(log, CALLS)
    assert cli.main(["usage", "--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "total:  calls=4" in out and "by day:" in out and "estimate" not in out

    args = ["usage", "--log", str(log), "--token", "grok-bot"]
    assert cli.main([*args, "--rate", "0.25", "--unit", "% weekly usage"]) == 0
    out = capsys.readouterr().out
    assert "estimate=0.75" in out and "not a Grok Bot reading" in out

    window = ["--since", "2026-10-13T08:00", "--until", "2026-10-13T10:00"]
    assert cli.main([*args, *window, "--calibrate", "1.5", "--unit", "% weekly usage"]) == 0
    assert "Saved the estimate rate" in capsys.readouterr().err
    # The saved rate applies to later summaries until it is calibrated again.
    assert cli.main([*args, "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["total"]["estimate"] == 1.5 and summary["estimate"]["rate_per_call"] == 0.5

    for bad in (
        ["--rate", "1"],
        ["--calibrate", "1", "--unit", "%"],
        ["--rate", "1", "--calibrate", "1", "--unit", "%"],
    ):
        with pytest.raises(SystemExit):
            cli.main([*args, *bad])
    capsys.readouterr()


async def test_stdio_feeds_the_hook_without_a_request_log(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    hook_path = tmp_path / "hook.jsonl"
    command = shlex.join(recorder(hook_path))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "grok_gadgets_gateway.cli", "stdio", "--simulator", "--usage-hook", command],
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        await session.call_tool("gadgets_list_devices", {})
    assert wait_for(lambda: hook_path.exists() and hook_path.read_text().count("\n") == 1)
    line = json.loads(hook_path.read_text())
    assert line["transport"] == "stdio" and line["tool"] == "gadgets_list_devices"
    assert "token" not in line and "remote" not in line
