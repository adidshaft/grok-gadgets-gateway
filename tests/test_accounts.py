"""GW-CLAIM-001 (hub ADR 011): the accounts store, claim codes and their limits."""

import json
import re
import stat

import pytest

from grok_gadgets_gateway.accounts import (
    CLAIM_SECONDS,
    Accounts,
    ClaimRejected,
    normal_code,
)

CODE = re.compile(r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
RECOVERY = re.compile(r"^(?:[A-HJ-NP-Z2-9]{4}-){3}[A-HJ-NP-Z2-9]{4}$")
ACCOUNT = re.compile(r"^acct_[a-z2-7]{26}$")


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class Log:
    def __init__(self):
        self.lines = []

    def write(self, record):
        self.lines.append(record)

    def events(self, name):
        return [line for line in self.lines if line["event"] == name]


def store(tmp_path, devices=("kit-1",), **kwargs):
    clock = kwargs.pop("clock", None) or Clock()
    log = kwargs.pop("log", None) or Log()
    accounts = Accounts(tmp_path / "accounts.json", log=log, clock=clock, **kwargs)
    accounts.open(devices)
    return accounts, clock, log


def test_first_open_makes_the_operator_account_and_gives_it_every_enrolled_device(tmp_path):
    accounts, _, log = store(tmp_path, devices=("kit-1", "kit-2"))
    operator = accounts.operator
    assert ACCOUNT.match(operator)
    assert accounts.owner("kit-1") == accounts.owner("kit-2") == operator
    path = tmp_path / "accounts.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert log.events("accounts_migrated") == [
        {"event": "accounts_migrated", "operator": operator, "devices": 2}
    ]
    # A later start keeps the operator and does not hand new devices to anyone.
    again, _, _ = store(tmp_path, devices=("kit-1", "kit-2", "kit-3"))
    assert again.operator == operator
    assert again.owner("kit-1") == operator and again.owner("kit-3") is None


def test_only_a_gadget_that_opts_in_gets_claim_fields(tmp_path):
    accounts, _, _ = store(tmp_path, devices=())
    assert accounts.hello("kit-1", None) is None
    fields = accounts.hello("kit-2", "ready")
    assert fields["claimed"] is False and CODE.match(fields["claim_code"])
    accounts.assign("kit-3", accounts.operator, via="operator")
    assert accounts.hello("kit-3", "ready") == {"claimed": True}
    with pytest.raises(ValueError):
        accounts.hello("kit-4", "please")


def test_a_code_lives_ten_minutes_and_a_fresh_one_goes_out_in_a_poll_reply(tmp_path):
    accounts, clock, _ = store(tmp_path, devices=())
    first = accounts.hello("kit-1", "ready")["claim_code"]
    assert accounts.poll("kit-1") is None  # nothing changed
    clock.now += CLAIM_SECONDS - 1
    assert accounts.poll("kit-1") is None
    clock.now += 1
    update = accounts.poll("kit-1")
    assert update["claimed"] is False and update["claim_code"] != first
    with pytest.raises(ClaimRejected):
        accounts.claim_new_account(first, caller="198.51.100.7")
    accounts.claim_new_account(update["claim_code"], caller="198.51.100.7")


def test_a_disconnect_ends_the_code(tmp_path):
    accounts, _, _ = store(tmp_path, devices=())
    code = accounts.hello("kit-1", "ready")["claim_code"]
    accounts.disconnect("kit-1")
    with pytest.raises(ClaimRejected):
        accounts.claim_new_account(code, caller="198.51.100.7")
    assert accounts.poll("kit-1") is None


def test_a_claim_creates_an_account_once_and_the_gadget_learns_it(tmp_path):
    accounts, _, log = store(tmp_path, devices=())
    code = accounts.hello("kit-1", "ready")["claim_code"]
    account, recovery = accounts.claim_new_account(code.lower().replace("-", ""), caller="a")
    assert ACCOUNT.match(account) and account != accounts.operator
    assert RECOVERY.match(recovery)
    assert accounts.owner("kit-1") == account
    assert accounts.poll("kit-1") == {"claimed": True}
    with pytest.raises(ClaimRejected):
        accounts.claim_new_account(code, caller="a")  # one use
    assert [line["account"] for line in log.events("account_created")] == [account]
    assert log.events("device_claimed") == [
        {"event": "device_claimed", "device": "kit-1", "account": account, "via": "sign_in"}
    ]
    # Nothing secret reaches the file or the log.
    text = (tmp_path / "accounts.json").read_text() + json.dumps(log.lines)
    assert code not in text and recovery not in text and recovery.replace("-", "") not in text


def test_a_claimed_gadget_offers_no_code_that_could_open_its_owners_account(tmp_path):
    accounts, _, _ = store(tmp_path, devices=())
    code = accounts.hello("kit-1", "ready")["claim_code"]
    owner, _ = accounts.claim_new_account(code, caller="a")
    accounts.disconnect("kit-1")
    assert accounts.hello("kit-1", "ready") == {"claimed": True}
    assert accounts.owner("kit-1") == owner


def test_a_recovery_code_opens_its_account_once_and_is_replaced(tmp_path):
    accounts, _, log = store(tmp_path, devices=())
    code = accounts.hello("kit-1", "ready")["claim_code"]
    account, recovery = accounts.claim_new_account(code, caller="a")
    again, replacement = accounts.recover(recovery, caller="a")
    assert again == account and replacement != recovery and RECOVERY.match(replacement)
    with pytest.raises(ClaimRejected):
        accounts.recover(recovery, caller="a")
    assert accounts.recover(replacement, caller="a")[0] == account
    assert log.events("account_recovered")[0]["account"] == account


def test_wrong_codes_get_one_answer_and_the_limits_hold(tmp_path):
    accounts, clock, log = store(tmp_path, devices=())
    live = accounts.hello("kit-1", "ready")["claim_code"]
    messages = set()
    for _ in range(20):
        with pytest.raises(ClaimRejected) as caught:
            accounts.claim_new_account("AAAA-AAAA", caller="198.51.100.7")
        messages.add(str(caught.value))
    assert messages == {"That code did not work."}
    # The 21st wrong code in an hour from one address is refused even when it is right.
    with pytest.raises(ClaimRejected) as caught:
        accounts.claim_new_account(live, caller="198.51.100.7")
    assert caught.value.limited
    assert accounts.owner("kit-1") is None
    clock.now += 3600
    live = accounts.poll("kit-1")["claim_code"]  # the old code expired meanwhile
    accounts.claim_new_account(live, caller="198.51.100.7")
    lines = log.events("claim_rejected")
    assert lines and all(set(line) == {"event", "reason", "caller"} for line in lines)


def test_a_relay_wide_flood_closes_code_entry_for_ten_minutes(tmp_path):
    accounts, clock, log = store(tmp_path, devices=())
    live = accounts.hello("kit-1", "ready")["claim_code"]
    for number in range(200):
        with pytest.raises(ClaimRejected):
            accounts.claim_new_account("BBBB-BBBB", caller=f"203.0.113.{number % 250}")
    assert len(log.events("claim_flood")) == 1
    with pytest.raises(ClaimRejected) as caught:
        accounts.claim_new_account(live, caller="198.51.100.9")
    assert caught.value.limited
    clock.now += 61
    with pytest.raises(ClaimRejected):
        accounts.claim_new_account(live, caller="198.51.100.9")
    assert len(log.events("claim_flood")) == 2  # one line a minute while closed
    clock.now += 600
    live = accounts.poll("kit-1")["claim_code"]
    assert accounts.claim_new_account(live, caller="198.51.100.9")


def test_normal_code_accepts_phone_typing():
    assert normal_code(" abcd-efgh ") == "ABCD-EFGH"
    assert normal_code("abcd efgh") == "ABCD-EFGH"
    assert normal_code("ABCD-EFGH-JKLM-NPQR") == "ABCD-EFGH-JKLM-NPQR"
    assert normal_code("ABCD-EFG0") is None  # 0 is not in the alphabet
    assert normal_code("ABC") is None


def test_the_service_runs_serve_with_accounts():
    from grok_gadgets_gateway.background import serve_arguments

    args = serve_arguments(None, 8766, 8765, "/logs/r.jsonl", ["gadgets.example.net"], True)
    assert "--accounts" not in args
    args = serve_arguments(
        None, 8766, 8765, "/logs/r.jsonl", ["gadgets.example.net"], True, accounts=True
    )
    assert args[args.index("--oauth") + 1] == "--accounts"


def test_accounts_list_shows_accounts_and_their_gadgets_without_secrets(
    tmp_path, capsys, private_home
):
    from grok_gadgets_gateway.cli import main
    from grok_gadgets_gateway.oauth import oauth_dir

    assert main(["accounts", "list"]) == 0
    assert "No accounts yet" in capsys.readouterr().err
    accounts = Accounts(oauth_dir() / "accounts.json")
    accounts.open(["kit-1", "kit-2"])
    code = accounts.hello("kit-3", "ready")["claim_code"]
    account, recovery = accounts.claim_new_account(code, caller="a")
    capsys.readouterr()
    assert main(["accounts", "list"]) == 0
    out = capsys.readouterr().out
    assert f"{accounts.operator}  operator  2 gadgets" in out
    assert f"{account}  -  1 gadget" in out
    assert recovery not in out and code not in out


def test_a_limited_attempt_is_logged_once_a_minute(tmp_path):
    accounts, clock, log = store(tmp_path, devices=())
    for _ in range(20):
        with pytest.raises(ClaimRejected):
            accounts.claim_new_account("EEEE-EEEE", caller="198.51.100.20")
    for _ in range(3):
        with pytest.raises(ClaimRejected):
            accounts.claim_new_account("EEEE-EEEE", caller="198.51.100.20")
    assert log.events("claim_limited") == [
        {"event": "claim_limited", "reason": "caller_limit", "caller": "198.51.100.20"}
    ]
    clock.now += 61
    with pytest.raises(ClaimRejected):
        accounts.claim_new_account("EEEE-EEEE", caller="198.51.100.20")
    assert len(log.events("claim_limited")) == 2
