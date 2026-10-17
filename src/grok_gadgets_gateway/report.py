"""`builder report` (GW-BUILD-002, hub ADR 013 §3): what a builder's gadgets did, as counts.

It reads the relay's request log (with its rotated files), `firmware/status.json` and
`oauth/accounts.json` on the relay host. It shows devices, when each was last seen, calls a
day, error outcomes, firmware versions and whether someone claimed it. It never copies a
token, command arguments, reported state, an address, an account ID or a code.
"""

import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .operator import OperatorError

DEVICE_EVENTS = ("device_hello", "device_disconnected", "lan_hello", "lan_disconnected")


def _records(paths):
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OperatorError(f"cannot read {path}: {exc.strerror}") from None
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("ts"), str):
                yield record


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def build_report(paths, *, firmware_dir=None, accounts_path=None, today=None, days=7):
    """A dict of per-device counts. `paths` are request-log files, oldest first."""
    end = date.fromisoformat(today) if today else datetime.now(UTC).date()
    start = end - timedelta(days=days - 1)
    devices = defaultdict(
        lambda: {
            "name": None,
            "online": None,
            "last_seen": None,
            "calls": Counter(),
            "errors": Counter(),
            "firmware": None,
            "update": None,
            "claimed": None,
        }
    )
    without_device = 0
    for record in _records(paths):
        event, ts = record.get("event"), record["ts"]
        if event in DEVICE_EVENTS and isinstance(record.get("device"), str):
            entry = devices[record["device"]]
            entry["online"] = event.endswith("_hello")
            entry["last_seen"] = ts
        elif event == "mcp_tool_call":
            try:
                day = datetime.fromisoformat(ts).astimezone(UTC).date()
            except ValueError:
                continue
            if not start <= day <= end:
                continue
            device = record.get("device_id")
            if not isinstance(device, str):
                without_device += 1
                continue
            entry = devices[device]
            entry["calls"][day.isoformat()] += 1
            outcome = record.get("outcome")
            if outcome != "ok":
                entry["errors"][str(outcome)] += 1
        elif event == "update_applied" and isinstance(record.get("device"), str):
            devices[record["device"]]["firmware"] = record.get("version")

    status = (_read_json(Path(firmware_dir) / "status.json") or {}) if firmware_dir else {}
    for device, result in (status.get("devices") or {}).items():
        entry = devices[device]
        if result.get("reported"):
            entry["firmware"] = result["reported"]
        if result.get("version"):
            entry["update"] = {
                key: result[key] for key in ("version", "status", "reason") if result.get(key)
            }

    accounts = _read_json(accounts_path) if accounts_path else None
    if accounts:
        operator = accounts.get("operator")
        names = (accounts.get("labels") or {}).get(operator) or {}
        owned = accounts.get("devices") or {}
        for device, entry in devices.items():
            entry["claimed"] = bool((owned.get(device) or {}).get("owner"))
            # Only names the operator gave; another account's names stay private.
            entry["name"] = (names.get(device) or {}).get("name")

    out = {}
    for device in sorted(devices):
        entry = devices[device]
        out[device] = {
            **entry,
            "calls": dict(sorted(entry["calls"].items())),
            "calls_total": sum(entry["calls"].values()),
            "errors": dict(sorted(entry["errors"].items())),
        }
    return {
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "devices": out,
        "calls_without_device": without_device,
    }


def format_report(report):
    window = report["window"]
    lines = [f"Gadgets, {window['from']} to {window['to']} (UTC):", ""]
    if not report["devices"]:
        lines.append("No gadget appears in the request log yet.")
    for device, entry in report["devices"].items():
        title = f"{device} ({entry['name']})" if entry["name"] else device
        state = {True: "online", False: "offline", None: "never connected over TLS"}
        lines.append(title)
        lines.append(f"  {state[entry['online']]}; last seen {entry['last_seen'] or '-'}")
        claimed = {True: "yes", False: "no", None: "-"}[entry["claimed"]]
        lines.append(f"  claimed: {claimed}; firmware: {entry['firmware'] or '-'}")
        if entry["update"]:
            update = entry["update"]
            reason = f" ({update['reason']})" if update.get("reason") else ""
            lines.append(f"  last update: {update['version']} {update.get('status', '')}{reason}")
        days = ", ".join(f"{day[5:]} {count}" for day, count in entry["calls"].items())
        lines.append(f"  calls: {entry['calls_total']}" + (f" ({days})" if days else ""))
        if entry["errors"]:
            errors = ", ".join(f"{code} {count}" for code, count in entry["errors"].items())
            lines.append(f"  errors: {errors}")
    if report["calls_without_device"]:
        lines.append("")
        lines.append(
            f"Calls for no single gadget (lists, events): {report['calls_without_device']}"
        )
    return "\n".join(lines)
