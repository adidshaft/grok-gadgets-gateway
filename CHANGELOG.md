# Changelog

## Unreleased

- Signed firmware updates over the device connection ([#209](https://github.com/adidshaft/grok-gadgets-gateway/issues/209), hub ADR 012). `firmware keygen` makes the builder's ECDSA P-256 key pair on the builder's computer; `firmware sign` writes `image.bin`, a manifest (board, version, size, SHA-256, minimum protocol, key ID) and a signature over the manifest. On the relay host, `firmware add --public-key` stores an update only if the signature and hash match; `firmware assign`, `status`, `list` and `remove` manage offers, and `serve` picks up a change at the next poll. A gadget that sends `"update": "ready"` in its hello gets an `update` offer in the hello or poll reply, asks for 1,024-byte chunks with `update_chunk` (replies under 1,400 bytes) and reports `update_status`; other gadgets get the same replies as before. At most 8 gadgets download at once; chunks are read from disk. A rejected, failed or reverted version is not offered to that gadget again until it is assigned again. New lines `update_offered`, `update_started`, `update_progress`, `update_verified`, `update_applied`, `update_rejected`, `update_failed`, `update_reverted`, `update_busy`, `update_withdrawn`. `rehearse --update` runs the whole exchange with a simulated gadget. Protocol 0.1.0 gains the optional hello field `update`, the requests `update_chunk` and `update_status`, the reply fields `update` and `data`, the error `update_unavailable` and `fixtures/update-transcript.json`. Guide: [signed updates](docs/updates.md).

- `firmware status` keeps `reverted` when the gadget then refuses that version again (`rejected`/`version`), as the owner's kit does after a revert. Found in the Phase 12b acceptance.

## 0.1.0-alpha.15 — 17 October 2026

- On 17 October 2026 two people, each with Grok Bot on their own iPhone, used one relay in accounts mode and the owner's ESP32-S3 OLED kit: claim by the code on the screen, isolation of list, state, commands and events, share and revoke, wrong and expired codes, a flood, and a transfer after a reset that stayed pending until the old owner removed it. The owner's sign-in and kit kept working through the migration ([hub record](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/verification/claim-2026-10-17.md)). Two sign-in fixes came from that run (#196, #198).

- Gadget names and rooms in accounts mode ([#202](https://github.com/adidshaft/grok-gadgets-gateway/issues/202)). `gadgets_name` gives a gadget a plain name and an optional room, private to the caller's account (up to 32 characters, unique among their gadgets). Every tool accepts the name in place of `device_id`, only among the caller's own gadgets. `gadgets_list_devices` adds `name`, `room` and a one-line `summary` such as "Desk display, Office, online, yours". The server now has the title Grok Gadgets and instructions that tell Grok Bot to ask for the code on the display, name the gadget after a claim, use names and offer a recovery code once. A pending transfer answers `transfer_pending` instead of the share message. Names go when a gadget is unclaimed or changes hands; line `gadget_named`.

- A code refused by a wrong-code limit now writes `claim_limited` (reason and caller or account), at most once a minute per key ([#200](https://github.com/adidshaft/grok-gadgets-gateway/issues/200)). `service install` right after `service uninstall` no longer reports the device port as in use while only closed connections linger.

- The sign-in page no longer sends a `form-action` CSP directive ([#198](https://github.com/adidshaft/grok-gadgets-gateway/issues/198)). Browsers apply it to the redirect after the code form, so phones stopped before handing the sign-in back to Grok Bot. Found in the two-account acceptance; reproduced and fixed in a browser against a local relay.

- A right code on the sign-in page now hands the sign-in straight back to Grok Bot with a redirect, as the operator approval does ([#196](https://github.com/adidshaft/grok-gadgets-gateway/issues/196)). On a phone the earlier page, with a recovery code and a link, never finished the sign-in. The recovery code now comes from `gadgets_share action=recovery_code`, which replaces the old one.

- Unclaim, transfer and the grace period ([#180](https://github.com/adidshaft/grok-gadgets-gateway/issues/180), hub ADR 011 §7). A reset never changes the owner. The first hello after a reset and a phone re-join (`"claim": "after_setup"`) gives a claimed gadget one 10-minute transfer code. Entering it (sign-in page or `gadgets_claim`) makes a pending transfer: the old owner keeps full use, the new person sees only `pending_transfer` with its end time. The transfer completes when the old owner runs `gadgets_remove` or after 7 days (the owner's choice), also across a restart; the old owner can stop it with `gadgets_share action=refuse_transfer`. No second transfer while one is pending; codes work once. Operator commands `accounts unclaim`, `accounts assign` and `accounts link` are queued in `oauth/admin/` and applied by `serve` within a second. `rehearse --accounts` runs two accounts on a private relay with two simulated gadgets. New lines `transfer_pending`, `transfer_done`, `transfer_refused`, `transfer_withdrawn`, `admin_rejected`.

- Tools scoped by account ([#160](https://github.com/adidshaft/grok-gadgets-gateway/issues/160), hub ADR 011 §6–8). In accounts mode all six tools answer only for the caller's gadgets: another account's gadget answers `unknown_device` like a missing one, its command receipts `unknown_command`, a reused command ID `duplicate_conflict`; events and diagnostics count only the caller's gadgets and commands. The operator account sees only its own gadgets. New tools in accounts mode: `gadgets_claim` (a claim code or a share code), `gadgets_remove` (owner unclaims and every share ends; a grantee drops a share) and `gadgets_share` (`create` a 10-minute, one-use share code with scope `use` or `view`; `list`; `revoke`; `link` for a second app). The sign-in page also takes share codes (a new account with the share) and link codes (the same account, no new recovery code). Accounts never merge; a grantee cannot share on. Codes never reach the request log. New lines `share_created`, `share_used`, `share_revoked`, `share_dropped`, `link_created`, `account_linked`, `device_unclaimed`. A cross-account test for each tool.

- Accounts mode: `serve --oauth --accounts` ([#159](https://github.com/adidshaft/grok-gadgets-gateway/issues/159), hub ADR 011). A gadget that sends `"claim": "ready"` in its hello gets `claimed` and, while unclaimed, an 8-character `claim_code` (40 bits, 10 minutes, live session only) in the hello reply; a fresh code or a claim reaches it in the next poll reply. Gadgets without the field get the same replies as before. The sign-in page takes that code: the relay creates an account (`acct_` and 128 random bits), gives it the gadget and shows a recovery code once (16 characters, stored as a hash, replaced after each use). Token records carry the account and a refresh copies it; the `client_id` is never used. The shared `mcp-token` and named tokens get 401 `shared_credential_refused`. `oauth approve` still works and signs in to the operator account. The first start in accounts mode gives every enrolled device and every live sign-in to the operator account (`oauth/accounts.json`). Wrong codes: 5 per sign-in, 20 an hour per caller, 200 an hour per relay (then closed 10 minutes, `claim_flood`). New lines `accounts_migrated`, `account_created`, `account_recovered`, `device_claimed`, `claim_rejected`, `claim_flood`; codes never reach a file or a log. `accounts list`; `service install --accounts`. Protocol 0.1.0 gains the optional hello field `claim` and the reply fields `claimed` and `claim_code`.

- Device tokens are stored as SHA-256 hashes ([#161](https://github.com/adidshaft/grok-gadgets-gateway/issues/161), hub ADR 011). `enroll` stores the hash and shows the token once (or writes `--token-file`). On the first start of `serve` or `stdio`, a registry with plain tokens is rewritten as hashes; revocations are kept and the old file stays as `credentials.json.pre-hash` (mode 0600). Device checks compare two SHA-256 digests in constant time, also for an unknown device. `wifi-provision` and `factory-provision` take `--token-file`, which needs no copy of the relay's registry, or `--rotate`, which stores the new hash only after the board confirms; `factory-provision --enroll` also enrolls only after the board confirms. A registry with plain tokens still provisions before it is migrated.

## 0.1.0-alpha.14 — 14 October 2026

- On 14 October 2026 `factory-provision` wrote the owner's ESP32-S3 OLED kit, and the owner joined it to home Wi-Fi from an iPhone and an Android phone with no computer; it went Online through the relay ([hub record](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/verification/phone-setup-2026-10-14.md)). `relay-check` passed against the owner's relay.

- `factory-provision` and `label` ([#158](https://github.com/adidshaft/grok-gadgets-gateway/issues/158), hub ADR 010). `factory-provision PORT --device ID --gateway NAME[:PORT]` runs the relay preflight, checks the board, and sends the device token, relay name and port, CA, a new hotspot passphrase (16 characters, 80 bits) and setup code (8 characters, 40 bits) with no Wi-Fi details. It prints the setup label and writes it as text, a `WIFI:` QR PNG and a printable SVG; records stay in `labels/`, mode 0600. `--enroll` enrolls a missing device, `--replace` overwrites a board's identity, `--new-label` makes new label values. `label ID` reprints a label. New dependency: `segno` 1.6.6 (BSD-3-Clause) for the QR code.

- Relay port and DNS preflight ([#177](https://github.com/adidshaft/grok-gadgets-gateway/issues/177)). New relays default to device port 8444; Cloudflare proxies 8443 as HTTPS. Relays and devices already on 8443 keep it with an explicit `:8443`. `relay-check NAME[:PORT]` refuses a name that resolves to a Cloudflare proxy address, and checks that the port presents a certificate from the relay CA. `wifi-provision --gateway` runs the same check before it sends anything (`--no-relay-check` skips it). The Docker example publishes 8444.

- Both MCP eras ([#175](https://github.com/adidshaft/grok-gadgets-gateway/issues/175)). `rehearse` now calls the six tools with the `initialize` handshake and with the 2026-07-28 revision (`server/discover`); `--protocol legacy|modern|both` picks one, and both is the default. Each `mcp_tool_call` request-log line names the revision in `protocol`. `serve --oauth` metadata states `client_id_metadata_document_supported: false`, so clients keep using registration. `docs/daily-use.md` explains what to do if Grok Bot stops connecting.

## 0.1.0-alpha.13 — 14 October 2026

- On 13 October 2026 the owner's ESP32-S3 OLED kit connected to `serve --device-listen` on an always-on Linux server, and Grok Bot on the iPhone, on cellular, showed text on it while the Mac slept. Server reboot, `remote off`, revoke, re-enroll, re-provision and a wrong CA also passed ([hub record](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/verification/mac-free-route-2026-10-13.md)).
- Relay host example ([#156](https://github.com/adidshaft/grok-gadgets-gateway/issues/156), hub ADR 009). `examples/docker/` runs `serve --oauth --allowed-host --device-listen` in a container with a pinned Cloudflare Tunnel client that shares its network, so `/mcp` stays on `127.0.0.1:8766` and only port 8443 is published. Config, tokens and logs live in a volume. `docs/relay-host.md` covers the container, a Linux user service, provisioning with the relay's CA, a daily check and turn-off. The container was built and checked only on a Mac (device hello, wrong token, wrong CA, plain HTTP, restart); the owner's relay ran the user-service route.
- Relay device listener ([#154](https://github.com/adidshaft/grok-gadgets-gateway/issues/154), hub ADR 009). `serve --device-listen ADDRESS[:PORT]` (port 8443 by default, off unless set) runs a TLS device listener on any address of an always-on host and accepts any peer. It has per-address limits under global ones: 20 new connections a minute and 4 waiting connections per address, 32 waiting and 64 sessions in total, and a 5 s handshake. It logs `device_*` lines with the peer address and never a token, and one `device_flood` line a minute while a limit refuses connections. `diagnostics` and `service status` show it; `service install --device-listen` passes it to the service.
- DNS names for Wi-Fi devices ([#155](https://github.com/adidshaft/grok-gadgets-gateway/issues/155)). `device-cert NAME` issues the relay listener certificate for one public DNS name from the `init` CA, beside the LAN certificate. `wifi-provision --gateway NAME[:PORT]` stores a DNS name for the board, and `--ca FILE` pins a relay's CA when the relay was set up on another host. `lan-cert` and `--lan` are unchanged.
- `usage --calibrate 0` now saves nothing. A whole-percent usage page often shows no change, and a zero rate would count every later call as free.

## 0.1.0-alpha.12 — 13 October 2026

- Daily use ([#142](https://github.com/adidshaft/grok-gadgets-gateway/issues/142)): `docs/daily-use.md` covers the tunnel client as a user service, a daily check, sleep settings and floods. `service install --usage-hook CMD` passes a usage hook to the service (absolute program path). `examples/usage-hooks/macos-notify.py` posts a local macOS notification for each remote call; no external service.
- `service status` shows the Grok Bot route ([#142](https://github.com/adidshaft/grok-gadgets-gateway/issues/142)): the allowed host and `--oauth` from the installed unit, `remote` on or off, live sign-ins with their end date, the last remote tool call from the request log (rotated files included) and any `oauth_flood` line in the last 24 hours.
- Log rotation ([#142](https://github.com/adidshaft/grok-gadgets-gateway/issues/142), hub ADR 008). The request log rotates at 5 MiB and keeps 5 files (`requests.jsonl.1` to `.5`, mode 0600). When the gateway runs as the service, its `gateway.log` is copied to `gateway.log.1` and truncated in place at the same size. `usage` reads the rotated request logs.
- Sign-in floods cannot lock the owner out ([#142](https://github.com/adidshaft/grok-gadgets-gateway/issues/142), hub ADR 008). Registered clients without a live sign-in are pruned after an hour or when the list of 20 is full; a client with a live sign-in is kept. Waiting sign-ins drop the oldest instead of refusing, at most 2 per client. Sign-in paths have separate budgets per caller and in total, so registration floods do not block refresh. A hit limit writes one `oauth_flood` line a minute.

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
