"""GW-CREDITS-001 and GW-CREDITS-004 (hub ADR 015 Stage 0): balances enforced in the call path.

Unit tests for the store and the gate, then two people on a relay with `--credits`: the gate
debits before a command runs, refuses at zero with plain text, never shows one account's
balance to another, and changes nothing when credits are off.
"""

import asyncio
import io
import json
import threading
from contextlib import AsyncExitStack

import pytest
from test_scoped_tools import TOKENS, error, gadget, sign_in

from grok_gadgets_gateway.accounts import Accounts
from grok_gadgets_gateway.credits import OUT_OF_CREDITS, Credits, queue_admin, read_ledger
from grok_gadgets_gateway.operator import OperatorError, atomic_write
from grok_gadgets_gateway.protocol import GatewayError
from grok_gadgets_gateway.service import serve_gateway
from grok_gadgets_gateway.usage import UsageHook

RGB = {"r": 1, "g": 2, "b": 3, "on": True}
DAY = 1_792_454_400  # 2026-10-20T00:00:00Z


class Clock:
    def __init__(self, now=DAY + 3600):
        self.now = now

    def __call__(self):
        return self.now


def store(tmp_path, clock=None):
    """An accounts store with the operator, A and B, and a credits store beside it."""
    clock = clock or Clock()
    accounts = Accounts(tmp_path / "accounts.json", clock=clock).open(["dev-op"])
    a, _ = accounts._new_account()
    b, _ = accounts._new_account()
    accounts.assign("dev-a", a, via="operator")
    accounts.assign("dev-b", b, via="operator")
    credits = Credits(tmp_path, accounts, clock=clock).open()
    return accounts, credits, a, b


# --- the store and the gate ---


def test_a_new_store_has_zero_balances_and_the_default_settings(tmp_path):
    accounts, credits, a, b = store(tmp_path)
    assert credits.settings == {"command_weight": 1, "gadget_day_weight": 1, "allowance": 0}
    assert credits.balance(a) == credits.balance(b) == 0
    assert credits.view(accounts.operator) == {"exempt": True}
    assert json.loads((tmp_path / "credits.json").read_text())["accounts"] == {}


def test_existing_accounts_migrate_without_touching_accounts_json(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json").open(["dev-op"])
    b, _ = accounts._new_account()
    accounts._save()
    before = (tmp_path / "accounts.json").read_bytes()
    credits = Credits(tmp_path, Accounts(tmp_path / "accounts.json").open([])).open()
    assert credits.balance(b) == 0 and credits.view(b) == {"balance": 0}
    assert (tmp_path / "accounts.json").read_bytes() == before


def test_the_gate_debits_first_and_refuses_at_zero_without_debiting(tmp_path):
    _, credits, a, _ = store(tmp_path)
    credits.grant(a, 2)
    first = credits.charge(a, "dev-a", "req-1")
    credits.charge(a, "dev-a", "req-2")
    assert credits.balance(a) == 0 and first["amount"] == 1
    with pytest.raises(GatewayError) as refused:
        credits.charge(a, "dev-a", "req-3")
    assert refused.value.code == "out_of_credits"
    assert refused.value.message == OUT_OF_CREDITS
    assert credits.balance(a) == 0
    kinds = [e["kind"] for e in read_ledger(tmp_path / "credits-ledger.jsonl")]
    assert kinds == ["grant", "command", "command"]


def test_the_out_of_credits_text_names_no_price_or_link():
    lowered = OUT_OF_CREDITS.lower()
    assert "http" not in lowered and "www" not in lowered
    assert not any(c.isdigit() for c in OUT_OF_CREDITS)
    assert not any(symbol in OUT_OF_CREDITS for symbol in "$€£¥₹")
    for word in ("price", "pay", "buy", "purchase", "token", "grok"):
        assert word not in lowered


def test_a_refund_is_paid_once(tmp_path):
    _, credits, a, _ = store(tmp_path)
    credits.grant(a, 1)
    charge = credits.charge(a, "dev-a", "req-1")
    credits.refund(charge, "unavailable")
    credits.refund(charge, "unavailable")
    assert credits.balance(a) == 1
    credits.refund(None, "nothing")


def test_the_operator_is_exempt(tmp_path):
    accounts, credits, _, _ = store(tmp_path)
    assert credits.charge(accounts.operator, "dev-op", "req-1") is None
    assert credits.charge(None, "dev-op", "req-2") is None
    assert credits.balance(accounts.operator) == 0
    assert credits.meter_days({"dev-op"}) == 0
    assert read_ledger(tmp_path / "credits-ledger.jsonl") == []


def test_no_double_spend_under_concurrent_charges(tmp_path):
    _, credits, a, _ = store(tmp_path)
    credits.grant(a, 5)
    won, lost = [], []

    def spend(number):
        try:
            won.append(credits.charge(a, "dev-a", f"req-{number}"))
        except GatewayError:
            lost.append(number)

    threads = [threading.Thread(target=spend, args=(n,)) for n in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(won) == 5 and len(lost) == 35 and credits.balance(a) == 0


def test_balances_survive_a_restart_and_a_torn_last_line(tmp_path):
    accounts, credits, a, b = store(tmp_path)
    credits.grant(a, 5)
    credits.charge(a, "dev-a", "req-1")
    credits.grant(b, 2)
    credits.close()
    with open(tmp_path / "credits-ledger.jsonl", "a") as file:
        file.write('{"seq": 9, "kind": "gra')  # a crash in the middle of a write
    again = Credits(tmp_path, accounts).open()
    assert again.balance(a) == 4 and again.balance(b) == 2
    again.grant(b, 1)
    assert Credits(tmp_path, accounts).open().balance(b) == 3


def test_a_damaged_middle_line_stops_the_relay(tmp_path):
    accounts, credits, a, _ = store(tmp_path)
    credits.grant(a, 1)
    credits.close()
    path = tmp_path / "credits-ledger.jsonl"
    path.write_text("not json\n" + path.read_text())
    with pytest.raises(OperatorError, match="damaged"):
        Credits(tmp_path, accounts).open()


def test_gadget_days_are_charged_once_a_day_and_may_go_below_zero(tmp_path):
    clock = Clock()
    _, credits, a, b = store(tmp_path, clock)
    assert credits.meter_days({"dev-a", "dev-b", "dev-op", "dev-unclaimed"}) == 2
    assert credits.meter_days({"dev-a", "dev-b"}) == 0
    assert credits.balance(a) == credits.balance(b) == -1
    clock.now += 86400
    assert credits.meter_days({"dev-a"}) == 1
    assert credits.balance(a) == -2 and credits.balance(b) == -1


def test_weights_and_the_monthly_allowance(tmp_path):
    clock = Clock()
    _, credits, a, b = store(tmp_path, clock)
    credits.set_settings(command_weight=2, gadget_day_weight=0, allowance=10)
    credits.set_allowance(b, 3)
    assert credits.meter_days({"dev-a"}) == 0
    credits.charge(a, "dev-a", "req-1")
    assert credits.balance(a) == 8  # raised to 10 on the month's first use, then 2 spent
    credits.charge(a, "dev-a", "req-2")
    assert credits.balance(a) == 6  # once a month only
    assert credits.view(b) == {"balance": 3}
    clock.now += 31 * 86400
    credits.charge(a, "dev-a", "req-3")
    assert credits.balance(a) == 8
    with pytest.raises(OperatorError):
        credits.set_settings(command_weight=-1)
    with pytest.raises(OperatorError):
        credits.set_settings(price=1)


def test_queued_operator_changes_are_applied_by_serve(tmp_path):
    _, credits, a, _ = store(tmp_path)
    queue_admin(tmp_path, {"action": "grant", "account": a, "amount": 5})
    queue_admin(tmp_path, {"action": "grant", "account": "acct_unknown", "amount": 5})
    queue_admin(tmp_path, {"action": "settings", "settings": {"gadget_day_weight": 2}})
    assert credits.apply_admin() == 2
    assert credits.balance(a) == 5 and credits.settings["gadget_day_weight"] == 2
    assert list((tmp_path / "credits-admin").glob("*.json")) == []
    with pytest.raises(OperatorError):
        credits.grant(a, 0)


# --- on a relay: two people, credits on ---


def relay(tmp_path, *, credits=True, usage_hook=None):
    path = tmp_path / "credentials.json"
    devices = {d: {"token": t, "revoked": False} for d, t in TOKENS.items()}
    atomic_write(path, json.dumps({"devices": devices}).encode())
    atomic_write(tmp_path / "mcp-token", b"mcp-test-token-value\n")
    Accounts(tmp_path / "oauth" / "accounts.json").open([])
    from test_http import free_port

    port, device_port = free_port(), free_port()
    server = serve_gateway(
        path,
        tmp_path / "mcp-token",
        port=port,
        device_port=device_port,
        log_stream=io.StringIO(),
        request_log=tmp_path / "requests.jsonl",
        oauth=True,
        oauth_issuer=f"http://localhost:{port}",
        oauth_directory=tmp_path / "oauth",
        accounts=True,
        credits=credits,
        usage_hook=usage_hook,
    )
    return server, device_port


async def acknowledge(exchange, seen):
    """A gadget that polls every 20 ms and reports each command executed."""
    while True:
        reply = await exchange({"type": "poll"})
        for command in reply.get("commands", []):
            seen.append(command["command_id"])
            ack = {"type": "ack", "command_id": command["command_id"], "status": "executed"}
            await exchange({**ack, "state": {"rgb": RGB}})
        await asyncio.sleep(0.02)


async def two_people(stack, tmp_path, **options):
    server, device_port = relay(tmp_path, **options)
    running = await stack.enter_async_context(server)
    url = running.url.replace("127.0.0.1", "localhost")
    people, seen = [], {}
    for device_id in ("dev-1", "dev-2"):
        writer, reply, exchange = await gadget(device_port, device_id)
        stack.callback(writer.close)
        seen[device_id] = []
        worker = asyncio.create_task(acknowledge(exchange, seen[device_id]))
        stack.callback(worker.cancel)
        people.append(await sign_in(stack, url, reply["claim_code"]))
    return running, people[0], people[1], seen


def account_of(running, device_id):
    return running.credits.accounts.owner(device_id)


async def command(person, device_id, **extra):
    return await person.call(
        "gadgets_command", device_id=device_id, capability="rgb.set", arguments=RGB, **extra
    )


async def test_a_command_at_zero_does_not_run_and_reads_stay_free(tmp_path):
    async with AsyncExitStack() as stack:
        running, alice, _, seen = await two_people(stack, tmp_path)
        refused = await command(alice, "dev-1")
        assert error(refused) == "out_of_credits"
        assert refused["error"]["message"] == OUT_OF_CREDITS
        await asyncio.sleep(0.2)
        assert seen["dev-1"] == []
        listed = await alice.call("gadgets_list_devices")
        assert listed["ok"] and [d["device_id"] for d in listed["devices"]] == ["dev-1"]
        assert listed["credits"] == {"balance": 0}
        assert (await alice.call("gadgets_get_state", device_id="dev-1"))["ok"]
        assert (await alice.call("gadgets_read_events"))["ok"]
        assert (await alice.call("gadgets_diagnostics"))["ok"]
        assert running.credits.balance(account_of(running, "dev-1")) == 0


async def test_granted_credits_are_spent_one_per_command_then_refused(tmp_path):
    async with AsyncExitStack() as stack:
        running, alice, _, seen = await two_people(stack, tmp_path)
        a = account_of(running, "dev-1")
        running.credits.grant(a, 2)
        first = await command(alice, "dev-1")
        second = await command(alice, "dev-1")
        assert first["command"]["status"] == second["command"]["status"] == "executed"
        assert error(await command(alice, "dev-1")) == "out_of_credits"
        assert len(seen["dev-1"]) == 2
        running.credits.grant(a, 1)  # a top-up works at once
        assert (await command(alice, "dev-1"))["ok"]
        assert running.credits.balance(a) == 0


async def test_one_account_never_sees_another_accounts_balance(tmp_path):
    async with AsyncExitStack() as stack:
        running, alice, bob, _ = await two_people(stack, tmp_path)
        running.credits.grant(account_of(running, "dev-1"), 7)
        running.credits.grant(account_of(running, "dev-2"), 3)
        assert (await alice.call("gadgets_list_devices"))["credits"] == {"balance": 7}
        assert (await bob.call("gadgets_list_devices"))["credits"] == {"balance": 3}
        for tool in ("gadgets_diagnostics", "gadgets_read_events"):
            assert "7" not in json.dumps((await bob.call(tool)).get("credits", ""))
        # Alice cannot spend on Bob's gadget, and the attempt costs her nothing.
        assert error(await command(alice, "dev-2")) == "unknown_device"
        assert running.credits.balance(account_of(running, "dev-1")) == 7


async def test_refused_or_repeated_commands_are_not_charged(tmp_path):
    async with AsyncExitStack() as stack:
        running, alice, _, seen = await two_people(stack, tmp_path)
        a = account_of(running, "dev-1")
        running.credits.grant(a, 3)
        bad = await alice.call(
            "gadgets_command", device_id="dev-1", capability="no.such", arguments={}
        )
        assert error(bad) == "unsupported_capability"
        assert running.credits.balance(a) == 3
        sent = await command(alice, "dev-1", command_id="retry-1")
        again = await command(alice, "dev-1", command_id="retry-1")
        assert sent["ok"] and again["command"]["duplicate"] is True
        assert running.credits.balance(a) == 2 and len(seen["dev-1"]) == 1
        # A retry of a known command ID is answered even at zero.
        running.credits.grant(a, -2)
        assert (await command(alice, "dev-1", command_id="retry-1"))["command"]["duplicate"]


async def test_request_records_carry_the_charge_for_reconciliation(tmp_path):
    async with AsyncExitStack() as stack:
        running, alice, _, _ = await two_people(stack, tmp_path)
        a = account_of(running, "dev-1")
        running.credits.grant(a, 1)
        await command(alice, "dev-1")
        await command(alice, "dev-1")
    records = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    calls = [r for r in records if r.get("tool") == "gadgets_command"]
    assert [(c["outcome"], c.get("credits")) for c in calls] == [
        ("ok", 1),
        ("out_of_credits", None),
    ]
    assert all(c["account"] == a for c in calls)
    debits = [
        e
        for e in read_ledger(tmp_path / "oauth" / "credits-ledger.jsonl")
        if e["kind"] == "command"
    ]
    assert [d["request_id"] for d in debits] == [calls[0]["request_id"]]


async def test_dropped_usage_hook_lines_never_change_balances(tmp_path):
    hook = UsageHook(["/nonexistent/hook"], queue_size=1)  # it never starts: every line drops
    async with AsyncExitStack() as stack:
        running, alice, _, _ = await two_people(stack, tmp_path, usage_hook=hook)
        a = account_of(running, "dev-1")
        running.credits.grant(a, 3)
        for _ in range(3):
            assert (await command(alice, "dev-1"))["ok"]
        assert error(await command(alice, "dev-1")) == "out_of_credits"
        assert running.credits.balance(a) == 0
    assert hook.close()["sent"] == 0


async def test_credits_off_changes_nothing(tmp_path):
    async with AsyncExitStack() as stack:
        running, alice, _, _ = await two_people(stack, tmp_path, credits=False)
        assert running.credits is None
        listed = await alice.call("gadgets_list_devices")
        assert "credits" not in listed
        for _ in range(3):
            assert (await command(alice, "dev-1"))["ok"]
    assert not (tmp_path / "oauth" / "credits.json").exists()
    assert not (tmp_path / "oauth" / "credits-ledger.jsonl").exists()
    for line in (tmp_path / "requests.jsonl").read_text().splitlines():
        record = json.loads(line)
        if record.get("event") == "mcp_tool_call":
            assert "credits" not in record and "account" not in record


async def test_credits_need_accounts(tmp_path):
    atomic_write(tmp_path / "mcp-token", b"mcp-test-token-value\n")
    with pytest.raises(OperatorError, match="--credits needs --accounts"):
        async with serve_gateway(
            None,
            tmp_path / "mcp-token",
            simulator=True,
            request_log=tmp_path / "r.jsonl",
            oauth=True,
            oauth_issuer="http://localhost:1",
            credits=True,
        ):
            pass


async def test_stdio_and_operator_mode_are_never_gated():
    from test_command_wait import device
    from test_domain import hello

    from grok_gadgets_gateway.domain import Gateway
    from grok_gadgets_gateway.mcp_server import make_server

    gateway = Gateway()
    session = gateway.register(hello("dev-1"))
    server = make_server(gateway)
    worker = asyncio.create_task(device(gateway, session))
    result = await server.call_tool(
        "gadgets_command", {"device_id": "dev-1", "capability": "rgb.set", "arguments": RGB}
    )
    await worker
    body = result.structured_content or json.loads(result.content[0].text)
    assert body["ok"] and body["command"]["status"] == "executed"
    listed = await server.call_tool("gadgets_list_devices", {})
    assert "credits" not in (listed.structured_content or json.loads(listed.content[0].text))
    with pytest.raises(ValueError):
        make_server(gateway, credits=object())
