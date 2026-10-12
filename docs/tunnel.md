# Owner-run TLS tunnel

This guide puts an HTTPS name that you control in front of `serve`. Grok Bot can then reach the gateway as a **Remote HTTPS** MCP server. You run the tunnel. The project runs no URL, relay or account for you.

**Status: untested with Grok Bot.** The route comes from [ADR 004](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/decisions/004-grok-bot-route.md). In the 12 October 2026 test, Grok Bot reached the gateway only through approved local `rehearse` commands. The owner test of this tunnel route is [HUB-GROK-REMOTE-TEST-001](https://github.com/adidshaft/grok-gadgets/issues/154). Until it records a result, treat this guide as a design.

```text
Grok Bot ──HTTPS──▶ gadgets.example.net (your tunnel) ──▶ 127.0.0.1:8766 /mcp (serve)
                                                          127.0.0.1:8765 devices: never tunnelled
```

## What you need

- The gateway with named tokens and limits (`grok-gadgets-gateway mcp-token --help` works).
- A TLS tunnel client and a hostname that you control. The client must end HTTPS at that hostname and forward to `http://127.0.0.1:8766`. It must not forward any other port.
- A simulated light first. Connect a real device only after the simulator works.

In the commands, replace `gadgets.example.net` with your hostname.

## 1. Issue a token for Grok Bot

```sh
grok-gadgets-gateway mcp-token issue grok-bot --token-file ~/.config/grok-gadgets/grok-bot.token
```

Expected: `Wrote the grok-bot token to …/grok-bot.token (mode 0600).` Only a hash is stored in `mcp-tokens.json`.

## 2. Start the gateway for that hostname

```sh
grok-gadgets-gateway serve --simulator --allowed-host gadgets.example.net \
  --request-log ~/grok-gadgets-requests.jsonl
```

Expected: `MCP http://127.0.0.1:8766/mcp on 127.0.0.1`. The gateway still listens on loopback only. `--allowed-host` lets requests with that Host header through. Requests with any other Host get HTTP 421.

The default [limits](remote-access.md#limits) apply per token: 60 tool calls and 10 commands a minute, 8 open requests, 64 KiB bodies and 30 seconds per request.

## 3. Check the gateway before you open the tunnel

These requests go to loopback with your hostname in the Host header, as the tunnel will send them:

```sh
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8766/mcp \
  -H "Host: gadgets.example.net" -H "Content-Type: application/json" -d '{}'
```

Expected: `401`. A request without the token is refused.

```sh
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8766/mcp \
  -H "Host: gadgets.example.net" \
  -H "Authorization: Bearer $(cat ~/.config/grok-gadgets/grok-bot.token)" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"tunnel-check","version":"1"}}}'
```

Expected: `200`. The request log shows no tool call yet, because `initialize` is not a tool call.

```sh
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8766/mcp \
  -H "Host: wrong.example.org" -H "Content-Type: application/json" -d '{}'
```

Expected: `401`. With a valid token and a wrong Host, the answer is `421`.

## 4. Open the tunnel

Start your tunnel client so that `https://gadgets.example.net` forwards to `http://127.0.0.1:8766`. Follow your tunnel provider's own guide. Then repeat the first check from another network, for example a phone hotspot:

```sh
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://gadgets.example.net/mcp \
  -H "Content-Type: application/json" -d '{}'
```

Expected: `401`. If you get anything else, stop the tunnel and check its target.

## 5. Add the server in Grok Bot

Add a **Remote HTTPS** MCP server:

- URL: `https://gadgets.example.net/mcp`
- Header: `Authorization: Bearer <contents of grok-bot.token>`

Then ask: "list my gadgets", "set the light blue", "what's its state?". The request log must show `mcp_tool_call` lines with `"token":"grok-bot"` and a client other than `grok-gadgets-rehearse`. If no lines appear, Grok Bot did not reach the gateway, whatever it says.

## Turn it off and revoke

1. Revoke the token. It fails on its next request, also inside an open session:

   ```sh
   grok-gadgets-gateway mcp-token revoke grok-bot
   ```

2. Stop the tunnel client.
3. Stop `serve` with Ctrl+C.
4. Remove the MCP server from Grok Bot.
5. Delete the token file: `rm ~/.config/grok-gadgets/grok-bot.token`.

To shut off all remote access at once, stop the tunnel or `serve`.

## Security checklist

- Only `127.0.0.1:8766` goes through the tunnel. Never forward the device port 8765.
- Each client has its own named token. The token is in a header, never in the URL.
- `--allowed-host` names only your tunnel hostname.
- The request log is on, and you read it after each session.
- The token, the log and screenshots of your account stay private.
- The tunnel is on only while you use it.

A tunnel makes the gateway reachable. Authentication comes from the bearer token, and the limits bound the damage from a leaked one. This is not OAuth; per-person sign-in is [GW-OAUTH-001](https://github.com/adidshaft/grok-gadgets-gateway/issues/71).
