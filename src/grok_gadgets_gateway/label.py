"""Setup labels for factory-provisioned gadgets (hub ADR 010).

A label carries what a person needs to join a gadget to home Wi-Fi with a phone: the setup
hotspot's name and per-device WPA2 passphrase (also as a `WIFI:` QR code), the setup code the
captive page asks for, the page address and the device ID. The label is a secret: anyone who
holds it and the gadget can set the gadget up again after a reset.

The builder keeps one record per device, mode 0600, so a lost label can be reprinted without
changing the gadget.
"""

import json
import re
import secrets
from html import escape
from io import BytesIO
from pathlib import Path

import segno

from .operator import OperatorError, atomic_write, config_dir

# The firmware's setup alphabet: no 0/O or 1/I look-alikes (GrokWifi.h SetupAlphabet).
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8  # 40 bits
PASSPHRASE_LENGTH = 16  # 80 bits
HOTSPOT_PREFIX = "GrokGadget-"
PAGE = "http://192.168.4.1"


def labels_dir():
    return config_dir() / "labels"


def hotspot_name(device_id):
    """`GrokGadget-` and the device ID's last four characters, upper case, as the firmware."""
    return HOTSPOT_PREFIX + device_id[-4:].upper()


def new_secret(length):
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def check_secret(value, length, what):
    if len(value) != length or any(c not in ALPHABET for c in value):
        raise OperatorError(f"The stored {what} is not valid; reprint with --new-label")


def grouped(code):
    """`ABCD2345` -> `ABCD-2345`, as printed. The page accepts either."""
    return f"{code[:4]}-{code[4:]}"


def _wifi_escape(text):
    return re.sub(r'([\\;,:"])', r"\\\1", text)


def wifi_qr_text(ssid, passphrase):
    """The `WIFI:` payload that the iPhone and Android cameras join from."""
    return f"WIFI:T:WPA;S:{_wifi_escape(ssid)};P:{_wifi_escape(passphrase)};;"


def record_path(device_id, directory=None):
    return Path(directory or labels_dir()) / f"{device_id}.json"


def load_record(device_id, directory=None):
    path = record_path(device_id, directory)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        raise OperatorError(f"cannot read the label record {path}") from None
    check_secret(record.get("passphrase", ""), PASSPHRASE_LENGTH, "passphrase")
    check_secret(record.get("setup_code", ""), CODE_LENGTH, "setup code")
    return record


def label_record(device_id, directory=None, *, new=False):
    """The device's label values: the stored record, or new random values (saved, 0600)."""
    record = None if new else load_record(device_id, directory)
    if record is None:
        record = {
            "device_id": device_id,
            "hotspot": hotspot_name(device_id),
            "passphrase": new_secret(PASSPHRASE_LENGTH),
            "setup_code": new_secret(CODE_LENGTH),
        }
    return record


def save_record(record, directory=None):
    path = record_path(record["device_id"], directory)
    atomic_write(path, (json.dumps(record, indent=2) + "\n").encode())
    return path


def label_text(record):
    """The label as plain text, with the Wi-Fi QR code drawn in characters."""
    qr = segno.make(wifi_qr_text(record["hotspot"], record["passphrase"]), error="m")
    lines = [
        "Grok Gadgets setup label (keep it private)",
        "",
        f"Wi-Fi:       {record['hotspot']}",
        f"Passphrase:  {record['passphrase']}",
        f"Setup code:  {grouped(record['setup_code'])}",
        f"Setup page:  {PAGE}",
        f"Device:      {record['device_id']}",
        "",
        "1. Scan the QR code with the phone camera and join the Wi-Fi.",
        "2. Enter the setup code, pick your home Wi-Fi and type its password.",
        "",
        _qr_blocks(qr),
    ]
    return "\n".join(lines) + "\n"


def _qr_blocks(qr):
    """Two QR rows per text line with half blocks, dark modules drawn, a 2-module margin."""
    matrix = [[bool(v) for v in row] for row in qr.matrix_iter(scale=1, border=2)]
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))
    chars = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    return "\n".join(
        "".join(chars[top, bottom] for top, bottom in zip(matrix[y], matrix[y + 1], strict=True))
        for y in range(0, len(matrix), 2)
    )


def label_svg(record):
    """A printable label: the QR code beside the text, 90 x 50 mm."""
    qr = segno.make(wifi_qr_text(record["hotspot"], record["passphrase"]), error="m")
    size = qr.symbol_size(scale=1, border=2)[0]
    modules = "".join(
        f'<rect x="{x}" y="{y}" width="1" height="1"/>'
        for y, row in enumerate(qr.matrix_iter(scale=1, border=2))
        for x, dark in enumerate(row)
        if dark
    )
    rows = [
        ("Wi-Fi", record["hotspot"]),
        ("Passphrase", record["passphrase"]),
        ("Setup code", grouped(record["setup_code"])),
        ("Setup page", PAGE),
        ("Device", record["device_id"]),
    ]
    text = "".join(
        f'<text x="44" y="{11 + 7 * i}" font-size="3.2"><tspan font-weight="700">'
        f"{escape(name)}:</tspan> {escape(value)}</text>"
        for i, (name, value) in enumerate(rows)
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="90mm" height="50mm" viewBox="0 0 90 50" '
        'font-family="Helvetica, Arial, sans-serif">'
        '<rect width="90" height="50" fill="#fff" stroke="#000" stroke-width="0.3"/>'
        f'<svg x="3" y="5" width="38" height="38" viewBox="0 0 {size} {size}">{modules}</svg>'
        f"{text}"
        '<text x="44" y="47" font-size="2.6">Scan to join, then enter the setup code.</text>'
        "</svg>\n"
    )


def write_label_files(record, directory):
    """Write the text label, the Wi-Fi QR PNG and the printable SVG, mode 0600."""
    directory = Path(directory)
    base = directory / record["device_id"]
    qr = segno.make(wifi_qr_text(record["hotspot"], record["passphrase"]), error="m")
    png = base.with_name(base.name + "-wifi-qr.png")
    buffer = BytesIO()
    qr.save(buffer, kind="png", scale=10, border=2)
    paths = {
        "text": base.with_name(base.name + "-label.txt"),
        "png": png,
        "svg": base.with_name(base.name + "-label.svg"),
    }
    atomic_write(paths["text"], label_text(record).encode())
    atomic_write(paths["png"], buffer.getvalue())
    atomic_write(paths["svg"], label_svg(record).encode())
    return paths
