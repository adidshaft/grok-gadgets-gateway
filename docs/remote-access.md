# Remote access

`grok-gadgets-gateway serve` listens on this computer only:

- MCP: `http://127.0.0.1:8766/mcp`
- Devices: `127.0.0.1:8765`

Local `rehearse` sends `Authorization: Bearer <token>`. Grok Bot uses OAuth sign-in; the owner’s personal app has no fixed-header form. The token is the single line in `~/.config/grok-gadgets/mcp-token` (or `$XDG_CONFIG_HOME/grok-gadgets/mcp-token`), mode 0600. `rotate-mcp-token` replaces it and prints `GROK_GADGETS_MCP_TOKEN=...` once on stdout. Update the token in your local test configuration and reconnect. The server reads the file on each request; no gateway restart is needed.

For an HTTPS name in front of `serve`, follow the [owner-run TLS tunnel guide](tunnel.md). Grok Bot uses it with [OAuth sign-in](tunnel.md#oauth-sign-in-for-grok-bot).

## Named tokens

Give each client its own token, so you can revoke one without changing the others:

```sh
grok-gadgets-gateway mcp-token issue grok-bot
grok-gadgets-gateway mcp-token list
grok-gadgets-gateway mcp-token revoke grok-bot
```

`issue` prints `GROK_GADGETS_MCP_TOKEN=...` once, or writes it to a mode-0600 file with `--token-file`. The list in `mcp-tokens.json` (mode 0600, next to `mcp-token`) stores only a SHA-256 hash. `serve` reads the list on every request. A new token works at once. A revoked token fails on its next request, also inside an open session. The `mcp-token` file still works and is named `default`. The request log records the token name, never the token. A damaged list grants nothing, and `serve` refuses to start if the list is readable by other users.

## Limits

`serve` limits each token separately. The defaults suit one person and one Grok Bot:

| Option | Default | Over the limit |
| --- | --- | --- |
| `--calls-per-minute` | 60 tool calls | Tool error `rate_limited`, with the wait in seconds |
| `--commands-per-minute` | 10 `gadgets_command` calls | Tool error `rate_limited` |
| `--max-concurrent` | 8 open HTTP requests, event streams included | HTTP 429 with `Retry-After: 1` |
| `--max-body-bytes` | 65536 | HTTP 413 |
| `--request-timeout` | 30 seconds for one POST | HTTP 504 |

A rejected call does not count toward the limit. The windows reset when `serve` restarts. HTTP rejections are written to the logs as `http_rejected` lines with the status, the reason and the token name. They never contain the token or the body. `stdio` has no token and no limits.

This package does not terminate public TLS. With `serve --oauth` it is its own OAuth sign-in server, and you approve each sign-in on this computer; see [OAuth sign-in](tunnel.md#oauth-sign-in-for-grok-bot). On 13 October 2026 the owner's Grok Bot called the six tools this way from the Mac and the iPhone ([record](https://github.com/adidshaft/grok-gadgets/blob/main/docs/verification/grok-remote-https-2026-10-13.md)). A cloud Bot cannot open `127.0.0.1` on your computer. If you want a Bot to reach the gateway, you run `serve` on the gadget host and you publish HTTPS yourself.

## What you may publish

Publish only the MCP port. Point the proxy or tunnel at `127.0.0.1:8766` and leave port 8765 on loopback. The device protocol is unencrypted and is not safe on a shared network. If a tunnel is pointed at the device port by mistake, the gateway closes each HTTP connection there and logs `device_http_rejected`.

Pass the public hostname so the Host check accepts the tunnel:

```sh
uv run grok-gadgets-gateway serve --allowed-host your-name.example
```

That flag adds the hostname, `hostname:*`, and `http`/`https` origins for it. It does not change the bind address. The process still listens on `127.0.0.1` only. Requests with any other Host are rejected. `--allowed-host` needs `--request-log`, so every call through the tunnel is recorded.

## Remote requests

A request is **remote** when its Host is not `127.0.0.1`, `localhost` or `[::1]`, or when it carries `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host` or `X-Real-IP`. A remote request:

- needs a named token. The local `default` token gets HTTP 403 `named_token_required`;
- is refused with HTTP 503 `remote_disabled` while the kill switch is off;
- is marked `"remote":true` in the request log.

## Kill switch

```sh
grok-gadgets-gateway remote off      # every remote request gets 503 at once
grok-gadgets-gateway remote status
grok-gadgets-gateway remote on
```

`remote off` writes `remote-off` next to `mcp-token`. `serve` checks it on every remote request, so no restart is needed. Loopback clients, `rehearse` and devices keep working. To stop all access, also stop the tunnel and revoke the token.

For fixed-token test tools, keep the token in local configuration. For Grok Bot, use OAuth sign-in. A tunnel alone does not grant access. Do not put the token in the URL, a screenshot, or a support report.

## Process supervisors

`examples/launchd` and `examples/systemd` show a user service that runs `serve`. Replace `/ABSOLUTE/PATH/grok-gadgets-gateway` with the installed command. Run `init` once as the same user before you load the service. Stderr lines are redacted tool logs: they include a hash of arguments, not tokens, state, or event bodies.

## What this does not prove

Local tests call `serve` over Streamable HTTP on `127.0.0.1`. They check a missing token, a wrong token, a matching token, a simulator command, the absence of test controls, and a rejected Host. With the tunnel Host, they check missing, invalid and revoked tokens (401), the default token (403), a wrong Host (421), the kill switch (503) and HTTP on the device port. They do not open a public URL and they do not talk to Grok Bot or a board.

## Security checklist

The owner tested the daily route for one day on 13 October 2026. A second day and another tester are still needed.
See the [daily record](https://github.com/adidshaft/grok-gadgets/blob/main/docs/verification/daily-route-2026-10-13.md).

- `serve` binds `127.0.0.1` only. There is no flag for a public address.
- Publish the MCP port only. Leave device port 8765 on loopback.
- `mcp-token` and `credentials.json` are mode 0600 and stay out of Git.
- Pass the tunnel hostname with `--allowed-host`. Any other Host is rejected.
- For fixed-token remote tests, issue a named token; the `default` token never works remotely.
- Grok Bot uses the access token from OAuth sign-in. Do not configure a fixed bearer header.
- Keep the request log on. `remote off` stops remote access at once.
- Grok Bot signs in through OAuth. Fixed-token test tools send `Authorization: Bearer`; never put a token in the URL.
- Run `init` as the same user that will run `serve`.
- `init` prints connector JSON and hides the MCP token unless you pass `--show-token`.
- A local test or `rehearse` run is not a Grok Bot session and not physical proof.
