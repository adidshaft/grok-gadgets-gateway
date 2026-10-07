"""Run the README quick start from this checkout, including the Grok Bot rehearsal.

    python3 scripts/check_first_success.py

The commands come from the README "Quick start" block, so the README cannot drift
(its `git clone` and `cd` lines are skipped: this checkout is the clone).
Uses a temporary config directory and free loopback ports. Needs uv.
Software only: a simulated light. It proves nothing about Grok Bot or hardware.
"""

import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def check(condition, message):
    if not condition:
        raise SystemExit("First success check failed: " + message)
    print("ok  " + message, flush=True)


def refused_without_token(port):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp", data=b"{}", headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        return error.code == 401
    return False


def main():
    steps = quick_start()
    with tempfile.TemporaryDirectory(prefix="grok-first-success-") as folder:
        env = {**os.environ, "XDG_CONFIG_HOME": folder}
        port, device_port = free_port(), free_port()
        serve = None
        rehearsed = None
        try:
            for step in steps:
                if step[-2:] == ["serve", "--simulator"]:
                    command = [*step, "--port", str(port), "--device-port", str(device_port)]
                    serve = subprocess.Popen(command, cwd=ROOT, env=env)
                    wait_for_port(port, serve)
                    check(
                        refused_without_token(port), "a request without the bearer token is refused"
                    )
                elif step[-1] == "rehearse":
                    rehearsed = subprocess.run(
                        [*step, "--port", str(port)],
                        cwd=ROOT,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                    print(rehearsed.stdout, end="", flush=True)
                else:
                    subprocess.run(step, cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
            check(serve is not None, "README quick start ran init and serve --simulator")
            check(rehearsed is not None, "README quick start runs the Grok Bot rehearsal")
            check(rehearsed.returncode == 0, "rehearse exits 0")
            output = rehearsed.stdout
            check("six tools Grok Bot will use" in output, "the gateway offers the six tools")
            check("sim-c124 (simulated)" in output, "the simulated light sim-c124 is listed")
            check("rgb.set blue -> executed" in output, "gadgets_command rgb.set returns executed")
            check('"b": 255' in output, "gadgets_get_state reports the new colour")
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
