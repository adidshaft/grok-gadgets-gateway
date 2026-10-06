# Local operation

## Software demo and MCP launch

1. Run `uv sync --locked`.
2. Run `uv run python -m grok_gadgets_gateway.demo`.

To configure a local MCP test client, use the absolute uv path and this repository as the working directory.
Set the arguments to `run`, `--directory`, `/absolute/path/grok-gadgets-gateway`, `grok-gadgets-gateway`, `--simulator`.
These instructions configure a local MCP client. They are not a verified Grok Bot configuration.

The client starts and manages the stdio process. Keep the client and host awake.
A stdio process stops when that client exits. `grok-gadgets-gateway serve` is the long-running process: it keeps the device listener and Streamable HTTP MCP up without an MCP client. See [remote access](remote-access.md) for the URL, the bearer token, and the rule against publishing the device port.
State and history are stored in memory. Event cursors reset when the process restarts.

## Where to run it

Keep the local MCP client, gateway, and device agent or USB bridge on the same host.
The device listener accepts only `127.0.0.1` or `::1`; another computer cannot use this local transport.
The public website provides documentation and downloads. It does not keep your gateway running.
Local simulation needs no public endpoint, domain, or hosted service.

`serve` authenticates MCP on loopback. A tunnel can carry that HTTP port to a public HTTPS name. It does not add OAuth, and it is not Grok Bot verification. Do not expose the raw device port. Details are in [remote access](remote-access.md). Broader hosting boundaries remain in
[HARD-GROK-REMOTE-001](https://github.com/adidshaft/grok-gadgets/issues/4) and the
[hosting FAQ](https://github.com/adidshaft/grok-gadgets/blob/main/docs/getting-started/hosting.md).

## Per-device credential enrollment

For an installable configurable software device instead, see [simulator settings](simulator.md).
`--simulator-config` only works with `--simulator` and never enables test controls implicitly.

Config lives in `$XDG_CONFIG_HOME/grok-gadgets` or `~/.config/grok-gadgets` when `XDG_CONFIG_HOME` is unset.

```sh
uv run grok-gadgets-gateway init
uv run grok-gadgets-gateway enroll atoms3-lite-1
uv run grok-gadgets-gateway devices
uv run grok-gadgets-gateway serve
```

`init` creates the directory mode 0700, `credentials.json`, and `mcp-token`. Stdout is copy-paste stdio MCP JSON, then HTTP settings for `http://127.0.0.1:8766/mcp`. It does not print the MCP token unless you pass `--show-token`. `enroll` writes the device with an atomic replace (temp file, mode 0600, `os.replace`) and prints one stdout line, `GROK_GADGETS_DEVICE_TOKEN=...`. Copy that value to the device once. It is not printed again. `devices` prints ids only. `revoke <device-id>` sets `revoked` to true. `rotate-mcp-token` replaces the MCP bearer token and prints `GROK_GADGETS_MCP_TOKEN=...` once.

The registry shape is `{ "devices": { "device-id": { "token": "private value >=16 chars", "revoked": false } } }`.
Never commit the file or include it in a support report. The gateway rejects permissions that allow group or public access.

Stdio still accepts `--credentials /absolute/path`. If you omit it and `credentials.json` already exists in the config directory, stdio uses that file. `serve` uses the same default. The device listener starts with `serve`, or during stdio MCP initialization when a registry is configured. It stops when the process stops.
Credential IDs must match the SDK hello. Revocation is checked on the device's next request.
A denied request closes the session. Revocation prevents subsequent access. It does not erase previously reported state or history.
To replace a device token, enroll a new id or edit the private file, update the agent or bridge secret, and reconnect. Do not commit the edit.

## USB bridge

When hardware is available, use a USB-C data cable and verified C124 firmware.
Find the serial device in your operating system. Common paths are `/dev/cu.usbmodem…` on macOS and `/dev/ttyACM…` on Linux.
Load the private token into the current shell environment without printing it:

```sh
export GROK_GADGETS_DEVICE_TOKEN='value-printed-once-by-enroll'
uv run grok-gadgets-gateway usb-bridge /dev/cu.YOUR_DEVICE
unset GROK_GADGETS_DEVICE_TOKEN
```

`serve` or an initialized stdio gateway must already be listening before you start the bridge.
The bridge does not log the token or compile it into firmware.
It forwards one reply for each firmware request and enforces frame limits.
The baud rate is 115200. Native USB CDC can ignore the baud rate.

If TCP disconnects or the gateway restarts, the bridge returns `gateway_unavailable`.
It then waits for a new firmware hello. Each hello opens a new authenticated TCP session.
Firmware controls the retry delay. It must discard pending acknowledgements from the old session.
If USB disconnects, the bridge retries the same serial path automatically with a delay of up to 2 seconds. If the operating system assigns a different path, restart the bridge with that path.
This procedure restores the session. It does not replay actions from durable storage.

Physical USB operation remains unverified. Local software tests with a pseudo-terminal pass.

## Troubleshooting and recovery

- No devices: simulator requires --simulator; hardware agent requires private credentials and initialized device listener.
- Unauthorized: verify credential ID, >=16-character token and 0600 permissions. No raw input is logged.
- Offline: agent ended or idle timeout exceeded 15s. Poll/ping regularly; reconnect starts a new session.
- Stale state: received state older than 10s. Heartbeat proves connection, not fresh sensor state; periodically report state.
- Unconfirmed/timed_out: do not blindly resend a physical action under a fresh command ID. Inspect state or device safely.
- Retry protection: use the same command ID and arguments within 10 minutes of the first request, in the same gateway process. An evicted receipt returns `stale_command_id` without dispatch. At 1024 recent IDs, new commands return `busy` until the oldest ID leaves the window. A restart or an older ID has no replay guarantee. Inspect state before acting again.
- Cursor reset/history_lost: read with null cursor to establish retained baseline. Initial read reports dropped history honestly.
- Port busy: choose a free --device-port and configure SDK/bridge to match.
- USB failed: verify data cable, port permissions and firmware model. Unplug/reconnect is a pending physical test.

To roll back, select known compatible gateway and SDK commits. Start a new process.
Protocol 0.1.0 rejects mismatched versions. There are no persistent database migrations.
Run the demo after rollback. Commands and history are lost. Earlier action outcomes remain uncertain.
