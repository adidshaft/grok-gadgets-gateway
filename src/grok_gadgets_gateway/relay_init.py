"""`relay init` (GW-BUILD-003, hub ADR 013 §1): the files for a new relay, with no secret.

It writes `setup-relay.sh` (the whole host setup, run as root), `cloud-init.yaml` (runs that
script on a fresh Ubuntu host) and `NEXT-STEPS.md` (what the builder does by hand). The compose
mode adds `compose.yaml`, a `Dockerfile`, `.env` and a tunnel config example. The builder adds
the tunnel's credentials on the host; no generated file holds a token, key or password.
"""

import os
import shlex
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .lan import is_dns_name
from .operator import OperatorError

DEFAULT_PORT = 8444
USER = "grokgadgets"
UV_VERSION = "0.12.3"
CLOUDFLARED_VERSION = "2026.7.3"
MODES = ("systemd", "compose")


def gateway_release():
    try:
        return version("grok-gadgets-gateway")
    except PackageNotFoundError:  # pragma: no cover - only in a raw source tree
        return "0.1.0a17"


def _check(mcp_host, device_host, port, mode):
    for label, host in (("--mcp-host", mcp_host), ("--device-host", device_host)):
        if not is_dns_name(host):
            raise OperatorError(
                f"{label} must be a public DNS name, for example devices.example.net"
            )
    if mcp_host == device_host:
        raise OperatorError(
            "use two names: the MCP name goes through the tunnel, the device name is DNS only"
        )
    if port == 8443:
        raise OperatorError(
            "port 8443 is one Cloudflare proxies; a proxied record would break every device's "
            "CA pin. New relays use 8444"
        )
    if not 1024 <= port <= 65535 or port == 8766:
        raise OperatorError("the device port must be 1024-65535, and not 8766")
    if mode not in MODES:
        raise OperatorError(f"--mode must be one of {', '.join(MODES)}")


def _serve_flags(mcp_host, port, accounts):
    flags = ["--allowed-host", mcp_host, "--oauth"]
    if accounts:
        flags.append("--accounts")
    return [*flags, "--device-listen", f"0.0.0.0:{port}"]


def setup_script(mcp_host, device_host, port, accounts, mode, release):
    q = shlex.quote
    common = f"""#!/usr/bin/env bash
# Grok Gadgets relay setup, written by `grok-gadgets-gateway relay init` (hub ADR 013).
# Run as root on a fresh Ubuntu 24.04 host. It holds no secret; it is safe to read aloud.
set -euo pipefail
MCP_HOST={q(mcp_host)}
DEVICE_HOST={q(device_host)}
PORT={port}
RELAY_USER={USER}
export DEBIAN_FRONTEND=noninteractive

apt-get update -q
apt-get install -y -q curl ufw ca-certificates
ufw allow 22/tcp  # SSH; the OpenSSH profile is missing on minimal images
ufw allow {port}/tcp
ufw --force enable
"""
    if mode == "compose":
        return (
            common
            + """
apt-get install -y -q docker.io docker-compose-v2
id -u "$RELAY_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash -G docker "$RELAY_USER"
echo "Docker is ready. Copy this folder to /home/$RELAY_USER/relay and follow NEXT-STEPS.md."
"""
        )
    flags = " ".join(q(f) for f in _serve_flags(mcp_host, port, accounts))
    return (
        common
        + f"""
ARCH="$(dpkg --print-architecture)"
curl -fsSL -o /tmp/cloudflared.deb \\
  "https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}/cloudflared-linux-$ARCH.deb"
dpkg -i /tmp/cloudflared.deb

id -u "$RELAY_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$RELAY_USER"
# The keys that reach root also reach the relay user, for scp and enroll from your computer.
if [ -s /root/.ssh/authorized_keys ] && [ ! -s "/home/$RELAY_USER/.ssh/authorized_keys" ]; then
  install -d -m 700 -o "$RELAY_USER" -g "$RELAY_USER" "/home/$RELAY_USER/.ssh"
  install -m 600 -o "$RELAY_USER" -g "$RELAY_USER" /root/.ssh/authorized_keys \\
    "/home/$RELAY_USER/.ssh/authorized_keys"
fi
loginctl enable-linger "$RELAY_USER"
RUNTIME="/run/user/$(id -u "$RELAY_USER")"
for _ in $(seq 1 30); do [ -d "$RUNTIME" ] && break; sleep 1; done
as_relay() {{ su - "$RELAY_USER" -c "export XDG_RUNTIME_DIR=$RUNTIME; $1"; }}

as_relay "curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh | sh"
# GATEWAY may name a reviewed wheel to try a candidate; the default is this release.
GATEWAY="${{GATEWAY:-grok-gadgets-gateway=={release}}}"
as_relay "~/.local/bin/uv tool install $GATEWAY"
GW="~/.local/bin/grok-gadgets-gateway"
# init prints a local bearer token: keep it out of the cloud-init log.
as_relay "$GW init --client http > /dev/null"
as_relay "$GW device-cert $DEVICE_HOST"
as_relay "$GW service install {flags}"
as_relay "$GW lan-ca show"
echo "Relay set up for $MCP_HOST and $DEVICE_HOST:$PORT. Next: NEXT-STEPS.md, step 2."
"""
    )


def cloud_init(script):
    body = "".join(f"      {line}\n" if line else "\n" for line in script.splitlines())
    return (
        "#cloud-config\n"
        "# Written by `grok-gadgets-gateway relay init` (hub ADR 013). No secret inside: cloud\n"
        "# providers keep user data readable to anyone with the account.\n"
        "write_files:\n"
        "  - path: /root/setup-relay.sh\n"
        "    permissions: '0755'\n"
        "    content: |\n"
        f"{body}"
        "runcmd:\n"
        "  - [bash, /root/setup-relay.sh]\n"
    )


def next_steps(mcp_host, device_host, port, mode):
    host_check = (
        f"grok-gadgets-gateway relay-check {device_host}:{port} --ca relay-ca.pem "
        f"--mcp https://{mcp_host}/mcp"
    )
    if mode == "compose":
        start = f"""3. Copy this folder to the host, for example `scp -r . {USER}@HOST:relay`. On the host,
   put your tunnel's credentials file in `relay/cloudflared/` and copy
   `config.yml.example` to `config.yml` with the tunnel ID. Then:

   ```sh
   cd relay
   docker compose build
   docker compose run --rm gateway init --client http > /dev/null
   docker compose run --rm gateway device-cert {device_host}
   docker compose up -d
   docker compose cp gateway:/data/config/grok-gadgets/lan/ca.pem ./relay-ca.pem
   ```
"""
    else:
        start = f"""3. Create the tunnel in your tunnel provider. Route `{mcp_host}` to
   `http://127.0.0.1:8766`, nothing else. On the host, install the tunnel with the command the
   provider shows (`sudo cloudflared service install …`). Type it on the host only: the token
   in it is a secret. Then copy the relay's CA to your computer:

   ```sh
   scp {USER}@HOST:.config/grok-gadgets/lan/ca.pem relay-ca.pem
   ```
"""
    return f"""# Next steps for your relay

Written by `grok-gadgets-gateway relay init` for `{mcp_host}` (Grok Bot) and
`{device_host}:{port}` (gadgets). Mode: {mode}. These files hold no secret.

1. Create a small Ubuntu 24.04 host. Paste `cloud-init.yaml` as its user data, or run
   `sudo bash setup-relay.sh` on it. The script opens SSH and TCP {port} in the firewall.
   Also allow TCP {port} in your provider's firewall.
2. DNS: point `{device_host}` at the host's public address with a **DNS only** record (no
   proxy). Never send `{device_host}` through the tunnel.
{start}
4. From your computer, check everything at once:

   ```sh
   {host_check}
   ```

   Every line must start with `ok`.
5. Connect Grok Bot to `https://{mcp_host}/mcp` with OAuth sign-in, and approve the code on
   the host with `grok-gadgets-gateway oauth approve CODE --name grok-bot`.
6. Prepare your gadgets: `enroll --batch` and `factory-provision --batch` (see the relay host
   guide, "Prepare many gadgets at once").
"""


def compose_files(mcp_host, port, accounts, release):
    command = "".join(
        f"      - {flag}\n"
        for flag in [
            "serve",
            *_serve_flags("${MCP_HOST:?set MCP_HOST in .env}", port, accounts),
            "--request-log",
            "/data/logs/requests.jsonl",
        ]
    )
    compose = f"""# Grok Gadgets relay, written by `grok-gadgets-gateway relay init` (hub ADR 013).
# The MCP port 8766 is never published; the tunnel shares the gateway's network.
name: grok-gadgets-relay
services:
  gateway:
    build: .
    image: grok-gadgets-gateway:{release}
    restart: unless-stopped
    command:
{command}    volumes:
      - gateway-data:/data
    ports:
      - "{port}:{port}"
  cloudflared:
    image: cloudflare/cloudflared:{CLOUDFLARED_VERSION}
    restart: unless-stopped
    network_mode: "service:gateway"
    depends_on: [gateway]
    command: ["tunnel", "--no-autoupdate", "--config", "/etc/cloudflared/config.yml", "run"]
    volumes:
      - ./cloudflared:/etc/cloudflared:ro
volumes:
  gateway-data:
"""
    dockerfile = f"""FROM python:3.12-slim
RUN pip install --no-cache-dir grok-gadgets-gateway=={release} \\
 && useradd -m -u 10001 gateway && mkdir -p /data/logs /data/config \\
 && chown -R gateway /data
USER gateway
ENV XDG_CONFIG_HOME=/data/config HOME=/data
WORKDIR /data
ENTRYPOINT ["grok-gadgets-gateway"]
"""
    config = f"""# Copy to config.yml and put the tunnel's credentials file beside it as TUNNEL-ID.json.
# Keep both private: anyone with the credentials file can run your tunnel.
tunnel: TUNNEL-ID
ingress:
  - hostname: {mcp_host}
    service: http://127.0.0.1:8766
  - service: http_status:404
"""
    return {
        "compose.yaml": compose,
        "Dockerfile": dockerfile,
        ".env": f"MCP_HOST={mcp_host}\n",
        "cloudflared/config.yml.example": config,
    }


def write_files(out, *, mcp_host, device_host, mode="systemd", port=DEFAULT_PORT, accounts=True):
    """Write the relay files to a new folder; return their paths."""
    _check(mcp_host, device_host, port, mode)
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise OperatorError(f"{out} is not empty; use a new folder")
    release = gateway_release()
    script = setup_script(mcp_host, device_host, port, accounts, mode, release)
    files = {
        "setup-relay.sh": script,
        "cloud-init.yaml": cloud_init(script),
        "NEXT-STEPS.md": next_steps(mcp_host, device_host, port, mode),
    }
    if mode == "compose":
        files.update(compose_files(mcp_host, port, accounts, release))
    written = []
    for name, text in files.items():
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.chmod(path, 0o755 if name.endswith(".sh") else 0o644)
        written.append(path)
    return written
