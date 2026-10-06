"""Run the README quick start from this checkout, then drive it with MCP Inspector.

    python3 scripts/check_first_success.py

The commands come from the README "Quick start" block, so the README cannot drift
(its `git clone` and `cd` lines are skipped: this checkout is the clone).
Uses a temporary config directory and free loopback ports. Needs uv and Node.js 22+.
Software only: a simulated light. It proves nothing about Grok Bot or hardware.
"""

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSPECTOR = "@modelcontextprotocol/inspector@2.9.0"
TOOLS = {
    "gadgets_list_devices",
    "gadgets_get_state",
    "gadgets_command",
    "gadgets_command_status",
    "gadgets_read_events",
    "gadgets_diagnostics",
}


def quick_start():
    readme = (ROOT / "README.md").read_text()
    match = re.search(r"## Quick start\n.*?```sh\n(.*?)```", readme, re.S)
    if not match:
        raise SystemExit("README has no Quick start sh block")
    # The clone already exists: this script runs inside it.
    lines = [line for line in match[1].splitlines() if line.strip()]
    return [shlex.split(line) for line in lines if not line.startswith(("git clone", "cd "))]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port, process, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"serve exited early with code {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit("serve did not open its MCP port")


class Inspector:
    def __init__(self, url, token, env):
        self.base = ["npx", "-y", INSPECTOR, "--cli", url, "--transport", "http"]
        self.header = ["--header", "Authorization: Bearer " + token]
        self.env = env

    def run(self, *args, authorized=True):
        command = [*self.base, *(self.header if authorized else []), *args, "--format", "json"]
        return subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=180)

    def call(self, tool, arguments=None):
        args = ["--method", "tools/call", "--tool-name", tool]
        if arguments is not None:
            args += ["--tool-args-json", json.dumps(arguments)]
        result = self.run(*args)
        if result.returncode:
            raise SystemExit(f"Inspector call {tool} failed:\n{result.stdout}{result.stderr}")
        payload = json.loads(result.stdout)["result"]
        if payload.get("isError"):
            raise SystemExit(f"{tool} returned an MCP error: {payload}")
        return json.loads(payload["content"][0]["text"])


def check(condition, message):
    if not condition:
        raise SystemExit("First success check failed: " + message)
    print("ok  " + message, flush=True)


def main():
    steps = quick_start()
    with tempfile.TemporaryDirectory(prefix="grok-first-success-") as folder:
        env = {**os.environ, "XDG_CONFIG_HOME": folder}
        port, device_port = free_port(), free_port()
        serve = None
        try:
            for step in steps:
                if step[-2:] == ["serve", "--simulator"]:
                    command = [*step, "--port", str(port), "--device-port", str(device_port)]
                    serve = subprocess.Popen(command, cwd=ROOT, env=env)
                    wait_for_port(port, serve)
                else:
                    subprocess.run(step, cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
            check(serve is not None, "README quick start ran init and serve --simulator")
            token = (Path(folder) / "grok-gadgets/mcp-token").read_text().strip()
            inspector = Inspector(f"http://127.0.0.1:{port}/mcp", token, env)

            denied = inspector.run("--method", "tools/list", authorized=False)
            check(denied.returncode != 0, "a request without the bearer token is refused")
            listed = inspector.run("--method", "tools/list")
            names = {tool["name"] for tool in json.loads(listed.stdout)["result"]["tools"]}
            check(names == TOOLS, "MCP Inspector lists the six gadgets tools")

            devices = inspector.call("gadgets_list_devices")["devices"]
            check(
                [d["device_id"] for d in devices] == ["sim-c124"] and devices[0]["simulated"],
                "gadgets_list_devices shows the simulated light sim-c124",
            )
            rgb = {"r": 255, "g": 120, "b": 0, "on": True}
            command = inspector.call(
                "gadgets_command",
                {"device_id": "sim-c124", "capability": "rgb.set", "arguments": rgb},
            )["command"]
            check(command["status"] == "executed", "gadgets_command rgb.set returns executed")
            state = inspector.call("gadgets_get_state", {"device_id": "sim-c124"})["device"]
            check(state["state"]["rgb"] == rgb, "gadgets_get_state reports the new colour")
        finally:
            if serve is not None and serve.poll() is None:
                serve.send_signal(signal.SIGINT)
                try:
                    serve.wait(20)
                except subprocess.TimeoutExpired:
                    serve.kill()
    print("First success path passed (simulated device; no Grok Bot or hardware).")


if __name__ == "__main__":
    sys.exit(main())
