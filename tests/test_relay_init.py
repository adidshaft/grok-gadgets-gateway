"""`relay init` and the full preflight (GW-BUILD-003, hub ADR 013 §1)."""

import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from grok_gadgets_gateway import relay_check, relay_init
from grok_gadgets_gateway.cli import main
from grok_gadgets_gateway.operator import OperatorError

MCP = "gadgets.example.net"
DEVICES = "devices.example.net"


def test_systemd_files_hold_the_hosts_and_no_secret(tmp_path):
    files = relay_init.write_files(tmp_path / "relay", mcp_host=MCP, device_host=DEVICES)
    names = sorted(p.name for p in files)
    assert names == ["NEXT-STEPS.md", "cloud-init.yaml", "setup-relay.sh"]
    script = (tmp_path / "relay" / "setup-relay.sh").read_text()
    cloud = (tmp_path / "relay" / "cloud-init.yaml").read_text()
    steps = (tmp_path / "relay" / "NEXT-STEPS.md").read_text()
    assert cloud.startswith("#cloud-config\n") and "setup-relay.sh" in cloud
    for text in (script, cloud):
        assert f"grok-gadgets-gateway=={relay_init.gateway_release()}" in text
        assert 'GATEWAY="${GATEWAY:-grok-gadgets-gateway==' in text
        assert "ufw allow 8444/tcp" in text and "8443" not in text
        assert "ufw allow 22/tcp" in text and "ufw allow OpenSSH" not in text
        assert "/root/.ssh/authorized_keys" in text
        assert MCP in text and DEVICES in text and "--accounts" in text
        assert "service install" in text and "enable-linger" in text
    # No secret is ever written: tunnel credentials and tokens go by hand.
    for text in (script, cloud, steps):
        for secret in ("TUNNEL_TOKEN=", "credentials-file:", "gga_", "BEGIN PRIVATE"):
            assert secret not in text
    assert "init --client http > /dev/null" in script  # init prints a token; keep it out of logs.
    assert "relay-check" in steps and "DNS only" in steps and "scp" in steps
    subprocess.run(["bash", "-n", str(tmp_path / "relay" / "setup-relay.sh")], check=True)
    assert stat.S_IMODE((tmp_path / "relay" / "setup-relay.sh").stat().st_mode) == 0o755


def test_compose_files(tmp_path):
    relay_init.write_files(
        tmp_path / "c", mcp_host=MCP, device_host=DEVICES, mode="compose", accounts=False
    )
    compose = (tmp_path / "c" / "compose.yaml").read_text()
    assert "8444:8444" in compose and "8766:" not in compose and "--accounts" not in compose
    assert (
        f"grok-gadgets-gateway=={relay_init.gateway_release()}"
        in (tmp_path / "c" / "Dockerfile").read_text()
    )
    assert (tmp_path / "c" / ".env").read_text() == f"MCP_HOST={MCP}\n"
    config = (tmp_path / "c" / "cloudflared" / "config.yml.example").read_text()
    assert f"hostname: {MCP}" in config and "http://127.0.0.1:8766" in config
    assert "docker" in (tmp_path / "c" / "setup-relay.sh").read_text()


def test_relay_init_refuses_bad_input(tmp_path):
    for bad in (
        {"mcp_host": "not a name", "device_host": DEVICES},
        {"mcp_host": MCP, "device_host": "192.0.2.1"},
        {"mcp_host": MCP, "device_host": MCP},
        {"mcp_host": MCP, "device_host": DEVICES, "port": 8443},
        {"mcp_host": MCP, "device_host": DEVICES, "mode": "kubernetes"},
    ):
        with pytest.raises(OperatorError):
            relay_init.write_files(tmp_path / "x", **bad)
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "f").write_text("x")
    with pytest.raises(OperatorError, match="not empty"):
        relay_init.write_files(tmp_path / "used", mcp_host=MCP, device_host=DEVICES)


def test_relay_init_cli(tmp_path, capsys):
    out = tmp_path / "relay"
    assert (
        main(["relay", "init", "--mcp-host", MCP, "--device-host", DEVICES, "--out", str(out)]) == 0
    )
    assert "NEXT-STEPS.md" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["relay", "init", "--mcp-host", MCP, "--out", str(tmp_path / "y")])
    capsys.readouterr()


# --- the full preflight ---


class _Mcp(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/.well-known/oauth-protected-resource/mcp" else 404)
        self.end_headers()

    def do_POST(self):
        self.send_response(401)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def mcp_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Mcp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/mcp"
    server.shutdown()


def _silent_port():
    """A TCP port that accepts, reads and closes without answering: like a TLS listener."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            conn.recv(1024)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock


def _http_port():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Mcp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_full_check_reports_every_item(mcp_url):
    silent = _silent_port()
    port = silent.getsockname()[1]
    results = relay_check.full_check(
        "localhost",
        port,
        "CA",
        mcp_url=mcp_url,
        resolver=lambda name: ["203.0.113.7"],
        handshake=lambda name, p, ca: True,
        fingerprint=lambda ca: "AB:CD",
    )
    assert [ok for ok, _ in results] == [True] * 5
    text = "\n".join(line for _, line in results)
    assert "DNS-only" in text and "open" in text and "AB:CD" in text
    assert "refuses plain HTTP" in text and "401" in text
    silent.close()


def test_full_check_keeps_going_after_a_failure(mcp_url):
    http = _http_port()  # A proxy or web server on the device port answers HTTP.
    results = relay_check.full_check(
        "localhost",
        http.server_port,
        "CA",
        mcp_url=mcp_url.replace("/mcp", "/nope"),
        resolver=lambda name: ["104.16.1.1"],
        handshake=lambda name, p, ca: True,
        fingerprint=lambda ca: "AB:CD",
    )
    assert [ok for ok, _ in results] == [False, True, True, False, False]
    assert "proxied" in results[0][1] and "answers HTTP" in results[3][1]
    http.shutdown()


def test_relay_check_cli_exit_code(monkeypatch, capsys, mcp_url, tmp_path, private_home):
    assert main(["init", "--client", "http"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(relay_check, "resolve_all", lambda name: ["203.0.113.7"])
    monkeypatch.setattr(relay_check, "tls_handshake", lambda name, port, ca: True)
    silent = _silent_port()
    real = relay_check.socket.create_connection
    local = silent.getsockname()[1]
    monkeypatch.setattr(
        relay_check.socket,
        "create_connection",
        lambda addr, timeout=None, *rest: real(
            ("127.0.0.1", local) if addr[0] == DEVICES else addr, timeout, *rest
        ),
    )
    target = f"{DEVICES}:8444"
    assert main(["relay-check", target, "--mcp", mcp_url]) == 0
    out = capsys.readouterr().out
    assert out.count("ok  ") == 5 and "SHA-256" in out and "all 5 items" in out
    assert main(["relay-check", target, "--mcp", mcp_url + "x"]) == 1
    assert "FAIL" in capsys.readouterr().out
    silent.close()


def test_linger_hint_only_when_not_lingering(monkeypatch):
    import getpass

    from grok_gadgets_gateway import cli

    monkeypatch.setattr(getpass, "getuser", lambda: "no-such-user-for-linger-test")
    assert cli._lingering() is False
