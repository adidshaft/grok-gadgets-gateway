# Local alpha architecture and decision record

Protocol 0.1.0 lives in protocol/0.1.0 and is shipped unchanged inside the gateway wheel. Consumer SDKs pin and hash those source files. Network/USB use 2048-byte LF JSON frames and one request/reply exchange; polling avoids unsolicited message scheduling and fits small firmware buffers. This intentionally favors an understandable local alpha over a public broker.

Python 3.11+ with official MCP Python SDK 1.26.0 is pinned to the v1 implementation. Historical implementation reference: https://github.com/modelcontextprotocol/python-sdk/blob/v1.26.0/README.md (reviewed 2026-10-04; no claim about current upstream main). Dependency resolution lives in uv.lock. Device schemas use JSON Schema Draft 2020-12.

Domain Gateway knows devices, commands and retained events, not sockets or assistants. Simulator consumes exactly the same register/poll/ack/state/event interface; its explicit control adapter injects button/disconnect/reconnect. MCP adapter exposes typed official SDK tools over stdio and, with `serve`, over loopback Streamable HTTP. DeviceServer wraps the domain in loopback authenticated TCP. usb_bridge relays USB requests and responses, injecting the private host token into hello. Linux SDK and ESP32 firmware remain separate libraries, not gateway modules.

Each new connection creates a session; boot identity comes from firmware. Commands never replay into a new session. A lost physical acknowledgement is unconfirmed or timed_out and requires inspecting reported state/manual recovery, not inventing another command ID. This is bounded at-most-once dispatch within a retained session, not durable exactly-once execution. The gateway deliberately does not infer physical success.

The local threat model is one trusted operator on one host, with separate device tokens scoped by ID. Credentials reload on every request for revocation, and Unix file permissions must be mode 0600. This is not tenant authorization, TLS or an internet service. MCP stdio inherits the local client's process trust. A cloud Grok Bot cannot launch a local Mac filesystem path; authorized remote reachability/authentication and actual Bot/client acceptance remain separate gates.

## Hosting boundary

The gateway serves MCP over stdio to a supervising client, or over Streamable HTTP when you run `serve`. `serve` binds `127.0.0.1:8766/mcp` and checks a static bearer token. Its authenticated device TCP listener accepts loopback connections only. The local device agent or USB bridge must run on that same host. Hosting the static project website does not start either process.

`serve` does not terminate public TLS and it is not an OAuth server. A tunnel can carry the MCP port to a hostname you allow with `--allowed-host`. Reachability is not Grok Bot verification. Do not expose the raw device port. See [remote access](remote-access.md) and [HARD-GROK-REMOTE-001](https://github.com/adidshaft/grok-gadgets/issues/4). The canonical [hosting FAQ](https://github.com/adidshaft/grok-gadgets/blob/main/docs/getting-started/hosting.md) still separates the website from the gateway process.

## Diagnostics

Diagnostics use a strict allowlist: identities/status/counts and gateway epoch; no credentials, state, event data, arguments or raw errors. Device errors shown to the assistant are sanitized. Application state/event payloads remain visible to the requesting operator through normal tools and should not contain secrets.
