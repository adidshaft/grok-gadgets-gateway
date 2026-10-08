# Changelog

## Unreleased

- Add `serve --request-log PATH`: an opt-in, mode-0600 JSONL file with one line per tool call (time, client name, tool, device, command, non-secret arguments, result). Secret-looking and `writeOnly` argument values are redacted; tokens are never written ([#65](https://github.com/adidshaft/grok-gadgets-gateway/issues/65)).
- `rehearse` identifies itself to the gateway as `grok-gadgets-rehearse`.

## 0.1.0-alpha.4 — 8 October 2026

- Update to `mcp` 2.3.0. The server uses `MCPServer`, and its HTTP settings move to `streamable_http_app()`. The six Grok Bot tools, their annotations, `serve` authentication and transport security are unchanged. The server still accepts MCP protocol versions 2024-11-05 to 2025-11-25 and adds 2026-07-28.
- `rehearse` sends its bearer token through an `httpx2` client, the HTTP library `mcp` 2.x uses.
- Development tools: `ruff` 0.16.10, `pytest` 9.1.1, `pytest-asyncio` 1.4.0 and `hatchling` 1.32.4. Code follows the new `ruff` default rules.

## 0.1.0-alpha.3 — 8 October 2026

- Security: update `mcp` from 1.26.0 to 1.28.1. This closes the high-severity Dependabot alerts for `mcp`. `pytest` (development only) moves to 9.0.3.
- Dependabot opens weekly grouped update PRs into `dev`.
- CONTRIBUTING starts fork branches from `upstream/dev`. CI also runs the plain-language check on CONTRIBUTING and SUPPORT.

## 0.1.0-alpha.2 — 7 October 2026

- Add `grok-gadgets-gateway rehearse` for a clean-checkout, assertion-backed simulated journey.
- Keep the newcomer guide and its automated check centered on Grok Bot.

- `grok-gadgets-gateway rehearse` calls the six tools against a running `serve` in the order Grok Bot will: list gadgets, set the light blue, read state. The first-success guide, README quick start and CI use it; they no longer need Node.js or a third-party tool.
- Docs describe Grok Bot as the only assistant the gateway serves.

## 0.1.0a1 — 6 October 2026

- `enroll <id> --rotate` replaces a device's token and keeps its identity and receipts; the old token is refused at once. `enroll --token-file <path>` writes the token to a mode-0600 file instead of printing it. Re-enrolling an existing ID now says the exact command to run.
- `gadgets_list_devices` returns `capability_descriptions`: each capability's JSON Schema `description` (1–300 characters, validated at hello), and a built-in description for `rgb.set`.
- TCP devices may send request frames up to 16 KiB, so a Linux hello can carry schemas and descriptions for all capabilities. USB frames and gateway replies stay at 2048 bytes.
- Real subcommands: `init`, `serve`, `stdio`, `enroll`, `revoke`, `devices`, `rotate-mcp-token`, `usb-bridge`. `--help` lists them. The bare command in a terminal prints help instead of silently waiting; connectors that start it without arguments, or with the older `--simulator` flags, still get stdio.
- `init` prints pasteable settings: one complete JSON block per mode, with the absolute executable path. `--client stdio|http` prints a single block.
- The harmless `IncompleteFieldDefinitionWarning` from the `mcp` dependency no longer prints on start.
- `serve` no longer publishes OAuth discovery metadata that pointed at loopback. A 401 now carries a plain `WWW-Authenticate: Bearer` challenge; `/.well-known/oauth-*` return 404.
- `serve` handles SIGTERM and Ctrl+C: it closes device sessions and HTTP, prints `Gateway stopped`, and exits 0.
- Protocol README split into sections, with `late_ack` (non-fatal, session kept) and a device-action table for every error code. Wire format unchanged.
- `gadgets_command` now waits up to 3 seconds (never past the 10-second ACK deadline) for the device's report and returns the final status, so a model sees `executed` or `failed` instead of `accepted`. A slow device still returns `accepted`/`dispatched`; poll `gadgets_command_status`.
- Add a five-minute first-success guide and a CI job that runs the README quick start from a clean checkout on every push and nightly.

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
- MCP session acceptance, TCP authentication/lifecycle tests and USB pseudo-terminal transcript acceptance.

### Review corrections, 2026-10-05 — unpublished

- Classify `button`, reserved `history_lost`, and inline `"x-grok-gadgets-kind": "event"` schemas as events. Reject an event whose name is a command. Wire format stays 0.1.0.
- USB bridge: discard an oversize frame to the next LF and reply once, catch `RecursionError`, ignore non-JSON log lines, and reopen the serial port with backoff.
- Add `init`, `enroll`, `revoke`, `devices`, `rotate-mcp-token`, and `serve`. `init` prints copy-paste stdio and HTTP connector JSON and hides the MCP token unless `--show-token` is set. `serve` is a loopback device listener plus bearer-token Streamable HTTP MCP on `127.0.0.1:8766/mcp`. Local tests drive a real MCP session. This is not Grok Bot or hardware verification.
- README is a 3-step `serve` path. The installed-wheel demo remains `uv run python -m grok_gadgets_gateway.demo`.

### Audit corrections

- Reject simulator event/read names as commands, expose callable discovery accurately, and preserve custom SDK command capabilities.
- Isolate 256 retained event IDs per device/current boot; preserve same-boot reconnect deduplication and bound retired boot/session bookkeeping.
- Add real MCP negative command coverage, two authenticated device event isolation, and direct lifecycle/retention boundary regressions.

Pending: Grok Bot/mobile verification, physical C124, operator HTTPS in front of the loopback MCP port, OAuth, durable delivery, and independent installation.
