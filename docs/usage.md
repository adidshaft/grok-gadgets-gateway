# Track usage

The gateway counts every tool call that reaches it. Use it to see which token, device and capability your Grok Bot uses, and to feed your own tools. Design: [GW-USAGE-001](https://github.com/adidshaft/grok-gadgets-gateway/issues/132).

The gateway never reads your Grok Bot account. Grok Bot shows its own plan usage in **Settings → Weekly usage limit** on the desktop and **Weekly usage** on the phone ([hub research note](https://github.com/adidshaft/grok-gadgets/blob/dev/docs/research/grok-remote-https-2026-10.md#5-usage-and-credits)). Any credits figure from the gateway is an **estimate**.

## What each call records

With `--request-log PATH`, each tool call adds one `mcp_tool_call` line:

| Field | Meaning |
| --- | --- |
| `token` | Name of the bearer token (`grok-bot`, `default`); never the token |
| `remote` | `true` through a tunnel, `false` on loopback, `null` on stdio |
| `client` | Name the client sent in `initialize`; self-reported |
| `tool`, `device_id`, `capability` | What was called |
| `outcome`, `status` | `ok` or the error code; the command status |
| `duration_ms` | Time in the gateway |
| `bytes_in`, `bytes_out` | Size of the tool arguments and the result, as compact JSON |

The always-on service writes this log already: `grok-gadgets-gateway service status` shows where.

## Summarize

```sh
grok-gadgets-gateway usage
```

Without `--log`, it reads the service's `requests.jsonl`. Example output from a test log:

```text
total:  calls=4  errors=1  commands=2  duration_ms=40.0  bytes=80
by token:
  default  calls=1  errors=1  commands=1  duration_ms=10.0  bytes=20
  grok-bot  calls=3  errors=0  commands=1  duration_ms=30.0  bytes=60
by device:
  -  calls=1  errors=0  commands=0  duration_ms=10.0  bytes=20
  esp-1  calls=1  errors=1  commands=1  duration_ms=10.0  bytes=20
  sim-c124  calls=2  errors=0  commands=1  duration_ms=20.0  bytes=40
by capability:
  -  calls=2  errors=0  commands=0  duration_ms=20.0  bytes=40
  rgb.set  calls=1  errors=0  commands=1  duration_ms=10.0  bytes=20
  text.show  calls=1  errors=1  commands=1  duration_ms=10.0  bytes=20
by day:
  2026-10-13  calls=3  errors=0  commands=1  duration_ms=30.0  bytes=60
  2026-10-14  calls=1  errors=1  commands=1  duration_ms=10.0  bytes=20
Days are UTC. Counts come from this gateway's request log only.
```

A `-` row holds calls without a device or capability, such as `gadgets_list_devices`.

The request log rotates at 5 MiB and keeps 5 old files (`requests.jsonl.1` is the newest). `usage` reads them too, so a summary covers the last 25 to 30 MiB of calls.

Options:

- `--log PATH`: read this log; repeat it for several logs.
- `--since 2026-10-13`, `--until 2026-10-13T10:00`: a window. Times without a zone are UTC. A plain `--until` date includes that whole day.
- `--token grok-bot`: only that token.
- `--json`: the same summary as JSON.

## Estimate credits

The gateway cannot see Grok Bot credits. Grok Bot measures plan usage by its own work, not by tool calls. A per-call figure is therefore an average, labelled **estimate**.

Set a rate yourself:

```sh
grok-gadgets-gateway usage --token grok-bot --rate 0.2 --unit "% weekly usage"
```

Or calibrate it from two readings that you took yourself. Read **Weekly usage** before and after a session, and note both times:

```sh
grok-gadgets-gateway usage --token grok-bot \
  --since 2026-10-13T09:00 --until 2026-10-13T09:30 \
  --calibrate 1.5 --unit "% weekly usage"
```

`--calibrate` divides your measured change (1.5) by the calls in that window and saves the rate to `usage-rate.json` next to your tokens. Later summaries add an `estimate` column and say where the rate came from. Calibrate again whenever you take new readings. The estimate includes everything Grok Bot did in that window, not only tool calls.

## Usage hook

`--usage-hook CMD` sends one JSON line per tool call to a program of yours, for example a dashboard or a meter:

```sh
grok-gadgets-gateway serve --request-log ~/grok-gadgets-requests.jsonl \
  --usage-hook "/usr/bin/tee -a /tmp/usage.jsonl"
```

`stdio` takes the same option. The gateway starts `CMD` once, without a shell, and writes to its standard input:

```json
{"event":"usage","ts":"2026-10-13T09:00:05+00:00","request_id":"…","transport":"streamable-http","token":"grok-bot","remote":true,"client":"…","tool":"gadgets_command","device_id":"sim-c124","capability":"rgb.set","outcome":"ok","status":"executed","simulated":true,"duration_ms":41.2,"bytes_in":71,"bytes_out":402}
```

Rules:

- **No secrets or arguments.** A line holds only the fields above: never a token, command arguments, an argument hash, device state or the user agent.
- **Never blocks a call.** Lines go to a bounded queue. If your program is slow, stops or cannot start, lines are dropped and counted. The call itself is not delayed or changed.
- The program's output is discarded. Environment variables that start with `GROK_GADGETS_` are removed.
- If the program exits, the gateway starts it again for a later line, at most once every 10 seconds.
- When `serve` stops, it prints `Usage hook: N lines sent, M dropped.` A `stdio` request log records the same counts in `stdio_stopped`.

Use `usage` for totals. The hook is for live use; it has no history.

Example: [`examples/usage-hooks/macos-notify.py`](../examples/usage-hooks/macos-notify.py) posts a local macOS notification for each remote call. `service install --usage-hook` passes a hook to the service; see [daily use](daily-use.md#5-notify-on-each-remote-call-optional).
