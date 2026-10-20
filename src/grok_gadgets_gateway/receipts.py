"""Signed, hash-chained credits receipts (GW-CREDITS-002, hub ADR 015 Stage 0).

Once a day, and when the operator asks, the relay closes each account's ledger entries since
its last receipt into one receipt: counts, credits in and out, the opening and closing
balance, a sequence number and the SHA-256 hash of the previous receipt. An Ed25519 key in the
relay's configuration directory signs it; the key never lives in a repository. Each account
has its own file, `oauth/receipts/ACCOUNT.jsonl`. A receipt holds the account ID and counts:
never a name, token, argument, address or code.
"""

import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .operator import OperatorError, atomic_write, config_dir

VERSION = 1
SPENT_KINDS = ("command", "gadget_day")


def key_path():
    return config_dir() / "credits-receipt-key.pem"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(receipt):
    return hashlib.sha256(canonical(receipt)).hexdigest()


def key_id(public):
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def public_text(public):
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode() + "\n"


def read_public(path):
    try:
        raw = base64.b64decode(Path(path).read_text().strip(), validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except FileNotFoundError:
        raise OperatorError(f"no receipt public key at {path}") from None
    except (OSError, ValueError):
        raise OperatorError(f"{path} is not a receipt public key") from None


def load_key(path=None, create=True):
    """The relay's receipt signing key; made on first use, mode 0600."""
    path = Path(path or key_path())
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        if not create:
            raise OperatorError(f"no receipt key at {path}") from None
        key = Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        atomic_write(path, pem)
        os.chmod(path, 0o600)
        return key
    try:
        key = serialization.load_pem_private_key(data, None)
    except ValueError:
        raise OperatorError(f"{path} is not a receipt key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise OperatorError(f"{path} is not an Ed25519 key")
    return key


def read_receipts(path):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise OperatorError(f"cannot read {path}: {exc.strerror}") from None
    receipts = []
    for number, line in enumerate(lines, 1):
        try:
            receipts.append(json.loads(line))
        except ValueError:
            raise OperatorError(f"{path} line {number} is not JSON") from None
    return receipts


def summarize(account, entries, opening):
    """Counts and credits for one account's ledger entries, in ledger order."""
    refunded = sum(e["delta"] for e in entries if e["kind"] == "refund")
    spent = -sum(e["delta"] for e in entries if e["kind"] in SPENT_KINDS) - refunded
    grants = sum(e["delta"] for e in entries if e["kind"] == "grant")
    allowance = sum(e["delta"] for e in entries if e["kind"] == "allowance")
    return {
        "account": account,
        "commands": sum(e["kind"] == "command" for e in entries)
        - sum(e["kind"] == "refund" for e in entries),
        "refunds": sum(e["kind"] == "refund" for e in entries),
        "gadget_days": sum(e["kind"] == "gadget_day" for e in entries),
        "spent": spent,
        "grants": grants,
        "allowance": allowance,
        "opening": opening,
        "closing": opening + grants + allowance - spent,
    }


class Receipts:
    """Closes ledger entries into signed receipts for one relay."""

    def __init__(self, directory, key=None, *, log=None):
        self.directory = Path(directory) / "receipts"
        self.public_path = Path(directory) / "credits-receipt-key.pub"
        self.key = key
        self.log = log

    def open(self):
        if self.key is None:
            self.key = load_key()
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        atomic_write(self.public_path, public_text(self.key.public_key()).encode())
        return self

    def path(self, account):
        return self.directory / f"{account}.jsonl"

    def close(self, entries, cut, now, inclusive=False):
        """Write a receipt for each account with entries since its last receipt and before
        `cut` (an ISO time; at or before it when `inclusive`). Returns the receipts written."""
        written = []
        by_account = {}
        for entry in entries:
            by_account.setdefault(entry["account"], []).append(entry)
        for account in sorted(by_account):
            previous = read_receipts(self.path(account))
            last = previous[-1] if previous else None
            after = last["ledger"]["last"] if last else 0
            batch = [
                e
                for e in by_account[account]
                if e["seq"] > after and (e["ts"] < cut or (inclusive and e["ts"] <= cut))
            ]
            if not batch:
                continue
            receipt = {
                "v": VERSION,
                **summarize(account, batch, last["closing"] if last else 0),
                "seq": (last["seq"] + 1) if last else 1,
                "period": {"from": last["period"]["to"] if last else batch[0]["ts"], "to": cut},
                "ledger": {"first": batch[0]["seq"], "last": batch[-1]["seq"], "count": len(batch)},
                "issued": now,
                "prev": digest(last) if last else None,
                "key": key_id(self.key.public_key()),
            }
            receipt["sig"] = base64.b64encode(self.key.sign(canonical(receipt))).decode()
            with open(self.path(account), "a", encoding="utf-8") as file:
                file.write(json.dumps(receipt, separators=(",", ":")) + "\n")
                file.flush()
                os.fsync(file.fileno())
            written.append(receipt)
            if self.log is not None:
                self.log.append(
                    {"event": "credits_receipt", "account": account, "seq": receipt["seq"]}
                )
        return written


def verify(receipts, public, entries=None):
    """Problems with one account's receipt chain; an empty list means it verifies.

    Checks each signature, the sequence, the hash chain and the balances. With the ledger's
    entries for that account, it also recomputes every receipt from them.
    """
    problems = []
    wanted = key_id(public)
    previous = None
    for index, receipt in enumerate(receipts, 1):
        where = f"receipt {index}"
        if not isinstance(receipt, dict):
            problems.append(f"{where}: not a receipt")
            break
        body = {k: v for k, v in receipt.items() if k != "sig"}
        try:
            public.verify(base64.b64decode(receipt.get("sig", ""), validate=True), canonical(body))
        except (InvalidSignature, ValueError):
            problems.append(f"{where}: signature does not match")
        if receipt.get("key") != wanted:
            problems.append(f"{where}: signed by another key")
        if receipt.get("seq") != index:
            problems.append(f"{where}: sequence {receipt.get('seq')} (missing or reordered)")
        if receipt.get("prev") != (digest(previous) if previous else None):
            problems.append(f"{where}: previous hash does not match (missing or changed)")
        opening = previous["closing"] if previous else 0
        try:
            net = receipt["grants"] + receipt["allowance"] - receipt["spent"]
            if receipt["opening"] != opening or receipt["closing"] != opening + net:
                problems.append(f"{where}: balances do not add up")
        except (KeyError, TypeError):
            problems.append(f"{where}: fields missing")
        if entries is not None:
            ledger = receipt.get("ledger") or {}
            batch = [
                e for e in entries if ledger.get("first", 0) <= e["seq"] <= ledger.get("last", -1)
            ]
            if len(batch) != ledger.get("count") or (
                summarize(receipt.get("account"), batch, opening)
                != {k: receipt.get(k) for k in summarize(None, [], 0)}
            ):
                problems.append(f"{where}: does not match the ledger")
        previous = receipt
    if entries is not None and receipts:
        tail = [e for e in entries if e["seq"] > receipts[-1].get("ledger", {}).get("last", 0)]
        covered = sum((r.get("ledger") or {}).get("count", 0) for r in receipts)
        if covered + len(tail) != len(entries):
            problems.append("ledger entries are missing from the receipts")
    return problems


def utc_now():
    return datetime.now(UTC).isoformat()
