"""GW-CLAIM-004: `rehearse --accounts` runs two accounts against a relay in accounts mode."""

from grok_gadgets_gateway.cli import main
from grok_gadgets_gateway.rehearse import rehearse_accounts


async def test_two_accounts_claim_share_revoke_and_transfer(tmp_path):
    lines = []
    await rehearse_accounts(lines.append, directory=tmp_path)
    text = "\n".join(lines)
    for step in (
        "A signed in with the code gadget-a showed",
        "B signed in with the code gadget-b showed",
        "A lists only gadget-a",
        "A cannot read, command or see events of gadget-b",
        "A cannot read B's command status",
        "a wrong code is refused",
        "B shared gadget-b; A commanded it",
        "B revoked the share; A lost gadget-b",
        "after a reset gadget-b showed a transfer code; A's claim is pending",
        "B removed gadget-b; A owns it and commanded it",
    ):
        assert f"ok  {step}" in text, step
    assert "Traceback" not in text


def test_the_cli_runs_the_account_rehearsal(tmp_path, capsys, private_home):
    assert main(["rehearse", "--accounts"]) == 0
    out = capsys.readouterr().out
    assert "Account rehearsal passed" in out and "not prove" in out
