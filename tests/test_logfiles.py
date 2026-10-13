"""The request log and the service log rotate by size; `usage` reads the rotated files."""

import io
import json
import os
import stat

from grok_gadgets_gateway import cli
from grok_gadgets_gateway.logfiles import rotated
from grok_gadgets_gateway.mcp_server import RequestLog


def call(n):
    return {"event": "mcp_tool_call", "tool": "gadgets_list_devices", "token": "grok-bot", "n": n}


def test_the_request_log_rotates_and_keeps_a_fixed_number_of_files(tmp_path):
    path = tmp_path / "requests.jsonl"
    log = RequestLog("streamable-http", stream=io.StringIO(), path=path, max_bytes=400, keep=2)
    for n in range(60):
        log.write(call(n))
    log.close()
    files = rotated(path, keep=2)
    assert [f.name for f in files] == ["requests.jsonl.2", "requests.jsonl.1", "requests.jsonl"]
    assert not (tmp_path / "requests.jsonl.3").exists()
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert f.stat().st_size < 400 + 200
    numbers = [json.loads(line)["n"] for f in files for line in f.read_text().splitlines()]
    # Oldest first, no gaps, and the newest line is in the live file.
    assert numbers == list(range(numbers[0], 60))
    assert numbers[0] > 0


def test_usage_counts_calls_in_rotated_files(tmp_path, capsys):
    path = tmp_path / "requests.jsonl"
    log = RequestLog("streamable-http", stream=io.StringIO(), path=path, max_bytes=400, keep=5)
    for n in range(12):
        log.write(call(n))
    log.close()
    assert len(rotated(path)) > 1
    assert cli.main(["usage", "--log", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"]["calls"] == 12


def test_the_service_log_is_copied_and_truncated_in_place(tmp_path):
    path = tmp_path / "gateway.log"
    path.write_text("")
    os.chmod(path, 0o644)
    # The service manager holds the log open in append mode, like launchd and systemd.
    with open(path, "a", encoding="utf-8") as stream:
        log = RequestLog("streamable-http", stream=stream, max_bytes=400, keep=2, service_log=path)
        for n in range(30):
            log.write({"event": "lan_hello", "n": n})
        assert path.stat().st_size < 400 + 200
        log.write({"event": "lan_hello", "n": "last"})
    assert path.read_text().splitlines()[-1].endswith('"n":"last"}')
    assert (tmp_path / "gateway.log.1").is_file()
    assert (tmp_path / "gateway.log.2").is_file()
    assert not (tmp_path / "gateway.log.3").exists()
    # Writes after the truncate land at the start of the file, not after a gap of zeros.
    assert not path.read_bytes().startswith(b"\0")


def test_a_terminal_or_pipe_is_never_rotated():
    log = RequestLog("stdio", stream=io.StringIO())
    assert log.service_log is None
