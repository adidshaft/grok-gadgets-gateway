"""GW-CLAIM-004: `rehearse --accounts` runs two accounts against a relay in accounts mode."""

import asyncio

import pytest

from grok_gadgets_gateway import rehearse
from grok_gadgets_gateway.cli import main
from grok_gadgets_gateway.rehearse import RehearsalError, _Gadget, rehearse_accounts


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


class _Reader:
    """A relay connection whose next reply the test hands over itself."""

    def __init__(self):
        self.reply = asyncio.get_running_loop().create_future()
        self.waiting = asyncio.Event()

    async def readline(self):
        self.waiting.set()
        return await self.reply


class _Writer:
    closed = False

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True


async def test_a_gadget_stops_when_a_reply_and_the_stop_arrive_together():
    # GW-SCALE-002 (#260): on Python 3.11, asyncio.wait_for returned this reply and dropped
    # the cancellation sent in the same loop step, so the poll loop ran on and stop() hung.
    gadget = _Gadget(0, "gadget-a")
    gadget.writer = writer = _Writer()
    reader = _Reader()
    gadget.task = asyncio.create_task(gadget._serve(reader))
    await reader.waiting.wait()
    reader.reply.set_result(b'{"ok": true}\n')
    async with asyncio.timeout(2):
        await gadget.stop()
    assert gadget.task is None and writer.closed


async def test_a_stalled_account_rehearsal_fails_with_a_clear_message(tmp_path, monkeypatch):
    monkeypatch.setattr(rehearse, "ACCOUNTS_SECONDS", 0.5)

    async def stalled(out, directory):
        await asyncio.sleep(3600)

    monkeypatch.setattr(rehearse, "_rehearse_accounts", stalled)
    with pytest.raises(RehearsalError, match=r"did not finish within 0\.5 s"):
        await rehearse_accounts(directory=tmp_path)
