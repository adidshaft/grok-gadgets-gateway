# Leave the Grok Bot route on

This guide keeps the [tunnel route](tunnel.md#oauth-sign-in-for-grok-bot) running every day, so Grok Bot on your Mac and phone can reach your gadgets from home and away. The design and threat model are in hub [ADR 008](https://github.com/adidshaft/grok-gadgets/blob/main/docs/decisions/008-daily-native-route.md).

**Status:** written for daily use and not yet recorded over several days on the owner's setup. Hub [#251](https://github.com/adidshaft/grok-gadgets/issues/251) tracks that record.

```text
login ─▶ gateway service (launchd/systemd) ─▶ 127.0.0.1:8766
login ─▶ tunnel client (user service)       ─▶ gadgets.example.net → 127.0.0.1:8766 only
```

You need a tunnel with a fixed hostname on your own domain. A quick tunnel changes its name at every start, and Grok Bot would need the server again each time.

## 1. Run the gateway as a service with sign-in

```sh
grok-gadgets-gateway service uninstall
grok-gadgets-gateway service install --lan-listen my-mac.local \
  --allowed-host gadgets.example.net --oauth
grok-gadgets-gateway service status
```

If `install` reports that a port is in use right after `uninstall`, wait a few seconds and run it again.

`service status` shows the allowed host, `--oauth`, the kill switch, live sign-ins, the last remote call and any sign-in flood.

## 2. Run the tunnel client as a user service

The tunnel client must start at login, like the gateway, and forward only `http://127.0.0.1:8766`. With Cloudflare Tunnel, a named tunnel uses a config file such as `~/.cloudflared/config.yml`:

```yaml
tunnel: TUNNEL-ID
credentials-file: /Users/you/.cloudflared/TUNNEL-ID.json
ingress:
  - hostname: gadgets.example.net
    service: http://127.0.0.1:8766
  - service: http_status:404
```

The last rule answers 404 for any other hostname. Then install the client as a login agent, without `sudo`:

```sh
cloudflared service install
```

Cloudflare's [macOS service guide](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/local-management/as-a-service/macos/) describes this command. With `sudo`, it installs a system daemon instead; this project does not need one. For another provider, use its own user-service setup with the same rule: one hostname, one target, `127.0.0.1:8766`.

Never point the tunnel at port 8765, 8767 or any device address.

## 3. Daily check

About a minute, once a day or after a change:

```sh
grok-gadgets-gateway service status
curl -s -o /dev/null -w "%{http_code}\n" https://gadgets.example.net/.well-known/oauth-protected-resource/mcp
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://gadgets.example.net/mcp
grok-gadgets-gateway usage --token grok-bot --since "$(date -u +%Y-%m-%d)"
```

Expected:

- `service: running`, the allowed host, `sign-in (--oauth): on`, `remote: on`.
- `live sign-ins:` lists only names that you approved, such as `grok-bot`.
- `sign-in flood: none in the last 24 hours`. If not, see [Floods](#floods).
- The metadata check gives `200`, and `/mcp` without a token gives `401`.
- `usage` lists only calls that you expect.

## 4. Stay reachable: sleep

A sleeping Mac runs neither the gateway nor the tunnel. Grok Bot then gets an error from the tunnel provider, not a gadget result, and nothing is queued for later. The board shows **Gateway...** until the Mac wakes.

To stay reachable while the Mac is on power:

- System Settings → Battery → Options: turn on **Prevent automatic sleeping on power adapter when the display is off**.
- A closed lid still puts the Mac to sleep, unless an external display, keyboard and power are connected.
- For a session, `caffeinate -s` keeps the Mac awake on power while it runs.

On battery the Mac sleeps as usual. Decide whether your Mac stays awake; the gateway does not change power settings.

## 5. Notify on each remote call (optional)

[`examples/usage-hooks/macos-notify.py`](../examples/usage-hooks/macos-notify.py) posts a local macOS notification for each remote tool call. It reads the gateway's [usage lines](usage.md#usage-hook), never tokens or arguments, and calls `osascript` on the Mac. Nothing leaves the computer. Loopback calls are ignored, and a burst becomes one notification with a count.

```sh
grok-gadgets-gateway service install --lan-listen my-mac.local \
  --allowed-host gadgets.example.net --oauth \
  --usage-hook "/ABSOLUTE/PATH/.venv/bin/python /ABSOLUTE/PATH/macos-notify.py"
```

The service needs absolute paths. Notifications appear only if macOS allows them for the program that shows them; check System Settings → Notifications. Test the script without the gateway:

```sh
echo '{"event":"usage","remote":true,"token":"grok-bot","tool":"gadgets_list_devices","outcome":"ok"}' \
  | python3 macos-notify.py --print
```

Expected: `Remote call as grok-bot: gadgets_list_devices`.

## Floods

Anyone who learns the hostname can ask to register or sign in. The gateway limits this ([details](tunnel.md#oauth-sign-in-for-grok-bot)), and your live sign-in keeps working. `service status` and the request log show `oauth_flood` lines while a limit is hit.

- Approve only the code that your own sign-in page shows, right after you start it.
- To stop all remote access at once: `grok-gadgets-gateway remote off`. Loopback, `rehearse` and devices keep working.
- To end a sign-in: `grok-gadgets-gateway oauth revoke grok-bot`.
- Stop the tunnel client to take the hostname offline.

Then run `remote on` and sign in again when you are ready.

## After a restart

The gateway and the tunnel client start at login. The board returns to **Online** without help. The gateway keeps sign-ins in `oauth/state.json`, so a sign-in should survive a gateway restart; this is not yet recorded with Grok Bot. The time from login to a working phone call is not yet recorded; hub [#251](https://github.com/adidshaft/grok-gadgets/issues/251) and [#115](https://github.com/adidshaft/grok-gadgets-gateway/issues/115) track it.
