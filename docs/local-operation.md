# Local operation

## Software demo and MCP launch

1. Run `uv sync --locked`.
2. Run `uv run python -m grok_gadgets_gateway.demo`.

To configure a local MCP test client, use the absolute uv path and this repository as the working directory.
Set the arguments to `run`, `--directory`, `/absolute/path/grok-gadgets-gateway`, `grok-gadgets-gateway`, `--simulator`.
These instructions configure a local MCP client. They are not a verified Grok Bot configuration.

The client starts and manages the stdio process. Keep the client and host awake.
The gateway is not a background systemd service. A stdio process cannot serve assistant requests without its client.
A reachable MCP transport with authentication is separate future work.
If the client exits, restart the gateway and device agent. State and history are stored in memory. Event cursors reset on restart.

## Where to run it

Keep the local MCP client, gateway, and device agent or USB bridge on the same host.
The device listener accepts only `127.0.0.1` or `::1`; another computer cannot use this local transport.
The public website provides documentation and downloads. It does not keep your gateway running.
Local simulation needs no public endpoint, domain, or hosted service.

A tunnel provides reachability. It does not turn this stdio gateway into an authenticated remote MCP service.
Do not expose the raw device port. Remote HTTPS/OAuth and service access controls remain future work in
[HARD-GROK-REMOTE-001](https://github.com/adidshaft/grok-gadgets/issues/4).
Use the [hosting FAQ](https://github.com/adidshaft/grok-gadgets/blob/main/docs/getting-started/hosting.md)
to distinguish website hosting, local processes, and the proposed remote route.

## Per-device credential enrollment

For an installable configurable software device instead, see [simulator settings](simulator.md).
`--simulator-config` only works with `--simulator` and never enables test controls implicitly.

Create a private file outside the repository. This command creates a token without printing it:

```sh
mkdir -p "$HOME/.config/grok-gadgets"
python3 - <<'PY'
import json, os, secrets
from pathlib import Path
path = Path.home() / '.config/grok-gadgets/devices.json'
if path.exists():
    raise SystemExit('Existing credentials preserved; edit to enroll another device')
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as handle:
    json.dump({'devices': {'atoms3-lite-1': {'token': secrets.token_urlsafe(32), 'revoked': False}}}, handle)
PY
```

Launch the gateway from your MCP client with `--credentials /absolute/private/path/devices.json --device-port 8765`.
The TCP listener starts during MCP initialization. It stops when the process stops.
Credential IDs must match the SDK hello. Set `revoked` to `true` to terminate access on the device's next request.
A denied request closes the session. Revocation prevents subsequent access. It does not erase previously reported state or history.

Use this JSON structure: `{ "devices": { "device-id": { "token": "private value >=16 chars", "revoked": false } } }`.
Never commit the file or include it in a support report. The gateway rejects permissions that allow group or public access.
To rotate a token, update the private credential file and the agent or bridge secret. Then reconnect.

## USB bridge

When hardware is available, use a USB-C data cable and verified C124 firmware.
Find the serial device in your operating system. Common paths are `/dev/cu.usbmodem…` on macOS and `/dev/ttyACM…` on Linux.
Load the private token into the current shell environment without printing it:

```sh
export GROK_GADGETS_DEVICE_TOKEN="$(python3 -c 'import json,pathlib;print(json.loads((pathlib.Path.home()/".config/grok-gadgets/devices.json").read_text())["devices"]["atoms3-lite-1"]["token"])')"
uv run python -m grok_gadgets_gateway.usb_bridge /dev/cu.YOUR_DEVICE
unset GROK_GADGETS_DEVICE_TOKEN
```

The gateway must be running and initialized before you start the bridge.
The bridge does not log the token or compile it into firmware.
It forwards one reply for each firmware request and enforces frame limits.
The baud rate is 115200. Native USB CDC can ignore the baud rate.

If TCP disconnects or the gateway restarts, the bridge returns `gateway_unavailable`.
It then waits for a new firmware hello. Each hello opens a new authenticated TCP session.
Firmware controls the retry delay. It must discard pending acknowledgements from the old session.
If USB disconnects, wait for the device to return. Then restart the bridge.
This procedure restores the session. It does not replay actions from durable storage.

Physical USB operation remains unverified. Local software tests with a pseudo-terminal pass.

## Troubleshooting and recovery

- No devices: simulator requires --simulator; hardware agent requires private credentials and initialized device listener.
- Unauthorized: verify credential ID, >=16-character token and 0600 permissions. No raw input is logged.
- Offline: agent ended or idle timeout exceeded 15s. Poll/ping regularly; reconnect starts a new session.
- Stale state: received state older than 10s. Heartbeat proves connection, not fresh sensor state; periodically report state.
- Unconfirmed/timed_out: do not blindly resend a physical action under a fresh command ID. Inspect state or device safely.
- Cursor reset/history_lost: read with null cursor to establish retained baseline. Initial read reports dropped history honestly.
- Port busy: choose a free --device-port and configure SDK/bridge to match.
- USB failed: verify data cable, port permissions and firmware model. Unplug/reconnect is a pending physical test.

To roll back, select known compatible gateway and SDK commits. Start a new process.
Protocol 0.1.0 rejects mismatched versions. There are no persistent database migrations.
Run the demo after rollback. Commands and history are lost. Earlier action outcomes remain uncertain.
