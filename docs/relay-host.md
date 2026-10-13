# Relay host: run the gateway away from your Mac

A relay is this gateway on an always-on Linux host. Wi-Fi devices connect out to it over TLS.
Grok Bot reaches it through your tunnel with OAuth sign-in. Your Mac can then sleep or stay
off. The design is in hub
[ADR 009](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/decisions/009-relay-route.md).

**Status:** the container and the commands below pass this repository's tests. No owner's kit
has run through a relay host yet; hub Phase 9 records that check. Run one relay for your own
gadgets only. It has one operator and no accounts.

```text
Grok Bot ─HTTPS + OAuth─▶ gadgets.example.net (tunnel) ─▶ 127.0.0.1:8766 /mcp   ┐
Wi-Fi device ─TLS, outbound─▶ devices.example.net:8443 (relay device listener)  ┘ relay host
```

## What you need

- A Linux host that stays on: a small VPS, or a home Linux box. It needs a public address that
  devices can reach on TCP port 8443. A home box usually needs a router port forward; review
  that plan before you open a port.
- Two names on a domain you control. `gadgets.example.net` goes through your tunnel to `/mcp`.
  `devices.example.net` is a DNS-only record (no proxy) that points at the host's address.
- A tunnel client for `gadgets.example.net`. The example uses Cloudflare Tunnel; the same rule
  holds for any provider: one hostname, one target, `http://127.0.0.1:8766`.
- Your board on firmware that accepts a DNS name for the gateway, and a computer with USB to
  provision it once.

Never publish port 8766, and never send port 8443 through the tunnel.

## Option A: container (Docker Compose)

`examples/docker/` holds a Dockerfile and a compose file. The gateway container runs
`serve --oauth --allowed-host --device-listen` and keeps config, CA, tokens and logs in the
`gateway-data` volume. The `cloudflared` container shares the gateway's network, so it reaches
`127.0.0.1:8766` while that port stays unpublished.

1. Get the code at the commit or release tag you want to run:

   ```sh
   git clone https://github.com/adidshaft/grok-gadgets-gateway.git
   cd grok-gadgets-gateway
   git checkout v0.1.0-alpha.13        # or a reviewed commit
   cd examples/docker
   cp .env.example .env                # set MCP_HOST=gadgets.example.net
   ```

2. Put your tunnel's files in `examples/docker/cloudflared/`: `config.yml` (start from
   `config.yml.example`) and the tunnel credentials file. Git ignores both. If you move a tunnel
   from your Mac, stop it on the Mac first.

3. Build, then set up the gateway once:

   ```sh
   docker compose build
   docker compose run --rm gateway init
   docker compose run --rm gateway device-cert devices.example.net
   docker compose run --rm gateway enroll oledkit-0123456789ab
   ```

   `init` prints a local bearer token. You do not need it for Grok Bot, which signs in with
   OAuth. Do not paste it anywhere.

4. Start both containers, and open TCP port 8443 in the host's firewall:

   ```sh
   docker compose up -d
   docker compose logs gateway
   ```

   The log shows `Relay TLS device listener devices.example.net on 0.0.0.0:8443`.

Run every other command inside the running container, for example:

```sh
docker compose exec gateway grok-gadgets-gateway oauth pending
docker compose exec gateway grok-gadgets-gateway oauth approve K7QX-M2PA --name grok-bot
docker compose exec gateway grok-gadgets-gateway usage --log /data/logs/requests.jsonl
```

Both containers restart by themselves after a crash or a host reboot, while Docker starts at
boot.

## Option B: Linux user service (systemd)

```sh
uv tool install grok-gadgets-gateway==0.1.0a13
grok-gadgets-gateway init
grok-gadgets-gateway device-cert devices.example.net
grok-gadgets-gateway enroll oledkit-0123456789ab
grok-gadgets-gateway service install --allowed-host gadgets.example.net --oauth \
  --device-listen 0.0.0.0:8443
loginctl enable-linger              # start at boot, without a login
grok-gadgets-gateway service status
```

Run the tunnel client as a system service with its own instructions, forwarding only
`http://127.0.0.1:8766`. [Always on](always-on.md) covers the service commands.

## Provision the board

The board is on USB at your computer, and the relay's CA and tokens are on the host. Copy two
files to the computer, provision, then delete them. With the container:

```sh
docker compose cp gateway:/data/config/grok-gadgets/lan/ca.pem ./relay-ca.pem
docker compose cp gateway:/data/config/grok-gadgets/credentials.json ./relay-credentials.json
```

With the user service, the files are `~/.config/grok-gadgets/lan/ca.pem` and
`~/.config/grok-gadgets/credentials.json`. Move them to your computer, for example with `scp`.
Then, on the computer with the board:

```sh
grok-gadgets-gateway wifi-provision /dev/cu.usbmodem101 --device oledkit-0123456789ab \
  --gateway devices.example.net --ca relay-ca.pem --credentials relay-credentials.json
rm relay-credentials.json relay-ca.pem
```

The command asks for the Wi-Fi name and password in your terminal and sends them only to the
board. They never reach the relay. `relay-credentials.json` holds every device token; delete
it at once. After provisioning, power the board from a USB charger. It shows **Online** when
the relay accepts it.

## Connect Grok Bot

Ask Grok Bot to add `https://gadgets.example.net/mcp` as a Remote HTTPS server with OAuth
sign-in, and approve the code on the relay with `oauth approve`. The hub guide
[Connect your Grok Bot](https://github.com/adidshaft/grok-gadgets/blob/main/docs/getting-started/connect-grok-bot.md)
has the exact chat text. If Grok Bot already lists `grok-gadgets` for the Mac, ask it to replace
that server; the hostname can stay the same if the tunnel moved with it.

## Daily check

From any computer, not the relay host:

```sh
curl -s -o /dev/null -w "%{http_code}\n" https://gadgets.example.net/.well-known/oauth-protected-resource/mcp   # 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://gadgets.example.net/mcp                                 # 401
curl -s -m 5 http://devices.example.net:8443/ ; echo "exit $?"                                                   # no HTTP answer
```

On the host, the request log shows `device_hello` for the board and `mcp_tool_call` lines with
`"remote":true` for Grok Bot. Look for `device_flood` and `oauth_flood` lines:

```sh
docker compose exec gateway sh -c 'tail -n 50 /data/logs/requests.jsonl'
```

## Update, turn off and back up

- Update: check out the new tag, then `docker compose build && docker compose up -d`.
- Stop remote calls at once: `grok-gadgets-gateway remote off` (inside the container). Turn the
  relay off with `docker compose down`; the data volume stays.
- `revoke DEVICE` ends a device's session on its next frame.
- The `gateway-data` volume holds the CA key, device tokens and sign-ins. Back it up privately,
  or plan to provision every device again if you lose it.

## Security notes

- The device port accepts any address. Tokens, TLS and the limits in
  [Wi-Fi devices](wifi-devices.md#relay-listener-devices-on-any-network) protect it.
- Keep the host's OS updated and allow only SSH and TCP 8443 in.
- Anyone with a shell on the host can approve sign-ins and read device tokens.
- The relay sees device IDs, commands, reported state and events. The request log file is for
  the operator. It records command arguments, such as the text sent to a display, with secret
  fields redacted, and the state that `gadgets_get_state` returned. It never records tokens.
  It rotates at 5 MiB and keeps 5 files.
