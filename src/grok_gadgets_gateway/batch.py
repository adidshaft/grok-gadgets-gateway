"""Batches (GW-BUILD-001, hub ADR 013 §2): enroll and label many gadgets at once.

An ESP32 gadget's ID comes from its chip, so the builder lists the IDs read from the boards
(`factory-provision PORT --read-id`). Linux gadgets choose their IDs, so a count and a prefix
can make them. Each device gets a token, written once to a mode-0600 file in the batch folder,
and (unless `labels=False`) a setup label in the label store with copies in the folder.
`batch.csv` lists the devices and their files, never a secret.
"""

import csv
import io
import os
import re
from pathlib import Path

from .label import label_record, label_svg, save_record, write_label_files
from .operator import OperatorError, atomic_write, credentials_path, enroll, new_device_token
from .transport import read_registry

DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
MAX_BATCH = 500
FIELDS = (
    "device_id",
    "hotspot",
    "token_file",
    "label_svg",
    "label_png",
    "label_text",
    "provisioned",
)


def read_ids(path):
    """Device IDs from a CSV with a `device_id` column; blank lines are skipped."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise OperatorError(f"cannot read {path}: {exc.strerror}") from None
    reader = csv.DictReader(io.StringIO(text))
    if "device_id" not in (reader.fieldnames or []):
        raise OperatorError(f"{path} needs a device_id column")
    ids = [(row.get("device_id") or "").strip() for row in reader]
    ids = [i for i in ids if i]
    return _checked(ids)


def count_ids(count, prefix):
    if not 1 <= count <= 99:
        raise OperatorError("--count must be 1 to 99")
    return _checked([f"{prefix}{n:02d}" for n in range(1, count + 1)])


def _checked(ids):
    if not ids:
        raise OperatorError("the batch has no device IDs")
    if len(ids) > MAX_BATCH:
        raise OperatorError(f"a batch holds at most {MAX_BATCH} gadgets")
    for device_id in ids:
        if not DEVICE_ID.fullmatch(device_id):
            raise OperatorError(f"not a valid device ID: {device_id!r}")
    if len(set(ids)) != len(ids):
        raise OperatorError("the batch lists a device ID twice")
    return ids


def _private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_rows(out, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(Path(out) / "batch.csv", buffer.getvalue().encode())


def make_batch(ids, out, *, credentials=None, labels=True, label_dir=None):
    """Enroll every ID, write its token file and label; return the batch.csv rows."""
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise OperatorError(f"{out} is not empty; use a new folder for each batch")
    path = Path(credentials) if credentials else credentials_path()
    enrolled = read_registry(path)
    taken = [device_id for device_id in ids if device_id in enrolled]
    if taken:
        raise OperatorError(
            f"already enrolled: {', '.join(taken)}. Nothing was changed; remove them from the "
            "list, or use enroll ID --rotate for each"
        )
    _private_dir(out)
    tokens = _private_dir(out / "tokens")
    label_out = _private_dir(out / "labels") if labels else None
    rows, sheets = [], []
    for device_id in ids:
        token = new_device_token()
        token_file = tokens / f"{device_id}.token"
        atomic_write(token_file, (token + "\n").encode())
        enroll(device_id, path, token=token)
        row = dict.fromkeys(FIELDS, "")
        row.update(device_id=device_id, token_file=str(token_file.relative_to(out)))
        row["provisioned"] = "no"
        if labels:
            record = label_record(device_id, label_dir, new=True)
            save_record(record, label_dir)
            files = write_label_files(record, label_out)
            row.update(
                hotspot=record["hotspot"],
                label_svg=str(files["svg"].relative_to(out)),
                label_png=str(files["png"].relative_to(out)),
                label_text=str(files["text"].relative_to(out)),
            )
            sheets.append(label_svg(record))
        rows.append(row)
        _write_rows(out, rows)  # After each device, so a failure leaves an honest list.
    if labels:
        page = (
            "<!doctype html><meta charset=utf-8><title>Setup labels</title>"
            "<style>body{margin:8mm}svg{margin:2mm;page-break-inside:avoid}</style>"
            "<p>Setup labels: keep them private, one per gadget.</p>" + "".join(sheets)
        )
        atomic_write(label_out / "print.html", page.encode())
    return rows


def read_rows(out):
    try:
        with open(Path(out) / "batch.csv", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        raise OperatorError(f"{out} has no batch.csv; make one with enroll --batch") from None


def batch_token(out, device_id):
    """The batch row and token file for a board, or OperatorError."""
    for row in read_rows(out):
        if row["device_id"] == device_id:
            if row["provisioned"] == "yes":
                raise OperatorError(f"{device_id} is already provisioned from this batch")
            token_file = Path(out) / row["token_file"]
            if not token_file.is_file():
                raise OperatorError(f"the token file for {device_id} is missing")
            return row, token_file
    raise OperatorError(f"{device_id} is not in this batch; nothing sent")


def mark_provisioned(out, device_id):
    """Record a confirmed board and delete its token file: it was shown only to the board."""
    rows = read_rows(out)
    for row in rows:
        if row["device_id"] == device_id:
            row["provisioned"] = "yes"
            (Path(out) / row["token_file"]).unlink(missing_ok=True)
    _write_rows(out, rows)
