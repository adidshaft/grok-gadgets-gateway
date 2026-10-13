"""Run `serve` as a per-user background service (hub ADR 006).

macOS: a launchd user agent in ~/Library/LaunchAgents. Linux: a systemd user unit in
~/.config/systemd/user. No root, no system files. Config, CA and tokens are never touched.
"""

import os
import plistlib
import shlex
import socket
import subprocess
import sys
from pathlib import Path

from .operator import OperatorError

LABEL = "org.grokgadgets.gateway"
UNIT = "grok-gadgets-gateway.service"


def platform_name(system=None):
    system = system or sys.platform
    if system == "darwin":
        return "macos"
    if system.startswith("linux"):
        return "linux"
    raise OperatorError("service supports macOS (launchd) and Linux (systemd) only")


def log_dir(platform, home=None):
    home = Path(home or Path.home())
    if platform == "macos":
        return home / "Library" / "Logs" / "grok-gadgets"
    state = os.environ.get("XDG_STATE_HOME")
    return (Path(state) if state else home / ".local" / "state") / "grok-gadgets"


def unit_path(platform, home=None):
    home = Path(home or Path.home())
    if platform == "macos":
        return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    config = os.environ.get("XDG_CONFIG_HOME")
    return (Path(config) if config else home / ".config") / "systemd" / "user" / UNIT


def serve_arguments(
    lan_listen=None, port=8766, device_port=8765, request_log=None, allowed_hosts=(), oauth=False
):
    """The `serve` flags the service runs with; the same rules as a foreground `serve`."""
    args = ["serve", "--port", str(port), "--device-port", str(device_port)]
    if lan_listen:
        args += ["--lan-listen", lan_listen]
    for host in allowed_hosts:
        args += ["--allowed-host", host]
    if oauth:
        args.append("--oauth")
    if request_log:
        args += ["--request-log", str(request_log)]
    return args


def unit_text(platform, command, logs, environment=None):
    """The launchd plist or systemd unit for `command` (a list: executable and arguments)."""
    log = logs / "gateway.log"
    if platform == "macos":
        plist = {
            "Label": LABEL,
            "ProgramArguments": list(command),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
            # Not "Background": macOS throttles background jobs, and at boot that delayed
            # serve by about 90 s on the owner's Mac. Devices wait on this process.
            "ProcessType": "Standard",
        }
        if environment:
            plist["EnvironmentVariables"] = dict(environment)
        return plistlib.dumps(plist).decode()
    lines = [
        "[Unit]",
        "Description=Grok Gadgets gateway (per-user)",
        "After=network-online.target",
        "",
        "[Service]",
        "ExecStart=" + shlex.join(command),
        "Restart=on-failure",
        "RestartSec=5",
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
    ]
    for key, value in sorted((environment or {}).items()):
        lines.append(f"Environment={shlex.quote(f'{key}={value}')}")
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def port_free(host, port):
    with socket.socket() as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def run(command):
    return subprocess.run(command, capture_output=True, text=True, check=False)


class Service:
    """Install, remove and inspect the per-user service. `runner` runs launchctl/systemctl."""

    def __init__(self, platform=None, home=None, runner=run, uid=None):
        self.platform = platform or platform_name()
        self.home = Path(home or Path.home())
        self.runner = runner
        self.uid = os.getuid() if uid is None else uid
        self.path = unit_path(self.platform, self.home)
        self.logs = log_dir(self.platform, self.home)

    @property
    def target(self):
        return f"gui/{self.uid}"

    def _check(self, command, what):
        result = self.runner(command)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise OperatorError(f"{what} failed: {detail[-1] if detail else result.returncode}")
        return result

    def installed(self):
        return self.path.is_file()

    def install(self, command, environment=None, ports=()):
        if self.installed():
            raise OperatorError(
                f"already installed at {self.path}. Run: grok-gadgets-gateway service uninstall"
            )
        for host, port in ports:
            if not port_free(host, port):
                raise OperatorError(
                    f"{host}:{port} is in use. Another gateway may be running; stop it first "
                    "(Ctrl-C in its terminal), then install the service."
                )
        self.logs.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(unit_text(self.platform, command, self.logs, environment))
        if self.platform == "macos":
            self._check(["launchctl", "bootstrap", self.target, str(self.path)], "launchctl")
        else:
            self._check(["systemctl", "--user", "daemon-reload"], "systemctl")
            self._check(["systemctl", "--user", "enable", "--now", UNIT], "systemctl")
        return self.path

    def uninstall(self):
        if not self.installed():
            raise OperatorError("the service is not installed")
        if self.platform == "macos":
            self.runner(["launchctl", "bootout", f"{self.target}/{LABEL}"])
        else:
            self.runner(["systemctl", "--user", "disable", "--now", UNIT])
        self.path.unlink()
        if self.platform == "linux":
            self.runner(["systemctl", "--user", "daemon-reload"])
        return self.path

    def status(self):
        """A dict: installed, running, pid (if known), unit path and log file."""
        report = {
            "installed": self.installed(),
            "running": False,
            "pid": None,
            "unit": str(self.path),
            "log": str(self.logs / "gateway.log"),
        }
        if not report["installed"]:
            return report
        if self.platform == "macos":
            result = self.runner(["launchctl", "print", f"{self.target}/{LABEL}"])
            for line in result.stdout.splitlines():
                # Only top-level properties: nested blocks repeat keys such as `state = active`.
                if not line.startswith("\t") or line.startswith("\t\t"):
                    continue
                key, _, value = line.strip().partition(" = ")
                if key == "state":
                    report["running"] = value == "running"
                elif key == "pid" and value.isdigit():
                    report["pid"] = int(value)
        else:
            result = self.runner(["systemctl", "--user", "show", UNIT, "-p", "ActiveState,MainPID"])
            for line in result.stdout.splitlines():
                key, _, value = line.partition("=")
                if key == "ActiveState":
                    report["running"] = value == "active"
                elif key == "MainPID" and value.isdigit() and value != "0":
                    report["pid"] = int(value)
        return report

    def tail(self, lines=40):
        log = self.logs / "gateway.log"
        if not log.is_file():
            return []
        return log.read_text(errors="replace").splitlines()[-lines:]
