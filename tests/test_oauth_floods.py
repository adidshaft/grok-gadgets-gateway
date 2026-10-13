"""Strangers cannot lock the owner out of sign-in (hub ADR 008, gateway #142)."""

import time

import httpx2
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from test_http import ForcedHost, free_port
from test_oauth import list_devices, oauth_server, signed_in_client

from grok_gadgets_gateway.oauth import (
    CLIENT_IDLE_SECONDS,
    MAX_CLIENTS,
    MAX_PENDING,
    OwnerApprovedOAuth,
    pending_requests,
)
from grok_gadgets_gateway.service import OAUTH_BUDGETS


class Log:
    def __init__(self):
        self.lines = []

    def write(self, record):
        self.lines.append(record)

    def events(self, name):
        return [line for line in self.lines if line["event"] == name]


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def client(n):
    return OAuthClientInformationFull(
        client_id=f"client-{n}",
        client_name=f"stranger-{n}",
        redirect_uris=["https://bot.example.org/callback"],
    )


def params():
    return AuthorizationParams(
        state="s",
        scopes=["gadgets"],
        code_challenge="x" * 43,
        redirect_uri="https://bot.example.org/callback",
        redirect_uri_provided_explicitly=True,
    )


def provider(tmp_path, clock, log):
    return OwnerApprovedOAuth("https://gadgets.example.net", tmp_path / "oauth", log, clock)


async def test_a_full_client_list_makes_room_but_keeps_a_live_sign_in(tmp_path):
    clock, log = Clock(), Log()
    oauth = provider(tmp_path, clock, log)
    owner = client("owner")
    await oauth.register_client(owner)
    oauth._issue(owner, "grok-bot", ["gadgets"])
    for n in range(MAX_CLIENTS + 5):
        clock.now += 1
        await oauth.register_client(client(n))
    assert len(oauth.state["clients"]) == MAX_CLIENTS
    assert "client-owner" in oauth.state["clients"]
    # The newest strangers stay; the oldest ones without a sign-in were removed.
    assert f"client-{MAX_CLIENTS + 4}" in oauth.state["clients"]
    assert "client-0" not in oauth.state["clients"]
    # One alert line, not one per request.
    assert [line["reason"] for line in log.events("oauth_flood")] == ["clients_full"]


async def test_idle_clients_are_pruned_after_a_restart(tmp_path):
    clock, log = Clock(), Log()
    oauth = provider(tmp_path, clock, log)
    for n in range(MAX_CLIENTS):
        await oauth.register_client(client(n))
    clock.now += CLIENT_IDLE_SECONDS
    # A new process reads the saved list; the next registration prunes it.
    again = provider(tmp_path, clock, log)
    await again.register_client(client("owner"))
    assert list(again.state["clients"]) == ["client-owner"]
    assert log.events("oauth_flood") == []


async def test_waiting_sign_ins_drop_the_oldest_instead_of_refusing(tmp_path):
    clock, log = Clock(), Log()
    oauth = provider(tmp_path, clock, log)
    stranger = client("stranger")
    await oauth.register_client(stranger)
    for _ in range(5):
        await oauth.authorize(stranger, params())
    # One client keeps at most two waiting sign-ins.
    assert len(oauth.requests) == 2
    for n in range(MAX_PENDING):
        await oauth.register_client(client(n))
        await oauth.authorize(client(n), params())
    assert len(oauth.requests) == MAX_PENDING
    owner = client("owner")
    await oauth.register_client(owner)
    await oauth.authorize(owner, params())
    assert any(v["client_id"] == "client-owner" for v in oauth.requests.values())
    codes = {r["code"] for r in pending_requests(tmp_path / "oauth")}
    assert codes == {v["code"] for v in oauth.requests.values()}
    assert [line["reason"] for line in log.events("oauth_flood")] == ["pending_full"]


async def test_a_registration_flood_does_not_stop_a_live_sign_in(tmp_path):
    port = free_port()
    url = f"http://localhost:{port}/mcp"
    async with oauth_server(tmp_path, port):
        auth, storage = signed_in_client(url, tmp_path / "oauth")
        await list_devices(url, auth)
        per_caller, total = OAUTH_BUDGETS["sign_up"]
        async with httpx2.AsyncClient(transport=ForcedHost(f"localhost:{port}")) as http:
            register = f"http://127.0.0.1:{port}/register"

            async def flood(address, count):
                headers = {"X-Forwarded-For": address}
                return [
                    (await http.post(register, json={}, headers=headers)).status_code
                    for _ in range(count)
                ]

            # Each caller is cut off after its own budget.
            first = await flood("203.0.113.1", per_caller + 5)
            assert 429 not in first[:per_caller] and set(first[per_caller:]) == {429}
            second = await flood("203.0.113.2", per_caller + 5)
            assert 429 not in second[:per_caller]
            # Then the total cuts off every caller.
            third = await flood("203.0.113.3", per_caller)
            assert third[0] != 429 and third[-1] == 429
            assert sum(s != 429 for s in first + second + third) <= total
            # Refresh has its own budget, so the signed-in client keeps working.
            storage.tokens.expires_in = 0
            refresh = await http.post(
                f"http://127.0.0.1:{port}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": storage.tokens.refresh_token,
                    "client_id": storage.client.client_id,
                    "client_secret": storage.client.client_secret or "",
                },
            )
            assert refresh.status_code == 200
        lines = (tmp_path / "requests.jsonl").read_text()
        assert lines.count('"oauth_flood"') == 1
