"""Batches (GW-BUILD-001, hub ADR 013 §2): many gadgets enrolled and labelled at once."""

import csv
import json
import stat

import pytest
from test_factory_provision import NAME
from test_lan import FakeSerialPort

from grok_gadgets_gateway import batch, label, relay_check
from grok_gadgets_gateway.cli import main
from grok_gadgets_gateway.operator import OperatorError, enroll
from grok_gadgets_gateway.transport import digest

KIT = "oledkit-0123456789ab"


def registry(tmp_path):
    return json.loads((tmp_path / "grok-gadgets" / "credentials.json").read_text())["devices"]


def rows(out):
    with open(out / "batch.csv", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert main(["init", "--client", "http"]) == 0
    capsys.readouterr()
    return tmp_path


def test_ids_from_a_csv_or_a_count(tmp_path):
    ids = tmp_path / "ids.csv"
    ids.write_text("device_id,note\noledkit-0123456789ab,desk\n\noledkit-ffffffffffff,\n")
    assert batch.read_ids(ids) == ["oledkit-0123456789ab", "oledkit-ffffffffffff"]
    assert batch.count_ids(3, "lamp-") == ["lamp-01", "lamp-02", "lamp-03"]
    for bad in ("id\nx\n", "device_id\nbad id\n", "device_id\na\na\n", "device_id\n"):
        ids.write_text(bad)
        with pytest.raises(OperatorError):
            batch.read_ids(ids)
    with pytest.raises(OperatorError):
        batch.count_ids(0, "lamp-")
    with pytest.raises(OperatorError):
        batch.count_ids(1000, "lamp-")


def test_a_batch_enrolls_writes_token_files_and_labels(config):
    out = config / "batch-1"
    made = batch.make_batch(["lamp-01", KIT], out)
    assert [r["device_id"] for r in made] == ["lamp-01", KIT]
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    devices = registry(config)
    for row in rows(out):
        token_file = out / row["token_file"]
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        token = token_file.read_text().strip()
        assert devices[row["device_id"]]["sha256"] == digest(token)
        assert row["provisioned"] == "no"
        # The CSV holds no secret: no token, passphrase or setup code.
        record = label.load_record(row["device_id"])
        csv_text = (out / "batch.csv").read_text()
        for secret in (token, record["passphrase"], record["setup_code"]):
            assert secret not in csv_text
        assert (out / row["label_svg"]).exists() and (out / row["label_png"]).exists()
    assert rows(out)[1]["hotspot"] == "GrokGadget-89AB"
    sheet = (out / "labels" / "print.html").read_text()
    assert sheet.count("<svg") >= 2 and stat.S_IMODE((out / "labels").stat().st_mode) == 0o700


def test_a_batch_without_labels_for_linux_gadgets(config):
    out = config / "linux"
    batch.make_batch(batch.count_ids(2, "lamp-"), out, labels=False)
    assert [r["label_svg"] for r in rows(out)] == ["", ""]
    assert not (out / "labels").exists() and label.load_record("lamp-01") is None


def test_a_batch_never_touches_enrolled_devices_or_a_used_folder(config):
    enroll(KIT)
    before = registry(config)
    with pytest.raises(OperatorError, match="already enrolled"):
        batch.make_batch(["lamp-01", KIT], config / "b")
    assert registry(config) == before and not (config / "b").exists()
    (config / "used").mkdir()
    (config / "used" / "x").write_text("x")
    with pytest.raises(OperatorError, match="not empty"):
        batch.make_batch(["lamp-02"], config / "used")


def test_enroll_batch_cli_shows_no_token(config, capsys):
    ids = config / "ids.csv"
    ids.write_text(f"device_id\n{KIT}\n")
    assert main(["enroll", "--batch", str(ids), "--out", str(config / "b1")]) == 0
    out, err = capsys.readouterr()
    token = (config / "b1" / "tokens" / f"{KIT}.token").read_text().strip()
    assert token not in out + err and "1 gadget" in err and "keep" in err.lower()
    assert (
        main(
            [
                "enroll",
                "--count",
                "2",
                "--prefix",
                "lamp-",
                "--out",
                str(config / "b2"),
                "--no-labels",
            ]
        )
        == 0
    )
    assert [r["device_id"] for r in rows(config / "b2")] == ["lamp-01", "lamp-02"]
    for bad in (
        ["enroll"],
        ["enroll", KIT, "--batch", str(ids), "--out", str(config / "b3")],
        ["enroll", "--batch", str(ids)],
        ["enroll", "--count", "2", "--out", str(config / "b4")],
    ):
        with pytest.raises(SystemExit):
            main(bad)
    capsys.readouterr()


def test_factory_provision_from_a_batch(config, monkeypatch, capsys):
    import serial

    out = config / "kit-batch"
    batch.make_batch([KIT], out)
    record = label.load_record(KIT)
    # factory-provision runs on another computer: its own label store does not have the record.
    label.record_path(KIT).unlink()
    assert stat.S_IMODE((out / "labels" / f"{KIT}.json").stat().st_mode) == 0o600
    status = json.dumps({"ok": True, "device_id": KIT, "factory": False}).encode() + b"\n"
    ok = json.dumps({"ok": True, "device_id": KIT, "restarting": True}).encode() + b"\n"
    # The board answers twice: once for its ID, once for the identity check before sending.
    board = FakeSerialPort([status, status, ok])
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: board)
    monkeypatch.setattr(relay_check, "resolve_all", lambda name: ["203.0.113.7"])
    monkeypatch.setattr(relay_check, "tls_handshake", lambda name, port, ca: True)
    token = (out / "tokens" / f"{KIT}.token").read_text().strip()
    assert main(["factory-provision", "/dev/cu.test", "--gateway", NAME, "--batch", str(out)]) == 0
    frame = json.loads(board.written[-1])
    assert frame["token"] == token and frame["passphrase"] == record["passphrase"]
    assert frame["setup_code"] == record["setup_code"]
    assert rows(out)[0]["provisioned"] == "yes"
    assert not (out / "tokens" / f"{KIT}.token").exists()  # Shown once, then gone.
    assert token not in "".join(capsys.readouterr())

    # A board that is not in the batch: nothing is sent.
    other = json.dumps({"ok": True, "device_id": "oledkit-ffffffffffff", "factory": False})
    board = FakeSerialPort([other.encode() + b"\n"])
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: board)
    with pytest.raises(SystemExit):
        main(["factory-provision", "/dev/cu.test", "--gateway", NAME, "--batch", str(out)])
    assert "not in this batch" in capsys.readouterr().err
    assert all(b'"factory_provision"' not in line for line in board.written)

    # Reading a board's ID for the CSV.
    board = FakeSerialPort([status])
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: board)
    assert main(["factory-provision", "/dev/cu.test", "--read-id"]) == 0
    assert capsys.readouterr().out.strip() == KIT
