"""`builder report` (GW-BUILD-002, hub ADR 013 §3): counts from the relay's own files only."""

import json

import pytest

from grok_gadgets_gateway import cli
from grok_gadgets_gateway.report import build_report, format_report

SECRET_BITS = (
    "Hello secret text",  # command arguments
    "203.0.113.7",  # caller and peer addresses
    "acct_",  # account IDs
    "ABCD-EFGH",  # codes
    "gga_",  # tokens
    '"rgb"',  # reported state
)


def line(ts, event, **fields):
    return json.dumps({"ts": f"2026-10-{ts}+00:00", "event": event, **fields})


@pytest.fixture
def relay(tmp_path):
    log = tmp_path / "requests.jsonl"
    rotated = tmp_path / "requests.jsonl.1"
    rotated.write_text(
        "\n".join(
            [
                line("10T08:00:00", "device_hello", device="lamp-01", peer="203.0.113.7"),
                line("10T09:00:00", "mcp_tool_call", device_id="lamp-01", outcome="ok"),
            ]
        )
        + "\n"
    )
    log.write_text(
        "\n".join(
            [
                line("16T08:00:00", "device_hello", device="lamp-01", peer="203.0.113.7"),
                line(
                    "16T08:01:00",
                    "mcp_tool_call",
                    device_id="lamp-01",
                    tool="gadgets_command",
                    outcome="ok",
                    token="acct_abcdefghijklmnopqrstuvwxyz",
                    arguments={"text": "Hello secret text"},
                    peer="203.0.113.7",
                ),
                line("16T09:00:00", "mcp_tool_call", device_id="lamp-01", outcome="ok"),
                line("17T10:00:00", "mcp_tool_call", device_id="lamp-01", outcome="timed_out"),
                line("17T10:05:00", "mcp_tool_call", device_id="lamp-01", outcome="timed_out"),
                line("17T10:06:00", "mcp_tool_call", device_id="lamp-01", outcome="unknown_device"),
                line("17T11:00:00", "device_disconnected", device="lamp-01", peer="203.0.113.7"),
                line("17T11:30:00", "device_hello", device="oledkit-0123456789ab"),
                line("17T11:31:00", "claim_rejected", code="ABCD-EFGH", caller="203.0.113.7"),
                line(
                    "17T12:00:00",
                    "update_applied",
                    device="oledkit-0123456789ab",
                    version="1.0.1",
                ),
                line("17T12:00:01", "mcp_tool_call", tool="gadgets_list_devices", outcome="ok"),
                "not json",
                line("17T12:30:00", "device_hello", device="never-called"),
            ]
        )
        + "\n"
    )
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    (firmware / "status.json").write_text(
        json.dumps(
            {
                "devices": {
                    "oledkit-0123456789ab": {
                        "version": "1.0.1",
                        "status": "applied",
                        "reported": "1.0.1",
                        "at": "2026-10-17T12:00:00+00:00",
                    },
                    "lamp-01": {
                        "version": "2.0.0",
                        "status": "reverted",
                        "reason": "not_online",
                        "reported": "1.9.0",
                    },
                }
            }
        )
    )
    accounts = tmp_path / "accounts.json"
    accounts.write_text(
        json.dumps(
            {
                "operator": "acct_operator",
                "accounts": {"acct_operator": {}, "acct_other": {}},
                "devices": {
                    "oledkit-0123456789ab": {"owner": "acct_operator"},
                    "lamp-01": {"owner": None},
                },
                "labels": {
                    "acct_operator": {"oledkit-0123456789ab": {"name": "Desk display"}},
                    "acct_other": {"lamp-01": {"name": "Someone's private name"}},
                },
            }
        )
    )
    return tmp_path, [rotated, log], firmware, accounts


def test_report_counts_without_private_data(relay):
    _, logs, firmware, accounts = relay
    report = build_report(
        logs, firmware_dir=firmware, accounts_path=accounts, today="2026-10-17", days=7
    )
    lamp = report["devices"]["lamp-01"]
    assert lamp["online"] is False and lamp["last_seen"] == "2026-10-17T11:00:00+00:00"
    assert lamp["calls"] == {"2026-10-16": 2, "2026-10-17": 3}  # 10 Oct is outside 7 days
    assert lamp["calls_total"] == 5
    assert lamp["errors"] == {"timed_out": 2, "unknown_device": 1}
    assert lamp["firmware"] == "1.9.0"
    assert lamp["update"] == {"version": "2.0.0", "status": "reverted", "reason": "not_online"}
    assert (
        lamp["claimed"] is False and lamp["name"] is None
    )  # Another account's name stays private.

    kit = report["devices"]["oledkit-0123456789ab"]
    assert kit["online"] is True and kit["calls_total"] == 0
    assert kit["firmware"] == "1.0.1" and kit["claimed"] is True
    assert kit["name"] == "Desk display"
    assert report["devices"]["never-called"]["online"] is True
    assert report["calls_without_device"] == 1
    assert report["window"] == {"from": "2026-10-11", "to": "2026-10-17"}

    for text in (json.dumps(report), format_report(report)):
        for bit in SECRET_BITS:
            assert bit not in text


def test_report_without_accounts_or_firmware(tmp_path):
    log = tmp_path / "requests.jsonl"
    log.write_text(line("17T10:00:00", "device_hello", device="lamp-01") + "\n")
    report = build_report([log], firmware_dir=tmp_path / "none", accounts_path=tmp_path / "none")
    assert report["devices"]["lamp-01"]["claimed"] is None
    assert report["devices"]["lamp-01"]["firmware"] is None
    assert "lamp-01" in format_report(report)


def test_builder_report_cli(relay, capsys, private_home):
    root, logs, firmware, accounts = relay
    args = ["builder", "report", "--firmware-dir", str(firmware), "--accounts", str(accounts)]
    assert cli.main([*args, "--log", str(logs[1]), "--json", "--days", "30"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["devices"]["lamp-01"]["calls_total"] == 6  # The rotated file is read too.
    assert cli.main([*args, "--log", str(logs[1])]) == 0
    out = capsys.readouterr().out
    assert "lamp-01" in out and "Desk display" in out and "offline" in out
    with pytest.raises(SystemExit):
        cli.main(["builder", "report", "--log", str(root / "missing.jsonl")])
    capsys.readouterr()
