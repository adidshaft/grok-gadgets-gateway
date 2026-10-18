# Relay host: run the gateway away from your Mac

A relay is this gateway on an always-on Linux host. Wi-Fi devices connect out to it over TLS.
Grok Bot reaches it through your tunnel with OAuth sign-in. Your Mac can then sleep or stay
off. The design is in hub
[ADR 009](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/decisions/009-relay-route.md).

**Status:** on 13 October 2026 the owner's ESP32-S3 OLED kit ran through a relay on a small
Linux server with the user-service route below. Grok Bot on the iPhone, on cellular, showed text
on it while the Mac slept ([record](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/verification/mac-free-route-2026-10-13.md)). The container route was checked only on a Mac.
Run one relay for your own gadgets only. It has one operator and no accounts.

```text
Grok Bot ─HTTPS + OAuth─▶ gadgets.example.net (tunnel) ─▶ 127.0.0.1:8766 /mcp   ┐
Wi-Fi device ─TLS, outbound─▶ devices.example.net:8444 (relay device listener)  ┘ relay host
```

## What you need

- A Linux host that stays on: a small VPS, or a home Linux box. It needs a public address that
  devices can reach on TCP port 8444. A home box usually needs a router port forward; review
  that plan before you open a port.
- Two names on a domain you control. `gadgets.example.net` goes through your tunnel to `/mcp`.
  `devices.example.net` is a DNS-only record (no proxy) that points at the host's address.

> **Warning: the device name must be DNS only.** If the record for `devices.example.net` is
> proxied (orange cloud in Cloudflare), devices meet the proxy's certificate instead of the
> relay's, and every device's CA pin fails. Cloudflare proxies port 8443, so a proxied record
> can look like it works. `grok-gadgets-gateway relay-check devices.example.net` refuses a
> proxied record, and `wifi-provision --gateway` runs the same check before it sends anything.

**Port.** New relays use port 8444. A relay set up before 14 October 2026 uses 8443: keep it,
because its devices store that port. Pass `--device-listen 0.0.0.0:8443` and
`--gateway devices.example.net:8443` for such a relay.
- A tunnel client for `gadgets.example.net`. The example uses Cloudflare Tunnel; the same rule
  holds for any provider: one hostname, one target, `http://127.0.0.1:8766`.
- Your board on firmware that accepts a DNS name for the gateway, and a computer with USB to
  provision it once.

Never publish port 8766, and never send the device port through the tunnel.

## Start here: `relay init`

`relay init` writes the files for a new relay into a new folder. They hold no secret.

```sh
grok-gadgets-gateway relay init --mcp-host gadgets.example.net \
  --device-host devices.example.net --out my-relay
```

- `setup-relay.sh` sets up a fresh Ubuntu 24.04 host as root. It opens SSH and TCP 8444,
  installs the tunnel client and this gateway release, and installs the relay as a user
  service with accounts.
- `cloud-init.yaml` runs that script when the host first starts. Paste it as the host's user
  data.
- `NEXT-STEPS.md` lists what is left by hand: the DNS-only record, the tunnel, the CA copy and
  the preflight.

`--mode compose` writes Docker Compose files instead. `--no-accounts` makes a relay for one
operator. The two options below describe the same setup step by step.

When the host is up, run the preflight from your computer. Every line must start with `ok`:

```sh
grok-gadgets-gateway relay-check devices.example.net --ca relay-ca.pem \
  --mcp https://gadgets.example.net/mcp
```

It checks five things:

1. The device name is a DNS-only record.
2. The device port is open.
3. The port shows the relay's CA, with its SHA-256 fingerprint.
4. The device port refuses HTTP.
5. `/mcp` asks for a sign-in.

## Option A: container (Docker Compose)

`examples/docker/` holds a Dockerfile and a compose file. The gateway container runs
`serve --oauth --allowed-host --device-listen` and keeps config, CA, tokens and logs in the
`gateway-data` volume. The `cloudflared` container shares the gateway's network, so it reaches
`127.0.0.1:8766` while that port stays unpublished.

1. Get the code at the commit or release tag you want to run:

   ```sh
   git clone https://github.com/adidshaft/grok-gadgets-gateway.git
   cd grok-gadgets-gateway
   git checkout v0.1.0-alpha.18        # or a reviewed commit
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
   docker compose run --rm gateway enroll oledkit-0123456789ab \
     --token-file /data/config/grok-gadgets/oledkit.token
   ```

   `init` prints a local bearer token. You do not need it for Grok Bot, which signs in with
   OAuth. Do not paste it anywhere.

4. Start both containers, and open TCP port 8444 in the host's firewall:

   ```sh
   docker compose up -d
   docker compose logs gateway
   ```

   The log shows `Relay TLS device listener devices.example.net on 0.0.0.0:8444`.

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
uv tool install grok-gadgets-gateway==0.1.0a18
grok-gadgets-gateway init
grok-gadgets-gateway device-cert devices.example.net
grok-gadgets-gateway enroll oledkit-0123456789ab --token-file oledkit.token
grok-gadgets-gateway service install --allowed-host gadgets.example.net --oauth \
  --device-listen 0.0.0.0:8444
loginctl enable-linger              # start at boot, without a login
grok-gadgets-gateway service status
```

Run the tunnel client as a system service with its own instructions, forwarding only
`http://127.0.0.1:8766`. [Always on](always-on.md) covers the service commands.

## Provision the board

The board is on USB at your computer, and the relay's CA and the token file are on the host.
The relay's `credentials.json` keeps only token hashes. Copy two files to the computer,
provision, then delete them. With the container:

```sh
docker compose cp gateway:/data/config/grok-gadgets/lan/ca.pem ./relay-ca.pem
docker compose cp gateway:/data/config/grok-gadgets/oledkit.token ./oledkit.token
```

With the user service, the files are `~/.config/grok-gadgets/lan/ca.pem` and the
`oledkit.token` you wrote with `enroll`. Move them to your computer, for example with `scp`,
and delete the token file on the host. Then, on the computer with the board:

```sh
grok-gadgets-gateway wifi-provision /dev/cu.usbmodem101 --device oledkit-0123456789ab \
  --gateway devices.example.net --ca relay-ca.pem --token-file oledkit.token
rm oledkit.token relay-ca.pem
```

The command asks for the Wi-Fi name and password in your terminal and sends them only to the
board. They never reach the relay. The token file is the device's credential; delete it at
once. A lost token file is no problem: run `enroll DEVICE --rotate --token-file FILE` on the
relay and provision again. After provisioning, power the board from a USB charger. It shows **Online** when
the relay accepts it.

## Prepare many gadgets at once

A batch enrolls many gadgets and prints their setup labels in one step. An ESP32 board's ID
comes from its chip, so first read each board's ID over USB and list them in a CSV:

```sh
grok-gadgets-gateway factory-provision /dev/cu.usbmodem101 --read-id    # prints oledkit-…
printf 'device_id\noledkit-0123456789ab\noledkit-0123456789ac\n' > ids.csv
grok-gadgets-gateway enroll --batch ids.csv --out batch-1
```

For Linux gadgets, which choose their own IDs, use a count and a prefix:

```sh
grok-gadgets-gateway enroll --count 3 --prefix lamp- --out batch-2 --no-labels
```

The folder holds one token file for each gadget in `tokens/` (mode 0600, shown nowhere else). It also holds the setup labels in `labels/` (`print.html` prints them all) and `batch.csv`. The CSV lists
the devices and their files, never a secret. Copy the folder to the computer with the boards
privately, for example with `scp -r`. Then provision each board from the batch. The command
reads the board's ID, sends that row's token and label, and deletes the token file:

```sh
grok-gadgets-gateway factory-provision /dev/cu.usbmodem101 --gateway devices.example.net \
  --ca relay-ca.pem --batch batch-1
```

Keep the batch folder off shared drives and out of chats until every row says
`provisioned: yes`. A Linux gadget takes its token file with `--token-file` instead.

## How many gadgets one relay holds

A relay holds 64 gadgets by default. A 65th device ID gets `busy` until one leaves.
`serve --max-devices N` (at most 2,000) raises the limit for the registry and the device
port together; `service install` takes the same flag. Check the measured sizes in the hub
[support matrix](https://github.com/adidshaft/grok-gadgets/blob/main/docs/public/support-matrix.md)
before you raise it. They name the host size, the gateway version and the poll rate.

Measure your own host on a **test relay** with test tokens, never on a relay with real
gadgets. A batch holds up to 500 gadgets, so make several for a larger fleet:

```sh
# On the test relay host
grok-gadgets-gateway init && grok-gadgets-gateway device-cert devices.example.net
grok-gadgets-gateway enroll --batch fleet-ids.csv --out fleet-1 --no-labels   # a device_id column
grok-gadgets-gateway serve --device-listen 0.0.0.0:8444 --max-devices 1000 \
  --commands-per-minute 120 &
grok-gadgets-gateway fleet sample --pid "$!" --out sample.csv          # Ctrl-C when done

# On another computer, with fleet-1/ and the relay's ca.pem copied privately
ssh -N -L 18766:127.0.0.1:8766 grokgadgets@devices.example.net &    # MCP for the timer
grok-gadgets-gateway fleet run --batch fleet-1 --count 200 --gateway devices.example.net \
  --ca relay-ca.pem --mcp-url http://127.0.0.1:18766/mcp --mcp-token-file mcp-token \
  --duration 900 --out results-200
```

Each simulated gadget pins the CA, checks the name, polls every 100 ms like the ESP32 firmware
and acknowledges each command. The fleet ramps up at 18 new connections a minute, inside the
relay's limit of 20 for one address. So 1,000 gadgets take about an hour to connect. A driver
sends one `gadgets_command` a second and times it until `executed`. `results-200/` holds
`summary.json` and `fleet.csv`; `sample.csv` holds the gateway's CPU share and memory. The
files hold counts and timings only, never a token, address or name. `--connect ADDRESS` dials
an address when the name is not in DNS yet; TLS still checks the name.

## Connect Grok Bot

Ask Grok Bot to add `https://gadgets.example.net/mcp` as a Remote HTTPS server with OAuth
sign-in, and approve the code on the relay with `oauth approve`. The hub guide
[Connect your Grok Bot](https://github.com/adidshaft/grok-gadgets/blob/main/docs/getting-started/connect-grok-bot.md)
has the exact chat text. If Grok Bot already lists `grok-gadgets` for the Mac, ask it to replace that server. The hostname can stay the same if the tunnel moved with it.

## Accounts: other people claim their own gadgets

Add `--accounts` to `serve --oauth` (or `service install --oauth --accounts`) when other
people use the relay (hub ADR 011). Then:

- A gadget with SDK firmware that supports claim codes shows `Code XXXX-XXXX` while nobody
  owns it. The person adds the relay in their Grok Bot and types that code on the sign-in
  page. The relay creates their account, gives them the gadget and hands the sign-in straight
  back to Grok Bot. They then ask Grok Bot for a recovery code (`gadgets_share action=recovery_code`) and keep it. It signs them in again after 30 days without use, or on a new phone. Asking again replaces the old code.
- The shared bearer tokens (`mcp-token` and named tokens) stop working on `/mcp`. Only
  sign-ins with an account call tools.
- Your own `oauth approve CODE --name NAME` still works and signs in to the operator
  account.
- On the first start in accounts mode, every enrolled device and every live sign-in, such as
  `grok-bot`, joins the operator account. Back up the config directory first.
- Every tool answers only for the caller's own gadgets and shares. Another account's gadget,
  command or event looks exactly like one that does not exist. The operator account sees
  only its own gadgets.
- Three more tools: `gadgets_claim` adds a gadget by its code or accepts a share code. `gadgets_remove` unclaims a gadget (its shares end and it shows a new code) or drops a share. `gadgets_share` makes a 10-minute, one-use share code (`use` or `view`), and lists and revokes shares. It also makes a link code that signs a second app in to the same account.
- **Handing a gadget on.** A BOOT reset never changes the owner on the relay. After a reset
  and a phone re-join, the gadget shows a transfer code for 10 minutes. The new person enters
  it on the sign-in page or with `gadgets_claim`: the transfer is pending, and the old owner
  keeps full use. It completes when the old owner removes the gadget, or after 7 days. The old
  owner sees `pending_transfer` in the list and can stop it with
  `gadgets_share action=refuse_transfer`. Lines: `transfer_pending`, `transfer_done` (with
  `via` `unclaim` or `grace_period`), `transfer_refused`, `transfer_withdrawn`.
- `gadgets_name` gives a gadget a plain name and a room ("Desk display", "Office") for that
  person only. Every tool then accepts the name, and the list says each gadget in one line,
  for example "Desk display, Office, online, yours". Grok Bot is told at connect time to ask
  for a name after each claim.
- `accounts list` shows the accounts and how many gadgets each owns, never a code.
- Operator changes reach the running relay through queued files in `oauth/admin/`:
  `accounts unclaim DEVICE`, `accounts assign DEVICE ACCOUNT`, and `accounts link ACCOUNT`,
  which prints a 10-minute code that signs a new app in to that account. Use it when someone
  lost their recovery code. `oauth revoke ACCOUNT` ends every sign-in of an account.
- `rehearse --accounts` runs the whole flow on a private, temporary relay with two simulated gadgets. It covers two sign-ins, isolation, a share and its revocation, and a transfer. It uses no
  configuration of this computer.
- Codes live in memory only. The relay limits wrong codes: 5 per sign-in, 20 an hour per address and 200 an hour for the relay. After that, code entry closes for 10 minutes with a `claim_flood` line. `account_created`, `device_claimed` and `claim_rejected` lines record
  claims without codes.

## Daily check

From any computer, not the relay host:

```sh
curl -s -o /dev/null -w "%{http_code}\n" https://gadgets.example.net/.well-known/oauth-protected-resource/mcp   # 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://gadgets.example.net/mcp                                 # 401
curl -s -m 5 http://devices.example.net:8444/ ; echo "exit $?"                                                   # no HTTP answer
grok-gadgets-gateway relay-check devices.example.net --ca relay-ca.pem                                           # DNS only, relay CA
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
- Keep the host's OS updated and allow only SSH and the device port (TCP 8444, or 8443 on older relays) in.
- Anyone with a shell on the host can approve sign-ins and read device tokens.
- The relay sees device IDs, commands, reported state and events. The request log file is for
  the operator. It records command arguments, such as the text sent to a display, with secret
  fields redacted, and the state that `gadgets_get_state` returned. It never records tokens.
  It rotates at 5 MiB and keeps 5 files.
