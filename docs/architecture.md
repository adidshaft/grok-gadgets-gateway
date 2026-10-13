# Local alpha architecture and decision record

Protocol 0.1.0 lives in protocol/0.1.0 and is shipped unchanged inside the gateway wheel. Consumer SDKs pin and hash those source files. Network/USB use 2048-byte LF JSON frames and one request/reply exchange; polling avoids unsolicited message scheduling and fits small firmware buffers. This intentionally favors an understandable local alpha over a public broker.

Python 3.11+ runs the MCP adapter. Current dependency versions are pinned in `uv.lock`. Device schemas use JSON Schema Draft 2020-12.

Custom capability schemas must be inline. The gateway rejects `pattern` and `patternProperties` before registration because device-supplied regular expressions can block command processing. Use explicit properties, `enum`, `minLength` and `maxLength` instead. Other schema dialects and references are not supported.

Domain Gateway knows devices, commands and retained events, not sockets or assistants. Simulator consumes exactly the same register/poll/ack/state/event interface; its explicit control adapter injects button/disconnect/reconnect. MCP adapter exposes typed official SDK tools over stdio and, with `serve`, over loopback Streamable HTTP. DeviceServer wraps the domain in loopback authenticated TCP. usb_bridge relays USB requests and responses, injecting the private host token into hello. Linux SDK and ESP32 firmware remain separate libraries, not gateway modules.

Each new connection creates a session; boot identity comes from firmware. Commands never replay into a new session. A lost physical acknowledgement is unconfirmed or timed_out and requires inspecting reported state/manual recovery, not inventing another command ID. This is bounded at-most-once dispatch within a retained session, not durable exactly-once execution. The gateway deliberately does not infer physical success.

The local threat model is one trusted operator on one host, with separate device tokens scoped by ID. Credentials reload on every request for revocation, and Unix file permissions must be mode 0600. This local boundary does not provide tenant authorization. MCP stdio inherits the local process trust.
The remote route adds OAuth and an owner-run HTTPS tunnel. The owner verified it from Mac and iPhone.
The [daily record](https://github.com/adidshaft/grok-gadgets/blob/main/docs/verification/daily-route-2026-10-13.md) covers one day on that setup.

## Hosting boundary

The gateway serves MCP over stdio to a supervising client, or over Streamable HTTP when you run `serve`. `serve` binds `127.0.0.1:8766/mcp` and checks a local or named token, or an OAuth-issued access token when OAuth is enabled. Its default authenticated device TCP listener accepts loopback connections only. The local device agent or USB bridge must run on that same host. Hosting the static project website does not start either process.

`serve --oauth` provides the sign-in server. An owner-run tunnel terminates public HTTPS for MCP.
The default device listener stays on loopback. The opt-in `--lan-listen` listener uses TLS on a private network.
See [Wi-Fi devices](wifi-devices.md) and [remote access](remote-access.md).
A public device relay is proposed work, not a supported deployment. Do not expose the raw device port. The canonical [hosting FAQ](https://github.com/adidshaft/grok-gadgets/blob/main/docs/getting-started/hosting.md) still separates the website from the gateway process.

## Diagnostics

Diagnostics use a strict allowlist: identities/status/counts and gateway epoch; no credentials, state, event data, arguments or raw errors. Device errors shown to the assistant are sanitized. Application state/event payloads remain visible to the requesting operator through normal tools and should not contain secrets.
