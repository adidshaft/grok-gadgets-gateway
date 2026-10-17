"""GW-CLAIM-002 (hub ADR 011 §6–8): claim more gadgets, share, link and remove."""

import pytest
from test_accounts import CODE, store

from grok_gadgets_gateway.accounts import SHARE_SECONDS, ClaimRejected
from grok_gadgets_gateway.protocol import GatewayError


def claimed(accounts, device_id, caller="a"):
    code = accounts.hello(device_id, "ready")["claim_code"]
    account, _ = accounts.claim_new_account(code, caller=caller)
    return account


def fails(code, action):
    with pytest.raises(GatewayError) as caught:
        action()
    assert caught.value.code == code


def test_a_signed_in_account_claims_another_gadget_with_its_code(tmp_path):
    accounts, _, log = store(tmp_path, devices=())
    owner = claimed(accounts, "kit-1")
    code = accounts.hello("kit-2", "ready")["claim_code"]
    assert accounts.claim(code, account=owner) == {"device_id": "kit-2", "role": "owner"}
    assert accounts.visible(owner) == {"kit-1": "owner", "kit-2": "owner"}
    assert log.events("device_claimed")[-1]["via"] == "tool"


def test_a_share_code_gives_a_scoped_grant_once_and_never_merges_accounts(tmp_path):
    accounts, clock, log = store(tmp_path, devices=())
    owner = claimed(accounts, "kit-1")
    friend = claimed(accounts, "kit-2")
    share = accounts.share_create(owner, "kit-1", "view")
    assert CODE.match(share["share_code"]) and share["expires_in"] == SHARE_SECONDS
    assert accounts.claim(share["share_code"], account=friend) == {
        "device_id": "kit-1",
        "role": "view",
    }
    assert accounts.visible(friend) == {"kit-2": "owner", "kit-1": "view"}
    assert accounts.visible(owner) == {"kit-1": "owner"}  # nothing of the friend's
    with pytest.raises(ClaimRejected):
        accounts.claim(share["share_code"], account=friend)  # one use
    # A grantee cannot share further; a stranger cannot see the gadget at all.
    fails("not_owner", lambda: accounts.share_create(friend, "kit-1", "use"))
    stranger = claimed(accounts, "kit-3")
    fails("unknown_device", lambda: accounts.share_create(stranger, "kit-1", "use"))
    # Expired share codes fail.
    late = accounts.share_create(owner, "kit-1", "use")["share_code"]
    clock.now += SHARE_SECONDS
    with pytest.raises(ClaimRejected):
        accounts.claim(late, account=stranger)
    assert {line["event"] for line in log.lines} >= {"share_created", "share_used"}
    assert all("share_code" not in line for line in log.lines)


def test_the_owner_lists_and_revokes_grants_and_a_grantee_drops_one(tmp_path):
    accounts, _, log = store(tmp_path, devices=())
    owner = claimed(accounts, "kit-1")
    friend = claimed(accounts, "kit-2")
    other = claimed(accounts, "kit-3")
    for account, scope in ((friend, "use"), (other, "view")):
        code = accounts.share_create(owner, "kit-1", scope)["share_code"]
        accounts.claim(code, account=account)
    grants = accounts.shares(owner, "kit-1")
    assert sorted(g["scope"] for g in grants) == ["use", "view"]
    assert all(set(g) == {"share_id", "scope", "created"} for g in grants)  # no account IDs
    use = next(g for g in grants if g["scope"] == "use")
    accounts.share_revoke(owner, "kit-1", use["share_id"])
    assert "kit-1" not in accounts.visible(friend)
    fails("unknown_share", lambda: accounts.share_revoke(owner, "kit-1", use["share_id"]))
    assert accounts.remove(other, "kit-1") == "dropped"
    assert "kit-1" not in accounts.visible(other)
    assert accounts.owner("kit-1") == owner
    assert {"share_revoked", "share_dropped"} <= {line["event"] for line in log.lines}


def test_the_owner_removes_a_gadget_and_it_shows_a_new_code(tmp_path):
    accounts, _, log = store(tmp_path, devices=())
    owner = claimed(accounts, "kit-1")
    friend = claimed(accounts, "kit-2")
    accounts.claim(accounts.share_create(owner, "kit-1", "use")["share_code"], account=friend)
    assert accounts.poll("kit-1") == {"claimed": True}
    assert accounts.remove(owner, "kit-1") == "unclaimed"
    assert accounts.owner("kit-1") is None
    assert "kit-1" not in accounts.visible(friend)  # grants go with the owner
    update = accounts.poll("kit-1")
    assert update["claimed"] is False and CODE.match(update["claim_code"])
    assert log.events("device_unclaimed")[-1] == {
        "event": "device_unclaimed",
        "device": "kit-1",
        "account": owner,
        "via": "tool",
    }
    fails("unknown_device", lambda: accounts.remove(owner, "kit-1"))


def test_a_link_code_signs_a_new_app_in_to_the_same_account(tmp_path):
    accounts, clock, _ = store(tmp_path, devices=())
    owner = claimed(accounts, "kit-1")
    link = accounts.link_code(owner)
    assert CODE.match(link["link_code"])
    assert accounts.sign_in_code(link["link_code"], caller="b") == (owner, None)
    with pytest.raises(ClaimRejected):
        accounts.sign_in_code(link["link_code"], caller="b")  # one use
    late = accounts.link_code(owner)["link_code"]
    clock.now += SHARE_SECONDS
    with pytest.raises(ClaimRejected):
        accounts.sign_in_code(late, caller="b")
    # A link code is not a share or claim code for another signed-in account.
    other = claimed(accounts, "kit-2")
    with pytest.raises(ClaimRejected):
        accounts.claim(accounts.link_code(owner)["link_code"], account=other)


def test_a_share_code_at_sign_in_makes_a_new_account_with_the_grant(tmp_path):
    accounts, _, _ = store(tmp_path, devices=())
    owner = claimed(accounts, "kit-1")
    code = accounts.share_create(owner, "kit-1", "use")["share_code"]
    account, recovery = accounts.sign_in_code(code, caller="c")
    assert account != owner and recovery
    assert accounts.visible(account) == {"kit-1": "use"}


def test_wrong_codes_from_one_account_are_limited(tmp_path):
    accounts, _, _ = store(tmp_path, devices=())
    owner = claimed(accounts, "kit-1")
    live = accounts.hello("kit-2", "ready")["claim_code"]
    for _ in range(10):
        with pytest.raises(ClaimRejected):
            accounts.claim("CCCC-CCCC", account=owner)
    with pytest.raises(ClaimRejected) as caught:
        accounts.claim(live, account=owner)
    assert caught.value.limited and accounts.owner("kit-2") is None


def test_the_operator_account_sees_only_its_own_gadgets(tmp_path):
    accounts, _, _ = store(tmp_path, devices=("kit-1",))
    other = claimed(accounts, "kit-2")
    assert accounts.visible(accounts.operator) == {"kit-1": "owner"}
    assert accounts.visible(other) == {"kit-2": "owner"}
