# Changelog

## Unreleased

## 0.1.0-alpha.11 — 13 October 2026

- On 13 October 2026 the owner's Grok Bot called the six tools natively from the Mac and the iPhone through `serve --oauth` behind an owner-run tunnel; the request log recorded nine `grok-bot` calls and the owner saw the OLED text (hub #154, ADR 007).
- OAuth sign-in for Grok Bot ([#71](https://github.com/adidshaft/grok-gadgets-gateway/issues/71)). A personal Grok Bot cannot add a server with a fixed bearer header (hub #154). With `serve --oauth`, the gateway is its own OAuth 2.1 sign-in server at `https://ALLOWED-HOST`: protected-resource metadata, dynamic client registration, PKCE, refresh and revocation, built on the MCP SDK's auth routes. The sign-in page asks for nothing; the owner approves its code on the Mac with `grok-gadgets-gateway oauth approve CODE --name grok-bot`. Tokens are stored as hashes, carry the approved name into the request log and limits, and end at once with `oauth revoke NAME` or `remote off`. `service install --oauth` passes it to the service.
- Usage tracking ([#132](https://github.com/adidshaft/grok-gadgets-gateway/issues/132), guide `docs/usage.md`). Tool-call lines add `bytes_in` and `bytes_out`. `grok-gadgets-gateway usage` summarizes a request log by token, device, capability and UTC day, with `--since`, `--until`, `--token` and `--json`. `--rate` or `--calibrate` (from the owner's own Grok Bot readings) adds a column labelled estimate; the gateway never reads a Grok Bot account. `serve` and `stdio` accept an opt-in `--usage-hook CMD` that receives one allowlisted JSON line per call, never tokens or arguments, without delaying or failing a call.

- Tunnel hardening ([#130](https://github.com/adidshaft/grok-gadgets-gateway/issues/130), hub #4). A request through a tunnel Host or with a forwarding header is remote: it needs a named token (the local `default` token gets 403), and `grok-gadgets-gateway remote off|on|status` is a kill switch that refuses remote requests with 503 at once. `--allowed-host` requires `--request-log`, and tool-call lines record `"remote"`. The device port closes HTTP connections and logs `device_http_rejected`. `service install --allowed-host` passes the tunnel name to the service.

## 0.1.0-alpha.10 — 13 October 2026

- On 12–13 October 2026 the owner's Mac and OLED kit passed name, two address-change and restart checks with the service (`docs/verification/always-on-2026-10-13.md`). Linux systemd is not yet tested on a real host.
- The macOS service runs as a standard process, not a throttled background job, so it starts sooner after a restart ([#115](https://github.com/adidshaft/grok-gadgets-gateway/issues/115)).

- `lan-cert`, `serve --lan-listen` and `wifi-provision --lan` accept an mDNS name such as `my-mac.local`. A name certificate holds only the name. `serve` binds the name's current private address, checks it every 30 seconds and moves the Wi-Fi listener when it changes (`lan_rebound`). The board needs name support from ESP32 SDK #99 ([#110](https://github.com/adidshaft/grok-gadgets-gateway/issues/110), hub ADR 006).
- `service install|uninstall|status|logs` runs `serve` as a per-user background service: a launchd agent on macOS, a systemd user unit on Linux. It starts at every login, needs no administrator rights and refuses to install over a running gateway. Guide: `docs/always-on.md` ([#109](https://github.com/adidshaft/grok-gadgets-gateway/issues/109), hub ADR 006).

## 0.1.0-alpha.9 — 12 October 2026

- `serve --simulator` and `stdio --simulator` without `--credentials` serve only the simulated light: they never read the device registry and open no device port, so the beginner path never touches real device credentials and runs beside a running gateway. Add `--credentials` to serve enrolled devices too. `--lan-listen` with simulator-only mode stops with a clear message ([#96](https://github.com/adidshaft/grok-gadgets-gateway/issues/96)).

## 0.1.0-alpha.8 — 12 October 2026

- `wifi-provision` opens the serial port and checks the board's device ID before it asks for the Wi-Fi name and password. Ctrl-C or end of input at a prompt prints "Cancelled; nothing was sent to the board." instead of a traceback ([#98](https://github.com/adidshaft/grok-gadgets-gateway/issues/98)). Checked on the owner's kit.
- `stdio` and `serve` stop with a one-line message when the device port is taken ("Another gateway may be running; stop it or choose another --device-port") instead of a traceback. The simulator demo runs in an empty config directory, so it never loads the operator's devices or opens the device port ([#96](https://github.com/adidshaft/grok-gadgets-gateway/issues/96)).

## 0.1.0-alpha.7 — 12 October 2026

- `grok-gadgets-gateway --version` prints the package version and exits without starting a server ([#17](https://github.com/adidshaft/grok-gadgets-gateway/issues/17)).
- A hand-started **release packages** workflow builds a tag, writes `SHA256SUMS` with plain file names, checks it in a fresh folder and attaches the files to the release. PyPI upload is a separate opt-in through trusted publishing; no token is stored ([#90](https://github.com/adidshaft/grok-gadgets-gateway/issues/90), hub [#151](https://github.com/adidshaft/grok-gadgets/issues/151)). The package is not on PyPI yet.
- Tests run with a private home and config directory, so they pass beside a running gateway and never read the operator's credentials ([#88](https://github.com/adidshaft/grok-gadgets-gateway/issues/88)).
- Docs match the 12 October evidence: one owner ESP32-S3 kit passed over USB and Wi-Fi; Grok Bot ran approved `rehearse` commands; native MCP calls are unverified. README adds pip install from the release wheel. Homepage is https://grokgadgets.org/.

## 0.1.0-alpha.6 — 12 October 2026

- Add an opt-in LAN TLS device listener for Wi-Fi devices: `serve --lan-listen ADDRESS[:PORT]` (port 8767). It binds one private IPv4 address of this computer, refuses all, loopback, shared and public addresses, accepts private peers only and limits sessions, handshakes, new connections and frame rate. Same protocol 0.1.0 frames and device tokens; revocation ends a live session on its next frame ([#79](https://github.com/adidshaft/grok-gadgets-gateway/issues/79), hub ADR 005).
- `init` creates a local CA (`lan/ca.pem`, `lan/ca.key`). New commands: `lan-cert ADDRESS`, `lan-ca show|rotate` and `wifi-provision PORT --device ID --lan ADDRESS` (prompts for the Wi-Fi password without echo; never prints or stores it or the token).
- The request log records LAN connects, hellos, rejected tokens, TLS failures, rejected peers and rate limits, without tokens. Guide: `docs/wifi-devices.md`.
- Closing a device connection never raises and waits at most 2 seconds. A Wi-Fi device that lost power or its network left a TLS socket whose shutdown timed out after 30 seconds with an unhandled traceback; the gateway now aborts it (found in the Phase 4 physical test).
- `cryptography` 50.0.2 becomes a direct dependency (it was already installed through `mcp`).
- The ESP32-S3 OLED kit passed physical acceptance over Wi-Fi through this listener on the owner's kit on 12 October 2026 ([ESP32 SDK record](https://github.com/adidshaft/grok-gadgets-esp32-sdk/blob/dev/docs/verification/oled-kit-wifi-2026-10-12.md)).

## 0.1.0-alpha.5 — 12 October 2026

- Add `docs/tunnel.md`: an owner-run TLS tunnel guide in front of `127.0.0.1:8766`, with a named token, limits, the request log and revocation. Untested with Grok Bot ([#70](https://github.com/adidshaft/grok-gadgets-gateway/issues/70)).
- Add per-token limits to `serve`: tool calls and `gadgets_command` calls per minute (`rate_limited`), open requests (429), body size (413) and POST time (504). Rejections are logged without tokens or bodies ([#69](https://github.com/adidshaft/grok-gadgets-gateway/issues/69)).
- Add named MCP tokens: `mcp-token issue|list|revoke`. `serve` accepts each active named token and the `mcp-token` file, reads the list per request, and logs the token name ([#68](https://github.com/adidshaft/grok-gadgets-gateway/issues/68)).
- Add `serve --request-log PATH` (and the same option for `stdio`): an opt-in, mode-0600 JSONL file with one line per tool call (time, client name, tool, device, command, non-secret arguments, result). Secret-looking and `writeOnly` argument values are redacted; tokens are never written ([#65](https://github.com/adidshaft/grok-gadgets-gateway/issues/65)).
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
