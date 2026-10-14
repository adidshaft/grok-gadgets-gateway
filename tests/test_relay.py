"""Relay device listener and DNS names (hub ADR 009, GW-RELAY-001 and GW-RELAY-002).

The listener binds loopback in tests. Unlike the LAN listener it needs no private-peer
widening: 127.0.0.1 is not a home-network address, so every session here is an outside peer.
"""

import asyncio
import json
import plistlib
import socket
import ssl

import pytest
from cryptography import x509
from test_domain import hello
from test_lan import FakeSerialPort, Log, closed, exchange
from test_transports import TOKEN, write_credentials

from grok_gadgets_gateway import lan, relay_check
from grok_gadgets_gateway.domain import Gateway
from grok_gadgets_gateway.operator import OperatorError
from grok_gadgets_gateway.service import serve_gateway
from grok_gadgets_gateway.transport import Credentials, DeviceServer

NAME = "devices.example.net"
OUTSIDE = (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError)


@pytest.fixture
def pki(tmp_path):
    directory = tmp_path / "lan"
    lan.create_ca(directory)
    lan.issue_device_cert(NAME, directory)
    return directory


def client_context(directory):
    context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def start(tmp_path, pki, **kwargs):
    path = tmp_path / "auth.json"
    write_credentials(path)
    log = Log()
    gateway = Gateway()
    server = DeviceServer(
        gateway,
        Credentials(path),
        port=0,
        tls=lan.device_context(pki),
        log=log,
        public=True,
        **kwargs,
    )
    await server.start()
    return gateway, server, log


async def connect(server, context, *, token=TOKEN):
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", server.port, ssl=context, server_hostname=NAME
    )
    message = hello("dev-1")
    message["token"] = token
    return reader, writer, await exchange(reader, writer, message)


# Addresses and names


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.0.0.0", ("0.0.0.0", 8444)),
        ("203.0.113.7:9443", ("203.0.113.7", 9443)),
        ("[::]:8443", ("::", 8443)),
        ("[::]", ("::", 8444)),
        ("127.0.0.1:8443", ("127.0.0.1", 8443)),
    ],
)
def test_device_listen_takes_any_address(text, expected):
    assert lan.parse_device_listen(text) == expected


@pytest.mark.parametrize(
    "text", [NAME, "my-mac.local", "0.0.0.0:0", "0.0.0.0:70000", "[::", "[::]x", "::1:x"]
)
def test_device_listen_refuses_names_and_bad_ports(text):
    with pytest.raises(OperatorError):
        lan.parse_device_listen(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (NAME, (NAME, 8444)),
        (f"{NAME}:8443", (NAME, 8443)),
        ("Devices.Example.NET:9443", (NAME, 9443)),
        ("my-mac.local:8767", ("my-mac.local", 8767)),
        ("192.168.1.20:8767", ("192.168.1.20", 8767)),
    ],
)
def test_gateway_address_takes_a_dns_name(text, expected):
    assert lan.parse_gateway_address(text) == expected


@pytest.mark.parametrize(
    "text", ["8.8.8.8", "localhost", "bad_name.example.net", "example.123", f"{NAME}:0", ""]
)
def test_gateway_address_refuses_public_addresses_and_bad_names(text):
    with pytest.raises(OperatorError):
        lan.parse_gateway_address(text)


def test_device_certificate_names_only_the_dns_name(tmp_path, pki):
    cert = x509.load_pem_x509_certificate((pki / "device.pem").read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == [NAME]
    assert names.get_values_for_type(x509.IPAddress) == []
    assert cert.subject.rfc4514_string() == f"CN={NAME}"
    assert (pki / "device.key").stat().st_mode & 0o777 == 0o600
    assert lan.device_cert_name(pki) == NAME
    # It sits beside the LAN certificate, so one gateway can run both listeners.
    lan.issue_server_cert("192.168.50.10", pki)
    assert (pki / "server.pem").is_file() and (pki / "device.pem").is_file()


@pytest.mark.parametrize("name", ["my-mac.local", "192.168.1.20", "localhost", "bad name"])
def test_device_certificate_refuses_local_names_and_addresses(tmp_path, pki, name):
    with pytest.raises(OperatorError, match="DNS name"):
        lan.issue_device_cert(name, pki)


def test_device_certificate_needs_a_ca_and_a_private_key(tmp_path, pki):
    with pytest.raises(OperatorError, match="init"):
        lan.issue_device_cert(NAME, tmp_path / "none")
    with pytest.raises(OperatorError, match="device-cert"):
        lan.device_context(tmp_path / "none")
    (pki / "device.key").chmod(0o644)
    with pytest.raises(OperatorError, match="0600"):
        lan.device_context(pki)


def test_ca_rotation_removes_the_relay_certificate(pki):
    lan.create_ca(pki, rotate=True)
    assert not (pki / "device.pem").exists() and not (pki / "device.key").exists()


def test_relay_listener_needs_tls(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    with pytest.raises(ValueError, match="TLS"):
        DeviceServer(Gateway(), Credentials(path), host="0.0.0.0", public=True)


# Listener behaviour


async def test_outside_peer_session_logs_device_lines_without_tokens(tmp_path, pki):
    gateway, server, log = await start(tmp_path, pki)
    try:
        reader, writer, reply = await connect(server, client_context(pki))
        assert reply["ok"]
        assert gateway.devices["dev-1"]["connected"]
        writer.close()
        assert await closed(reader)
        await asyncio.sleep(0.05)
    finally:
        await server.close()
    events = [r["event"] for r in log.records]
    assert events == ["device_connected", "device_hello", "device_disconnected"]
    assert log.records[0]["peer"] == "127.0.0.1"
    assert all(not e.startswith("lan_") for e in events)
    assert TOKEN not in json.dumps(log.records)
    assert server.status()["public"] is True


async def test_wrong_token_is_rejected_and_logged_without_it(tmp_path, pki):
    _, server, log = await start(tmp_path, pki)
    wrong = "w" * 32
    try:
        reader, _, reply = await connect(server, client_context(pki), token=wrong)
        assert reply["error"]["code"] == "unauthorized"
        assert await closed(reader)
        await asyncio.sleep(0.05)
    finally:
        await server.close()
    rejected = log.events("device_auth_rejected")
    assert rejected and rejected[0]["device"] == "dev-1"
    assert wrong not in json.dumps(log.records) and TOKEN not in json.dumps(log.records)


async def test_wrong_ca_and_wrong_name_are_refused_by_the_device(tmp_path, pki):
    other = tmp_path / "other"
    lan.create_ca(other)
    _, server, log = await start(tmp_path, pki)
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            await connect(server, client_context(other))
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.open_connection(
                "127.0.0.1", server.port, ssl=client_context(pki), server_hostname="evil.example"
            )
        await asyncio.sleep(0.05)
        assert log.events("device_tls_failed")
    finally:
        await server.close()


async def test_plain_http_is_refused(tmp_path, pki):
    _, server, log = await start(tmp_path, pki)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"GET / HTTP/1.1\r\nHost: devices.example.net\r\n\r\n")
        await writer.drain()
        assert await closed(reader)
        await asyncio.sleep(0.05)
        assert log.events("device_tls_failed")
        assert not log.events("device_hello")
        # HTTP inside TLS (a proxy pointed at the port) is refused too.
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.port, ssl=client_context(pki), server_hostname=NAME
        )
        writer.write(b"GET / HTTP/1.1\n")
        await writer.drain()
        assert await closed(reader)
        await asyncio.sleep(0.05)
        assert log.events("device_http_rejected")
    finally:
        await server.close()


async def test_connection_rate_is_limited_and_logged_once_a_minute(tmp_path, pki):
    _, server, log = await start(tmp_path, pki, connects_per_minute=2)
    context = client_context(pki)
    try:
        for _ in range(2):
            _, writer, reply = await connect(server, context)
            assert reply["ok"]
            writer.close()
        for _ in range(3):
            with pytest.raises(OUTSIDE):
                await connect(server, context)
        await asyncio.sleep(0.05)
    finally:
        await server.close()
    assert len(log.events("device_rate_limited")) == 1
    floods = log.events("device_flood")
    assert len(floods) == 1 and floods[0]["reason"] == "connection_rate"


async def test_one_address_cannot_hold_every_pending_slot(tmp_path, pki):
    _, server, log = await start(tmp_path, pki, pending_per_address=2, handshake_timeout=2)
    try:
        idle = [await asyncio.open_connection("127.0.0.1", server.port) for _ in range(2)]
        third = await asyncio.open_connection("127.0.0.1", server.port)
        assert await closed(third[0])
        assert log.events("device_flood")[0]["reason"] == "pending_per_address"
        for _, writer in idle:
            writer.close()
        await asyncio.sleep(0.1)
        _, _, reply = await connect(server, client_context(pki))
        assert reply["ok"]
    finally:
        await server.close()


async def test_handshake_timeout_closes_a_silent_peer(tmp_path, pki):
    _, server, log = await start(tmp_path, pki, handshake_timeout=0.3)
    try:
        reader, _ = await asyncio.open_connection("127.0.0.1", server.port)
        assert await closed(reader)
        assert log.events("device_tls_failed")
    finally:
        await server.close()


# serve


def write_setup(tmp_path):
    path = tmp_path / "auth.json"
    write_credentials(path)
    token = tmp_path / "mcp-token"
    token.write_text("t" * 32)
    token.chmod(0o600)
    return str(path), str(token)


async def diagnostics(url):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx2.AsyncClient(headers={"Authorization": "Bearer " + "t" * 32}) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool("gadgets_diagnostics", {})
        return (result.structured_content or json.loads(result.content[0].text))["report"]


async def test_serve_runs_the_relay_listener_only_when_asked(tmp_path, pki):
    credentials, token = write_setup(tmp_path)
    async with serve_gateway(credentials, token, device_port=0, port=0) as running:
        assert running.relay is None
    port = free_port()
    log = tmp_path / "requests.jsonl"
    async with serve_gateway(
        credentials,
        token,
        device_port=0,
        port=0,
        device_listen=f"127.0.0.1:{port}",
        device_directory=pki,
        request_log=str(log),
    ) as running:
        assert running.relay == f"{NAME} on 127.0.0.1:{port}"
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=client_context(pki), server_hostname=NAME
        )
        message = hello("dev-1")
        message["token"] = TOKEN
        assert (await exchange(reader, writer, message))["ok"]
        relay = (await diagnostics(running.url))["device_listener"]["relay"]
        assert relay == {"name": NAME, "port": port, "connections": 1}
        writer.close()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    started = next(r for r in records if r["event"] == "serve_started")
    assert started["relay_listener"] == f"{NAME} on 127.0.0.1:{port}"
    assert any(r["event"] == "device_hello" for r in records)


async def test_serve_refuses_bad_relay_settings(tmp_path, pki):
    credentials, token = write_setup(tmp_path)
    with pytest.raises(OperatorError, match="IP address"):
        async with serve_gateway(credentials, token, port=0, device_listen=NAME):
            pass
    with pytest.raises(OperatorError, match="device-cert"):
        async with serve_gateway(
            credentials, token, port=0, device_listen="127.0.0.1", device_directory=tmp_path
        ):
            pass
    with pytest.raises(OperatorError, match="credentials"):
        async with serve_gateway(
            None, token, simulator=True, port=0, device_listen="127.0.0.1:8443"
        ):
            pass


# CLI


def test_cli_device_cert(tmp_path, monkeypatch, capsys):
    from grok_gadgets_gateway.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert main(["init", "--client", "http"]) == 0
    capsys.readouterr()
    assert main(["device-cert", "Devices.Example.net"]) == 0
    assert "--gateway devices.example.net" in capsys.readouterr().err
    assert lan.device_cert_name() == NAME
    with pytest.raises(SystemExit):
        main(["device-cert", "my-mac.local"])
    assert "lan-cert" in capsys.readouterr().err


def provision(tmp_path, monkeypatch, capsys, *extra):
    import builtins
    import getpass

    import serial

    from grok_gadgets_gateway.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if not (tmp_path / "grok-gadgets" / "credentials.json").exists():
        assert main(["init", "--client", "http"]) == 0
        assert main(["enroll", "dev-1"]) == 0
    capsys.readouterr()
    # Two status checks (before the prompt and before sending), then the provisioning reply.
    board = FakeSerialPort([b'{"ok":true,"device_id":"dev-1"}\n'] * 3)
    answers = ["home", "password1"]
    monkeypatch.setattr(builtins, "input", lambda prompt="": answers.pop(0))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": answers.pop(0))
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: board)
    monkeypatch.setattr(relay_check, "resolve_all", lambda name: ["203.0.113.7"])
    monkeypatch.setattr(relay_check, "tls_handshake", lambda name, port, ca: True)
    code = main(["wifi-provision", "/dev/cu.test", "--device", "dev-1", *extra])
    return code, board


def test_wifi_provision_stores_the_relay_name(tmp_path, monkeypatch, capsys):
    code, board = provision(tmp_path, monkeypatch, capsys, "--gateway", NAME)
    assert code == 0
    frame = json.loads(board.written[-1])
    assert frame["host"] == NAME and frame["port"] == 8444
    assert frame["ca"] == lan.ca_pem_text()
    err = capsys.readouterr().err
    assert f"for {NAME}:8444" in err and "password1" not in err
    assert f"ok  {NAME} is a DNS-only record" in err


def test_wifi_provision_pins_a_relay_ca_from_a_file(tmp_path, monkeypatch, capsys):
    relay = tmp_path / "relay"
    lan.create_ca(relay)
    lan.issue_device_cert(NAME, relay)
    code, board = provision(
        tmp_path, monkeypatch, capsys, "--gateway", f"{NAME}:9443", "--ca", str(relay / "ca.pem")
    )
    assert code == 0
    frame = json.loads(board.written[-1])
    assert frame["port"] == 9443 and frame["ca"] == (relay / "ca.pem").read_text()
    for bad, message in ((relay / "device.pem", "server certificate"), (relay, "not a PEM")):
        with pytest.raises(SystemExit):
            provision(tmp_path, monkeypatch, capsys, "--gateway", NAME, "--ca", str(bad))
        assert message in capsys.readouterr().err


def test_wifi_provision_needs_one_place_to_connect(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        provision(tmp_path, monkeypatch, capsys)
    assert "--gateway NAME[:PORT]" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        provision(tmp_path, monkeypatch, capsys, "--gateway", NAME, "--lan", "192.168.1.20")
    assert "not allowed with" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        provision(tmp_path, monkeypatch, capsys, "--gateway", "8.8.8.8")
    assert "public address" in capsys.readouterr().err


def test_service_install_runs_the_relay_listener(private_home, capsys):
    from test_background import Runner

    from grok_gadgets_gateway import cli
    from grok_gadgets_gateway.background import Service, serve_arguments

    assert serve_arguments(device_listen="0.0.0.0:8443")[-2:] == ["--device-listen", "0.0.0.0:8443"]
    service = Service("linux", private_home, Runner(), uid=501)
    parser = cli.build_parser()
    ports = [str(free_port()) for _ in range(3)]

    def install(*extra):
        args = parser.parse_args(
            ["service", "install", "--port", ports[0], "--device-port", ports[1], *extra]
        )
        return cli.run_service(parser, args, service)

    assert cli.main(["init"]) == 0
    with pytest.raises(SystemExit):
        install("--device-listen", f"127.0.0.1:{ports[2]}")  # No relay certificate yet.
    assert "device-cert" in capsys.readouterr().err
    assert cli.main(["device-cert", NAME]) == 0
    assert install("--device-listen", f"127.0.0.1:{ports[2]}") == 0
    command = service.unit_command()
    assert command[command.index("--device-listen") + 1] == f"127.0.0.1:{ports[2]}"
    lines = cli.route_status(service)
    assert f"relay device listener: 127.0.0.1:{ports[2]} for {NAME}" in lines


def test_macos_unit_keeps_the_relay_flag(tmp_path):
    from grok_gadgets_gateway.background import serve_arguments, unit_text

    command = ["/bin/gw", *serve_arguments(device_listen="0.0.0.0:8443")]
    plist = plistlib.loads(unit_text("macos", command, tmp_path).encode())
    assert plist["ProgramArguments"][-2:] == ["--device-listen", "0.0.0.0:8443"]
