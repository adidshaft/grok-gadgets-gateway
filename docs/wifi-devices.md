# Wi-Fi devices: the LAN TLS listener

A Wi-Fi device, such as the ESP32 SDK's `esp32s3-oled-kit-wifi` build, reaches the gateway
over your home network. The design is in hub
[ADR 005](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/decisions/005-wifi-device-route.md).

The LAN listener is **off by default**. It starts only with `serve --lan-listen`. It uses
the same protocol 0.1.0 frames and the same per-device tokens as the loopback listener, so
USB devices keep working beside it.

The ESP32-S3 OLED kit passed physical acceptance through this listener on 12 October 2026
([record](https://github.com/adidshaft/grok-gadgets-esp32-sdk/blob/dev/docs/verification/oled-kit-wifi-2026-10-12.md)).
That record covers one kit, one router and one Mac.

## What protects it

| Control | Detail |
| --- | --- |
| One private address | `--lan-listen` takes one IPv4 address of this computer in `10.0.0.0/8`, `172.16.0.0/12` or `192.168.0.0/16`. The gateway refuses `0.0.0.0`, `::`, loopback, link-local, shared (`100.64.0.0/10`) and public addresses. |
| Private peers only | A connection from an address outside those ranges is closed before TLS and logged as `lan_peer_rejected`. |
| TLS | TLS 1.2 or newer, ECDHE-ECDSA with AES-GCM. The certificate comes from a local CA that `init` creates. The CA key stays in the config directory (mode 0600). |
| Device credential | The hello carries the device's enroll token. The gateway re-reads the credential file on every frame, so `revoke` ends a live session on its next poll. |
| Limits | 8 LAN sessions, 4 connections waiting for a hello, 5 s TLS handshake, 20 new connections a minute per source address, 30 frames a second per session (`busy` above it). Frame size, hello and idle timeouts match loopback. |

Never forward the port on your router, and never put it behind a tunnel. The listener is for
devices on your own network.

## Set it up

Find the Mac's Wi-Fi address, for example with `ipconfig getifaddr en0`. A DHCP reservation
on the router keeps it stable. If the address changes, run `lan-cert` again and provision
the device again.

```sh
uv run grok-gadgets-gateway init                    # creates lan/ca.pem and lan/ca.key if missing
uv run grok-gadgets-gateway lan-cert 192.168.1.20   # the listener certificate for that address
uv run grok-gadgets-gateway lan-ca show             # CA path and SHA-256 fingerprint
uv run grok-gadgets-gateway serve --lan-listen 192.168.1.20 --request-log ~/grok-gadgets-requests.jsonl
```

macOS may ask whether Python may accept incoming network connections. Allow it, or the
device cannot connect.

When the board shows **Online**, you can [run the gateway as a background service](always-on.md)
so it comes back after a reboot.

## Provision a device over USB

Provisioning needs the USB data cable once. Stop any `usb-bridge` for the board first.

```sh
uv run grok-gadgets-gateway enroll oledkit-0123456789ab        # once per device
uv run grok-gadgets-gateway wifi-provision /dev/cu.usbmodem101 \
  --device oledkit-0123456789ab --lan 192.168.1.20
```

The command asks for the Wi-Fi name and the password. The password prompt does not echo.
The command first checks that the board reports the same device ID. It then sends one line:
the Wi-Fi settings, the device token from the credential file, the listener address and the
CA certificate. It prints only the board's answer. It never prints, logs or saves the password
or the token. The board stores them in its NVS and restarts.

The ESP32-S3 joins 2.4 GHz networks only.

After provisioning, power the board from any USB charger.

## Revoke, re-enroll and reset

- `revoke DEVICE` ends the device's LAN session on its next frame, within about a second.
  Every new connection then gets `revoked` at hello. The firmware waits 60 seconds between
  attempts and shows "Token rejected".
- `enroll DEVICE --rotate` issues a new token and clears the revocation. Provision the new
  token with `wifi-provision` over USB.
- `wifi-provision PORT --device DEVICE --factory-reset` erases the board's Wi-Fi settings.
  Holding BOOT for 10 seconds does the same on the board. Also revoke the device's token.
- `lan-ca rotate` replaces the CA and removes the listener certificate. Run `lan-cert`,
  restart `serve`, and provision every Wi-Fi device again.

## Request log

With `--request-log`, the gateway writes these LAN lines. Each has `ts`, `event`,
`listener` and the source address `peer`. None contains a token.

| Event | Meaning |
| --- | --- |
| `lan_connected` | TLS handshake completed |
| `lan_hello` | Device authenticated; `device` is its ID |
| `lan_auth_rejected` | `code` is `unauthorized` or `revoked` |
| `lan_tls_failed` | Handshake failed or timed out, for example a device that pins another CA |
| `lan_peer_rejected` | Source address outside the private ranges |
| `lan_rate_limited` | Too many new connections from one address |
| `lan_disconnected` | Session ended |

## Limits

- The device has no clock, and its TLS library does not check certificate dates. Rotate
  the CA to replace trust.
- The board keeps the Wi-Fi password and token in plain flash. Anyone who holds the board
  can read them. If you lose a board, revoke its token and change the Wi-Fi password.
- The device listener accepts device frames only. Grok Bot reaches the gateway only through
  the routes in the hub's ADR 004.
