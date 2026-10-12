# Keep the gateway running

A Wi-Fi gadget needs the gateway running on your computer. Run `serve` in a terminal while you
test. When the setup works, install it as a background service. The service starts the gateway
now and again at every login, so your gadget comes back without a terminal.

On 12–13 October 2026 the owner's Mac and OLED kit passed name, address-change and restart
checks ([record](verification/always-on-2026-10-13.md)).

The design is hub [ADR 006](https://github.com/adidshaft/grok-gadgets/blob/main/docs/decisions/006-always-on-gateway.md).

## Install

Stop any `serve` you started by hand (Ctrl-C in its terminal). Then, in the gateway checkout:

```sh
uv run grok-gadgets-gateway service install --lan-listen 192.168.1.20
uv run grok-gadgets-gateway service status
```

Use the same `--lan-listen` address that you gave to `lan-cert` and `wifi-provision`. Leave it
out if you only use USB or the loopback MCP port.

| Computer | What `install` creates | When it starts |
| --- | --- | --- |
| macOS | A launchd agent: `~/Library/LaunchAgents/org.grokgadgets.gateway.plist` | Now, and at every login |
| Linux | A systemd user unit: `~/.config/systemd/user/grok-gadgets-gateway.service` | Now, and at every login |

On Linux, run `loginctl enable-linger` once to start it at boot without a login.

`install` refuses to run when the gateway is not set up, when the LAN certificate is missing, or
when another gateway already holds a port. It needs no administrator rights and writes no
system files.

## Check and stop

```sh
uv run grok-gadgets-gateway service status
uv run grok-gadgets-gateway service logs
uv run grok-gadgets-gateway service uninstall
```

`status` says whether the service is running. `logs` prints the end of the log; the request log
is in the same folder. `uninstall` stops the service and removes its file. Your config, CA and
device tokens stay.

After a restart, the gateway can take a minute or two to start while the computer finishes
logging in. The board shows **Gateway...** until then, and connects without help.

The service runs the gateway from this checkout. If you move or delete the checkout, run
`service uninstall` and install again from the new place.
