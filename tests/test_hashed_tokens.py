"""GW-CLAIM-003 (hub ADR 011): device tokens are stored as SHA-256 hashes only."""

import hashlib
import hmac
import json
import stat

import pytest
from test_lan import FakeSerialPort

from grok_gadgets_gateway import relay_check, transport
from grok_gadgets_gateway.cli import main
from grok_gadgets_gateway.operator import enroll, revoke
from grok_gadgets_gateway.protocol import GatewayError
from grok_gadgets_gateway.transport import CredentialError, Credentials, parse_registry

NAME = "devices.example.net"
DEVICE = "oledkit-0123456789ab"
STATUS = json.dumps({"ok": True, "device_id": DEVICE, "factory": False}).encode() + b"\n"
DONE = b'{"ok":true,"device_id":"' + DEVICE.encode() + b'","restarting":true}\n'
FAILED = b'{"ok":false,"error":{"code":"nvs_failed","message":"x"}}\n'


def sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


def write_registry(path, devices):
    path.write_text(json.dumps({"devices": devices}))
    path.chmod(0o600)


def test_enroll_stores_only_the_hash(tmp_path):
    path = tmp_path / "credentials.json"
    token = enroll("dev-1", path)
    record = json.loads(path.read_text())["devices"]["dev-1"]
    assert record == {"sha256": sha(token), "revoked": False}
    assert token not in path.read_text()
    credentials = Credentials(path)
    credentials.validate()
    credentials.check("dev-1", token)
    with pytest.raises(GatewayError, match="rejected"):
        credentials.check("dev-1", token + "x")
    rotated = enroll("dev-1", path, rotate=True)
    with pytest.raises(GatewayError, match="rejected"):
        credentials.check("dev-1", token)
    credentials.check("dev-1", rotated)
    revoke("dev-1", path)
    with pytest.raises(GatewayError) as caught:
        credentials.check("dev-1", rotated)
    assert caught.value.code == "revoked"


def test_registry_entries_need_exactly_one_of_token_or_hash():
    good = {"devices": {"d": {"sha256": "a" * 64, "revoked": False}}}
    assert parse_registry(json.dumps(good).encode())["d"]["sha256"] == "a" * 64
    for record in (
        {"sha256": "a" * 63},
        {"sha256": "A" * 64},
        {"sha256": "a" * 64, "token": "t" * 32},
        {"revoked": False},
    ):
        with pytest.raises(CredentialError):
            parse_registry(json.dumps({"devices": {"d": record}}).encode())


def test_first_start_migrates_plain_tokens_and_keeps_a_private_backup(tmp_path):
    path = tmp_path / "credentials.json"
    plain = {
        "dev-1": {"token": "a" * 43, "revoked": False},
        "dev-2": {"token": "b" * 43, "revoked": True},
        "dev-3": {"sha256": sha("c" * 43), "revoked": False},
    }
    write_registry(path, plain)
    original = path.read_text()
    credentials = Credentials(path)
    assert credentials.validate() == 3
    devices = json.loads(path.read_text())["devices"]
    assert devices == {
        "dev-1": {"sha256": sha("a" * 43), "revoked": False},
        "dev-2": {"sha256": sha("b" * 43), "revoked": True},
        "dev-3": {"sha256": sha("c" * 43), "revoked": False},
    }
    backup = tmp_path / "credentials.json.pre-hash"
    assert backup.read_text() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The same tokens still work; revocation survives the migration.
    credentials.check("dev-1", "a" * 43)
    credentials.check("dev-3", "c" * 43)
    with pytest.raises(GatewayError) as caught:
        credentials.check("dev-2", "b" * 43)
    assert caught.value.code == "revoked"
    # A second start changes nothing and never overwrites the backup.
    write_before = path.read_text()
    Credentials(path).validate()
    assert path.read_text() == write_before and backup.read_text() == original


def test_a_registry_without_plain_tokens_is_not_rewritten(tmp_path):
    path = tmp_path / "credentials.json"
    write_registry(path, {"dev-1": {"sha256": sha("a" * 43), "revoked": False}})
    before = path.stat().st_mtime_ns
    Credentials(path).validate()
    assert path.stat().st_mtime_ns == before
    assert not (tmp_path / "credentials.json.pre-hash").exists()


def test_checks_compare_hashes_in_constant_time(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    token = enroll("dev-1", path)
    seen = []
    original = hmac.compare_digest

    def spy(left, right):
        seen.append((left, right))
        return original(left, right)

    monkeypatch.setattr(transport.hmac, "compare_digest", spy)
    credentials = Credentials(path)
    credentials.validate()
    credentials.check("dev-1", token)
    with pytest.raises(GatewayError):
        credentials.check("unknown", token)
    # Both checks compared two 32-byte digests, also for the unknown device.
    assert len(seen) == 2 and all(len(a) == len(b) == 32 for a, b in seen)


# --- provisioning with hashed tokens ---


def setup(tmp_path, monkeypatch, capsys, replies, *, enrolled=True):
    import serial

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if not (tmp_path / "grok-gadgets" / "credentials.json").exists():
        assert main(["init", "--client", "http"]) == 0
        if enrolled:
            assert main(["enroll", DEVICE, "--token-file", str(tmp_path / "dev.token")]) == 0
    capsys.readouterr()
    board = FakeSerialPort(replies)
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: board)
    monkeypatch.setattr(relay_check, "resolve_all", lambda name: ["203.0.113.7"])
    monkeypatch.setattr(relay_check, "tls_handshake", lambda name, port, ca: True)
    return board


def registry(tmp_path):
    return json.loads((tmp_path / "grok-gadgets" / "credentials.json").read_text())["devices"]


def factory(*extra):
    return main(
        ["factory-provision", "/dev/cu.test", "--device", DEVICE, "--gateway", NAME, *extra]
    )


def test_factory_provision_needs_a_token_source_once_tokens_are_hashed(
    tmp_path, monkeypatch, capsys
):
    import serial

    setup(tmp_path, monkeypatch, capsys, [])
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: pytest.fail("board opened"))
    with pytest.raises(SystemExit):
        factory()
    err = capsys.readouterr().err
    assert "--token-file" in err and "--rotate" in err


def test_factory_provision_sends_the_token_from_a_token_file(tmp_path, monkeypatch, capsys):
    board = setup(tmp_path, monkeypatch, capsys, [STATUS, DONE])
    token_file = tmp_path / "dev.token"
    assert factory("--token-file", str(token_file)) == 0
    frame = json.loads(board.written[-1])
    assert frame["token"] == token_file.read_text().strip()
    assert registry(tmp_path)[DEVICE]["sha256"] == sha(frame["token"])
    out, err = capsys.readouterr()
    assert frame["token"] not in out + err


def test_factory_provision_refuses_a_token_file_that_does_not_match(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch, capsys, [STATUS, DONE])
    wrong = tmp_path / "wrong.token"
    wrong.write_text("w" * 43 + "\n")
    wrong.chmod(0o600)
    with pytest.raises(SystemExit):
        factory("--token-file", str(wrong))
    assert "does not match" in capsys.readouterr().err
    wrong.chmod(0o644)
    with pytest.raises(SystemExit):
        factory("--token-file", str(wrong))
    assert "0600" in capsys.readouterr().err


def test_factory_provision_with_a_token_file_needs_no_local_registry(tmp_path, monkeypatch, capsys):
    # The relay holds the hash; the computer at the bench holds only the token file.
    board = setup(tmp_path, monkeypatch, capsys, [STATUS, DONE], enrolled=False)
    token_file = tmp_path / "relay.token"
    token_file.write_text("r" * 43 + "\n")
    token_file.chmod(0o600)
    assert factory("--token-file", str(token_file)) == 0
    assert json.loads(board.written[-1])["token"] == "r" * 43
    assert DEVICE not in registry(tmp_path)


def test_rotate_stores_the_new_hash_only_after_the_board_confirms(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch, capsys, [STATUS, FAILED])
    before = registry(tmp_path)[DEVICE]
    with pytest.raises(SystemExit):
        factory("--rotate")
    assert registry(tmp_path)[DEVICE] == before

    board = setup(tmp_path, monkeypatch, capsys, [STATUS, DONE])
    assert factory("--rotate") == 0
    sent = json.loads(board.written[-1])["token"]
    assert registry(tmp_path)[DEVICE]["sha256"] == sha(sent) != before["sha256"]
    out, err = capsys.readouterr()
    assert sent not in out + err


def test_enroll_during_factory_provision_happens_after_the_board_confirms(
    tmp_path, monkeypatch, capsys
):
    setup(tmp_path, monkeypatch, capsys, [STATUS, FAILED], enrolled=False)
    with pytest.raises(SystemExit):
        factory("--enroll")
    assert DEVICE not in registry(tmp_path)
    board = setup(tmp_path, monkeypatch, capsys, [STATUS, DONE], enrolled=False)
    assert factory("--enroll") == 0
    assert registry(tmp_path)[DEVICE]["sha256"] == sha(json.loads(board.written[-1])["token"])


def wifi(tmp_path, monkeypatch, capsys, *extra, replies=None):
    import builtins
    import getpass

    board = setup(
        tmp_path,
        monkeypatch,
        capsys,
        replies or [b'{"ok":true,"device_id":"' + DEVICE.encode() + b'"}\n'] * 3,
    )
    answers = ["home", "password1"]
    monkeypatch.setattr(builtins, "input", lambda prompt="": answers.pop(0))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": answers.pop(0))
    code = main(["wifi-provision", "/dev/cu.test", "--device", DEVICE, "--gateway", NAME, *extra])
    return code, board


def test_wifi_provision_with_a_token_file_or_rotate(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        wifi(tmp_path, monkeypatch, capsys)
    assert "--token-file" in capsys.readouterr().err
    code, board = wifi(tmp_path, monkeypatch, capsys, "--token-file", str(tmp_path / "dev.token"))
    assert code == 0
    assert json.loads(board.written[-1])["token"] == (tmp_path / "dev.token").read_text().strip()
    code, board = wifi(tmp_path, monkeypatch, capsys, "--rotate")
    assert code == 0
    assert registry(tmp_path)[DEVICE]["sha256"] == sha(json.loads(board.written[-1])["token"])


def test_a_legacy_plain_registry_still_provisions_before_migration(tmp_path, monkeypatch, capsys):
    board = setup(tmp_path, monkeypatch, capsys, [STATUS, DONE], enrolled=False)
    path = tmp_path / "grok-gadgets" / "credentials.json"
    write_registry(path, {DEVICE: {"token": "p" * 43, "revoked": False}})
    assert factory() == 0
    assert json.loads(board.written[-1])["token"] == "p" * 43
