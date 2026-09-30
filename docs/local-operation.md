# Local operation

## Software demo and MCP launch

Run `uv sync --locked`, then `uv run python -m grok_gadgets_gateway.demo`. To configure a local MCP test client, use absolute uv path and this repository's working directory with arguments `run`, `--directory`, `/absolute/path/grok-gadgets-gateway`, `grok-gadgets-gateway`, `--simulator`. This is local MCP configuration guidance, not a verified Grok Bot configuration.

The client launches and supervises the stdio process. Keep the client and host awake. The gateway is intentionally not a background systemd service: a standalone stdio process without its client cannot serve assistant requests. A future authenticated reachable MCP transport is tracked separately. If the client exits, restart the gateway and device agent; state/history are in memory and event cursors reset explicitly.

## Per-device credential enrollment

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

Launch the gateway from your MCP client with `--credentials /absolute/private/path/devices.json --device-port 8765`. The TCP listener starts during MCP initialization and stops on process shutdown. Credential IDs must match the SDK hello. Set revoked true to terminate that device on its next request; denied requests close the session. Revocation does not erase previously reported state/history; it prevents subsequent access.

The JSON structure is `{ "devices": { "device-id": { "token": "private value >=16 chars", "revoked": false } } }`. Never commit the file or put it in a support report. Permissions allowing group/world access are rejected. Rotation requires updating both operator file and agent/bridge secret, then reconnecting.

## USB bridge

Use an actual USB-C data cable and verified C124 firmware when hardware is available. Identify the serial device through your OS (commonly /dev/cu.usbmodem… on macOS, /dev/ttyACM… on Linux). Obtain your private token from the file into the current shell environment without echoing it:

```sh
export GROK_GADGETS_DEVICE_TOKEN="$(python3 -c 'import json,pathlib;print(json.loads((pathlib.Path.home()/".config/grok-gadgets/devices.json").read_text())["devices"]["atoms3-lite-1"]["token"])')"
uv run python -m grok_gadgets_gateway.usb_bridge /dev/cu.YOUR_DEVICE
unset GROK_GADGETS_DEVICE_TOKEN
```

The gateway must already be running and initialized. The bridge never logs the token or compiles it into firmware. It forwards one reply for every firmware request and enforces frame bounds. Baudrate is 115200; native USB CDC may ignore baudrate. If TCP disconnects or the gateway restarts, the bridge returns gateway_unavailable and waits for a fresh firmware hello; each hello opens a new authenticated TCP session. Firmware owns the retry backoff and must discard pending old-session acknowledgements. A USB cable/serial device disconnect requires restarting the bridge after the device returns. This is session recovery, not durable action replay. USB hardware operation remains pending; pseudo-terminal software transport acceptance passes locally.

## Troubleshooting and recovery

- No devices: simulator requires --simulator; hardware agent requires private credentials and initialized device listener.
- Unauthorized: verify credential ID, >=16-character token and 0600 permissions. No raw input is logged.
- Offline: agent ended or idle timeout exceeded 15s. Poll/ping regularly; reconnect starts a new session.
- Stale state: received state older than 10s. Heartbeat proves connection, not fresh sensor state; periodically report state.
- Unconfirmed/timed_out: do not blindly resend a physical action under a fresh command ID. Inspect state or device safely.
- Cursor reset/history_lost: read with null cursor to establish retained baseline. Initial read reports dropped history honestly.
- Port busy: choose a free --device-port and configure SDK/bridge to match.
- USB failed: verify data cable, port permissions and firmware model. Unplug/reconnect is a pending physical test.

Rollback uses a known compatible gateway/SDK commit and fresh process. Protocol 0.1.0 rejects mismatched versions. No persistent database migrations exist. Run the demo after rollback; commands/history are intentionally lost and earlier action outcomes remain uncertain.
