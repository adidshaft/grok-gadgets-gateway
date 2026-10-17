"""Signed firmware updates (GW-OTA-001, hub ADR 012): keys, signing, store and delivery."""

import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jsonschema import Draft202012Validator

from grok_gadgets_gateway import cli, firmware
from grok_gadgets_gateway.firmware import CHUNK, Store, Updates
from grok_gadgets_gateway.operator import OperatorError
from grok_gadgets_gateway.protocol import MAX_FRAME, GatewayError, validate_request

PROTOCOL = Path(__file__).parents[1] / "protocol/0.1.0"
RESPONSE = Draft202012Validator(json.loads((PROTOCOL / "device-response.schema.json").read_text()))
BOARD = "esp32s3-oled-kit-wifi"


class Log:
    def __init__(self):
        self.lines = []

    def write(self, record, detail=None):
        self.lines.append(record)

    def events(self, name=None):
        return [r for r in self.lines if name is None or r["event"] == name]


def image(size=CHUNK * 3 + 100, seed=b"grok"):
    out = bytearray()
    while len(out) < size:
        seed = hashlib.sha256(seed).digest()
        out += seed
    return bytes(out[:size])


@pytest.fixture
def builder(tmp_path):
    key = tmp_path / "keys" / "builder.key"
    made = firmware.keygen(key)
    return key, made["public"]


def signed(tmp_path, key, version, data=None, board=BOARD, name=None):
    source = tmp_path / f"{name or version}.bin"
    source.write_bytes(data if data is not None else image(seed=version.encode()))
    out = tmp_path / f"update-{name or version}"
    firmware.sign(source, key, board=board, version=version, out=out)
    return out


def hello(version="0.2.0-alpha.16", ready=True, device_id="kit-1"):
    message = {
        "type": "hello",
        "protocol_version": "0.1.0",
        "token": "t" * 32,
        "device": {
            "device_id": device_id,
            "model": "ESP32-S3 OLED kit",
            "firmware_version": version,
            "boot_id": "boot-1",
            "simulated": False,
            "capabilities": ["display.text", "state"],
            "state": {},
        },
    }
    if ready:
        message["update"] = "ready"
    validate_request(message)
    return message


def frame_size(reply):
    return len(json.dumps(reply, separators=(",", ":")).encode()) + 1


# --- keys and signing ---


def test_keygen_writes_a_private_key_and_its_public_half(tmp_path):
    key = tmp_path / "builder.key"
    made = firmware.keygen(key)
    assert (key.stat().st_mode & 0o777) == 0o600
    assert made["public"] == tmp_path / "builder.pub.pem"
    assert len(made["key_id"]) == 16 and int(made["key_id"], 16) >= 0
    assert firmware.key_id(made["public"]) == made["key_id"]
    assert b"PRIVATE" not in made["public"].read_bytes()
    with pytest.raises(OperatorError, match="exists"):
        firmware.keygen(key)


def test_sign_writes_a_manifest_the_public_key_verifies(tmp_path, builder):
    key, public = builder
    out = signed(tmp_path, key, "0.2.0-alpha.17")
    data = (out / "image.bin").read_bytes()
    raw = (out / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    assert raw == json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    assert len(raw) <= firmware.MAX_MANIFEST
    assert manifest == {
        "board": BOARD,
        "format": "grok-gadgets-update/1",
        "key_id": firmware.key_id(public),
        "min_protocol": "0.1.0",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "version": "0.2.0-alpha.17",
    }
    signature = (out / "manifest.sig").read_bytes()
    assert len(signature) <= 72
    loaded = serialization.load_pem_public_key(public.read_bytes())
    loaded.verify(signature, raw, ec.ECDSA(hashes.SHA256()))  # Raises if wrong.
    update = firmware.read_update(out)
    firmware.verify(update, public)
    assert update.manifest == manifest


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("board", "bad board", "board"),
        ("version", "", "version"),
        ("version", "x" * 33, "version"),
        ("version", "1.0 beta", "version"),
        ("min_protocol", "0.2.0", "protocol"),
    ],
)
def test_sign_refuses_bad_fields(tmp_path, builder, field, value, message):
    key, _ = builder
    source = tmp_path / "image.bin"
    source.write_bytes(image())
    fields = {"board": BOARD, "version": "1.0.0", "min_protocol": "0.1.0", field: value}
    with pytest.raises(OperatorError, match=message):
        firmware.sign(source, key, out=tmp_path / "out", **fields)


def test_sign_refuses_an_empty_or_oversized_image(tmp_path, builder):
    key, _ = builder
    source = tmp_path / "image.bin"
    source.write_bytes(b"")
    with pytest.raises(OperatorError, match="empty"):
        firmware.sign(source, key, board=BOARD, version="1", out=tmp_path / "a")
    with open(source, "wb") as handle:
        handle.truncate(firmware.MAX_IMAGE + 1)
    with pytest.raises(OperatorError, match="larger"):
        firmware.sign(source, key, board=BOARD, version="1", out=tmp_path / "b")


def test_a_wrong_key_or_a_changed_byte_is_refused(tmp_path, builder):
    key, public = builder
    other = firmware.keygen(tmp_path / "other.key")
    out = signed(tmp_path, key, "2.0.0")
    with pytest.raises(OperatorError, match="signature"):
        firmware.verify(firmware.read_update(out), other["public"])
    data = bytearray((out / "image.bin").read_bytes())
    data[100] ^= 1
    (out / "image.bin").write_bytes(bytes(data))
    with pytest.raises(OperatorError, match="SHA-256"):
        firmware.read_update(out)
    out = signed(tmp_path, key, "2.0.1")
    raw = (out / "manifest.json").read_bytes().replace(b"2.0.1", b"2.0.2")
    (out / "manifest.json").write_bytes(raw)
    with pytest.raises(OperatorError, match="signature"):
        firmware.verify(firmware.read_update(out), public)


# --- the relay's store and assignments ---


def test_store_adds_checked_updates_and_assigns_them(tmp_path, builder):
    key, public = builder
    store = Store(tmp_path / "firmware")
    added = store.add(signed(tmp_path, key, "2.0.0"), public_key=public)
    assert added["version"] == "2.0.0"
    stored = tmp_path / "firmware" / BOARD / "2.0.0"
    assert sorted(p.name for p in stored.iterdir()) == [
        "image.bin",
        "manifest.json",
        "manifest.sig",
    ]
    with pytest.raises(OperatorError, match="already"):
        store.add(signed(tmp_path, key, "2.0.0", name="again"), public_key=public)
    with pytest.raises(OperatorError, match="public key"):
        store.add(signed(tmp_path, key, "2.0.1"))
    assert [m["version"] for m in store.list()] == ["2.0.0"]

    store.assign("kit-1", "2.0.0")
    assigned = store.assignments()["kit-1"]
    assert assigned["board"] == BOARD and assigned["version"] == "2.0.0"
    with pytest.raises(OperatorError, match="no stored update"):
        store.assign("kit-1", "9.9.9")
    with pytest.raises(OperatorError, match="device ID"):
        store.assign("bad id", "2.0.0")
    store.withdraw("kit-1")
    assert store.assignments() == {}

    store.add(signed(tmp_path, key, "2.0.0", board="other-board", name="other"), public_key=public)
    with pytest.raises(OperatorError, match="--board"):
        store.assign("kit-1", "2.0.0")
    store.assign("kit-1", "2.0.0", board="other-board")
    store.remove(BOARD, "2.0.0")
    assert [(m["board"], m["version"]) for m in store.list()] == [("other-board", "2.0.0")]
    store.remove("other-board", "2.0.0")
    assert store.assignments() == {}  # Removing an update withdraws it everywhere.


def test_store_refuses_a_wrong_key_unless_unchecked(tmp_path, builder):
    key, _ = builder
    other = firmware.keygen(tmp_path / "other.key")
    store = Store(tmp_path / "firmware")
    update = signed(tmp_path, key, "3.0.0")
    with pytest.raises(OperatorError, match="signature"):
        store.add(update, public_key=other["public"])
    assert store.add(update, unchecked=True)["version"] == "3.0.0"


def test_store_never_stores_a_changed_image(tmp_path, builder):
    key, _ = builder
    update = signed(tmp_path, key, "3.0.0")
    with open(update / "image.bin", "r+b") as handle:
        handle.seek(10)
        handle.write(b"\xff")
    with pytest.raises(OperatorError, match="SHA-256"):
        Store(tmp_path / "firmware").add(update, unchecked=True)


# --- delivery to a gadget ---


@pytest.fixture
def relay(tmp_path, builder):
    key, public = builder
    store = Store(tmp_path / "firmware")
    data = image(size=CHUNK * 5 + 17, seed=b"v2")
    store.add(signed(tmp_path, key, "0.2.0-alpha.17", data=data), public_key=public)
    log = Log()
    clock = [1000.0]
    updates = Updates(store.root, log=log, clock=lambda: clock[0])
    return store, updates, log, data, public, clock


def download(updates, offer, device_id="kit-1", sid="s1"):
    received = bytearray()
    for offset in range(0, offer["size"], offer["chunk"]):
        request = {"type": "update_chunk", "version": offer["version"], "offset": offset}
        validate_request(request)
        reply = updates.handle(device_id, sid, request)
        RESPONSE.validate(reply)
        assert frame_size(reply) <= MAX_FRAME
        received += base64.b64decode(reply["data"])
    return bytes(received)


def status(updates, state, version="0.2.0-alpha.17", reason=None, device_id="kit-1", sid="s1"):
    message = {"type": "update_status", "version": version, "status": state}
    if reason:
        message["reason"] = reason
    validate_request(message)
    reply = updates.handle(device_id, sid, message)
    assert reply == {"ok": True}
    return reply


def test_no_offer_without_the_opt_in_or_an_assignment(relay):
    store, updates, *_ = relay
    assert updates.hello("kit-1", hello(), "s1") == {}
    store.assign("kit-1", "0.2.0-alpha.17")
    assert updates.hello("kit-1", hello(ready=False), "s2") == {}
    assert updates.poll("kit-1", "s2") == {}
    with pytest.raises(GatewayError) as refused:
        updates.handle("kit-1", "s2", {"type": "update_chunk", "version": "x", "offset": 0})
    assert refused.value.code == "invalid_request"
    assert updates.hello("kit-1", hello("0.2.0-alpha.17"), "s3") == {}  # Already running it.


def test_an_assigned_update_is_offered_downloaded_and_applied(relay):
    store, updates, log, data, public, _ = relay
    store.assign("kit-1", "0.2.0-alpha.17")
    reply = {"ok": True, "session_id": "s1", "protocol_version": "0.1.0"}
    reply.update(updates.hello("kit-1", hello(), "s1"))
    RESPONSE.validate(reply)
    assert frame_size(reply) <= MAX_FRAME
    offer = reply["update"]
    assert offer["version"] == "0.2.0-alpha.17" and offer["size"] == len(data)
    assert offer["chunk"] == CHUNK
    manifest = base64.b64decode(offer["manifest"])
    signature = base64.b64decode(offer["signature"])
    key = serialization.load_pem_public_key(public.read_bytes())
    key.verify(signature, manifest, ec.ECDSA(hashes.SHA256()))  # What the gadget checks.
    assert json.loads(manifest)["sha256"] == hashlib.sha256(data).hexdigest()
    assert [e["event"] for e in log.events()] == ["update_offered"]

    status(updates, "started")
    assert download(updates, offer) == data
    status(updates, "verified")
    assert [e.get("percent") for e in log.events("update_progress")] == [25, 50, 75]
    updates.disconnect("kit-1", "s1")

    # The gadget restarts on the new image and says so in its hello.
    assert updates.hello("kit-1", hello("0.2.0-alpha.17"), "s2") == {}
    status(updates, "applied", sid="s2")
    names = [e["event"] for e in log.events()]
    assert names.count("update_applied") == 1
    assert names[:2] == ["update_offered", "update_started"] and "update_verified" in names
    result = store.status()["kit-1"]
    assert result["status"] == "applied" and result["version"] == "0.2.0-alpha.17"
    assert result["reported"] == "0.2.0-alpha.17"
    for line in log.lines:  # Never image bytes or key material in a log line.
        assert "data" not in line and "manifest" not in line and "signature" not in line
        assert line["device"] == "kit-1"


@pytest.mark.parametrize("state", ["rejected", "failed", "reverted"])
def test_a_refused_or_reverted_version_is_not_offered_again(relay, state):
    store, updates, log, *_ = relay
    store.assign("kit-1", "0.2.0-alpha.17")
    assert "update" in updates.hello("kit-1", hello(), "s1")
    reason = {"rejected": "signature", "failed": "sha256", "reverted": "not_online"}[state]
    status(updates, state, reason=reason)
    assert log.events(f"update_{state}")[0]["reason"] == reason
    assert updates.poll("kit-1", "s1") == {"update": None}  # Withdrawn from this session.
    assert updates.poll("kit-1", "s1") == {}
    assert updates.hello("kit-1", hello(), "s2") == {}
    result = store.status()["kit-1"]
    assert result["status"] == state and result["reason"] == reason
    store.assign("kit-1", "0.2.0-alpha.17")  # Assigning again clears the result.
    assert updates.poll("kit-1", "s2")["update"]["version"] == "0.2.0-alpha.17"


def test_an_assignment_made_during_a_session_arrives_in_one_poll(relay, tmp_path):
    store, updates, log, *_ = relay
    assert updates.hello("kit-1", hello(), "s1") == {}
    assert updates.poll("kit-1", "s1") == {}
    store.assign("kit-1", "0.2.0-alpha.17")
    first = updates.poll("kit-1", "s1")
    assert first["update"]["version"] == "0.2.0-alpha.17"
    assert updates.poll("kit-1", "s1") == {}  # Only when it changes.
    store.withdraw("kit-1")
    assert updates.poll("kit-1", "s1") == {"update": None}
    assert log.events("update_withdrawn")
    with pytest.raises(GatewayError) as gone:
        updates.handle(
            "kit-1", "s1", {"type": "update_chunk", "version": "0.2.0-alpha.17", "offset": 0}
        )
    assert gone.value.code == "update_unavailable"


def test_chunk_requests_are_checked(relay):
    store, updates, *_ = relay
    store.assign("kit-1", "0.2.0-alpha.17")
    offer = updates.hello("kit-1", hello(), "s1")["update"]
    for offset in (1, CHUNK + 3, offer["size"], offer["size"] + CHUNK):
        with pytest.raises(GatewayError) as bad:
            updates.handle(
                "kit-1",
                "s1",
                {"type": "update_chunk", "version": offer["version"], "offset": offset},
            )
        assert bad.value.code == "invalid_request"
    with pytest.raises(GatewayError) as other:
        updates.handle("kit-1", "s1", {"type": "update_chunk", "version": "0.0.1", "offset": 0})
    assert other.value.code == "update_unavailable"
    last = (offer["size"] - 1) // CHUNK * CHUNK
    tail = updates.handle(
        "kit-1", "s1", {"type": "update_chunk", "version": offer["version"], "offset": last}
    )
    assert len(base64.b64decode(tail["data"])) == offer["size"] - last


def test_at_most_eight_gadgets_download_at_once(relay):
    store, updates, log, _data, _public, clock = relay
    for n in range(10):
        store.assign(f"kit-{n}", "0.2.0-alpha.17")
        updates.hello(f"kit-{n}", hello(device_id=f"kit-{n}"), f"s{n}")
    chunk = {"type": "update_chunk", "version": "0.2.0-alpha.17", "offset": 0}
    for n in range(8):
        updates.handle(f"kit-{n}", f"s{n}", chunk)
    with pytest.raises(GatewayError) as busy:
        updates.handle("kit-8", "s8", chunk)
    assert busy.value.code == "busy"
    assert log.events("update_busy")[0]["device"] == "kit-8"
    updates.handle("kit-0", "s0", chunk)  # A gadget already downloading continues.
    updates.disconnect("kit-1", "s1")
    updates.handle("kit-8", "s8", chunk)  # A disconnect frees a place.
    clock[0] += firmware.ACTIVE_SECONDS + 1
    updates.handle("kit-9", "s9", chunk)  # So does a stalled download.


def test_a_removed_image_file_answers_unavailable(relay):
    store, updates, *_ = relay
    store.assign("kit-1", "0.2.0-alpha.17")
    offer = updates.hello("kit-1", hello(), "s1")["update"]
    os.unlink(store.root / BOARD / "0.2.0-alpha.17" / "image.bin")
    with pytest.raises(GatewayError) as gone:
        updates.handle(
            "kit-1", "s1", {"type": "update_chunk", "version": offer["version"], "offset": 0}
        )
    assert gone.value.code == "update_unavailable"


def test_status_reports_for_another_version_are_recorded(relay):
    store, updates, log, *_ = relay
    updates.hello("kit-1", hello(), "s1")
    status(updates, "reverted", version="0.1.9", reason="boot")
    assert log.events("update_reverted")[0]["version"] == "0.1.9"
    assert store.status()["kit-1"]["status"] == "reverted"


def test_the_schemas_accept_the_update_frames():
    validate_request(hello())
    for frame in (
        {"type": "update_chunk", "version": "1", "offset": 0},
        {"type": "update_status", "version": "1", "status": "applied"},
        {"type": "update_status", "version": "1", "status": "failed", "reason": "sha256"},
    ):
        validate_request(frame)
    for bad in (
        {**hello(), "update": "maybe"},
        {"type": "update_chunk", "version": "1", "offset": -1},
        {"type": "update_chunk", "version": "1", "offset": 1.0},
        {"type": "update_chunk", "version": "", "offset": 0},
        {"type": "update_status", "version": "1", "status": "done"},
        {"type": "update_status", "version": "1", "status": "failed", "reason": "other"},
    ):
        with pytest.raises(GatewayError):
            validate_request(bad)
    RESPONSE.validate({"ok": True, "data": "AAAA"})
    RESPONSE.validate({"ok": True, "commands": [], "update": None})
    fixture = json.loads((PROTOCOL / "fixtures/update-transcript.json").read_text())
    for frame in fixture["requests"]:
        validate_request(frame)
    for reply in fixture["replies"]:
        RESPONSE.validate(reply)
        assert frame_size(reply) <= MAX_FRAME


# --- through the device listener ---


async def test_a_tcp_gadget_gets_an_offer_only_after_opting_in(tmp_path, builder, private_home):
    from grok_gadgets_gateway.operator import atomic_write
    from grok_gadgets_gateway.service import serve_gateway
    from grok_gadgets_gateway.transport import digest

    key, public = builder
    store = Store(tmp_path / "firmware")
    store.add(signed(tmp_path, key, "0.2.0-alpha.17"), public_key=public)
    store.assign("kit-1", "0.2.0-alpha.17")
    credentials = tmp_path / "credentials.json"
    devices = {"kit-1": {"sha256": digest("t" * 32), "revoked": False}}
    atomic_write(credentials, json.dumps({"devices": devices}).encode())
    atomic_write(tmp_path / "mcp-token", b"x" * 32 + b"\n")
    from grok_gadgets_gateway.rehearse import _free_port

    async with serve_gateway(
        credentials,
        tmp_path / "mcp-token",
        port=_free_port(),
        device_port=_free_port(),
        log_stream=io.StringIO(),
        firmware_directory=store.root,
    ) as running:

        async def session(message):
            reader, writer = await asyncio.open_connection("127.0.0.1", running.device_port)
            frames = [message, {"type": "poll"}]
            replies = []
            for frame in frames:
                writer.write((json.dumps(frame) + "\n").encode())
                await writer.drain()
                replies.append(json.loads(await asyncio.wait_for(reader.readline(), 5)))
            return reader, writer, replies

        _, writer, (plain, polled) = await session(hello(ready=False))
        assert "update" not in plain and "update" not in polled
        writer.close()
        reader, writer, (offered, polled) = await session(hello())
        assert offered["update"]["version"] == "0.2.0-alpha.17" and "update" not in polled
        writer.write(
            (
                json.dumps({"type": "update_chunk", "version": "0.2.0-alpha.17", "offset": 0})
                + "\n"
            ).encode()
        )
        await writer.drain()
        chunk = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert len(base64.b64decode(chunk["data"])) == CHUNK
        writer.close()


# --- operator CLI ---


def test_firmware_cli(tmp_path, capsys, private_home):
    key = tmp_path / "builder.key"
    assert cli.main(["firmware", "keygen", "--out", str(key)]) == 0
    out = capsys.readouterr().out
    public = tmp_path / "builder.pub.pem"
    assert str(public) in out and firmware.key_id(public) in out
    source = tmp_path / "firmware.bin"
    source.write_bytes(image())
    bundle = tmp_path / "bundle"
    sign = ["firmware", "sign", str(source), "--key", str(key), "--board", BOARD]
    assert cli.main([*sign, "--version", "1.2.3", "--out", str(bundle)]) == 0
    assert "1.2.3" in capsys.readouterr().out
    assert cli.main(["firmware", "add", str(bundle), "--public-key", str(public)]) == 0
    assert cli.main(["firmware", "list"]) == 0
    assert f"{BOARD} 1.2.3" in capsys.readouterr().out
    assert cli.main(["firmware", "assign", "kit-1", "1.2.3"]) == 0
    assert cli.main(["firmware", "status"]) == 0
    out = capsys.readouterr().out
    assert "kit-1" in out and "1.2.3" in out and "waiting" in out
    assert cli.main(["firmware", "status", "kit-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kit-1"]["assigned"] == "1.2.3"
    assert cli.main(["firmware", "assign", "kit-1", "--none"]) == 0
    assert cli.main(["firmware", "remove", BOARD, "1.2.3"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["firmware", "assign", "kit-1", "1.2.3"])
    with pytest.raises(SystemExit):
        cli.main(["firmware", "add", str(bundle)])  # A public key, or --unchecked.
    capsys.readouterr()


async def test_rehearse_update(capsys):
    from grok_gadgets_gateway.rehearse import rehearse_update

    lines = []
    await rehearse_update(out=lines.append)
    text = "\n".join(lines)
    assert "applied" in text and "refused" in text and "reverted" in text
    assert all(line.startswith("ok  ") for line in lines)
