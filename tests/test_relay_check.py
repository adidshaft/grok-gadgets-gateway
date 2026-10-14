"""GW-RELAY-004: the relay preflight refuses a proxied DNS record and checks the relay CA."""

import asyncio
import socket
import ssl

import pytest

from grok_gadgets_gateway import lan, relay_check
from grok_gadgets_gateway.cli import main
from grok_gadgets_gateway.operator import OperatorError

NAME = "devices.example.net"


@pytest.mark.parametrize("address", ["104.16.1.1", "172.67.10.10", "2606:4700::1111"])
def test_a_proxied_record_is_refused(address):
    with pytest.raises(OperatorError, match="proxied. Set the record to DNS only"):
        relay_check.check_dns(NAME, lambda _name: ["203.0.113.7", address])


def test_a_dns_only_record_passes():
    assert relay_check.check_dns(NAME, lambda _name: ["203.0.113.7"]) == ["203.0.113.7"]


def test_a_missing_record_is_refused():
    def fail(_name):
        raise socket.gaierror("no such name")

    with pytest.raises(OperatorError, match="does not resolve"):
        relay_check.check_dns(NAME, fail)
    with pytest.raises(OperatorError, match="does not resolve"):
        relay_check.check_dns(NAME, lambda _name: [])


def test_tls_failures_name_the_cause():
    def wrong_ca(*_args):
        raise ssl.SSLCertVerificationError("unknown ca")

    def refused(*_args):
        raise ConnectionRefusedError

    with pytest.raises(OperatorError, match="the relay CA did not issue"):
        relay_check.check_tls(NAME, 8444, "ca", wrong_ca)
    with pytest.raises(OperatorError, match="use 8443, so pass NAME:8443"):
        relay_check.check_tls(NAME, 8444, "ca", refused)


async def test_the_handshake_trusts_only_the_relay_ca(tmp_path, monkeypatch):
    relay, other = tmp_path / "relay", tmp_path / "other"
    lan.create_ca(relay)
    lan.issue_device_cert(NAME, relay)
    lan.create_ca(other)
    context = lan.device_context(relay)

    async def answer(_reader, writer):
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    real = socket.create_connection
    monkeypatch.setattr(
        relay_check.socket, "create_connection", lambda _addr, timeout: real(("127.0.0.1", port))
    )
    async with server:
        ca = (relay / "ca.pem").read_text()
        assert await asyncio.to_thread(relay_check.tls_handshake, NAME, port, ca)
        wrong = (other / "ca.pem").read_text()
        with pytest.raises(OperatorError, match="did not issue"):
            await asyncio.to_thread(relay_check.check_tls, NAME, port, wrong)


def test_relay_check_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert main(["init", "--client", "http"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(relay_check, "tls_handshake", lambda name, port, ca: True)
    monkeypatch.setattr(relay_check, "resolve_all", lambda name: ["203.0.113.7"])
    assert main(["relay-check", NAME]) == 0
    out = capsys.readouterr().out
    assert f"ok  {NAME}:8444 presents a certificate" in out and "Relay check passed" in out
    monkeypatch.setattr(relay_check, "resolve_all", lambda name: ["104.16.1.1"])
    assert main(["relay-check", f"{NAME}:8443"]) == 1
    assert "DNS only" in capsys.readouterr().err
