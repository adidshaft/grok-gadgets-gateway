"""Gadget Credits (hub ADR 015, Stage 0): a balance per account, enforced in the call path.

`serve --oauth --accounts --credits` owns two files beside `accounts.json`:

- `oauth/credits.json`: the relay's settings (meter weights, the default monthly allowance)
  and any per-account allowance.
- `oauth/credits-ledger.jsonl`: the append-only ledger. A balance is the sum of its
  account's entries; nothing else changes a balance.

The gate sits in `gadgets_command`, after the rate limit and the access check and before the
gateway queues the command. It debits first and refunds when the command does not go out.
The usage hook never changes a balance. The operator account is never debited. There is no
price, currency, payment or link anywhere in this module.
"""

import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .operator import OperatorError, atomic_write
from .protocol import GatewayError

OUT_OF_CREDITS = "Your credits on this relay are used up; ask its operator."
DEFAULT_SETTINGS = {"command_weight": 1, "gadget_day_weight": 1, "allowance": 0}
KINDS = ("command", "refund", "gadget_day", "grant", "allowance")
MAX_SETTING = 1_000_000


def _day(seconds):
    return datetime.fromtimestamp(seconds, UTC).date().isoformat()


def _whole(value, name, low=0):
    if type(value) is not int or not low <= value <= MAX_SETTING:
        raise OperatorError(f"{name} must be a whole number from {low} to {MAX_SETTING}")
    return value


def read_ledger(path):
    """Ledger entries in order; a torn last line (a crash mid-write) is skipped."""
    entries = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return entries
    except OSError as exc:
        raise OperatorError(f"cannot read {path}: {exc.strerror}") from None
    for number, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
        except ValueError:
            if number == len(lines):
                continue
            raise OperatorError(f"{path} line {number} is damaged") from None
        if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
            raise OperatorError(f"{path} line {number} is damaged")
        entries.append(entry)
    return entries


class Credits:
    """Balances, the gate and gadget-day metering for one relay."""

    def __init__(self, directory, accounts, *, log=None, clock=time.time, receipts=None):
        folder = Path(directory)
        self.path = folder / "credits.json"
        self.ledger_path = folder / "credits-ledger.jsonl"
        self.admin_dir = folder / "credits-admin"
        self.accounts = accounts
        self.receipts = receipts
        self.log = log
        self.clock = clock
        self.lock = threading.Lock()
        self.data = None
        self.balances = {}
        self.metered = set()  # (account, device, day) already charged a gadget-day
        self.refilled = set()  # (account, month) whose allowance was applied
        self.seq = 0
        self.file = None
        self.closed_day = None  # the UTC day whose start was the last daily receipt cut

    # --- storage ---

    def open(self):
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {"settings": dict(DEFAULT_SETTINGS), "accounts": {}}
            self._save()
        except (OSError, ValueError):
            raise OperatorError(f"{self.path} is damaged") from None
        self.data.setdefault("settings", {})
        for key, value in DEFAULT_SETTINGS.items():
            self.data["settings"].setdefault(key, value)
        self.data.setdefault("accounts", {})
        for entry in read_ledger(self.ledger_path):
            self._count(entry)
        self._trim_torn_tail()
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.ledger_path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        self.file = os.fdopen(descriptor, "a", encoding="utf-8")
        self._write("credits_opened", accounts=len(self.balances), entries=self.seq)
        return self

    def _trim_torn_tail(self):
        """Cut a last line that a crash left without its newline, so the next entry is whole."""
        try:
            with open(self.ledger_path, "rb+") as file:
                data = file.read()
                if data and not data.endswith(b"\n"):
                    file.truncate(data.rfind(b"\n") + 1)
                    self._write("credits_ledger_trimmed", bytes=len(data) - data.rfind(b"\n") - 1)
        except FileNotFoundError:
            pass

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None

    def _save(self):
        atomic_write(self.path, (json.dumps(self.data, indent=1, sort_keys=True) + "\n").encode())

    def _write(self, event, **fields):
        if self.log is not None:
            self.log.append({"event": event, **fields})

    def _count(self, entry):
        account = entry["account"]
        self.balances[account] = self.balances.get(account, 0) + entry["delta"]
        self.seq = max(self.seq, entry.get("seq", 0))
        if entry["kind"] == "gadget_day":
            self.metered.add((account, entry["device"], entry["day"]))
        if entry["kind"] == "allowance":
            self.refilled.add((account, entry["month"]))

    def _append(self, account, kind, delta, **fields):
        """Write one ledger entry (flushed and synced) and apply it; returns the entry."""
        self.seq += 1
        entry = {
            "seq": self.seq,
            "ts": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            "account": account,
            "kind": kind,
            "delta": delta,
            "balance": self.balances.get(account, 0) + delta,
            **fields,
        }
        self.file.write(json.dumps(entry, separators=(",", ":")) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())
        self._count(entry)
        return entry

    # --- settings ---

    @property
    def settings(self):
        return dict(self.data["settings"])

    def allowance(self, account):
        own = (self.data["accounts"].get(account) or {}).get("allowance")
        return self.data["settings"]["allowance"] if own is None else own

    def set_settings(self, **changes):
        with self.lock:
            for key, value in changes.items():
                if key not in DEFAULT_SETTINGS:
                    raise OperatorError(f"unknown setting {key}")
                self.data["settings"][key] = _whole(value, key)
            self._save()
        self._write("credits_settings", **changes)

    def set_allowance(self, account, value):
        self._known(account)
        with self.lock:
            entry = self.data["accounts"].setdefault(account, {})
            if value is None:
                entry.pop("allowance", None)
            else:
                entry["allowance"] = _whole(value, "allowance")
            self._save()
        self._write("credits_allowance", account=account, allowance=value)

    # --- balances ---

    def exempt(self, account):
        return account is None or account == self.accounts.operator

    def _known(self, account):
        if account not in self.accounts.data["accounts"]:
            raise OperatorError(f"no account {account}")

    def balance(self, account):
        """This account's balance; an account never seen in the ledger has 0."""
        return self.balances.get(account, 0)

    def view(self, account):
        """What `gadgets_list_devices` shows the caller about their own credits."""
        if self.exempt(account):
            return {"exempt": True}
        with self.lock:
            self._refill(account)
            return {"balance": self.balance(account)}

    def _refill(self, account):
        """On the first metered event of a UTC month, raise a low balance to the allowance."""
        month = _day(self.clock())[:7]
        if (account, month) in self.refilled:
            return
        allowance = self.allowance(account)
        balance = self.balance(account)
        if allowance > balance:
            self._append(account, "allowance", allowance - balance, month=month)
        else:
            self.refilled.add((account, month))

    def grant(self, account, amount, *, via="operator"):
        """Add credits (or take them back with a negative amount); returns the new balance."""
        self._known(account)
        if type(amount) is not int or amount == 0 or abs(amount) > MAX_SETTING:
            raise OperatorError(f"amount must be a whole number, not 0, at most {MAX_SETTING}")
        with self.lock:
            entry = self._append(account, "grant", amount, via=via)
        self._write("credits_granted", account=account, amount=amount, balance=entry["balance"])
        return entry["balance"]

    # --- the gate (GW-CREDITS-001) ---

    def charge(self, account, device_id, request_id):
        """Debit one command before it runs; returns a charge to settle, or None if exempt.

        Raises `out_of_credits` and debits nothing when the balance is below the weight.
        """
        if self.exempt(account):
            return None
        weight = self.data["settings"]["command_weight"]
        if weight == 0:
            return None
        with self.lock:
            self._refill(account)
            if self.balance(account) < weight:
                self._write("credits_refused", account=account, request_id=request_id)
                raise GatewayError("out_of_credits", OUT_OF_CREDITS)
            entry = self._append(
                account, "command", -weight, device=device_id, request_id=request_id
            )
        return {"account": account, "amount": weight, "seq": entry["seq"], "settled": False}

    def refund(self, charge, reason):
        """Give back a charge whose command did not go out; a second refund does nothing."""
        if charge is None or charge["settled"]:
            return
        charge["settled"] = True
        with self.lock:
            self._append(
                charge["account"],
                "refund",
                charge["amount"],
                refund_of=charge["seq"],
                reason=reason,
            )

    # --- gadget-days (GW-CREDITS-002) ---

    def meter_days(self, online):
        """Charge each owned gadget that is online today once a day; returns how many."""
        weight = self.data["settings"]["gadget_day_weight"]
        if weight == 0:
            return 0
        today = _day(self.clock())
        charged = 0
        with self.lock:
            for device_id in sorted(online):
                owner = self.accounts.owner(device_id)
                if self.exempt(owner) or (owner, device_id, today) in self.metered:
                    continue
                self._refill(owner)
                # A gadget-day never refuses anything; it may take the balance below zero.
                self._append(owner, "gadget_day", -weight, device=device_id, day=today)
                charged += 1
        return charged

    # --- receipts (GW-CREDITS-002) ---

    def close_receipts(self, now=False):
        """Receipt every account's entries before today (UTC), or before now when asked."""
        if self.receipts is None:
            return []
        clock = self.clock()
        moment = datetime.fromtimestamp(clock, UTC)
        cut = moment if now else moment.replace(hour=0, minute=0, second=0, microsecond=0)
        if not now and self.closed_day == cut.date():
            return []
        self.closed_day = None if now else cut.date()
        with self.lock:
            entries = read_ledger(self.ledger_path)
            return self.receipts.close(entries, cut.isoformat(), moment.isoformat(), now)

    # --- operator commands: the CLI queues files, serve applies them ---

    def apply_admin(self):
        applied = 0
        folder = self.admin_dir
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                request = json.loads(path.read_text())
                action = request["action"]
                if action == "grant":
                    self.grant(request["account"], request["amount"])
                elif action == "settings":
                    self.set_settings(**request["settings"])
                elif action == "allowance":
                    self.set_allowance(request["account"], request["allowance"])
                elif action == "close":
                    self.close_receipts(now=True)
                else:
                    raise ValueError(action)
                applied += 1
            except (OSError, ValueError, KeyError, TypeError, OperatorError) as exc:
                self._write("credits_admin_rejected", reason=type(exc).__name__)
            path.unlink(missing_ok=True)
        return applied


def queue_admin(directory, request):
    """CLI side: queue one credits change for `serve` to apply within a second."""
    folder = Path(directory) / "credits-admin"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    name = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    atomic_write(folder / name, json.dumps({**request, "created": time.time()}).encode())
    return folder / name


def report(entries, calls, *, operator=None, since=None, until=None):
    """The operator's view of credits per account, in credits only (no currency).

    `entries` are ledger entries and `calls` `mcp_tool_call` records, both already read.
    Reconciliation matches each command debit that was kept with the request record of the
    same `request_id` that says it kept credits.
    """

    def inside(ts):
        return (since is None or ts >= since) and (until is None or ts <= until)

    rows = {}
    requests = {e["seq"]: e.get("request_id") for e in entries if e["kind"] == "command"}
    kept, refunded = {}, set()
    for entry in entries:
        if not inside(entry["ts"]):
            continue
        row = rows.setdefault(
            entry["account"],
            {
                "grants": 0,
                "allowance": 0,
                "commands": 0,
                "refunds": 0,
                "gadget_days": 0,
                "spent": 0,
                "balance": 0,
            },
        )
        kind, delta = entry["kind"], entry["delta"]
        row["balance"] = entry["balance"]
        if kind in ("grant", "allowance"):
            row["grants" if kind == "grant" else "allowance"] += delta
        elif kind == "command":
            row["commands"] += 1
            row["spent"] -= delta
            kept[entry.get("request_id")] = entry["account"]
        elif kind == "gadget_day":
            row["gadget_days"] += 1
            row["spent"] -= delta
        elif kind == "refund":
            row["refunds"] += 1
            row["commands"] -= 1
            row["spent"] -= delta
            refunded.add(requests.get(entry.get("refund_of")))
    for request_id in refunded:
        kept.pop(request_id, None)
    logged = {
        call.get("request_id")
        for call in calls
        if call.get("tool") == "gadgets_command"
        and (call.get("credits") or 0) > 0
        and inside(call.get("ts", ""))
    }
    ledger_only = sorted(r for r in kept if r not in logged)
    log_only = sorted(r for r in logged if r not in kept)
    for account, row in rows.items():
        row["operator"] = account == operator
    return {
        "window": {"from": since, "to": until},
        "accounts": dict(sorted(rows.items())),
        "total_spent": sum(row["spent"] for row in rows.values()),
        "reconciliation": {
            "commands_in_ledger": len(kept),
            "commands_in_log": len(logged),
            "only_in_ledger": ledger_only,
            "only_in_log": log_only,
            "matches": not ledger_only and not log_only,
        },
    }


def format_report(value):
    window = value["window"]
    lines = [f"Credits, {window['from'] or 'start'} to {window['to'] or 'now'} (UTC):", ""]
    if not value["accounts"]:
        lines.append("No credits activity in this window.")
    for account, row in value["accounts"].items():
        lines.append(
            f"{account}  spent {row['spent']} (commands {row['commands']}, gadget-days "
            f"{row['gadget_days']}, refunds {row['refunds']})  granted {row['grants']}  "
            f"allowance {row['allowance']}  balance {row['balance']}"
        )
    check = value["reconciliation"]
    lines.append("")
    lines.append(f"Total credits spent: {value['total_spent']}")
    if check["matches"]:
        lines.append(
            f"Reconciled: {check['commands_in_ledger']} charged commands match the request log."
        )
    else:
        lines.append(
            f"Not reconciled: {len(check['only_in_ledger'])} charged commands have no request "
            f"record (a rotated log?), {len(check['only_in_log'])} request records have no "
            "ledger entry."
        )
    lines.append("Credits are a usage measure on this relay. They are not money.")
    return "\n".join(lines)
