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

### Use a name instead of an address

`lan-cert`, `serve --lan-listen` and `wifi-provision --lan` also accept this computer's mDNS
name, for example `my-mac.local`. On macOS, `scutil --get LocalHostName` prints the name; add
`.local`. The certificate then names only the name, so it stays valid when the address changes.
`serve` binds the name's current private address and checks it again every 30 seconds. When the
address changes, it moves the Wi-Fi listener and logs `lan_rebound`. The board needs firmware
that looks up `.local` names.
ESP32 SDK v0.2.0-alpha.12 and later support these names.
Use an IPv4 address only with older firmware that does not support name lookup.

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

## Relay listener: devices on any network

The relay listener lets a Wi-Fi device reach a gateway on an always-on host, away from your
home network. The design is in hub
[ADR 009](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/decisions/009-relay-route.md).
It is **off by default** and starts only with `serve --device-listen`. It uses the same
protocol 0.1.0 frames, the same tokens and the same CA as the LAN listener. The owner's kit
passed through it on 13 October 2026 ([record](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/verification/mac-free-route-2026-10-13.md)).

| Control | Detail |
| --- | --- |
| Any address | `--device-listen` takes one IP address of the host, for example `0.0.0.0:8444` or `[::]:8444`. The default port is 8444. Relays set up before 14 October 2026 keep 8443. |
| Any peer | Every source address may connect. The token, TLS and the limits protect the port. |
| TLS and name | The certificate from `device-cert NAME` names one public DNS name only. The device checks the certificate against that name and pins your CA. |
| Limits | 64 sessions, 32 connections waiting for a hello, 4 waiting per source address, 5 s TLS handshake, 20 new connections a minute per source address, 30 frames a second per session. |
| No HTTP | A plain HTTP request fails the TLS handshake. HTTP inside TLS is closed and logged. Never put the port behind a web tunnel. |

On the relay host:

```sh
grok-gadgets-gateway init
grok-gadgets-gateway device-cert devices.example.net
grok-gadgets-gateway enroll oledkit-0123456789ab
grok-gadgets-gateway serve --device-listen 0.0.0.0:8444 --request-log ~/grok-gadgets-requests.jsonl
```

Point a DNS-only record for `devices.example.net` at the host, and open TCP port 8444 in its
firewall. Never proxy this record: a proxy answers with its own certificate and every
device's CA pin fails. Check it with `grok-gadgets-gateway relay-check devices.example.net`. `diagnostics` and `service status` show the relay listener.

Provision the board over USB on the computer it is plugged into. If the relay was set up on
another host, copy its `lan/ca.pem` and `credentials.json` to this computer first:

```sh
grok-gadgets-gateway wifi-provision /dev/cu.usbmodem101 --device oledkit-0123456789ab \
  --gateway devices.example.net --ca relay-ca.pem --credentials relay-credentials.json
```

Before it sends anything, `wifi-provision --gateway` checks that the name is a DNS-only record
and that the port presents a certificate from the CA. For a relay on port 8443, pass
`--gateway devices.example.net:8443`.

Delete the copied `credentials.json` afterwards: it holds every device token. The board needs
firmware that accepts a DNS name for the gateway.

The relay listener writes the same lines as the LAN listener with a `device_` prefix:
`device_connected`, `device_hello`, `device_auth_rejected`, `device_tls_failed`,
`device_rate_limited` and `device_disconnected`. A source address that hits its connection
limit is logged once a minute. While any limit refuses connections, the gateway writes one
`device_flood` line a minute with a `reason`: `connection_rate`, `pending`,
`pending_per_address` or `sessions`.

## Factory-provision a gadget for phone setup

A builder prepares a gadget once with a computer. The person who receives it then joins it to
home Wi-Fi with a phone alone, through the gadget's setup hotspot (hub
[ADR 010](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/decisions/010-phone-setup.md)).
The board needs ESP32 SDK firmware with a factory identity (0.2.0-alpha.15 or later).

```sh
grok-gadgets-gateway factory-provision /dev/cu.usbmodem101 --device oledkit-0123456789ab \
  --gateway devices.example.net --ca relay-ca.pem --credentials relay-credentials.json
```

The command:

1. Checks that the relay name is a DNS-only record and that the port presents the relay CA.
2. Checks that the board on the port is this device and supports a factory identity.
3. Sends the device token, the relay name and port, and the CA. It adds a new hotspot
   passphrase and setup code. It sends no Wi-Fi details.
4. Prints the setup label. It also writes the label as text, a Wi-Fi QR PNG and a printable SVG.

The label shows the hotspot name: `GrokGadget-` and the last four characters of the device
ID. It also shows the hotspot passphrase with a `WIFI:` QR code, the setup code and the page
address. The
label is a secret: anyone with the label and the gadget can set it up again after a reset.

- The device must be enrolled in the registry you pass. `--enroll` enrolls it first; copy the
  registry back to the relay afterwards, or run `enroll` on the relay instead.
- A board accepts a factory identity once. `--replace` overwrites it. The label stays the same
  unless you pass `--new-label`.
- Records live in `labels/` in the config folder, mode 0600. `grok-gadgets-gateway label ID`
  prints a lost label again.

## Limits

- The device has no clock, and its TLS library does not check certificate dates. Rotate
  the CA to replace trust.
- The board keeps the Wi-Fi password and token in plain flash. Anyone who holds the board
  can read them. If you lose a board, revoke its token and change the Wi-Fi password.
- The device listener accepts device frames only. Grok Bot reaches the gateway only through
  the routes in the hub's ADR 004.
