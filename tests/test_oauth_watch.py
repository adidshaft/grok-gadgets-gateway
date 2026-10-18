"""`oauth watch` (hub ADR 014 §4): did Grok Bot's sign-in survive each hour without a new one?"""

import json

import pytest

from grok_gadgets_gateway import signin_watch
from grok_gadgets_gateway.cli import main


def line(ts, event, **fields):
    return json.dumps({"ts": f"2026-10-{ts}+00:00", "event": event, **fields})


def write(tmp_path, *lines):
    path = tmp_path / "requests.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_counts_each_day_and_pairs_expiries_with_refreshes(tmp_path):
    path = write(
        tmp_path,
        line("19T08:00:00", "oauth_token_issued", token="grok-bot", client_name="c"),
        line("19T08:01:00", "mcp_tool_call", remote=True, outcome="ok", token="grok-bot"),
        line("19T09:00:05", "oauth_access_expired", token="grok-bot"),
        line("19T09:00:06", "oauth_token_refreshed", token="grok-bot"),
        line("20T10:00:00", "oauth_token_refreshed", token="grok-bot"),
        line("20T11:00:01", "oauth_access_expired", token="grok-bot"),
        line("20T11:00:02", "http_rejected", status=401, reason="invalid_token", token=None),
        line("20T11:30:00", "oauth_token_refreshed", token="grok-bot"),
        line("20T12:00:00", "oauth_refresh_rejected", reason="expired", token="grok-bot"),
        "not json",
    )
    result = signin_watch.watch([path])
    days = {day["day"]: day for day in result["days"]}
    assert days["2026-10-19"]["sign_ins"] == 1
    assert days["2026-10-19"]["refreshes"] == 1
    assert days["2026-10-19"]["remote_calls"] == 1
    assert days["2026-10-20"]["refreshes"] == 2
    assert days["2026-10-20"]["unauthorized"] == 1
    assert days["2026-10-20"]["refresh_rejected"] == {"expired": 1}
    assert result["expiries"] == 2
    assert result["refreshed_after_expiry"] == 2
    assert result["expiry_then_401"] == [
        {
            "token": "grok-bot",
            "expired": "2026-10-20T11:00:01Z",
            "first_401": "2026-10-20T11:00:02Z",
        }
    ]


def test_since_skips_older_lines(tmp_path):
    path = write(
        tmp_path,
        line("18T08:00:00", "oauth_token_issued", token="grok-bot"),
        line("19T08:00:00", "oauth_token_refreshed", token="grok-bot"),
    )
    result = signin_watch.watch([path], since="2026-10-19")
    assert [day["day"] for day in result["days"]] == ["2026-10-19"]
    assert result["sign_ins"] == 0


def test_the_text_never_shows_a_client_name_or_address(tmp_path):
    path = write(
        tmp_path,
        line("19T08:00:00", "oauth_token_issued", token="grok-bot", client_name="Secret client"),
        line(
            "19T08:00:01", "http_rejected", status=401, reason="invalid_token", peer="203.0.113.9"
        ),
    )
    text = "\n".join(signin_watch.format_watch(signin_watch.watch([path])))
    assert "Secret client" not in text and "203.0.113.9" not in text
    assert "2026-10-19" in text and "fresh sign-ins 1" in text


def test_cli_oauth_watch(tmp_path, capsys):
    path = write(tmp_path, line("19T08:00:00", "oauth_token_refreshed", token="grok-bot"))
    assert main(["oauth", "watch", "--log", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["refreshes"] == 1
    with pytest.raises(SystemExit):
        main(["oauth", "watch", "--log", str(tmp_path / "missing.jsonl")])
