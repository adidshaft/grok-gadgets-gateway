import plistlib
import shutil
import socket
import subprocess
from types import SimpleNamespace

import pytest

from grok_gadgets_gateway.background import (
    LABEL,
    UNIT,
    Service,
    platform_name,
    serve_arguments,
    unit_text,
)
from grok_gadgets_gateway.operator import OperatorError

COMMAND = ["/Users/me/my gadgets/.venv/bin/grok-gadgets-gateway", *serve_arguments("192.168.1.20")]


class Runner:
    def __init__(self, stdout="", returncode=0):
        self.calls, self.stdout, self.returncode = [], stdout, returncode

    def __call__(self, command):
        self.calls.append(command)
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr="boom")


def test_platforms():
    assert platform_name("darwin") == "macos" and platform_name("linux") == "linux"
    with pytest.raises(OperatorError, match="macOS"):
        platform_name("win32")


def test_serve_arguments_never_add_simulator_or_public_addresses():
    args = serve_arguments("192.168.1.20:8767", 8766, 8765, "/logs/requests.jsonl")
    assert args == [
        "serve",
        "--port",
        "8766",
        "--device-port",
        "8765",
        "--lan-listen",
        "192.168.1.20:8767",
        "--request-log",
        "/logs/requests.jsonl",
    ]
    assert "--simulator" not in serve_arguments()


def test_launchd_plist(tmp_path):
    text = unit_text("macos", COMMAND, tmp_path, {"XDG_CONFIG_HOME": "/cfg"})
    plist = plistlib.loads(text.encode())
    assert plist["Label"] == LABEL and plist["ProgramArguments"] == COMMAND
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(tmp_path / "gateway.log")
    assert plist["EnvironmentVariables"] == {"XDG_CONFIG_HOME": "/cfg"}
    if shutil.which("plutil"):
        path = tmp_path / "agent.plist"
        path.write_text(text)
        assert (
            subprocess.run(
                ["plutil", "-lint", str(path)], capture_output=True, check=False
            ).returncode
            == 0
        )


def test_systemd_unit_quotes_paths(tmp_path):
    text = unit_text("linux", COMMAND, tmp_path, {"XDG_CONFIG_HOME": "/my cfg"})
    lines = text.splitlines()
    assert "ExecStart='/Users/me/my gadgets/.venv/bin/grok-gadgets-gateway' serve" in text
    assert "Restart=on-failure" in lines and "WantedBy=default.target" in lines
    assert f"StandardOutput=append:{tmp_path / 'gateway.log'}" in lines
    assert "Environment='XDG_CONFIG_HOME=/my cfg'" in lines


def test_macos_install_status_uninstall(tmp_path):
    runner = Runner()
    service = Service("macos", tmp_path, runner, uid=501)
    path = service.install(COMMAND)
    assert path == tmp_path / "Library/LaunchAgents" / f"{LABEL}.plist" and path.is_file()
    assert runner.calls == [["launchctl", "bootstrap", "gui/501", str(path)]]
    assert (tmp_path / "Library/Logs/grok-gadgets").is_dir()
    with pytest.raises(OperatorError, match="already installed"):
        service.install(COMMAND)
    runner.stdout = "\tstate = running\n\tpid = 4242\n"
    assert service.status() | {"unit": None, "log": None} == {
        "installed": True,
        "running": True,
        "pid": 4242,
        "unit": None,
        "log": None,
    }
    service.uninstall()
    assert runner.calls[-1] == ["launchctl", "bootout", f"gui/501/{LABEL}"] and not path.exists()
    assert service.status()["installed"] is False
    with pytest.raises(OperatorError, match="not installed"):
        service.uninstall()


def test_linux_install_status_uninstall(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    runner = Runner()
    service = Service("linux", tmp_path, runner, uid=1000)
    path = service.install(COMMAND)
    assert path == tmp_path / ".config/systemd/user" / UNIT
    assert runner.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", UNIT],
    ]
    assert service.logs == tmp_path / ".local/state/grok-gadgets"
    runner.stdout = "ActiveState=failed\nMainPID=0\n"
    report = service.status()
    assert report["installed"] and not report["running"] and report["pid"] is None
    service.uninstall()
    assert runner.calls[-2:] == [
        ["systemctl", "--user", "disable", "--now", UNIT],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_install_refuses_a_taken_port_and_reports_tool_failures(tmp_path):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    with held:
        port = held.getsockname()[1]
        with pytest.raises(OperatorError, match="Another gateway may be running"):
            Service("macos", tmp_path, Runner()).install(COMMAND, ports=[("127.0.0.1", port)])
    assert not (tmp_path / "Library/LaunchAgents").exists()
    with pytest.raises(OperatorError, match="launchctl failed: boom"):
        Service("macos", tmp_path, Runner(returncode=5)).install(COMMAND)


def test_tail_reads_only_the_end_of_the_log(tmp_path):
    service = Service("macos", tmp_path, Runner())
    assert service.tail() == []
    service.logs.mkdir(parents=True)
    (service.logs / "gateway.log").write_text("\n".join(str(n) for n in range(100)))
    assert service.tail(3) == ["97", "98", "99"]


def test_cli_install_checks_setup_and_runs_serve(private_home, capsys):
    from grok_gadgets_gateway import cli

    runner = Runner()
    service = Service("macos", private_home, runner, uid=501)
    parser = cli.build_parser()

    def free():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return str(probe.getsockname()[1])

    mcp_port, device_port = free(), free()

    def install(*extra):
        args = parser.parse_args(
            ["service", "install", "--port", mcp_port, "--device-port", device_port, *extra]
        )
        return cli.run_service(parser, args, service)

    with pytest.raises(SystemExit):
        install()  # Not initialized.
    assert "Run: grok-gadgets-gateway init" in capsys.readouterr().err
    assert cli.main(["init"]) == 0
    with pytest.raises(SystemExit):
        install("--lan-listen", "192.168.1.20")  # No LAN certificate yet.
    assert "lan-cert 192.168.1.20" in capsys.readouterr().err
    assert install() == 0
    plist = plistlib.loads(service.path.read_bytes())
    args = plist["ProgramArguments"]
    assert args[args.index("serve") :][:5] == [
        "serve",
        "--port",
        mcp_port,
        "--device-port",
        device_port,
    ]
    assert "--request-log" in args and "--simulator" not in args
    assert plist["EnvironmentVariables"]["XDG_CONFIG_HOME"] == str(private_home / ".config")
