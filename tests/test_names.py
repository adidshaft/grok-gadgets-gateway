"""GW-NAME-001: gadgets get plain names and rooms per account; tools accept the name.

Asked for in the Phase 11 acceptance on 17 October 2026: "tag the gadgets in Grok Bot or rename
them in simple language".
"""

import asyncio
import json
from contextlib import AsyncExitStack

from test_accounts import store
from test_accounts_share import claimed, fails
from test_domain import hello
from test_scoped_tools import TOKENS, error, two_people

from grok_gadgets_gateway.accounts import TRANSFER_GRACE_SECONDS

# --- the store ---


def test_each_account_keeps_its_own_names(tmp_path):
    accounts, _, log = store(tmp_path, devices=())
    owner = claimed(accounts, "kit-1")
    friend = claimed(accounts, "kit-2")
    accounts.claim(accounts.share_create(owner, "kit-1", "view")["share_code"], account=friend)
    assert accounts.set_label(owner, "kit-1", "Desk display", "Office") == {
        "device_id": "kit-1",
        "name": "Desk display",
        "room": "Office",
    }
    accounts.set_label(friend, "kit-1", "Their screen")
    assert accounts.label(owner, "kit-1") == {"name": "Desk display", "room": "Office"}
    assert accounts.label(friend, "kit-1") == {"name": "Their screen", "room": None}
    # Names resolve within the caller's own gadgets only, case and spaces aside.
    assert accounts.resolve(owner, "  desk DISPLAY ") == "kit-1"
    assert accounts.resolve(owner, "kit-1") == "kit-1"
    assert accounts.resolve(friend, "Desk display") == "Desk display"  # not theirs: unchanged
    assert accounts.resolve(owner, "Their screen") == "Their screen"
    assert log.events("gadget_named")[-1] == {
        "event": "gadget_named",
        "device": "kit-1",
        "account": friend,
    }


def test_names_are_checked_and_unique_per_account(tmp_path):
    accounts, _, _ = store(tmp_path, devices=())
    owner = claimed(accounts, "kit-1")
    other = claimed(accounts, "kit-3")
    accounts.assign("kit-2", owner, via="operator")
    accounts.set_label(owner, "kit-1", "Lamp")
    fails("name_taken", lambda: accounts.set_label(owner, "kit-2", "lamp"))
    accounts.set_label(other, "kit-3", "Lamp")  # another account may use the same name
    for bad in ("", " ", "x" * 33, "tab\there", "kit-2"):
        fails("invalid_name", lambda bad=bad: accounts.set_label(owner, "kit-1", bad))
    fails("invalid_name", lambda: accounts.set_label(owner, "kit-1", "Lamp", "r" * 33))
    fails("unknown_device", lambda: accounts.set_label(other, "kit-1", "Mine now"))
    # Clearing the name keeps nothing.
    accounts.set_label(owner, "kit-1", None)
    assert accounts.label(owner, "kit-1") == {"name": None, "room": None}


def test_names_go_with_the_gadget_when_it_changes_hands(tmp_path):
    accounts, clock, _ = store(tmp_path, devices=())
    owner = claimed(accounts, "kit-1")
    accounts.set_label(owner, "kit-1", "Desk display")
    new = claimed(accounts, "kit-2")
    accounts.disconnect("kit-1")
    code = accounts.hello("kit-1", "after_setup")["claim_code"]
    accounts.claim(code, account=new)
    clock.now += TRANSFER_GRACE_SECONDS
    assert accounts.owner("kit-1") == new
    # The old owner's name for it is gone; the new owner names it themselves.
    assert accounts.label(owner, "kit-1") == {"name": None, "room": None}
    assert accounts.resolve(owner, "Desk display") == "Desk display"


# --- the tools ---


async def test_names_and_rooms_through_the_tools(tmp_path):
    async with AsyncExitStack() as stack:
        _, alice, bob, _, _, _ = await two_people(stack, tmp_path)
        named = await alice.call(
            "gadgets_name", device_id="dev-1", name="Desk display", room="Office"
        )
        assert named["ok"] and named["name"] == "Desk display"
        listed = (await alice.call("gadgets_list_devices"))["devices"][0]
        assert listed["name"] == "Desk display" and listed["room"] == "Office"
        assert listed["summary"] == "Desk display, Office, online, yours"
        # Every tool takes the name where it took the device ID.
        state = await alice.call("gadgets_get_state", device_id="desk display")
        assert state["ok"] and state["device"]["device_id"] == "dev-1"
        sent = await alice.call(
            "gadgets_command",
            device_id="Desk display",
            capability="rgb.set",
            arguments={"r": 1, "g": 1, "b": 1, "on": True},
        )
        assert sent["ok"] and sent["command"]["device_id"] == "dev-1"
        assert (await alice.call("gadgets_read_events", device_id="Desk display"))["ok"]
        # Bob cannot use Alice's name to reach her gadget, and cannot name it.
        assert error(await bob.call("gadgets_get_state", device_id="Desk display")) == (
            "unknown_device"
        )
        assert error(await bob.call("gadgets_name", device_id="dev-1", name="Mine")) == (
            "unknown_device"
        )
        # An unnamed gadget still lists a summary with its ID.
        bob_listed = (await bob.call("gadgets_list_devices"))["devices"][0]
        assert bob_listed["name"] is None and bob_listed["summary"].startswith("dev-2, ")


async def after_setup_code(device_port, device_id):
    """A reset and a phone re-join: a new session whose hello says after_setup."""
    reader, writer = await asyncio.open_connection("127.0.0.1", device_port)
    message = hello(device_id)
    message.update(token=TOKENS[device_id], claim="after_setup")
    writer.write((json.dumps(message) + "\n").encode())
    await writer.drain()
    return writer, json.loads(await asyncio.wait_for(reader.readline(), 3))["claim_code"]


async def test_a_pending_transfer_says_so(tmp_path):
    async with AsyncExitStack() as stack:
        _, alice, _, _, _, device_port = await two_people(stack, tmp_path)
        writer, code = await after_setup_code(device_port, "dev-2")
        stack.callback(writer.close)
        assert (await alice.call("gadgets_claim", code=code))["role"] == "pending_transfer"
        rgb = {"r": 1, "g": 1, "b": 1, "on": True}
        for tool, arguments in (
            ("gadgets_get_state", {"device_id": "dev-2"}),
            ("gadgets_command", {"device_id": "dev-2", "capability": "rgb.set", "arguments": rgb}),
        ):
            refused = await alice.call(tool, **arguments)
            assert error(refused) == "transfer_pending"
            assert "share" not in refused["error"]["message"]
        listed = (await alice.call("gadgets_list_devices"))["devices"]
        pending = next(d for d in listed if d["device_id"] == "dev-2")
        assert pending["summary"].startswith("dev-2, ") and "waiting" in pending["summary"]


async def test_the_server_teaches_grok_bot_the_flow(tmp_path):
    async with AsyncExitStack() as stack:
        _, alice, _, _, _, _ = await two_people(stack, tmp_path)
        info = alice.initialized
        assert info.server_info.title == "Grok Gadgets"
        for phrase in ("code", "gadgets_claim", "gadgets_name", "recovery_code"):
            assert phrase in info.instructions
