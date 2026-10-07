# First success in five minutes

Run the gateway with its simulated light. Then rehearse what your Grok Bot will do: call the
six gadgets tools in the same order, on your own computer. You need no hardware, account or
API key.

You need Git and [uv](https://docs.astral.sh/uv/getting-started/installation/).

## 1. Start the gateway

```sh
git clone https://github.com/adidshaft/grok-gadgets-gateway.git
cd grok-gadgets-gateway
uv sync --locked
uv run grok-gadgets-gateway init
uv run grok-gadgets-gateway serve --simulator
```

Leave it running. It serves MCP on `http://127.0.0.1:8766/mcp` and accepts only the bearer
token that `init` wrote to `~/.config/grok-gadgets/mcp-token`.

## 2. Rehearse the Grok Bot calls

In a second terminal, in the same folder:

```sh
uv run grok-gadgets-gateway rehearse
```

Expected output:

```text
ok  http://127.0.0.1:8766/mcp offers the six tools Grok Bot will use
ok  gadget sim-c124 (simulated); commands: rgb.set
ok  gadgets_command rgb.set {"r":0,"g":120,"b":255,"on":true} -> executed
ok  gadgets_get_state sim-c124: {"rgb": {"r": 0, "g": 120, "b": 255, "on": true}, "button": {"pressed": false}}
Rehearsal passed: these are the calls Grok Bot will make. It does not prove a Grok Bot connection or any physical effect.
```

`rehearse` connects with the same bearer token and the same MCP tools that Grok Bot's custom
MCP connector will use. For your own gadget, name the command and its arguments:

```sh
uv run grok-gadgets-gateway rehearse --device desk-lamp --command set.light --args '{"on": true}'
```

## What this proves

The gateway, the simulator and the six tools work on your computer. It does not prove a Grok
Bot connection or any physical effect.

CI runs this path from a clean checkout on every push and every night:
[`scripts/check_first_success.py`](../scripts/check_first_success.py) runs the commands from the
README quick start, including `rehearse`.

## If something fails

| Symptom | Next step |
| --- | --- |
| `no authenticated gateway answers` | Start `serve` first. If you changed `--port` on `serve`, pass the same `--port` to `rehearse`. |
| `MCP token file does not exist` | Run `uv run grok-gadgets-gateway init`. |
| Port 8766 or 8765 in use | Stop the other process, or pass `--port` and `--device-port` to `serve`. |
| `no gadget offers rgb.set` | Start `serve` with `--simulator`. |
