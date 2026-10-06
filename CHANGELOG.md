# Changelog

## Unreleased

- `enroll <id> --rotate` replaces a device's token and keeps its identity and receipts; the old token is refused at once. `enroll --token-file <path>` writes the token to a mode-0600 file instead of printing it. Re-enrolling an existing ID now says the exact command to run.
- `gadgets_list_devices` returns `capability_descriptions`: each capability's JSON Schema `description` (1–300 characters, validated at hello), and a built-in description for `rgb.set`.
- TCP devices may send request frames up to 16 KiB, so a Linux hello can carry schemas and descriptions for all capabilities. USB frames and gateway replies stay at 2048 bytes.
- Real subcommands: `init`, `serve`, `stdio`, `enroll`, `revoke`, `devices`, `rotate-mcp-token`, `usb-bridge`. `--help` lists them. The bare command in a terminal prints help instead of silently waiting; MCP clients that start it without arguments, or with the older `--simulator` flags, still get stdio.
- `init` prints pasteable settings: one complete JSON block per mode, with the absolute executable path. `--client stdio|http` prints a single block.
- The harmless `IncompleteFieldDefinitionWarning` from the `mcp` dependency no longer prints on start.
- `serve` no longer publishes OAuth discovery metadata that pointed at loopback. A 401 now carries a plain `WWW-Authenticate: Bearer` challenge; `/.well-known/oauth-*` return 404.
- `serve` handles SIGTERM and Ctrl+C: it closes device sessions and HTTP, prints `Gateway stopped`, and exits 0.
- Protocol README split into sections, with `late_ack` (non-fatal, session kept) and a device-action table for every error code. Wire format unchanged.
- `gadgets_command` now waits up to 3 seconds (never past the 10-second ACK deadline) for the device's report and returns the final status, so a model sees `executed` or `failed` instead of `accepted`. A slow device still returns `accepted`/`dispatched`; poll `gadgets_command_status`.
- Add a five-minute first-success guide with MCP Inspector and a CI job that runs the README quick start from a clean checkout on every push and nightly.

## 0.1.0a1 — local alpha candidate, 2026-10-04

### Launch preparation, 2026-10-05 — unpublished

- Add standalone wheel guidance, an evidence table, an architecture diagram, and
  component contribution/support/conduct/security routes.
- Prepare `adidshaft` ownership and canonical hub links; activation remains pending.
- Preserve runtime, canonical protocol, Apache-2.0 license, and dependency notices.

- Canonical 0.1.0 schemas/fixtures and custom capability discovery.
- Reusable device lifecycle, bounded event cursor history, command idempotency and honest acknowledgement statuses.
- Explicit C124 software simulator and Grok-facing tools via official MCP Python SDK stdio.
- Loopback device credentials/revocation and USB NDJSON bridge.
- Official MCP client acceptance, TCP authentication/lifecycle tests and USB pseudo-terminal transcript acceptance.

### Review corrections, 2026-10-05 — unpublished

- Classify `button`, reserved `history_lost`, and inline `"x-grok-gadgets-kind": "event"` schemas as events. Reject an event whose name is a command. Wire format stays 0.1.0.
- USB bridge: discard an oversize frame to the next LF and reply once, catch `RecursionError`, ignore non-JSON log lines, and reopen the serial port with backoff.
- Add `init`, `enroll`, `revoke`, `devices`, `rotate-mcp-token`, and `serve`. `init` prints copy-paste stdio and HTTP client JSON and hides the MCP token unless `--show-token` is set. `serve` is a loopback device listener plus bearer-token Streamable HTTP MCP on `127.0.0.1:8766/mcp`. Local tests use the official MCP client. This is not Grok Bot or hardware verification.
- README is a 3-step `serve` path. The installed-wheel demo remains `uv run python -m grok_gadgets_gateway.demo`.

### Audit corrections

- Reject simulator event/read names as commands, expose callable discovery accurately, and preserve custom SDK command capabilities.
- Isolate 256 retained event IDs per device/current boot; preserve same-boot reconnect deduplication and bound retired boot/session bookkeeping.
- Add real MCP negative command coverage, two authenticated device event isolation, and direct lifecycle/retention boundary regressions.

Pending: Grok Bot/mobile verification, physical C124, operator HTTPS in front of the loopback MCP port, OAuth, durable delivery, and independent installation.
