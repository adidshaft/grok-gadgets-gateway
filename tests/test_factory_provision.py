"""GW-SETUP-001 (hub ADR 010): factory-provision writes the identity once and prints the label."""

import json
import stat

import pytest
import segno
from test_lan import FakeSerialPort

from grok_gadgets_gateway import label, lan, relay_check
from grok_gadgets_gateway.cli import main
from grok_gadgets_gateway.operator import OperatorError
from grok_gadgets_gateway.wifi_provision import ProvisionError, factory_frame

NAME = "devices.example.net"
DEVICE = "oledkit-0123456789ab"
STATUS = json.dumps({"ok": True, "device_id": DEVICE, "factory": False}).encode() + b"\n"


def test_label_values_match_the_firmware_rules():
    assert label.hotspot_name(DEVICE) == "GrokGadget-89AB"
    for _ in range(50):
        code = label.new_secret(label.CODE_LENGTH)
        assert len(code) == 8 and set(code) <= set(label.ALPHABET)
    assert not set("01IO") & set(label.ALPHABET) and len(label.ALPHABET) == 32
    assert label.grouped("ABCD2345") == "ABCD-2345"
    assert label.wifi_qr_text("GrokGadget-89AB", "K7QX") == "WIFI:T:WPA;S:GrokGadget-89AB;P:K7QX;;"
    assert label.wifi_qr_text('a;b,c:d"e\\', "p") == 'WIFI:T:WPA;S:a\\;b\\,c\\:d\\"e\\\\;P:p;;'


def test_label_files_carry_the_wifi_qr_and_stay_private(tmp_path):
    record = label.label_record(DEVICE, tmp_path)
    paths = label.write_label_files(record, tmp_path / "out")
    for path in paths.values():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert paths["png"].read_bytes().startswith(b"\x89PNG")
    text = paths["text"].read_text()
    assert record["passphrase"] in text and label.grouped(record["setup_code"]) in text
    assert "GrokGadget-89AB" in text and "http://192.168.4.1" in text and "█" in text
    svg = paths["svg"].read_text()
    assert svg.startswith("<svg") and record["passphrase"] in svg and "<rect" in svg
    # The QR code encodes the hotspot and its passphrase, nothing else.
    expected = segno.make(label.wifi_qr_text(record["hotspot"], record["passphrase"]), error="m")
    assert label._qr_blocks(expected) in text
    assert record["setup_code"] not in label.wifi_qr_text(record["hotspot"], record["passphrase"])


def test_records_are_reused_unless_a_new_label_is_asked_for(tmp_path):
    first = label.label_record(DEVICE, tmp_path)
    assert label.load_record(DEVICE, tmp_path) is None  # Saved only after the board accepts.
    label.save_record(first, tmp_path)
    assert stat.S_IMODE(label.record_path(DEVICE, tmp_path).stat().st_mode) == 0o600
    assert label.label_record(DEVICE, tmp_path) == first
    assert label.label_record(DEVICE, tmp_path, new=True) != first
    label.record_path(DEVICE, tmp_path).write_text(json.dumps({**first, "setup_code": "SHORT"}))
    with pytest.raises(OperatorError, match="--new-label"):
        label.load_record(DEVICE, tmp_path)


def test_factory_frame_has_no_wifi_and_fits_the_console():
    frame = json.loads(factory_frame("t" * 43, NAME, 8444, "CA", "P" * 16, "ABCD2345"))
    assert frame == {
        "type": "factory_provision",
        "token": "t" * 43,
        "host": NAME,
        "port": 8444,
        "ca": "CA",
        "passphrase": "P" * 16,
        "setup_code": "ABCD2345",
    }
    assert json.loads(factory_frame("t", NAME, 8444, "CA", "P", "C", replace=True))["replace"]
    with pytest.raises(ProvisionError, match="4096"):
        factory_frame("t", NAME, 8444, "C" * 4096, "P", "C")


def run(tmp_path, monkeypatch, capsys, replies, *extra, enroll=True):
    import serial

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if not (tmp_path / "grok-gadgets" / "credentials.json").exists():
        assert main(["init", "--client", "http"]) == 0
        if enroll:
            assert main(["enroll", DEVICE]) == 0
    capsys.readouterr()
    board = FakeSerialPort(replies)
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: board)
    monkeypatch.setattr(relay_check, "resolve_all", lambda name: ["203.0.113.7"])
    monkeypatch.setattr(relay_check, "tls_handshake", lambda name, port, ca: True)
    code = main(["factory-provision", "/dev/cu.test", "--device", DEVICE, *extra])
    return code, board


def test_factory_provision_writes_the_identity_and_prints_the_label(tmp_path, monkeypatch, capsys):
    ok = b'{"ok":true,"device_id":"' + DEVICE.encode() + b'","restarting":true}\n'
    code, board = run(tmp_path, monkeypatch, capsys, [STATUS, ok], "--gateway", NAME)
    assert code == 0
    frame = json.loads(board.written[-1])
    registry = json.loads((tmp_path / "grok-gadgets" / "credentials.json").read_text())
    assert frame["type"] == "factory_provision" and "ssid" not in frame and "password" not in frame
    assert frame["token"] == registry["devices"][DEVICE]["token"]
    assert frame["host"] == NAME and frame["port"] == 8444 and frame["ca"] == lan.ca_pem_text()
    out, err = capsys.readouterr()
    record = label.load_record(DEVICE)
    assert (
        record["passphrase"] == frame["passphrase"] and record["setup_code"] == frame["setup_code"]
    )
    assert "Wi-Fi:       GrokGadget-89AB" in out and label.grouped(frame["setup_code"]) in out
    assert frame["token"] not in out + err
    assert (
        f"ok  {NAME} is a DNS-only record" in err
        and "GrokGadget-89AB for 10 minutes" in err
        and "until a factory reset" in err
    )
    assert (tmp_path / "grok-gadgets" / "labels" / f"{DEVICE}-label.svg").exists()

    # Running it again keeps the same label; the board refuses without --replace.
    refused = b'{"ok":false,"error":{"code":"already_provisioned","message":"x"}}\n'
    with pytest.raises(SystemExit):
        run(tmp_path, monkeypatch, capsys, [STATUS, refused], "--gateway", NAME)
    assert "pass --replace" in capsys.readouterr().err
    code, board = run(tmp_path, monkeypatch, capsys, [STATUS, ok], "--gateway", NAME, "--replace")
    again = json.loads(board.written[-1])
    assert code == 0 and again["replace"] is True
    assert again["setup_code"] == frame["setup_code"] and again["passphrase"] == frame["passphrase"]
    code, board = run(
        tmp_path, monkeypatch, capsys, [STATUS, ok], "--gateway", NAME, "--replace", "--new-label"
    )
    assert json.loads(board.written[-1])["passphrase"] != frame["passphrase"]

    # The label command reprints from the record without the board.
    capsys.readouterr()
    assert main(["label", DEVICE]) == 0
    assert label.load_record(DEVICE)["passphrase"] in capsys.readouterr().out


def test_factory_provision_refuses_before_sending(tmp_path, monkeypatch, capsys):
    # Not enrolled: nothing is sent; --enroll adds it first.
    with pytest.raises(SystemExit):
        run(tmp_path, monkeypatch, capsys, [STATUS], "--gateway", NAME, enroll=False)
    assert "not enrolled" in capsys.readouterr().err
    ok = b'{"ok":true,"device_id":"' + DEVICE.encode() + b'"}\n'
    code, board = run(tmp_path, monkeypatch, capsys, [STATUS, ok], "--gateway", NAME, "--enroll")
    assert code == 0 and json.loads(board.written[-1])["type"] == "factory_provision"
    assert label.load_record(DEVICE) is not None

    # Another board on the port, or firmware without a factory identity: nothing is sent.
    other = json.dumps({"ok": True, "device_id": "oledkit-ffffffffffff", "factory": False})
    for reply, message in (
        (other.encode() + b"\n", "nothing sent"),
        (b'{"ok":true,"device_id":"' + DEVICE.encode() + b'"}\n', "0.2.0-alpha.15"),
    ):
        with pytest.raises(SystemExit):
            run(tmp_path, monkeypatch, capsys, [reply], "--gateway", NAME, "--replace")
        assert message in capsys.readouterr().err

    # A proxied relay name is refused before the board is touched.
    import serial

    monkeypatch.setattr(serial, "Serial", lambda *a, **k: pytest.fail("board opened"))
    monkeypatch.setattr(relay_check, "resolve_all", lambda name: ["104.16.1.1"])
    with pytest.raises(SystemExit):
        main(["factory-provision", "/dev/cu.test", "--device", DEVICE, "--gateway", NAME])
    assert "DNS only" in capsys.readouterr().err
