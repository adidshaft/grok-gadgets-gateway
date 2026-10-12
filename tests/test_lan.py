"""LAN TLS device listener (hub ADR 005, GW-LAN-001).

Tests bind loopback with TLS and widen the private-peer rule to 127.0.0.1, because a test
host may have no private address. The address and peer rules have their own tests.
"""

import asyncio
import io
import json
import socket
import ssl
import stat

import pytest
from cryptography import x509
from test_domain import hello
from test_transports import TOKEN, write_credentials

from grok_gadgets_gateway import lan
from grok_gadgets_gateway.domain import Gateway
from grok_gadgets_gateway.operator import OperatorError
from grok_gadgets_gateway.service import serve_gateway
from grok_gadgets_gateway.transport import Credentials, DeviceServer
from grok_gadgets_gateway.wifi_provision import (
    ProvisionError,
    provision,
    provision_frame,
    read_reply,
)

ADDRESS = "192.168.50.10"


class Log:
    def __init__(self):
        self.records = []

    def write(self, record, detail=None):
        self.records.append(record)

    def events(self, name):
        return [r for r in self.records if r["event"] == name]


@pytest.fixture
def pki(tmp_path):
    directory = tmp_path / "lan"
    lan.create_ca(directory)
    lan.issue_server_cert(ADDRESS, directory)
    # The test server answers on 127.0.0.1 with a certificate for ADDRESS; clients verify
    # the chain and the name ADDRESS, as the device does.
    return directory


@pytest.fixture
def loopback_is_private(monkeypatch):
    real = lan.is_private
    monkeypatch.setattr(lan, "is_private", lambda a: a == "127.0.0.1" or real(a))


def client_context(directory):
    context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


async def start(tmp_path, pki, **kwargs):
    path = tmp_path / "auth.json"
    write_credentials(path)
    log = Log()
    gateway = Gateway()
    server = DeviceServer(
        gateway,
        Credentials(path),
        port=0,
        tls=lan.server_context(ADDRESS, pki),
        log=log,
        **kwargs,
    )
    await server.start()
    return gateway, server, log, path


async def tls_connect(server, context, *, token=TOKEN):
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", server.port, ssl=context, server_hostname=ADDRESS
    )
    message = hello("dev-1")
    message["token"] = token
    return reader, writer, await exchange(reader, writer, message)


async def exchange(reader, writer, message):
    writer.write((json.dumps(message) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 2)
    return json.loads(line) if line else None


async def closed(reader):
    try:
        return await asyncio.wait_for(reader.read(), 2) == b""
    except (ConnectionError, ssl.SSLError):
        return True


# Address rules


@pytest.mark.parametrize(
    "text",
    [
        "0.0.0.0",
        "::",
        "127.0.0.1",
        "8.8.8.8",
        "100.64.1.1",
        "169.254.1.1",
        "gateway.lan",
        "bad_name.local",
        "fd00::1",
        "192.168.1.20:0",
        "192.168.1.20:70000",
    ],
)
def test_lan_address_refuses_all_loopback_public_and_shared(text):
    with pytest.raises(OperatorError):
        lan.parse_lan_address(text)


def test_lan_address_accepts_one_private_address():
    assert lan.parse_lan_address("192.168.1.20") == ("192.168.1.20", 8767)
    assert lan.parse_lan_address("10.0.0.5:9000") == ("10.0.0.5", 9000)
    assert lan.parse_lan_address("172.20.1.1") == ("172.20.1.1", 8767)


def test_private_peer_rule():
    assert lan.is_private("192.168.1.30")
    assert lan.is_private("::ffff:10.1.2.3")
    for address in ("127.0.0.1", "8.8.8.8", "100.64.0.1", "fe80::1", None, "nonsense"):
        assert not lan.is_private(address)


def test_device_server_refuses_non_private_or_plain_lan_bind(tmp_path, pki):
    path = tmp_path / "auth.json"
    write_credentials(path)
    context = lan.server_context(ADDRESS, pki)
    for host, tls in (("0.0.0.0", context), ("8.8.8.8", context), (ADDRESS, None)):
        with pytest.raises(ValueError):
            DeviceServer(Gateway(), Credentials(path), host=host, tls=tls)


# Certificates


def test_ca_and_listener_certificate(tmp_path):
    directory = tmp_path / "lan"
    assert lan.create_ca(directory) == directory / "ca.pem"
    assert lan.create_ca(directory) is None  # init keeps an existing CA
    lan.issue_server_cert(ADDRESS, directory)
    for name in ("ca.key", "server.key", "ca.pem", "server.pem"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    cert = x509.load_pem_x509_certificate((directory / "server.pem").read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(ip) for ip in names.get_values_for_type(x509.IPAddress)] == [ADDRESS]
    assert names.get_values_for_type(x509.DNSName) == [ADDRESS]
    assert cert.subject.rfc4514_string() == f"CN={ADDRESS}"
    assert len(lan.ca_fingerprint(directory).split(":")) == 32
    with pytest.raises(OperatorError, match="does not name"):
        lan.server_context("192.168.50.11", directory)
    old = lan.ca_fingerprint(directory)
    lan.create_ca(directory, rotate=True)
    assert lan.ca_fingerprint(directory) != old
    assert not (directory / "server.pem").exists()
    with pytest.raises(OperatorError, match="lan-cert"):
        lan.server_context(ADDRESS, directory)


def test_listener_certificate_needs_a_ca(tmp_path):
    with pytest.raises(OperatorError, match="init"):
        lan.issue_server_cert(ADDRESS, tmp_path / "none")


# Listener behaviour


async def test_listener_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "auth.json"
    write_credentials(path)
    token = tmp_path / "mcp-token"
    token.write_text("t" * 32)
    token.chmod(0o600)
    async with serve_gateway(str(path), str(token), device_port=0, port=0) as running:
        assert running.lan is None


async def test_serve_refuses_bad_lan_settings(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    token = tmp_path / "mcp-token"
    token.write_text("t" * 32)
    token.chmod(0o600)
    with pytest.raises(OperatorError, match="private"):
        async with serve_gateway(str(path), str(token), port=0, lan_listen="0.0.0.0"):
            pass
    with pytest.raises(OperatorError, match="lan-cert"):
        async with serve_gateway(
            str(path), str(token), port=0, lan_listen=ADDRESS, lan_directory=tmp_path / "x"
        ):
            pass


async def test_tls_device_session_command_and_events(tmp_path, pki, loopback_is_private):
    gateway, server, log, _ = await start(tmp_path, pki)
    try:
        reader, writer, reply = await tls_connect(server, client_context(pki))
        assert reply["ok"] and reply["protocol_version"] == "0.1.0"
        gateway.command("dev-1", "rgb.set", {"r": 1, "g": 2, "b": 3, "on": True}, "c1")
        command = (await exchange(reader, writer, {"type": "poll"}))["commands"][0]
        ack = {"type": "ack", "command_id": "c1", "status": "executed", "state": {}}
        assert command["command_id"] == "c1"
        assert (await exchange(reader, writer, ack))["ok"]
        assert gateway.command_status("c1")["status"] == "executed"
        writer.close()
        await asyncio.sleep(0.05)
        assert [r["device"] for r in log.events("lan_hello")] == ["dev-1"]
        assert log.events("lan_connected")[0]["peer"] == "127.0.0.1"
        assert log.events("lan_disconnected")
        assert TOKEN not in json.dumps(log.records)
    finally:
        await server.close()


async def test_wrong_ca_is_rejected_by_the_device_and_logged(tmp_path, pki, loopback_is_private):
    other = tmp_path / "other"
    lan.create_ca(other)
    _, server, log, _ = await start(tmp_path, pki)
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            await tls_connect(server, client_context(other))
        await asyncio.sleep(0.05)
        assert log.events("lan_tls_failed")
        assert not log.events("lan_hello")
    finally:
        await server.close()


async def test_wrong_name_and_plain_tcp_are_rejected(tmp_path, pki, loopback_is_private):
    _, server, log, _ = await start(tmp_path, pki)
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.open_connection(
                "127.0.0.1", server.port, ssl=client_context(pki), server_hostname="192.168.50.99"
            )
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        message = hello("dev-1")
        message["token"] = TOKEN
        writer.write((json.dumps(message) + "\n").encode())
        await writer.drain()
        assert await closed(reader)
        await asyncio.sleep(0.05)
        assert len(log.events("lan_tls_failed")) == 2
        assert not log.events("lan_hello")
    finally:
        await server.close()


async def test_wrong_and_revoked_tokens(tmp_path, pki, loopback_is_private):
    gateway, server, log, path = await start(tmp_path, pki)
    context = client_context(pki)
    try:
        reader, _, reply = await tls_connect(server, context, token="x" * 32)
        assert reply["error"]["code"] == "unauthorized"
        assert await closed(reader)
        reader, writer, reply = await tls_connect(server, context)
        assert reply["ok"]
        write_credentials(path, revoked=True)  # revoke while the session is live
        reply = await exchange(reader, writer, {"type": "poll"})
        assert reply["error"]["code"] == "revoked"
        assert await closed(reader)
        reader, _, reply = await tls_connect(server, context)
        assert reply["error"]["code"] == "revoked"
        await asyncio.sleep(0.05)
        codes = [r["code"] for r in log.events("lan_auth_rejected")]
        assert codes == ["unauthorized", "revoked", "revoked"]
        assert not gateway.state("dev-1")["available"]
        assert TOKEN not in json.dumps(log.records)
    finally:
        await server.close()


async def test_replayed_hello_and_frames(tmp_path, pki, loopback_is_private):
    gateway, server, _, _ = await start(tmp_path, pki)
    context = client_context(pki)
    try:
        r1, w1, first = await tls_connect(server, context)
        r2, w2, second = await tls_connect(server, context)  # the same hello, replayed
        assert first["session_id"] != second["session_id"]
        assert (await exchange(r1, w1, {"type": "poll"}))["error"]["code"] == "stale_session"
        assert await closed(r1)
        event = {"type": "event", "event_id": "e1", "name": "button", "data": {"pressed": True}}
        assert not (await exchange(r2, w2, event)).get("duplicate")
        assert (await exchange(r2, w2, event))["duplicate"]
        assert len(gateway.read_events()["events"]) == 1
        gateway.command("dev-1", "rgb.set", {"r": 0, "g": 0, "b": 0, "on": False}, "c9")
        assert (await exchange(r2, w2, {"type": "poll"}))["commands"]
        ack = {"type": "ack", "command_id": "c9", "status": "executed", "state": {}}
        assert (await exchange(r2, w2, ack))["ok"]
        assert (await exchange(r2, w2, ack))["duplicate"]
    finally:
        await server.close()


async def test_reconnect_after_listener_restart(tmp_path, pki, loopback_is_private):
    gateway, server, _, path = await start(tmp_path, pki)
    port = server.port
    reader, _, reply = await tls_connect(server, client_context(pki))
    assert reply["ok"]
    await server.close()
    assert await closed(reader)
    again = DeviceServer(
        gateway, Credentials(path), port=port, tls=lan.server_context(ADDRESS, pki), log=Log()
    )
    await again.start()
    try:
        _, _, reply = await tls_connect(again, client_context(pki))
        assert reply["ok"]
    finally:
        await again.close()


async def test_non_private_peer_is_closed_before_tls(tmp_path, pki):
    _, server, log, _ = await start(tmp_path, pki)  # real rule: 127.0.0.1 is not private
    try:
        with pytest.raises((ConnectionError, ssl.SSLError, asyncio.IncompleteReadError)):
            await tls_connect(server, client_context(pki))
        await asyncio.sleep(0.05)
        assert log.events("lan_peer_rejected")[0]["peer"] == "127.0.0.1"
        assert not log.events("lan_tls_failed")
    finally:
        await server.close()


async def test_connection_rate_limit_per_source(tmp_path, pki, loopback_is_private):
    _, server, log, _ = await start(tmp_path, pki, connects_per_minute=2)
    context = client_context(pki)
    try:
        for _ in range(2):
            _, writer, reply = await tls_connect(server, context)
            assert reply["ok"]
            writer.close()
        with pytest.raises((ConnectionError, ssl.SSLError, asyncio.IncompleteReadError)):
            await tls_connect(server, context)
        await asyncio.sleep(0.05)
        assert log.events("lan_rate_limited")
    finally:
        await server.close()


async def test_frame_rate_limit_answers_busy(tmp_path, pki, loopback_is_private):
    _, server, _, _ = await start(tmp_path, pki, frames_per_second=3)
    try:
        reader, writer, reply = await tls_connect(server, client_context(pki))
        assert reply["ok"]
        replies = [await exchange(reader, writer, {"type": "poll"}) for _ in range(3)]
        assert [r["ok"] for r in replies] == [True, True, False]
        assert replies[-1]["error"]["code"] == "busy"
        await asyncio.sleep(1.05)
        assert (await exchange(reader, writer, {"type": "poll"}))["ok"]
    finally:
        await server.close()


async def test_handshake_timeout_and_pending_cap(tmp_path, pki, loopback_is_private):
    _, server, log, _ = await start(tmp_path, pki, handshake_timeout=0.3, max_pending=2)
    try:
        idle = [await asyncio.open_connection("127.0.0.1", server.port) for _ in range(3)]
        assert await closed(idle[0][0])  # the oldest waiting socket is evicted
        await asyncio.sleep(0.5)
        assert log.events("lan_tls_failed")
        _, _, reply = await tls_connect(server, client_context(pki))
        assert reply["ok"]
    finally:
        await server.close()


# Provisioning tool


class FakeSerial:
    def __init__(self, replies):
        self.replies = list(replies)
        self.written = []
        self.pending = io.BytesIO()

    def write(self, data):
        self.written.append(bytes(data))
        if self.replies:
            self.pending = io.BytesIO(self.replies.pop(0))

    def read(self, size):
        return self.pending.read(size)


def test_provisioning_frame_checks_wifi_settings():
    with pytest.raises(ProvisionError):
        provision_frame("", "password1", "t" * 32, ADDRESS, 8767, "CA")
    with pytest.raises(ProvisionError) as info:
        provision_frame("home", "short", "t" * 32, ADDRESS, 8767, "CA")
    assert "short" not in str(info.value)
    frame = json.loads(provision_frame("home", "", "t" * 32, ADDRESS, 8767, "CA"))
    assert frame["type"] == "provision" and frame["port"] == 8767


def test_provisioning_checks_board_identity_before_sending():
    frame = provision_frame("home", "secret-pass", "t" * 32, ADDRESS, 8767, "CA")
    port = FakeSerial([b'boot text\n{"ok":true,"device_id":"other"}\n'])
    with pytest.raises(ProvisionError, match="nothing sent"):
        provision(port, "dev-1", frame)
    assert port.written == [b'{"type":"status"}\n']
    port = FakeSerial(
        [b'{"ok":true,"device_id":"dev-1"}\n', b'wifi: saving\n{"ok":true,"restarting":true}\n']
    )
    assert provision(port, "dev-1", frame)["restarting"]
    assert port.written[-1] == frame


def test_provisioning_failure_never_echoes_secrets():
    frame = provision_frame("home", "secret-pass", "t" * 32, ADDRESS, 8767, "CA")
    reply = b'{"ok":false,"error":{"code":"invalid_settings","message":"CA is not PEM"}}\n'
    port = FakeSerial([b'{"ok":true,"device_id":"dev-1"}\n', reply])
    with pytest.raises(ProvisionError) as info:
        provision(port, "dev-1", frame)
    assert "invalid_settings" in str(info.value)
    assert "secret-pass" not in str(info.value) and "t" * 32 not in str(info.value)


def test_read_reply_times_out():
    ticks = iter(range(100))
    with pytest.raises(ProvisionError, match="did not answer"):
        read_reply(FakeSerial([]), timeout=3, clock=lambda: next(ticks))


# CLI


def test_cli_init_creates_ca_and_lan_cert_and_serve_default_is_off(tmp_path, monkeypatch, capsys):
    from grok_gadgets_gateway.cli import build_parser, main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert main(["init", "--client", "http"]) == 0
    ca = tmp_path / "grok-gadgets" / "lan" / "ca.pem"
    assert ca.is_file()
    assert main(["lan-cert", ADDRESS]) == 0
    assert (ca.parent / "server.pem").is_file()
    capsys.readouterr()
    assert main(["lan-ca", "show"]) == 0
    assert "SHA-256" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["lan-cert", "0.0.0.0"])
    assert build_parser().parse_args(["serve"]).lan_listen is None


class VanishedPeerWriter:
    """A writer whose TLS shutdown never completes, as when a device loses power."""

    def __init__(self, error):
        self.error = error
        self.aborted = False
        self.transport = self

    def close(self):
        pass

    async def wait_closed(self):
        if self.error is None:
            await asyncio.sleep(3600)
        raise self.error

    def abort(self):
        self.aborted = True


@pytest.mark.parametrize("error", [None, TimeoutError("SSL shutdown timed out"), ssl.SSLError()])
async def test_closing_a_vanished_tls_peer_never_raises(monkeypatch, error):
    from grok_gadgets_gateway import transport

    monkeypatch.setattr(transport, "CLOSE_TIMEOUT", 0.05)
    writer = VanishedPeerWriter(error)
    await transport._close_writer(writer)
    assert writer.aborted


class FakeSerialPort(FakeSerial):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def provision_cli(tmp_path, monkeypatch, opener, answers):
    import builtins
    import getpass

    import serial

    from grok_gadgets_gateway.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if not (tmp_path / "grok-gadgets" / "credentials.json").exists():
        assert main(["init", "--client", "http"]) == 0
        assert main(["enroll", "dev-1"]) == 0
    prompts = []

    def ask(prompt=""):
        prompts.append(prompt)
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(builtins, "input", ask)
    monkeypatch.setattr(getpass, "getpass", ask)
    monkeypatch.setattr(serial, "Serial", opener)
    with pytest.raises(SystemExit) as stopped:
        main(["wifi-provision", "/dev/cu.test", "--device", "dev-1", "--lan", ADDRESS])
    return stopped.value.code, prompts


def test_wifi_provision_finds_the_board_before_asking(tmp_path, monkeypatch, capsys):
    import serial

    def missing(*args, **kwargs):
        raise serial.SerialException("could not open port")

    code, prompts = provision_cli(tmp_path, monkeypatch, missing, ["home"])
    assert code == 2 and prompts == []
    assert "cannot use /dev/cu.test" in capsys.readouterr().err

    board = FakeSerialPort([b'{"ok":true,"device_id":"other"}\n'])
    code, prompts = provision_cli(tmp_path, monkeypatch, lambda *a, **k: board, ["home"])
    assert code == 2 and prompts == [] and board.written == [b'{"type":"status"}\n']
    assert "nothing sent" in capsys.readouterr().err


def test_wifi_provision_cancel_sends_nothing(tmp_path, monkeypatch, capsys):
    board = FakeSerialPort([b'{"ok":true,"device_id":"dev-1"}\n'])
    for answers in (["home", EOFError()], [KeyboardInterrupt()]):
        board.written.clear()
        board.replies = [b'{"ok":true,"device_id":"dev-1"}\n']
        code, _ = provision_cli(tmp_path, monkeypatch, lambda *a, **k: board, answers)
        assert code == 2 and board.written == [b'{"type":"status"}\n']
        err = capsys.readouterr().err
        assert "Cancelled; nothing was sent to the board." in err and "Traceback" not in err


# mDNS names (hub ADR 006)

NAME = "test-mac.local"


def test_lan_address_accepts_an_mdns_name():
    assert lan.parse_lan_address("Test-Mac.local:9000") == (NAME, 9000)
    assert lan.parse_lan_address(NAME) == (NAME, lan.DEFAULT_LAN_PORT)


def test_resolving_a_name_picks_a_private_address():
    assert lan.resolve_lan_host(ADDRESS) == ADDRESS
    assert lan.resolve_lan_host(NAME, lambda host: ["127.0.0.1", "8.8.8.8", ADDRESS]) == ADDRESS
    with pytest.raises(OperatorError, match="no private home-network address"):
        lan.resolve_lan_host(NAME, lambda host: ["127.0.0.1"])

    def unknown(host):
        raise socket.gaierror("not found")

    with pytest.raises(OperatorError, match="scutil --get LocalHostName"):
        lan.resolve_lan_host(NAME, unknown)


def test_a_name_certificate_names_only_the_name(tmp_path):
    directory = tmp_path / "lan"
    lan.create_ca(directory)
    cert = x509.load_pem_x509_certificate(lan.issue_server_cert(NAME, directory).read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == [NAME]
    assert names.get_values_for_type(x509.IPAddress) == []
    assert lan.server_context(NAME, directory)
    with pytest.raises(OperatorError, match="lan-cert other-mac.local"):
        lan.server_context("other-mac.local", directory)
    with pytest.raises(OperatorError, match=f"lan-cert {ADDRESS}"):
        lan.server_context(ADDRESS, directory)


def this_computers_private_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # No packet is sent for UDP connect.
            address = probe.getsockname()[0]
        except OSError:
            return None
    return address if lan.is_private(address) else None


async def test_a_named_listener_follows_an_address_change(tmp_path, monkeypatch):
    moved_to = this_computers_private_address()
    if moved_to is None:
        pytest.skip("needs a private network address on this computer")
    real = lan.is_private
    monkeypatch.setattr(lan, "is_private", lambda a: a == "127.0.0.1" or real(a))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    directory = tmp_path / "grok-gadgets" / "lan"
    lan.create_ca(directory)
    lan.issue_server_cert(NAME, directory)
    path = tmp_path / "auth.json"
    write_credentials(path)
    token = tmp_path / "mcp-token"
    token.write_text("t" * 32)
    token.chmod(0o600)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        lan_port = probe.getsockname()[1]
    answers = iter([["127.0.0.1"], OSError("mDNS paused"), [moved_to]])

    def resolver(host):
        assert host == NAME
        answer = next(answers, [moved_to])
        if isinstance(answer, Exception):
            raise answer
        return answer

    stream = io.StringIO()
    async with serve_gateway(
        str(path),
        str(token),
        device_port=0,
        port=0,
        lan_listen=f"{NAME}:{lan_port}",
        lan_recheck=0.05,
        resolver=resolver,
        log_stream=stream,
    ) as running:
        assert running.lan == f"{NAME} (127.0.0.1:{lan_port})"
        for _ in range(100):
            if moved_to in running.lan:
                break
            await asyncio.sleep(0.02)
        assert running.lan == f"{NAME} ({moved_to}:{lan_port})"
        reader, writer = await asyncio.open_connection(
            moved_to, lan_port, ssl=client_context(directory), server_hostname=NAME
        )
        message = hello("dev-1")
        message["token"] = TOKEN
        assert (await exchange(reader, writer, message))["ok"]
        writer.close()
    events = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
    assert "lan_resolve_failed" in events and "lan_rebound" in events
