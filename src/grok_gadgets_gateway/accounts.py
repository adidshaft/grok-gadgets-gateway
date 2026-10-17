"""Accounts mode (hub ADR 011): relay-minted accounts and the claim codes gadgets show.

`serve --oauth --accounts` owns `oauth/accounts.json`. An account is created only when a
sign-in is approved with a claim code from an unclaimed gadget. The refresh-token family
carries the account (see oauth.py); a `client_id` never does.

Claim codes live in memory only, as SHA-256 hashes: one per connected, unclaimed gadget that
asked for claim fields in its hello, for 10 minutes. Recovery codes are stored as hashes.
Neither ever reaches a file or a log line.
"""

import base64
import hashlib
import json
import secrets
import time
from collections import deque
from pathlib import Path

from .operator import OperatorError, atomic_write

# Codes use the sign-in alphabet: no 0/O or 1/I, so a code read off a display is typed right.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CLAIM_SECONDS = 600
HOUR = 3600
# Wrong codes (hub ADR 011 §5).
REQUEST_WRONG_MAX = 5
CALLER_WRONG_PER_HOUR = 20
ACCOUNT_WRONG_PER_HOUR = 10
RELAY_WRONG_PER_HOUR = 200
CLOSED_SECONDS = 600
FLOOD_ALERT_SECONDS = 60
CLAIM_MODES = ("ready", "after_setup")
WRONG = "That code did not work."


class ClaimRejected(Exception):
    """A code was refused. The message never says whether a gadget or account exists."""

    def __init__(self, reason, limited=False):
        super().__init__("Too many tries; wait and try again." if limited else WRONG)
        self.reason = reason
        self.limited = limited


def _digest(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _random_code(groups):
    raw = "".join(secrets.choice(ALPHABET) for _ in range(4 * groups))
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def new_account_id():
    """`acct_` and 26 base32 characters: 128 random bits."""
    return "acct_" + base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=").lower()


def normal_code(text):
    """`ABCD-EFGH` (claim, share, link) or `ABCD-EFGH-JKLM-NPQR` (recovery); None if neither."""
    raw = "".join(str(text or "").split()).replace("-", "").upper()
    if len(raw) not in (8, 16) or any(c not in ALPHABET for c in raw):
        return None
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


class Accounts:
    def __init__(self, path, *, log=None, clock=time.time):
        self.path = Path(path)
        self.log = log
        self.clock = clock
        self.data = None
        self.sessions = {}  # device -> {"mode", "code" (digest), "expires", "sent"}
        self.codes = {}  # claim code digest -> device
        self.wrong = {}  # caller or account -> deque of wrong-code times
        self.relay_wrong = deque()
        self.closed_until = 0.0
        self.alerted_at = None

    # --- storage ---

    def open(self, enrolled_devices):
        """Load the store; on the first start make the operator account (migration, §11)."""
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            operator = new_account_id()
            self.data = {
                "operator": operator,
                "accounts": {operator: {"created": self.clock(), "recovery_sha256": None}},
                "devices": {},
            }
            for device_id in sorted(enrolled_devices):
                self.data["devices"][device_id] = {"owner": operator, "claimed_at": self.clock()}
            self._save()
            self._write("accounts_migrated", operator=operator, devices=len(self.data["devices"]))
        except (OSError, ValueError):
            raise OperatorError(f"{self.path} is damaged") from None
        return self

    def _save(self):
        atomic_write(self.path, (json.dumps(self.data, indent=1, sort_keys=True) + "\n").encode())

    def _write(self, event, **fields):
        if self.log is not None:
            self.log.write({"event": event, **fields})

    @property
    def operator(self):
        return self.data["operator"]

    def owner(self, device_id):
        record = self.data["devices"].get(device_id)
        return record["owner"] if record else None

    def assign(self, device_id, account, *, via):
        if account not in self.data["accounts"]:
            raise OperatorError(f"no account {account}")
        self.data["devices"][device_id] = {"owner": account, "claimed_at": self.clock()}
        self._save()
        self._end_code(device_id)
        self._write("device_claimed", device=device_id, account=account, via=via)

    def _new_account(self):
        account = new_account_id()
        recovery = _random_code(4)
        self.data["accounts"][account] = {
            "created": self.clock(),
            "recovery_sha256": _digest(recovery),
        }
        self._write("account_created", account=account)
        return account, recovery

    # --- gadget sessions: the claim fields of hello and poll replies (§3) ---

    def hello(self, device_id, mode, session_id=None):
        """Fields to add to the hello reply, or None for a gadget that did not opt in."""
        self._end_code(device_id)
        if mode is None:
            self.sessions.pop(device_id, None)
            return None
        if mode not in CLAIM_MODES:
            raise ValueError(mode)
        self.sessions[device_id] = {
            "mode": mode,
            "session": session_id,
            "code": None,
            "expires": 0.0,
            "sent": None,
        }
        return self._fields(device_id, force=True)

    def poll(self, device_id):
        """Fields for a poll reply when the claim state or code changed; otherwise None."""
        if device_id not in self.sessions:
            return None
        return self._fields(device_id)

    def disconnect(self, device_id, session_id=None):
        """End the gadget's code, unless a newer session of the same gadget replaced it."""
        session = self.sessions.get(device_id)
        if session is None or session_id not in (None, session["session"]):
            return
        self._end_code(device_id)
        self.sessions.pop(device_id, None)

    def _fields(self, device_id, force=False):
        session = self.sessions[device_id]
        code = None
        claimed = self.owner(device_id) is not None
        if claimed:
            self._end_code(device_id)
        elif session["code"] is None or session["expires"] <= self.clock():
            self._end_code(device_id)
            code = _random_code(2)
            session.update(code=_digest(code), expires=self.clock() + CLAIM_SECONDS)
            self.codes[session["code"]] = device_id
        if not force and code is None and session["sent"] == claimed:
            return None
        session["sent"] = claimed
        return {"claimed": claimed, **({"claim_code": code} if code else {})}

    def _end_code(self, device_id):
        session = self.sessions.get(device_id)
        if session and session["code"]:
            self.codes.pop(session["code"], None)
            session["code"] = None

    def _live_device(self, code):
        device_id = self.codes.get(_digest(code))
        session = self.sessions.get(device_id) if device_id else None
        if session is None or session["expires"] <= self.clock():
            return None
        if self.owner(device_id) is not None:
            return None
        return device_id

    # --- codes typed by a person: limits (§5) ---

    def _window(self, key):
        window = self.wrong.setdefault(key, deque())
        while window and self.clock() - window[0] >= HOUR:
            window.popleft()
        return window

    def _check_open(self, caller, account=None):
        now = self.clock()
        while self.relay_wrong and now - self.relay_wrong[0] >= HOUR:
            self.relay_wrong.popleft()
        if now < self.closed_until:
            self._flood()
            raise ClaimRejected("closed", limited=True)
        if caller is not None and len(self._window(("caller", caller))) >= CALLER_WRONG_PER_HOUR:
            raise ClaimRejected("caller_limit", limited=True)
        if account is not None and len(self._window(("account", account))) >= (
            ACCOUNT_WRONG_PER_HOUR
        ):
            raise ClaimRejected("account_limit", limited=True)

    def _failed(self, reason, caller, account=None):
        now = self.clock()
        if caller is not None:
            self._window(("caller", caller)).append(now)
        if account is not None:
            self._window(("account", account)).append(now)
        self.relay_wrong.append(now)
        if len(self.wrong) > 4096:  # Bounded bookkeeping: forget quiet callers.
            self.wrong = {k: w for k, w in self.wrong.items() if w and now - w[-1] < HOUR}
        self._write("claim_rejected", reason=reason, caller=caller)
        if len(self.relay_wrong) >= RELAY_WRONG_PER_HOUR:
            self.closed_until = now + CLOSED_SECONDS
            self.relay_wrong.clear()
            self._flood()
        raise ClaimRejected(reason)

    def _flood(self):
        now = self.clock()
        if self.alerted_at is None or now - self.alerted_at >= FLOOD_ALERT_SECONDS:
            self.alerted_at = now
            self._write("claim_flood", reason="wrong_codes")

    # --- sign-in page (§4) ---

    def claim_new_account(self, text, *, caller):
        """A claim code makes a new account that owns the gadget: (account, recovery code)."""
        self._check_open(caller)
        code = normal_code(text)
        device_id = self._live_device(code) if code and len(code) == 9 else None
        if device_id is None:
            self._failed("wrong_code", caller)
        account, recovery = self._new_account()
        self.assign(device_id, account, via="sign_in")
        return account, recovery

    def recover(self, text, *, caller):
        """A recovery code opens its account once and is replaced: (account, new code)."""
        self._check_open(caller)
        code = normal_code(text)
        digest = _digest(code) if code and len(code) == 19 else None
        for account, record in self.data["accounts"].items():
            if digest and secrets.compare_digest(record.get("recovery_sha256") or "", digest):
                replacement = _random_code(4)
                record["recovery_sha256"] = _digest(replacement)
                self._save()
                self._write("account_recovered", account=account)
                return account, replacement
        self._failed("wrong_recovery_code", caller)
