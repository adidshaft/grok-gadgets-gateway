# Protocol 0.1.0 (canonical)

The device protocol between a gadget (or the USB bridge) and the gateway. The schemas
[device-request.schema.json](device-request.schema.json) and
[device-response.schema.json](device-response.schema.json) and the transcript
[fixtures/device-transcript.json](fixtures/device-transcript.json) are normative; this page
explains them. The wire format is 0.1.0.

## Transport

- LF-delimited UTF-8 JSON. One request produces one response. The gateway never sends
  unsolicited messages.
- Maximum frame: **2048 bytes including the LF** for every gateway reply and for every USB
  serial frame. Over TCP, a device's request frames may be up to **16384 bytes**, so a hello
  can carry schemas and descriptions for all its capabilities. USB firmware keeps its whole
  hello within 2048 bytes.
- TCP defaults to `127.0.0.1:8765`. It is loopback only and unencrypted, for a same-host
  agent or the USB bridge. Remote and Wi-Fi transport are not implemented.
- Keep the socket open and poll every 100–500 ms, also when idle. 15 seconds without a
  request closes the session.

## Hello and identity

- The first request is `hello` with `protocol_version` `0.1.0`, the device descriptor and a
  per-device token (16 or more characters). The USB bridge supplies the token, so firmware
  does not send it.
- Device IDs match `[A-Za-z0-9][A-Za-z0-9._:-]{0,63}` against the whole string (the schema
  anchor `$(?![\s\S])` rejects a trailing LF).
- Each hello gets a fresh gateway session. `boot_id` distinguishes a device reboot. A replaced
  session cannot write.

## Claim fields (accounts mode, hub ADR 011)

- A gadget that can show a claim code adds `"claim": "ready"` to its hello, or
  `"claim": "after_setup"` in the first hello after a reset and a phone re-join.
- Only then, and only on a relay that runs `serve --oauth --accounts`, does the hello reply
  carry `claimed` (true or false) and, while unclaimed, `claim_code` (`XXXX-XXXX`, valid 10
  minutes while the session lives).
- A poll reply carries the same fields only when one of them changed, for example a fresh
  code after 10 minutes or `claimed: true` after a claim.
- A gadget without the hello field gets the replies it got before. A relay before this change
  answers the field with `invalid_request`: send the hello again without it until the next
  boot.

## Signed updates (hub ADR 012)

- A gadget whose firmware holds a builder's public key adds `"update": "ready"` to its hello.
  Only then may a reply carry `update` or `data`. A relay before this change answers the
  field with `invalid_request`: send the hello again without it until the next boot (drop
  `update` before `claim` when both were sent).
- When the operator assigned an update to this gadget and its version differs from the
  hello's `firmware_version`, the hello reply carries `update`: `version`, `size`, `chunk`
  (1024), `manifest` (base64 of the signed manifest JSON) and `signature` (base64 of the DER
  ECDSA P-256 signature over SHA-256 of those manifest bytes). A poll reply carries `update`
  only when the offer changed in this session; `null` withdraws it.
- `{"type": "update_chunk", "version": V, "offset": N}` asks for `chunk` bytes at `N`, a
  multiple of `chunk` below `size`. The reply is `{"ok": true, "data": "<base64>"}`; the last
  chunk is shorter. `update_unavailable` means the offer was withdrawn or changed: abandon the
  download. `busy` means too many gadgets are updating: try again with backoff, on the same
  session.
- `{"type": "update_status", "version": V, "status": S, "reason": R}` reports `started`,
  `verified`, `rejected`, `failed`, `applied` or `reverted`; `reason` is optional (`signature`,
  `board`, `version`, `size`, `protocol`, `sha256`, `write`, `link`, `not_online`, `boot`).
  The reply is `{"ok": true}`. After `rejected`, `failed` or `reverted` the relay does not
  offer that version to that gadget again until the operator assigns it again.
- Check the signature with a compiled public key, then the manifest (`format`
  `grok-gadgets-update/1`, `board`, `version`, `size`, `min_protocol`), before downloading.
  Check the byte count and SHA-256 of the image before making it bootable. Mark a new image
  valid only after it is Online through the relay. The relay never proves an image is safe.
- [fixtures/update-transcript.json](fixtures/update-transcript.json) is a complete exchange
  for a 2,500-byte update with its public key; request *i* is answered by reply *i*.

## Capabilities and schemas

- Capabilities are string names. `rgb.set` has the canonical strict arguments
  `{r, g, b: integer 0..255, on: boolean}`.
- JSON integers are type-strict everywhere the gateway validates: `255.0` and booleans are
  not integers.
- Optional `device.capability_schemas` maps custom capability names to inline JSON Schema
  Draft 2020-12 objects. `$ref` and `$dynamicRef` are not allowed. `rgb.set` always uses the
  canonical schema. Custom commands without a schema take object arguments and must
  validate them on the device.
- A name is an **event** when it is the reserved name `button` or `history_lost`, or when its
  inline schema contains `"x-grok-gadgets-kind": "event"`. Other custom names are commands,
  for compatibility with string-only clients. The annotation is not a new frame field.
- Describe what a capability does with the standard JSON Schema `description` keyword in its
  inline schema (1–300 characters). The gateway shows it to the assistant as
  `capability_descriptions` and gives `rgb.set` a built-in one. It is device-supplied
  information, not an instruction.
- `state` is read through `gadgets_get_state`. Events and state are not commands: discovery
  keeps the original `capabilities` list, adds `command_capabilities` and
  `event_capabilities`, and returns `capability_contracts` for command names only.

## Commands and acknowledgements

- A poll delivers at most one command (`command_id`, `capability`, `arguments`).
- When it finishes, the device sends an `ack` with `status` `executed` or `failed` and its
  current state. `failed` requires `error`. An acknowledgement is a report of execution,
  never proof of a physical effect.
- Gateway command status: `accepted`, `dispatched`, `executed`, `failed`, `not_delivered`,
  `timed_out`, `unconfirmed`. A dispatched command times out 10 seconds after delivery.
- A duplicate `command_id` with identical device, capability and arguments returns the
  original result while it is retained (128 commands). Changed parameters conflict.
- Commands are never replayed into a new session. Devices should deduplicate commands within
  a boot. This is bounded retry safety, not durable exactly-once delivery.

### Late acknowledgements (`late_ack`)

If an ACK arrives after its command closed as `timed_out` or `unconfirmed`, the gateway
replies `{"ok": false, "error": {"code": "late_ack", ...}}`. It **keeps the session**,
records the reported state, and keeps the command's honest status; a late ACK never turns
it into a success. Devices must treat `late_ack` as non-fatal: drop that ACK and continue
polling on the same session. Do not reconnect and do not resend it.

## Events

- An event has a boot-unique `event_id`, `name`, `data` and optional `observed_at` (device
  time). `button` requires exactly `{pressed: boolean}`. The gateway's `received_at` is the
  authoritative arrival time.
- Repeated identical event IDs are accepted as duplicates; changed data is rejected. IDs are
  deduplicated over the last 256 newly accepted IDs per registered device's current boot, so
  one busy device cannot evict another's window. Identical retries do not refresh retention.
- Reconnecting with the same `boot_id` keeps that window. A new `boot_id` discards it.
  Returning later to an old `boot_id` starts a fresh window, so use a fresh boot identity on
  every reboot. IDs outside the window may be accepted again.
- At most 64 devices are registered per process by default, including disconnected ones, so
  duplicate bookkeeping is bounded at 16,384 IDs. `serve --max-devices N` (at most 2,000) sets
  another limit; bookkeeping then stays below 256 IDs for each device. A device past the
  limit gets `busy`.

## Reading events and state

- Readable history keeps the last 128 events, independent of duplicate retention.
- Cursor `<gateway epoch>:<sequence>` is global to the process. A null cursor reads retained
  history and reports `history_lost` when earlier history was dropped. Device filtering
  still advances `next_cursor` over all scanned events.
- An invalid or future cursor, dropped history and a previous-process epoch return explicit
  errors. A cursor in an error message is not a recovery contract: call again with a null
  cursor.
- The `history_lost` capability name is the event classification above; the `history_lost`
  boolean on an event read is a separate cursor flag.
- State is reported with a received timestamp and freshness (`fresh`, `stale`, `offline`);
  state is stale after 10 seconds. Nothing persists across a gateway restart.

## Errors

Every error response is `{"ok": false, "error": {"code": ..., "message": ...}}`. Token
failures never echo input. Revocation is reloaded from the credential file on each request.

| Code | Meaning | Device action |
| --- | --- | --- |
| `unauthorized`, `revoked` | Token rejected or revoked. The gateway closes the connection. | Stop; fix the credential. |
| `stale_session` | This session was replaced. The gateway closes the connection. | Reconnect with a new hello. |
| `protocol_mismatch`, `invalid_request` | Frame breaks the contract. | Fix the device; do not retry the same frame. |
| `duplicate_conflict` | A retained ACK or event ID was reused with changed content. | Contract fault; do not retry. |
| `late_ack` | ACK arrived after the command closed. Session kept. | Drop the ACK; keep polling. |
| `unknown_command` | ACK for a command this session does not own. | Drop the ACK; keep polling. |
| `unsupported_capability`, `invalid_event` | Undeclared or malformed event. | Drop it; keep polling. |
| `busy`, `unavailable` | Gateway temporarily cannot accept the request. | Retry with capped backoff. |
| `update_unavailable` | The update offer was withdrawn or changed. Session kept. | Abandon the download. |

SDKs reconnect with capped backoff after connection loss. Never retry a physical action under
a new command ID only because an acknowledgement was lost.
