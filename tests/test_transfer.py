"""GW-CLAIM-004 (hub ADR 011 §7): unclaim, transfer after a reset, grace period, audit lines."""

import json

import pytest
from test_accounts import CODE, store
from test_accounts_share import claimed, fails

from grok_gadgets_gateway.accounts import (
    CLAIM_SECONDS,
    TRANSFER_GRACE_SECONDS,
    Accounts,
    ClaimRejected,
)


def reset_and_rejoin(accounts, device_id="kit-1"):
    """A BOOT reset and a phone re-join: the first hello says after_setup."""
    accounts.disconnect(device_id)
    return accounts.hello(device_id, "after_setup")


def test_only_the_first_hello_after_a_reset_offers_a_transfer_code(tmp_path):
    accounts, _, _ = store(tmp_path, devices=())
    old = claimed(accounts, "kit-1")
    accounts.disconnect("kit-1")
    assert accounts.hello("kit-1", "ready") == {"claimed": True}  # a plain restart
    fields = reset_and_rejoin(accounts)
    assert fields["claimed"] is True and CODE.match(fields["claim_code"])
    assert accounts.owner("kit-1") == old  # a reset alone changes nothing


def test_a_transfer_waits_for_the_old_owner_to_unclaim(tmp_path):
    accounts, _, log = store(tmp_path, devices=())
    old = claimed(accounts, "kit-1")
    friend = claimed(accounts, "kit-9")
    accounts.claim(accounts.share_create(old, "kit-1", "use")["share_code"], account=friend)
    new = claimed(accounts, "kit-2")
    code = reset_and_rejoin(accounts)["claim_code"]
    assert accounts.claim(code, account=new) == {"device_id": "kit-1", "role": "pending_transfer"}
    # Pending: the old owner keeps full use; the new person sees only the pending transfer.
    assert accounts.owner("kit-1") == old
    assert accounts.visible(new) == {"kit-2": "owner", "kit-1": "pending_transfer"}
    pending = accounts.pending_transfer("kit-1")
    assert pending["completes_at"] == pending["since"] + TRANSFER_GRACE_SECONDS
    assert accounts.poll("kit-1") == {"claimed": True}  # the code is gone from the display
    with pytest.raises(ClaimRejected):
        accounts.claim(code, account=new)  # one use
    # The old owner unclaims: the gadget moves at once; old shares end.
    assert accounts.remove(old, "kit-1") == "transferred"
    assert accounts.owner("kit-1") == new
    assert accounts.visible(new)["kit-1"] == "owner"
    assert "kit-1" not in accounts.visible(old) and "kit-1" not in accounts.visible(friend)
    assert log.events("transfer_pending")[-1] == {
        "event": "transfer_pending",
        "device": "kit-1",
        "account": new,
        "owner": old,
    }
    assert log.events("transfer_done")[-1]["via"] == "unclaim"


def test_a_transfer_completes_after_the_grace_period(tmp_path):
    accounts, clock, log = store(tmp_path, devices=())
    old = claimed(accounts, "kit-1")
    new = claimed(accounts, "kit-2")
    accounts.claim(reset_and_rejoin(accounts)["claim_code"], account=new)
    clock.now += TRANSFER_GRACE_SECONDS - 1
    assert accounts.owner("kit-1") == old
    clock.now += 1
    assert accounts.owner("kit-1") == new
    assert log.events("transfer_done")[-1]["via"] == "grace_period"
    # It survives a restart of the relay.
    again = Accounts(tmp_path / "accounts.json", clock=clock).open(())
    assert again.owner("kit-1") == new


def test_the_old_owner_refuses_a_transfer(tmp_path):
    accounts, clock, log = store(tmp_path, devices=())
    old = claimed(accounts, "kit-1")
    new = claimed(accounts, "kit-2")
    accounts.claim(reset_and_rejoin(accounts)["claim_code"], account=new)
    fails("not_owner", lambda: accounts.refuse_transfer(new, "kit-1"))
    accounts.refuse_transfer(old, "kit-1")
    assert "kit-1" not in accounts.visible(new)
    clock.now += TRANSFER_GRACE_SECONDS
    assert accounts.owner("kit-1") == old
    fails("no_transfer", lambda: accounts.refuse_transfer(old, "kit-1"))
    assert log.events("transfer_refused")[-1]["device"] == "kit-1"


def test_no_second_transfer_while_one_is_pending_and_codes_expire(tmp_path):
    accounts, clock, _ = store(tmp_path, devices=())
    old = claimed(accounts, "kit-1")
    late = reset_and_rejoin(accounts)["claim_code"]
    clock.now += CLAIM_SECONDS
    assert accounts.poll("kit-1") == {"claimed": True}  # not refreshed: a new reset is needed
    stranger = claimed(accounts, "kit-3")
    with pytest.raises(ClaimRejected):
        accounts.claim(late, account=stranger)
    new = claimed(accounts, "kit-2")
    accounts.claim(reset_and_rejoin(accounts)["claim_code"], account=new)
    assert reset_and_rejoin(accounts) == {"claimed": True}
    assert accounts.owner("kit-1") == old


def test_the_owner_cannot_transfer_to_itself_and_a_sign_in_can_start_a_transfer(tmp_path):
    accounts, _, _ = store(tmp_path, devices=())
    old = claimed(accounts, "kit-1")
    code = reset_and_rejoin(accounts)["claim_code"]
    with pytest.raises(ClaimRejected):
        accounts.claim(code, account=old)
    code = reset_and_rejoin(accounts)["claim_code"]
    new, recovery = accounts.sign_in_code(code, caller="x")
    assert recovery and accounts.visible(new) == {"kit-1": "pending_transfer"}


def test_operator_commands_reach_a_running_relay_through_files(tmp_path):
    accounts, _, log = store(tmp_path, devices=("kit-1",))
    other = claimed(accounts, "kit-2")
    admin = tmp_path / "admin"
    from grok_gadgets_gateway.accounts import queue_admin

    queue_admin(admin, {"action": "unclaim", "device": "kit-2"})
    queue_admin(admin, {"action": "assign", "device": "kit-3", "account": other})
    code = queue_admin(admin, {"action": "link", "account": other})["code"]
    assert accounts.apply_admin(admin) == 3 and not list(admin.glob("*.json"))
    assert accounts.owner("kit-2") is None and accounts.owner("kit-3") == other
    assert accounts.sign_in_code(code, caller="op") == (other, None)
    assert {line["via"] for line in log.events("device_unclaimed")} == {"operator"}
    # A bad request is dropped with a line, never applied.
    queue_admin(admin, {"action": "assign", "device": "kit-4", "account": "acct_missing"})
    assert accounts.apply_admin(admin) == 0
    assert log.events("admin_rejected")
    assert code not in json.dumps(log.lines)


def test_accounts_cli_unclaim_assign_and_link(tmp_path, capsys, private_home):
    from grok_gadgets_gateway.cli import main
    from grok_gadgets_gateway.oauth import oauth_dir

    accounts = Accounts(oauth_dir() / "accounts.json")
    accounts.open(["kit-1"])
    capsys.readouterr()
    assert main(["accounts", "unclaim", "kit-1", "--no-wait"]) == 0
    assert main(["accounts", "link", accounts.operator, "--no-wait"]) == 0
    out, err = capsys.readouterr()
    assert CODE.search(out) and "10 minutes" in err
    assert accounts.apply_admin(oauth_dir() / "admin") == 2
    assert accounts.owner("kit-1") is None
    with pytest.raises(SystemExit):
        main(["accounts", "assign", "kit-1", "not-an-account", "--no-wait"])
