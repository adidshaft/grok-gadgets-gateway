"""GW-CREDITS-002 and GW-CREDITS-003 (hub ADR 015): receipts, verify, report and the CLI.

Receipts are signed and hash-chained per account; a changed, removed or reordered receipt
fails, and so does one signed by another key. The report reconciles the ledger with the
`mcp_tool_call` records by request ID.
"""

import json
from contextlib import AsyncExitStack

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_credits import DAY, Clock, command, two_people

from grok_gadgets_gateway.accounts import Accounts
from grok_gadgets_gateway.credits import Credits, read_ledger, report
from grok_gadgets_gateway.oauth import oauth_dir
from grok_gadgets_gateway.receipts import Receipts, read_public, read_receipts, verify


def relay_store(tmp_path, clock=None):
    clock = clock or Clock()
    accounts = Accounts(tmp_path / "accounts.json", clock=clock).open(["dev-op"])
    a, _ = accounts._new_account()
    b, _ = accounts._new_account()
    accounts.assign("dev-a", a, via="operator")
    accounts.assign("dev-b", b, via="operator")
    receipts = Receipts(tmp_path, Ed25519PrivateKey.generate()).open()
    credits = Credits(tmp_path, accounts, clock=clock, receipts=receipts).open()
    return credits, receipts, a, b


def busy_days(tmp_path):
    """Two UTC days of grants, commands, a refund and gadget-days; receipts closed daily."""
    clock = Clock()
    credits, receipts, a, b = relay_store(tmp_path, clock)
    credits.grant(a, 5)
    credits.charge(a, "dev-a", "r1")
    refunded = credits.charge(a, "dev-a", "r2")
    credits.refund(refunded, "unavailable")
    credits.meter_days({"dev-a", "dev-b"})
    clock.now += 86400
    assert len(credits.close_receipts()) == 2  # day one, for A and for B
    assert credits.close_receipts() == []  # once a day
    credits.charge(a, "dev-a", "r3")
    credits.grant(b, 2)
    clock.now += 86400
    assert len(credits.close_receipts()) == 2
    return credits, receipts, a, b


def entries_of(tmp_path, account):
    return [e for e in read_ledger(tmp_path / "credits-ledger.jsonl") if e["account"] == account]


def test_receipts_add_up_and_verify(tmp_path):
    credits, receipts, a, b = busy_days(tmp_path)
    public = read_public(tmp_path / "credits-receipt-key.pub")
    chain = read_receipts(receipts.path(a))
    assert [r["seq"] for r in chain] == [1, 2]
    first = chain[0]
    assert (first["commands"], first["refunds"], first["gadget_days"]) == (1, 1, 1)
    assert (first["grants"], first["spent"], first["opening"], first["closing"]) == (5, 2, 0, 3)
    assert chain[1]["opening"] == 3 and chain[1]["closing"] == 2 == credits.balance(a)
    assert verify(chain, public) == []
    assert verify(chain, public, entries_of(tmp_path, a)) == []
    assert verify(read_receipts(receipts.path(b)), public, entries_of(tmp_path, b)) == []
    # Counts and the account ID only: no device, request, name, token or argument fields.
    fields = {
        "v",
        "account",
        "commands",
        "refunds",
        "gadget_days",
        "spent",
        "grants",
        "allowance",
        "opening",
        "closing",
        "seq",
        "period",
        "ledger",
        "issued",
        "prev",
        "key",
        "sig",
    }
    assert all(set(r) == fields for r in chain)
    assert "dev-a" not in receipts.path(a).read_text()


def test_a_changed_receipt_fails(tmp_path):
    _, receipts, a, _ = busy_days(tmp_path)
    public = read_public(tmp_path / "credits-receipt-key.pub")
    chain = read_receipts(receipts.path(a))
    chain[0]["spent"] = 0
    chain[0]["grants"] = 3
    problems = verify(chain, public)
    assert any("signature" in p for p in problems)
    assert any("previous hash" in p for p in problems)  # the next receipt no longer chains


def test_a_removed_or_reordered_receipt_fails(tmp_path):
    _, receipts, a, _ = busy_days(tmp_path)
    public = read_public(tmp_path / "credits-receipt-key.pub")
    chain = read_receipts(receipts.path(a))
    assert verify(chain[1:], public)  # the first is missing
    assert verify(list(reversed(chain)), public)
    assert verify(chain[:1], public) == []  # a prefix is honest about where it stops
    # A later entry not yet receipted is fine; one dropped from the ledger is not.
    assert verify(chain[:1], public, entries_of(tmp_path, a)) == []
    ledger = entries_of(tmp_path, a)
    del ledger[1]
    assert any("ledger" in p for p in verify(chain, public, ledger))


def test_receipts_signed_by_another_key_fail(tmp_path):
    _, receipts, a, _ = busy_days(tmp_path)
    other = Ed25519PrivateKey.generate().public_key()
    problems = verify(read_receipts(receipts.path(a)), other)
    assert any("signature" in p for p in problems) and any("another key" in p for p in problems)


def test_a_receipt_that_disagrees_with_the_ledger_fails(tmp_path):
    _, receipts, a, _ = busy_days(tmp_path)
    public = read_public(tmp_path / "credits-receipt-key.pub")
    ledger = entries_of(tmp_path, a)
    ledger[1]["delta"] = -3  # an edited ledger line
    assert any("ledger" in p for p in verify(read_receipts(receipts.path(a)), public, ledger))


def test_entries_after_the_cut_wait_for_the_next_receipt(tmp_path):
    clock = Clock()
    credits, _receipts, a, _ = relay_store(tmp_path, clock)
    credits.grant(a, 1)
    assert credits.close_receipts() == []  # nothing before today
    written = credits.close_receipts(now=True)
    assert [r["closing"] for r in written] == [1]
    assert credits.close_receipts(now=True) == []


def test_the_report_reconciles_with_request_records(tmp_path):
    _credits, _, a, b = busy_days(tmp_path)
    entries = read_ledger(tmp_path / "credits-ledger.jsonl")
    call = {"event": "mcp_tool_call", "tool": "gadgets_command", "credits": 1}
    calls = [
        {**call, "request_id": "r1", "ts": "2026-10-20T02:00:00+00:00"},
        {**call, "request_id": "r2", "ts": "2026-10-20T02:00:00+00:00", "credits": 0},
        {**call, "request_id": "r3", "ts": "2026-10-21T02:00:00+00:00"},
    ]
    value = report(entries, calls)
    assert value["reconciliation"]["matches"] is True
    assert value["accounts"][a]["spent"] == 3 and value["accounts"][a]["commands"] == 2
    assert value["accounts"][b]["gadget_days"] == 1 and value["accounts"][b]["balance"] == 1
    assert value["total_spent"] == 4
    missing = report(entries, calls[:1])
    assert missing["reconciliation"]["only_in_ledger"] == ["r3"]
    day_one = report(entries, calls, until="2026-10-20T23:59:59+00:00")
    assert day_one["accounts"][a]["spent"] == 2


def test_the_cli_grants_lists_verifies_and_reports(tmp_path, capsys):
    from grok_gadgets_gateway.cli import main

    folder = oauth_dir()
    folder.mkdir(parents=True)
    credits, receipts, a, _b = busy_days(folder)
    assert main(["credits", "grant", a, "4", "--no-wait"]) == 0
    assert credits.apply_admin() == 1 and credits.balance(a) == 6
    capsys.readouterr()
    assert main(["credits", "list"]) == 0
    listed = capsys.readouterr().out
    assert f"{a}  balance 6" in listed and "operator  exempt" in listed
    assert main(["credits", "verify"]) == 0
    assert capsys.readouterr().out.count("OK ") == 2
    copy = tmp_path / "copy.jsonl"
    lines = receipts.path(a).read_text().splitlines()
    copy.write_text(lines[0].replace('"spent":2', '"spent":1') + "\n" + lines[1] + "\n")
    assert main(["credits", "verify", "--receipts", str(copy), "--no-ledger"]) == 1
    assert "FAIL" in capsys.readouterr().out
    assert main(["credits", "ledger", a]) == 0
    assert "grant" in capsys.readouterr().out
    log = tmp_path / "requests.jsonl"
    log.write_text("")
    assert main(["credits", "report", "--log", str(log), "--json"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["accounts"][a]["operator"] is False
    assert main(["credits", "report", "--log", str(log)]) == 0
    assert "They are not money." in capsys.readouterr().out


async def test_receipts_and_report_on_a_running_relay(tmp_path):
    async with AsyncExitStack() as stack:
        running, alice, _bob, _ = await two_people(stack, tmp_path)
        a = running.credits.accounts.owner("dev-1")
        running.credits.grant(a, 2)
        assert (await command(alice, "dev-1"))["ok"]
        assert (await command(alice, "dev-1"))["ok"]
        assert (await command(alice, "dev-1"))["error"]["code"] == "out_of_credits"
        assert len(running.credits.close_receipts(now=True)) >= 1
    folder = tmp_path / "oauth"
    public = read_public(folder / "credits-receipt-key.pub")
    entries = read_ledger(folder / "credits-ledger.jsonl")
    chain = read_receipts(folder / "receipts" / f"{a}.jsonl")
    assert verify(chain, public, [e for e in entries if e["account"] == a]) == []
    records = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    calls = [r for r in records if r.get("event") == "mcp_tool_call"]
    value = report(entries, calls)
    assert value["reconciliation"]["matches"] and value["accounts"][a]["commands"] == 2


def test_day_is_a_utc_midnight():
    assert DAY % 86400 == 0
