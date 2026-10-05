# Remote access

`grok-gadgets-gateway serve` listens on this computer only:

- MCP: `http://127.0.0.1:8766/mcp`
- Devices: `127.0.0.1:8765`

The MCP client sends `Authorization: Bearer <token>`. The token is the single line in `~/.config/grok-gadgets/mcp-token` (or `$XDG_CONFIG_HOME/grok-gadgets/mcp-token`), mode 0600. `rotate-mcp-token` replaces it and prints `GROK_GADGETS_MCP_TOKEN=...` once on stdout. Restart `serve` after you rotate it if a client still has the old value cached; the server reads the file on each request.

This package does not terminate public TLS and it does not speak OAuth. It has not been verified with Grok Bot. A cloud Bot cannot open `127.0.0.1` on your computer. If you want a Bot to reach the gateway, you run `serve` on the gadget host and you publish HTTPS yourself.

## What you may publish

Publish only the MCP port. Point the proxy or tunnel at `127.0.0.1:8766` and leave port 8765 on loopback. The device protocol is unencrypted and is not safe on a shared network.

Pass the public hostname so the Host check accepts the tunnel:

```sh
uv run grok-gadgets-gateway serve --allowed-host your-name.example
```

That flag adds the hostname, `hostname:*`, and `http`/`https` origins for it. It does not change the bind address. The process still listens on `127.0.0.1` only. Requests with any other Host are rejected.

Keep the bearer token in the client configuration. A tunnel without that token is not access. Do not put the token in the URL, a screenshot, or a support report.

## Process supervisors

`examples/launchd` and `examples/systemd` show a user service that runs `serve`. Replace `/ABSOLUTE/PATH/grok-gadgets-gateway` with the installed command. Run `init` once as the same user before you load the service. Stderr lines are redacted tool logs: they include a hash of arguments, not tokens, state, or event bodies.

## What this does not prove

Local tests use the official MCP Streamable HTTP client against `127.0.0.1`. They check a missing token, a wrong token, a matching token, a simulator command, the absence of test controls, and a rejected Host. They do not open a public URL and they do not talk to Grok Bot or a board.
