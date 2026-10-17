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
from .protocol import GatewayError

# Codes use the sign-in alphabet: no 0/O or 1/I, so a code read off a display is typed right.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CLAIM_SECONDS = 600
SHARE_SECONDS = 600  # share and link codes (§6)
SCOPES = ("use", "view")
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
        self.share_codes = {}  # digest -> {"device", "scope", "owner", "expires"}
        self.link_codes = {}  # digest -> {"account", "expires"}
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

    def _record(self, device_id):
        return self.data["devices"].get(device_id)

    def role(self, account, device_id):
        """`owner`, `use`, `view` or None: what this account may do with the gadget."""
        record = self._record(device_id)
        if record is None or account is None:
            return None
        if record["owner"] == account:
            return "owner"
        for grant in record.get("grants", {}).values():
            if grant["account"] == account:
                return grant["scope"]
        return None

    def visible(self, account):
        """{device: role} for every gadget this account owns or holds a grant on."""
        found = {}
        for device_id in self.data["devices"]:
            role = self.role(account, device_id)
            if role is not None:
                found[device_id] = role
        return found

    def assign(self, device_id, account, *, via):
        if account not in self.data["accounts"]:
            raise OperatorError(f"no account {account}")
        self.data["devices"][device_id] = {
            "owner": account,
            "claimed_at": self.clock(),
            "grants": {},
        }
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

    def sign_in_code(self, text, *, caller):
        """Sign-in page: a claim or share code makes a new account; a link code opens its own.

        Returns (account, recovery code or None). A code never opens another existing account.
        """
        self._check_open(caller)
        code = normal_code(text)
        if code is not None and len(code) == 9:
            device_id = self._live_device(code)
            if device_id is not None:
                account, recovery = self._new_account()
                self.assign(device_id, account, via="sign_in")
                return account, recovery
            share = self._take(self.share_codes, code)
            if share is not None:
                account, recovery = self._new_account()
                self._grant(share, account)
                return account, recovery
            link = self._take(self.link_codes, code)
            if link is not None and link["account"] in self.data["accounts"]:
                self._write("account_linked", account=link["account"])
                return link["account"], None
        if code is not None and len(code) == 19:
            return self.recover(code, caller=caller)
        self._failed("wrong_code", caller)

    def _take(self, table, code):
        """Remove and return a live share or link code entry; None if unknown or expired."""
        entry = table.pop(_digest(code), None)
        if entry is None or entry["expires"] <= self.clock():
            return None
        return entry

    # --- tools (§6–8) ---

    def claim(self, text, *, account):
        """gadgets_claim: a claim code adds the gadget; a share code adds a grant."""
        self._check_open(None, account)
        code = normal_code(text)
        if code is not None and len(code) == 9:
            device_id = self._live_device(code)
            if device_id is not None:
                self.assign(device_id, account, via="tool")
                return {"device_id": device_id, "role": "owner"}
            share = self._take(self.share_codes, code)
            if share is not None and share["owner"] != account:
                return {"device_id": share["device"], "role": self._grant(share, account)}
        self._failed("wrong_code", None, account)

    def _grant(self, share, account):
        record = self._record(share["device"])
        if record is None or record["owner"] != share["owner"]:
            self._failed("stale_share", None, account)  # The gadget changed hands since.
        grants = record.setdefault("grants", {})
        for share_id, grant in list(grants.items()):
            if grant["account"] == account:
                del grants[share_id]  # A new share replaces an older one; never two.
        share_id = "share_" + secrets.token_hex(6)
        grants[share_id] = {"account": account, "scope": share["scope"], "created": self.clock()}
        self._save()
        self._write(
            "share_used",
            device=share["device"],
            account=account,
            share_id=share_id,
            scope=share["scope"],
        )
        return share["scope"]

    def _owned(self, account, device_id):
        role = self.role(account, device_id)
        if role is None:
            raise GatewayError("unknown_device", "Device not registered")
        if role != "owner":
            raise GatewayError("not_owner", "Only the gadget's owner can do this")
        return self._record(device_id)

    def share_create(self, account, device_id, scope="use"):
        if scope not in SCOPES:
            raise GatewayError("invalid_scope", "scope is use or view")
        self._owned(account, device_id)
        code = _random_code(2)
        self.share_codes[_digest(code)] = {
            "device": device_id,
            "scope": scope,
            "owner": account,
            "expires": self.clock() + SHARE_SECONDS,
        }
        self._write("share_created", device=device_id, account=account, scope=scope)
        return {"share_code": code, "scope": scope, "expires_in": SHARE_SECONDS}

    def shares(self, account, device_id):
        record = self._owned(account, device_id)
        return [
            {"share_id": share_id, "scope": grant["scope"], "created": grant["created"]}
            for share_id, grant in sorted(record.get("grants", {}).items())
        ]

    def share_revoke(self, account, device_id, share_id):
        record = self._owned(account, device_id)
        if record.get("grants", {}).pop(str(share_id), None) is None:
            raise GatewayError("unknown_share", "No such share on this gadget")
        self._save()
        self._write("share_revoked", device=device_id, account=account, share_id=share_id)

    def link_code(self, account):
        """A 10-minute code that signs a second app in to this account (§6)."""
        code = _random_code(2)
        self.link_codes[_digest(code)] = {
            "account": account,
            "expires": self.clock() + SHARE_SECONDS,
        }
        self._write("link_created", account=account)
        return {"link_code": code, "expires_in": SHARE_SECONDS}

    def remove(self, account, device_id):
        """gadgets_remove: the owner unclaims (grants end too); a grantee drops the grant."""
        role = self.role(account, device_id)
        if role is None:
            raise GatewayError("unknown_device", "Device not registered")
        record = self._record(device_id)
        if role == "owner":
            del self.data["devices"][device_id]
            self._save()
            self._write("device_unclaimed", device=device_id, account=account, via="tool")
            return "unclaimed"
        for share_id, grant in list(record.get("grants", {}).items()):
            if grant["account"] == account:
                del record["grants"][share_id]
                self._write("share_dropped", device=device_id, account=account, share_id=share_id)
        self._save()
        return "dropped"

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
